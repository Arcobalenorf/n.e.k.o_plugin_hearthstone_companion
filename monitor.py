from __future__ import annotations

import inspect
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
EventCallback = Callable[..., None]
StateCallback = Callable[..., None]
LIVE_STATE_MAX_AGE_SECONDS = 300.0
LIVE_CONTEXT_PUBLISH_INTERVAL_SECONDS = 0.5
BATTLEGROUNDS_SHOP_SETTLE_SECONDS = 0.5


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
        on_state: StateCallback | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._on_llm = on_llm
        self._on_status = on_status
        self._on_result = on_result
        self._on_event = on_event
        self._on_event_accepts_source_generation = self._callback_accepts_source_generation(
            on_event
        )
        self._on_state = on_state
        self._on_state_accepts_source_generation = (
            self._state_callback_accepts_source_generation(on_state)
        )
        self._parser = PowerLogParser()
        self._tailer = PowerLogTailer(
            PowerLogLocator(config.log_path), initial_read_max_bytes=config.initial_read_max_bytes
        )
        self._arbiter = CommentaryArbiter(config)
        self._status = RuntimeStatus()
        self._snapshot = GameSnapshot()
        self._last_notified_snapshot = GameSnapshot()
        self._next_state_publish_at = 0.0
        self._lock = threading.RLock()
        self._emission_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_count = 0
        self._source_generation = 0
        self._source_reset_preapplied = False
        self._live_context_generation: int | None = None
        self._bootstrap_complete = False
        self._state_ready_notified = False
        self._state_stale_notified = False
        self._shop_settle_signature: tuple[Any, ...] | None = None
        self._shop_settle_started_at = 0.0
        self._pending_recruit_emission: tuple[GameEvent, GameSnapshot] | None = None

    @staticmethod
    def _callback_accepts_source_generation(callback: EventCallback | None) -> bool:
        if callback is None:
            return False
        try:
            inspect.signature(callback).bind(None, None, None)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _state_callback_accepts_source_generation(
        callback: StateCallback | None,
    ) -> bool:
        if callback is None:
            return False
        try:
            inspect.signature(callback).bind(None, None)
        except (TypeError, ValueError):
            return False
        return True

    def _begin_source_generation_locked(self) -> None:
        self._source_generation += 1
        self._live_context_generation = None
        self._bootstrap_complete = False
        self._state_ready_notified = False
        self._state_stale_notified = False
        self._last_notified_snapshot = GameSnapshot()
        self._next_state_publish_at = 0.0
        self._arbiter.reset()
        self._shop_settle_signature = None
        self._shop_settle_started_at = 0.0
        self._pending_recruit_emission = None
        self._status.source_state = (
            "waiting_for_log" if self._status.monitor_running else "waiting"
        )
        self._status.resolved_log_path = ""
        self._status.source_modified_at = 0.0
        self._status.last_line_at = 0.0
        self._status.last_state_at = 0.0
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
        self._last_notified_snapshot = GameSnapshot()
        self._next_state_publish_at = 0.0
        self._begin_source_generation_locked()
        self._source_reset_preapplied = True

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
                    self._source_reset_preapplied = True

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

    def run_if_source_generation(
        self,
        expected_generation: int,
        expected_game_number: int,
        callback: Callable[[], Any],
        snapshot_valid: Callable[[GameSnapshot], bool] | None = None,
    ) -> tuple[bool, Any]:
        """Linearize a short delivery with the expected source and game."""
        with self._lock:
            if expected_generation != self._source_generation:
                return False, None
            if expected_game_number != self._snapshot.game_number:
                return False, None
            if snapshot_valid is not None and not snapshot_valid(self._snapshot):
                return False, None
            return True, callback()

    def try_capture(
        self, *, timeout_seconds: float = 0.05
    ) -> tuple[GameSnapshot, RuntimeStatus, int] | None:
        """Return a coherent view without making latency-sensitive callers wait on parsing."""
        acquired = self._lock.acquire(timeout=max(0.0, float(timeout_seconds)))
        if not acquired:
            return None
        try:
            return self._snapshot, replace(self._status), self._source_generation
        finally:
            self._lock.release()

    def _run(self, stop_event: threading.Event) -> None:
        with self._lock:
            self._status.monitor_running = True
        next_report = 0.0
        while not stop_event.is_set():
            batch = None
            state_ready = False
            state_stale = False
            state_resumed = False
            state_unavailable = False
            publish_state = False
            try:
                now = time.time()
                with self._lock:
                    batch = self._tailer.poll()
                    self._status.last_error_code = ""
                    if batch.source_reset:
                        # A rebuilt reader may wait through empty polls before
                        # its first concrete log appears.
                        source_reset_preapplied = self._source_reset_preapplied
                        self._source_reset_preapplied = False
                        self._parser.reset_source()
                        if not source_reset_preapplied:
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
                        finalize_packets = getattr(
                            self._parser,
                            "finalize_quiet_packet_baselines",
                            None,
                        )
                        if callable(finalize_packets):
                            finalize_packets(
                                now=now,
                                quiet_seconds=BATTLEGROUNDS_SHOP_SETTLE_SECONDS,
                            )
                        snapshot = self._settle_live_snapshot(
                            self._parser.snapshot(),
                            now=time.monotonic(),
                        )
                        emissions = [
                            (
                                event,
                                self._settled_batch_event_snapshot(
                                    event,
                                    event_snapshot,
                                    snapshot,
                                ),
                            )
                            for event, event_snapshot in emissions
                        ]
                        if batch.bootstrap:
                            self._pending_recruit_emission = None
                        else:
                            emissions = self._settle_recruit_emissions(
                                emissions,
                                snapshot,
                            )
                        state_changed = snapshot != self._snapshot
                        previous_game_number = self._snapshot.game_number
                        previous_active_snapshot = bool(
                            self._snapshot.game_number > 0
                            and self._snapshot.phase not in {"idle", "ended", "spectator"}
                        )
                        self._snapshot = snapshot
                        active_snapshot = bool(
                            snapshot.game_number > 0
                            and snapshot.phase not in {"idle", "ended", "spectator"}
                        )
                        if processed_lines:
                            activity_at = float(batch.modified_at or now)
                            self._status.last_line_at = min(now, max(0.0, activity_at))
                            self._state_stale_notified = False
                            if state_changed:
                                self._status.last_state_at = self._status.last_line_at
                        activity_at = self._status.last_state_at
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
                        state_waiting_to_publish = bool(
                            live_active_snapshot
                            and snapshot != self._last_notified_snapshot
                        )
                        publish_state = bool(
                            state_waiting_to_publish
                            and (state_ready or now >= self._next_state_publish_at)
                        )
                        if publish_state:
                            self._last_notified_snapshot = snapshot
                            self._next_state_publish_at = (
                                now + LIVE_CONTEXT_PUBLISH_INTERVAL_SECONDS
                            )
                        state_resumed = bool(
                            processed_lines
                            and not batch.bootstrap
                            and was_stale
                            and live_active_snapshot
                            and snapshot.game_number == previous_game_number
                            and not any(
                                event.kind == "game_started"
                                for event, _event_snapshot in emissions
                            )
                        )
                        if processed_lines and not batch.bootstrap and live_active_snapshot:
                            self._live_context_generation = self._source_generation
                        if not active_snapshot:
                            state_unavailable = bool(
                                state_changed
                                and previous_active_snapshot
                                and not any(
                                    event.kind
                                    in {"battlegrounds_game_ended", "game_ended"}
                                    for event, _event_snapshot in emissions
                                )
                            )
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
                if state_unavailable:
                    self._notify_event(
                        GameEvent(
                            "state_unavailable",
                            0,
                            "当前局势已离开可用对局",
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
                        suppress_commentary=state_resumed,
                    )
                if publish_state:
                    self._notify_state(
                        snapshot,
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
                    snapshot = self._snapshot
                    tick_generation = self._source_generation
                    self._status.source_state = "degraded"
                    self._status.last_error_code = code
                self._notify_event(
                    GameEvent(
                        "source_reset",
                        0,
                        "日志来源已重置",
                        time.time(),
                        {"reason": "monitor_error"},
                    ),
                    snapshot,
                    source_generation=tick_generation,
                )
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

    def _settle_live_snapshot(
        self,
        snapshot: GameSnapshot,
        *,
        now: float | None = None,
    ) -> GameSnapshot:
        """Require a short unchanged trailing window before a complete shop.

        Power.log has no transaction marker around a shop refresh. A tail poll can
        therefore end after the first new entity even though more slots are about
        to arrive. The parser keeps the observed cards, while this monitor-level
        gate prevents query and passive-context consumers from treating that
        transient prefix as the complete shop.
        """
        battlegrounds = snapshot.battlegrounds
        shop_area = (
            battlegrounds.areas.get("shop")
            if battlegrounds is not None
            else None
        )
        if (
            snapshot.mode != "battlegrounds"
            or snapshot.phase != "recruit"
            or battlegrounds is None
            or shop_area is None
            or not shop_area.complete
        ):
            self._shop_settle_signature = None
            self._shop_settle_started_at = 0.0
            return snapshot

        signature = (
            snapshot.game_number,
            battlegrounds.round,
            battlegrounds.phase,
            battlegrounds.shop,
            battlegrounds.frozen,
            shop_area.revision,
        )
        current_time = time.monotonic() if now is None else float(now)
        if (
            signature == self._shop_settle_signature
            and current_time - self._shop_settle_started_at
            >= BATTLEGROUNDS_SHOP_SETTLE_SECONDS
        ):
            return snapshot

        if signature != self._shop_settle_signature:
            self._shop_settle_signature = signature
            self._shop_settle_started_at = current_time
        areas = dict(battlegrounds.areas)
        areas["shop"] = replace(shop_area, complete=False)
        return replace(snapshot, battlegrounds=replace(battlegrounds, areas=areas))

    def _handle_event(self, event: GameEvent, snapshot: GameSnapshot, now: float) -> None:
        self._handle_batch([(event, snapshot)], now)

    def _handle_batch(
        self,
        emissions: list[tuple[GameEvent, GameSnapshot]],
        now: float,
        *,
        source_generation: int | None = None,
        suppress_commentary: bool = False,
    ) -> None:
        with self._emission_lock:
            with self._lock:
                if (
                    source_generation is not None
                    and source_generation != self._source_generation
                ):
                    return
            self._handle_batch_serial(
                emissions,
                now,
                source_generation=source_generation,
                suppress_commentary=suppress_commentary,
            )

    def _handle_batch_serial(
        self,
        emissions: list[tuple[GameEvent, GameSnapshot]],
        now: float,
        *,
        source_generation: int | None = None,
        suppress_commentary: bool = False,
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
                self._notify_event(
                    event,
                    snapshot,
                    source_generation=source_generation,
                )

        lifecycle_owned_batch = suppress_commentary or any(
            event.kind
            in {"game_started", "battlegrounds_game_ended", "game_ended"}
            for event, _snapshot in emissions
        )
        candidates = [] if lifecycle_owned_batch else [
            (event, snapshot)
            for event, snapshot in emissions
            if snapshot.phase != "spectator"
            and self._event_snapshot_ready_for_llm(event, snapshot)
            and self._arbiter.allow_llm(event, snapshot, now=now)
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
                self._notify_event(
                    event,
                    snapshot,
                    source_generation=source_generation,
                )

    @staticmethod
    def _snapshot_for_event(event: GameEvent, snapshot: GameSnapshot) -> GameSnapshot:
        battlegrounds = snapshot.battlegrounds
        if event.kind != "battlegrounds_combat_result" or battlegrounds is None:
            return snapshot
        event_round = int(event.details.get("round") or 0)
        if event_round <= 0 or event_round == battlegrounds.round:
            return snapshot
        return replace(snapshot, battlegrounds=replace(battlegrounds, round=event_round))

    @staticmethod
    def _settled_batch_event_snapshot(
        event: GameEvent,
        event_snapshot: GameSnapshot,
        batch_snapshot: GameSnapshot,
    ) -> GameSnapshot:
        if event.kind == "game_started":
            if (
                batch_snapshot.game_number == event_snapshot.game_number
                and batch_snapshot.game_number > 0
                and batch_snapshot.mode in {"constructed", "battlegrounds"}
                and batch_snapshot.phase not in {"idle", "ended", "spectator"}
            ):
                return batch_snapshot
            return event_snapshot

        if event.kind == "turn_started" and batch_snapshot.mode == "constructed":
            event_turn = int(event.details.get("turn") or 0)
            event_side = str(event.details.get("active_side") or "unknown")
            if (
                event_turn > 0
                and batch_snapshot.game_number == event_snapshot.game_number
                and batch_snapshot.turn == event_turn
                and batch_snapshot.active_side == event_side
                and batch_snapshot.phase == "playing"
            ):
                return batch_snapshot
            return event_snapshot

        expected_phase = {
            "battlegrounds_recruit_started": "recruit",
            "battlegrounds_combat_started": "combat",
        }.get(event.kind)
        event_round = int(event.details.get("round") or 0)
        battlegrounds = batch_snapshot.battlegrounds
        if (
            expected_phase is not None
            and event_round > 0
            and batch_snapshot.mode == "battlegrounds"
            and batch_snapshot.game_number == event_snapshot.game_number
            and batch_snapshot.phase == expected_phase
            and battlegrounds is not None
            and battlegrounds.round == event_round
            and battlegrounds.phase == expected_phase
        ):
            return batch_snapshot
        return event_snapshot

    @staticmethod
    def _event_snapshot_ready_for_llm(
        event: GameEvent,
        snapshot: GameSnapshot,
    ) -> bool:
        if event.kind != "battlegrounds_recruit_started":
            return True
        battlegrounds = snapshot.battlegrounds
        shop_area = (
            battlegrounds.areas.get("shop")
            if battlegrounds is not None
            else None
        )
        return bool(
            battlegrounds is not None
            and snapshot.phase == "recruit"
            and shop_area is not None
            and shop_area.complete
            and shop_area.round == battlegrounds.round
            and shop_area.phase == "recruit"
        )

    def _settle_recruit_emissions(
        self,
        emissions: list[tuple[GameEvent, GameSnapshot]],
        snapshot: GameSnapshot,
    ) -> list[tuple[GameEvent, GameSnapshot]]:
        ready: list[tuple[GameEvent, GameSnapshot]] = []
        for event, event_snapshot in emissions:
            if (
                event.kind == "battlegrounds_recruit_started"
                and not self._event_snapshot_ready_for_llm(event, event_snapshot)
            ):
                self._pending_recruit_emission = (event, event_snapshot)
                continue
            ready.append((event, event_snapshot))

        pending = self._pending_recruit_emission
        if pending is None:
            return ready
        event, event_snapshot = pending
        battlegrounds = snapshot.battlegrounds
        event_round = int(event.details.get("round") or 0)
        still_same_recruit = bool(
            snapshot.mode == "battlegrounds"
            and snapshot.phase == "recruit"
            and snapshot.game_number == event_snapshot.game_number
            and battlegrounds is not None
            and battlegrounds.round == event_round
            and battlegrounds.phase == "recruit"
        )
        if not still_same_recruit:
            self._pending_recruit_emission = None
            return ready
        if self._event_snapshot_ready_for_llm(event, snapshot):
            ready.append((event, snapshot))
            self._pending_recruit_emission = None
        return ready

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
                if self._on_event_accepts_source_generation:
                    self._on_event(event, snapshot, source_generation)
                else:
                    self._on_event(event, snapshot)
            except Exception as exc:
                with self._lock:
                    self._status.last_error_code = f"event:{type(exc).__name__}"
                self.logger.warning(
                    "Hearthstone companion event hook failed code=%s",
                    type(exc).__name__,
                )

    def _notify_state(
        self,
        snapshot: GameSnapshot,
        *,
        source_generation: int | None = None,
    ) -> None:
        if self._on_state is None:
            return
        with self._emission_lock:
            with self._lock:
                if (
                    source_generation is not None
                    and source_generation != self._source_generation
                ):
                    return
            try:
                if self._on_state_accepts_source_generation:
                    self._on_state(snapshot, source_generation)
                else:
                    self._on_state(snapshot)
            except Exception as exc:
                with self._lock:
                    self._status.last_error_code = f"state:{type(exc).__name__}"
                self.logger.warning(
                    "Hearthstone live-state hook failed code=%s",
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


__all__ = [
    "CompanionMonitor",
    "LIVE_STATE_MAX_AGE_SECONDS",
]
