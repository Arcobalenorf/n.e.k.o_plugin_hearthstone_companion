from __future__ import annotations

import os
from pathlib import Path

import pytest
from hearthstone_companion_under_test import tailer as tailer_module
from hearthstone_companion_under_test.tailer import MAX_LINE_BYTES, PowerLogLocator, PowerLogTailer


def test_tailer_default_bootstrap_window_matches_runtime_contract() -> None:
    tailer = PowerLogTailer(PowerLogLocator())

    assert tailer.initial_read_max_bytes == 64 * 1024 * 1024


def test_tailer_preserves_half_line_and_utf8_across_byte_chunks(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_bytes(b"")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    assert tailer.poll().bootstrap is True

    encoded = "SHOW_ENTITY 猫娘\r\n".encode("utf-8")
    with path.open("ab") as handle:
        handle.write(encoded[:-2])

    collected: list[str] = []
    for _ in range(len(encoded) + 2):
        collected.extend(tailer.poll(max_bytes=1).lines)
    assert collected == []

    with path.open("ab") as handle:
        handle.write(encoded[-2:])
    for _ in range(4):
        collected.extend(tailer.poll(max_bytes=1).lines)

    assert collected == ["SHOW_ENTITY 猫娘"]


def test_bootstrap_does_not_consume_trailing_incomplete_line(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_bytes(b"CREATE_GAME\r\nTAG_CHANGE Entity=1 tag=TURN val")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))

    first = tailer.poll()
    assert first.lines == ("CREATE_GAME",)

    with path.open("ab") as handle:
        handle.write(b"ue=2\r\n")
    second = tailer.poll()
    assert second.lines == ("TAG_CHANGE Entity=1 tag=TURN value=2",)


def test_bootstrap_preserves_active_spectator_marker_before_create_game(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text(
        "old game\nBegin Spectating 1st player\nCREATE_GAME\nTAG_CHANGE Entity=1 tag=TURN value=1\n",
        encoding="utf-8",
    )

    batch = PowerLogTailer(PowerLogLocator(str(path))).poll()

    assert batch.lines == (
        "Begin Spectating 1st player",
        "CREATE_GAME",
        "TAG_CHANGE Entity=1 tag=TURN value=1",
    )


def test_bootstrap_drops_ended_spectator_marker_before_create_game(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text(
        "Begin Spectating 1st player\nEnd Spectator Game\nCREATE_GAME\n",
        encoding="utf-8",
    )

    batch = PowerLogTailer(PowerLogLocator(str(path))).poll()

    assert batch.lines == ("CREATE_GAME",)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_bootstrap_prefers_current_canonical_game_for_lf_and_crlf(
    tmp_path: Path,
    newline: bytes,
) -> None:
    path = tmp_path / "Power.log"
    old_create = b"D GameState.DebugPrintPower() - CREATE_GAME"
    current_create = b"D GameState.DebugPrintPower() - CREATE_GAME"
    current_entity = b"D GameState.DebugPrintPower() - Player EntityID=8 PlayerID=3"
    mirror_create = b"D PowerTaskList.DebugPrintPower() - CREATE_GAME"
    path.write_bytes(
        newline.join(
            (
                old_create,
                b"D GameState.DebugPrintPower() - old game",
                current_create,
                current_entity,
                mirror_create,
                b"D PowerTaskList.DebugPrintPower() - mirrored",
            )
        )
        + newline
    )

    batch = PowerLogTailer(PowerLogLocator(str(path))).poll()

    assert batch.lines == tuple(
        line.decode("ascii")
        for line in (
            current_create,
            current_entity,
            mirror_create,
            b"D PowerTaskList.DebugPrintPower() - mirrored",
        )
    )
    assert batch.bootstrap_complete is True


def test_truncated_bootstrap_with_canonical_start_outside_window_is_incomplete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Power.log"
    path.write_bytes(
        b"D GameState.DebugPrintPower() - CREATE_GAME\r\n"
        + b"D GameState.DebugPrintPower() - old\r\n" * (32 * 1024)
    )

    batch = PowerLogTailer(
        PowerLogLocator(str(path)), initial_read_max_bytes=1024 * 1024
    ).poll()

    assert batch.bootstrap is True
    assert batch.bootstrap_complete is False
    assert not any("CREATE_GAME" in line for line in batch.lines)


def test_truncated_crlf_bootstrap_recovers_at_current_canonical_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Power.log"
    current_create = b"D GameState.DebugPrintPower() - CREATE_GAME"
    current_entity = b"D GameState.DebugPrintPower() - Player EntityID=8 PlayerID=3"
    path.write_bytes(
        b"D GameState.DebugPrintPower() - old\r\n" * (32 * 1024)
        + current_create
        + b"\r\n"
        + current_entity
        + b"\r\n"
    )

    batch = PowerLogTailer(
        PowerLogLocator(str(path)), initial_read_max_bytes=1024 * 1024
    ).poll()

    assert batch.bootstrap is True
    assert batch.bootstrap_complete is True
    assert batch.lines == (current_create.decode("ascii"), current_entity.decode("ascii"))


def test_oversized_partial_line_is_discarded_with_bounded_memory(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_bytes(b"")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    tailer.poll()

    with path.open("ab") as handle:
        handle.write(b"x" * (MAX_LINE_BYTES + 100))
    assert tailer.poll(max_bytes=MAX_LINE_BYTES + 100).lines == ()
    assert tailer._partial == b""

    with path.open("ab") as handle:
        handle.write(b"\nCREATE_GAME\n")
    assert tailer.poll().lines == ("CREATE_GAME",)


def test_truncation_reboots_source_and_marks_reset(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text("CREATE_GAME\nold line that makes the source longer\n", encoding="utf-8")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    tailer.poll()

    path.write_text("CREATE_GAME\n", encoding="utf-8")
    batch = tailer.poll()

    assert batch.bootstrap is True
    assert batch.source_reset is True
    assert batch.lines == ("CREATE_GAME",)


def test_same_inode_truncate_and_regrow_reboots_source(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text("CREATE_GAME\nold session\n" + "x" * 256 + "\n", encoding="utf-8")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    tailer.poll()
    old_offset = tailer.offset
    old_identity = tailer._identity

    path.write_text(
        "CREATE_GAME\nnew session\n" + "y" * (old_offset + 256) + "\n",
        encoding="utf-8",
    )
    batch = tailer.poll()

    assert tailer._identity == old_identity
    assert batch.bootstrap is True
    assert batch.source_reset is True
    assert batch.lines[0] == "CREATE_GAME"
    assert batch.lines[1] == "new session"


def test_bootstrap_reports_source_modified_time(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text("CREATE_GAME\n", encoding="utf-8")
    expected = 1_700_000_000.0
    os.utime(path, (expected, expected))

    batch = PowerLogTailer(PowerLogLocator(str(path))).poll()

    assert batch.modified_at == expected


def test_same_path_file_replacement_reboots_source(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    canonical_create = b"D GameState.DebugPrintPower() - CREATE_GAME"
    path.write_bytes(
        b"D GameState.DebugPrintPower() - discarded\r\n"
        + canonical_create
        + b"\r\nold\r\n"
    )
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    first = tailer.poll()

    assert first.lines == (canonical_create.decode("ascii"), "old")

    replacement = tmp_path / "Power.next.log"
    replacement.write_bytes(
        b"D GameState.DebugPrintPower() - stale\r\n"
        + canonical_create
        + b"\r\nnew session\r\n"
    )
    replacement.replace(path)
    batch = tailer.poll()

    assert batch.bootstrap is True
    assert batch.source_reset is True
    assert batch.lines == (canonical_create.decode("ascii"), "new session")


def test_disappearing_source_emits_one_reset_edge(tmp_path: Path) -> None:
    path = tmp_path / "Power.log"
    path.write_text("CREATE_GAME\n", encoding="utf-8")
    tailer = PowerLogTailer(PowerLogLocator(str(path)))
    tailer.poll()

    path.unlink()
    lost = tailer.poll()
    still_missing = tailer.poll()

    assert lost.path is None
    assert lost.source_reset is True
    assert still_missing.source_reset is False


def test_locator_and_tailer_rotate_to_newest_session_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    logs = tmp_path / "Blizzard" / "Hearthstone" / "Logs"
    old_path = logs / "Hearthstone_2026_08_19_010000" / "Power.log"
    new_path = logs / "Hearthstone_2026_08_19_020000" / "Power.log"
    old_path.parent.mkdir(parents=True)
    new_path.parent.mkdir(parents=True)
    old_path.write_text("CREATE_GAME\nold\n", encoding="utf-8")
    new_path.write_text("CREATE_GAME\nnew\n", encoding="utf-8")
    os.utime(old_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new_path, ns=(2_000_000_000, 2_000_000_000))

    locator = PowerLogLocator(
        executable_paths_provider=lambda: (), install_paths_provider=lambda: ()
    )
    tailer = PowerLogTailer(locator)
    first = tailer.poll()
    assert first.path == new_path.resolve()
    assert first.lines[-1] == "new"

    old_path.write_text("CREATE_GAME\nrotated\n", encoding="utf-8")
    os.utime(old_path, ns=(3_000_000_000, 3_000_000_000))
    second = tailer.poll()
    assert second.path == old_path.resolve()
    assert second.bootstrap is True
    assert second.source_reset is True
    assert second.lines[-1] == "rotated"


def test_locator_discovers_non_default_uid_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = (
        tmp_path
        / "Blizzard"
        / "Hearthstone"
        / "hs_cn"
        / "Logs"
        / "Hearthstone_2026_08_19_030000"
        / "Power.log"
    )
    path.parent.mkdir(parents=True)
    path.write_text("CREATE_GAME\n", encoding="utf-8")

    assert PowerLogLocator(
        executable_paths_provider=lambda: (), install_paths_provider=lambda: ()
    ).resolve() == path.resolve()


def test_configured_logs_directory_discovers_nested_session(tmp_path: Path) -> None:
    logs = tmp_path / "Logs"
    older = logs / "Hearthstone_2026_08_19_010000" / "Power.log"
    newer = logs / "uid" / "Hearthstone_2026_08_19_020000" / "Power.log"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("old\n", encoding="utf-8")
    newer.write_text("new\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    assert PowerLogLocator(str(logs)).resolve() == newer.resolve()


def test_locator_discovers_logs_beside_running_game_executable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    game = tmp_path / "gameLibrary" / "Hearthstone" / "Hearthstone.exe"
    log_path = game.parent / "Logs" / "Hearthstone_2026_08_20_011437" / "Power.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")

    locator = PowerLogLocator(
        executable_paths_provider=lambda: (game,), install_paths_provider=lambda: ()
    )

    assert locator.resolve() == log_path.resolve()


def test_locator_chooses_newest_session_beside_running_game(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    game = tmp_path / "Hearthstone" / "Hearthstone.exe"
    older = game.parent / "Logs" / "Hearthstone_old" / "Power.log"
    current = game.parent / "Logs" / "Hearthstone_current" / "Power.log"
    older.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    older.write_text("old\n", encoding="utf-8")
    current.write_text("current\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(current, ns=(2_000_000_000, 2_000_000_000))

    locator = PowerLogLocator(
        executable_paths_provider=lambda: (game,), install_paths_provider=lambda: ()
    )

    assert locator.resolve() == current.resolve()


def test_locator_retries_running_game_discovery_after_cache_interval(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    now = [10.0]
    monkeypatch.setattr(tailer_module.time, "monotonic", lambda: now[0])
    game = tmp_path / "Hearthstone" / "Hearthstone.exe"
    log_path = game.parent / "Logs" / "session" / "Power.log"
    calls = 0

    def executable_paths() -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        return () if calls == 1 else (game,)

    locator = PowerLogLocator(
        executable_paths_provider=executable_paths,
        install_paths_provider=lambda: (),
        process_scan_interval_seconds=1.0,
    )
    assert locator.resolve() is None
    log_path.parent.mkdir(parents=True)
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")
    assert locator.resolve() is None
    assert calls == 1
    now[0] = 11.1

    assert locator.resolve() == log_path.resolve()
    assert calls == 2


def test_configured_path_never_queries_running_processes(tmp_path: Path) -> None:
    log_path = tmp_path / "Power.log"
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")

    def unexpected_provider() -> tuple[Path, ...]:
        raise AssertionError("configured path must take precedence")

    assert PowerLogLocator(
        str(log_path), executable_paths_provider=unexpected_provider
    ).resolve() == log_path.resolve()


def test_process_discovery_failure_keeps_appdata_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    log_path = (
        tmp_path / "Blizzard" / "Hearthstone" / "Logs" / "session" / "Power.log"
    )
    log_path.parent.mkdir(parents=True)
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")

    def denied_provider() -> tuple[Path, ...]:
        raise OSError("access denied")

    assert PowerLogLocator(
        executable_paths_provider=denied_provider, install_paths_provider=lambda: ()
    ).resolve() == log_path.resolve()


def test_locator_chooses_newest_log_across_appdata_and_running_game(
    tmp_path: Path, monkeypatch
) -> None:
    local_data = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    appdata_log = (
        local_data / "Blizzard" / "Hearthstone" / "Logs" / "old" / "Power.log"
    )
    game = tmp_path / "installed" / "Hearthstone.exe"
    game_log = game.parent / "Logs" / "current" / "Power.log"
    appdata_log.parent.mkdir(parents=True)
    game_log.parent.mkdir(parents=True)
    appdata_log.write_text("old\n", encoding="utf-8")
    game_log.write_text("current\n", encoding="utf-8")
    os.utime(appdata_log, ns=(1_000_000_000, 1_000_000_000))
    os.utime(game_log, ns=(2_000_000_000, 2_000_000_000))

    assert PowerLogLocator(
        executable_paths_provider=lambda: (game,), install_paths_provider=lambda: ()
    ).resolve() == game_log.resolve()


def test_locator_discovers_logs_from_installed_game_when_process_is_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    install = tmp_path / "library" / "Hearthstone"
    log_path = install / "Logs" / "session" / "Power.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")

    locator = PowerLogLocator(
        executable_paths_provider=lambda: (),
        install_paths_provider=lambda: (install,),
    )

    assert locator.resolve() == log_path.resolve()


def test_configured_path_never_queries_install_metadata(tmp_path: Path) -> None:
    log_path = tmp_path / "Power.log"
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")

    def unexpected_provider() -> tuple[Path, ...]:
        raise AssertionError("configured path must not query install metadata")

    locator = PowerLogLocator(
        str(log_path),
        executable_paths_provider=unexpected_provider,
        install_paths_provider=unexpected_provider,
    )

    assert locator.resolve() == log_path.resolve()


def test_locator_retries_install_discovery_after_cache_interval(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    now = [20.0]
    monkeypatch.setattr(tailer_module.time, "monotonic", lambda: now[0])
    install = tmp_path / "library" / "Hearthstone"
    log_path = install / "Logs" / "session" / "Power.log"
    calls = 0

    def install_paths() -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        return () if calls == 1 else (install,)

    locator = PowerLogLocator(
        executable_paths_provider=lambda: (),
        install_paths_provider=install_paths,
        process_scan_interval_seconds=1.0,
    )
    assert locator.resolve() is None
    log_path.parent.mkdir(parents=True)
    log_path.write_text("CREATE_GAME\n", encoding="utf-8")
    assert locator.resolve() is None
    assert calls == 1
    now[0] = 21.1

    assert locator.resolve() == log_path.resolve()
    assert calls == 2
