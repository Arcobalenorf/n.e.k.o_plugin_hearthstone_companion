from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _strict_bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    """Accept only real booleans; malformed explicit values fail closed."""
    if key not in data:
        return default
    value = data[key]
    return value if isinstance(value, bool) else False


@dataclass(slots=True)
class CompanionConfig:
    monitor_on_start: bool = True
    log_path: str = ""
    poll_interval_seconds: float = 0.1
    initial_read_max_bytes: int = 64 * 1024 * 1024
    llm_commentary_enabled: bool = False
    llm_data_consent: bool = False
    llm_min_priority: int = 5
    llm_cooldown_seconds: float = 25.0
    llm_critical_cooldown_seconds: float = 8.0
    llm_max_reply_chars: int = 28
    user_chat_quiet_window_seconds: float = 30.0
    target_lanlan: str = ""
    card_catalog_network_enabled: bool = True
    card_catalog_refresh_hours: float = 24.0
    overlay_enabled: bool = True
    overlay_auto_start: bool = False
    overlay_window_titles: str = "Hearthstone|炉石传说|爐石戰記|하스스톤"
    overlay_height_percent: int = 32
    overlay_font_size: int = 24
    overlay_speed_px_per_second: float = 150.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CompanionConfig":
        data = dict(value or {})
        return cls(
            monitor_on_start=_strict_bool(data, "monitor_on_start", True),
            log_path=str(data.get("log_path") or "").strip(),
            poll_interval_seconds=_bounded_float(data.get("poll_interval_seconds"), 0.1, 0.1, 5.0),
            initial_read_max_bytes=_bounded_int(
                data.get("initial_read_max_bytes"), 64 * 1024 * 1024, 1024 * 1024, 64 * 1024 * 1024
            ),
            llm_commentary_enabled=_strict_bool(data, "llm_commentary_enabled", False),
            llm_data_consent=_strict_bool(data, "llm_data_consent", False),
            llm_min_priority=_bounded_int(data.get("llm_min_priority"), 5, 1, 10),
            llm_cooldown_seconds=_bounded_float(data.get("llm_cooldown_seconds"), 25.0, 5.0, 300.0),
            llm_critical_cooldown_seconds=_bounded_float(
                data.get("llm_critical_cooldown_seconds"), 8.0, 2.0, 120.0
            ),
            llm_max_reply_chars=_bounded_int(data.get("llm_max_reply_chars"), 28, 8, 80),
            user_chat_quiet_window_seconds=_bounded_float(
                data.get("user_chat_quiet_window_seconds"), 30.0, 0.0, 120.0
            ),
            target_lanlan=str(data.get("target_lanlan") or "").strip()[:80],
            card_catalog_network_enabled=_strict_bool(data, "card_catalog_network_enabled", True),
            card_catalog_refresh_hours=_bounded_float(
                data.get("card_catalog_refresh_hours"), 24.0, 6.0, 168.0
            ),
            overlay_enabled=_strict_bool(data, "overlay_enabled", True),
            overlay_auto_start=_strict_bool(data, "overlay_auto_start", False),
            overlay_window_titles=str(
                data.get("overlay_window_titles") or "Hearthstone|炉石传说|爐石戰記|하스스톤"
            ).strip()[:240],
            overlay_height_percent=_bounded_int(data.get("overlay_height_percent"), 32, 15, 80),
            overlay_font_size=_bounded_int(data.get("overlay_font_size"), 24, 14, 48),
            overlay_speed_px_per_second=_bounded_float(
                data.get("overlay_speed_px_per_second"), 150.0, 60.0, 360.0
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    def public_dict(self) -> dict[str, Any]:
        return self.to_dict()
