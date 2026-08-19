from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.models import (
    BattlegroundsSnapshot,
    GameEvent,
    GameSnapshot,
)
from hearthstone_companion_under_test.stats import BattlegroundsStats

PACKAGE_NAME = "hearthstone_companion_under_test"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _decorator(*args: Any, **kwargs: Any):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda target: target


def _load_sdk_entry(monkeypatch: pytest.MonkeyPatch):
    plugin_package = types.ModuleType("plugin")
    plugin_package.__path__ = []
    sdk_package = types.ModuleType("plugin.sdk")
    sdk_package.__path__ = []
    sdk_module = types.ModuleType("plugin.sdk.plugin")

    class FakePluginBase:
        def __init__(self, ctx: Any) -> None:
            self.ctx = ctx
            self.logger = getattr(ctx, "logger", None) or ctx.fallback_logger
            self.config = ctx.config
            self.store = ctx.store

        @property
        def config_dir(self) -> Path:
            return Path(self.ctx.config_path).parent

        def data_path(self, *parts: str) -> Path:
            return Path(self.ctx.data_dir).joinpath(*parts)

        def report_status(self, _status: dict[str, Any]) -> None:
            return None

    sdk_module.Err = lambda value: value
    sdk_module.NekoPluginBase = FakePluginBase
    sdk_module.Ok = lambda value: value
    sdk_module.SdkError = RuntimeError
    sdk_module.lifecycle = _decorator
    sdk_module.llm_tool = _decorator
    sdk_module.message = _decorator
    sdk_module.neko_plugin = _decorator
    sdk_module.plugin_entry = _decorator
    sdk_module.tr = lambda _key, default="": default
    sdk_module.ui = types.SimpleNamespace(action=_decorator, context=_decorator)
    sdk_module.unwrap_or = lambda value, default: default if value is None else value

    monkeypatch.setitem(sys.modules, "plugin", plugin_package)
    monkeypatch.setitem(sys.modules, "plugin.sdk", sdk_package)
    monkeypatch.setitem(sys.modules, "plugin.sdk.plugin", sdk_module)

    module_name = f"{PACKAGE_NAME}.sdk_entry"
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_plugin_constructs_and_starts_with_stable_sdk_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    logger = types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "hearthstone_companion": {
                    "monitor_on_start": False,
                    "card_catalog_network_enabled": False,
                    "overlay_auto_start": False,
                }
            }

    class Store:
        async def get(self, _key: str) -> None:
            return None

        async def set(self, _key: str, _value: dict[str, Any]) -> None:
            return None

    plugin_dir = tmp_path / "installed" / "hearthstone_companion"
    data_dir = tmp_path / "runtime-data"
    plugin_dir.mkdir(parents=True)
    ctx = types.SimpleNamespace(
        logger=None,
        fallback_logger=logger,
        config=Config(),
        store=Store(),
        config_path=plugin_dir / "plugin.toml",
        data_dir=data_dir,
    )

    plugin = entry.HearthstoneCompanionPlugin(ctx)

    assert not hasattr(plugin, "plugin_dir")
    assert not hasattr(plugin, "cache_path")
    assert plugin.logger is logger
    assert plugin._overlay.plugin_dir == plugin_dir
    assert plugin._catalog.cache_file == (
        data_dir / "battlegrounds" / "hsbg-cards-current-v1.json.gz"
    )

    startup_result = asyncio.run(plugin.startup())
    assert startup_result["status"] == "ready"
    assert startup_result["monitor_started"] is False
    assert startup_result["card_catalog_started"] is False

    shutdown_result = asyncio.run(plugin.shutdown())
    assert shutdown_result["status"] == "stopped"


def test_legacy_none_push_receipt_is_treated_as_accepted(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    assert entry._submitted(None) is True
    assert entry._submitted({"submitted": True}) is True
    assert entry._submitted({"submitted": False}) is False


def test_legacy_none_push_receipt_preserves_targeted_context_lifecycle(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs)

    assert plugin._dispatch_llm(
        "structured event prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 101.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )
    assert plugin._dispatch_llm(
        "terminal prompt",
        GameEvent("battlegrounds_game_ended", 10, "ended", 102.0, {"placement": 1}),
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )

    assert [item["ai_behavior"] for item in submitted] == ["read", "respond", "respond", "read"]
    assert submitted[-1]["metadata"]["context_expired"] is True
    assert plugin._context_target is None


def test_legacy_none_push_receipt_counts_default_target_submission(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs)
    monitor = entry.CompanionMonitor(
        plugin.cfg,
        types.SimpleNamespace(warning=lambda *_args: None),
        on_llm=plugin._dispatch_llm,
    )

    monitor._handle_event(
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
        100.0,
    )

    assert [item["ai_behavior"] for item in submitted] == ["respond"]
    assert "target_lanlan" not in submitted[0]
    assert monitor.status().llm_submissions == 1


def test_startup_failure_rolls_back_started_workers(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    class Writer:
        running = False

        def start(self) -> bool:
            self.running = True
            return True

        def stop(self, **_kwargs: Any) -> bool:
            self.running = False
            return True

        def is_running(self) -> bool:
            return self.running

    writer = Writer()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        monitor_on_start=False,
        card_catalog_network_enabled=False,
        overlay_auto_start=False,
    )
    plugin._reload_config = no_op
    plugin._load_stats = no_op
    plugin._store_writer = writer
    plugin._catalog = types.SimpleNamespace(start=lambda: False, stop=lambda **_kwargs: True)
    plugin._overlay = types.SimpleNamespace(stop=lambda **_kwargs: {"ok": True, "running": False})
    plugin._monitor = None
    plugin._ensure_monitor = lambda: (_ for _ in ()).throw(RuntimeError("monitor init failed"))
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._settings_lock = asyncio.Lock()
    plugin._context_target = None
    plugin._started = False
    plugin._monitor_dispatch_enabled = False
    plugin._settings_transition = False
    plugin.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="monitor init failed"):
        asyncio.run(plugin.startup())

    assert writer.is_running() is False
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


def test_llm_state_tool_does_not_read_state_without_data_consent(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    def fail_if_called():
        raise AssertionError("monitor must not be read without explicit LLM data consent")

    fake_plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_commentary_enabled=True, llm_data_consent=False),
        _ensure_monitor=fail_if_called,
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(fake_plugin))

    assert result == {
        "available": False,
        "state": {},
        "privacy_scope": "public_game_state_only",
        "reason": "llm_data_sharing_not_authorized",
    }


def test_llm_state_tool_returns_public_state_with_consent_even_when_proactive_is_off(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    public_state = {"phase": "playing", "turn": 7}
    snapshot = types.SimpleNamespace(
        phase="playing",
        game_number=3,
        battlegrounds=None,
        to_public_dict=lambda: public_state,
    )
    now = time.time()
    monitor = types.SimpleNamespace(
        snapshot=lambda: snapshot,
        status=lambda: types.SimpleNamespace(
            source_state="watching",
            monitor_running=True,
            last_line_at=now,
            last_event_at=now,
        ),
    )
    fake_plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_commentary_enabled=False, llm_data_consent=True),
        _ensure_monitor=lambda: monitor,
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(fake_plugin))

    assert result["available"] is True
    assert result["state"] == public_state
    assert result["freshness"]["source"] == "live"
    assert result["reason"] == ""
    assert result["privacy_scope"] == "public_game_state_only"


def test_game_context_is_read_silently_and_visible_words_use_respond(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(mode="battlegrounds", phase="playing")

    plugin._observe_game_event(
        GameEvent("battlegrounds_detected", 8, "detected", 100.0, {}), snapshot
    )
    assert submitted[-1]["visibility"] == []
    assert submitted[-1]["ai_behavior"] == "read"
    assert submitted[-1]["target_lanlan"] == "兰兰A"
    assert submitted[-1]["metadata"]["context_expired"] is False

    assert plugin._dispatch_llm(
        "structured event prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 101.0, {}),
        snapshot,
    )
    assert submitted[-1]["visibility"] == []
    assert submitted[-1]["ai_behavior"] == "respond"
    assert submitted[-1]["metadata"]["kind"] == "catgirl_commentary"

    plugin._observe_game_event(
        GameEvent("battlegrounds_game_ended", 10, "ended", 102.0, {"placement": 1}),
        snapshot,
    )
    assert submitted[-1]["ai_behavior"] == "read"
    assert "场景结束" in submitted[-1]["parts"][0]["text"]
    assert submitted[-1]["metadata"]["context_expired"] is True
    assert plugin._context_target is None


def test_recent_user_chat_suppresses_noncritical_proactive_commentary(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        user_chat_quiet_window_seconds=30.0,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = time.time()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    accepted = plugin._dispatch_llm(
        "prompt",
        GameEvent("battlegrounds_recruit_started", 7, "recruit", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    assert accepted is False
    assert submitted == []


def test_recent_user_chat_suppresses_priority_eight_but_not_nine(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        user_chat_quiet_window_seconds=30.0,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = time.time()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(mode="battlegrounds", phase="playing")

    assert not plugin._dispatch_llm(
        "priority eight",
        GameEvent("battlegrounds_hero_damaged", 8, "damage", 100.0, {}),
        snapshot,
    )
    assert plugin._dispatch_llm(
        "priority nine",
        GameEvent("battlegrounds_triple", 9, "triple", 101.0, {}),
        snapshot,
    )
    assert [item["ai_behavior"] for item in submitted] == ["read", "respond"]


def test_critical_commentary_queues_same_key_tombstone_after_terminal_response(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = time.time()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    assert plugin._dispatch_llm(
        "older queued prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 99.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )
    terminal = GameEvent(
        "battlegrounds_game_ended", 10, "ended", 100.0, {"placement": 2}
    )
    accepted = plugin._dispatch_llm(
        "terminal prompt",
        terminal,
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )
    plugin._observe_game_event(terminal, GameSnapshot(mode="battlegrounds", phase="ended"))

    assert accepted is True
    assert [item["ai_behavior"] for item in submitted] == ["read", "respond", "respond", "read"]
    assert "# 炉石猫娘陪玩场景" in submitted[2]["parts"][0]["text"]
    assert "context_expired" not in submitted[2]["metadata"]
    assert submitted[3]["metadata"]["context_expired"] is True
    assert submitted[0]["coalesce_key"] == submitted[3]["coalesce_key"]
    assert submitted[1]["coalesce_key"] == submitted[2]["coalesce_key"]
    assert submitted[2]["coalesce_key"] != submitted[3]["coalesce_key"]
    assert submitted[3]["target_lanlan"] == "兰兰A"
    assert plugin._context_target is None


def test_context_injection_rejection_prevents_visible_response(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": False}

    accepted = plugin._dispatch_llm(
        "prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    assert accepted is False
    assert len(submitted) == 1
    assert submitted[0]["ai_behavior"] == "read"


def test_target_change_restores_old_role_before_injecting_and_responding_to_new_role(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    assert plugin._inject_context() is True

    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰B",
    )
    accepted = plugin._dispatch_llm(
        "prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    assert accepted is True
    assert [item.get("target_lanlan") for item in submitted] == ["兰兰A", "兰兰A", "兰兰B", "兰兰B"]
    assert submitted[0]["coalesce_key"] == submitted[1]["coalesce_key"]
    assert submitted[1]["coalesce_key"] != submitted[2]["coalesce_key"]
    assert submitted[1]["metadata"]["context_expired"] is True
    assert plugin._context_target == "兰兰B"


def test_empty_target_never_freezes_role_hints_across_messages(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    asyncio.run(plugin.on_chat_message(_ctx={"lanlan_name": "兰兰A"}))
    plugin._last_user_chat_at = 0.0
    assert plugin._dispatch_llm(
        "first prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    asyncio.run(plugin.on_chat_message(_ctx={"lanlan_name": "兰兰B"}))
    plugin._last_user_chat_at = 0.0
    assert plugin._dispatch_llm(
        "second prompt",
        GameEvent("battlegrounds_hero_damaged", 9, "damage", 101.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    assert [item["ai_behavior"] for item in submitted] == ["respond", "respond"]
    assert all("target_lanlan" not in item for item in submitted)
    assert all("coalesce_key" not in item for item in submitted)
    assert all(entry.HEARTHSTONE_CONTEXT_INSTRUCTIONS in item["parts"][0]["text"] for item in submitted)
    assert plugin._context_target is None


def test_stable_target_uses_only_explicit_configuration(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰B")

    assert plugin._stable_target() == ""
    assert plugin._stable_target(CompanionConfig(target_lanlan="兰兰A")) == "兰兰A"


def test_sdk_routes_context_and_commentary_when_no_private_role_is_available(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    assert plugin._dispatch_llm(
        "oversized untrusted prompt " * 200,
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )
    assert plugin._dispatch_llm(
        "terminal prompt",
        GameEvent("battlegrounds_game_ended", 10, "ended", 101.0, {"placement": 1}),
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )

    assert [item["ai_behavior"] for item in submitted] == ["respond", "respond"]
    assert all("target_lanlan" not in item for item in submitted)
    assert all("coalesce_key" not in item for item in submitted)
    assert all(entry.HEARTHSTONE_CONTEXT_INSTRUCTIONS in item["parts"][0]["text"] for item in submitted)
    assert all(
        len(item["parts"][0]["text"]) <= entry._LLM_DELIVERY_MAX_CHARS
        for item in submitted
    )
    assert plugin._context_target is None


def test_reload_config_uses_only_native_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    effective = CompanionConfig(
        log_path="native.log",
        target_lanlan="lanlan-a",
        overlay_font_size=31,
    ).to_dict()

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {entry._CONFIG_SECTION: dict(effective)}

    class Store:
        async def get(self, _key: str) -> object:
            raise AssertionError("settings must not be loaded from Plugin Store")

        async def delete(self, _key: str) -> object:
            raise AssertionError("settings must not be deleted from Plugin Store")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin.config = Config()
    plugin.store = Store()
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(configure=lambda _config: None)
    plugin._monitor = None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    asyncio.run(plugin._reload_config())

    assert plugin.cfg.log_path == "native.log"
    assert plugin.cfg.target_lanlan == "lanlan-a"
    assert plugin.cfg.overlay_font_size == 31
    assert plugin.cfg.initial_read_max_bytes == 64 * 1024 * 1024


def test_save_settings_patches_only_explicit_fields(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    patches: list[dict[str, Any]] = []
    current = CompanionConfig(
        log_path="custom.log",
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
        overlay_height_percent=41,
    ).to_dict()

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            patches.append(patch)
            current.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: dict(current)}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.config = Config()
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(overlay_font_size=29))

    assert result["llm_enabled"] is True
    assert patches == [{entry._CONFIG_SECTION: {"overlay_font_size": 29}}]
    assert plugin.cfg.log_path == "custom.log"
    assert plugin.cfg.target_lanlan == "兰兰A"
    assert plugin.cfg.overlay_height_percent == 41
    assert plugin.cfg.overlay_font_size == 29


def test_save_settings_succeeds_without_hearthstone_or_power_log(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    current = CompanionConfig(monitor_on_start=True).to_dict()
    persisted: list[dict[str, Any]] = []
    monitor_updates: list[CompanionConfig] = []

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            persisted.append(patch)
            current.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: dict(current)}

    monitor = types.SimpleNamespace(
        update_config=lambda config: monitor_updates.append(config),
        status=lambda: (_ for _ in ()).throw(AssertionError("save must not inspect log status")),
        snapshot=lambda: (_ for _ in ()).throw(AssertionError("save must not read game state")),
        start=lambda: (_ for _ in ()).throw(AssertionError("save must not start Hearthstone monitoring")),
    )
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="")
    plugin.config = Config()
    plugin._monitor = monitor
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: monitor

    result = asyncio.run(
        plugin.save_settings(
            llm_data_consent=True,
            llm_commentary_enabled=True,
        )
    )

    assert result["llm_enabled"] is True
    assert persisted == [
        {
            entry._CONFIG_SECTION: {
                "llm_commentary_enabled": True,
                "llm_data_consent": True,
            }
        }
    ]
    assert len(monitor_updates) == 1


def test_save_settings_creates_default_profile_for_stable_sdk(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    patches: list[dict[str, Any]] = []
    profiles: list[tuple[str, dict[str, Any]]] = []
    update_attempts = 0
    current = CompanionConfig(log_path="custom.log").to_dict()
    MissingProfileValidationError = type("ValidationError", (Exception,), {})

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            nonlocal update_attempts
            update_attempts += 1
            if update_attempts == 1:
                raise MissingProfileValidationError("no active profile")
            patches.append(patch)
            current.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: dict(current)}

        async def profile_ensure_active(
            self,
            name: str,
            initial: dict[str, Any],
            **_kwargs: Any,
        ) -> str:
            profiles.append((name, initial))
            return name

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(
        info=lambda *_args: None,
        warning=lambda *_args: None,
    )
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(llm_data_consent=True))

    assert result["llm_enabled"] is False
    assert update_attempts == 2
    assert patches == [{entry._CONFIG_SECTION: {"llm_data_consent": True}}]
    assert profiles == [
        ("default", {entry._CONFIG_SECTION: CompanionConfig(log_path="custom.log").to_dict()})
    ]
    assert plugin.cfg.llm_data_consent is True


def test_save_settings_uses_runtime_config_when_stable_sdk_has_no_profile(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    current = CompanionConfig(log_path="custom.log").to_dict()
    updates: list[dict[str, Any]] = []
    MissingProfileValidationError = type("ValidationError", (Exception,), {})

    class Config:
        async def update(self, _patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            raise MissingProfileValidationError("no active profile")

        async def profile_ensure_active(self, *_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("ctx.upsert_own_profile_config is not available")

        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {entry._CONFIG_SECTION: dict(current)}

    class Context:
        _current_lanlan = "兰兰A"

        async def update_own_config(
            self,
            patch: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            updates.append(patch)
            current.update(patch[entry._CONFIG_SECTION])
            return {
                "success": True,
                "persisted": True,
                "data": {"config": {entry._CONFIG_SECTION: dict(patch[entry._CONFIG_SECTION])}},
            }

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin.ctx = Context()
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(llm_data_consent=True))

    assert result["llm_enabled"] is False
    assert updates == [{entry._CONFIG_SECTION: {"llm_data_consent": True}}]
    assert plugin.cfg.log_path == "custom.log"
    assert plugin.cfg.llm_data_consent is True


def test_stable_sdk_runtime_config_fallback_rejects_unpersisted_write(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    MissingProfileValidationError = type("ValidationError", (Exception,), {})

    class Config:
        async def update(self, _patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            raise MissingProfileValidationError("no active profile")

        async def profile_ensure_active(self, *_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("ctx.upsert_own_profile_config is not available")

    class Context:
        async def update_own_config(
            self,
            _patch: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "success": False,
                "persisted": False,
                "config": {entry._CONFIG_SECTION: CompanionConfig().to_dict()},
                "message": "disk write timed out",
            }

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin.ctx = Context()
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(info=lambda *_args: None)

    with pytest.raises(RuntimeError, match="disk write timed out"):
        asyncio.run(plugin._persist_settings_config({"llm_data_consent": True}))


def test_stable_sdk_profile_timeout_does_not_retry_through_runtime_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    direct_calls = 0
    MissingProfileValidationError = type("ValidationError", (Exception,), {})

    class Config:
        async def update(self, _patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            raise MissingProfileValidationError("no active profile")

        async def profile_ensure_active(self, *_args: Any, **_kwargs: Any) -> str:
            raise TimeoutError("profile persistence timed out")

    class Context:
        async def update_own_config(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal direct_calls
            direct_calls += 1
            return {}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin.ctx = Context()
    plugin.config = Config()

    with pytest.raises(TimeoutError, match="profile persistence timed out"):
        asyncio.run(plugin._persist_settings_config({"llm_data_consent": True}))
    assert direct_calls == 0


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "config": {}, "message": "rejected"},
        {"persisted": False, "config": {}},
        {"persisted": None, "config": {}},
    ],
)
def test_persisted_config_unwrap_rejects_nonpersistent_responses(
    monkeypatch,
    response: dict[str, Any],
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    with pytest.raises(RuntimeError, match="persistence failed"):
        entry._unwrap_persisted_config(response)


@pytest.mark.parametrize(
    "response",
    [
        {"hearthstone_companion": {"llm_data_consent": True}},
        {"config": {"hearthstone_companion": {"llm_data_consent": True}}},
        {"data": {"config": {"hearthstone_companion": {"llm_data_consent": True}}}},
    ],
)
def test_persisted_config_unwrap_accepts_supported_success_shapes(
    monkeypatch,
    response: dict[str, Any],
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    assert entry._unwrap_persisted_config(response) == {
        entry._CONFIG_SECTION: {"llm_data_consent": True}
    }


def test_consent_revocation_blocks_dispatch_before_config_write_finishes(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr

    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        entered = asyncio.Event()
        release = asyncio.Event()
        persisted: list[dict[str, Any]] = []
        submitted: list[dict[str, Any]] = []

        class Config:
            async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
                persisted.append(patch)
                entered.set()
                await release.wait()
                section = CompanionConfig(
                    llm_commentary_enabled=False,
                    llm_data_consent=False,
                    target_lanlan="兰兰A",
                ).to_dict()
                return {entry._CONFIG_SECTION: section}

        monitor = types.SimpleNamespace(update_config=lambda _config: None)
        overlay = types.SimpleNamespace(
            status=lambda: {"running": False},
            configure=lambda _config: None,
        )
        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin.cfg = CompanionConfig(
            llm_commentary_enabled=True,
            llm_data_consent=True,
            target_lanlan="兰兰A",
        )
        plugin._context_target = "兰兰A"
        plugin._ownership_lock = threading.RLock()
        plugin._settings_lock = asyncio.Lock()
        plugin._settings_transition = False
        plugin._last_user_chat_at = 0.0
        plugin._started = True
        plugin._monitor_dispatch_enabled = True
        plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
        plugin.config = Config()
        plugin._overlay = overlay
        plugin._ensure_monitor = lambda: monitor
        plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

        task = asyncio.create_task(
            plugin.save_settings(
                llm_commentary_enabled=False,
                llm_data_consent=False,
                target_lanlan="兰兰A",
            )
        )
        await entered.wait()
        assert plugin.cfg.llm_data_consent is False
        assert plugin._settings_transition is True
        assert plugin._dispatch_llm(
            "must not be sent",
            GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
            GameSnapshot(mode="battlegrounds", phase="playing"),
        ) is False
        release.set()
        return await task, submitted, persisted

    result, submitted, persisted = asyncio.run(scenario())

    assert result["llm_enabled"] is False
    assert len(submitted) == 1
    assert submitted[0]["ai_behavior"] == "read"
    assert submitted[0]["metadata"]["context_expired"] is True
    assert persisted[0][entry._CONFIG_SECTION]["llm_data_consent"] is False


def test_failed_context_restore_still_persists_and_applies_consent_revocation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr
    persisted: list[dict[str, Any]] = []

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            persisted.append(patch)
            section = CompanionConfig(
                llm_commentary_enabled=False,
                llm_data_consent=False,
                target_lanlan="兰兰A",
            ).to_dict()
            return {entry._CONFIG_SECTION: section}

    async def scenario() -> tuple[Any, Any]:
        monitor = types.SimpleNamespace(update_config=lambda _config: None)
        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin.cfg = CompanionConfig(
            llm_commentary_enabled=True,
            llm_data_consent=True,
            target_lanlan="兰兰A",
        )
        plugin._context_target = "兰兰A"
        plugin._ownership_lock = threading.RLock()
        plugin._settings_lock = asyncio.Lock()
        plugin._settings_transition = False
        plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
        plugin.config = Config()
        plugin._overlay = types.SimpleNamespace(
            status=lambda: {"running": False},
            configure=lambda _config: None,
        )
        plugin._ensure_monitor = lambda: monitor
        plugin.push_message = lambda **_kwargs: {"submitted": False}
        result = await plugin.save_settings(
            llm_commentary_enabled=False,
            llm_data_consent=False,
            target_lanlan="兰兰A",
        )
        return result, plugin

    result, plugin = asyncio.run(scenario())

    assert isinstance(result, FakeErr)
    assert persisted[0][entry._CONFIG_SECTION]["llm_data_consent"] is False
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert plugin._settings_transition is False
    assert plugin._context_target == "兰兰A"


def test_reload_and_save_settings_are_serialized(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def scenario() -> Any:
        dump_entered = asyncio.Event()
        release_dump = asyncio.Event()
        update_entered = asyncio.Event()
        catalog_configs: list[tuple[bool, float]] = []

        class Store:
            async def get(self, _key: str) -> None:
                return None

        class Config:
            async def dump(self, **_kwargs: Any) -> dict[str, Any]:
                dump_entered.set()
                await release_dump.wait()
                return {
                    entry._CONFIG_SECTION: CompanionConfig(
                        llm_commentary_enabled=True,
                        llm_data_consent=True,
                    ).to_dict()
                }

            async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
                update_entered.set()
                merged = CompanionConfig(
                    llm_commentary_enabled=True,
                    llm_data_consent=True,
                ).to_dict()
                merged.update(patch[entry._CONFIG_SECTION])
                return {entry._CONFIG_SECTION: merged}

        monitor = types.SimpleNamespace(update_config=lambda _config: None)
        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin.cfg = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)
        plugin._context_target = None
        plugin._ownership_lock = threading.RLock()
        plugin._settings_lock = asyncio.Lock()
        plugin._settings_transition = False
        plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
        plugin.config = Config()
        plugin.store = Store()
        plugin._monitor = monitor
        plugin._catalog = types.SimpleNamespace(
            configure=lambda *, network_enabled, refresh_hours: catalog_configs.append(
                (network_enabled, refresh_hours)
            )
        )
        plugin._overlay = types.SimpleNamespace(
            status=lambda: {"running": False},
            configure=lambda _config: None,
        )
        plugin._ensure_monitor = lambda: monitor

        reload_task = asyncio.create_task(plugin._reload_config())
        await dump_entered.wait()
        save_task = asyncio.create_task(
            plugin.save_settings(
                llm_commentary_enabled=False,
                llm_data_consent=False,
                card_catalog_network_enabled=False,
            )
        )
        await asyncio.sleep(0)
        assert not update_entered.is_set()
        release_dump.set()
        await reload_task
        result = await save_task
        return result, plugin, catalog_configs

    result, plugin, catalog_configs = asyncio.run(scenario())

    assert result["llm_enabled"] is False
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert plugin.cfg.card_catalog_network_enabled is False
    assert catalog_configs[-1] == (False, 24.0)
    assert plugin._settings_transition is False


def test_save_settings_serializes_with_overlay_start(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

        def is_err(self) -> bool:
            return True

    entry.Err = FakeErr

    async def scenario() -> tuple[Any, Any, Any]:
        update_entered = asyncio.Event()
        release_update = asyncio.Event()

        class Config:
            async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
                update_entered.set()
                await release_update.wait()
                merged = CompanionConfig().to_dict()
                merged.update(patch[entry._CONFIG_SECTION])
                return {entry._CONFIG_SECTION: merged}

        class Overlay:
            def __init__(self) -> None:
                self.config = CompanionConfig(overlay_enabled=True)
                self.running = False
                self.start_calls = 0

            def status(self) -> dict[str, Any]:
                return {"running": self.running}

            def configure(self, config: CompanionConfig) -> None:
                self.config = config

            def start(self) -> dict[str, Any]:
                self.start_calls += 1
                if not self.config.overlay_enabled:
                    return {"ok": False, "running": False, "error_code": "overlay_disabled"}
                self.running = True
                return {"ok": True, "running": True}

        overlay = Overlay()
        monitor = types.SimpleNamespace(update_config=lambda _config: None)
        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin.cfg = CompanionConfig(overlay_enabled=True)
        plugin._context_target = None
        plugin._ownership_lock = threading.RLock()
        plugin._settings_lock = asyncio.Lock()
        plugin._settings_transition = False
        plugin._started = True
        plugin.config = Config()
        plugin._overlay = overlay
        plugin._ensure_monitor = lambda: monitor

        save_task = asyncio.create_task(plugin.save_settings(overlay_enabled=False))
        await update_entered.wait()
        start_task = asyncio.create_task(plugin.start_overlay())
        await asyncio.sleep(0)
        assert start_task.done() is False
        release_update.set()
        return await save_task, await start_task, overlay

    save_result, start_result, overlay = asyncio.run(scenario())

    assert save_result["llm_enabled"] is False
    assert isinstance(start_result, FakeErr)
    assert overlay.config.overlay_enabled is False
    assert overlay.running is False
    assert overlay.start_calls == 1


def test_settings_transition_resets_when_runtime_apply_raises(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def scenario() -> Any:
        class Config:
            async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
                merged = CompanionConfig().to_dict()
                merged.update(patch[entry._CONFIG_SECTION])
                return {entry._CONFIG_SECTION: merged}

        def fail_update(_config: CompanionConfig) -> None:
            raise RuntimeError("apply failed")

        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin.cfg = CompanionConfig()
        plugin._context_target = None
        plugin._ownership_lock = threading.RLock()
        plugin._settings_lock = asyncio.Lock()
        plugin._settings_transition = False
        plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
        plugin.config = Config()
        plugin._overlay = types.SimpleNamespace(
            status=lambda: {"running": False},
            configure=lambda _config: None,
        )
        plugin._ensure_monitor = lambda: types.SimpleNamespace(update_config=fail_update)
        with pytest.raises(RuntimeError, match="apply failed"):
            await plugin.save_settings(llm_commentary_enabled=False, llm_data_consent=False)
        return plugin

    plugin = asyncio.run(scenario())

    assert plugin._settings_transition is False


def test_clear_stats_timeout_confirms_serial_compensation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    release = threading.Event()
    writes: list[dict[str, Any]] = []

    async def write(value: dict[str, Any]) -> object:
        if not value["seasons"]:
            await asyncio.to_thread(release.wait)
        writes.append(value)
        return object()

    writer = entry.AsyncStoreWriter(
        write,
        types.SimpleNamespace(warning=lambda *_args: None),
    )
    writer.start()
    wait_for_write = writer.write_and_wait
    writer.write_and_wait = lambda value, **_kwargs: wait_for_write(
        value,
        timeout=0.05 if not value["seasons"] else 1.0,
    )

    stats = BattlegroundsStats()
    stats.record_game(season="S14", mode="solo", placement=2, hero_id="BG_HERO_1")
    previous = stats.to_store_dict()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = stats
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._store_writer = writer
    plugin._stats_store_error_code = ""
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    release_timer = threading.Timer(0.1, release.set)
    release_timer.start()
    assert plugin._clear_battlegrounds_stats() is False
    release_timer.join()
    assert plugin._stats.to_store_dict() == previous
    assert plugin._stats_store_error_code == ""
    assert writer.stop(timeout=1.0)
    assert writes == [{"schema_version": 1, "seasons": {}}, previous]


def test_clear_stats_store_error_confirms_compensation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="S14", mode="solo", placement=3, hero_id="BG_HERO_1")
    previous = stats.to_store_dict()
    calls: list[tuple[dict[str, Any], float]] = []
    results = iter((False, True))

    class Writer:
        def write_and_wait(self, value: dict[str, Any], *, timeout: float) -> bool:
            calls.append((value, timeout))
            return next(results)

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = stats
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._store_writer = Writer()
    plugin._stats_store_error_code = "old:error"
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    assert plugin._clear_battlegrounds_stats() is False
    assert plugin._stats.to_store_dict() == previous
    assert [value for value, _timeout in calls] == [
        {"schema_version": 1, "seasons": {}},
        previous,
    ]
    assert all(timeout == entry._STATS_CLEAR_WRITE_TIMEOUT_SECONDS for _value, timeout in calls)
    assert plugin._stats_store_error_code == ""


def test_clear_stats_exposes_unconfirmed_compensation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="S14", mode="solo", placement=3, hero_id="BG_HERO_1")
    previous = stats.to_store_dict()
    warnings: list[str] = []

    class Writer:
        def write_and_wait(self, _value: dict[str, Any], *, timeout: float) -> bool:
            assert timeout == entry._STATS_CLEAR_WRITE_TIMEOUT_SECONDS
            return False

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = stats
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._store_writer = Writer()
    plugin._stats_store_error_code = ""
    plugin.logger = types.SimpleNamespace(warning=lambda message, *_args: warnings.append(message))

    assert plugin._clear_battlegrounds_stats() is False
    assert plugin._stats.to_store_dict() == previous
    assert plugin._stats_store_error_code == "stats:clear_compensation_unconfirmed"
    assert warnings == ["Battlegrounds statistics Store compensation was not confirmed"]


def test_stats_store_read_error_does_not_overwrite_unknown_history(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    warnings: list[str] = []
    writes: list[dict[str, Any]] = []

    class StoreReadErr:
        def is_err(self) -> bool:
            return True

    class Store:
        async def get(self, _key: str) -> StoreReadErr:
            return StoreReadErr()

    class Writer:
        def submit(self, value: dict[str, Any]) -> bool:
            writes.append(value)
            return True

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.store = Store()
    plugin.logger = types.SimpleNamespace(warning=lambda message, *_args: warnings.append(message))
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = False
    plugin._stats_store_error_code = ""
    plugin._stats_submission_lock = threading.RLock()
    plugin._store_writer = Writer()

    asyncio.run(plugin._load_stats())
    plugin._record_battlegrounds_result(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "ended",
            100.0,
            {"placement": 2, "variant": "solo", "hero_card_id": "BG_HERO_1"},
        ),
        GameSnapshot(),
    )

    assert plugin._stats_loaded is False
    assert plugin._stats_store_error_code == "stats:load_store_err"
    assert plugin._stats.to_public_dict()["seasons"] == {}
    assert writes == []
    assert plugin._clear_battlegrounds_stats() is False
    assert warnings == [
        "Battlegrounds statistics Store read returned Err",
        "Battlegrounds result skipped because statistics were not loaded",
    ]


def test_stats_store_read_exception_does_not_block_core_startup_path(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    warnings: list[str] = []

    class Store:
        async def get(self, _key: str) -> None:
            raise OSError("store unavailable")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.store = Store()
    plugin.logger = types.SimpleNamespace(warning=lambda message, *_args: warnings.append(message))
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = False
    plugin._stats_store_error_code = ""

    asyncio.run(plugin._load_stats())

    assert plugin._stats_loaded is False
    assert plugin._stats_store_error_code == "stats:load:OSError"
    assert plugin._stats.to_public_dict()["seasons"] == {}
    assert warnings == ["Battlegrounds statistics Store read failed code=%s"]


def test_successful_empty_stats_load_enables_future_persistence(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Store:
        async def get(self, _key: str) -> None:
            return None

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.store = Store()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = False
    plugin._stats_store_error_code = "stats:old_error"

    asyncio.run(plugin._load_stats())

    assert plugin._stats_loaded is True
    assert plugin._stats_store_error_code == ""


def test_result_submit_failure_is_exposed_in_dashboard_storage_status(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Writer:
        def submit(self, _value: dict[str, Any]) -> bool:
            return False

        def last_error_code(self) -> str:
            return ""

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = True
    plugin._season = {"key": "S14"}
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_store_error_code = ""
    plugin._store_writer = Writer()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._ensure_monitor = lambda: types.SimpleNamespace(
        status=lambda: types.SimpleNamespace(to_dict=lambda: {}),
        snapshot=GameSnapshot,
    )
    plugin._overlay = types.SimpleNamespace(status=lambda: {})
    plugin._catalog_status = lambda: {}

    plugin._record_battlegrounds_result(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "ended",
            100.0,
            {"placement": 2, "variant": "solo", "hero_card_id": "BG_HERO_1"},
        ),
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )
    state = plugin._dashboard_state()

    assert state["battlegrounds_stats_storage"] == {
        "degraded": True,
        "error_code": "stats:writer_unavailable",
    }


def test_async_store_error_is_exposed_in_dashboard_storage_status(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Err:
        pass

    async def fail_write(_value: dict[str, Any]) -> Err:
        return Err()

    writer = entry.AsyncStoreWriter(
        fail_write,
        types.SimpleNamespace(warning=lambda *_args: None),
    )
    writer.start()
    assert writer.write_and_wait({"schema_version": 1, "seasons": {}}) is False

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._stats = BattlegroundsStats()
    plugin._season = {"key": "S14"}
    plugin._stats_store_error_code = ""
    plugin._store_writer = writer
    plugin._ensure_monitor = lambda: types.SimpleNamespace(
        status=lambda: types.SimpleNamespace(to_dict=lambda: {}),
        snapshot=GameSnapshot,
    )
    plugin._overlay = types.SimpleNamespace(status=lambda: {})
    plugin._catalog_status = lambda: {}

    state = plugin._dashboard_state()

    assert state["battlegrounds_stats_storage"] == {
        "degraded": True,
        "error_code": "stats:store_err",
    }
    assert writer.stop(timeout=1.0) is False


def test_shutdown_leaves_host_store_open_even_when_monitor_does_not_stop(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    calls: list[str] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._context_target = None
    plugin._monitor = types.SimpleNamespace(
        stop=lambda **_kwargs: calls.append("monitor") or False
    )
    plugin._overlay = types.SimpleNamespace(
        stop=lambda **_kwargs: calls.append("overlay") or {"ok": True, "running": False}
    )
    plugin._store_writer = types.SimpleNamespace(
        stop=lambda **_kwargs: calls.append("writer") or True,
        is_running=lambda: False,
    )

    class Store:
        async def close(self) -> None:
            raise AssertionError("the SDK Store lifecycle belongs to the host")

    plugin.store = Store()

    result = asyncio.run(plugin.shutdown())

    assert isinstance(result, RuntimeError)
    assert set(calls[:2]) == {"monitor", "overlay"}
    assert calls[2:] == ["writer"]
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


def test_shutdown_reports_context_restore_error_without_closing_host_store(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

        def is_err(self) -> bool:
            return True

    entry.Err = FakeErr
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._context_target = "兰兰A"
    plugin._monitor = None
    plugin._overlay = types.SimpleNamespace(
        stop=lambda **_kwargs: {"ok": True, "running": False}
    )
    plugin._store_writer = types.SimpleNamespace(
        stop=lambda **_kwargs: True,
        is_running=lambda: False,
    )
    plugin.push_message = lambda **_kwargs: {"submitted": False}

    class Store:
        async def close(self) -> FakeErr:
            raise AssertionError("the SDK Store lifecycle belongs to the host")

    plugin.store = Store()

    result = asyncio.run(plugin.shutdown())

    assert isinstance(result, FakeErr)
    assert "context restore was rejected" in str(result.value)
    assert plugin._context_target == "兰兰A"


def test_shutdown_is_bounded_and_continues_after_component_exception(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    calls: list[str] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._context_target = None

    def fail_monitor(**_kwargs: Any) -> bool:
        calls.append("monitor")
        raise RuntimeError("monitor failed")

    def slow_catalog(*, timeout: float) -> bool:
        calls.append("catalog")
        time.sleep(min(timeout, 0.1))
        return False

    plugin._monitor = types.SimpleNamespace(stop=fail_monitor)
    plugin._catalog = types.SimpleNamespace(stop=slow_catalog)
    plugin._overlay = types.SimpleNamespace(
        stop=lambda **_kwargs: calls.append("overlay") or {"ok": True, "running": False}
    )
    plugin._store_writer = types.SimpleNamespace(
        stop=lambda **_kwargs: calls.append("writer") or True,
        is_running=lambda: False,
    )

    class Store:
        async def close(self) -> None:
            raise AssertionError("the SDK Store lifecycle belongs to the host")

    plugin.store = Store()
    started = time.monotonic()
    result = asyncio.run(plugin.shutdown())
    elapsed = time.monotonic() - started

    assert isinstance(result, RuntimeError)
    assert elapsed < 1.2
    assert set(calls) == {"monitor", "catalog", "overlay", "writer"}


def test_commentary_reports_error_when_no_output_channel_accepts(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_commentary_enabled=False, llm_data_consent=False)
    plugin._ownership_lock = threading.RLock()
    plugin._overlay = types.SimpleNamespace(push=lambda *_args, **_kwargs: False)
    plugin._ensure_monitor = lambda: types.SimpleNamespace(snapshot=GameSnapshot)

    result = asyncio.run(plugin.test_commentary())

    assert isinstance(result, RuntimeError)
    assert "no commentary output channel" in str(result)


def test_commentary_reports_error_when_character_rejects_but_overlay_accepts(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)
    plugin._ownership_lock = threading.RLock()
    plugin._context_target = None
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin._last_user_chat_at = 0.0
    plugin._overlay = types.SimpleNamespace(push=lambda *_args, **_kwargs: True)
    plugin._ensure_monitor = lambda: types.SimpleNamespace(snapshot=GameSnapshot)
    plugin.push_message = lambda **_kwargs: {"submitted": False}

    result = asyncio.run(plugin.test_commentary())

    assert isinstance(result, RuntimeError)
    assert "current NEKO character did not accept" in str(result)


def test_stop_overlay_reports_process_stop_failure(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._overlay = types.SimpleNamespace(
        stop=lambda: {
            "ok": False,
            "running": True,
            "error_code": "overlay_stop_failed",
        }
    )

    result = asyncio.run(plugin.stop_overlay())

    assert isinstance(result, RuntimeError)
    assert "overlay_stop_failed" in str(result)


def test_stop_monitoring_reports_context_restore_failure(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_dispatch_enabled = True
    plugin._context_target = "兰兰A"
    plugin._ensure_monitor = lambda: types.SimpleNamespace(stop=lambda: True)
    plugin.push_message = lambda **_kwargs: {"submitted": False}

    result = asyncio.run(plugin.stop_monitoring())

    assert isinstance(result, RuntimeError)
    assert "context restore was rejected" in str(result)


def test_monitor_start_waits_for_in_flight_stop_and_restarts_cleanly(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def scenario() -> tuple[Any, Any, Any]:
        stop_entered = threading.Event()
        release_stop = threading.Event()

        class Monitor:
            running = True

            def start(self) -> bool:
                if self.running:
                    return False
                self.running = True
                return True

            def stop(self) -> bool:
                stop_entered.set()
                release_stop.wait(1.0)
                self.running = False
                return True

        monitor = Monitor()
        plugin = object.__new__(entry.HearthstoneCompanionPlugin)
        plugin._ownership_lock = threading.RLock()
        plugin._monitor_action_lock = asyncio.Lock()
        plugin._monitor_dispatch_enabled = True
        plugin._context_target = None
        plugin._started = True
        plugin._monitor = monitor
        plugin._ensure_monitor = lambda: monitor

        stop_task = asyncio.create_task(plugin.stop_monitoring())
        assert await asyncio.to_thread(stop_entered.wait, 1.0)
        start_task = asyncio.create_task(plugin.start_monitoring())
        await asyncio.sleep(0)
        assert start_task.done() is False
        release_stop.set()
        return await stop_task, await start_task, plugin

    stop_result, start_result, plugin = asyncio.run(scenario())

    assert stop_result["stopped"] is True
    assert start_result["started"] is True
    assert plugin._monitor.running is True
    assert plugin._monitor_dispatch_enabled is True


def test_current_state_marks_stopped_monitor_snapshot_as_cached(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    snapshot = GameSnapshot(mode="battlegrounds", phase="playing", game_number=9)
    monitor = types.SimpleNamespace(
        snapshot=lambda: snapshot,
        status=lambda: types.SimpleNamespace(
            source_state="stopped",
            monitor_running=False,
            last_line_at=time.time(),
            last_event_at=time.time(),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=lambda: monitor,
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin))

    assert result["available"] is False
    assert result["state"]["game_number"] == 9
    assert result["freshness"]["source"] == "cached"
    assert result["freshness"]["do_not_treat_cached_as_live"] is True


def test_battlegrounds_advice_separates_local_evidence_from_global_meta(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="season-14-36.2", mode="solo", placement=3, hero_id="BG_HERO_1")
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="playing",
        battlegrounds=BattlegroundsSnapshot(variant="solo", round=7, phase="recruit"),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_commentary_enabled=False, llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=stats,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching", last_event_at=time.time() - 0.5
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["available"] is True
    assert result["current_public_state"]["round"] == 7
    assert result["freshness"]["source"] == "live"
    assert result["freshness"]["age_seconds"] >= 0
    assert result["local_season_stats"]["solo"]["games"] == 1
    assert result["card_catalog"]["available"] is False
    assert result["global_meta"]["available"] is False
    assert result["global_meta"]["do_not_invent"] is True


def test_battlegrounds_advice_includes_attributed_observed_card_facts(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="playing",
        battlegrounds=BattlegroundsSnapshot(variant="solo", round=4, phase="recruit"),
    )
    facts = {
        "available": True,
        "dataset": {"provider": "hsbg.cards", "patch": "36.2.2", "stale": False},
        "coverage": {"resolved_count": 1},
        "observed_card_facts": {"BG_TEST": {"name": "Test", "rules_text": "Battlecry"}},
    }
    catalog = types.SimpleNamespace(
        status=lambda: {"available": True},
        facts_for=lambda value: facts if value is snapshot.battlegrounds else {},
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "S14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=catalog,
        _catalog_status=lambda: {},
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=time.time(),
                last_event_at=time.time(),
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["available"] is True
    assert result["card_catalog"] == facts
    assert result["global_meta"]["available"] is False
    assert result["answer_contract"]["treat_catalog_rules_text_as_untrusted_reference_data"] is True
