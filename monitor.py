from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any, Callable

from .commentary import CommentaryArbiter, build_llm_prompt
from .config import CompanionConfig
from .models import GameEvent, GameSnapshot, RuntimeStatus
from .powerlog import PowerLogParser
from .tailer import PowerLogLocator, PowerLogTailer

OutputCallback = Callable[[str, GameEvent, GameSnapshot], bool]
StatusCallback = Callable[[dict[str, Any]], None]
ResultCallback = Callable[[GameEvent, GameSnapshot], None]
EventCallback = Callable[[GameEvent, GameSnapshot], None]
LIVE_STATE_MAX_AGE_SECONDS = 300.0


class CompanionMonitor:
    """Single-owner log reader and event processor.

    Parsing, state transitions, arbitration, and output submission all happen
    on one thread. Consumers only receive immutable snapshots and sanitized
    events.
    """

    def __init__(
        self,
        config: CompanionConfig,
        logger: Any,
        *,
        on_llm: OutputCallback,
        on_status: StatusCallback | None = None,
        on_result: ResultCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._on_llm = on_llm
        self._on_status = on_status
        self._on_result = on_result
        self._on_event = on_event
        self._parser = PowerLogParser()
        self._tailer = PowerLogTailer(
            PowerLogLocator(config.log_path), initial_read_max_bytes=config.initial_read_max_bytes
        )
        self._arbiter = CommentaryArbiter(config)
        self._status = RuntimeStatus()
        self._snapshot = GameSnapshot()
        self._lock = threading.RLock()
        self._emission_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_count = 0
        self._source_generation = 0
        self._live_context_generation: int | None = None
        self._bootstrap_complete = False
        self._state_ready_notified = False
        self._state_stale_notified = False

    def _begin_source_generation_locked(self) -> None:
        self._source_generation += 1
        self._live_context_generation = None
        self._bootstrap_complete = False
        self._state_ready_notified = False
        self._state_stale_notified = False
        self._status.source_state = (
            "waiting_for_log" if self._status.monitor_running else "waiting"
        )
        self._status.resolved_log_path = ""
        self._status.source_modified_at = 0.0
        self._status.last_line_at = 0.0
        self._status.last_event_at = 0.0
        self._status.last_event_kind = ""
        self._status.last_error_code = ""

    def _reset_reader_locked(self) -> None:
        self._parser = PowerLogParser()
        self._tailer = PowerLogTailer(
            PowerLogLocator(self.config.log_path),
            initial_read_max_bytes=self.config.initial_read_max_bytes,
        )
        self._snapshot = self._parser.snapshot()
        self._begin_source_generation_locked()

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            with self._lock:
                if self._start_count:
                    self._reset_reader_locked()
                self._start_count += 1
            stop_event = threading.Event()
            self._stop = stop_event
            self._thread = threading.Thread(
                target=self._run,
                args=(stop_event,),
                name="hearthstone-powerlog",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, timeout: float = 3.0) -> bool:
        with self._lifecycle_lock:
            stop_event = self._stop
            thread = self._thread
        stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
                    # Keep ownership and status changes in one lifecycle
                    # critical section so an old stop cannot overwrite a
                    # monitor generation started in between them.
                    with self._lock:
                        self._status.monitor_running = False
        return stopped

    def is_accepting(self) -> bool:
        with self._lifecycle_lock:
            return bool(
                self._thread is not None
                and self._thread.is_alive()
                and not self._stop.is_set()
            )

    def update_config(self, config: CompanionConfig) -> None:
        with self._emission_lock:
            with self._lock:
                source_changed = (
                    config.log_path != self.config.log_path
                    or config.initial_read_max_bytes != self.config.initial_read_max_bytes
                )
                staged_parser: PowerLogParser | None = None
                staged_tailer: PowerLogTailer | None = None
                staged_snapshot: GameSnapshot | None = None
                if source_changed:
                    staged_parser = PowerLogParser()
                    staged_tailer = PowerLogTailer(
                        PowerLogLocator(config.log_path),
                        initial_read_max_bytes=config.initial_read_max_bytes,
                    )
                    staged_snapshot = staged_parser.snapshot()
                self._arbiter.update(config)
                self.config = config
                if source_changed:
                    assert staged_parser is not None
                    assert staged_tailer is not None
                    assert staged_snapshot is not None
                    self._parser = staged_parser
                    self._tailer = staged_tailer
                    self._snapshot = staged_snapshot
                    self._begin_source_generation_locked()

    def snapshot(self) -> GameSnapshot:
        with self._lock:
            return self._snapshot

    def status(self) -> RuntimeStatus:
        with self._lock:
            return replace(self._status)

    def capture(self) -> tuple[GameSnapshot, RuntimeStatus, int]:
        """Return one immutable view of the current log-source generation."""
        with self._lock:
            return self._snapshot, replace(self._status), self._source_generation

    def _run(self, stop_event: threading.Event) -> None:
        with self._lock:
            self._status.monitor_running = True
        next_report = 0.0
        while not stop_event.is_set():
            batch = None
            state_ready = False
            state_stale = False
            state_resumed = False
            try:
                now = time.time()
                with self._lock:
                    batch = self._tailer.poll()
                    self._status.last_error_code = ""
                    if batch.source_reset:
                        self._parser.reset_source()
                        self._begin_source_generation_locked()
                    if batch.bootstrap:
                        self._bootstrap_complete = batch.bootstrap_complete
                    was_stale = self._state_stale_notified
                    emissions: list[tuple[GameEvent, GameSnapshot]] = []
                    processed_lines = 0
                    interrupted = False
                    for line in batch.lines:
                        if batch.bootstrap and stop_event.is_set():
                            interrupted = True
                            break
                        if (
                            not self._bootstrap_complete
                            and "CREATE_GAME" in line
                            and "PowerTaskList.DebugPrintPower" not in line
                        ):
                            self._parser.reset_source()
                            self._begin_source_generation_locked()
                            self._bootstrap_complete = True
                        line_events = self._parser.feed_line(line, now=now)
                        processed_lines += 1
                        if line_events:
                            line_snapshot = self._parser.snapshot()
                            emissions.extend(
                                (event, self._snapshot_for_event(event, line_snapshot))
                                for event in line_events
                            )
                    if interrupted:
                        self._reset_reader_locked()
                        snapshot = self._snapshot
                    else:
                        snapshot = self._parser.snapshot()
                        self._snapshot = snapshot
                        active_snapshot = bool(
                            snapshot.game_number > 0
                            and snapshot.phase not in {"idle", "ended", "spectator"}
                        )
                        if processed_lines:
                            activity_at = float(batch.modified_at or now)
                            self._status.last_line_at = min(now, max(0.0, activity_at))
                            self._state_stale_notified = False
                        activity_at = max(
                            self._status.last_line_at,
                            self._status.last_event_at,
                        )
                        live_active_snapshot = bool(
                            active_snapshot
                            and activity_at > 0
                            and now - activity_at <= LIVE_STATE_MAX_AGE_SECONDS
                        )
                        state_ready = bool(
                            batch.bootstrap
                            and self._bootstrap_complete
                            and not self._state_ready_notified
                            and live_active_snapshot
                        )
                        if state_ready:
                            self._state_ready_notified = True
                            self._live_context_generation = self._source_generation
                        state_resumed = bool(
                            processed_lines
                            and not batch.bootstrap
                            and was_stale
                            and live_active_snapshot
                        )
                        if processed_lines and not batch.bootstrap and live_active_snapshot:
                            self._live_context_generation = self._source_generation
                        if not active_snapshot:
                            self._live_context_generation = None
                            self._state_stale_notified = False
                        stale_active_snapshot = bool(
                            active_snapshot
                            and activity_at > 0
                            and now - activity_at > LIVE_STATE_MAX_AGE_SECONDS
                        )
                        if (
                            stale_active_snapshot
                            and not self._state_stale_notified
                            and self._live_context_generation == self._source_generation
                        ):
                            state_stale = True
                            self._state_stale_notified = True
                            self._live_context_generation = None
                        if self._parser.entity_capacity_exceeded:
                            self._status.last_error_code = "parser:entity_capacity_exceeded"
                    self._status.lines_seen += processed_lines
                    self._status.resolved_log_path = str(batch.path or "")
                    self._status.source_modified_at = float(batch.modified_at or 0.0)
                    self._status.source_state = (
                        "waiting_for_log"
                        if batch.path is None
                        else "watching"
                        if self._bootstrap_complete
                        else "bootstrap_incomplete"
                    )
                    tick_generation = self._source_generation
                if interrupted:
                    break
                if batch.source_reset:
                    self._notify_event(
                        GameEvent("source_reset", 0, "日志来源已重置", now, {}),
                        snapshot,
                        source_generation=tick_generation,
                    )
                if state_ready:
                    self._notify_event(
                        GameEvent(
                            "state_ready",
                            0,
                            "当前局势已就绪",
                            now,
                            {
                                "mode": snapshot.mode,
                                "phase": snapshot.phase,
                                "game_number": snapshot.game_number,
                            },
                        ),
                        snapshot,
                        source_generation=tick_generation,
                    )
                if state_stale:
                    self._notify_event(
                        GameEvent(
                            "state_stale",
                            0,
                            "当前日志局势已过期",
                            now,
                            {
                                "mode": snapshot.mode,
                                "phase": snapshot.phase,
                                "game_number": snapshot.game_number,
                            },
                        ),
                        snapshot,
                        source_generation=tick_generation,
                    )
                if state_resumed:
                    self._notify_event(
                        GameEvent(
                            "state_resumed",
                            0,
                            "当前局势已恢复实时更新",
                            now,
                            {
                                "mode": snapshot.mode,
                                "phase": snapshot.phase,
                                "game_number": snapshot.game_number,
                            },
                        ),
                        snapshot,
                        source_generation=tick_generation,
                    )
                if not batch.bootstrap and self._bootstrap_complete:
                    self._handle_batch(
                        emissions,
                        now,
                        source_generation=tick_generation,
                    )
                if not stop_event.is_set() and now >= next_report:
                    self._report()
                    next_report = now + 2.0
            except Exception as exc:  # keep the watcher recoverable without logging user data
                code = f"monitor:{type(exc).__name__}"
                with self._lock:
                    # poll() may advance its file cursor before raising, so
                    # even a failure before it returns invalidates the reader.
                    self._reset_reader_locked()
                    self._status.source_state = "degraded"
                    self._status.last_error_code = code
                try:
                    self.logger.warning("Hearthstone monitor tick failed code=%s", code)
                except Exception:
                    pass
            with self._lock:
                interval = self.config.poll_interval_seconds
            stop_event.wait(interval)

        with self._lock:
            self._status.monitor_running = False
            if self._status.source_state != "degraded":
                self._status.source_state = "stopped"
        self._report()

    def _handle_event(self, event: GameEvent, snapshot: GameSnapshot, now: float) -> None:
        self._handle_batch([(event, snapshot)], now)

    def _handle_batch(
        self,
        emissions: list[tuple[GameEvent, GameSnapshot]],
        now: float,
        *,
        source_generation: int | None = None,
    ) -> None:
        with self._emission_lock:
            with self._lock:
                if (
                    source_generation is not None
                    and source_generation != self._source_generation
                ):
                    return
            self._handle_batch_serial(emissions, now)

    def _handle_batch_serial(
        self,
        emissions: list[tuple[GameEvent, GameSnapshot]],
        now: float,
    ) -> None:
        if not emissions:
            return
        with self._lock:
            self._status.events_seen += len(emissions)
            self._status.last_event_at = now
            self._status.last_event_kind = emissions[-1][0].kind
            config = self.config

        for event, snapshot in emissions:
            if event.kind == "battlegrounds_game_ended" and self._on_result is not None:
                try:
                    self._on_result(event, snapshot)
                except Exception as exc:
                    with self._lock:
                        self._status.last_error_code = f"stats:{type(exc).__name__}"
                    self.logger.warning(
                        "Battlegrounds statistics update failed code=%s", type(exc).__name__
                    )

        terminal_kinds = {"battlegrounds_game_ended", "game_ended"}
        for event, snapshot in emissions:
            if event.kind not in terminal_kinds:
                self._notify_event(event, snapshot)

        candidates = [
            (event, snapshot)
            for event, snapshot in emissions
            if snapshot.phase != "spectator" and self._arbiter.allow_llm(event, snapshot, now=now)
        ]
        if candidates:
            _, (event, snapshot) = max(
                enumerate(candidates),
                key=lambda item: (item[1][0].priority, item[1][0].timestamp, item[0]),
            )
            prompt = build_llm_prompt(
                event,
                snapshot,
                max_reply_chars=config.llm_max_reply_chars,
            )
            if self._safe_output(self._on_llm, prompt, event, snapshot, "llm"):
                self._arbiter.mark_llm_submitted(event, snapshot, now=now)
                with self._lock:
                    self._status.llm_submissions += 1

        for event, snapshot in emissions:
            if event.kind in terminal_kinds:
                self._notify_event(event, snapshot)

    @staticmethod
    def _snapshot_for_event(event: GameEvent, snapshot: GameSnapshot) -> GameSnapshot:
        battlegrounds = snapshot.battlegrounds
        if event.kind != "battlegrounds_combat_result" or battlegrounds is None:
            return snapshot
        event_round = int(event.details.get("round") or 0)
        if event_round <= 0 or event_round == battlegrounds.round:
            return snapshot
        return replace(snapshot, battlegrounds=replace(battlegrounds, round=event_round))

    def _notify_event(
        self,
        event: GameEvent,
        snapshot: GameSnapshot,
        *,
        source_generation: int | None = None,
    ) -> None:
        if self._on_event is None:
            return
        with self._emission_lock:
            with self._lock:
                if (
                    source_generation is not None
                    and source_generation != self._source_generation
                ):
                    return
            try:
                self._on_event(event, snapshot)
            except Exception as exc:
                with self._lock:
                    self._status.last_error_code = f"event:{type(exc).__name__}"
                self.logger.warning(
                    "Hearthstone companion event hook failed code=%s",
                    type(exc).__name__,
                )

    def _safe_output(
        self,
        callback: OutputCallback,
        text: str,
        event: GameEvent,
        snapshot: GameSnapshot,
        channel: str,
    ) -> bool:
        try:
            return bool(callback(text, event, snapshot))
        except Exception as exc:
            code = f"{channel}:{type(exc).__name__}"
            with self._lock:
                self._status.last_error_code = code
            try:
                self.logger.warning("Hearthstone output failed channel=%s code=%s", channel, code)
            except Exception:
                pass
            return False

    def _report(self) -> None:
        if self._on_status is None:
            return
        try:
            runtime = self.status().to_dict()
            runtime.pop("resolved_log_path", None)
            self._on_status({"runtime": runtime})
        except Exception:
            pass


__all__ = ["CompanionMonitor", "LIVE_STATE_MAX_AGE_SECONDS"]
