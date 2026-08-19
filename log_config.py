from __future__ import annotations

import configparser
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

POWER_OPTIONS = {
    "LogLevel": "1",
    "FilePrinting": "true",
    "ConsolePrinting": "false",
    "ScreenPrinting": "false",
    "Verbose": "true",
}

LOADING_SCREEN_OPTIONS = {
    "LogLevel": "1",
    "FilePrinting": "true",
    "ConsolePrinting": "false",
    "ScreenPrinting": "false",
    "Verbose": "false",
}

_MANAGED_SECTIONS = {"power": "Power", "loadingscreen": "LoadingScreen"}
_MANAGED_OPTIONS = {
    key.lower(): key
    for key in {*POWER_OPTIONS, *LOADING_SCREEN_OPTIONS}
}
_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:[#;].*)?$")
_OPTION_RE = re.compile(r"^\s*([^#;][^:=\s]*)\s*[:=]")


class LogConfigError(RuntimeError):
    """Raised when log.config cannot be updated without guessing user intent."""


def _validate_managed_sections(text: str) -> None:
    sections_seen: dict[str, str] = {}
    options_seen: dict[str, set[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).strip()
            lowered = section.lower()
            current = lowered if lowered in _MANAGED_SECTIONS else None
            if current is not None:
                if current in sections_seen:
                    raise LogConfigError(f"duplicate managed section: {_MANAGED_SECTIONS[current]}")
                if section != _MANAGED_SECTIONS[current]:
                    raise LogConfigError(f"managed section has unexpected casing: {section}")
                sections_seen[current] = section
                options_seen[current] = set()
            continue
        if current is None:
            continue
        option_match = _OPTION_RE.match(line)
        if not option_match:
            continue
        option = option_match.group(1).strip().lower()
        if option in options_seen[current]:
            raise LogConfigError(f"duplicate option in {_MANAGED_SECTIONS[current]}: {option}")
        options_seen[current].add(option)
        canonical = _MANAGED_OPTIONS.get(option)
        if canonical is not None and option_match.group(1).strip() != canonical:
            raise LogConfigError(
                f"managed option has unexpected casing in {_MANAGED_SECTIONS[current]}: {option_match.group(1).strip()}"
            )


def default_log_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "Blizzard" / "Hearthstone" / "log.config"


def ensure_power_log_config(path: Path | None = None) -> dict[str, Any]:
    target = (path or default_log_config_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8-sig")
            _validate_managed_sections(existing)
            parser.read_string(existing)
        except (UnicodeError, configparser.Error) as exc:
            raise LogConfigError("invalid Hearthstone log.config") from exc

    changed = False
    for section, options in (("Power", POWER_OPTIONS), ("LoadingScreen", LOADING_SCREEN_OPTIONS)):
        if not parser.has_section(section):
            parser.add_section(section)
            changed = True
        for key, expected in options.items():
            current = parser.get(section, key, fallback=None)
            if current is None or current.strip().lower() != expected:
                parser.set(section, key, expected)
                changed = True

    backup = target.with_suffix(target.suffix + ".neko.bak")
    if changed:
        if target.is_file() and not backup.exists():
            shutil.copy2(target, backup)
        fd, temporary_name = tempfile.mkstemp(prefix="log.config.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                parser.write(handle, space_around_delimiters=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    return {
        "changed": changed,
        "path": str(target),
        "backup_path": str(backup) if backup.exists() else "",
        "restart_required": changed,
    }


__all__ = ["LogConfigError", "default_log_config_path", "ensure_power_log_config"]
