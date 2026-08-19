from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).with_name("data") / "battlegrounds-season.json"


def _fallback() -> dict[str, Any]:
    return {
        "key": "local-unversioned",
        "season": None,
        "patch": "unknown",
        "name": "Unknown",
        "verified_at": "",
        "source_url": "",
        "patch_source_url": "",
        "source_urls": [],
        "mechanics": [],
        "status": "unavailable",
        "is_win_rate_data": False,
    }


def load_current_battlegrounds_season() -> dict[str, Any]:
    try:
        value = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _fallback()
    required = (
        "key",
        "season",
        "patch",
        "name",
        "verified_at",
        "source_url",
        "patch_source_url",
        "source_urls",
        "mechanics",
    )
    if not isinstance(value, dict) or any(key not in value for key in required):
        return _fallback()
    value["status"] = "bundled_static"
    value["is_win_rate_data"] = False
    return value


__all__ = ["load_current_battlegrounds_season"]
