from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import CompanionConfig


class OverlayManager:
    def __init__(self, logger: Any, *, plugin_dir: Path, config: CompanionConfig) -> None:
        self.logger = logger
        self.plugin_dir = Path(plugin_dir)
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._availability: dict[str, Any] | None = None
        self._generation = 0
        self._stopping: tuple[subprocess.Popen[str], int] | None = None
        self._starts_allowed = threading.Event()
        self._starts_allowed.set()

    def suspend_starts(self) -> None:
        self._starts_allowed.clear()

    def resume_starts(self) -> None:
        self._starts_allowed.set()

    def configure(self, config: CompanionConfig) -> None:
        with self._lock:
            self.config = config

    def availability(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._availability is not None and not refresh:
                return dict(self._availability)
        if sys.platform != "win32":
            result = {"available": False, "reason": "windows_required"}
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                probe = subprocess.run(
                    [sys.executable, "-c", "import tkinter; print(tkinter.TkVersion)"],
                    capture_output=True,
                    timeout=5,
                    creationflags=flags,
                    check=False,
                )
                result = {
                    "available": probe.returncode == 0,
                    "reason": "" if probe.returncode == 0 else "tkinter_unavailable",
                }
            except Exception:
                result = {"available": False, "reason": "python_probe_failed"}
        with self._lock:
            self._availability = result
        return dict(result)

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            stopping = self._stopping
            if process is not None and process.poll() is None:
                running_process = process
            elif stopping is not None and stopping[0].poll() is None:
                running_process = stopping[0]
            else:
                running_process = None
            running = running_process is not None
            pid = running_process.pid if running_process is not None else None
        return {
            **self.availability(),
            "running": running,
            "pid": pid,
            "stopping": bool(stopping is not None),
        }

    def start(self) -> dict[str, Any]:
        if not self._starts_allowed.is_set():
            return {
                "ok": False,
                "running": False,
                "error_code": "overlay_start_suspended",
            }
        with self._lock:
            if not self._starts_allowed.is_set():
                return {
                    "ok": False,
                    "running": False,
                    "error_code": "overlay_start_suspended",
                }
            if not self.config.overlay_enabled:
                return {
                    "ok": False,
                    "running": False,
                    "error_code": "overlay_disabled",
                }
            if self._stopping is not None:
                stopping_process = self._stopping[0]
                return {
                    "ok": False,
                    "running": stopping_process.poll() is None,
                    "error_code": "overlay_stopping",
                    "pid": stopping_process.pid,
                }
            if self._process is not None and self._process.poll() is None:
                return {"ok": True, "running": True, "already_running": True, "pid": self._process.pid}
            available = self.availability(refresh=True)
            if not available.get("available"):
                return {"ok": False, "running": False, "error_code": available.get("reason")}
            script = self.plugin_dir / "overlay_process.py"
            if not script.is_file():
                return {"ok": False, "running": False, "error_code": "overlay_script_missing"}
            if not self._starts_allowed.is_set():
                return {
                    "ok": False,
                    "running": False,
                    "error_code": "overlay_start_suspended",
                }
            command = [
                sys.executable,
                str(script),
                "--window-titles",
                self.config.overlay_window_titles,
                "--height-percent",
                str(self.config.overlay_height_percent),
                "--font-size",
                str(self.config.overlay_font_size),
                "--speed",
                str(self.config.overlay_speed_px_per_second),
            ]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    close_fds=True,
                    creationflags=flags,
                    cwd=str(self.plugin_dir),
                )
            except Exception as exc:
                return {"ok": False, "running": False, "error_code": f"start:{type(exc).__name__}"}
            self._generation += 1
            generation = self._generation
            self._process = process
        time.sleep(0.2)
        if process.poll() is not None:
            with self._lock:
                if self._process is process and self._generation == generation:
                    self._process = None
                    self._generation += 1
            return {"ok": False, "running": False, "error_code": "overlay_exited_early"}
        with self._lock:
            if self._process is not process or self._generation != generation:
                current = self._process
                running = current is not None and current.poll() is None
                return {
                    "ok": False,
                    "running": running,
                    "error_code": "overlay_start_superseded",
                    "pid": current.pid if running and current is not None else None,
                }
        try:
            self.logger.info("Hearthstone overlay started pid=%s", process.pid)
        except Exception:
            pass
        return {"ok": True, "running": True, "pid": process.pid}

    def stop(self, timeout: float = 2.0) -> dict[str, Any]:
        try:
            timeout_seconds = max(0.01, float(timeout))
        except (TypeError, ValueError):
            timeout_seconds = 2.0
        with self._lock:
            if self._stopping is not None:
                process = self._stopping[0]
                running = process.poll() is None
                return {
                    "ok": False,
                    "running": running,
                    "was_running": running,
                    "error_code": "overlay_stopping",
                    "pid": process.pid,
                }
            process = self._process
            generation = self._generation
            if process is not None:
                self._process = None
                self._generation += 1
                self._stopping = (process, generation)
        if process is None:
            return {"ok": True, "running": False, "was_running": False}
        was_running = process.poll() is None
        deadline = time.monotonic() + timeout_seconds
        errors: list[str] = []
        if was_running:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception as exc:
                errors.append(f"stdin:{type(exc).__name__}")
            self._wait_for_exit(process, min(self._remaining(deadline), timeout_seconds * 0.5), errors)
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception as exc:
                    errors.append(f"terminate:{type(exc).__name__}")
                self._wait_for_exit(process, min(self._remaining(deadline), timeout_seconds * 0.3), errors)
            if process.poll() is None:
                try:
                    process.kill()
                except Exception as exc:
                    errors.append(f"kill:{type(exc).__name__}")
                self._wait_for_exit(process, self._remaining(deadline), errors)

        running = process.poll() is None
        with self._lock:
            if self._stopping == (process, generation):
                self._stopping = None
            if running and self._process is None:
                self._generation += 1
                self._process = process
        if running:
            return {
                "ok": False,
                "running": True,
                "was_running": was_running,
                "error_code": "overlay_stop_failed",
                "pid": process.pid,
                "details": errors,
            }
        return {"ok": True, "running": False, "was_running": was_running}

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _wait_for_exit(
        process: subprocess.Popen[str],
        timeout: float,
        errors: list[str],
    ) -> None:
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=max(0.001, timeout))
        except subprocess.TimeoutExpired:
            return
        except Exception as exc:
            errors.append(f"wait:{type(exc).__name__}")

    def push(self, text: str, *, priority: int = 3, style: str = "narration") -> bool:
        content = " ".join(str(text or "").split())[:120]
        if not content:
            return False
        payload = json.dumps(
            {
                "type": "danmaku",
                "text": content,
                "priority": max(0, min(10, int(priority))),
                "style": "catgirl" if style == "catgirl" else "narration",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                if self._process is process:
                    self._process = None
                    self._generation += 1
                return False


__all__ = ["OverlayManager"]
