from __future__ import annotations

import argparse
import ctypes
import json
import math
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import Any

BACKGROUND = "#010203"
NARRATION_COLOR = "#F4F7FA"
CATGIRL_COLOR = "#FF8FAF"
CRITICAL_COLOR = "#FFD166"
SHADOW_COLOR = "#111418"
MAX_QUEUE = 100


def parse_window_titles(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip().lower() for part in str(value or "").split("|") if part.strip()))


def find_window_rect(title_keywords: tuple[str, ...]) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32" or not title_keywords:
        return None
    user32 = ctypes.windll.user32
    virtual_x = user32.GetSystemMetrics(76)
    virtual_y = user32.GetSystemMetrics(77)
    virtual_w = user32.GetSystemMetrics(78)
    virtual_h = user32.GetSystemMetrics(79)
    found: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.lower()
        if not any(keyword in title for keyword in title_keywords):
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        on_screen = not (
            rect.right <= virtual_x
            or rect.left >= virtual_x + virtual_w
            or rect.bottom <= virtual_y
            or rect.top >= virtual_y + virtual_h
        )
        if width > 320 and height > 240 and on_screen:
            found.append((rect.left, rect.top, width, height))
        return True

    user32.EnumWindows(callback, 0)
    return max(found, key=lambda item: item[2] * item[3]) if found else None


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _make_click_through(root: Any) -> bool:
    if sys.platform != "win32":
        return False
    try:
        hwnd = root.winfo_id()
        get_style = ctypes.windll.user32.GetWindowLongW
        set_style = ctypes.windll.user32.SetWindowLongW
        current = get_style(hwnd, -20)
        required = 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000
        set_style(hwnd, -20, current | required)
        return get_style(hwnd, -20) & required == required
    except Exception:
        return False


class _InputReader(threading.Thread):
    def __init__(self, messages: queue.Queue[dict[str, Any]], stopped: threading.Event) -> None:
        super().__init__(name="overlay-input", daemon=True)
        self.messages = messages
        self.stopped = stopped

    def run(self) -> None:
        for raw in sys.stdin:
            if self.stopped.is_set():
                return
            if len(raw) > 8192:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("type") != "danmaku":
                continue
            text = " ".join(str(payload.get("text") or "").split())[:120]
            if not text:
                continue
            try:
                priority = int(payload.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            item = {
                "text": text,
                "priority": max(0, min(10, priority)),
                "style": "catgirl" if payload.get("style") == "catgirl" else "narration",
            }
            try:
                self.messages.put_nowait(item)
            except queue.Full:
                try:
                    self.messages.get_nowait()
                    self.messages.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass
        self.stopped.set()


class OverlayApp:
    def __init__(
        self,
        *,
        titles: tuple[str, ...],
        height_percent: int,
        font_size: int,
        speed: float,
    ) -> None:
        import tkinter as tk

        self.tk = tk
        self.titles = titles
        self.height_percent = max(15, min(80, height_percent))
        self.font_size = max(14, min(48, font_size))
        self.speed = max(60.0, min(360.0, speed))
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=BACKGROUND)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BACKGROUND)
        except Exception:
            self.root.attributes("-alpha", 0.92)
        self.canvas = tk.Canvas(self.root, bg=BACKGROUND, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUE)
        self.pending: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUE)
        self.stopped = threading.Event()
        self.reader = _InputReader(self.messages, self.stopped)
        self.reader.start()
        self.items: list[dict[str, Any]] = []
        self.top_ids: tuple[int, int] | None = None
        self.top_expires_at = 0.0
        self.width = 0
        self.height = 0
        self.visible = False
        self.last_tick = time.monotonic()
        self.root.after(50, self._tick)
        self.root.after(100, self._sync_window)

    def run(self) -> int:
        self.root.mainloop()
        self.stopped.set()
        return 0

    def _sync_window(self) -> None:
        if self.stopped.is_set():
            self.root.destroy()
            return
        rect = find_window_rect(self.titles)
        if rect is None:
            if self.visible:
                self.root.withdraw()
                self.visible = False
        else:
            x, y, width, full_height = rect
            height = max(120, math.floor(full_height * self.height_percent / 100))
            if (width, height) != (self.width, self.height):
                self.width, self.height = width, height
                self.canvas.configure(width=width, height=height)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            if not self.visible:
                self.root.update_idletasks()
                if not _make_click_through(self.root):
                    self.root.withdraw()
                    self.stopped.set()
                    self.root.after_idle(self.root.destroy)
                    return
                self.root.deiconify()
                self.root.lift()
                self.visible = True
        self.root.after(500, self._sync_window)

    def _tick(self) -> None:
        if self.stopped.is_set():
            self.root.destroy()
            return
        now = time.monotonic()
        elapsed = min(0.1, max(0.0, now - self.last_tick))
        self.last_tick = now
        self._drain_messages()
        self._emit_pending(now)
        self._animate(elapsed, now)
        self.root.after(16, self._tick)

    def _drain_messages(self) -> None:
        for _ in range(12):
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                return
            if message["priority"] >= 8:
                self._show_top(message)
            else:
                self.pending.append(message)

    def _show_top(self, message: dict[str, Any]) -> None:
        if self.width <= 0:
            self.pending.appendleft(message)
            return
        if self.top_ids:
            for item_id in self.top_ids:
                self.canvas.delete(item_id)
        text = message["text"]
        x, y = self.width / 2, max(34, self.font_size * 1.7)
        font = ("Microsoft YaHei UI", self.font_size + 2, "bold")
        shadow = self.canvas.create_text(x + 2, y + 2, text=text, fill=SHADOW_COLOR, font=font, anchor="center")
        foreground = self.canvas.create_text(x, y, text=text, fill=CRITICAL_COLOR, font=font, anchor="center")
        self.top_ids = (shadow, foreground)
        self.top_expires_at = time.monotonic() + 4.5

    def _emit_pending(self, now: float) -> None:
        if self.width <= 0 or not self.pending:
            return
        line_height = max(self.font_size + 16, int(self.font_size * 1.65))
        top_margin = max(58, self.font_size * 3)
        lane_count = max(1, (self.height - top_margin - 14) // line_height)
        for _ in range(min(4, len(self.pending))):
            lane = self._available_lane(lane_count)
            if lane is None:
                return
            message = self.pending.popleft()
            y = top_margin + lane * line_height
            text = message["text"]
            color = CATGIRL_COLOR if message["style"] == "catgirl" else NARRATION_COLOR
            font = ("Microsoft YaHei UI", self.font_size, "bold")
            x = self.width + 24
            shadow = self.canvas.create_text(x + 2, y + 2, text=text, fill=SHADOW_COLOR, font=font, anchor="w")
            foreground = self.canvas.create_text(x, y, text=text, fill=color, font=font, anchor="w")
            bbox = self.canvas.bbox(foreground)
            width = float((bbox[2] - bbox[0]) if bbox else max(60, len(text) * self.font_size))
            self.items.append(
                {
                    "ids": (shadow, foreground),
                    "x": float(x),
                    "width": width,
                    "lane": lane,
                    "speed": self.speed * (0.92 + min(0.18, len(text) / 200)),
                    "created_at": now,
                }
            )

    def _available_lane(self, lane_count: int) -> int | None:
        for lane in range(lane_count):
            right_edges = [item["x"] + item["width"] for item in self.items if item["lane"] == lane]
            if not right_edges or max(right_edges) < self.width - 140:
                return lane
        return None

    def _animate(self, elapsed: float, now: float) -> None:
        kept: list[dict[str, Any]] = []
        for item in self.items:
            distance = item["speed"] * elapsed
            item["x"] -= distance
            for item_id in item["ids"]:
                self.canvas.move(item_id, -distance, 0)
            if item["x"] + item["width"] < -20:
                for item_id in item["ids"]:
                    self.canvas.delete(item_id)
            else:
                kept.append(item)
        self.items = kept
        if self.top_ids and now >= self.top_expires_at:
            for item_id in self.top_ids:
                self.canvas.delete(item_id)
            self.top_ids = None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hearthstone companion transparent overlay")
    parser.add_argument("--window-titles", default="Hearthstone|炉石传说|爐石戰記|하스스톤")
    parser.add_argument("--height-percent", type=int, default=32)
    parser.add_argument("--font-size", type=int, default=24)
    parser.add_argument("--speed", type=float, default=150.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        return 2
    _enable_dpi_awareness()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    app = OverlayApp(
        titles=parse_window_titles(args.window_titles),
        height_percent=args.height_percent,
        font_size=args.font_size,
        speed=args.speed,
    )
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
