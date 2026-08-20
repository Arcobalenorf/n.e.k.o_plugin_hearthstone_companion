from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import hearthstone_companion_under_test.monitor as monitor_module
import pytest
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.models import (
    BattlegroundsSnapshot,
    GameEvent,
    GameSnapshot,
)
from hearthstone_companion_under_test.monitor import CompanionMonitor
from hearthstone_companion_under_test.tailer import TailBatch

PREFIX = "D 12:00:00.0000000 GameState.DebugPrintPower() - "


def _line(payload: str) -> str:
    return PREFIX + payload


def _logger() -> SimpleNamespace:
    return SimpleNamespace(warning=lambda *args, **kwargs: None)


class _BatchSequence:
    def __init__(self, monitor: CompanionMonitor, batches: list[TailBatch]) -> None:
        self.monitor = monitor
        self.batches = list(batches)

    def poll(self) -> TailBatch:
        if self.batches:
            return self.batches.pop(0)
        self.monitor._stop.set()
        return TailBatch((), Path("Power.log"), bootstrap_complete=True)


def _run_bootstrap_batches(
    batches: list[TailBatch],
) -> tuple[CompanionMonitor, list[tuple[str, GameSnapshot]], list[str], list[str]]:
    observed: list[tuple[str, GameSnapshot]] = []
    llm_events: list[str] = []
    results: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(
            poll_interval_seconds=0.1,
            llm_commentary_enabled=True,
            llm_data_consent=True,
        ),
        _logger(),
        on_llm=lambda _prompt, event, _snapshot: not llm_events.append(event.kind),
        on_result=lambda event, _snapshot: results.append(event.kind),
        on_event=lambda event, snapshot: observed.append((event.kind, snapshot)),
    )
    monitor._tailer = _BatchSequence(monitor, batches)
    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while not monitor._stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor._stop.is_set()
    assert monitor.stop(timeout=2.0)
    return monitor, observed, llm_events, results


def test_monitor_never_generates_local_visible_commentary() -> None:
    llm_prompts: list[str] = []
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(llm_commentary_enabled=False, llm_data_consent=False),
        _logger(),
        on_llm=lambda prompt, event, snapshot: bool(llm_prompts.append(prompt)),
        on_event=lambda event, snapshot: observed.append(event.kind),
    )

    monitor._handle_event(
        GameEvent("battlegrounds_triple", 10, "三连合成成功", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
        100.0,
    )

    assert llm_prompts == []
    assert observed == ["battlegrounds_triple"]
    assert monitor.status().llm_submissions == 0


def test_active_constructed_bootstrap_notifies_state_ready_without_replaying_events() -> None:
    path = Path("constructed/Power.log")
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )

    monitor, observed, llm_events, results = _run_bootstrap_batches(
        [TailBatch(lines, path, bootstrap=True, source_reset=True, bootstrap_complete=True)]
    )

    assert [kind for kind, _snapshot in observed] == ["source_reset", "state_ready"]
    assert observed[-1][1].phase == "playing"
    assert llm_events == []
    assert results == []
    assert monitor.status().events_seen == 0
    assert monitor.status().llm_submissions == 0


def test_active_battlegrounds_bootstrap_notifies_state_ready_before_first_turn() -> None:
    path = Path("battlegrounds/Power.log")
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
    )

    monitor, observed, llm_events, results = _run_bootstrap_batches(
        [TailBatch(lines, path, bootstrap=True, source_reset=True, bootstrap_complete=True)]
    )

    assert [kind for kind, _snapshot in observed] == ["source_reset", "state_ready"]
    assert observed[-1][1].mode == "battlegrounds"
    assert observed[-1][1].phase == "hero_select"
    assert observed[-1][1].turn == 0
    assert llm_events == []
    assert results == []
    assert monitor.status().events_seen == 0


def test_stale_active_bootstrap_does_not_notify_state_ready() -> None:
    path = Path("stale/Power.log")
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )
    modified_at = time.time() - 24 * 60 * 60

    monitor, observed, llm_events, results = _run_bootstrap_batches(
        [
            TailBatch(
                lines,
                path,
                bootstrap=True,
                source_reset=True,
                bootstrap_complete=True,
                modified_at=modified_at,
            )
        ]
    )

    assert [kind for kind, _snapshot in observed] == ["source_reset"]
    assert monitor.snapshot().phase == "playing"
    assert monitor.status().last_line_at == modified_at
    assert llm_events == []
    assert results == []


def test_live_state_emits_stale_edge_after_inactivity() -> None:
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    active = GameSnapshot(mode="constructed", phase="playing", game_number=1)
    monitor._parser = SimpleNamespace(
        snapshot=lambda: active,
        entity_capacity_exceeded=False,
    )
    monitor._snapshot = active
    monitor._bootstrap_complete = True
    monitor._state_ready_notified = True
    monitor._live_context_generation = monitor._source_generation
    monitor._status.last_line_at = time.time() - 301.0
    monitor._tailer = _BatchSequence(
        monitor,
        [TailBatch((), Path("Power.log"), bootstrap_complete=True)],
    )

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while not monitor._stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)

    assert observed == ["state_stale"]


def test_ended_bootstrap_does_not_replay_terminal_events_or_statistics() -> None:
    path = Path("ended/Power.log")
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE"),
    )

    monitor, observed, llm_events, results = _run_bootstrap_batches(
        [TailBatch(lines, path, bootstrap=True, source_reset=True, bootstrap_complete=True)]
    )

    assert [kind for kind, _snapshot in observed] == ["source_reset"]
    assert monitor.snapshot().phase == "ended"
    assert llm_events == []
    assert results == []
    assert monitor.status().events_seen == 0


def test_battlegrounds_terminal_bootstrap_does_not_replay_result_callback() -> None:
    results: list[str] = []
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
        on_result=lambda event, _snapshot: results.append(event.kind),
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    ended = GameSnapshot(
        mode="battlegrounds",
        phase="ended",
        game_number=7,
        battlegrounds=BattlegroundsSnapshot(round=9, phase="ended", placement=3),
    )
    terminal = GameEvent(
        "battlegrounds_game_ended", 10, "placed", 100.0, {"placement": 3}
    )
    monitor._parser = SimpleNamespace(
        feed_line=lambda _line_value, *, now: [terminal],
        snapshot=lambda: ended,
        entity_capacity_exceeded=False,
    )
    monitor._tailer = _BatchSequence(
        monitor,
        [
            TailBatch(
                ("terminal",),
                Path("battlegrounds-ended/Power.log"),
                bootstrap=True,
                source_reset=False,
                bootstrap_complete=True,
            )
        ],
    )

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while not monitor._stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)

    assert results == []
    assert observed == []
    assert monitor.status().events_seen == 0


def test_spectator_bootstrap_does_not_notify_state_ready() -> None:
    path = Path("spectator/Power.log")
    lines = (
        "D 12:00:00.0000000 SpectatorMode - Start Spectator Game",
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )

    monitor, observed, llm_events, results = _run_bootstrap_batches(
        [TailBatch(lines, path, bootstrap=True, source_reset=True, bootstrap_complete=True)]
    )

    assert [kind for kind, _snapshot in observed] == ["source_reset"]
    assert monitor.snapshot().phase == "spectator"
    assert llm_events == []
    assert results == []


def test_state_ready_is_once_per_log_source_generation() -> None:
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )
    first = Path("session-1/Power.log")
    second = Path("session-2/Power.log")

    _monitor, observed, llm_events, results = _run_bootstrap_batches(
        [
            TailBatch(lines, first, bootstrap=True, source_reset=True, bootstrap_complete=True),
            TailBatch((), first, bootstrap_complete=True),
            TailBatch(lines, second, bootstrap=True, source_reset=True, bootstrap_complete=True),
            TailBatch((), second, bootstrap_complete=True),
        ]
    )

    assert [kind for kind, _snapshot in observed] == [
        "source_reset",
        "state_ready",
        "source_reset",
        "state_ready",
    ]
    assert llm_events == []
    assert results == []


def test_source_reset_clears_previous_generation_activity_before_freshness_check() -> None:
    lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )
    first = Path("session-1/Power.log")
    second = Path("session-2/Power.log")
    now = time.time()

    monitor, observed, _llm_events, _results = _run_bootstrap_batches(
        [
            TailBatch(
                lines,
                first,
                bootstrap=True,
                source_reset=True,
                bootstrap_complete=True,
                modified_at=now,
            ),
            TailBatch(
                (_line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"),),
                first,
                bootstrap_complete=True,
                modified_at=now,
            ),
            TailBatch(
                lines,
                second,
                bootstrap=True,
                source_reset=True,
                bootstrap_complete=True,
                modified_at=now - 3600.0,
            ),
        ]
    )

    assert [kind for kind, _snapshot in observed] == [
        "source_reset",
        "state_ready",
        "turn_started",
        "source_reset",
    ]
    assert monitor.status().last_event_at == 0.0
    assert monitor.status().last_event_kind == ""
    assert monitor.status().last_line_at == now - 3600.0


def test_reader_reset_clears_generation_scoped_runtime_activity() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(log_path="old/Power.log"),
        _logger(),
        on_llm=lambda *_args: False,
    )
    monitor._status.resolved_log_path = "old/Power.log"
    monitor._status.source_modified_at = 100.0
    monitor._status.last_line_at = 101.0
    monitor._status.last_event_at = 102.0
    monitor._status.last_event_kind = "turn_started"

    monitor.update_config(CompanionConfig(log_path="new/Power.log"))

    status = monitor.status()
    assert status.resolved_log_path == ""
    assert status.source_modified_at == 0.0
    assert status.last_line_at == 0.0
    assert status.last_event_at == 0.0
    assert status.last_event_kind == ""


def test_failed_source_config_staging_keeps_old_identity_and_retry_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = CompanionMonitor(
        CompanionConfig(log_path="old/Power.log"),
        _logger(),
        on_llm=lambda *_args: False,
    )
    old_snapshot = GameSnapshot(mode="constructed", phase="playing", game_number=7)
    monitor._snapshot = old_snapshot
    _snapshot, _status, old_generation = monitor.capture()
    real_tailer = monitor_module.PowerLogTailer
    construction_calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal construction_calls
        construction_calls += 1
        if construction_calls == 1:
            raise RuntimeError("reader staging failed")
        return real_tailer(*args, **kwargs)

    monkeypatch.setattr(monitor_module, "PowerLogTailer", fail_once)
    updated = CompanionConfig(log_path="new/Power.log")

    with pytest.raises(RuntimeError, match="reader staging failed"):
        monitor.update_config(updated)

    failed_snapshot, _failed_status, failed_generation = monitor.capture()
    assert monitor.config.log_path == "old/Power.log"
    assert failed_snapshot == old_snapshot
    assert failed_generation == old_generation

    monitor.update_config(updated)

    reset_snapshot, reset_status, reset_generation = monitor.capture()
    assert construction_calls == 2
    assert monitor.config.log_path == "new/Power.log"
    assert reset_snapshot == GameSnapshot()
    assert reset_status.source_state == "waiting"
    assert reset_generation == old_generation + 1


def test_old_source_generation_drops_all_delayed_callbacks() -> None:
    observed: list[str] = []
    results: list[str] = []
    llm_events: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True),
        _logger(),
        on_llm=lambda _prompt, event, _snapshot: not llm_events.append(event.kind),
        on_result=lambda event, _snapshot: results.append(event.kind),
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    _snapshot, _status, old_generation = monitor.capture()
    event = GameEvent(
        "battlegrounds_game_ended",
        10,
        "ended",
        100.0,
        {"placement": 1},
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="ended",
        game_number=7,
        battlegrounds=BattlegroundsSnapshot(placement=1),
    )

    monitor.update_config(CompanionConfig(log_path="new/Power.log"))
    monitor._handle_batch(
        [(event, snapshot)],
        100.0,
        source_generation=old_generation,
    )

    assert observed == []
    assert results == []
    assert llm_events == []
    assert monitor.status().events_seen == 0


def test_source_update_waits_for_in_flight_event_callback() -> None:
    callback_entered = threading.Event()
    release_callback = threading.Event()
    update_finished = threading.Event()

    def on_event(_event: GameEvent, _snapshot: GameSnapshot) -> None:
        callback_entered.set()
        release_callback.wait(1.0)

    monitor = CompanionMonitor(
        CompanionConfig(log_path="old/Power.log"),
        _logger(),
        on_llm=lambda *_args: False,
        on_event=on_event,
    )
    _snapshot, _status, generation = monitor.capture()
    callback_thread = threading.Thread(
        target=lambda: monitor._notify_event(
            GameEvent("turn_started", 5, "turn", 100.0, {}),
            GameSnapshot(mode="constructed", phase="playing", game_number=1),
            source_generation=generation,
        )
    )
    callback_thread.start()
    assert callback_entered.wait(1.0)
    update_thread = threading.Thread(
        target=lambda: (
            monitor.update_config(CompanionConfig(log_path="new/Power.log")),
            update_finished.set(),
        )
    )
    update_thread.start()

    assert update_finished.wait(0.05) is False
    release_callback.set()
    callback_thread.join(1.0)
    update_thread.join(1.0)

    assert callback_thread.is_alive() is False
    assert update_thread.is_alive() is False
    assert update_finished.is_set()
    assert monitor.config.log_path == "new/Power.log"


def test_capture_returns_one_generation_and_copies_mutable_status() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(log_path="old/Power.log"),
        _logger(),
        on_llm=lambda *_args: False,
    )
    expected_snapshot = GameSnapshot(mode="constructed", phase="playing", game_number=4)
    monitor._snapshot = expected_snapshot
    monitor._status.source_state = "watching"
    monitor._status.last_line_at = 123.0

    snapshot, status, generation = monitor.capture()

    assert snapshot is expected_snapshot
    assert status.source_state == "watching"
    assert status.last_line_at == 123.0
    status.source_state = "modified_outside_monitor"
    assert monitor.status().source_state == "watching"

    monitor.update_config(CompanionConfig(log_path="new/Power.log"))
    reset_snapshot, reset_status, reset_generation = monitor.capture()

    assert reset_generation == generation + 1
    assert reset_snapshot == GameSnapshot()
    assert reset_status.source_state == "waiting"
    assert reset_status.last_line_at == 0.0


def test_incremental_live_game_emits_stale_without_bootstrap_state_ready() -> None:
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    active_lines = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )
    calls = 0

    def poll() -> TailBatch:
        nonlocal calls
        calls += 1
        if calls == 1:
            return TailBatch(
                (),
                Path("Power.log"),
                bootstrap=True,
                source_reset=True,
                bootstrap_complete=True,
            )
        if calls == 2:
            return TailBatch(active_lines, Path("Power.log"), bootstrap_complete=True)
        if calls == 3:
            monitor._status.last_line_at = time.time() - 301.0
            monitor._status.last_event_at = time.time() - 301.0
            return TailBatch((), Path("Power.log"), bootstrap_complete=True)
        monitor._stop.set()
        return TailBatch((), Path("Power.log"), bootstrap_complete=True)

    monitor._tailer = SimpleNamespace(poll=poll)

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while not monitor._stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)

    assert "state_ready" not in observed
    assert observed[-1] == "state_stale"
    assert observed.count("state_stale") == 1


def test_restart_bootstraps_current_log_without_replaying_stopped_backlog(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Power.log"
    first_game = (
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]"),
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        _line("FULL_ENTITY - Creating ID=103 CardID=TB_BaconShop_HERO_03"),
        _line("    tag=CONTROLLER value=3"),
        _line("    tag=ZONE value=PLAY"),
        _line("    tag=CARDTYPE value=HERO"),
        _line("    tag=PLAYER_ID value=3"),
        _line("    tag=PLAYER_LEADERBOARD_PLACE value=3"),
    )
    log_path.write_text("\n".join((*first_game, "")), encoding="utf-8")
    observed: list[str] = []
    llm_events: list[str] = []
    results: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(
            log_path=str(log_path),
            poll_interval_seconds=0.1,
            llm_commentary_enabled=True,
            llm_data_consent=True,
        ),
        _logger(),
        on_llm=lambda _prompt, event, _snapshot: not llm_events.append(event.kind),
        on_result=lambda event, _snapshot: results.append(event.kind),
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while "state_ready" not in observed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)
    assert "state_ready" in observed

    observed.clear()
    llm_events.clear()
    results.clear()
    stopped_backlog = (
        _line("TAG_CHANGE Entity=103 tag=PLAYER_LEADERBOARD_PLACE value=4"),
        _line("TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE"),
        _line("CREATE_GAME"),
        _line("GameEntity EntityID=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY"),
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join((*stopped_backlog, "")))

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while "state_ready" not in observed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)

    assert observed == ["source_reset", "state_ready"]
    assert llm_events == []
    assert results == []
    assert monitor.snapshot().phase == "playing"


def test_monitor_delegates_authorized_commentary_to_llm_callback() -> None:
    llm_prompts: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True),
        _logger(),
        on_llm=lambda prompt, event, snapshot: not llm_prompts.append(prompt),
    )

    monitor._handle_event(
        GameEvent("battlegrounds_triple", 10, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
        100.0,
    )

    assert len(llm_prompts) == 1
    assert "保持当前 N.E.K.O 角色的人设" in llm_prompts[0]
    assert monitor.status().llm_submissions == 1


def test_monitor_batch_uses_highest_priority_event_with_its_own_snapshot() -> None:
    submissions: list[tuple[str, str]] = []
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True),
        _logger(),
        on_llm=lambda prompt, event, snapshot: not submissions.append((event.kind, prompt)),
        on_event=lambda event, snapshot: observed.append(event.kind),
    )
    early = GameSnapshot(mode="battlegrounds", phase="recruit", game_number=2, turn=3)
    critical = GameSnapshot(mode="battlegrounds", phase="combat", game_number=2, turn=4)

    monitor._handle_batch(
        [
            (GameEvent("battlegrounds_combat_started", 6, "combat", 100.0, {}), early),
            (GameEvent("hero_damaged", 8, "low health", 101.0, {"health": 7}), critical),
        ],
        101.0,
    )

    assert [item[0] for item in submissions] == ["hero_damaged"]
    assert '"phase":"combat"' in submissions[0][1]
    assert observed == ["battlegrounds_combat_started", "hero_damaged"]


def test_monitor_batch_prefers_later_composite_event_on_an_exact_tie() -> None:
    submitted: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True),
        _logger(),
        on_llm=lambda prompt, event, snapshot: not submitted.append(event.kind),
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="combat",
        battlegrounds=BattlegroundsSnapshot(round=8, phase="combat"),
    )

    monitor._handle_batch(
        [
            (GameEvent("hero_damaged", 8, "damage", 100.0, {"health": 7}), snapshot),
            (
                GameEvent(
                    "battlegrounds_combat_result",
                    8,
                    "combat result",
                    100.0,
                    {"round": 7, "outcome": "lost"},
                ),
                monitor._snapshot_for_event(
                    GameEvent(
                        "battlegrounds_combat_result",
                        8,
                        "combat result",
                        100.0,
                        {"round": 7, "outcome": "lost"},
                    ),
                    snapshot,
                ),
            ),
        ],
        100.0,
    )

    assert submitted == ["battlegrounds_combat_result"]
    corrected = monitor._snapshot_for_event(
        GameEvent("battlegrounds_combat_result", 8, "result", 100.0, {"round": 7}),
        snapshot,
    )
    assert corrected.battlegrounds is not None
    assert corrected.battlegrounds.round == 7
    assert snapshot.battlegrounds.round == 8


def test_background_status_report_excludes_paths_and_game_snapshot() -> None:
    reports: list[dict[str, object]] = []
    monitor = CompanionMonitor(
        CompanionConfig(),
        _logger(),
        on_llm=lambda *_args: False,
        on_status=reports.append,
    )
    monitor._status.resolved_log_path = r"C:\\Users\\private\\Power.log"
    monitor._snapshot = GameSnapshot(mode="battlegrounds", phase="recruit")

    monitor._report()

    assert reports and "game" not in reports[0]
    assert "resolved_log_path" not in reports[0]["runtime"]
    assert "private" not in str(reports[0])


def test_stop_timeout_does_not_wait_for_monitor_state_lock() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(),
        _logger(),
        on_llm=lambda *_args: False,
    )
    lock_held = threading.Event()
    release = threading.Event()

    def hold_monitor_lock() -> None:
        with monitor._lock:
            lock_held.set()
            release.wait(1.0)

    thread = threading.Thread(target=hold_monitor_lock, daemon=True)
    monitor._thread = thread
    thread.start()
    assert lock_held.wait(1.0)

    started = time.monotonic()
    assert monitor.stop(timeout=0.05) is False
    elapsed = time.monotonic() - started
    release.set()
    thread.join(1.0)

    assert elapsed < 0.2


def test_monitor_is_accepting_tracks_start_stop_request_and_completion() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
    )
    poll_entered = threading.Event()
    release_poll = threading.Event()
    stop_result: list[bool] = []

    class BlockingTailer:
        def poll(self) -> TailBatch:
            poll_entered.set()
            assert release_poll.wait(1.0)
            return TailBatch((), Path("Power.log"), bootstrap_complete=True)

    monitor._tailer = BlockingTailer()  # type: ignore[assignment]
    assert monitor.is_accepting() is False
    assert monitor.start() is True
    assert poll_entered.wait(1.0)
    assert monitor.is_accepting() is True

    stopping = threading.Thread(
        target=lambda: stop_result.append(monitor.stop(timeout=1.0)),
        daemon=True,
    )
    stopping.start()
    deadline = time.monotonic() + 1.0
    while not monitor._stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.005)

    assert monitor._thread is not None and monitor._thread.is_alive()
    assert monitor.is_accepting() is False
    release_poll.set()
    stopping.join(1.0)

    assert stopping.is_alive() is False
    assert stop_result == [True]
    assert monitor.is_accepting() is False


def test_stop_flushes_a_fully_parsed_live_batch() -> None:
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    monitor._parser.feed_line(_line("CREATE_GAME"), now=100.0)
    monitor._parser.feed_line(_line("GameEntity EntityID=1"), now=100.0)
    monitor._snapshot = monitor._parser.snapshot()
    monitor._bootstrap_complete = True
    lines = (
        _line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=TURN value=2"),
    )
    monitor._tailer = SimpleNamespace(
        poll=lambda: TailBatch(lines, Path("Power.log"), bootstrap_complete=True)
    )
    original_feed = monitor._parser.feed_line
    feed_count = 0

    def stop_after_first_line(line: str, *, now: float) -> list[GameEvent]:
        nonlocal feed_count
        events = original_feed(line, now=now)
        feed_count += 1
        if feed_count == 1:
            monitor._stop.set()
        return events

    monitor._parser.feed_line = stop_after_first_line

    assert monitor.start()
    assert monitor.stop(timeout=2.0)

    assert feed_count == 2
    assert monitor.snapshot().turn == 2
    assert observed == ["turn_started", "turn_started"]
    assert monitor.status().events_seen == 2


def test_interrupted_bootstrap_rebuilds_reader_before_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "Power.log"
    log_path.write_text(
        "\n".join(
            [
                _line("CREATE_GAME"),
                _line("GameEntity EntityID=1"),
                _line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"),
                _line("TAG_CHANGE Entity=GameEntity tag=TURN value=2"),
                "",
            ]
        ),
        encoding="utf-8",
    )
    observed: list[str] = []
    monitor = CompanionMonitor(
        CompanionConfig(log_path=str(log_path), poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
        on_event=lambda event, _snapshot: observed.append(event.kind),
    )
    lines = (
        _line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"),
        _line("TAG_CHANGE Entity=GameEntity tag=TURN value=2"),
    )
    old_tailer = SimpleNamespace(
        poll=lambda: TailBatch(
            lines,
            log_path,
            bootstrap=True,
            source_reset=False,
            bootstrap_complete=True,
        )
    )
    old_parser = monitor._parser
    monitor._tailer = old_tailer
    original_feed = old_parser.feed_line
    feed_count = 0

    def stop_during_bootstrap(line: str, *, now: float) -> list[GameEvent]:
        nonlocal feed_count
        events = original_feed(line, now=now)
        feed_count += 1
        monitor._stop.set()
        return events

    old_parser.feed_line = stop_during_bootstrap

    assert monitor.start()
    assert monitor.stop(timeout=2.0)
    assert feed_count == 1
    assert monitor._parser is not old_parser
    assert monitor._tailer is not old_tailer
    assert monitor.snapshot().turn == 0
    assert observed == []

    assert monitor.start()
    deadline = time.monotonic() + 2.0
    while monitor.snapshot().turn != 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert monitor.stop(timeout=2.0)

    assert monitor.snapshot().game_number == 1
    assert monitor.snapshot().turn == 2
    assert observed == ["source_reset", "state_ready"]
    assert monitor.status().events_seen == 0


def test_parser_exception_rebuilds_the_advanced_reader() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
    )
    old_parser = monitor._parser
    old_tailer = SimpleNamespace(
        poll=lambda: TailBatch(
            (_line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"),),
            Path("Power.log"),
            bootstrap_complete=True,
        )
    )
    monitor._tailer = old_tailer

    def fail_after_poll(_line_value: str, *, now: float) -> list[GameEvent]:
        del now
        monitor._stop.set()
        raise ValueError("malformed parser state")

    old_parser.feed_line = fail_after_poll

    assert monitor.start()
    assert monitor.stop(timeout=2.0)

    assert monitor._parser is not old_parser
    assert monitor._tailer is not old_tailer
    assert monitor.snapshot() == GameSnapshot()
    assert monitor.status().last_error_code == "monitor:ValueError"


def test_poll_exception_rebuilds_reader_even_before_batch_assignment() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
    )
    old_parser = monitor._parser

    class AdvancingTailer:
        offset = 0

        def poll(self) -> TailBatch:
            self.offset = 4096
            monitor._stop.set()
            raise OSError("read failed after cursor advance")

    old_tailer = AdvancingTailer()
    monitor._tailer = old_tailer

    assert monitor.start()
    assert monitor.stop(timeout=2.0)

    assert old_tailer.offset == 4096
    assert monitor._parser is not old_parser
    assert monitor._tailer is not old_tailer
    assert monitor.snapshot() == GameSnapshot()
    assert monitor.status().last_error_code == "monitor:OSError"


def test_old_stop_cannot_clear_a_new_monitor_generation() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(),
        _logger(),
        on_llm=lambda *_args: False,
    )
    join_entered = threading.Event()
    release_join = threading.Event()

    class OldThread:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            del timeout
            join_entered.set()
            release_join.wait(1.0)

    old_thread = OldThread()
    monitor._thread = old_thread  # type: ignore[assignment]
    stop_result: list[bool] = []
    stopping = threading.Thread(target=lambda: stop_result.append(monitor.stop(timeout=1.0)))
    stopping.start()
    assert join_entered.wait(1.0)

    old_thread.alive = False
    monitor._run = lambda stop_event: stop_event.wait(1.0)  # type: ignore[method-assign]
    assert monitor.start()
    new_thread = monitor._thread
    assert new_thread is not None and new_thread is not old_thread

    release_join.set()
    stopping.join(1.0)

    assert stop_result == [True]
    assert monitor._thread is new_thread
    assert new_thread.is_alive()
    assert monitor.stop(timeout=2.0)


def test_old_stop_cannot_overwrite_new_monitor_running_status() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(),
        _logger(),
        on_llm=lambda *_args: False,
    )
    join_entered = threading.Event()
    release_join = threading.Event()
    old_ownership_released = threading.Event()
    resume_old_stop = threading.Event()

    class PausingLifecycleLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self._stop_exits = 0

        def __enter__(self) -> "PausingLifecycleLock":
            self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()
            if threading.current_thread() is stopping:
                self._stop_exits += 1
                if self._stop_exits == 2:
                    old_ownership_released.set()
                    resume_old_stop.wait(1.0)

    class OldThread:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            del timeout
            join_entered.set()
            release_join.wait(1.0)

    old_thread = OldThread()
    monitor._thread = old_thread  # type: ignore[assignment]
    monitor._lifecycle_lock = PausingLifecycleLock()  # type: ignore[assignment]
    stop_result: list[bool] = []
    stopping = threading.Thread(target=lambda: stop_result.append(monitor.stop(timeout=1.0)))
    stopping.start()
    assert join_entered.wait(1.0)

    old_thread.alive = False
    release_join.set()
    assert old_ownership_released.wait(1.0)
    monitor._run = lambda stop_event: stop_event.wait(1.0)  # type: ignore[method-assign]
    assert monitor.start()
    with monitor._lock:
        monitor._status.monitor_running = True
    resume_old_stop.set()
    stopping.join(1.0)

    assert stop_result == [True]
    assert monitor.status().monitor_running is True
    assert monitor.stop(timeout=2.0)


def test_poll_exception_rebuilds_a_potentially_advanced_reader() -> None:
    monitor = CompanionMonitor(
        CompanionConfig(poll_interval_seconds=0.1),
        _logger(),
        on_llm=lambda *_args: False,
    )
    old_parser = monitor._parser

    class AdvancingBrokenTailer:
        advanced = False

        def poll(self) -> TailBatch:
            self.advanced = True
            monitor._stop.set()
            raise OSError("read failed after advancing the cursor")

    old_tailer = AdvancingBrokenTailer()
    monitor._tailer = old_tailer  # type: ignore[assignment]

    assert monitor.start()
    assert monitor.stop(timeout=2.0)

    assert old_tailer.advanced is True
    assert monitor._parser is not old_parser
    assert monitor._tailer is not old_tailer
    assert monitor.snapshot() == GameSnapshot()
    assert monitor.status().last_error_code == "monitor:OSError"
