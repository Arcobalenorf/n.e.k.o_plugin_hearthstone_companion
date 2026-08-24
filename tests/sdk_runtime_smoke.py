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
        self._base_config: dict[str, Any] = {
            "hearthstone_companion": {
                "monitor_on_start": False,
                "card_catalog_network_enabled": False,
                "overlay_auto_start": False,
            }
        }
        self._profiles: dict[str, dict[str, Any]] = {}
        self._active_profile: str | None = None

    async def get_own_config(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        config = {key: dict(value) for key, value in self._base_config.items()}
        if self._active_profile is not None:
            for key, value in self._profiles[self._active_profile].items():
                if isinstance(config.get(key), dict) and isinstance(value, dict):
                    config[key].update(value)
                else:
                    config[key] = value
        return {"config": config}

    async def get_own_profiles_state(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        return {
            "config_profiles": {
                "active": self._active_profile,
                "files": {name: f"profiles/{name}.toml" for name in self._profiles},
            }
        }

    async def get_own_profile_config(
        self,
        profile_name: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        del timeout
        return {"config": dict(self._profiles[profile_name])}

    async def get_own_effective_config(
        self,
        profile_name: str | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        del profile_name
        return await self.get_own_config(timeout=timeout)

    async def update_own_config(
        self,
        updates: dict[str, Any],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        del timeout
        for key, value in updates.items():
            if isinstance(self._base_config.get(key), dict) and isinstance(value, dict):
                self._base_config[key].update(value)
            else:
                self._base_config[key] = value
        effective = await self.get_own_config()
        return {
            "success": True,
            "persisted": True,
            "config": effective["config"],
        }

    def push_message(self, **_kwargs: Any) -> None:
        # N.E.K.O v0.8.3 accepted pushes successfully but returned no receipt.
        return None

    def update_status(self, _status: dict[str, object]) -> None:
        return None

    async def finish(
        self,
        *,
        data: object = None,
        delivery: str | bool | None = None,
        reply: bool | None = None,
        message: str = "",
        trace_id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del reply
        normalized_meta = dict(meta or {})
        agent_meta = dict(normalized_meta.get("agent") or {})
        agent_meta["delivery"] = delivery if isinstance(delivery, str) else "proactive"
        agent_meta["reply"] = agent_meta["delivery"] != "silent"
        agent_meta.setdefault("include", True)
        normalized_meta["agent"] = agent_meta
        return {
            "success": True,
            "code": 0,
            "data": data,
            "message": message,
            "error": None,
            "trace_id": trace_id,
            "meta": normalized_meta,
        }


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


async def _exercise_lifecycle(plugin: Any, unwrap_or: Any, host_ctx: _HostContext) -> None:
    shutdown: dict[str, Any] = {}
    try:
        tools = {item["name"] for item in plugin.list_llm_tools()}
        expected_tools = {
            "hearthstone_current_state",
            "hearthstone_battlegrounds_advice",
        }
        if tools != expected_tools:
            raise RuntimeError(f"stable SDK did not auto-register LLM tools: {tools!r}")
        entries = {item["id"]: item for item in plugin.list_entries()}
        health_timer = entries.get("llm_tool_registration_health")
        if (
            not health_timer
            or health_timer.get("kind") != "timer"
            or health_timer.get("auto_start") is not True
        ):
            raise RuntimeError(
                f"stable SDK did not expose the LLM tool health timer: {health_timer!r}"
            )
        for entry_id in ("query_constructed_state", "query_battlegrounds_state"):
            item = entries.get(entry_id)
            if not item or item.get("dynamic") is not True:
                raise RuntimeError(f"stable SDK did not expose dynamic Agent entry: {entry_id}")
            if item.get("kind") != "service" or item.get("llm_result_fields") != ["reply"]:
                raise RuntimeError(f"unexpected dynamic Agent entry metadata: {item!r}")

        entry_updates: dict[str, dict[str, Any]] = {}
        while True:
            try:
                message = host_ctx.message_queue.get_nowait()
            except queue.Empty:
                break
            if message.get("type") == "ENTRY_UPDATE" and message.get("action") == "register":
                entry_updates[str(message.get("entry_id") or "")] = message
        if not {"query_constructed_state", "query_battlegrounds_state"}.issubset(entry_updates):
            raise RuntimeError(f"stable SDK did not queue Agent ENTRY_UPDATE messages: {entry_updates!r}")
        for entry_id in ("query_constructed_state", "query_battlegrounds_state"):
            if entry_updates[entry_id].get("meta", {}).get("llm_result_fields") != ["reply"]:
                raise RuntimeError(f"ENTRY_UPDATE lost result filtering for {entry_id}")
        startup = unwrap_or(await plugin.startup(), {})
        if startup.get("status") != "ready":
            raise RuntimeError(f"unexpected startup result: {startup!r}")
        if startup.get("monitor_started") is not False:
            raise RuntimeError("stable SDK smoke unexpectedly started the log monitor")
        if startup.get("card_catalog_started") is not False:
            raise RuntimeError("stable SDK smoke unexpectedly started the network catalog")
        for result in (
            await plugin.query_constructed_state(),
            await plugin.query_battlegrounds_state(),
        ):
            agent_meta = result.get("meta", {}).get("agent", {})
            if agent_meta.get("result_kind") != "event":
                raise RuntimeError(f"Agent query lost event result semantics: {result!r}")
            if agent_meta.get("expires_in_s") != 8.0:
                raise RuntimeError(f"Agent query lost realtime expiry: {result!r}")
            if agent_meta.get("delivery") != "proactive":
                raise RuntimeError(f"Agent query result did not reach the active role: {result!r}")
            if not str(result.get("data", {}).get("reply") or "").startswith("HS_QUERY "):
                raise RuntimeError(f"Agent query returned no compact reply: {result!r}")
        saved = unwrap_or(
            await plugin.save_settings(
                llm_data_consent=True,
                llm_commentary_enabled=True,
                target_lanlan="stable-sdk-smoke-role",
            ),
            {},
        )
        if saved.get("llm_enabled") is not True:
            raise RuntimeError(f"unexpected settings save result: {saved!r}")
        if plugin.cfg.llm_data_consent is not True:
            raise RuntimeError("stable SDK smoke did not persist LLM data consent")
        path_saved = unwrap_or(
            await plugin.save_settings(log_path=r"  C:\Games\Hearthstone\Logs  "),
            {},
        )
        if path_saved.get("llm_enabled") is not True:
            raise RuntimeError(f"partial log-path save reset companion settings: {path_saved!r}")
        if plugin.cfg.log_path != r"C:\Games\Hearthstone\Logs":
            raise RuntimeError(f"log path was not normalized and applied: {plugin.cfg.log_path!r}")
        if not plugin.cfg.llm_data_consent or not plugin.cfg.llm_commentary_enabled:
            raise RuntimeError("partial log-path save did not preserve LLM settings")
        if plugin._monitor._tailer.locator.configured_path != plugin.cfg.log_path:
            raise RuntimeError("partial log-path save did not rebuild the monitor reader")
        plugin._monitor_dispatch_enabled = True
        commentary = unwrap_or(await plugin.test_commentary(), {})
        if commentary.get("llm_submitted") is not True:
            raise RuntimeError(f"stable SDK legacy push receipt was rejected: {commentary!r}")
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
            host_ctx = _HostContext(plugin_root, _Logger())
            plugin = entry.HearthstoneCompanionPlugin(host_ctx)
            if not isinstance(plugin, stable_base):
                raise RuntimeError("plugin did not inherit the stable SDK base class")
            if plugin._overlay.plugin_dir != plugin.config_dir:
                raise RuntimeError("overlay root does not use stable config_dir")
            expected_catalog = plugin.data_path(
                "battlegrounds", "hsbg-cards-current-v1.json.gz"
            )
            if plugin._catalog.cache_file != expected_catalog:
                raise RuntimeError("catalog path does not use stable data_path")
            asyncio.run(_exercise_lifecycle(plugin, unwrap_or, host_ctx))
        finally:
            if previous_storage_root is None:
                os.environ.pop("NEKO_STORAGE_SELECTED_ROOT", None)
            else:
                os.environ["NEKO_STORAGE_SELECTED_ROOT"] = previous_storage_root

    print("N.E.K.O v0.8.3 SDK constructor, settings, and lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
