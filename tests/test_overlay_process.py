from __future__ import annotations

import types

import pytest
from hearthstone_companion_under_test import overlay_process
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.overlay_process import _parse_args, parse_window_titles


def test_window_title_parser_normalizes_deduplicates_and_keeps_unicode() -> None:
    assert parse_window_titles(" Hearthstone |炉石传说|HEARTHSTONE|| 爐石戰記 ") == (
        "hearthstone",
        "炉石传说",
        "爐石戰記",
    )
    assert parse_window_titles("") == ()


def test_overlay_cli_parses_explicit_parameters() -> None:
    args = _parse_args(
        [
            "--window-titles",
            "Hearthstone|炉石传说",
            "--height-percent",
            "44",
            "--font-size",
            "31",
            "--speed",
            "188.5",
        ]
    )

    assert args.window_titles == "Hearthstone|炉石传说"
    assert args.height_percent == 44
    assert args.font_size == 31
    assert args.speed == 188.5


def test_overlay_cli_rejects_non_numeric_values() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--font-size", "large"])


def test_overlay_config_clamps_untrusted_numeric_parameters() -> None:
    config = CompanionConfig.from_mapping(
        {
            "overlay_height_percent": 999,
            "overlay_font_size": -10,
            "overlay_speed_px_per_second": "9999",
        }
    )

    assert config.overlay_height_percent == 80
    assert config.overlay_font_size == 14
    assert config.overlay_speed_px_per_second == 360.0


def test_click_through_requires_verified_window_style(monkeypatch) -> None:
    style = 0

    class User32:
        @staticmethod
        def GetWindowLongW(_hwnd: int, _index: int) -> int:
            return style

        @staticmethod
        def SetWindowLongW(_hwnd: int, _index: int, value: int) -> int:
            nonlocal style
            previous = style
            style = value
            return previous

    monkeypatch.setattr(overlay_process.sys, "platform", "win32")
    monkeypatch.setattr(
        overlay_process.ctypes,
        "windll",
        types.SimpleNamespace(user32=User32()),
        raising=False,
    )

    assert overlay_process._make_click_through(types.SimpleNamespace(winfo_id=lambda: 42))


def test_click_through_fails_closed_when_style_does_not_stick(monkeypatch) -> None:
    user32 = types.SimpleNamespace(
        GetWindowLongW=lambda _hwnd, _index: 0,
        SetWindowLongW=lambda _hwnd, _index, _value: 0,
    )
    monkeypatch.setattr(overlay_process.sys, "platform", "win32")
    monkeypatch.setattr(
        overlay_process.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )

    assert not overlay_process._make_click_through(types.SimpleNamespace(winfo_id=lambda: 42))
