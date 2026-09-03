from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Mapping

from neko_answer_eval import build_answer_case, evaluate_passive_context_segments

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hearthstone_companion_checkpoint_probe"
NEKO_ROOT = PROJECT_ROOT.parent / "N.E.K.O"
NEKO_PYTHON = (
    NEKO_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
    if os.name == "nt"
    else NEKO_ROOT / ".venv" / "bin" / "python"
)

_SUPPORTED_KINDS = {
    "constructed_round_opponent",
    "bg_shop_round2",
    "bg_shop_round3",
    "bg_upgrade_blocked",
    "bg_upgrade_affordable",
}


def _decorator(*args: Any, **kwargs: Any):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def decorate(target: Any) -> Any:
        return target

    return decorate


def _load_entry() -> Any:
    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = str(PROJECT_ROOT / "__init__.py")
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(PROJECT_ROOT)]
    sys.modules[PACKAGE_NAME] = package

    sdk_module_names = ("plugin", "plugin.sdk", "plugin.sdk.plugin")
    missing = object()
    previous_sdk_modules = {
        name: sys.modules.get(name, missing) for name in sdk_module_names
    }

    plugin_package = types.ModuleType("plugin")
    plugin_package.__path__ = []
    sdk_package = types.ModuleType("plugin.sdk")
    sdk_package.__path__ = []
    sdk_module = types.ModuleType("plugin.sdk.plugin")

    class FakePluginBase:
        pass

    sdk_module.Err = lambda value: value
    sdk_module.NekoPluginBase = FakePluginBase
    sdk_module.Ok = lambda value: value
    sdk_module.SdkError = RuntimeError
    sdk_module.lifecycle = _decorator
    sdk_module.llm_tool = _decorator
    sdk_module.message = _decorator
    sdk_module.neko_plugin = _decorator
    sdk_module.plugin_entry = _decorator
    sdk_module.timer_interval = _decorator
    sdk_module.tr = lambda _key, default="": default
    sdk_module.ui = types.SimpleNamespace(action=_decorator, context=_decorator)
    sdk_module.unwrap_or = lambda value, default: default if value is None else value
    sys.modules["plugin"] = plugin_package
    sys.modules["plugin.sdk"] = sdk_package
    sys.modules["plugin.sdk.plugin"] = sdk_module

    try:
        module_name = f"{PACKAGE_NAME}.sdk_entry"
        spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / "__init__.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("sdk_entry_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_sdk_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


ENTRY = _load_entry()
CompanionConfig = importlib.import_module(f"{PACKAGE_NAME}.config").CompanionConfig
PowerLogParser = importlib.import_module(f"{PACKAGE_NAME}.powerlog").PowerLogParser
BattlegroundsStats = importlib.import_module(
    f"{PACKAGE_NAME}.stats"
).BattlegroundsStats
build_live_state_segments = importlib.import_module(
    f"{PACKAGE_NAME}.commentary"
).build_live_state_segments


def _replay_to_line(path: Path, line_limit: int, *, observed_at: float) -> Any:
    parser = PowerLogParser()
    lines_seen = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            parser.feed_line(line, now=observed_at)
            lines_seen = line_number
            if line_number >= line_limit:
                break
    if lines_seen < line_limit:
        raise ValueError("checkpoint_after_end_of_log")
    return parser.snapshot()


def _plugin_for_snapshot(snapshot: Any, *, observed_at: float) -> Any:
    runtime = types.SimpleNamespace(
        source_state="watching",
        monitor_running=True,
        last_line_at=observed_at,
        last_state_at=observed_at,
        last_event_at=observed_at,
        source_modified_at=observed_at,
    )
    monitor = types.SimpleNamespace(
        snapshot=lambda: snapshot,
        status=lambda: runtime,
    )
    plugin = object.__new__(ENTRY.HearthstoneCompanionPlugin)
    plugin.cfg = CompanionConfig(llm_data_consent=True)
    plugin._monitor = monitor
    plugin._season = {
        "key": "real-log-checkpoint",
        "status": "unavailable",
        "source_urls": [],
    }
    plugin._stats = BattlegroundsStats()
    plugin._catalog = None
    plugin._catalog_status = lambda: {"available": False}
    return plugin


async def _tool_results(snapshot: Any, *, query: str, observed_at: float) -> tuple[dict[str, Any], dict[str, Any]]:
    plugin = _plugin_for_snapshot(snapshot, observed_at=observed_at)
    turn = await ENTRY.HearthstoneCompanionPlugin.hearthstone_current_turn(plugin)
    focused = await ENTRY.HearthstoneCompanionPlugin.hearthstone_live_state(
        plugin,
        query=query,
    )
    return dict(turn.get("_canonical") or turn), dict(focused.get("_canonical") or focused)


def _public_cards(snapshot: Any, location: str) -> list[dict[str, Any]]:
    if location == "opponent":
        constructed = snapshot.to_public_dict().get("constructed") or {}
        board = ((constructed.get("opponent") or {}).get("board") or {})
        cards = board.get("minions") or []
    else:
        battlegrounds = snapshot.battlegrounds.to_public_dict()
        cards = battlegrounds.get(location) or []
    return [dict(card) for card in cards if isinstance(card, Mapping)]


def _all_ids_present(cards: list[Mapping[str, Any]], text: str) -> bool:
    identifiers = [str(card.get("card_id") or "") for card in cards]
    return bool(identifiers) and all(identifier and identifier in text for identifier in identifiers)


def _active_keywords(card: Mapping[str, Any]) -> set[str]:
    keywords = card.get("keywords")
    if isinstance(keywords, Mapping):
        return {str(name) for name, enabled in keywords.items() if enabled is True}
    if isinstance(keywords, (list, tuple)):
        return {str(name) for name in keywords}
    return set()


def _host_parse_push_texts(texts: list[str]) -> list[dict[str, Any]]:
    if not NEKO_PYTHON.is_file():
        raise ValueError("neko_host_unavailable")
    script = (
        "import json,sys;"
        "from utils.result_parser import parse_push_message_content;"
        "from utils.tokenize import count_tokens;"
        "items=json.load(sys.stdin);"
        "print(json.dumps([{'tokens':count_tokens(item),'parsed':"
        "parse_push_message_content(item)} for item in items],ensure_ascii=False))"
    )
    completed = subprocess.run(
        [str(NEKO_PYTHON), "-c", script],
        cwd=NEKO_ROOT,
        input=json.dumps(texts, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if completed.returncode != 0:
        raise ValueError("neko_host_parse_failed")
    try:
        parsed = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("neko_host_parse_failed") from exc
    if not isinstance(parsed, list) or len(parsed) != len(texts):
        raise ValueError("neko_host_parse_failed")
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _host_select_push_texts(texts: list[str]) -> dict[str, Any]:
    if not NEKO_PYTHON.is_file():
        raise ValueError("neko_host_unavailable")
    script = (
        "import json,sys;"
        "from config import AGENT_CALLBACK_TOTAL_MAX_TOKENS;"
        "from main_logic.core.callback_render import _select_callbacks_within_token_budget;"
        "from utils.result_parser import parse_push_message_content;"
        "items=[parse_push_message_content(item) for item in json.load(sys.stdin)];"
        "callbacks=[{'summary':item,'detail':item,'source_name':'hearthstone_companion'}"
        " for item in items];"
        "selected,deferred=_select_callbacks_within_token_budget("
        "callbacks,AGENT_CALLBACK_TOTAL_MAX_TOKENS);"
        "print(json.dumps({'selected':len(selected),'deferred':len(deferred)}))"
    )
    completed = subprocess.run(
        [str(NEKO_PYTHON), "-c", script],
        cwd=NEKO_ROOT,
        input=json.dumps(texts, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if completed.returncode != 0:
        raise ValueError("neko_host_select_failed")
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("neko_host_select_failed") from exc
    if not isinstance(result, Mapping):
        raise ValueError("neko_host_select_failed")
    return dict(result)


def _passive_case_ids(kind: str) -> tuple[str, ...]:
    if kind == "constructed_round_opponent":
        return ("constructed_round_v1", "constructed_opponent_v1")
    if kind in {"bg_shop_round2", "bg_shop_round3"}:
        return ("bg_shop_v1",)
    if kind == "bg_upgrade_blocked":
        return ("bg_upgrade_blocked_v1",)
    return ("bg_upgrade_affordable_v1",)


def _production_passive_evidence(
    snapshot: Any,
    *,
    kind: str,
    observed_at: float,
) -> dict[str, Any]:
    segments = build_live_state_segments(
        snapshot,
        observed_at=observed_at,
        max_prompt_bytes=900,
    )
    texts = [text for _name, text in segments]
    host_results = _host_parse_push_texts(texts)
    host_selection = _host_select_push_texts(texts)
    host_texts = [str(item.get("parsed") or "") for item in host_results]
    evaluations = {
        case_id: evaluate_passive_context_segments(
            build_answer_case(case_id, snapshot),
            host_texts,
        )
        for case_id in _passive_case_ids(kind)
    }
    return {
        "segment_count": len(segments),
        "host_exact": host_texts == texts,
        "host_selected_all": (
            int(host_selection.get("selected") or 0) == len(texts)
            and int(host_selection.get("deferred") or 0) == 0
        ),
        "max_tokens": max(
            (int(item.get("tokens") or 0) for item in host_results),
            default=0,
        ),
        "evaluations": evaluations,
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    condition: bool,
) -> None:
    checks.append({"name": name, "passed": bool(condition)})


async def _evaluate(
    *,
    alias: str,
    kind: str,
    line_limit: int,
    path: Path,
) -> dict[str, Any]:
    observed_at = time.time()
    snapshot = _replay_to_line(path, line_limit, observed_at=observed_at)
    passive = _production_passive_evidence(
        snapshot,
        kind=kind,
        observed_at=observed_at,
    )
    checks: list[dict[str, Any]] = []
    _check(checks, "passive_segments_observed", passive["segment_count"] > 0)
    _check(checks, "passive_host_parser_kept_every_segment", passive["host_exact"] is True)
    _check(checks, "passive_host_selector_kept_every_segment", passive["host_selected_all"] is True)
    _check(checks, "passive_segments_below_host_limit", passive["max_tokens"] <= 180)
    for case_id, evaluation in passive["evaluations"].items():
        _check(
            checks,
            f"passive_bundle_{case_id}",
            evaluation.get("passed") is True,
        )
    facts: dict[str, Any] = {
        "game_number": int(snapshot.game_number),
        "mode": str(snapshot.mode),
        "round": int(snapshot.round),
    }

    if kind == "constructed_round_opponent":
        turn, focused = await _tool_results(
            snapshot,
            query="对面场上有什么随从",
            observed_at=observed_at,
        )
        cards = _public_cards(snapshot, "opponent")
        tool_opponent = focused
        round_answer = (
            f"第{turn['round']}回合" if turn.get("round_known") else "回合未知"
        )
        facts.update(
            {
                "action_turn": int(snapshot.turn),
                "opponent_board_count": len(cards),
            }
        )
        _check(checks, "constructed_mode", snapshot.mode == "constructed")
        _check(checks, "round_is_11", turn.get("round") == 11)
        _check(checks, "action_turn_is_21", turn.get("action_turn") == 21)
        _check(
            checks,
            "round_answer_uses_round_not_action_turn",
            round_answer == "第11回合" and "21" not in round_answer,
        )
        _check(checks, "opponent_board_count_is_2", len(cards) == 2)
        _check(
            checks,
            "tool_opponent_delivery_full",
            isinstance(tool_opponent, Mapping)
            and tool_opponent.get("source_complete") is True
            and tool_opponent.get("area") == "opponent_board"
            and tool_opponent.get("slot_count") == len(cards),
        )
        _check(
            checks,
            "tool_opponent_contains_all_ids",
            _all_ids_present(
                cards,
                "|".join(str(item) for item in tool_opponent.get("required_card_ids") or []),
            ),
        )
        _check(
            checks,
            "tool_opponent_summary_contains_all_ids",
            _all_ids_present(cards, str(focused.get("summary") or "")),
        )
    elif kind in {"bg_shop_round2", "bg_shop_round3"}:
        turn, focused = await _tool_results(
            snapshot,
            query="当前商店有什么",
            observed_at=observed_at,
        )
        bg = snapshot.battlegrounds
        if bg is None:
            raise ValueError("battlegrounds_snapshot_missing")
        cards = _public_cards(snapshot, "shop")
        tool_shop = focused
        shop_area = bg.areas.get("shop")
        tavern_spells = [
            card
            for card in cards
            if str(card.get("card_type") or "").upper()
            in {"BATTLEGROUND_SPELL", "TAVERN_SPELL", "SPELL"}
        ]
        keyword_union = set().union(*(_active_keywords(card) for card in cards)) if cards else set()
        facts.update(
            {
                "shop_count": len(cards),
                "shop_complete": bool(shop_area and shop_area.complete),
                "gold": bg.gold,
                "refresh_cost": bg.refresh_cost,
                "upgrade_cost": bg.upgrade_cost,
                "tavern_spell_count": len(tavern_spells),
            }
        )
        expected_round = 2 if kind == "bg_shop_round2" else 3
        _check(checks, "battlegrounds_mode", snapshot.mode == "battlegrounds")
        _check(checks, "round_matches", turn.get("round") == expected_round)
        _check(checks, "shop_area_complete", bool(shop_area and shop_area.complete))
        _check(
            checks,
            "tool_shop_delivery_full",
            isinstance(tool_shop, Mapping)
            and tool_shop.get("source_complete") is True
            and tool_shop.get("area") == "shop"
            and tool_shop.get("slot_count") == len(cards),
        )
        _check(
            checks,
            "tool_shop_contains_all_ids",
            _all_ids_present(
                cards,
                "|".join(str(item) for item in tool_shop.get("required_card_ids") or []),
            ),
        )
        _check(
            checks,
            "tool_shop_summary_contains_all_ids",
            _all_ids_present(cards, str(focused.get("summary") or "")),
        )
        if kind == "bg_shop_round2":
            _check(
                checks,
                "golden_divine_shield_observed",
                any(
                    card.get("premium") is True
                    and "divine_shield" in _active_keywords(card)
                    for card in cards
                ),
            )
            _check(
                checks,
                "one_cost_tavern_spell_observed",
                any(card.get("current_cost") == 1 for card in tavern_spells),
            )
        else:
            _check(checks, "gold_is_5", bg.gold == 5)
            _check(checks, "refresh_cost_is_1", bg.refresh_cost == 1)
            _check(checks, "upgrade_cost_is_6", bg.upgrade_cost == 6)
            _check(checks, "shop_count_is_5", len(cards) == 5)
            _check(
                checks,
                "two_cost_tavern_spell_observed",
                any(card.get("current_cost") == 2 for card in tavern_spells),
            )
            _check(checks, "reborn_observed", "reborn" in keyword_union)
            _check(checks, "deathrattle_observed", "deathrattle" in keyword_union)
    else:
        turn, focused = await _tool_results(
            snapshot,
            query="现在能不能升本",
            observed_at=observed_at,
        )
        bg = snapshot.battlegrounds
        if bg is None:
            raise ValueError("battlegrounds_snapshot_missing")
        evidence = focused.get("evidence") or {}
        upgrade = evidence.get("upgrade_affordability") or {}
        tool_economy = focused.get("economy") or {}
        expected_cost = 6 if kind == "bg_upgrade_blocked" else 3
        expected_remaining = -1 if kind == "bg_upgrade_blocked" else 2
        remaining = (
            bg.gold - bg.upgrade_cost
            if isinstance(bg.gold, int) and isinstance(bg.upgrade_cost, int)
            else None
        )
        facts.update(
            {
                "gold": bg.gold,
                "upgrade_cost": bg.upgrade_cost,
                "gold_after_upgrade": remaining,
            }
        )
        _check(checks, "battlegrounds_mode", snapshot.mode == "battlegrounds")
        _check(checks, "round_is_3", turn.get("round") == 3)
        _check(checks, "gold_is_5", bg.gold == 5)
        _check(checks, "upgrade_cost_matches", bg.upgrade_cost == expected_cost)
        _check(checks, "gold_after_upgrade_matches", remaining == expected_remaining)
        _check(checks, "upgrade_capability_available", upgrade.get("available") is True)
        _check(
            checks,
            "tool_upgrade_cost_matches",
            tool_economy.get("upgrade_actual_cost") == expected_cost,
        )
        _check(
            checks,
            "tool_upgrade_affordability_matches",
            tool_economy.get("can_upgrade") is (kind == "bg_upgrade_affordable"),
        )
        _check(
            checks,
            "tool_upgrade_remaining_semantics_match",
            (
                tool_economy.get("remaining_after_upgrade") == 2
                and tool_economy.get("remaining_status") == "applicable"
            )
            if kind == "bg_upgrade_affordable"
            else (
                tool_economy.get("remaining_after_upgrade") is None
                and tool_economy.get("shortfall_for_upgrade") == 1
                and tool_economy.get("remaining_status")
                == "not_applicable_insufficient_gold"
            ),
        )

    passed = all(check["passed"] for check in checks)
    return {
        "alias": alias,
        "kind": kind,
        "line": line_limit,
        "status": "PASS" if passed else "FAIL",
        "facts": facts,
        "checks": checks,
        "final_neko_answer": {
            "status": "SKIP",
            "evidence": "UNVERIFIED",
            "reason": "official_sdk_has_no_final_response_receipt",
        },
    }


def _parse_checkpoint(raw: list[str]) -> tuple[str, str, int, Path]:
    alias, kind, line_text, path_text = raw
    if not alias or len(alias) > 48 or not alias.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid_checkpoint_alias")
    if kind not in _SUPPORTED_KINDS:
        raise ValueError("unsupported_checkpoint_kind")
    try:
        line_limit = int(line_text)
    except ValueError as exc:
        raise ValueError("invalid_checkpoint_line") from exc
    if line_limit <= 0:
        raise ValueError("invalid_checkpoint_line")
    return alias, kind, line_limit, Path(path_text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay sanitized, exact Power.log checkpoints through live tool serializers."
    )
    parser.add_argument(
        "--checkpoint",
        action="append",
        nargs=4,
        required=True,
        metavar=("ALIAS", "KIND", "LINE", "POWER_LOG"),
    )
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    aliases: set[str] = set()
    for raw in args.checkpoint:
        alias = str(raw[0])[:48]
        try:
            alias, kind, line_limit, path = _parse_checkpoint(raw)
            if alias in aliases:
                raise ValueError("duplicate_checkpoint_alias")
            aliases.add(alias)
            result = asyncio.run(
                _evaluate(
                    alias=alias,
                    kind=kind,
                    line_limit=line_limit,
                    path=path,
                )
            )
        except (OSError, ValueError) as exc:
            code = str(exc)
            if code not in {
                "checkpoint_after_end_of_log",
                "battlegrounds_snapshot_missing",
                "duplicate_checkpoint_alias",
                "invalid_checkpoint_alias",
                "invalid_checkpoint_line",
                "neko_host_parse_failed",
                "neko_host_select_failed",
                "neko_host_unavailable",
                "unsupported_checkpoint_kind",
            }:
                code = "checkpoint_unreadable"
            result = {
                "alias": alias,
                "status": "ERROR",
                "error_code": code,
            }
        results.append(result)

    passed = all(result.get("status") == "PASS" for result in results)
    output = {
        "schema": "hearthstone_real_log_checkpoints_v1",
        "status": "PASS" if passed else "FAIL",
        "checkpoints": results,
    }
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
