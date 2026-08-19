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
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bootstrap_complete = False

    def _reset_reader_locked(self) -> None:
        self._parser = PowerLogParser()
        self._tailer = PowerLogTailer(
            PowerLogLocator(self.config.log_path),
            initial_read_max_bytes=self.config.initial_read_max_bytes,
        )
        self._snapshot = self._parser.snapshot()
        self._bootstrap_complete = False

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
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

    def update_config(self, config: CompanionConfig) -> None:
        with self._lock:
            source_changed = (
                config.log_path != self.config.log_path
                or config.initial_read_max_bytes != self.config.initial_read_max_bytes
            )
            self.config = config
            self._arbiter.update(config)
            if source_changed:
                self._reset_reader_locked()

    def snapshot(self) -> GameSnapshot:
        with self._lock:
            return self._snapshot

    def status(self) -> RuntimeStatus:
        with self._lock:
            return replace(self._status)

    def _run(self, stop_event: threading.Event) -> None:
        with self._lock:
            self._status.monitor_running = True
        next_report = 0.0
        while not stop_event.is_set():
            batch = None
            try:
                now = time.time()
                with self._lock:
                    batch = self._tailer.poll()
                    self._status.resolved_log_path = str(batch.path or "")
                    self._status.last_error_code = ""
                    if batch.source_reset:
                        self._parser.reset_source()
                    if batch.bootstrap:
                        self._bootstrap_complete = batch.bootstrap_complete
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
                        if self._parser.entity_capacity_exceeded:
                            self._status.last_error_code = "parser:entity_capacity_exceeded"
                    self._status.lines_seen += processed_lines
                    self._status.source_state = (
                        "waiting_for_log"
                        if batch.path is None
                        else "watching"
                        if self._bootstrap_complete
                        else "bootstrap_incomplete"
                    )
                    if processed_lines:
                        self._status.last_line_at = now

                if interrupted:
                    break
                if batch.source_reset:
                    self._notify_event(
                        GameEvent("source_reset", 0, "日志来源已重置", now, {}), snapshot
                    )
                if not batch.bootstrap and self._bootstrap_complete:
                    self._handle_batch(emissions, now)
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
        self, emissions: list[tuple[GameEvent, GameSnapshot]], now: float
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

    def _notify_event(self, event: GameEvent, snapshot: GameSnapshot) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, snapshot)
        except Exception as exc:
            with self._lock:
                self._status.last_error_code = f"event:{type(exc).__name__}"
            self.logger.warning("Hearthstone companion event hook failed code=%s", type(exc).__name__)

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


__all__ = ["CompanionMonitor"]
