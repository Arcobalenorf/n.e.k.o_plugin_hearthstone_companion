from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import queue
import sys
import tempfile
import types
from enum import Enum
from pathlib import Path
from typing import Any


class _LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class _Logger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _HostContext:
    def __init__(self, plugin_root: Path, logger: _Logger) -> None:
        self.plugin_id = "hearthstone_companion"
        self.metadata: dict[str, object] = {}
        self.logger = logger
        self.config_path = plugin_root / "plugin.toml"
        self.bus = None
        self.message_queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._effective_config = {
            "plugin": {"store": {"enabled": False}},
            "plugin_state": {"persist_mode": "off"},
        }

    async def get_own_config(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        return {
            "config": {
                "hearthstone_companion": {
                    "monitor_on_start": False,
                    "card_catalog_network_enabled": False,
                    "overlay_auto_start": False,
                }
            }
        }

    def push_message(self, **_kwargs: Any) -> dict[str, bool]:
        return {"submitted": True}

    def update_status(self, _status: dict[str, object]) -> None:
        return None


def _install_stable_sdk(sdk_root: Path) -> tuple[type[Any], Any]:
    plugin_path = sdk_root / "plugin"
    if not (plugin_path / "sdk" / "plugin" / "base.py").is_file():
        raise RuntimeError(f"N.E.K.O Plugin SDK not found under {sdk_root}")

    plugin_package = types.ModuleType("plugin")
    plugin_package.__path__ = [str(plugin_path)]
    sys.modules["plugin"] = plugin_package

    logging_module = types.ModuleType("plugin.logging_config")
    logging_module.LogLevel = _LogLevel
    logging_module.configure_default_logger = lambda *_args, **_kwargs: None
    logging_module.format_log_text = lambda value, *_args, **_kwargs: str(value)
    logging_module.get_logger = lambda *_args, **_kwargs: _Logger()
    logging_module.intercept_standard_logging = lambda *_args, **_kwargs: None
    logging_module.setup_logging = lambda *_args, **_kwargs: None
    sys.modules["plugin.logging_config"] = logging_module

    from plugin.sdk.plugin import NekoPluginBase, unwrap_or

    if hasattr(NekoPluginBase, "plugin_dir") or hasattr(NekoPluginBase, "cache_path"):
        raise RuntimeError("smoke test requires the stable SDK surface without main-only path APIs")
    if not hasattr(NekoPluginBase, "config_dir") or not hasattr(NekoPluginBase, "data_path"):
        raise RuntimeError("stable SDK path APIs are unavailable")
    return NekoPluginBase, unwrap_or


def _load_plugin(plugin_root: Path) -> types.ModuleType:
    package_name = "hearthstone_companion_stable_sdk_smoke"
    package = types.ModuleType(package_name)
    package.__file__ = str(plugin_root / "__init__.py")
    package.__package__ = package_name
    package.__path__ = [str(plugin_root)]
    sys.modules[package_name] = package

    module_name = f"{package_name}.entry"
    spec = importlib.util.spec_from_file_location(module_name, plugin_root / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Hearthstone plugin entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


async def _exercise_lifecycle(plugin: Any, unwrap_or: Any) -> None:
    shutdown: dict[str, Any] = {}
    try:
        startup = unwrap_or(await plugin.startup(), {})
        if startup.get("status") != "ready":
            raise RuntimeError(f"unexpected startup result: {startup!r}")
        if startup.get("monitor_started") is not False:
            raise RuntimeError("stable SDK smoke unexpectedly started the log monitor")
        if startup.get("card_catalog_started") is not False:
            raise RuntimeError("stable SDK smoke unexpectedly started the network catalog")
    finally:
        shutdown = unwrap_or(await plugin.shutdown(), {})
    if shutdown.get("status") != "stopped":
        raise RuntimeError(f"unexpected shutdown result: {shutdown!r}")
    if plugin._store_writer.is_running():
        raise RuntimeError("statistics writer remained alive after shutdown")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, required=True)
    args = parser.parse_args()
    sdk_root = args.sdk_root.resolve()
    plugin_root = args.plugin_root.resolve()

    with tempfile.TemporaryDirectory(prefix="neko-hearthstone-sdk-smoke-") as temp_dir:
        previous_storage_root = os.environ.get("NEKO_STORAGE_SELECTED_ROOT")
        os.environ["NEKO_STORAGE_SELECTED_ROOT"] = temp_dir
        try:
            stable_base, unwrap_or = _install_stable_sdk(sdk_root)
            entry = _load_plugin(plugin_root)
            plugin = entry.HearthstoneCompanionPlugin(_HostContext(plugin_root, _Logger()))
            if not isinstance(plugin, stable_base):
                raise RuntimeError("plugin did not inherit the stable SDK base class")
            if plugin._overlay.plugin_dir != plugin.config_dir:
                raise RuntimeError("overlay root does not use stable config_dir")
            expected_catalog = plugin.data_path(
                "battlegrounds", "hsbg-cards-current-v1.json.gz"
            )
            if plugin._catalog.cache_file != expected_catalog:
                raise RuntimeError("catalog path does not use stable data_path")
            asyncio.run(_exercise_lifecycle(plugin, unwrap_or))
        finally:
            if previous_storage_root is None:
                os.environ.pop("NEKO_STORAGE_SELECTED_ROOT", None)
            else:
                os.environ["NEKO_STORAGE_SELECTED_ROOT"] = previous_storage_root

    print("N.E.K.O v0.8.3 SDK constructor and lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
