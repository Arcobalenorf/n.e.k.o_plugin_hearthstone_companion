from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

import pytest
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.models import (
    BattlegroundsAreaSnapshot,
    BattlegroundsCardSnapshot,
    BattlegroundsChoiceSnapshot,
    BattlegroundsEconomySnapshot,
    BattlegroundsHeroChoiceSnapshot,
    BattlegroundsPlayerSnapshot,
    BattlegroundsSnapshot,
    ChoiceSnapshot,
    ConstructedCardSnapshot,
    ConstructedSideSnapshot,
    ConstructedSnapshot,
    GameEvent,
    GameSnapshot,
)
from hearthstone_companion_under_test.stats import BattlegroundsStats

PACKAGE_NAME = "hearthstone_companion_under_test"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _decorator(*args: Any, **kwargs: Any):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def decorate(target: Any) -> Any:
        target.__neko_test_decorator_kwargs__ = dict(kwargs)
        return target

    return decorate


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


def _make_lifecycle_test_plugin(
    entry: Any,
    *,
    load_stats: Callable[[], Any],
    initially_running: bool = False,
    monitor_stop_entered: threading.Event | None = None,
    release_monitor_stop: threading.Event | None = None,
) -> tuple[Any, dict[str, Any], list[str]]:
    calls: list[str] = []

    class Worker:
        def __init__(self, name: str) -> None:
            self.name = name
            self.running = initially_running

        def configure(self, **_kwargs: Any) -> None:
            calls.append(f"{self.name}.configure")

        def update_config(self, _config: CompanionConfig) -> None:
            calls.append(f"{self.name}.configure")

        def start(self) -> bool:
            calls.append(f"{self.name}.start")
            self.running = True
            return True

        def stop(self, **_kwargs: Any) -> bool:
            if self.name == "monitor" and monitor_stop_entered is not None:
                calls.append("monitor.stop.enter")
                monitor_stop_entered.set()
                assert release_monitor_stop is not None
                assert release_monitor_stop.wait(1.0)
            calls.append(f"{self.name}.stop")
            self.running = False
            return True

        def status(self) -> Any:
            return types.SimpleNamespace(monitor_running=self.running)

        def is_running(self) -> bool:
            return self.running

        def is_accepting(self) -> bool:
            return self.running

    class Overlay:
        running = initially_running

        def configure(self, _config: CompanionConfig) -> None:
            calls.append("overlay.configure")

        def resume_starts(self) -> None:
            calls.append("overlay.resume")

        def suspend_starts(self) -> None:
            calls.append("overlay.suspend")

        def start(self) -> dict[str, Any]:
            calls.append("overlay.start")
            self.running = True
            return {"ok": True, "running": True}

        def stop(self, **_kwargs: Any) -> dict[str, Any]:
            calls.append("overlay.stop")
            self.running = False
            return {"ok": True, "running": False}

    monitor = Worker("monitor")
    catalog = Worker("catalog")
    writer = Worker("writer")
    overlay = Overlay()
    workers = {
        "monitor": monitor,
        "catalog": catalog,
        "writer": writer,
        "overlay": overlay,
    }
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        monitor_on_start=True,
        card_catalog_network_enabled=False,
        overlay_enabled=False,
        overlay_auto_start=False,
    )
    plugin._read_effective_config = lambda: _return_async(plugin.cfg)
    plugin._load_stats = load_stats
    plugin._store_writer = writer
    plugin._catalog = catalog
    plugin._overlay = overlay
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(plugin.cfg.to_dict())
    plugin._ensure_monitor = lambda: monitor
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._settings_lock = asyncio.Lock()
    plugin._lifecycle_lock = asyncio.Lock()
    plugin._lifecycle_generation = 0
    plugin._startup_task = None
    plugin._context_target = None
    plugin._started = initially_running
    plugin._monitor_dispatch_enabled = initially_running
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._config_reconciled_revision = 0
    plugin._config_reconcile_task = None
    plugin._config_reconcile_accepting = initially_running
    plugin.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    return plugin, workers, calls


def _bg_area(
    *,
    round: int,
    phase: str,
    observed_at: float,
    revision: int = 1,
    complete: bool = True,
) -> BattlegroundsAreaSnapshot:
    return BattlegroundsAreaSnapshot(
        complete=complete,
        revision=revision,
        observed_at=observed_at,
        round=round,
        phase=phase,
    )


def _recruit_snapshot(
    *,
    round: int,
    observed_at: float,
    shop: tuple[BattlegroundsCardSnapshot, ...],
    hand: tuple[BattlegroundsCardSnapshot, ...] = (),
    warband: tuple[BattlegroundsCardSnapshot, ...] = (),
    gold: int = 5,
    max_gold: int = 5,
    refresh_cost: int = 1,
    upgrade_cost: int = 5,
    gold_observed_round: int | None = None,
    refresh_observed_round: int | None = None,
    upgrade_observed_round: int | None = None,
    variant: str = "solo",
    current_choice: BattlegroundsChoiceSnapshot | None = None,
) -> BattlegroundsSnapshot:
    areas = {
        "shop": _bg_area(round=round, phase="recruit", observed_at=observed_at),
        "hand": _bg_area(round=round, phase="recruit", observed_at=observed_at),
        "warband": _bg_area(round=round, phase="recruit", observed_at=observed_at),
        "economy": _bg_area(round=round, phase="recruit", observed_at=observed_at),
    }
    if current_choice is not None:
        areas["choice"] = _bg_area(round=round, phase="recruit", observed_at=observed_at)
    return BattlegroundsSnapshot(
        variant=variant,
        round=round,
        phase="recruit",
        gold=gold,
        max_gold=max_gold,
        refresh_cost=refresh_cost,
        upgrade_cost=upgrade_cost,
        shop=shop,
        hand=hand,
        warband=warband,
        current_choice=current_choice,
        economy=BattlegroundsEconomySnapshot(
            upgrade_cost=upgrade_cost,
            refresh_cost=refresh_cost,
            revision=1,
            observed_at=observed_at,
            gold_observation=_bg_area(
                round=round if gold_observed_round is None else gold_observed_round,
                phase="recruit",
                observed_at=observed_at,
            ),
            refresh_observation=_bg_area(
                round=(
                    round
                    if refresh_observed_round is None
                    else refresh_observed_round
                ),
                phase="recruit",
                observed_at=observed_at,
            ),
            upgrade_observation=_bg_area(
                round=(
                    round
                    if upgrade_observed_round is None
                    else upgrade_observed_round
                ),
                phase="recruit",
                observed_at=observed_at,
            ),
        ),
        areas=areas,
    )


def _combat_snapshot(
    *,
    round: int,
    observed_at: float,
    warband: tuple[BattlegroundsCardSnapshot, ...],
    shop: tuple[BattlegroundsCardSnapshot, ...] = (),
    variant: str = "solo",
) -> BattlegroundsSnapshot:
    return BattlegroundsSnapshot(
        variant=variant,
        round=round,
        phase="combat",
        shop=shop,
        warband=warband,
        areas={
            "warband": _bg_area(round=round, phase="combat", observed_at=observed_at),
        },
    )


async def _return_async(value: Any) -> Any:
    return value


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


def test_proactive_commentary_requires_explicit_target_even_with_cached_test_attribute(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._recent_conversation_target = "当前角色"
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

    assert submitted == []
    assert monitor.status().llm_submissions == 0


def test_startup_failure_rolls_back_started_workers(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def read_config() -> CompanionConfig:
        return plugin.cfg

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
    plugin._read_effective_config = read_config
    plugin._load_stats = no_op
    plugin._store_writer = writer
    plugin._catalog = types.SimpleNamespace(
        configure=lambda **_kwargs: None,
        start=lambda: False,
        stop=lambda **_kwargs: True,
    )
    plugin._overlay = types.SimpleNamespace(
        configure=lambda _config: None,
        stop=lambda **_kwargs: {"ok": True, "running": False},
    )
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


@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("dump_timeout", "Hearthstone config load failed: TimeoutError"),
        ("missing_section", "Hearthstone config load failed: InvalidSection"),
        ("catalog_configure", "Hearthstone startup config apply failed: catalog:RuntimeError"),
    ],
)
def test_startup_config_failure_never_starts_runtime_workers(
    monkeypatch,
    failure_kind: str,
    expected_error: str,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    starts = {"writer": 0, "catalog": 0, "monitor": 0}

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            if failure_kind == "dump_timeout":
                raise TimeoutError("host config IPC stalled")
            if failure_kind == "missing_section":
                return {}
            return {
                entry._CONFIG_SECTION: CompanionConfig(
                    monitor_on_start=True,
                    card_catalog_network_enabled=True,
                ).to_dict()
            }

    class Writer:
        def start(self) -> bool:
            starts["writer"] += 1
            return True

        def stop(self, **_kwargs: Any) -> bool:
            return True

    class Catalog:
        def configure(self, **_kwargs: Any) -> None:
            if failure_kind == "catalog_configure":
                raise RuntimeError("catalog apply failed")

        def start(self) -> bool:
            starts["catalog"] += 1
            return True

        def stop(self, **_kwargs: Any) -> bool:
            return True

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin.config = Config()
    plugin._store_writer = Writer()
    plugin._catalog = Catalog()
    plugin._overlay = types.SimpleNamespace(
        configure=lambda _config: None,
        stop=lambda **_kwargs: {"ok": True, "running": False},
    )
    plugin._monitor = None
    plugin._ensure_monitor = lambda: starts.__setitem__("monitor", starts["monitor"] + 1)
    plugin._load_stats = lambda: (_ for _ in ()).throw(
        AssertionError("Store load must not begin before config is valid")
    )
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._settings_lock = asyncio.Lock()
    plugin._context_target = None
    plugin._started = False
    plugin._monitor_dispatch_enabled = False
    plugin._settings_transition = False
    plugin.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match=expected_error):
        asyncio.run(plugin.startup())

    assert starts == {"writer": 0, "catalog": 0, "monitor": 0}
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


def test_shutdown_supersedes_blocked_startup_without_worker_resurrection(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def scenario() -> tuple[Any, Any, Any, dict[str, Any], list[str]]:
        load_entered = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release_load = asyncio.Event()

        async def load_stats() -> None:
            load_entered.set()
            try:
                await release_load.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_load.wait()

        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=load_stats,
        )
        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.wait_for(load_entered.wait(), timeout=1.0)

        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1.0)
        assert plugin._lifecycle_generation == 2
        assert plugin._started is False
        release_load.set()

        startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, startup_result, shutdown_result, workers, calls

    plugin, startup_result, shutdown_result, workers, calls = asyncio.run(scenario())

    assert isinstance(startup_result, RuntimeError)
    assert "startup superseded" in str(startup_result)
    assert shutdown_result["status"] == "stopped"
    assert not any(call.endswith(".start") for call in calls)
    assert all(not worker.running for worker in workers.values())
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False
    assert plugin._startup_task is None


def test_startup_queued_during_shutdown_runs_only_after_cleanup(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stop_entered = threading.Event()
    release_stop = threading.Event()

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
            monitor_stop_entered=stop_entered,
            release_monitor_stop=release_stop,
        )
        shutdown_task = asyncio.create_task(plugin.shutdown())
        assert await asyncio.wait_for(
            asyncio.to_thread(stop_entered.wait, 1.0),
            timeout=1.0,
        )

        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        assert not any(call.endswith(".start") for call in calls)
        release_stop.set()

        shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
        startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, startup_result, shutdown_result, workers, calls

    plugin, startup_result, shutdown_result, workers, calls = asyncio.run(scenario())

    assert shutdown_result["status"] == "stopped"
    assert startup_result["status"] == "ready"
    last_stop = max(index for index, call in enumerate(calls) if call.endswith(".stop"))
    first_start = min(index for index, call in enumerate(calls) if call.endswith(".start"))
    assert last_stop < first_start
    assert workers["monitor"].running is True
    assert workers["catalog"].running is True
    assert workers["writer"].running is True
    assert workers["overlay"].running is False
    assert plugin._started is True
    assert plugin._monitor_dispatch_enabled is True
    assert plugin._startup_task is None


def test_shutdown_queued_after_startup_reclaims_final_lifecycle_ownership(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
        )
        await plugin._lifecycle_lock.acquire()
        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        assert plugin._lifecycle_generation == 0

        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.sleep(0)
        assert plugin._lifecycle_generation == 1
        plugin._lifecycle_lock.release()

        startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, startup_result, shutdown_result, workers, calls

    plugin, startup_result, shutdown_result, workers, calls = asyncio.run(scenario())

    assert isinstance(startup_result, RuntimeError)
    assert "startup superseded" in str(startup_result)
    assert shutdown_result["status"] == "stopped"
    assert not any(call.endswith(".start") for call in calls)
    assert all(not worker.running for worker in workers.values())
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False
    assert plugin._config_reconcile_accepting is False
    assert plugin._startup_task is None


def test_startup_queued_after_shutdown_supersedes_cleanup_and_becomes_owner(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        workers["overlay"].running = False
        await plugin._lifecycle_lock.acquire()
        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.sleep(0)
        assert plugin._latest_lifecycle_request == 1

        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        assert plugin._latest_lifecycle_request == 2
        plugin._lifecycle_lock.release()

        shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
        startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, startup_result, shutdown_result, workers, calls

    plugin, startup_result, shutdown_result, workers, calls = asyncio.run(scenario())

    assert shutdown_result == {"status": "superseded", "cleanup_skipped": True}
    assert startup_result["status"] == "ready"
    assert not any(call.endswith(".stop") for call in calls)
    assert all(workers[name].running for name in ("monitor", "catalog", "writer"))
    assert workers["overlay"].running is False
    assert plugin._started is True
    assert plugin._monitor_dispatch_enabled is True
    assert plugin._config_reconcile_accepting is True
    assert plugin._startup_task is None


def test_new_startup_never_reuses_reconcile_task_cancelled_by_old_shutdown(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, Any, dict[str, Any]]:
        old_cancel_seen = asyncio.Event()
        release_old_reconcile = asyncio.Event()
        new_reconcile_started = asyncio.Event()
        plugin, workers, _calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        workers["overlay"].running = False
        plugin._config_revision = 1
        plugin._config_reconciled_revision = 0

        async def old_reconcile() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_cancel_seen.set()
                await release_old_reconcile.wait()
                raise

        async def new_reconcile() -> None:
            new_reconcile_started.set()
            plugin._config_reconciled_revision = plugin._config_revision

        old_reconcile_task = asyncio.create_task(old_reconcile())
        plugin._config_reconcile_task = old_reconcile_task
        old_reconcile_task.add_done_callback(plugin._clear_config_reconcile_task)
        plugin._reconcile_config_changes = new_reconcile
        await plugin._lifecycle_lock.acquire()
        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.wait_for(old_cancel_seen.wait(), timeout=1.0)
        assert old_reconcile_task.cancelling() > 0

        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        plugin._lifecycle_lock.release()
        try:
            shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
            await asyncio.wait_for(new_reconcile_started.wait(), timeout=1.0)
            startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        finally:
            release_old_reconcile.set()
            await asyncio.gather(old_reconcile_task, return_exceptions=True)
            if not startup_task.done():
                startup_task.cancel()
                await asyncio.gather(startup_task, return_exceptions=True)
        await asyncio.sleep(0)
        return plugin, startup_result, shutdown_result, workers

    plugin, startup_result, shutdown_result, workers = asyncio.run(scenario())

    assert shutdown_result == {"status": "superseded", "cleanup_skipped": True}
    assert startup_result["status"] == "ready"
    assert all(workers[name].running for name in ("monitor", "catalog", "writer"))
    assert plugin._config_reconciled_revision == 1
    assert plugin._config_reconcile_task is None
    assert plugin._started is True
    assert plugin._monitor_dispatch_enabled is True


@pytest.mark.parametrize(
    ("updated_enabled", "expected_start_calls"),
    [(False, 0), (True, 1)],
)
def test_new_startup_adopts_running_overlay_with_effective_runtime_config(
    monkeypatch,
    updated_enabled: bool,
    expected_start_calls: int,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, Any, Any, list[str]]:
        plugin, _workers, _worker_calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        old = CompanionConfig(
            monitor_on_start=True,
            overlay_enabled=True,
            overlay_auto_start=False,
            overlay_font_size=24,
        )
        updated_values = old.to_dict()
        updated_values.update(
            {
                "overlay_enabled": updated_enabled,
                "overlay_font_size": 31,
            }
        )
        updated = CompanionConfig.from_mapping(updated_values)
        calls: list[str] = []

        class Overlay:
            running = True
            config = old

            def status(self) -> dict[str, Any]:
                return {"running": self.running}

            def configure(self, config: CompanionConfig) -> None:
                calls.append("configure")
                self.config = config

            def resume_starts(self) -> None:
                calls.append("resume")

            def stop(self, **_kwargs: Any) -> dict[str, Any]:
                calls.append("stop")
                self.running = False
                return {"ok": True, "running": False}

            def start(self) -> dict[str, Any]:
                calls.append("start")
                self.running = True
                return {"ok": True, "running": True}

        overlay = Overlay()
        plugin.cfg = old
        plugin._overlay = overlay
        plugin._overlay_applied_config = old
        plugin._read_effective_config = lambda: _return_async(updated)
        await plugin._lifecycle_lock.acquire()
        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.sleep(0)
        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        plugin._lifecycle_lock.release()

        shutdown_result = await asyncio.wait_for(shutdown_task, timeout=1.0)
        startup_result = await asyncio.wait_for(startup_task, timeout=1.0)
        return plugin, startup_result, shutdown_result, overlay, calls

    plugin, startup_result, shutdown_result, overlay, calls = asyncio.run(scenario())

    assert shutdown_result == {"status": "superseded", "cleanup_skipped": True}
    assert startup_result["status"] == "ready"
    assert calls.count("stop") == 1
    assert calls.count("start") == expected_start_calls
    if expected_start_calls:
        assert calls.index("stop") < calls.index("start")
    assert overlay.running is updated_enabled
    assert overlay.config.overlay_font_size == 31
    assert plugin._overlay_applied_config.to_dict() == plugin.cfg.to_dict()


@pytest.mark.parametrize(
    ("case", "restore_accepted", "expect_ready", "expected_target"),
    [
        ("consent_revoked", True, True, None),
        ("target_changed", False, False, "Lanlan-A"),
        ("unchanged", True, True, "Lanlan-A"),
    ],
)
def test_startup_reconciles_or_preserves_existing_character_context(
    monkeypatch,
    case: str,
    restore_accepted: bool,
    expect_ready: bool,
    expected_target: str | None,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    restore_calls: list[str] = []

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        workers["overlay"].running = False
        previous = CompanionConfig(
            monitor_on_start=True,
            llm_data_consent=True,
            target_lanlan="Lanlan-A",
            log_path="old/Power.log",
        )
        updated_values = previous.to_dict()
        if case == "consent_revoked":
            updated_values["llm_data_consent"] = False
        elif case == "target_changed":
            updated_values["target_lanlan"] = "Lanlan-B"
        updated = CompanionConfig.from_mapping(updated_values)
        plugin.cfg = previous
        plugin._overlay_applied_config = previous
        plugin._context_target = "Lanlan-A"
        plugin._read_effective_config = lambda: _return_async(updated)

        def restore_context() -> bool:
            restore_calls.append(str(plugin._context_target))
            if restore_accepted:
                plugin._context_target = None
            return restore_accepted

        plugin._restore_context = restore_context
        if expect_ready:
            result = await plugin.startup()
        else:
            with pytest.raises(RuntimeError, match="context_restore:rejected") as raised:
                await plugin.startup()
            result = raised.value
        await asyncio.sleep(0)
        return plugin, result, workers, calls

    plugin, result, workers, calls = asyncio.run(scenario())

    if case == "unchanged":
        assert restore_calls == []
    else:
        assert restore_calls
    assert plugin._context_target == expected_target
    if expect_ready:
        assert result["status"] == "ready"
        assert plugin._started is True
    else:
        assert {"monitor.stop", "catalog.stop", "overlay.stop", "writer.stop"} <= set(calls)
        assert all(not worker.running for worker in workers.values())
        assert plugin._started is False
        assert plugin._monitor_dispatch_enabled is False


def test_cancelled_startup_rollback_cannot_clear_or_stop_new_startup(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stop_entered = threading.Event()
    release_stop = threading.Event()

    async def scenario() -> tuple[Any, Any, dict[str, Any], list[str]]:
        first_load_entered = asyncio.Event()
        second_load_entered = asyncio.Event()
        release_second_load = asyncio.Event()
        load_count = 0

        async def load_stats() -> None:
            nonlocal load_count
            load_count += 1
            if load_count == 1:
                first_load_entered.set()
                await asyncio.Event().wait()
            second_load_entered.set()
            await release_second_load.wait()

        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=load_stats,
            monitor_stop_entered=stop_entered,
            release_monitor_stop=release_stop,
        )
        old_startup = asyncio.create_task(plugin.startup())
        await asyncio.wait_for(first_load_entered.wait(), timeout=1.0)
        old_startup.cancel()
        assert await asyncio.wait_for(
            asyncio.to_thread(stop_entered.wait, 1.0),
            timeout=1.0,
        )

        new_startup = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0)
        assert plugin._startup_task is old_startup
        release_stop.set()
        await asyncio.wait_for(second_load_entered.wait(), timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await old_startup
        await asyncio.sleep(0)
        assert plugin._startup_task is new_startup

        release_second_load.set()
        startup_result = await asyncio.wait_for(new_startup, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, startup_result, workers, calls

    plugin, startup_result, workers, calls = asyncio.run(scenario())

    assert startup_result["status"] == "ready"
    assert calls.count("monitor.start") == 1
    assert calls.count("catalog.start") == 1
    assert calls.count("writer.start") == 1
    assert workers["monitor"].running is True
    assert workers["catalog"].running is True
    assert workers["writer"].running is True
    assert plugin._started is True
    assert plugin._monitor_dispatch_enabled is True
    assert plugin._startup_task is None


def test_cancelled_shutdown_still_cleans_runtime_after_claiming_lifecycle(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        await plugin._lifecycle_lock.acquire()
        shutdown_task = asyncio.create_task(plugin.shutdown())
        await asyncio.sleep(0)
        assert plugin._lifecycle_generation == 1
        assert plugin._started is False

        shutdown_task.cancel()
        plugin._lifecycle_lock.release()
        try:
            await asyncio.wait_for(shutdown_task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return plugin, workers, calls

    plugin, workers, calls = asyncio.run(scenario())

    assert {"monitor.stop", "catalog.stop", "overlay.stop", "writer.stop"} <= set(calls)
    assert all(not worker.running for worker in workers.values())
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


def test_repeated_startup_cancellation_cannot_interrupt_owned_rollback(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stop_entered = threading.Event()
    release_stop = threading.Event()

    async def scenario() -> tuple[Any, dict[str, Any], list[str]]:
        load_entered = asyncio.Event()

        async def load_stats() -> None:
            load_entered.set()
            await asyncio.Event().wait()

        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=load_stats,
            initially_running=True,
            monitor_stop_entered=stop_entered,
            release_monitor_stop=release_stop,
        )
        startup_task = asyncio.create_task(plugin.startup())
        await asyncio.wait_for(load_entered.wait(), timeout=1.0)
        startup_task.cancel()
        assert await asyncio.wait_for(
            asyncio.to_thread(stop_entered.wait, 1.0),
            timeout=1.0,
        )

        startup_task.cancel()
        release_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup_task, timeout=1.0)
        await asyncio.sleep(0)
        return plugin, workers, calls

    plugin, workers, calls = asyncio.run(scenario())

    assert {"monitor.stop", "catalog.stop", "overlay.stop", "writer.stop"} <= set(calls)
    assert all(not worker.running for worker in workers.values())
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


@pytest.mark.parametrize("unhealthy_component", ["writer", "monitor"])
def test_startup_rejects_nonstarting_unhealthy_required_worker(
    monkeypatch,
    unhealthy_component: str,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
        )
        worker = workers[unhealthy_component]

        def refuse_start() -> bool:
            calls.append(f"{unhealthy_component}.start")
            return False

        worker.start = refuse_start
        worker.running = False
        with pytest.raises(RuntimeError):
            await plugin.startup()
        await asyncio.sleep(0)
        return plugin, workers, calls

    plugin, workers, calls = asyncio.run(scenario())

    assert f"{unhealthy_component}.start" in calls
    assert {"monitor.stop", "catalog.stop", "overlay.stop", "writer.stop"} <= set(calls)
    assert all(not worker.running for worker in workers.values())
    assert plugin._started is False
    assert plugin._monitor_dispatch_enabled is False


def test_startup_accepts_nonstarting_workers_that_are_still_healthy(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    async def no_op() -> None:
        return None

    async def scenario() -> tuple[Any, Any, dict[str, Any], list[str]]:
        plugin, workers, calls = _make_lifecycle_test_plugin(
            entry,
            load_stats=no_op,
            initially_running=True,
        )
        workers["overlay"].running = False

        for name in ("writer", "catalog", "monitor"):
            worker = workers[name]

            def already_running(worker_name: str = name) -> bool:
                calls.append(f"{worker_name}.start")
                return False

            worker.start = already_running

        result = await plugin.startup()
        await asyncio.sleep(0)
        return plugin, result, workers, calls

    plugin, result, workers, calls = asyncio.run(scenario())

    assert result["status"] == "ready"
    assert result["monitor_started"] is False
    assert result["card_catalog_started"] is False
    assert all(workers[name].running for name in ("writer", "catalog", "monitor"))
    assert workers["overlay"].running is False
    assert plugin._started is True
    assert plugin._monitor_dispatch_enabled is True


def test_llm_state_tool_does_not_read_state_without_data_consent(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    def fail_if_called():
        raise AssertionError("monitor must not be read without explicit LLM data consent")

    fake_plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_commentary_enabled=True, llm_data_consent=False),
        _ensure_monitor=fail_if_called,
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(fake_plugin))

    assert result["available"] is False
    assert result["state"] == {}
    assert result["privacy_scope"] == "player_visible_game_state"
    assert result["reason"] == "llm_data_sharing_not_authorized"
    assert result["answer_contract"]["own_visible_hand_cards_included_when_observed"] is True
    assert (
        result["answer_contract"][
            "specific_card_play_analysis_requires_complete_visible_hand"
        ]
        is True
    )
    assert result["answer_contract"]["complete_legal_actions_available"] is False


@pytest.mark.parametrize("tool_name", ["current_state", "battlegrounds_advice"])
def test_live_tools_do_not_read_stale_monitor_after_plugin_stops(
    monkeypatch,
    tool_name: str,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    def fail_if_called() -> Any:
        raise AssertionError("stopped plugin must not read its previous monitor snapshot")

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _started=False,
        _settings_transition=False,
        _settings_transition_revision=4,
        _ensure_monitor=fail_if_called,
    )

    async def call_tool() -> dict[str, Any]:
        if tool_name == "current_state":
            return await entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin)
        return await entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin,
            topic="current_strategy",
        )

    result = asyncio.run(call_tool())

    assert result["available"] is False
    assert result["reason"] == "plugin_not_running"


def test_status_entry_never_routes_game_snapshot_through_llm_result_fields(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    metadata = entry.HearthstoneCompanionPlugin.get_status.__neko_test_decorator_kwargs__

    assert "game" not in metadata["llm_result_fields"]
    assert set(metadata["llm_result_fields"]) == {"summary", "runtime", "overlay", "privacy"}


def test_llm_state_tool_returns_public_state_with_consent_even_when_proactive_is_off(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    public_state = {"phase": "playing", "turn": 7}
    snapshot = types.SimpleNamespace(
        mode="constructed",
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
    assert result["privacy_scope"] == "player_visible_game_state"


def test_llm_tool_descriptions_route_battlegrounds_strategy_exclusively(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    current_metadata = entry.HearthstoneCompanionPlugin.hearthstone_current_state.__neko_test_decorator_kwargs__
    advice_metadata = entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice.__neko_test_decorator_kwargs__

    current_description = current_metadata["description"]
    advice_description = advice_metadata["description"]
    for phrase in ("turn", "active player", "hand", "which card to play", "current choices"):
        assert phrase in current_description
    assert "hearthstone_battlegrounds_advice instead" in current_description
    assert "never answer those questions as constructed" in current_description
    for keyword in (
        "Battlegrounds",
        "hero selection",
        "tavern spells",
        "warband composition and positioning",
        "leveling",
        "stabilizing",
        "purchases",
        "refreshes",
    ):
        assert keyword in advice_description
    assert "Battlegrounds-only" in advice_description
    assert "never answer with constructed decks" in advice_description
    topic_description = advice_metadata["parameters"]["properties"]["topic"]["description"]
    assert "live match" in topic_description
    assert "purchases" in topic_description
    assert "current_strategy" in topic_description
    assert "season_meta" in topic_description
    assert "never composition win-rate rankings" in topic_description


def test_constructed_current_state_exposes_fresh_analysis_capabilities(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=3,
        turn=5,
        round=3,
        active_side="player",
        constructed=ConstructedSnapshot(
            game_type="GT_RANKED",
            variant="ranked",
            player=ConstructedSideSnapshot(
                mana_available=4,
                mana_max=5,
                hand_count=1,
                known_hand=(
                    ConstructedCardSnapshot(
                        card_id="CS2_029",
                        name="火球术",
                        card_type="SPELL",
                        cost=4,
                    ),
                ),
                hand_identities_complete=True,
            ),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin))

    assert result["available"] is True
    assert result["state"]["turn"] == 5
    assert result["state"]["round"] == 3
    assert "timeline" not in result["state"]
    assert result["answer_contract"]["round_question_field"] == "state.round"
    assert result["answer_contract"]["action_turn_is_raw_alternating_counter"] is True
    assert result["state"]["constructed"]["player"]["hand"]["known_cards"][0][
        "card_id"
    ] == "CS2_029"
    assert result["capabilities"] == {
        "turn_tracking": True,
        "round_tracking": True,
        "active_side_tracking": True,
        "own_visible_hand_cards": True,
        "own_hand_identities_complete": True,
        "specific_card_play_analysis": True,
        "current_choice_options": False,
        "complete_legal_actions": False,
    }


@pytest.mark.parametrize(
    ("phase", "known_cards", "identities_complete"),
    [
        ("mulligan", True, True),
        ("playing", False, True),
        ("playing", True, False),
        ("ended", True, True),
    ],
)
def test_constructed_current_state_disables_specific_card_analysis_when_incomplete(
    monkeypatch,
    phase: str,
    known_cards: bool,
    identities_complete: bool,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    hand = (
        (
            ConstructedCardSnapshot(
                card_id="CS2_029",
                name="火球术",
                card_type="SPELL",
                cost=4,
            ),
        )
        if known_cards
        else ()
    )
    snapshot = GameSnapshot(
        mode="constructed",
        phase=phase,
        game_number=3,
        turn=5,
        round=3,
        active_side="player",
        constructed=ConstructedSnapshot(
            player=ConstructedSideSnapshot(
                hand_count=1,
                known_hand=hand,
                hand_identities_complete=identities_complete,
            )
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin))

    assert result["capabilities"]["specific_card_play_analysis"] is False


def test_context_instructions_route_current_constructed_questions_to_state_tool(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    instructions = entry.HEARTHSTONE_CONTEXT_INSTRUCTIONS

    assert "hearthstone_current_state" in instructions
    for keyword in ("第几回合", "轮到谁", "应该出什么牌", "当前手牌", "当前场面"):
        assert keyword in instructions
    assert "state.round" in instructions
    assert "state.timeline" not in instructions
    assert "hearthstone_live_state" in instructions
    assert "不是当前事实的权威来源" in instructions
    assert "不得用聊天历史里的被动提示" in instructions


def test_current_state_redirects_battlegrounds_strategy_to_advice_tool(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=2,
        battlegrounds=BattlegroundsSnapshot(round=3, phase="recruit"),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin))

    assert result["available"] is False
    assert result["state"] == {}
    assert result["reason"] == "battlegrounds_requires_specialized_tool"
    assert result["strategy_routing"] == {
        "tool": "hearthstone_battlegrounds_advice",
        "do_not_answer_strategy_from_this_tool": True,
        "do_not_answer_as_constructed": True,
    }


def test_unauthorized_battlegrounds_advice_keeps_battlegrounds_only_contract(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = types.SimpleNamespace(cfg=CompanionConfig(llm_data_consent=False))

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(plugin)
    )

    assert result["available"] is False
    assert result["reason"] == "llm_data_sharing_not_authorized"
    assert result["game_mode"] == "battlegrounds"
    assert result["scope"] == "hearthstone_battlegrounds_only"
    assert result["answer_contract"]["if_unavailable_do_not_fallback_to_constructed"] is True
    assert result["answer_contract"]["if_unavailable_do_not_recommend_from_cached_state"] is True


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


def test_bootstrap_state_ready_enters_authorized_game_context(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="兰兰A")
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    plugin._observe_game_event(
        GameEvent("state_ready", 0, "ready", 100.0, {"mode": "battlegrounds"}),
        GameSnapshot(mode="battlegrounds", phase="recruit", game_number=3),
    )

    assert len(submitted) == 1
    assert submitted[0]["ai_behavior"] == "read"
    assert submitted[0]["visibility"] == []
    assert plugin._context_target == "兰兰A"


def test_bootstrap_state_ready_respects_data_consent(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=False, target_lanlan="兰兰A")
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    plugin._observe_game_event(
        GameEvent("state_ready", 0, "ready", 100.0, {"mode": "battlegrounds"}),
        GameSnapshot(mode="battlegrounds", phase="recruit", game_number=3),
    )

    assert submitted == []


def test_live_state_is_shared_silently_to_explicit_target(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=False,
        llm_data_consent=True,
        target_lanlan="当前角色",
    )
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            round=6,
            phase="recruit",
            gold=7,
            tavern_tier=4,
            refresh_cost=0,
            upgrade_cost=6,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_RUNTIME",
                    name="实时酒馆法术",
                    card_type="SPELL",
                    position=1,
                    premium=False,
                    current_cost=1,
                    keywords={"divine_shield": True},
                ),
            ),
        ),
    )

    assert plugin._share_live_state(snapshot) is True

    assert len(submitted) == 1
    assert submitted[0]["metadata"]["segment"] == "core"
    assert submitted[0]["coalesce_key"] == plugin._live_state_key("当前角色")
    assert all(message["visibility"] == [] for message in submitted)
    assert all(message["ai_behavior"] == "read" for message in submitted)
    assert all(message["target_lanlan"] == "当前角色" for message in submitted)
    assert all(message["metadata"]["kind"] == "game_live_state" for message in submitted)
    assert all(
        message["metadata"]["privacy_scope"] == "capability_routing_only"
        for message in submitted
    )
    assert len(submitted[0]["parts"][0]["text"].encode("utf-8")) <= 900
    notice = json.loads(submitted[0]["parts"][0]["text"].split(":", 1)[1])
    assert notice["kind"] == "hearthstone_tool_routing"
    assert notice["tools"] == [
        "hearthstone_current_state",
        "hearthstone_battlegrounds_advice",
    ]
    assert set(notice) == {"kind", "tools", "rule"}
    assert "BG_SPELL_RUNTIME" not in json.dumps(notice, ensure_ascii=False)
    assert plugin._live_state_shared is True
    assert plugin._live_state_target == "当前角色"
    assert plugin._live_state_segment_count == 1


def test_unresolved_current_role_sends_untargeted_live_state_for_host_routing(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    bus_calls = 0

    def get_recent(**_kwargs: Any) -> Any:
        nonlocal bus_calls
        bus_calls += 1
        raise AssertionError("synchronous delivery must not query Conversations Bus")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._recent_conversation_target = ""
    plugin._ownership_lock = threading.RLock()
    plugin._delivery_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.bus = types.SimpleNamespace(
        conversations=types.SimpleNamespace(get=get_recent)
    )
    plugin.logger = types.SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=3, phase="recruit"),
    )

    assert plugin._publish_live_state(snapshot) is True
    assert plugin._share_live_state(snapshot) is True
    assert plugin._dispatch_llm(
        "prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        snapshot,
    ) is False
    assert bus_calls == 0
    assert len(submitted) == 1
    assert all(item["visibility"] == [] for item in submitted)
    assert all(item["ai_behavior"] == "read" for item in submitted)
    assert all("target_lanlan" not in item for item in submitted)
    assert all(
        item["metadata"]["privacy_scope"] == "capability_routing_only"
        for item in submitted
    )
    assert all(
        item["coalesce_key"] == "hearthstone:live-state:active-session"
        for item in submitted
    )
    assert plugin._live_state_shared is True
    assert plugin._live_state_target == ""


def test_live_state_expiration_replaces_pending_snapshot_with_same_key(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._recent_conversation_target = "当前角色"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=3, phase="recruit"),
    )

    assert plugin._share_live_state(snapshot) is True
    assert plugin._expire_live_state() is True

    assert [item["ai_behavior"] for item in submitted] == ["read", "read"]
    assert submitted[0]["coalesce_key"] == submitted[1]["coalesce_key"]
    assert [item["metadata"]["segment"] for item in submitted] == ["core", "core"]
    assert submitted[1]["metadata"]["kind"] == "game_live_state_expired"
    assert submitted[1]["metadata"]["context_expired"] is True
    assert "工具能力提示已失效" in submitted[1]["parts"][0]["text"]
    assert "BG_" not in submitted[1]["parts"][0]["text"]
    assert plugin._live_state_shared is False
    assert plugin._live_state_segment_count == 0


def test_live_state_rejection_does_not_mark_notice_as_shared(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._recent_conversation_target = "当前角色"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False

    def push_message(**kwargs: Any) -> dict[str, bool]:
        submitted.append(kwargs)
        return {"submitted": False}

    plugin.push_message = push_message
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            round=3,
            phase="recruit",
            shop=(BattlegroundsCardSnapshot(card_id="BG_SHOP_1", position=1),),
        ),
    )

    assert plugin._share_live_state(snapshot) is False

    assert [
        (item["metadata"]["kind"], item["metadata"]["segment"])
        for item in submitted
    ] == [("game_live_state", "core")]
    assert plugin._live_state_shared is False
    assert plugin._live_state_segment_count == 0


def test_static_capability_notice_is_not_republished_for_same_target(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._recent_conversation_target = "当前角色"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    battlegrounds = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=3, phase="recruit"),
    )
    constructed = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=3,
        round=2,
        constructed=ConstructedSnapshot(),
    )

    assert plugin._share_live_state(battlegrounds) is True
    first_count = len(submitted)
    assert first_count == 1
    assert plugin._share_live_state(constructed) is True

    assert submitted[first_count:] == []
    assert plugin._live_state_shared is True
    assert plugin._live_state_segment_count == 1


def test_restore_context_expires_untargeted_live_state_after_consent_revocation(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=False)
    plugin._context_target = None
    plugin._live_state_shared = True
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 1
    plugin._ownership_lock = threading.RLock()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    assert plugin._restore_context() is True

    assert len(submitted) == 1
    assert submitted[0]["coalesce_key"] == "hearthstone:live-state:active-session"
    assert all(item["metadata"]["context_expired"] is True for item in submitted)
    assert plugin._live_state_shared is False
    assert plugin._live_state_segment_count == 0


def test_untargeted_live_state_requires_cleanup_for_privacy_and_routing_changes(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(log_path="old.log", llm_data_consent=True)
    plugin._context_target = None
    plugin._live_state_shared = True
    plugin._live_state_target = ""

    assert plugin._context_restore_required(
        plugin.cfg,
        CompanionConfig(log_path="old.log", llm_data_consent=False),
    ) is True
    assert plugin._context_restore_required(
        plugin.cfg,
        CompanionConfig(log_path="new.log", llm_data_consent=True),
    ) is True
    assert plugin._context_restore_required(
        plugin.cfg,
        CompanionConfig(
            log_path="old.log",
            llm_data_consent=True,
            target_lanlan="lanlan-a",
        ),
    ) is True
    assert plugin._context_restore_required(
        plugin.cfg,
        CompanionConfig(
            log_path="old.log",
            llm_data_consent=True,
            llm_commentary_enabled=True,
        ),
    ) is False


def test_stale_state_restores_context_and_resumed_state_reenters_it(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = "lanlan-a"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(mode="battlegrounds", phase="recruit", game_number=3)

    plugin._observe_game_event(
        GameEvent("state_stale", 0, "stale", 100.0, {}),
        snapshot,
    )
    plugin._observe_game_event(
        GameEvent("state_resumed", 0, "resumed", 101.0, {}),
        snapshot,
    )

    assert [item["metadata"]["context_expired"] for item in submitted] == [True, False]
    assert plugin._context_target == "lanlan-a"


def test_sync_active_context_rejects_stale_snapshot(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = "lanlan-a"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(mode="battlegrounds", phase="recruit", game_number=3)
    stale_at = time.time() - 3600.0
    plugin._monitor = types.SimpleNamespace(
        snapshot=lambda: snapshot,
        status=lambda: types.SimpleNamespace(
            source_state="watching",
            monitor_running=True,
            last_line_at=stale_at,
            last_event_at=0.0,
            source_modified_at=stale_at,
        ),
    )

    plugin._sync_active_game_context()

    assert len(submitted) == 1
    assert submitted[0]["metadata"]["context_expired"] is True
    assert plugin._context_target is None
    assert plugin._context_target is None


def test_sync_active_context_returns_quickly_while_monitor_refreshes(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    timeouts: list[float] = []

    class Monitor:
        def try_capture(self, *, timeout_seconds: float) -> None:
            timeouts.append(timeout_seconds)
            return None

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            raise AssertionError("active context sync must use bounded capture")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = "lanlan-a"
    plugin._ownership_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin._monitor = Monitor()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    started_at = time.monotonic()
    plugin._sync_active_game_context()
    elapsed = time.monotonic() - started_at

    assert timeouts == [0.05]
    assert elapsed < 0.25
    assert submitted == []
    assert plugin._context_target == "lanlan-a"


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


def test_context_cleanup_rejection_keeps_target_for_retry_after_consent_race(
    monkeypatch,
) -> None:
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
    plugin._delivery_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False

    def push_message(**kwargs: Any) -> dict[str, bool]:
        submitted.append(kwargs)
        if len(submitted) == 1:
            with plugin._ownership_lock:
                plugin.cfg = CompanionConfig(
                    llm_commentary_enabled=False,
                    llm_data_consent=False,
                    target_lanlan="兰兰A",
                )
            return {"submitted": True}
        return {"submitted": False}

    plugin.push_message = push_message

    assert plugin._inject_context() is False
    assert [item["metadata"]["context_expired"] for item in submitted] == [False, True]
    assert plugin._context_target == "兰兰A"


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


def test_empty_config_ignores_uncontracted_role_hints_for_proactive_output(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace()
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    observed = asyncio.run(plugin.on_chat_message(_ctx={"lanlan_name": "兰兰A"}))
    plugin._last_user_chat_at = 0.0
    accepted = plugin._dispatch_llm(
        "first prompt",
        GameEvent("battlegrounds_triple", 9, "triple", 100.0, {}),
        GameSnapshot(mode="battlegrounds", phase="playing"),
    )

    assert observed["target_configured"] is False
    assert accepted is False
    assert submitted == []
    assert plugin._context_target is None


def test_stable_target_uses_only_explicit_configuration(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰B")

    assert plugin._stable_target() == ""
    assert plugin._stable_target(CompanionConfig(target_lanlan="兰兰A")) == "兰兰A"


def test_delivery_target_prefers_explicit_configuration_over_cache_and_bus(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(target_lanlan="配置角色")
    plugin._ownership_lock = threading.RLock()
    plugin._recent_conversation_target = "缓存角色"
    plugin.bus = types.SimpleNamespace(
        conversations=types.SimpleNamespace(
            get=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("bus called"))
        )
    )

    assert plugin._delivery_target() == "配置角色"


def test_clearing_explicit_target_returns_to_untargeted_static_notice(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    previous = CompanionConfig(target_lanlan="固定角色")
    updated = CompanionConfig(target_lanlan="")
    plugin.cfg = previous
    plugin._ownership_lock = threading.RLock()
    plugin.cfg = updated

    assert plugin._delivery_target() == ""


@pytest.mark.parametrize(
    "updated",
    [
        CompanionConfig(log_path="old.log", llm_data_consent=False),
        CompanionConfig(log_path="new.log", llm_data_consent=True),
    ],
)
def test_privacy_or_source_transition_needs_no_restore_without_active_output(
    monkeypatch,
    updated: CompanionConfig,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    previous = CompanionConfig(log_path="old.log", llm_data_consent=True)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._context_target = None
    plugin._live_state_shared = False
    assert plugin._context_restore_required(previous, updated) is False


def test_chat_observer_does_not_infer_delivery_target_from_conversations_bus(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._ownership_lock = threading.RLock()
    plugin.bus = types.SimpleNamespace(
        conversations=types.SimpleNamespace(
            get=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("bus called"))
        )
    )

    result = asyncio.run(plugin.on_chat_message())

    assert result["target_configured"] is False
    assert plugin._delivery_target() == ""


def test_restore_then_explicit_target_routes_the_new_notice(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=4, phase="recruit"),
    )
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=False)
    plugin._context_target = None
    plugin._live_state_shared = True
    plugin._live_state_target = "旧角色"
    plugin._live_state_segment_count = 1
    plugin._ownership_lock = threading.RLock()
    plugin._delivery_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    plugin._sync_active_game_context = lambda: None

    assert plugin._restore_context() is True
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="当前角色")

    assert plugin._publish_live_state(snapshot) is True
    assert [item.get("target_lanlan") for item in submitted] == ["旧角色", "当前角色"]
    assert [item["metadata"]["kind"] for item in submitted] == [
        "game_live_state_expired",
        "game_live_state",
    ]
    assert submitted[1]["coalesce_key"] == plugin._live_state_key("当前角色")


def test_blocked_live_delivery_does_not_hold_ownership_lock_during_revocation(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    push_started = threading.Event()
    release_push = threading.Event()
    result: list[bool] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._recent_conversation_target = "当前角色"
    plugin._ownership_lock = threading.RLock()
    plugin._delivery_lock = threading.RLock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False

    def push_message(**kwargs: Any) -> dict[str, bool]:
        submitted.append(kwargs)
        if kwargs["metadata"]["kind"] == "game_live_state":
            push_started.set()
            assert release_push.wait(1.0)
        return {"submitted": True}

    plugin.push_message = push_message
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=4, phase="recruit"),
    )
    worker = threading.Thread(
        target=lambda: result.append(plugin._share_live_state(snapshot)),
        daemon=True,
    )
    worker.start()
    assert push_started.wait(0.5)

    assert plugin._ownership_lock.acquire(timeout=0.2)
    try:
        plugin.cfg = CompanionConfig(llm_data_consent=False)
    finally:
        plugin._ownership_lock.release()
    release_push.set()
    worker.join(1.0)

    assert worker.is_alive() is False
    assert result == [False]
    assert plugin.cfg.llm_data_consent is False
    assert [item["metadata"]["kind"] for item in submitted] == [
        "game_live_state",
        "game_live_state_expired",
    ]
    assert plugin._live_state_shared is False


def test_conversations_bus_failure_cannot_affect_explicit_only_target(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._ownership_lock = threading.RLock()
    plugin.bus = types.SimpleNamespace(
        conversations=types.SimpleNamespace(
            get=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable"))
        )
    )

    assert plugin._delivery_target() == ""


def test_conversations_bus_property_is_not_read_for_delivery_target(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._ownership_lock = threading.RLock()

    class BrokenBus:
        @property
        def conversations(self) -> object:
            raise RuntimeError("unavailable")

    plugin.bus = BrokenBus()

    assert plugin._delivery_target() == ""


def test_explicit_target_switch_expires_old_notice_before_sharing_to_new_role(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="角色A")
    plugin._context_target = None
    plugin._live_state_shared = True
    plugin._live_state_target = "角色A"
    plugin._live_state_segment_count = 1
    plugin._ownership_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=4, phase="recruit"),
    )
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="角色B")

    assert plugin._publish_live_state(snapshot) is True
    assert [item["target_lanlan"] for item in submitted] == ["角色A", "角色B"]
    assert [item["metadata"]["kind"] for item in submitted] == [
        "game_live_state_expired",
        "game_live_state",
    ]
    assert submitted[0]["coalesce_key"] != submitted[1]["coalesce_key"]
    assert plugin._live_state_target == "角色B"


def test_live_state_publish_has_no_private_host_http_dependency(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    assert not hasattr(entry, "_fetch_current_catgirl")
    assert not hasattr(entry, "_fetch_main_tool_registry")
    assert not hasattr(entry, "_main_server_tools_url")
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="角色A",
    )
    plugin._context_target = None
    plugin._live_state_shared = False
    plugin._live_state_target = ""
    plugin._live_state_segment_count = 0
    plugin._ownership_lock = threading.RLock()
    plugin._delivery_lock = threading.RLock()
    plugin._last_user_chat_at = 0.0
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin.logger = types.SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(round=4, phase="recruit"),
    )

    assert plugin._publish_live_state(snapshot) is True
    assert len(submitted) == 1
    assert submitted[0]["metadata"]["kind"] == "game_live_state"
    assert submitted[0]["metadata"]["privacy_scope"] == "capability_routing_only"
    assert submitted[0]["target_lanlan"] == "角色A"
    assert submitted[0]["coalesce_key"] == plugin._live_state_key("角色A")
    assert plugin._live_state_target == "角色A"


def test_sdk_routes_context_and_commentary_to_explicit_configured_role(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        llm_max_reply_chars=80,
        target_lanlan="当前角色",
    )
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
    private_hand = ConstructedCardSnapshot(
        card_id="PRIVATE_HAND_SENTINEL",
        name="只允许主动询问读取的手牌",
        card_type="SPELL",
        cost=4,
    )
    public_board = tuple(
        ConstructedCardSnapshot(
            card_id=f"PUBLIC_BOARD_{index}",
            name="公开但很长的场面随从" * 20,
            card_type="MINION",
            attack=20,
            health=20,
            keywords=("taunt", "divine_shield", "lifesteal"),
        )
        for index in range(7)
    )
    side = ConstructedSideSnapshot(
        mana_available=10,
        mana_max=10,
        hand_count=1,
        known_hand=(private_hand,),
        hand_identities_complete=True,
        board=public_board,
        weapon=public_board[0],
        hero_power=public_board[1],
        locations=(public_board[2], public_board[3]),
    )
    assert plugin._dispatch_llm(
        "oversized constructed prompt " * 200,
        GameEvent("hero_damaged", 9, "damage", 100.5, {"amount": 12}),
        GameSnapshot(
            mode="constructed",
            phase="playing",
            turn=19,
            round=10,
            active_side="player",
            constructed=ConstructedSnapshot(
                game_type="GT_RANKED_STANDARD",
                format="standard",
                variant="ranked",
                player=side,
                opponent=side,
            ),
            choice=ChoiceSnapshot(
                choice_type="discover",
                count_min=1,
                count_max=1,
                options=(
                    ConstructedCardSnapshot(
                        card_id="PRIVATE_CHOICE_SENTINEL",
                        name="只允许主动询问读取的发现选项",
                        card_type="SPELL",
                    ),
                ),
            ),
        ),
    )
    assert "PRIVATE_HAND_SENTINEL" not in submitted[-1]["parts"][0]["text"]
    assert "只允许主动询问读取的手牌" not in submitted[-1]["parts"][0]["text"]
    assert "PRIVATE_CHOICE_SENTINEL" not in submitted[-1]["parts"][0]["text"]
    assert "只允许主动询问读取的发现选项" not in submitted[-1]["parts"][0]["text"]
    assert plugin._dispatch_llm(
        "terminal prompt",
        GameEvent("battlegrounds_game_ended", 10, "ended", 101.0, {"placement": 1}),
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )

    assert [item["ai_behavior"] for item in submitted] == [
        "read",
        "respond",
        "respond",
        "respond",
        "read",
    ]
    assert all(item["target_lanlan"] == "当前角色" for item in submitted)
    assert all("coalesce_key" in item for item in submitted)
    assert submitted[0]["parts"][0]["text"] == entry.HEARTHSTONE_CONTEXT_INSTRUCTIONS
    assert entry.HEARTHSTONE_CONTEXT_INSTRUCTIONS in submitted[-2]["parts"][0]["text"]
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


def test_reload_config_failure_preserves_current_runtime_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    monitor_updates: list[CompanionConfig] = []

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("config IPC timed out")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(log_path="keep.log", overlay_font_size=31)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin.config = Config()
    plugin._monitor = types.SimpleNamespace(update_config=lambda cfg: monitor_updates.append(cfg))
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    asyncio.run(plugin._reload_config())

    assert plugin.cfg.log_path == "keep.log"
    assert plugin.cfg.overlay_font_size == 31
    assert monitor_updates == []


def test_config_change_applies_host_effective_config_without_redumping(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    monitor_updates: list[CompanionConfig] = []

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("config_change must use the host-provided effective config")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin.config = Config()
    plugin._monitor = types.SimpleNamespace(update_config=lambda cfg: monitor_updates.append(cfg))
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(
        llm_data_consent=False,
        llm_commentary_enabled=False,
        overlay_font_size=35,
    )

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        assert plugin.cfg.overlay_font_size == 35
        assert monitor_updates == []
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert result["reconcile_scheduled"] is True
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.overlay_font_size == 35
    assert monitor_updates[-1].llm_data_consent is False


def test_config_change_does_not_wait_for_settings_lock_and_revokes_consent_immediately(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    monitor_updates: list[CompanionConfig] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(
        update_config=lambda config: monitor_updates.append(config)
    )
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(
        llm_commentary_enabled=False,
        llm_data_consent=False,
    )

    async def scenario() -> dict[str, Any]:
        await plugin._settings_lock.acquire()
        try:
            result = await asyncio.wait_for(
                plugin.config_change(
                    new_config={entry._CONFIG_SECTION: updated.to_dict()}
                ),
                timeout=0.05,
            )
            assert plugin.cfg.llm_data_consent is False
            assert plugin.cfg.llm_commentary_enabled is False
            assert monitor_updates == []
        finally:
            plugin._settings_lock.release()
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert len(monitor_updates) == 1
    assert monitor_updates[0].llm_data_consent is False
    assert monitor_updates[0].llm_commentary_enabled is False


def test_older_settings_transaction_cannot_reopen_newer_consent_revocation(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    first_persist_entered: asyncio.Event | None = None
    release_first_persist: asyncio.Event | None = None
    monitor_consents: list[bool] = []
    sync_states: list[tuple[bool, bool]] = []
    base = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
        overlay_font_size=24,
    )

    async def persist(patch: dict[str, Any]) -> dict[str, Any]:
        section = patch
        values = base.to_dict()
        values.update(section)
        if "llm_data_consent" not in section:
            assert first_persist_entered is not None
            assert release_first_persist is not None
            first_persist_entered.set()
            await release_first_persist.wait()
        return {entry._CONFIG_SECTION: values}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = base
    plugin._started = True
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._consent_request_revision = 0
    plugin._consent_revocation_pending = False
    plugin._config_runtime_error_codes = ()
    plugin._config_restart_required = False
    plugin._overlay_applied_config = base
    plugin._persist_settings_config = persist
    plugin._monitor = types.SimpleNamespace(
        update_config=lambda config: monitor_consents.append(config.llm_data_consent)
    )
    plugin._ensure_monitor = lambda: plugin._monitor
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._sync_active_game_context = lambda: sync_states.append(
        (plugin.cfg.llm_data_consent, plugin._settings_transition)
    )
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> None:
        nonlocal first_persist_entered, release_first_persist
        first_persist_entered = asyncio.Event()
        release_first_persist = asyncio.Event()
        older = asyncio.create_task(plugin.save_settings(overlay_font_size=31))
        await first_persist_entered.wait()
        revocation = asyncio.create_task(
            plugin.save_settings(llm_data_consent=False)
        )
        for _ in range(10):
            await asyncio.sleep(0)
            if not plugin.cfg.llm_data_consent:
                break
        assert plugin.cfg.llm_data_consent is False
        assert plugin._settings_transition is True
        release_first_persist.set()
        await asyncio.gather(older, revocation)

    asyncio.run(scenario())

    assert monitor_consents
    assert all(consent is False for consent in monitor_consents)
    assert sync_states
    assert all(consent is False for consent, _transition in sync_states)
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert plugin._settings_transition is False


def test_config_change_coalesces_pending_revisions_to_latest_effective_config(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    applied_sizes: list[int] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_font_size=24)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(
        update_config=lambda config: applied_sizes.append(config.overlay_font_size)
    )
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> None:
        await plugin._settings_lock.acquire()
        try:
            first = await plugin.config_change(
                new_config={
                    entry._CONFIG_SECTION: CompanionConfig(
                        overlay_font_size=30
                    ).to_dict()
                }
            )
            second = await plugin.config_change(
                new_config={
                    entry._CONFIG_SECTION: CompanionConfig(
                        overlay_font_size=37
                    ).to_dict()
                }
            )
            assert first["revision"] == 1
            assert second["revision"] == 2
            assert plugin.cfg.overlay_font_size == 37
        finally:
            plugin._settings_lock.release()
        await plugin._wait_for_config_reconcile()

    asyncio.run(scenario())

    assert applied_sizes == [37]
    assert plugin._config_reconciled_revision == 2
    assert plugin._config_restart_required is False


def test_startup_config_apply_reconciles_change_accepted_while_runtime_is_blocked(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    first_update_entered = threading.Event()
    release_first_update = threading.Event()
    applied_paths: list[str] = []

    class Monitor:
        def update_config(self, config: CompanionConfig) -> None:
            applied_paths.append(config.log_path)
            if len(applied_paths) == 1:
                first_update_entered.set()
                assert release_first_update.wait(1.0)

    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    startup_config = CompanionConfig(log_path="startup/Power.log", llm_data_consent=True)
    host_config = CompanionConfig(log_path="host/Power.log", llm_data_consent=True)
    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(previous.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._config_reconciled_revision = 0
    plugin._config_reconcile_accepting = False
    plugin._config_reconcile_task = None
    plugin._context_target = None
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._sync_active_game_context = lambda: None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> None:
        configure_task = asyncio.create_task(
            asyncio.to_thread(
                plugin._configure_startup_runtime,
                startup_config,
                expected_config_revision=0,
            )
        )
        assert await asyncio.to_thread(first_update_entered.wait, 1.0)
        change = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: host_config.to_dict()}
        )
        assert change["reconcile_scheduled"] is False
        release_first_update.set()
        await configure_task
        assert plugin.cfg.log_path == "host/Power.log"
        assert plugin._settings_transition is True
        with plugin._ownership_lock:
            plugin._config_reconcile_accepting = True
        assert plugin._schedule_config_reconcile() is True
        await plugin._wait_for_config_reconcile()

    asyncio.run(scenario())

    assert applied_paths == ["startup/Power.log", "host/Power.log"]
    assert plugin.cfg.to_dict() == host_config.to_dict()
    assert plugin._monitor_applied_instance is monitor
    assert plugin._monitor_applied_config.to_dict() == host_config.to_dict()
    assert plugin._settings_transition is False
    assert plugin._config_reconciled_revision == 1


def test_stale_save_transaction_cannot_overwrite_newer_host_log_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    persist_entered: asyncio.Event | None = None
    release_persist: asyncio.Event | None = None
    monitor_updates: list[str] = []
    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    saved = CompanionConfig(log_path="save/Power.log", llm_data_consent=True)
    host = CompanionConfig(log_path="host/Power.log", llm_data_consent=True)

    async def persist(_patch: dict[str, Any]) -> dict[str, Any]:
        assert persist_entered is not None
        assert release_persist is not None
        persist_entered.set()
        await release_persist.wait()
        return {entry._CONFIG_SECTION: saved.to_dict()}

    class Monitor:
        def update_config(self, config: CompanionConfig) -> None:
            monitor_updates.append(config.log_path)

    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._started = True
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(previous.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._config_reconciled_revision = 0
    plugin._config_reconcile_accepting = True
    plugin._config_reconcile_task = None
    plugin._consent_request_revision = 0
    plugin._consent_revocation_pending = False
    plugin._context_target = None
    plugin._persist_settings_config = persist
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._sync_active_game_context = lambda: None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> Any:
        nonlocal persist_entered, release_persist
        persist_entered = asyncio.Event()
        release_persist = asyncio.Event()
        save_task = asyncio.create_task(plugin.save_settings(log_path=saved.log_path))
        await persist_entered.wait()
        await plugin.config_change(
            new_config={entry._CONFIG_SECTION: host.to_dict()}
        )
        release_persist.set()
        result = await save_task
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["summary"] == "炉石陪玩设置已保存。"
    assert plugin.cfg.to_dict() == host.to_dict()
    assert monitor_updates == ["host/Power.log"]
    assert plugin._monitor_applied_instance is monitor
    assert plugin._monitor_applied_config.to_dict() == host.to_dict()
    assert plugin._settings_transition is False


def test_stale_reload_cannot_overwrite_newer_host_log_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    read_entered: asyncio.Event | None = None
    release_read: asyncio.Event | None = None
    monitor_updates: list[str] = []
    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    stale_dump = CompanionConfig(log_path="dump/Power.log", llm_data_consent=True)
    host = CompanionConfig(log_path="host/Power.log", llm_data_consent=True)

    async def read_config() -> CompanionConfig:
        assert read_entered is not None
        assert release_read is not None
        read_entered.set()
        await release_read.wait()
        return stale_dump

    class Monitor:
        def update_config(self, config: CompanionConfig) -> None:
            monitor_updates.append(config.log_path)

    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(previous.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._config_reconciled_revision = 0
    plugin._config_reconcile_accepting = True
    plugin._config_reconcile_task = None
    plugin._consent_revocation_pending = False
    plugin._context_target = None
    plugin._read_effective_config = read_config
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._sync_active_game_context = lambda: None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> None:
        nonlocal read_entered, release_read
        read_entered = asyncio.Event()
        release_read = asyncio.Event()
        reload_task = asyncio.create_task(plugin._reload_config())
        await read_entered.wait()
        await plugin.config_change(
            new_config={entry._CONFIG_SECTION: host.to_dict()}
        )
        release_read.set()
        assert await reload_task is True
        await plugin._wait_for_config_reconcile()

    asyncio.run(scenario())

    assert plugin.cfg.to_dict() == host.to_dict()
    assert monitor_updates == ["host/Power.log"]
    assert plugin._monitor_applied_config.to_dict() == host.to_dict()
    assert plugin._settings_transition is False


def test_startup_monitor_apply_failure_retries_on_next_startup_configure(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    attempts = 0
    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    updated = CompanionConfig(log_path="new/Power.log", llm_data_consent=True)

    class Monitor:
        def update_config(self, _config: CompanionConfig) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first startup apply failed")

    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(previous.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(configure=lambda _config: None)

    with pytest.raises(RuntimeError, match="startup config apply failed"):
        plugin._configure_startup_runtime(updated, expected_config_revision=0)

    assert plugin._monitor_applied_config.to_dict() == previous.to_dict()
    assert plugin._settings_transition is False

    plugin._configure_startup_runtime(updated, expected_config_revision=0)

    assert attempts == 2
    assert plugin._monitor_applied_instance is monitor
    assert plugin._monitor_applied_config.to_dict() == updated.to_dict()
    assert plugin._settings_transition is False


def test_consent_revocation_updates_monitor_without_holding_ownership_lock(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    current = CompanionConfig(llm_commentary_enabled=True, llm_data_consent=True)

    class Monitor:
        def update_config(self, _config: CompanionConfig) -> None:
            other_thread_acquired = threading.Event()

            def acquire_from_other_thread() -> None:
                with plugin._ownership_lock:
                    other_thread_acquired.set()

            contender = threading.Thread(target=acquire_from_other_thread)
            contender.start()
            assert other_thread_acquired.wait(0.5)
            contender.join(0.5)
            assert contender.is_alive() is False

    async def persist(patch: dict[str, Any]) -> dict[str, Any]:
        values = current.to_dict()
        values.update(patch)
        return {entry._CONFIG_SECTION: values}

    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = current
    plugin._started = True
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(current.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._consent_request_revision = 0
    plugin._consent_revocation_pending = False
    plugin._context_target = None
    plugin._persist_settings_config = persist
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._sync_active_game_context = lambda: None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    result = asyncio.run(plugin.save_settings(llm_data_consent=False))

    assert result["llm_enabled"] is False
    assert plugin.cfg.llm_data_consent is False
    assert plugin._monitor_applied_config.llm_data_consent is False


def test_config_change_with_missing_effective_section_degrades_fail_closed(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("config IPC timed out")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    result = asyncio.run(plugin.config_change())

    assert result["status"] == "degraded"
    assert result["restart_required"] is True
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert plugin._config_runtime_error_codes == ("config:invalid_effective_section",)


def test_config_change_revocation_stays_fail_closed_when_context_restore_is_rejected(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    monitor_updates: list[CompanionConfig] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = "lanlan-a"
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(update_config=lambda cfg: monitor_updates.append(cfg))
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.push_message = lambda **_kwargs: {"submitted": False}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(llm_data_consent=False, target_lanlan="lanlan-a")

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        assert plugin.cfg.llm_data_consent is False
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert monitor_updates
    assert all(config.llm_data_consent is False for config in monitor_updates)
    assert plugin._context_target == "lanlan-a"
    assert "context_restore:rejected" in plugin._config_runtime_error_codes
    assert plugin._config_restart_required is True


def test_config_change_revocation_restores_context_and_resets_transition_when_monitor_raises(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []

    def fail_update(_config: CompanionConfig) -> None:
        raise RuntimeError("monitor apply failed")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="lanlan-a")
    plugin._context_target = "lanlan-a"
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(update_config=fail_update)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(llm_data_consent=False, target_lanlan="lanlan-a")

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        assert plugin.cfg.llm_data_consent is False
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False
    assert plugin._settings_transition is False
    assert plugin._context_target is None
    assert len(submitted) == 1
    assert submitted[0]["metadata"]["context_expired"] is True
    assert "monitor:RuntimeError" in plugin._config_runtime_error_codes
    assert plugin._config_restart_required is True


def test_config_change_keeps_host_config_on_non_privacy_runtime_apply_failure(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    previous = CompanionConfig(overlay_font_size=24)
    applied_sizes: list[int] = []
    def update_monitor(config: CompanionConfig) -> None:
        applied_sizes.append(config.overlay_font_size)
        raise RuntimeError("monitor apply failed")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(update_config=update_monitor)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(overlay_font_size=39)

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert plugin.cfg.overlay_font_size == 39
    assert applied_sizes == [39]
    assert plugin._config_runtime_error_codes == ("monitor:RuntimeError",)
    assert plugin._config_restart_required is True
    assert plugin._settings_transition is False


def test_config_change_does_not_restart_running_overlay_for_non_runtime_fields(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Overlay:
        def __init__(self) -> None:
            self.config = CompanionConfig(overlay_font_size=24)

        def status(self) -> dict[str, Any]:
            return {"running": True}

        def configure(self, config: CompanionConfig) -> None:
            self.config = config

        def stop(self) -> dict[str, Any]:
            raise AssertionError("unchanged overlay runtime must not restart")

        def start(self) -> dict[str, Any]:
            raise AssertionError("unchanged overlay runtime must not restart")

    overlay = Overlay()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_font_size=24, llm_min_priority=5)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = overlay
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(overlay_font_size=24, llm_min_priority=7)

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert plugin.cfg.llm_min_priority == 7
    assert overlay.config.llm_min_priority == 7


def test_config_change_reports_inconsistent_overlay_stop_without_rolling_back_host_config(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Overlay:
        def __init__(self) -> None:
            self.config = CompanionConfig(overlay_font_size=24)
            self.start_calls = 0

        def status(self) -> dict[str, Any]:
            return {"running": True}

        def configure(self, config: CompanionConfig) -> None:
            self.config = config

        def stop(self) -> dict[str, Any]:
            return {"ok": True, "running": True}

        def start(self) -> dict[str, Any]:
            self.start_calls += 1
            return {"ok": True, "running": True}

    overlay = Overlay()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_font_size=24)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = overlay
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(overlay_font_size=31)

    async def scenario() -> dict[str, Any]:
        result = await plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
        await plugin._wait_for_config_reconcile()
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "accepted"
    assert plugin.cfg.overlay_font_size == 31
    assert overlay.config.overlay_font_size == 24
    assert overlay.start_calls == 0
    assert any(
        code.startswith("overlay_runtime:overlay stop returned an inconsistent")
        for code in plugin._config_runtime_error_codes
    )
    assert plugin._config_restart_required is True
    assert plugin._settings_transition is False


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


def test_settings_transition_resyncs_active_game_context(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted_messages: list[dict[str, Any]] = []
    current = CompanionConfig(
        log_path="old.log",
        llm_data_consent=True,
        target_lanlan="兰兰A",
    ).to_dict()
    snapshot = GameSnapshot(mode="battlegrounds", phase="recruit", game_number=5)

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            current.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: dict(current)}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.config = Config()

    def update_config(_config: CompanionConfig) -> None:
        plugin._observe_game_event(
            GameEvent("state_ready", 0, "ready", 100.0, {}), snapshot
        )

    plugin._monitor = types.SimpleNamespace(
        update_config=update_config,
        snapshot=lambda: snapshot,
        status=lambda: types.SimpleNamespace(
            source_state="watching",
            monitor_running=True,
            last_line_at=time.time(),
            last_event_at=time.time(),
            source_modified_at=time.time(),
        ),
    )
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor
    plugin.push_message = lambda **kwargs: submitted_messages.append(kwargs) or {"submitted": True}

    result = asyncio.run(plugin.save_settings(log_path="new.log"))

    assert result["summary"] == "炉石陪玩设置已保存。"
    assert plugin._settings_transition is False
    assert len(submitted_messages) == 2
    assert [message["ai_behavior"] for message in submitted_messages] == ["read", "read"]
    assert submitted_messages[1]["metadata"]["kind"] == "game_live_state"
    assert plugin._context_target == "兰兰A"


def test_save_settings_reads_back_full_config_after_sparse_profile_result(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    current = CompanionConfig(
        log_path="old.log",
        llm_data_consent=True,
        target_lanlan="lanlan-a",
        overlay_font_size=31,
    ).to_dict()
    patches: list[dict[str, Any]] = []
    dump_calls = 0
    monitor_updates: list[CompanionConfig] = []

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            patches.append(patch)
            current.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: dict(patch[entry._CONFIG_SECTION])}

        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            nonlocal dump_calls
            dump_calls += 1
            return {entry._CONFIG_SECTION: dict(current)}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig.from_mapping(current)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="lanlan-a")
    plugin.config = Config()
    plugin._monitor = types.SimpleNamespace(update_config=lambda cfg: monitor_updates.append(cfg))
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(log_path="  new.log  "))

    assert result["llm_enabled"] is False
    assert patches == [{entry._CONFIG_SECTION: {"log_path": "new.log"}}]
    assert dump_calls == 1
    assert plugin.cfg.log_path == "new.log"
    assert plugin.cfg.target_lanlan == "lanlan-a"
    assert plugin.cfg.overlay_font_size == 31
    assert monitor_updates[-1].log_path == "new.log"


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


def test_consent_revocation_fails_closed_before_waiting_for_settings_lock(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    update_entered: asyncio.Event | None = None

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            assert update_entered is not None
            update_entered.set()
            merged = CompanionConfig().to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin._monitor = types.SimpleNamespace(update_config=lambda _config: None)
    plugin._catalog = types.SimpleNamespace(configure=lambda **_kwargs: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._ensure_monitor = lambda: plugin._monitor
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    async def scenario() -> Any:
        nonlocal update_entered
        update_entered = asyncio.Event()
        await plugin._settings_lock.acquire()
        task = asyncio.create_task(plugin.save_settings(llm_data_consent=False))
        await asyncio.sleep(0)
        assert plugin.cfg.llm_data_consent is False
        assert plugin.cfg.llm_commentary_enabled is False
        assert plugin._settings_transition is True
        assert not update_entered.is_set()
        plugin._settings_lock.release()
        return await task

    result = asyncio.run(scenario())

    assert result["llm_enabled"] is False


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
        plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
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
    assert plugin._config_runtime_error_codes == ("context_restore:rejected",)
    assert plugin._config_restart_required is True


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


def test_save_settings_does_not_restart_running_overlay_for_unrelated_fields(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            merged = CompanionConfig(overlay_enabled=True, overlay_font_size=24).to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    class Overlay:
        def __init__(self) -> None:
            self.config = CompanionConfig(overlay_enabled=True, overlay_font_size=24)

        def status(self) -> dict[str, Any]:
            return {"running": True}

        def configure(self, config: CompanionConfig) -> None:
            self.config = config

        def stop(self) -> dict[str, Any]:
            raise AssertionError("unrelated settings must not restart the overlay")

        def start(self) -> dict[str, Any]:
            raise AssertionError("unrelated settings must not restart the overlay")

    overlay = Overlay()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_enabled=True, overlay_font_size=24)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._overlay = overlay
    plugin._ensure_monitor = lambda: types.SimpleNamespace(update_config=lambda _config: None)

    result = asyncio.run(plugin.save_settings(log_path="new.log"))

    assert result["summary"] == "炉石陪玩设置已保存。"
    assert plugin.cfg.log_path == "new.log"
    assert overlay.config.log_path == "new.log"


def test_save_settings_reports_overlay_stop_failure_after_persisting(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            merged = CompanionConfig(overlay_enabled=True, overlay_font_size=24).to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    class Overlay:
        def __init__(self) -> None:
            self.config = CompanionConfig(overlay_enabled=True, overlay_font_size=24)
            self.running = True

        def status(self) -> dict[str, Any]:
            return {"running": self.running}

        def configure(self, config: CompanionConfig) -> None:
            self.config = config

        def stop(self) -> dict[str, Any]:
            return {"ok": False, "running": True, "error_code": "overlay_stop_failed"}

        def start(self) -> dict[str, Any]:
            raise AssertionError("start must not run after a failed stop")

    overlay = Overlay()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_enabled=True, overlay_font_size=24)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._overlay = overlay
    plugin._ensure_monitor = lambda: types.SimpleNamespace(update_config=lambda _config: None)

    result = asyncio.run(plugin.save_settings(overlay_font_size=31))

    assert isinstance(result, FakeErr)
    assert "settings were saved" in str(result.value)
    assert "overlay_stop_failed" in str(result.value)
    assert plugin.cfg.overlay_font_size == 31
    assert overlay.config.overlay_font_size == 24
    assert overlay.running is True


def test_save_settings_restores_previous_running_overlay_when_new_start_fails(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            merged = CompanionConfig(overlay_enabled=True, overlay_font_size=24).to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    class Overlay:
        def __init__(self) -> None:
            self.config = CompanionConfig(overlay_enabled=True, overlay_font_size=24)
            self.running = True
            self.start_calls = 0

        def status(self) -> dict[str, Any]:
            return {"running": self.running}

        def configure(self, config: CompanionConfig) -> None:
            self.config = config

        def stop(self) -> dict[str, Any]:
            self.running = False
            return {"ok": True, "running": False}

        def start(self) -> dict[str, Any]:
            self.start_calls += 1
            if self.config.overlay_font_size == 31:
                return {"ok": False, "running": False, "error_code": "new_overlay_failed"}
            self.running = True
            return {"ok": True, "running": True}

    overlay = Overlay()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(overlay_enabled=True, overlay_font_size=24)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._overlay = overlay
    plugin._ensure_monitor = lambda: types.SimpleNamespace(update_config=lambda _config: None)

    result = asyncio.run(plugin.save_settings(overlay_font_size=31))

    assert isinstance(result, FakeErr)
    assert "new_overlay_failed" in str(result.value)
    assert "previous overlay was restored" in str(result.value)
    assert plugin.cfg.overlay_font_size == 31
    assert overlay.config.overlay_font_size == 24
    assert overlay.running is True
    assert overlay.start_calls == 2


def test_settings_transition_resets_when_runtime_apply_raises(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr

    async def scenario() -> tuple[Any, Any]:
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
        plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
        plugin._overlay = types.SimpleNamespace(
            status=lambda: {"running": False},
            configure=lambda _config: None,
        )
        plugin._ensure_monitor = lambda: types.SimpleNamespace(update_config=fail_update)
        result = await plugin.save_settings(log_path="new.log")
        return result, plugin

    result, plugin = asyncio.run(scenario())

    assert isinstance(result, FakeErr)
    assert "settings were saved" in str(result.value)
    assert "restart the plugin" in str(result.value)
    assert plugin.cfg.log_path == "new.log"
    assert plugin._settings_transition is False


def test_save_settings_best_effort_applies_other_components_after_catalog_failure(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr
    monitor_configs: list[CompanionConfig] = []
    overlay_configs: list[CompanionConfig] = []

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            merged = CompanionConfig().to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(card_catalog_network_enabled=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._monitor = types.SimpleNamespace(
        update_config=lambda config: monitor_configs.append(config)
    )
    plugin._catalog = types.SimpleNamespace(
        configure=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("catalog failed"))
    )
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda config: overlay_configs.append(config),
    )
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(card_catalog_network_enabled=False))

    assert isinstance(result, FakeErr)
    assert "catalog:RuntimeError" in str(result.value)
    assert plugin.cfg.card_catalog_network_enabled is False
    assert monitor_configs[-1].card_catalog_network_enabled is False
    assert overlay_configs[-1].card_catalog_network_enabled is False
    assert plugin._settings_transition is False


def test_settings_transition_resets_when_fail_closed_update_raises(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class FakeErr:
        def __init__(self, value: Any) -> None:
            self.value = value

    entry.Err = FakeErr
    persisted: list[dict[str, Any]] = []

    class Config:
        async def update(self, patch: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            persisted.append(patch)
            merged = CompanionConfig().to_dict()
            merged.update(patch[entry._CONFIG_SECTION])
            return {entry._CONFIG_SECTION: merged}

    def fail_update(_config: CompanionConfig) -> None:
        raise RuntimeError("fail-closed apply failed")

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(
        llm_commentary_enabled=True,
        llm_data_consent=True,
    )
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin.config = Config()
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._monitor = types.SimpleNamespace(update_config=fail_update)
    plugin._ensure_monitor = lambda: plugin._monitor

    result = asyncio.run(plugin.save_settings(llm_data_consent=False))

    assert isinstance(result, FakeErr)
    assert "settings were saved" in str(result.value)
    assert persisted[0][entry._CONFIG_SECTION]["llm_data_consent"] is False
    assert plugin._settings_transition is False
    assert plugin.cfg.llm_data_consent is False
    assert plugin.cfg.llm_commentary_enabled is False


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


def test_later_full_stats_snapshot_recovers_unconfirmed_compensation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    callbacks: list[Callable[[], None]] = []

    class Writer:
        def submit(
            self,
            _value: dict[str, Any],
            *,
            on_success: Callable[[], None] | None = None,
        ) -> bool:
            assert on_success is not None
            callbacks.append(on_success)
            return True

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_recovery_generation = 4
    plugin._stats_store_error_code = "stats:clear_compensation_unconfirmed"
    plugin._store_writer = Writer()
    plugin._season = {"key": "S14"}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

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
    callbacks[0]()

    assert plugin._stats_store_error_code == ""


def test_earlier_stats_success_cannot_clear_newer_compensation_failure(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    callbacks: list[Callable[[], None]] = []

    class Writer:
        def submit(
            self,
            _value: dict[str, Any],
            *,
            on_success: Callable[[], None] | None = None,
        ) -> bool:
            assert on_success is not None
            callbacks.append(on_success)
            return True

        def write_and_wait(self, _value: dict[str, Any], *, timeout: float) -> bool:
            assert timeout == entry._STATS_CLEAR_WRITE_TIMEOUT_SECONDS
            return False

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_recovery_generation = 0
    plugin._stats_store_error_code = ""
    plugin._store_writer = Writer()
    plugin._season = {"key": "S14"}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    plugin._record_battlegrounds_result(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "ended",
            100.0,
            {"placement": 3, "variant": "solo", "hero_card_id": "BG_HERO_1"},
        ),
        GameSnapshot(),
    )
    assert plugin._clear_battlegrounds_stats() is False
    callbacks[0]()

    assert plugin._stats_recovery_generation == 1
    assert plugin._stats_store_error_code == "stats:clear_compensation_unconfirmed"


def test_failed_full_stats_snapshot_does_not_recover_compensation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class StoreErr:
        def is_err(self) -> bool:
            return True

    async def write(_value: dict[str, Any]) -> StoreErr:
        return StoreErr()

    writer = entry.AsyncStoreWriter(
        write,
        types.SimpleNamespace(warning=lambda *_args: None),
    )
    writer.start()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_recovery_generation = 2
    plugin._stats_store_error_code = "stats:clear_compensation_unconfirmed"
    plugin._store_writer = writer
    plugin._season = {"key": "S14"}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

    plugin._record_battlegrounds_result(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "ended",
            100.0,
            {"placement": 4, "variant": "solo", "hero_card_id": "BG_HERO_1"},
        ),
        GameSnapshot(),
    )

    assert writer.flush() is False
    assert plugin._stats_store_error_code == "stats:clear_compensation_unconfirmed"
    assert writer.stop() is False


def test_stats_recovery_callback_does_not_block_serial_clear_write(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    monkeypatch.setattr(entry, "_STATS_CLEAR_WRITE_TIMEOUT_SECONDS", 0.2)
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    clear_wait_entered = threading.Event()
    write_count = 0

    async def write(_value: dict[str, Any]) -> object:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            first_write_entered.set()
            await asyncio.to_thread(release_first_write.wait, 1.0)
        return object()

    writer = entry.AsyncStoreWriter(
        write,
        types.SimpleNamespace(warning=lambda *_args: None),
    )
    writer.start()
    original_write_and_wait = writer.write_and_wait

    def marked_write_and_wait(value: dict[str, Any], *, timeout: float) -> bool:
        clear_wait_entered.set()
        return original_write_and_wait(value, timeout=timeout)

    writer.write_and_wait = marked_write_and_wait
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._stats = BattlegroundsStats()
    plugin._stats_loaded = True
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_recovery_lock = threading.RLock()
    plugin._stats_recovery_generation = 0
    plugin._stats_store_error_code = "stats:clear_compensation_unconfirmed"
    plugin._store_writer = writer
    plugin._season = {"key": "S14"}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)

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
    assert first_write_entered.wait(1.0)
    clear_results: list[bool] = []
    clear_thread = threading.Thread(
        target=lambda: clear_results.append(plugin._clear_battlegrounds_stats())
    )
    clear_thread.start()
    assert clear_wait_entered.wait(1.0)
    release_first_write.set()
    clear_thread.join(1.0)

    assert clear_thread.is_alive() is False
    assert clear_results == [True]
    assert write_count == 2
    assert plugin._stats_store_error_code == ""
    assert writer.stop(timeout=1.0) is True


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
        def submit(
            self,
            value: dict[str, Any],
            *,
            on_success: Callable[[], None] | None = None,
        ) -> bool:
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
        def submit(
            self,
            _value: dict[str, Any],
            *,
            on_success: Callable[[], None] | None = None,
        ) -> bool:
            return False

        def last_error_code(self) -> str:
            return "stats:writer_unavailable"

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

    assert plugin._stats_store_error_code == ""
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


def test_dashboard_state_exposes_battlegrounds_live_detail_fields_to_hosted_ui(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        battlegrounds=_recruit_snapshot(
            round=6,
            observed_at=now,
            refresh_cost=0,
            upgrade_cost=6,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_1",
                    name="Shop Spell",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=2,
                    premium=False,
                    position=1,
                    keywords={
                        "taunt": True,
                        "divine_shield": False,
                        "reborn": None,
                    },
                ),
            ),
            hand=(
                BattlegroundsCardSnapshot(
                    card_id="BG_HAND_1",
                    name="Hand Spell",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=1,
                    premium=False,
                    position=1,
                    keywords={"taunt": False},
                ),
            ),
            warband=(
                BattlegroundsCardSnapshot(
                    card_id="BG_WARBAND_1",
                    name="Golden Defender",
                    card_type="MINION",
                    attack=8,
                    health=8,
                    premium=True,
                    position=1,
                    keywords={"divine_shield": True, "reborn": None},
                ),
            ),
            current_choice=BattlegroundsChoiceSnapshot(
                choice_type="discover",
                count_min=1,
                count_max=1,
                source=BattlegroundsCardSnapshot(
                    card_id="BG_CHOICE_SOURCE",
                    name="Choice Source",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=1,
                ),
                options=(
                    BattlegroundsCardSnapshot(
                        card_id="BG_CHOICE_1",
                        name="Choice Minion",
                        card_type="MINION",
                        current_cost=3,
                        position=1,
                    ),
                ),
            ),
        ),
    )
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig()
    plugin._stats = BattlegroundsStats()
    plugin._stats_store_error_code = ""
    plugin._store_writer = types.SimpleNamespace(last_error_code=lambda: "")
    plugin._season = {"key": "S14"}
    plugin._ensure_monitor = lambda: types.SimpleNamespace(
        status=lambda: types.SimpleNamespace(to_dict=lambda: {"source_state": "watching"}),
        snapshot=lambda: snapshot,
    )
    plugin._overlay = types.SimpleNamespace(status=lambda: {})
    plugin._catalog_status = lambda: {}

    state = plugin._dashboard_state()
    battlegrounds_state = state["game"]["battlegrounds"]

    assert battlegrounds_state["shop"][0] == {
        "card_id": "BG_SPELL_1",
        "name": "Shop Spell",
        "card_type": "BATTLEGROUND_SPELL",
        "attack": 0,
        "health": None,
        "tier": 0,
        "frozen": False,
        "position": 1,
        "premium": False,
        "current_cost": 2,
        "keywords": {
            "taunt": True,
            "divine_shield": False,
            "reborn": None,
        },
    }
    assert battlegrounds_state["hand"][0]["card_type"] == "BATTLEGROUND_SPELL"
    assert battlegrounds_state["hand"][0]["current_cost"] == 1
    assert battlegrounds_state["warband"][0]["premium"] is True
    assert battlegrounds_state["warband"][0]["keywords"] == {
        "divine_shield": True,
        "reborn": None,
    }
    assert battlegrounds_state["refresh_cost"] == 0
    assert battlegrounds_state["upgrade_cost"] == 6
    assert battlegrounds_state["economy"]["refresh_cost"] == 0
    assert battlegrounds_state["economy"]["upgrade_cost"] == 6
    assert battlegrounds_state["areas"]["economy"]["phase"] == "recruit"
    assert battlegrounds_state["areas"]["choice"]["complete"] is True
    assert battlegrounds_state["current_choice"]["choice_type"] == "discover"
    assert battlegrounds_state["current_choice"]["options"][0]["card_id"] == "BG_CHOICE_1"


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


def test_shutdown_suspends_late_overlay_starts_before_stopping_runtime(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    calls: list[str] = []

    class Overlay:
        suspended = False

        def suspend_starts(self) -> None:
            self.suspended = True
            calls.append("suspend")

        def stop(self, **_kwargs: Any) -> dict[str, Any]:
            assert self.suspended is True
            calls.append("stop")
            return {"ok": True, "running": False}

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._settings_lock = asyncio.Lock()
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._settings_transition = False
    plugin._context_target = None
    plugin._monitor = None
    plugin._catalog = None
    plugin._overlay = Overlay()
    plugin._store_writer = types.SimpleNamespace(stop=lambda **_kwargs: True)

    result = asyncio.run(plugin.shutdown())

    assert result["status"] == "stopped"
    assert calls == ["suspend", "stop"]


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


def test_shutdown_cancels_config_reconcile_without_waiting_for_settings_lock(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    reconcile_entered: asyncio.Event | None = None

    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._monitor = None
    plugin._catalog = types.SimpleNamespace(
        configure=lambda **_kwargs: None,
        stop=lambda **_kwargs: True,
    )
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
        stop=lambda **_kwargs: {"ok": True, "running": False},
    )
    plugin._store_writer = types.SimpleNamespace(stop=lambda **_kwargs: True)
    plugin.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    async def block_reconcile(*_args: Any, **_kwargs: Any) -> tuple[list[str], bool]:
        assert reconcile_entered is not None
        reconcile_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled reconcile must not resume")

    plugin._apply_runtime_config_best_effort = block_reconcile

    async def scenario() -> Any:
        nonlocal reconcile_entered
        reconcile_entered = asyncio.Event()
        await plugin.config_change(
            new_config={
                entry._CONFIG_SECTION: CompanionConfig(
                    llm_data_consent=False
                ).to_dict()
            }
        )
        await reconcile_entered.wait()
        return await asyncio.wait_for(plugin.shutdown(), timeout=1.0)

    result = asyncio.run(scenario())

    assert result["status"] == "stopped"
    assert plugin._config_reconcile_accepting is False
    assert plugin._config_reconcile_task is None
    assert plugin.cfg.llm_data_consent is False


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


def test_start_monitoring_opens_dispatch_gate_before_state_ready(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    snapshot = GameSnapshot(mode="battlegrounds", phase="recruit", game_number=8)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True, target_lanlan="兰兰A")
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._monitor_dispatch_enabled = False
    plugin._settings_transition = False
    plugin._context_target = None
    plugin._started = True
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}

    class Monitor:
        def start(self) -> bool:
            plugin._observe_game_event(
                GameEvent("state_ready", 0, "ready", 100.0, {}), snapshot
            )
            return True

        def snapshot(self) -> GameSnapshot:
            return snapshot

        def status(self) -> types.SimpleNamespace:
            now = time.time()
            return types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
                source_modified_at=now,
            )

    monitor = Monitor()
    plugin._monitor = monitor
    plugin._ensure_monitor = lambda: monitor

    result = asyncio.run(plugin.start_monitoring())

    assert result["started"] is True
    assert plugin._monitor_dispatch_enabled is True
    assert len(submitted) == 2
    assert [message["ai_behavior"] for message in submitted] == ["read", "read"]
    assert submitted[1]["metadata"]["kind"] == "game_live_state"
    assert plugin._context_target == "兰兰A"


def test_start_monitoring_rolls_back_dispatch_gate_when_start_raises(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._monitor_dispatch_enabled = False
    plugin._started = True
    monitor = types.SimpleNamespace(
        start=lambda: (_ for _ in ()).throw(RuntimeError("start failed")),
    )
    plugin._ensure_monitor = lambda: monitor

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(plugin.start_monitoring())

    assert plugin._monitor_dispatch_enabled is False


def test_start_monitoring_rejects_nonaccepting_monitor_left_by_stop_timeout(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._monitor_dispatch_enabled = False
    plugin._started = True
    plugin._sync_active_game_context = lambda: (_ for _ in ()).throw(
        AssertionError("unhealthy monitor must not publish active context")
    )
    monitor = types.SimpleNamespace(
        start=lambda: False,
        is_accepting=lambda: False,
    )
    plugin._ensure_monitor = lambda: monitor

    with pytest.raises(RuntimeError, match="monitor did not start"):
        asyncio.run(plugin.start_monitoring())

    assert plugin._monitor_dispatch_enabled is False


def test_start_monitoring_accepts_healthy_already_running_monitor(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    synced: list[bool] = []
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin._ownership_lock = threading.RLock()
    plugin._monitor_action_lock = asyncio.Lock()
    plugin._monitor_dispatch_enabled = False
    plugin._started = True
    plugin._sync_active_game_context = lambda: synced.append(True)
    monitor = types.SimpleNamespace(
        start=lambda: False,
        is_accepting=lambda: True,
    )
    plugin._ensure_monitor = lambda: monitor

    result = asyncio.run(plugin.start_monitoring())

    assert result["started"] is False
    assert "已在运行" in result["summary"]
    assert plugin._monitor_dispatch_enabled is True
    assert synced == [True]


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
    assert result["state"] == {}
    assert result["freshness"]["source"] == "cached"
    assert result["freshness"]["do_not_treat_cached_as_live"] is True
    assert result["answer_contract"]["never_recommend_from_cached_state"] is True


def test_current_state_uses_atomic_capture_across_log_source_generations(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()

    class Monitor:
        def capture(self) -> tuple[GameSnapshot, Any, int]:
            return (
                GameSnapshot(),
                types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
                2,
            )

        def snapshot(self) -> GameSnapshot:
            raise AssertionError("snapshot and status must not be read separately")

        def status(self) -> Any:
            raise AssertionError("snapshot and status must not be read separately")

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=Monitor,
    )

    result = asyncio.run(entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin))

    assert result["available"] is False
    assert result["state"] == {}
    assert result["freshness"]["game_number"] == 0


@pytest.mark.parametrize("tool_name", ["current_state", "battlegrounds_advice"])
def test_live_tools_fail_fast_while_monitor_refresh_holds_state_lock(
    monkeypatch, tool_name: str
) -> None:
    entry = _load_sdk_entry(monkeypatch)

    class Monitor:
        def try_capture(self, *, timeout_seconds: float) -> None:
            assert timeout_seconds <= 0.05
            return None

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            raise AssertionError("latency-sensitive tools must use try_capture")

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _ensure_monitor=Monitor,
    )

    if tool_name == "current_state":
        result = asyncio.run(
            entry.HearthstoneCompanionPlugin.hearthstone_current_state(plugin)
        )
    else:
        result = asyncio.run(
            entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
                plugin, topic="current_strategy"
            )
        )

    assert result["available"] is False
    assert result["reason"] == "state_refresh_in_progress"


def test_monitor_creation_retries_latest_config_and_publishes_one_identity(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    first_constructor_entered = threading.Event()
    release_first_constructor = threading.Event()
    constructed_paths: list[str] = []
    constructor_callbacks: list[dict[str, Any]] = []

    class Monitor:
        def __init__(self, config: CompanionConfig, *_args: Any, **kwargs: Any) -> None:
            self.config = config
            constructed_paths.append(config.log_path)
            constructor_callbacks.append(kwargs)
            if len(constructed_paths) == 1:
                first_constructor_entered.set()
                assert release_first_constructor.wait(1.0)

        def update_config(self, config: CompanionConfig) -> None:
            self.config = config

    monkeypatch.setattr(entry, "CompanionMonitor", Monitor)
    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    updated = CompanionConfig(log_path="new/Power.log", llm_data_consent=True)
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = previous
    plugin._monitor = None
    plugin._monitor_applied_instance = None
    plugin._monitor_applied_config = None
    plugin._monitor_creation_lock = threading.Lock()
    plugin._ownership_lock = threading.RLock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._config_revision = 0
    plugin._config_reconciled_revision = 0
    plugin._config_reconcile_accepting = False
    plugin._config_reconcile_task = None
    plugin._context_target = None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    results: list[Any] = []
    first = threading.Thread(target=lambda: results.append(plugin._ensure_monitor()))
    second = threading.Thread(target=lambda: results.append(plugin._ensure_monitor()))
    first.start()
    second.start()
    assert first_constructor_entered.wait(1.0)

    change = asyncio.run(
        plugin.config_change(
            new_config={entry._CONFIG_SECTION: updated.to_dict()}
        )
    )
    assert change["reconcile_scheduled"] is False
    release_first_constructor.set()
    first.join(1.0)
    second.join(1.0)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert constructed_paths == ["old/Power.log", "new/Power.log"]
    assert all(
        callbacks["on_state"].__self__ is plugin
        and callbacks["on_state"].__func__ is plugin._publish_live_state.__func__
        for callbacks in constructor_callbacks
    )
    assert len(results) == 2
    assert results[0] is results[1] is plugin._monitor
    assert plugin._monitor.config.log_path == "new/Power.log"
    assert plugin._monitor_applied_instance is plugin._monitor
    assert plugin._monitor_applied_config.to_dict() == updated.to_dict()


def test_active_outputs_and_stats_require_monitor_applied_config(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    submitted: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []

    class Monitor:
        capture_calls = 0

        def update_config(self, _config: CompanionConfig) -> None:
            return None

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            self.capture_calls += 1
            now = time.time()
            return (
                GameSnapshot(mode="battlegrounds", phase="recruit", game_number=8),
                types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
                1,
            )

    class Stats:
        def record_game(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

        def to_store_dict(self) -> dict[str, Any]:
            return {"schema": 1}

    class Writer:
        def submit(self, _value: dict[str, Any], **_kwargs: Any) -> bool:
            return True

    old = CompanionConfig(
        log_path="old/Power.log",
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    new = CompanionConfig(
        log_path="new/Power.log",
        llm_commentary_enabled=True,
        llm_data_consent=True,
        target_lanlan="兰兰A",
    )
    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    plugin.cfg = new
    plugin._monitor = monitor
    plugin._monitor_applied_instance = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(old.to_dict())
    plugin._ownership_lock = threading.RLock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 1
    plugin._started = True
    plugin._monitor_dispatch_enabled = True
    plugin._last_user_chat_at = 0.0
    plugin._context_target = None
    plugin._stats = Stats()
    plugin._stats_loaded = True
    plugin._stats_store_error_code = ""
    plugin._stats_submission_lock = threading.RLock()
    plugin._stats_recovery_lock = threading.RLock()
    plugin._stats_recovery_generation = 0
    plugin._store_writer = Writer()
    plugin._season = {"key": "S14"}
    plugin.ctx = types.SimpleNamespace(_current_lanlan="兰兰A")
    plugin.push_message = lambda **kwargs: submitted.append(kwargs) or {"submitted": True}
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    event = GameEvent(
        "battlegrounds_game_ended",
        10,
        "ended",
        100.0,
        {"placement": 1, "variant": "solo", "hero_card_id": "BG_HERO_1"},
    )
    snapshot = GameSnapshot(mode="battlegrounds", phase="ended", game_number=7)

    assert plugin._dispatch_llm("old source prompt", event, snapshot) is False
    plugin._observe_game_event(event, snapshot)
    plugin._sync_active_game_context()
    plugin._record_battlegrounds_result(event, snapshot)
    assert submitted == []
    assert recorded == []
    assert monitor.capture_calls == 0

    plugin._update_monitor_config(monitor, new)
    plugin._sync_active_game_context()
    commentary_event = GameEvent("battlegrounds_triple", 9, "triple", 102.0, {})
    commentary_snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=8,
    )
    assert plugin._dispatch_llm(
        "new source prompt",
        commentary_event,
        commentary_snapshot,
    ) is True
    plugin._record_battlegrounds_result(event, snapshot)

    assert [item["ai_behavior"] for item in submitted] == ["read", "read", "respond"]
    assert submitted[1]["metadata"]["kind"] == "game_live_state"
    assert len(recorded) == 1
    assert monitor.capture_calls == 1


@pytest.mark.parametrize("tool_name", ["current_state", "battlegrounds_advice"])
@pytest.mark.parametrize("monitor_update_fails", [False, True])
def test_live_tools_fail_closed_while_log_path_change_is_reconciling(
    monkeypatch,
    tool_name: str,
    monitor_update_fails: bool,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=_recruit_snapshot(
            round=3,
            observed_at=now,
            shop=(BattlegroundsCardSnapshot(card_id="BG_SHOP_1", current_cost=3),),
        ),
    )

    class Monitor:
        capture_calls = 0
        applied_log_paths: list[str] = []

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            self.capture_calls += 1
            return (
                snapshot,
                types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
                len(self.applied_log_paths),
            )

        def update_config(self, config: CompanionConfig) -> None:
            self.applied_log_paths.append(config.log_path)
            if monitor_update_fails:
                raise RuntimeError("log source switch failed")

    class Catalog:
        def configure(self, **_kwargs: Any) -> None:
            return None

        def status(self) -> dict[str, Any]:
            return {"available": True}

        def facts_for(self, _value: Any) -> dict[str, Any]:
            return {"available": True, "observed_card_facts": {}}

    monitor = Monitor()
    plugin = object.__new__(entry.HearthstoneCompanionPlugin)
    previous = CompanionConfig(log_path="old/Power.log", llm_data_consent=True)
    plugin.cfg = previous
    plugin._context_target = None
    plugin._ownership_lock = threading.RLock()
    plugin._settings_lock = asyncio.Lock()
    plugin._settings_transition = False
    plugin._settings_transition_revision = 0
    plugin._monitor = monitor
    plugin._monitor_applied_config = CompanionConfig.from_mapping(previous.to_dict())
    plugin._monitor_applied_instance = monitor
    plugin._catalog = Catalog()
    plugin._overlay = types.SimpleNamespace(
        status=lambda: {"running": False},
        configure=lambda _config: None,
    )
    plugin._season = {"key": "S14", "status": "bundled_static"}
    plugin._stats = BattlegroundsStats()
    plugin._sync_active_game_context = lambda: None
    plugin.logger = types.SimpleNamespace(warning=lambda *_args: None)
    updated = CompanionConfig(log_path="new/Power.log", llm_data_consent=True)

    async def call_tool() -> dict[str, Any]:
        if tool_name == "current_state":
            return await plugin.hearthstone_current_state()
        return await plugin.hearthstone_battlegrounds_advice(topic="current_strategy")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await plugin._settings_lock.acquire()
        try:
            await plugin.config_change(
                new_config={entry._CONFIG_SECTION: updated.to_dict()}
            )
            assert plugin._settings_transition is True
            blocked = await call_tool()
            assert monitor.capture_calls == 0
        finally:
            plugin._settings_lock.release()
        await plugin._wait_for_config_reconcile()
        after_reconcile = await call_tool()
        return blocked, after_reconcile

    blocked, after_reconcile = asyncio.run(scenario())

    assert blocked["available"] is False
    assert blocked["reason"] == "configuration_reconciling"
    assert monitor.applied_log_paths == ["new/Power.log"]
    if monitor_update_fails:
        assert plugin._config_restart_required is True
        assert plugin._config_runtime_error_codes == ("monitor:RuntimeError",)
        assert monitor.capture_calls == 0
        assert after_reconcile["available"] is False
        assert after_reconcile["reason"] == "monitor_configuration_not_applied"
        assert plugin._monitor_applied_config == previous
    else:
        assert plugin._config_restart_required is False
        assert monitor.capture_calls == 1
        if tool_name == "current_state":
            assert after_reconcile["available"] is False
            assert after_reconcile["reason"] == "battlegrounds_requires_specialized_tool"
        else:
            assert after_reconcile["available"] is True
        assert plugin._monitor_applied_config.to_dict() == plugin.cfg.to_dict()


def test_battlegrounds_advice_separates_local_evidence_from_global_meta(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="season-14-36.2", mode="solo", placement=3, hero_id="BG_HERO_1")
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="playing",
        battlegrounds=_recruit_snapshot(
            variant="solo",
            round=7,
            observed_at=time.time(),
            shop=(BattlegroundsCardSnapshot(card_id="BG_SHOP_1", current_cost=3),),
        ),
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
    assert result["game_mode"] == "battlegrounds"
    assert result["scope"] == "hearthstone_battlegrounds_only"
    assert result["answer_contract"]["answer_as_battlegrounds_not_constructed"] is True
    assert result["answer_contract"]["do_not_answer_with_constructed_deck_archetypes"] is True


def test_battlegrounds_advice_never_falls_back_to_constructed_state(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    snapshot = GameSnapshot(mode="constructed", phase="playing", game_number=4)
    now = time.time()
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["available"] is False
    assert result["reason"] == "no_live_battlegrounds_state"
    assert result["game_mode"] == "battlegrounds"
    assert result["current_public_state"] is None
    assert result["answer_contract"]["if_unavailable_do_not_fallback_to_constructed"] is True


def test_battlegrounds_advice_includes_attributed_observed_card_facts(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="playing",
        battlegrounds=_recruit_snapshot(
            variant="solo",
            round=4,
            observed_at=time.time(),
            shop=(BattlegroundsCardSnapshot(card_id="BG_TEST", current_cost=3),),
        ),
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


def test_battlegrounds_advice_fails_closed_when_consent_is_revoked_during_catalog_wait(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    cfg = CompanionConfig(llm_data_consent=True)
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            phase="recruit",
            shop=(BattlegroundsCardSnapshot(card_id="BG_PRIVATE_AFTER_REVOKE"),),
        ),
    )

    class Catalog:
        def status(self) -> dict[str, Any]:
            return {"available": False}

        def wait_ready(self, _timeout: float) -> bool:
            cfg.llm_data_consent = False
            return True

        def facts_for(self, _value: Any) -> dict[str, Any]:
            raise AssertionError("catalog facts must not be exposed after consent revocation")

    now = time.time()
    plugin = types.SimpleNamespace(
        cfg=cfg,
        _season={"key": "S14", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=Catalog(),
        _catalog_status=lambda: {},
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["available"] is False
    assert result["reason"] == "llm_data_sharing_not_authorized"
    assert set(result) == {
        "available",
        "reason",
        "game_mode",
        "scope",
        "answer_contract",
        "privacy_scope",
    }


def test_battlegrounds_advice_recaptures_when_catalog_wait_enters_combat(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    recruit = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=_recruit_snapshot(
            round=6,
            observed_at=now,
            shop=(BattlegroundsCardSnapshot(card_id="OLD_SHOP", current_cost=3),),
        ),
    )
    combat = GameSnapshot(
        mode="battlegrounds",
        phase="combat",
        game_number=3,
        battlegrounds=_combat_snapshot(
            round=6,
            observed_at=now,
            shop=(BattlegroundsCardSnapshot(card_id="OLD_SHOP", current_cost=3),),
            warband=(BattlegroundsCardSnapshot(card_id="PUBLIC_BOARD"),),
        ),
    )

    class Monitor:
        current = recruit
        capture_calls = 0

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            self.capture_calls += 1
            return (
                self.current,
                types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
                1,
            )

    monitor = Monitor()

    class Catalog:
        calls: list[object] = []

        def status(self) -> dict[str, Any]:
            return {"available": False, "degraded_reason": "loading"}

        def wait_ready(self, _timeout: float) -> bool:
            monitor.current = combat
            return True

        def facts_for(self, value: Any) -> dict[str, Any]:
            self.calls.append(value)
            assert value.phase == "combat"
            assert value.shop == ()
            assert value.hand == ()
            assert value.current_choice is None
            return {
                "available": True,
                "observed_card_facts": {
                    "PUBLIC_BOARD": {"name": "Public Board", "card_type": "MINION"}
                },
            }

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "S14", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=Catalog(),
        _catalog_status=lambda: {},
        _ensure_monitor=lambda: monitor,
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert monitor.capture_calls == 2
    assert result["available"] is True
    assert result["current_public_state"]["phase"] == "combat"
    assert result["current_public_state"]["shop"] == []
    purchase = result["capabilities"]["specific_purchase_advice"]
    assert purchase["available"] is False
    assert purchase["reason"] in {"no_fresh_recruit_shop", "insufficient_recruit_evidence"}
    assert purchase["evidence"] == ""
    assert purchase["missing_evidence"]
    assert result["capabilities"]["shop_card_priority_advice"]["available"] is False
    assert result["capabilities"]["purchase_affordability"]["available"] is False
    assert result["capabilities"]["combat_commentary"]["available"] is True
    assert len(plugin._catalog.calls) == 1


def test_battlegrounds_advice_recaptures_freshness_after_catalog_wait(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            phase="recruit",
            gold=10,
            shop=(BattlegroundsCardSnapshot(card_id="OLD_SHOP"),),
        ),
    )

    class Monitor:
        activity_at = now
        capture_calls = 0

        def capture(self) -> tuple[GameSnapshot, Any, int]:
            self.capture_calls += 1
            return (
                snapshot,
                types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=self.activity_at,
                    last_event_at=self.activity_at,
                ),
                1,
            )

    monitor = Monitor()

    class Catalog:
        def status(self) -> dict[str, Any]:
            return {"available": False, "degraded_reason": "loading"}

        def wait_ready(self, _timeout: float) -> bool:
            monitor.activity_at = now - 3600.0
            return True

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "S14", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=Catalog(),
        _catalog_status=lambda: {},
        _ensure_monitor=lambda: monitor,
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert monitor.capture_calls == 2
    assert result["available"] is False
    assert result["reason"] == "no_live_battlegrounds_state"
    assert result["freshness"]["source"] == "cached"
    assert result["current_public_state"] is None
    assert result["capabilities"]["shop_card_priority_advice"]["available"] is False
    assert result["capabilities"]["purchase_affordability"]["available"] is False
    assert result["capabilities"]["specific_purchase_advice"]["available"] is False
    assert all(
        capability["unresolved_catalog_ids"] == []
        for capability in result["capabilities"].values()
    )
    capabilities_json = json.dumps(result["capabilities"])
    assert "OLD_SHOP" not in capabilities_json
    assert "fresh_recruit_phase" not in capabilities_json
    assert "fresh_hero_select" not in capabilities_json
    assert "current_gold_observed" not in capabilities_json


@pytest.mark.parametrize("topic", ["season_meta", "hero_performance", "post_game"])
def test_non_live_fact_advice_topics_never_wait_for_catalog(monkeypatch, topic: str) -> None:
    entry = _load_sdk_entry(monkeypatch)
    wait_calls = 0

    class Catalog:
        def status(self) -> dict[str, Any]:
            return {"available": False, "degraded_reason": "loading"}

        def wait_ready(self, _timeout: float) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            return False

    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "S14", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=Catalog(),
        _catalog_status=lambda: {},
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: GameSnapshot(),
            status=lambda: types.SimpleNamespace(
                source_state="waiting",
                monitor_running=True,
                last_line_at=0.0,
                last_event_at=0.0,
            ),
        ),
    )

    asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic=topic
        )
    )

    assert wait_calls == 0


def test_battlegrounds_hero_comparison_requires_live_observed_choices(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()

    def advice_for(choices: tuple[BattlegroundsHeroChoiceSnapshot, ...]) -> dict[str, Any]:
        snapshot = GameSnapshot(
            mode="battlegrounds",
            phase="hero_select",
            game_number=1,
            battlegrounds=BattlegroundsSnapshot(
                phase="hero_select",
                hero_choices=choices,
            ),
        )
        catalog = types.SimpleNamespace(
            status=lambda: {"available": True},
            facts_for=lambda _value: {
                "available": True,
                "observed_card_facts": {
                    choice.card_id: {"name": choice.name or choice.card_id}
                    for choice in choices
                    if choice.card_id
                },
            },
        )
        plugin = types.SimpleNamespace(
            cfg=CompanionConfig(llm_data_consent=True),
            _season={"key": "season-14-36.2", "status": "bundled_static"},
            _stats=BattlegroundsStats(),
            _catalog=catalog,
            _ensure_monitor=lambda: types.SimpleNamespace(
                snapshot=lambda: snapshot,
                status=lambda: types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
            ),
        )
        return asyncio.run(
            entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
                plugin, topic="current_strategy"
            )
        )

    unavailable = advice_for(())
    available = advice_for(
        (
            BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_1", name="Hero One"),
            BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_2", name="Hero Two"),
        )
    )

    assert unavailable["available"] is False
    assert unavailable["capabilities"]["hero_choice_comparison"]["available"] is False
    assert unavailable["current_public_state"]["hero_choices"] == []
    assert available["available"] is True
    assert available["capabilities"]["hero_choice_comparison"]["available"] is True
    assert [choice["card_id"] for choice in available["current_public_state"]["hero_choices"]] == [
        "BG_HERO_1",
        "BG_HERO_2",
    ]


def test_battlegrounds_hero_select_includes_candidate_catalog_facts(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="hero_select",
        game_number=1,
        battlegrounds=BattlegroundsSnapshot(
            phase="hero_select",
            hero_choices=(
                BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_1", name="Hero One"),
                BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_2", name="Hero Two"),
            ),
        ),
    )
    facts = {
        "available": True,
        "coverage": {"zone_ids": {"hero_choices": ["BG_HERO_1", "BG_HERO_2"]}},
        "observed_card_facts": {
            "BG_HERO_1": {"name": "Hero One", "rules_text": "Hero power one"},
            "BG_HERO_2": {"name": "Hero Two", "rules_text": "Hero power two"},
        },
    }
    catalog_calls: list[object] = []
    catalog = types.SimpleNamespace(
        status=lambda: {"available": True},
        facts_for=lambda value: catalog_calls.append(value) or facts,
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=catalog,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["capabilities"]["hero_choice_comparison"]["available"] is True
    assert result["card_catalog"] == facts
    assert catalog_calls == [snapshot.battlegrounds]


def test_battlegrounds_purchase_advice_requires_fresh_recruit_shop(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=2,
        battlegrounds=_recruit_snapshot(
            round=2,
            observed_at=now,
            refresh_cost=0,
            upgrade_cost=6,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_1",
                    name="Shop Spell",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=2,
                    premium=False,
                    position=1,
                    keywords={
                        "taunt": True,
                        "divine_shield": False,
                        "reborn": None,
                    },
                ),
            ),
            hand=(
                BattlegroundsCardSnapshot(
                    card_id="BG_HAND_1",
                    name="Hand Spell",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=1,
                    premium=False,
                    position=1,
                    keywords={"taunt": False},
                ),
            ),
            warband=(
                BattlegroundsCardSnapshot(
                    card_id="BG_WARBAND_1",
                    name="Golden Defender",
                    card_type="MINION",
                    attack=8,
                    health=8,
                    premium=True,
                    position=1,
                    keywords={"divine_shield": True, "reborn": None},
                ),
            ),
            current_choice=BattlegroundsChoiceSnapshot(
                choice_type="discover",
                count_min=1,
                count_max=1,
                source=BattlegroundsCardSnapshot(
                    card_id="BG_CHOICE_SOURCE",
                    name="Choice Source",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=1,
                ),
                options=(
                    BattlegroundsCardSnapshot(
                        card_id="BG_CHOICE_1",
                        name="Choice Minion",
                        card_type="MINION",
                        current_cost=3,
                        position=1,
                    ),
                ),
            ),
        ),
    )
    catalog = types.SimpleNamespace(
        status=lambda: {"available": True},
        facts_for=lambda value: {
            "available": True,
            "observed_card_facts": {
                "BG_SPELL_1": {"name": "Shop Spell", "card_type": "BATTLEGROUND_SPELL"},
                "BG_HAND_1": {"name": "Hand Spell", "card_type": "BATTLEGROUND_SPELL"},
                "BG_WARBAND_1": {"name": "Golden Defender", "card_type": "MINION"},
                "BG_CHOICE_1": {"name": "Choice Minion", "card_type": "MINION"},
            },
        }
        if value is snapshot.battlegrounds
        else {"available": False},
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=catalog,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    priority = result["capabilities"]["shop_card_priority_advice"]
    affordability = result["capabilities"]["purchase_affordability"]
    capability = result["capabilities"]["specific_purchase_advice"]
    assert priority["available"] is True
    assert priority["evidence"] == "fresh_complete_recruit_context_with_catalog_facts"
    assert affordability["available"] is True
    assert affordability["evidence"] == "fresh_observed_gold_and_shop_actual_costs"
    assert capability["available"] is True
    assert capability["evidence"] == "fresh_complete_recruit_context_with_actual_costs"
    assert result["decision_guardrails"] == {
        "mandatory_instruction": (
            "Use each capability independently; only give affordability or an exact purchase sequence "
            "when its corresponding capability is available."
        ),
        "qualitative_shop_priority_allowed": True,
        "purchase_affordability_allowed": True,
        "exact_purchase_sequence_allowed": True,
        "unknown_actual_cost_card_ids": [],
    }
    public_state = result["current_public_state"]
    assert public_state["shop"][0] == {
        "card_id": "BG_SPELL_1",
        "name": "Shop Spell",
        "card_type": "BATTLEGROUND_SPELL",
        "attack": 0,
        "health": None,
        "tier": 0,
        "frozen": False,
        "position": 1,
        "premium": False,
        "current_cost": 2,
        "keywords": {
            "taunt": True,
            "divine_shield": False,
            "reborn": None,
        },
    }
    assert public_state["hand"][0]["card_type"] == "BATTLEGROUND_SPELL"
    assert public_state["hand"][0]["current_cost"] == 1
    assert public_state["warband"][0]["premium"] is True
    assert public_state["warband"][0]["keywords"] == {
        "divine_shield": True,
        "reborn": None,
    }
    assert public_state["current_choice"]["choice_type"] == "discover"
    assert public_state["current_choice"]["options"][0]["card_id"] == "BG_CHOICE_1"
    assert public_state["refresh_cost"] == 0
    assert public_state["upgrade_cost"] == 6
    assert public_state["economy"]["refresh_cost"] == 0
    assert public_state["economy"]["upgrade_cost"] == 6
    assert public_state["areas"]["economy"]["phase"] == "recruit"


def test_battlegrounds_advice_rejects_old_round_economy_observations(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=2,
        battlegrounds=_recruit_snapshot(
            round=2,
            observed_at=now,
            gold=7,
            refresh_cost=0,
            upgrade_cost=6,
            refresh_observed_round=1,
            upgrade_observed_round=1,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_MINION_1",
                    name="Shop Minion",
                    card_type="MINION",
                    current_cost=3,
                    position=1,
                ),
            ),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=types.SimpleNamespace(
            status=lambda: {"available": True},
            facts_for=lambda _value: {
                "available": True,
                "observed_card_facts": {
                    "BG_MINION_1": {"name": "Shop Minion", "card_type": "MINION"}
                },
            },
        ),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["capabilities"]["upgrade_affordability"]["available"] is False
    assert "upgrade_area_round_mismatch" in result["capabilities"][
        "upgrade_affordability"
    ]["missing_evidence"]
    assert result["capabilities"]["refresh_advice"]["available"] is False
    assert "refresh_area_round_mismatch" in result["capabilities"]["refresh_advice"][
        "missing_evidence"
    ]
    public_state = result["current_public_state"]
    assert public_state["gold"] == 7
    assert public_state["refresh_cost"] is None
    assert public_state["upgrade_cost"] is None
    assert public_state["economy"]["refresh_cost"] is None
    assert public_state["economy"]["upgrade_cost"] is None


def test_battlegrounds_advice_rejects_old_round_gold_observation(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=2,
        battlegrounds=_recruit_snapshot(
            round=2,
            observed_at=now,
            gold=7,
            max_gold=8,
            refresh_cost=0,
            upgrade_cost=6,
            gold_observed_round=1,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_MINION_1",
                    name="Shop Minion",
                    card_type="MINION",
                    current_cost=3,
                    position=1,
                ),
            ),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=types.SimpleNamespace(
            status=lambda: {"available": True},
            facts_for=lambda _value: {
                "available": True,
                "observed_card_facts": {
                    "BG_MINION_1": {"name": "Shop Minion", "card_type": "MINION"}
                },
            },
        ),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    for capability_name in (
        "purchase_affordability",
        "upgrade_affordability",
        "refresh_advice",
    ):
        capability = result["capabilities"][capability_name]
        assert capability["available"] is False
        assert "gold_area_round_mismatch" in capability["missing_evidence"]
        assert "current_gold_not_observed" in capability["missing_evidence"]
    public_state = result["current_public_state"]
    assert public_state["gold"] is None
    assert public_state["max_gold"] is None
    assert public_state["refresh_cost"] == 0
    assert public_state["upgrade_cost"] == 6
    assert public_state["economy"]["refresh_cost"] == 0
    assert public_state["economy"]["upgrade_cost"] == 6


def test_battlegrounds_shop_priority_survives_unknown_actual_costs(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=2,
        battlegrounds=_recruit_snapshot(
            round=4,
            observed_at=now,
            gold=0,
            max_gold=10,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_MINION_UNKNOWN_COST",
                    name="Observed Minion",
                    card_type="MINION",
                    current_cost=None,
                    premium=False,
                    position=1,
                ),
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_OBSERVED_COST",
                    name="Observed Spell",
                    card_type="BATTLEGROUND_SPELL",
                    current_cost=1,
                    premium=False,
                    position=2,
                ),
            ),
            warband=(
                BattlegroundsCardSnapshot(
                    card_id="BG_WARBAND_CONTEXT",
                    name="Current Warband Minion",
                    card_type="MINION",
                    premium=False,
                    position=1,
                ),
            ),
        ),
    )
    catalog_facts = {
        "BG_MINION_UNKNOWN_COST": {"card_type": "MINION", "rules_text": "Rule A"},
        "BG_SPELL_OBSERVED_COST": {
            "card_type": "BATTLEGROUND_SPELL",
            "rules_text": "Rule B",
        },
        "BG_WARBAND_CONTEXT": {"card_type": "MINION", "rules_text": "Rule C"},
    }

    def advice_with_catalog(facts: dict[str, Any]) -> dict[str, Any]:
        plugin = types.SimpleNamespace(
            cfg=CompanionConfig(llm_data_consent=True),
            _season={"key": "season-14-36.2", "status": "bundled_static"},
            _stats=BattlegroundsStats(),
            _catalog=types.SimpleNamespace(
                status=lambda: {"available": True},
                facts_for=lambda _value: {
                    "available": True,
                    "observed_card_facts": facts,
                },
            ),
            _ensure_monitor=lambda: types.SimpleNamespace(
                snapshot=lambda: snapshot,
                status=lambda: types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=now,
                    last_event_at=now,
                ),
            ),
        )
        return asyncio.run(
            entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
                plugin, topic="current_strategy"
            )
        )

    result = advice_with_catalog(catalog_facts)

    priority = result["capabilities"]["shop_card_priority_advice"]
    affordability = result["capabilities"]["purchase_affordability"]
    purchase = result["capabilities"]["specific_purchase_advice"]
    assert priority["available"] is True
    assert "shop_actual_cost_incomplete" in priority["uncertain_evidence"]
    assert affordability["available"] is False
    assert affordability["status"] == "partial"
    assert "shop_actual_cost_incomplete" in affordability["missing_evidence"]
    assert purchase["available"] is False
    assert purchase["status"] == "partial"
    assert "shop_actual_cost_incomplete" in purchase["missing_evidence"]
    guardrails = result["decision_guardrails"]
    assert guardrails["qualitative_shop_priority_allowed"] is True
    assert guardrails["purchase_affordability_allowed"] is False
    assert guardrails["exact_purchase_sequence_allowed"] is False
    assert guardrails["whole_shop_affordability"] == "unknown"
    assert guardrails["unknown_actual_cost_card_ids"] == ["BG_MINION_UNKNOWN_COST"]
    assert "even when current Gold is 0" in guardrails["mandatory_instruction"]
    assert "可能有 0 费商品" in guardrails["required_disclaimer_zh_CN"]
    assert "只能空过" in guardrails["forbidden_conclusions_zh_CN"]
    assert result["current_public_state"]["shop"][0]["current_cost"] is None
    assert result["current_public_state"]["shop"][1]["current_cost"] == 1
    decision = result["current_recruit_decision"]
    assert decision["current_gold"] == 0
    assert decision["whole_shop_affordability"] == "unknown"
    assert decision["qualitative_shop_priority_allowed"] is True
    assert decision["whole_shop_affordability_allowed"] is False
    assert decision["exact_purchase_sequence_allowed"] is False
    assert decision["legal_actions_enumerated"] is False
    assert decision["forbidden_whole_turn_conclusion"] is True
    assert decision["cards"] == [
        {
            "position": 1,
            "card_id": "BG_MINION_UNKNOWN_COST",
            "name": "Observed Minion",
            "card_type": "MINION",
            "current_cost": None,
            "affordability": "unknown_cost_may_be_zero",
        },
        {
            "position": 2,
            "card_id": "BG_SPELL_OBSERVED_COST",
            "name": "Observed Spell",
            "card_type": "BATTLEGROUND_SPELL",
            "current_cost": 1,
            "affordability": "known_unaffordable",
        },
    ]
    assert "不能据此断言只能空过" in decision["required_answer_zh_CN"]
    assert (
        result["answer_contract"][
            "shop_card_priority_must_not_claim_affordability_or_exact_sequence"
        ]
        is True
    )
    assert result["answer_contract"]["never_claim_no_legal_actions_from_this_snapshot"] is True
    assert result["answer_contract"]["unknown_current_cost_must_remain_null"] is True
    assert (
        result["answer_contract"][
            "never_generalize_partial_affordability_to_the_entire_shop"
        ]
        is True
    )

    incomplete_catalog = dict(catalog_facts)
    incomplete_catalog.pop("BG_MINION_UNKNOWN_COST")
    incomplete = advice_with_catalog(incomplete_catalog)
    incomplete_priority = incomplete["capabilities"]["shop_card_priority_advice"]
    assert incomplete_priority["available"] is False
    assert incomplete_priority["status"] == "partial"
    assert "recruit_context_catalog_coverage_incomplete" in incomplete_priority[
        "missing_evidence"
    ]
    assert incomplete_priority["unresolved_catalog_ids"] == ["BG_MINION_UNKNOWN_COST"]


def test_battlegrounds_combat_contract_hides_shop_and_allows_only_board_commentary(
    monkeypatch,
) -> None:
    entry = _load_sdk_entry(monkeypatch)
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="combat",
        game_number=2,
        battlegrounds=_combat_snapshot(
            round=8,
            observed_at=now,
            shop=(BattlegroundsCardSnapshot(card_id="STALE_SHOP", name="Stale Shop"),),
            warband=(BattlegroundsCardSnapshot(card_id="PUBLIC_BOARD", name="Board Minion"),),
        ),
    )
    catalog_calls: list[object] = []
    catalog = types.SimpleNamespace(
        status=lambda: {"available": True, "provider": "test"},
        facts_for=lambda value: catalog_calls.append(value) or {"unexpected": True},
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=BattlegroundsStats(),
        _catalog=catalog,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="current_strategy"
        )
    )

    assert result["available"] is True
    assert result["current_public_state"]["shop"] == []
    assert result["capabilities"]["shop_card_priority_advice"]["available"] is False
    assert result["capabilities"]["purchase_affordability"]["available"] is False
    assert result["capabilities"]["specific_purchase_advice"]["available"] is False
    assert result["capabilities"]["combat_commentary"]["available"] is True
    assert result["answer_contract"]["combat_never_implies_current_shop_visibility"] is True
    assert len(catalog_calls) == 1
    catalog_snapshot = catalog_calls[0]
    assert catalog_snapshot.phase == "combat"
    assert catalog_snapshot.shop == ()
    assert catalog_snapshot.hand == ()
    assert catalog_snapshot.current_choice is None
    assert catalog_snapshot.hero_choices == ()


def test_hero_performance_targets_current_local_hero_sample(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="season-14-36.2", mode="solo", placement=1, hero_id="BG_OTHER")
    stats.record_game(season="season-14-36.2", mode="solo", placement=3, hero_id="BG_CURRENT")
    now = time.time()
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=4,
        battlegrounds=BattlegroundsSnapshot(
            variant="solo",
            phase="recruit",
            lobby=(
                BattlegroundsPlayerSnapshot(
                    player_id=1,
                    is_local=True,
                    hero_card_id="BG_CURRENT",
                    hero_name="Current Hero",
                ),
            ),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=stats,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=now,
                last_event_at=now,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="hero_performance"
        )
    )

    assert result["available"] is True
    assert result["hero_performance"]["hero"] == {
        "card_id": "BG_CURRENT",
        "name": "Current Hero",
    }
    assert result["hero_performance"]["stats_key"] == "BG_CURRENT"
    assert result["hero_performance"]["local_sample"]["games"] == 1


def test_stale_hero_performance_does_not_expose_snapshot_identity(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(
        season="season-14-36.2",
        mode="solo",
        placement=2,
        hero_id="BG_STALE_HERO",
    )
    stale_at = time.time() - 3600.0
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=4,
        battlegrounds=BattlegroundsSnapshot(
            variant="solo",
            phase="recruit",
            lobby=(
                BattlegroundsPlayerSnapshot(
                    player_id=1,
                    is_local=True,
                    hero_card_id="BG_STALE_HERO",
                    hero_name="Stale Hero",
                ),
            ),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=stats,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=stale_at,
                last_event_at=stale_at,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="hero_performance"
        )
    )

    performance = result["hero_performance"]
    assert result["available"] is False
    assert result["reason"] == "no_live_battlegrounds_state"
    assert performance["hero"] is None
    assert performance["stats_key"] == ""
    assert performance["variant"] == ""
    assert performance["local_sample"] == {}
    assert (
        result["local_season_stats"]["solo"]["heroes"]["BG_STALE_HERO"]["games"]
        == 1
    )


def test_stale_snapshot_does_not_affect_verified_season_meta(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stale_at = time.time() - 3600.0
    season = {
        "key": "season-14-36.2",
        "season": 14,
        "status": "bundled_static",
        "verified_at": "2026-08-21",
        "source_url": "https://hearthstone.blizzard.com/news/season-14",
    }
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=4,
        battlegrounds=BattlegroundsSnapshot(
            phase="recruit",
            shop=(BattlegroundsCardSnapshot(card_id="BG_STALE_SEASON_SHOP"),),
        ),
    )
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season=season,
        _stats=BattlegroundsStats(),
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="watching",
                monitor_running=True,
                last_line_at=stale_at,
                last_event_at=stale_at,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="season_meta"
        )
    )

    assert result["available"] is True
    assert result["season_rules"] == season
    assert result["local_season_stats"] == {}
    assert all(
        capability["unresolved_catalog_ids"] == []
        for capability in result["capabilities"].values()
    )
    assert "BG_STALE_SEASON_SHOP" not in json.dumps(result["capabilities"])


def test_post_game_does_not_treat_historical_aggregate_as_recent_game(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    stats = BattlegroundsStats()
    stats.record_game(season="season-14-36.2", mode="solo", placement=2, hero_id="BG_OLD")
    snapshot = GameSnapshot(mode="unknown", phase="idle")
    plugin = types.SimpleNamespace(
        cfg=CompanionConfig(llm_data_consent=True),
        _season={"key": "season-14-36.2", "status": "bundled_static"},
        _stats=stats,
        _ensure_monitor=lambda: types.SimpleNamespace(
            snapshot=lambda: snapshot,
            status=lambda: types.SimpleNamespace(
                source_state="waiting",
                monitor_running=True,
                last_line_at=0.0,
                last_event_at=0.0,
            ),
        ),
    )

    result = asyncio.run(
        entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
            plugin, topic="post_game"
        )
    )

    assert result["available"] is False
    assert result["reason"] == "no_recent_battlegrounds_post_game"
    assert result["current_public_state"] is None
    assert result["local_season_stats"] == {}


def test_post_game_requires_a_recent_ended_snapshot(monkeypatch) -> None:
    entry = _load_sdk_entry(monkeypatch)
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="ended",
        game_number=5,
        battlegrounds=BattlegroundsSnapshot(
            variant="solo",
            round=11,
            phase="ended",
            placement=2,
            shop=(BattlegroundsCardSnapshot(card_id="BG_ENDED_SHOP"),),
            lobby=(
                BattlegroundsPlayerSnapshot(
                    player_id=1,
                    is_local=True,
                    hero_card_id="BG_ENDED_HERO",
                    hero_name="Ended Hero",
                ),
            ),
        ),
    )

    def result_at(last_event_at: float) -> dict[str, Any]:
        plugin = types.SimpleNamespace(
            cfg=CompanionConfig(llm_data_consent=True),
            _season={"key": "season-14-36.2", "status": "bundled_static"},
            _stats=BattlegroundsStats(),
            _ensure_monitor=lambda: types.SimpleNamespace(
                snapshot=lambda: snapshot,
                status=lambda: types.SimpleNamespace(
                    source_state="watching",
                    monitor_running=True,
                    last_line_at=last_event_at,
                    last_event_at=last_event_at,
                ),
            ),
        )
        return asyncio.run(
            entry.HearthstoneCompanionPlugin.hearthstone_battlegrounds_advice(
                plugin, topic="post_game"
            )
        )

    recent = result_at(time.time())
    stale = result_at(time.time() - 3600.0)

    assert recent["available"] is True
    assert recent["current_public_state"]["placement"] == 2
    assert recent["current_public_state"]["local_player"]["hero_card_id"] == "BG_ENDED_HERO"
    assert all(
        capability["unresolved_catalog_ids"] == []
        for capability in recent["capabilities"].values()
    )
    assert "BG_ENDED_SHOP" not in json.dumps(recent["capabilities"])
    assert stale["available"] is False
    assert stale["reason"] == "no_recent_battlegrounds_post_game"
    assert stale["current_public_state"] is None
