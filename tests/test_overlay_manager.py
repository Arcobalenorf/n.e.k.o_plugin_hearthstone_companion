from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.overlay_manager import OverlayManager


class _Logger:
    def info(self, *_args: object) -> None:
        pass


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.process.exit_on_close:
            self.process.returncode = 0

    def write(self, _value: str) -> int:
        return 1

    def flush(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        exit_on_close: bool = False,
        exit_on_terminate: bool = False,
        exit_on_kill: bool = False,
        wait_entered: threading.Event | None = None,
        wait_release: threading.Event | None = None,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.exit_on_close = exit_on_close
        self.exit_on_terminate = exit_on_terminate
        self.exit_on_kill = exit_on_kill
        self.wait_entered = wait_entered
        self.wait_release = wait_release
        self.stdin = _FakeStdin(self)
        self.wait_timeouts: list[float] = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if self.wait_entered is not None:
            self.wait_entered.set()
        if self.wait_release is not None:
            self.wait_release.wait(1.0)
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("overlay", timeout)

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.exit_on_terminate:
            self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        if self.exit_on_kill:
            self.returncode = -9


def _manager(tmp_path: Path, *, enabled: bool = True) -> OverlayManager:
    (tmp_path / "overlay_process.py").write_text("# test overlay\n", encoding="utf-8")
    manager = OverlayManager(
        _Logger(),
        plugin_dir=tmp_path,
        config=CompanionConfig(overlay_enabled=enabled),
    )
    manager.availability = lambda *, refresh=False: {  # type: ignore[method-assign]
        "available": True,
        "reason": "",
    }
    return manager


def _install_processes(monkeypatch: Any, processes: list[_FakeProcess]) -> None:
    pending = list(processes)

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        return pending.pop(0)

    monkeypatch.setattr(
        "hearthstone_companion_under_test.overlay_manager.subprocess.Popen",
        popen,
    )


def test_start_is_stably_rejected_when_overlay_is_disabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path, enabled=False)
    manager.availability = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("availability must not be probed while disabled")
    )

    assert manager.start() == {
        "ok": False,
        "running": False,
        "error_code": "overlay_disabled",
    }


def test_start_and_short_timeout_stop_gracefully(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager = _manager(tmp_path)
    process = _FakeProcess(101, exit_on_close=True)
    _install_processes(monkeypatch, [process])
    monkeypatch.setattr("hearthstone_companion_under_test.overlay_manager.time.sleep", lambda _value: None)

    assert manager.start() == {"ok": True, "running": True, "pid": 101}
    assert manager.stop(timeout=0.01) == {
        "ok": True,
        "running": False,
        "was_running": True,
    }
    assert process.stdin.closed is True
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_stop_escalates_to_kill_and_confirms_exit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager = _manager(tmp_path)
    process = _FakeProcess(102, exit_on_kill=True)
    _install_processes(monkeypatch, [process])
    monkeypatch.setattr("hearthstone_companion_under_test.overlay_manager.time.sleep", lambda _value: None)
    manager.start()

    result = manager.stop(timeout=0.03)

    assert result == {"ok": True, "running": False, "was_running": True}
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.poll() == -9


def test_stop_failure_is_reported_and_process_remains_manageable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager = _manager(tmp_path)
    process = _FakeProcess(103)
    _install_processes(monkeypatch, [process])
    monkeypatch.setattr("hearthstone_companion_under_test.overlay_manager.time.sleep", lambda _value: None)
    manager.start()

    result = manager.stop(timeout=0.02)

    assert result["ok"] is False
    assert result["running"] is True
    assert result["error_code"] == "overlay_stop_failed"
    assert result["pid"] == 103
    assert manager.status()["running"] is True
    assert manager.status()["pid"] == 103

    process.exit_on_kill = True
    assert manager.stop(timeout=0.02)["ok"] is True


def test_old_start_health_check_cannot_clear_a_new_process(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager = _manager(tmp_path)
    old_process = _FakeProcess(201, exit_on_close=True)
    new_process = _FakeProcess(202, exit_on_close=True)
    _install_processes(monkeypatch, [old_process, new_process])
    first_sleep_entered = threading.Event()
    release_first_sleep = threading.Event()
    sleep_lock = threading.Lock()
    sleep_calls = 0

    def controlled_sleep(_value: float) -> None:
        nonlocal sleep_calls
        with sleep_lock:
            sleep_calls += 1
            current = sleep_calls
        if current == 1:
            first_sleep_entered.set()
            release_first_sleep.wait(1.0)

    monkeypatch.setattr(
        "hearthstone_companion_under_test.overlay_manager.time.sleep",
        controlled_sleep,
    )
    old_result: list[dict[str, Any]] = []
    old_thread = threading.Thread(target=lambda: old_result.append(manager.start()))
    old_thread.start()
    assert first_sleep_entered.wait(1.0)

    assert manager.stop(timeout=0.02)["ok"] is True
    assert manager.start() == {"ok": True, "running": True, "pid": 202}
    release_first_sleep.set()
    old_thread.join(1.0)

    assert old_thread.is_alive() is False
    assert old_result[0]["error_code"] == "overlay_exited_early"
    assert manager.status()["running"] is True
    assert manager.status()["pid"] == 202
    assert manager.stop(timeout=0.02)["ok"] is True


def test_start_during_stop_reports_stopping_without_spawning(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager = _manager(tmp_path)
    wait_entered = threading.Event()
    wait_release = threading.Event()
    process = _FakeProcess(
        301,
        exit_on_terminate=True,
        wait_entered=wait_entered,
        wait_release=wait_release,
    )
    _install_processes(monkeypatch, [process])
    monkeypatch.setattr("hearthstone_companion_under_test.overlay_manager.time.sleep", lambda _value: None)
    manager.start()
    stop_result: list[dict[str, Any]] = []
    stop_thread = threading.Thread(target=lambda: stop_result.append(manager.stop(timeout=0.2)))
    stop_thread.start()
    assert wait_entered.wait(1.0)

    assert manager.start() == {
        "ok": False,
        "running": True,
        "error_code": "overlay_stopping",
        "pid": 301,
    }
    wait_release.set()
    stop_thread.join(1.0)

    assert stop_thread.is_alive() is False
    assert stop_result[0]["ok"] is True
