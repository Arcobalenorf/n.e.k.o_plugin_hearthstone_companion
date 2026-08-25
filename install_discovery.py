from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

_BATTLE_NET_CONFIG_MAX_BYTES = 128 * 1024
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNINSTALL_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Hearthstone"
)


def _local_windows_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if (
        os.name != "nt"
        or not _WINDOWS_DRIVE_PATH_RE.match(text)
        or text.startswith(("\\\\?\\", "\\\\.\\", "//"))
        or "://" in text
    ):
        return None
    try:
        return Path(text).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _validated_install_path(value: Any) -> Path | None:
    install_path = _local_windows_path(value)
    if install_path is None:
        return None
    try:
        if not (install_path / "Hearthstone.exe").is_file():
            return None
        if not (install_path / "Logs").is_dir():
            return None
    except OSError:
        return None
    return install_path


def _registry_install_values() -> Iterable[Any]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    values: list[Any] = []
    views = tuple(
        dict.fromkeys(
            (
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
                0,
            )
        )
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            try:
                with winreg.OpenKey(
                    hive,
                    _UNINSTALL_KEY,
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    value, _value_type = winreg.QueryValueEx(key, "InstallLocation")
            except OSError:
                continue
            values.append(value)
    return values


def _battle_net_config_path() -> Path | None:
    app_data = os.environ.get("APPDATA", "").strip()
    if not app_data:
        return None
    root = _local_windows_path(app_data)
    if root is None:
        return None
    return root / "Battle.net" / "Battle.net.config"


def _battle_net_default_install_path(path: Path | None = None) -> Path | None:
    config_path = path or _battle_net_config_path()
    if config_path is None:
        return None
    try:
        stat = config_path.stat()
        if stat.st_size <= 0 or stat.st_size > _BATTLE_NET_CONFIG_MAX_BYTES:
            return None
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    client = payload.get("Client")
    install = client.get("Install") if isinstance(client, dict) else None
    default_root = (
        install.get("DefaultInstallPath") if isinstance(install, dict) else None
    )
    library_path = _local_windows_path(default_root)
    if library_path is None:
        return None
    return _validated_install_path(library_path / "Hearthstone")


def hearthstone_install_paths() -> tuple[Path, ...]:
    """Return bounded, trusted Hearthstone install roots on Windows.

    The lookup reads only the exact uninstall entry and Battle.net's single
    DefaultInstallPath field. It never scans drives or arbitrary directories.
    """

    if os.name != "nt":
        return ()
    paths: list[Path] = []
    for raw_value in _registry_install_values():
        install_path = _validated_install_path(raw_value)
        if install_path is not None:
            paths.append(install_path)
    battle_net_path = _battle_net_default_install_path()
    if battle_net_path is not None:
        paths.append(battle_net_path)
    return tuple(dict.fromkeys(paths))


__all__ = ["hearthstone_install_paths"]
