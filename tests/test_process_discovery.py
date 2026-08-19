from __future__ import annotations

from hearthstone_companion_under_test import process_discovery


def test_process_discovery_is_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(process_discovery.os, "name", "posix")

    assert process_discovery.hearthstone_executable_paths() == ()


def test_process_discovery_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(process_discovery.os, "name", "nt")
    monkeypatch.setattr(
        process_discovery,
        "_windows_hearthstone_executable_paths",
        lambda: (_ for _ in ()).throw(OSError("denied")),
    )

    assert process_discovery.hearthstone_executable_paths() == ()
