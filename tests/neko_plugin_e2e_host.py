from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ID = "hearthstone_companion"
TOOL_NAMES = ("hearthstone_current_turn", "hearthstone_live_state")
TOOL_ARGUMENT_FIELDS = {
    "hearthstone_current_turn": (),
    "hearthstone_live_state": ("query",),
}
CASE_IDS = {
    "constructed_round_v1",
    "constructed_opponent_v1",
    "bg_shop_v1",
    "bg_upgrade_blocked_v1",
    "bg_upgrade_affordable_v1",
    "constructed_started_v1",
    "constructed_ended_v1",
}
CASE_QUESTIONS = {
    "constructed_round_v1": "现在第几回合？只回答游戏里的完整回合数。",
    "constructed_opponent_v1": "对面场上现在有哪些随从？只用 CardID 完整列出。",
    "bg_shop_v1": (
        "请查询当前炉石酒馆战棋的商店有哪些牌。按 CardID 分组，逐组说出类型、实际费用、金色状态和完整的当前关键词。"
    ),
    "bg_upgrade_blocked_v1": "请查询当前炉石酒馆战棋局面：我能升本吗？升完还剩多少金币？",
    "bg_upgrade_affordable_v1": "请查询当前炉石酒馆战棋局面：我能升本吗？升完还剩多少金币？",
}
CONTROL_PREFIX = "/_hearthstone_e2e"
TOKEN_HEADER = "x-hearthstone-e2e-token"
MAX_CAPTURE_BYTES = 1_048_576
LIFECYCLE_SUBMISSION_TIMEOUT_SECONDS = 3.0

_ENV_NEKO_ROOT = "HEARTHSTONE_E2E_NEKO_ROOT"
_ENV_BUILTIN_ROOT = "HEARTHSTONE_E2E_BUILTIN_ROOT"
_ENV_USER_ROOT = "HEARTHSTONE_E2E_USER_ROOT"


def _bootstrap_plugin_roots() -> None:
    """Apply the same isolated roots in the server and spawned plugin child."""

    neko_root_text = os.getenv(_ENV_NEKO_ROOT, "").strip()
    builtin_root_text = os.getenv(_ENV_BUILTIN_ROOT, "").strip()
    user_root_text = os.getenv(_ENV_USER_ROOT, "").strip()
    if not (neko_root_text and builtin_root_text and user_root_text):
        return

    neko_root = Path(neko_root_text).resolve(strict=False)
    plugin_package_root = neko_root / "plugin"
    for candidate in (str(plugin_package_root), str(neko_root)):
        while candidate in sys.path:
            sys.path.remove(candidate)
        sys.path.insert(0, candidate)

    import plugin.settings as settings

    builtin_root = Path(builtin_root_text).resolve(strict=False)
    user_root = Path(user_root_text).resolve(strict=False)
    settings.BUILTIN_PLUGIN_CONFIG_ROOT = builtin_root
    settings.USER_PLUGIN_CONFIG_ROOT = user_root
    settings.PLUGIN_CONFIG_ROOT = builtin_root
    settings.PLUGIN_CONFIG_ROOTS = (builtin_root, user_root)


# multiprocessing.spawn executes the parent main module before unpickling the
# plugin target. Apply root isolation here as well as in main().
_bootstrap_plugin_roots()


def _is_same_or_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        return resolved == resolved_root or resolved.is_relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_json_object(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_CAPTURE_BYTES:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_fact_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    if line.startswith("final_answer="):
        line = line[len("final_answer=") :].strip()
    else:
        match = re.match(r"facts\[[A-Za-z0-9_]+\]=(.*)\Z", line)
        if match is not None:
            line = match.group(1).strip()
    if not line:
        return ""
    return hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    epoch: int
    name: str
    call_id: str
    argument_fields: tuple[str, ...]
    started_at: float
    completed_at: float
    status: str
    is_error: bool
    output_contract: Mapping[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "name": self.name,
            "call_id": self.call_id,
            "argument_fields": list(self.argument_fields),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "is_error": self.is_error,
            "output_contract": dict(self.output_contract),
        }


class ToolCallRecorder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epoch = 0
        self._calls: list[RecordedToolCall] = []

    def begin_epoch(self) -> int:
        with self._lock:
            self._epoch += 1
            return self._epoch

    def current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def record(
        self,
        *,
        epoch: int,
        name: str,
        call_id: str,
        argument_fields: tuple[str, ...],
        started_at: float,
        completed_at: float,
        status_code: int,
        response: Mapping[str, Any],
    ) -> None:
        is_error = bool(response.get("is_error", True)) or status_code != 200
        raw_output = response.get("output")
        output = raw_output if isinstance(raw_output, Mapping) else {}
        checklist = output.get("answer_checklist")
        checklist = checklist if isinstance(checklist, Mapping) else {}
        required_ids = output.get("required_card_ids")
        required_ids = required_ids if isinstance(required_ids, list) else []
        card_groups = output.get("card_groups")
        card_groups = card_groups if isinstance(card_groups, list) else []
        summary = str(raw_output) if isinstance(raw_output, str) else str(output.get("summary") or "")
        safe_keys = {
            "answer_checklist",
            "area",
            "available",
            "card_groups",
            "current",
            "economy",
            "focus",
            "format",
            "group_count",
            "mode",
            "required_card_ids",
            "slot_count",
            "source_complete",
            "summary",
        }
        output_contract = {
            "output_kind": "text" if isinstance(raw_output, str) else "object",
            "top_level_fields": sorted(str(key) for key in output if key in safe_keys),
            "summary_chars": len(summary),
            "authority": (
                "plain_text_canonical_v1" if isinstance(raw_output, str) else str(checklist.get("authority") or "")[:64]
            ),
            "required_card_id_count": (summary.count("CardID=") if isinstance(raw_output, str) else len(required_ids)),
            "card_group_count": (summary.count("CardID=") if isinstance(raw_output, str) else len(card_groups)),
            "summary_covers_required_card_ids": (
                summary.count("CardID=") > 0
                if isinstance(raw_output, str)
                else bool(required_ids) and all(str(card_id) in summary for card_id in required_ids)
            ),
            "fact_sha256": _canonical_fact_fingerprint(summary),
            "fact_chars": len(summary.splitlines()[0].strip()) if summary.strip() else 0,
        }
        call = RecordedToolCall(
            epoch=epoch,
            name=name,
            call_id=call_id[:160],
            argument_fields=argument_fields,
            started_at=started_at,
            completed_at=completed_at,
            status="completed",
            is_error=is_error,
            output_contract=output_contract,
        )
        with self._lock:
            self._calls.append(call)
            if len(self._calls) > 128:
                del self._calls[:-128]

    def calls_for(self, epoch: int) -> list[dict[str, Any]]:
        with self._lock:
            return [call.public_dict() for call in self._calls if call.epoch == epoch]


class ToolCallRecorderMiddleware:
    """Observe the official plugin callback without changing its response."""

    def __init__(self, app: Any, recorder: ToolCallRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "")
        prefix = f"/api/llm-tools/callback/{PLUGIN_ID}/"
        if scope.get("type") != "http" or not path.startswith(prefix):
            await self.app(scope, receive, send)
            return

        name = path[len(prefix) :]
        if name not in TOOL_NAMES:
            await self.app(scope, receive, send)
            return
        epoch = self.recorder.current_epoch()
        request_body = bytearray()
        response_body = bytearray()
        status_code = 0
        started_at = time.time()

        async def receive_proxy() -> dict[str, Any]:
            message = await receive()
            chunk = message.get("body") if isinstance(message, Mapping) else None
            if isinstance(chunk, (bytes, bytearray)) and len(request_body) < MAX_CAPTURE_BYTES:
                remaining = MAX_CAPTURE_BYTES - len(request_body)
                request_body.extend(bytes(chunk[:remaining]))
            return message

        async def send_proxy(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
            elif message.get("type") == "http.response.body":
                chunk = message.get("body")
                if isinstance(chunk, (bytes, bytearray)) and len(response_body) < MAX_CAPTURE_BYTES:
                    remaining = MAX_CAPTURE_BYTES - len(response_body)
                    response_body.extend(bytes(chunk[:remaining]))
            await send(message)

        try:
            await self.app(scope, receive_proxy, send_proxy)
        finally:
            request = _safe_json_object(bytes(request_body))
            arguments = request.get("arguments")
            fields = (
                tuple(field for field in TOOL_ARGUMENT_FIELDS[name] if field in arguments)
                if isinstance(arguments, Mapping)
                else ()
            )
            self.recorder.record(
                epoch=epoch,
                name=name,
                call_id=str(request.get("call_id") or ""),
                argument_fields=fields,
                started_at=started_at,
                completed_at=time.time(),
                status_code=status_code,
                response=_safe_json_object(bytes(response_body)),
            )


@dataclass(frozen=True, slots=True)
class HostOptions:
    token: str
    inbox_root: Path
    active_log: Path


@dataclass(frozen=True, slots=True)
class ActivationEvidence:
    lifecycle_submission_count: int
    lifecycle_stage: str
    tool_fact_sha256: str
    pre_bytes: int = 0
    post_bytes: int = 0
    appended_bytes: int = 0


class ProbeController:
    def __init__(self, options: HostOptions, recorder: ToolCallRecorder) -> None:
        self.options = options
        self.recorder = recorder
        self.server: Any = None
        self._activation_lock = asyncio.Lock()
        self._last_ready_reason = "checkpoint_not_checked"
        self._last_ready_details: tuple[str, ...] = ()
        self._prepared_edge_case = ""

    def authorized(self, headers: Mapping[str, Any]) -> bool:
        supplied = str(headers.get(TOKEN_HEADER) or "")
        return bool(supplied) and hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(),
            hashlib.sha256(self.options.token.encode()).digest(),
        )

    @staticmethod
    def _plugin_host() -> Any | None:
        from plugin.core.state import state

        with state.acquire_plugin_hosts_read_lock():
            return state.plugin_hosts.get(PLUGIN_ID)

    @staticmethod
    def _registered_tools() -> tuple[str, ...]:
        from plugin.server.messaging.llm_tool_registry import get_plugin_tool_names

        return tuple(get_plugin_tool_names(PLUGIN_ID))

    async def _trigger_entry(
        self,
        entry_id: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        host = self._plugin_host()
        if host is None or not bool(getattr(host, "is_alive", lambda: False)()):
            return {}
        try:
            result = await host.trigger(entry_id, dict(arguments), timeout=5.0)
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError, AttributeError):
            return {}
        return dict(result) if isinstance(result, Mapping) else {}

    async def _trigger(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = await self._trigger_entry(f"__llm_tool__{name}", arguments)
        canonical = result.get("_canonical")
        return dict(canonical) if isinstance(canonical, Mapping) else result

    async def _route_diagnostics(self) -> dict[str, dict[str, Any]]:
        result = await self._trigger_entry("get_status", {})
        payload = result.get("data")
        if not isinstance(payload, Mapping):
            payload = result
        diagnostics = payload.get("diagnostics")
        routes = diagnostics.get("routes") if isinstance(diagnostics, Mapping) else None
        if not isinstance(routes, Mapping):
            return {}
        sanitized: dict[str, dict[str, Any]] = {}
        for route_name in ("agent", "lifecycle", "llm_tool"):
            route = routes.get(route_name)
            if not isinstance(route, Mapping):
                continue
            observed_at = route.get("observed_at")
            sanitized[route_name] = {
                "status": str(route.get("status") or "")[:80],
                "reason": str(route.get("reason") or "")[:80],
                "mode": str(route.get("mode") or "")[:80],
                "focus": str(route.get("focus") or "")[:80],
                "fact_sha256": (
                    str(route.get("fact_sha256") or "")
                    if re.fullmatch(r"[0-9a-f]{64}", str(route.get("fact_sha256") or ""))
                    else ""
                ),
                "observed_at": (float(observed_at) if isinstance(observed_at, (int, float)) else 0.0),
                "sequence": max(0, int(route.get("sequence") or 0)),
                "submitted_count": max(
                    0,
                    int(route.get("submitted_count") or 0),
                ),
            }
        return sanitized

    async def _wait_lifecycle_submission(self, *, after_count: int) -> int:
        deadline = time.monotonic() + LIFECYCLE_SUBMISSION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            lifecycle = (await self._route_diagnostics()).get("lifecycle", {})
            submitted_count = int(lifecycle.get("submitted_count") or 0)
            if submitted_count > after_count:
                return submitted_count - after_count
            await asyncio.sleep(0.1)
        return 0

    async def _wait_snapshot_phase(
        self,
        *,
        phases: set[str],
        timeout: float = 15.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await self._trigger_entry("get_status", {})
            payload = result.get("data")
            if not isinstance(payload, Mapping):
                payload = result
            runtime = payload.get("runtime")
            diagnostics = payload.get("diagnostics")
            snapshot = diagnostics.get("snapshot") if isinstance(diagnostics, Mapping) else None
            if (
                isinstance(runtime, Mapping)
                and runtime.get("monitor_running") is True
                and runtime.get("log_found") is True
                and int(runtime.get("lines_seen") or 0) > 0
                and isinstance(snapshot, Mapping)
                and str(snapshot.get("phase") or "") in phases
            ):
                return True
            await asyncio.sleep(0.1)
        return False

    def _monitor_unready_reason(self, status_payload: Mapping[str, Any]) -> str:
        payload = status_payload
        nested = status_payload.get("data")
        if isinstance(nested, Mapping):
            payload = nested
        settings = payload.get("settings")
        configured_log = str(settings.get("log_path") or "") if isinstance(settings, Mapping) else ""
        try:
            configured_matches = bool(configured_log) and Path(configured_log).resolve(
                strict=False
            ) == self.options.active_log.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            configured_matches = False
        if not configured_matches:
            return "checkpoint_log_path_mismatch"
        runtime = payload.get("runtime")
        if not isinstance(runtime, Mapping):
            return "checkpoint_monitor_status_unavailable"
        if runtime.get("monitor_running") is not True:
            return "checkpoint_monitor_stopped"
        if runtime.get("log_found") is not True:
            return "checkpoint_log_not_found"
        if int(runtime.get("lines_seen") or 0) <= 0:
            return "checkpoint_log_unread"
        if str(runtime.get("source_state") or "") != "watching":
            return "checkpoint_source_not_watching"
        if int(runtime.get("snapshot_revision") or 0) <= 0:
            return "checkpoint_snapshot_empty"
        diagnostics = payload.get("diagnostics")
        snapshot = diagnostics.get("snapshot") if isinstance(diagnostics, Mapping) else None
        if isinstance(snapshot, Mapping):
            if int(snapshot.get("game_number") or 0) <= 0:
                return "checkpoint_snapshot_inactive"
            if str(snapshot.get("phase") or "") in {"idle", "ended", "spectator"}:
                return "checkpoint_snapshot_inactive"
        log_health = diagnostics.get("log") if isinstance(diagnostics, Mapping) else None
        if isinstance(log_health, Mapping) and log_health.get("fresh") is not True:
            return "checkpoint_snapshot_stale"
        return "checkpoint_no_live_game_state"

    @staticmethod
    def _evidence_available(state: Mapping[str, Any], name: str) -> bool:
        evidence = state.get("evidence")
        if not isinstance(evidence, Mapping):
            return False
        item = evidence.get(name)
        return item is True or (isinstance(item, Mapping) and item.get("available") is True)

    @staticmethod
    def _answer_checklist(state: Mapping[str, Any]) -> Mapping[str, Any]:
        checklist = state.get("answer_checklist")
        return checklist if isinstance(checklist, Mapping) else {}

    @staticmethod
    def _answer_groups(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        groups = state.get("card_groups")
        return [item for item in groups if isinstance(item, Mapping)] if isinstance(groups, list) else []

    @classmethod
    def _ready_for_case(
        cls,
        case_id: str,
        turn: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> bool:
        if case_id not in CASE_IDS or turn.get("available") is not True:
            return False
        if turn.get("format") != "hearthstone_current_turn_v1":
            return False
        mode = str(turn.get("mode") or "")
        round_number = turn.get("round")
        if case_id == "constructed_round_v1":
            return (
                mode == "constructed"
                and str(turn.get("phase") or "") == "playing"
                and round_number == 11
                and turn.get("action_turn") == 21
            )
        if case_id == "constructed_opponent_v1":
            current = state.get("current") or {}
            groups = cls._answer_groups(state)
            return (
                mode == "constructed"
                and str(turn.get("phase") or "") == "playing"
                and round_number == 11
                and turn.get("action_turn") == 21
                and state.get("format") == "hearthstone_compact_v1"
                and state.get("available") is True
                and state.get("mode") == "constructed"
                and state.get("focus") == "opponent"
                and current.get("round") == 11
                and current.get("action_turn") == 21
                and state.get("area") == "opponent_board"
                and state.get("source_complete") is True
                and state.get("slot_count") == 2
                and state.get("group_count") == len(groups)
                and sum(int(group.get("count") or 0) for group in groups) == 2
                and len(list(state.get("required_card_ids") or [])) == 2
                and all(str(group.get("card_id") or "") for group in groups)
                and all(
                    str(card_id) in str(state.get("summary") or "") for card_id in state.get("required_card_ids") or []
                )
            )
        if case_id == "bg_shop_v1":
            current = state.get("current") or {}
            groups = cls._answer_groups(state)
            return (
                mode == "battlegrounds"
                and str(turn.get("phase") or "") == "recruit"
                and round_number == 2
                and state.get("available") is True
                and state.get("format") == "hearthstone_compact_v1"
                and state.get("mode") == "battlegrounds"
                and state.get("focus") == "shop"
                and current.get("round") == 2
                and current.get("phase") == "recruit"
                and state.get("area") == "shop"
                and state.get("source_complete") is True
                and state.get("slot_count") == 4
                and state.get("group_count") == len(groups)
                and sum(int(group.get("count") or 0) for group in groups) == 4
                and len(list(state.get("required_card_ids") or [])) == 4
                and all(
                    str(group.get("card_id") or "")
                    and str(group.get("card_type") or "")
                    and isinstance(group.get("actual_cost"), int)
                    and isinstance(group.get("golden"), bool)
                    and group.get("keywords_complete") is True
                    for group in groups
                )
                and any(
                    group.get("card_type")
                    in {
                        "BATTLEGROUND_SPELL",
                        "TAVERN_SPELL",
                        "SPELL",
                    }
                    and group.get("actual_cost") == 1
                    for group in groups
                )
                and any(
                    group.get("golden") is True and "圣盾" in list(group.get("current_keywords") or [])
                    for group in groups
                )
                and all(
                    str(card_id) in str(state.get("summary") or "") for card_id in state.get("required_card_ids") or []
                )
            )
        expected_cost = 6 if case_id == "bg_upgrade_blocked_v1" else 3
        current = state.get("current") or {}
        economy = state.get("economy") or {}
        expected_affordable = case_id == "bg_upgrade_affordable_v1"
        return (
            mode == "battlegrounds"
            and str(turn.get("phase") or "") == "recruit"
            and round_number == 3
            and state.get("available") is True
            and state.get("format") == "hearthstone_compact_v1"
            and state.get("mode") == "battlegrounds"
            and state.get("focus") == "economy"
            and current.get("round") == 3
            and current.get("phase") == "recruit"
            and economy.get("gold") == 5
            and economy.get("upgrade_actual_cost") == expected_cost
            and economy.get("can_upgrade") is expected_affordable
            and economy.get("remaining_after_upgrade") == (2 if expected_affordable else None)
            and economy.get("shortfall_for_upgrade") == (None if expected_affordable else 1)
            and str(expected_cost) in str(state.get("summary") or "")
        )

    @classmethod
    def _readiness_mismatches(
        cls,
        case_id: str,
        turn: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> tuple[str, ...]:
        reasons: list[str] = []

        def require(condition: bool, code: str) -> None:
            if not condition:
                reasons.append(code)

        require(turn.get("available") is True, "turn_unavailable")
        require(
            turn.get("format") == "hearthstone_current_turn_v1",
            "turn_format_mismatch",
        )
        expected_mode = "constructed" if case_id.startswith("constructed_") else "battlegrounds"
        require(str(turn.get("mode") or "") == expected_mode, "turn_mode_mismatch")
        if case_id.startswith("constructed_"):
            require(str(turn.get("phase") or "") == "playing", "turn_phase_mismatch")
            require(turn.get("round") == 11, "turn_round_mismatch")
            require(turn.get("action_turn") == 21, "action_turn_mismatch")
        else:
            expected_round = 2 if case_id == "bg_shop_v1" else 3
            require(str(turn.get("phase") or "") == "recruit", "turn_phase_mismatch")
            require(turn.get("round") == expected_round, "turn_round_mismatch")
        if case_id == "constructed_round_v1":
            return tuple(reasons)

        expected_focus = {
            "constructed_opponent_v1": "opponent",
            "bg_shop_v1": "shop",
            "bg_upgrade_blocked_v1": "economy",
            "bg_upgrade_affordable_v1": "economy",
        }[case_id]
        require(state.get("available") is True, "state_unavailable")
        require(state.get("format") == "hearthstone_compact_v1", "state_format_mismatch")
        require(state.get("mode") == expected_mode, "state_mode_mismatch")
        require(state.get("focus") == expected_focus, "state_focus_mismatch")
        checklist = cls._answer_checklist(state)
        require(
            checklist.get("authority") == "canonical_top_level_fields",
            "answer_checklist_missing",
        )
        current = state.get("current") or {}
        if case_id == "constructed_opponent_v1":
            groups = cls._answer_groups(state)
            require(current.get("round") == 11, "checklist_round_mismatch")
            require(
                current.get("action_turn") == 21,
                "checklist_action_turn_mismatch",
            )
            require(state.get("area") == "opponent_board", "opponent_board_area_mismatch")
            require(state.get("source_complete") is True, "opponent_board_incomplete")
            require(state.get("slot_count") == 2, "opponent_board_count_mismatch")
            require(state.get("group_count") == len(groups), "opponent_board_group_count_mismatch")
            require(
                sum(int(group.get("count") or 0) for group in groups) == 2,
                "opponent_board_group_count_mismatch",
            )
            require(
                all(str(group.get("card_id") or "") for group in groups),
                "opponent_board_identity_missing",
            )
            require(len(list(state.get("required_card_ids") or [])) == 2, "opponent_board_required_ids_mismatch")
        elif case_id == "bg_shop_v1":
            groups = cls._answer_groups(state)
            require(current.get("round") == 2, "checklist_round_mismatch")
            require(current.get("phase") == "recruit", "checklist_phase_mismatch")
            require(state.get("area") == "shop", "shop_area_mismatch")
            require(state.get("source_complete") is True, "shop_incomplete")
            require(state.get("slot_count") == 4, "shop_count_mismatch")
            require(state.get("group_count") == len(groups), "shop_group_count_mismatch")
            require(
                sum(int(group.get("count") or 0) for group in groups) == 4,
                "shop_group_count_mismatch",
            )
            require(
                all(
                    str(group.get("card_id") or "")
                    and str(group.get("card_type") or "")
                    and isinstance(group.get("actual_cost"), int)
                    and isinstance(group.get("golden"), bool)
                    and group.get("keywords_complete") is True
                    for group in groups
                ),
                "shop_card_fields_missing",
            )
            require(
                any(
                    group.get("card_type") in {"BATTLEGROUND_SPELL", "TAVERN_SPELL", "SPELL"}
                    and group.get("actual_cost") == 1
                    for group in groups
                ),
                "shop_spell_cost_missing",
            )
            require(
                any(
                    group.get("golden") is True and "圣盾" in list(group.get("current_keywords") or [])
                    for group in groups
                ),
                "shop_golden_shield_missing",
            )
        else:
            expected_cost = 6 if case_id == "bg_upgrade_blocked_v1" else 3
            expected_affordable = case_id == "bg_upgrade_affordable_v1"
            economy = state.get("economy") or {}
            require(current.get("round") == 3, "checklist_round_mismatch")
            require(current.get("phase") == "recruit", "checklist_phase_mismatch")
            require(economy.get("gold") == 5, "gold_mismatch")
            require(
                economy.get("upgrade_actual_cost") == expected_cost,
                "upgrade_cost_mismatch",
            )
            require(
                economy.get("can_upgrade") is expected_affordable,
                "upgrade_affordability_mismatch",
            )
            require(
                economy.get("remaining_after_upgrade") == (2 if expected_affordable else None),
                "upgrade_remaining_mismatch",
            )
            require(
                economy.get("shortfall_for_upgrade") == (None if expected_affordable else 1),
                "upgrade_shortfall_mismatch",
            )
        return tuple(dict.fromkeys(reasons))

    async def _wait_case_ready(self, case_id: str, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if set(self._registered_tools()) != set(TOOL_NAMES):
                self._last_ready_reason = "checkpoint_tools_unavailable"
                await asyncio.sleep(0.1)
                continue
            turn = await self._trigger("hearthstone_current_turn", {})
            arguments: dict[str, Any] = {"mode": "auto", "focus": "overview"}
            if case_id == "constructed_opponent_v1":
                arguments.update(mode="constructed", focus="opponent")
            elif case_id == "bg_shop_v1":
                arguments.update(mode="battlegrounds", focus="shop")
            elif case_id.startswith("bg_upgrade_"):
                arguments.update(mode="battlegrounds", focus="economy")
            state = await self._trigger("hearthstone_live_state", arguments)
            if self._ready_for_case(case_id, turn, state):
                self._last_ready_reason = "ready"
                self._last_ready_details = ()
                return True
            self._last_ready_details = self._readiness_mismatches(
                case_id,
                turn,
                state,
            )
            if turn.get("available") is not True:
                reason = str(turn.get("reason") or "")
                self._last_ready_reason = {
                    "configuration_reconciling": "checkpoint_configuration_reconciling",
                    "llm_data_sharing_not_authorized": "checkpoint_data_sharing_disabled",
                    "monitor_configuration_not_applied": "checkpoint_monitor_config_unapplied",
                    "no_live_game_state": "checkpoint_no_live_game_state",
                    "plugin_not_running": "checkpoint_plugin_not_running",
                    "state_refresh_in_progress": "checkpoint_state_refresh_in_progress",
                }.get(reason, "checkpoint_turn_unavailable")
                if reason == "no_live_game_state":
                    status_payload = await self._trigger_entry("get_status", {})
                    self._last_ready_reason = self._monitor_unready_reason(status_payload)
                if self._last_ready_reason in {
                    "checkpoint_data_sharing_disabled",
                    "checkpoint_log_path_mismatch",
                    "checkpoint_monitor_config_unapplied",
                    "checkpoint_plugin_not_running",
                }:
                    return False
            elif turn.get("format") != "hearthstone_current_turn_v1":
                self._last_ready_reason = "checkpoint_turn_format_mismatch"
            elif str(turn.get("mode") or "") not in {"constructed", "battlegrounds"}:
                self._last_ready_reason = "checkpoint_mode_unavailable"
            elif state.get("available") is not True and case_id != "constructed_round_v1":
                self._last_ready_reason = "checkpoint_state_unavailable"
            else:
                self._last_ready_reason = "checkpoint_state_mismatch"
            await asyncio.sleep(0.1)
        return False

    async def _tool_fact_expectation(self, case_id: str) -> str:
        question = CASE_QUESTIONS[case_id]
        tool_name = "hearthstone_current_turn" if case_id == "constructed_round_v1" else "hearthstone_live_state"
        tool_arguments = {} if tool_name == "hearthstone_current_turn" else {"query": question}
        tool_result = await self._trigger_entry(
            f"__llm_tool__{tool_name}",
            tool_arguments,
        )
        fact_sha256 = _canonical_fact_fingerprint(tool_result.get("_model_text") or tool_result.get("output"))
        if not fact_sha256:
            raise TimeoutError("checkpoint_fact_evidence_unavailable")
        return fact_sha256

    def _replace_active_log(self, relative_source: str) -> bool:
        try:
            source = (self.options.inbox_root / relative_source).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("invalid_checkpoint_copy") from exc
        if not _is_same_or_within(source, self.options.inbox_root) or not source.is_file():
            raise ValueError("invalid_checkpoint_copy")
        staging = self.options.active_log.with_suffix(".next")
        try:
            staging.unlink(missing_ok=True)
            shutil.copyfile(source, staging)
            os.replace(staging, self.options.active_log)
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        os.utime(self.options.active_log, None)
        return True

    def _append_active_log(self, relative_source: str) -> dict[str, int]:
        try:
            source = (self.options.inbox_root / relative_source).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("invalid_checkpoint_copy") from exc
        if not _is_same_or_within(source, self.options.inbox_root) or not source.is_file():
            raise ValueError("invalid_checkpoint_copy")
        active_size = self.options.active_log.stat().st_size
        source_size = source.stat().st_size
        if source_size <= active_size:
            raise ValueError("invalid_checkpoint_edge")
        with self.options.active_log.open("rb") as active, source.open("rb") as candidate:
            remaining = active_size
            while remaining:
                chunk = active.read(min(1024 * 1024, remaining))
                if not chunk or candidate.read(len(chunk)) != chunk:
                    raise ValueError("checkpoint_edge_prefix_mismatch")
                remaining -= len(chunk)
            with self.options.active_log.open("ab") as destination:
                shutil.copyfileobj(candidate, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        os.utime(self.options.active_log, None)
        return {
            "pre_bytes": active_size,
            "post_bytes": source_size,
            "appended_bytes": source_size - active_size,
        }

    async def prepare_edge(self, case_id: str, relative_source: str) -> dict[str, Any]:
        if case_id not in {"constructed_started_v1", "constructed_ended_v1"}:
            raise ValueError("unsupported_case")
        async with self._activation_lock:
            self._prepared_edge_case = ""
            lifecycle_before = (await self._route_diagnostics()).get("lifecycle", {})
            submitted_before = int(lifecycle_before.get("submitted_count") or 0)
            await asyncio.to_thread(self._replace_active_log, relative_source)
            pre_bytes = self.options.active_log.stat().st_size
            if pre_bytes <= 0:
                raise ValueError("invalid_checkpoint_edge")
            expected_phases = {"idle"} if case_id == "constructed_started_v1" else {"playing"}
            if not await self._wait_snapshot_phase(phases=expected_phases):
                raise TimeoutError("checkpoint_edge_prepare_failed")
            pre_submission_count = 0
            pre_stage = ""
            if case_id == "constructed_ended_v1":
                pre_submission_count = await self._wait_lifecycle_submission(
                    after_count=submitted_before,
                )
                lifecycle_after = (await self._route_diagnostics()).get(
                    "lifecycle",
                    {},
                )
                pre_stage = str(lifecycle_after.get("focus") or "")
                if pre_submission_count != 1 or pre_stage != "resumed":
                    raise TimeoutError("checkpoint_edge_prepare_failed")
            else:
                await asyncio.sleep(0.25)
                lifecycle_after = (await self._route_diagnostics()).get(
                    "lifecycle",
                    {},
                )
                if int(lifecycle_after.get("submitted_count") or 0) != submitted_before:
                    raise TimeoutError("checkpoint_edge_prepare_failed")
            self._prepared_edge_case = case_id
            return {
                "pre_submission_count": pre_submission_count,
                "pre_stage": pre_stage,
                "pre_bytes": pre_bytes,
            }

    async def activate_edge(
        self,
        case_id: str,
        relative_source: str,
    ) -> ActivationEvidence:
        expected_stage = {
            "constructed_started_v1": "started",
            "constructed_ended_v1": "ended",
        }.get(case_id)
        if expected_stage is None or self._prepared_edge_case != case_id:
            raise ValueError("checkpoint_edge_not_prepared")
        async with self._activation_lock:
            lifecycle_before = (await self._route_diagnostics()).get("lifecycle", {})
            submitted_before = int(lifecycle_before.get("submitted_count") or 0)
            try:
                append_evidence = await asyncio.to_thread(
                    self._append_active_log,
                    relative_source,
                )
                expected_phases = {"playing"} if expected_stage == "started" else {"ended"}
                if not await self._wait_snapshot_phase(phases=expected_phases):
                    raise TimeoutError("checkpoint_edge_transition_failed")
                submission_count = await self._wait_lifecycle_submission(
                    after_count=submitted_before,
                )
                lifecycle_after = (await self._route_diagnostics()).get(
                    "lifecycle",
                    {},
                )
                observed_stage = str(lifecycle_after.get("focus") or "")
                if submission_count != 1:
                    raise TimeoutError("checkpoint_lifecycle_submission_missing")
                if observed_stage != expected_stage:
                    raise TimeoutError("checkpoint_lifecycle_stage_mismatch")
                return ActivationEvidence(
                    lifecycle_submission_count=submission_count,
                    lifecycle_stage=observed_stage,
                    tool_fact_sha256="",
                    pre_bytes=append_evidence["pre_bytes"],
                    post_bytes=append_evidence["post_bytes"],
                    appended_bytes=append_evidence["appended_bytes"],
                )
            finally:
                self._prepared_edge_case = ""

    async def activate(
        self,
        case_id: str,
        relative_source: str,
        *,
        lane: str,
    ) -> ActivationEvidence:
        if case_id not in CASE_IDS:
            raise ValueError("unsupported_case")
        if lane not in {"lifecycle", "query"}:
            raise ValueError("unsupported_lane")
        async with self._activation_lock:
            lifecycle_before = (await self._route_diagnostics()).get("lifecycle", {})
            lifecycle_submitted_before = int(lifecycle_before.get("submitted_count") or 0)
            source_replaced = await asyncio.to_thread(
                self._replace_active_log,
                relative_source,
            )
            if not await self._wait_case_ready(case_id):
                raise TimeoutError(self._last_ready_reason)
            refresh = await self._trigger_entry("live_state_context_refresh", {})
            refresh_payload = refresh.get("data")
            if not isinstance(refresh_payload, Mapping):
                refresh_payload = refresh
            if refresh_payload.get("refreshed") is not True:
                raise TimeoutError("checkpoint_context_refresh_failed")
            if not await self._wait_case_ready(case_id, timeout=2.0):
                raise TimeoutError(self._last_ready_reason)
            tool_fact_sha256 = ""
            if lane == "query":
                tool_fact_sha256 = await self._tool_fact_expectation(case_id)
            lifecycle_submission_count = 0
            lifecycle_stage = ""
            if source_replaced:
                lifecycle_submission_count = await self._wait_lifecycle_submission(
                    after_count=lifecycle_submitted_before,
                )
                if lifecycle_submission_count != 1:
                    lifecycle_after = (await self._route_diagnostics()).get(
                        "lifecycle",
                        {},
                    )
                    diagnostic_details = [
                        f"lifecycle_{field}_{value}"
                        for field in ("status", "reason", "mode", "focus")
                        if (value := str(lifecycle_after.get(field) or "")) and value.replace("_", "").isalnum()
                    ]
                    self._last_ready_details = tuple(dict.fromkeys((*self._last_ready_details, *diagnostic_details)))
                    raise TimeoutError("checkpoint_lifecycle_submission_missing")
                lifecycle_after = (await self._route_diagnostics()).get(
                    "lifecycle",
                    {},
                )
                lifecycle_stage = str(lifecycle_after.get("focus") or "")
                if lifecycle_stage != "resumed":
                    raise TimeoutError("checkpoint_lifecycle_stage_mismatch")
            return ActivationEvidence(
                lifecycle_submission_count=lifecycle_submission_count,
                lifecycle_stage=lifecycle_stage,
                tool_fact_sha256=tool_fact_sha256,
            )

    async def stop_plugin(self) -> bool:
        if self._plugin_host() is None:
            return not self._registered_tools()
        from plugin.server.application.plugins import PluginLifecycleService

        result = await PluginLifecycleService().stop_plugin(PLUGIN_ID)
        return result.get("success") is True


def _build_app(controller: ProbeController, recorder: ToolCallRecorder) -> Any:
    from fastapi import Header, HTTPException
    from plugin.server.http_app import build_plugin_server_app

    app = build_plugin_server_app(title="N.E.K.O Hearthstone E2E Plugin Server")
    app.add_middleware(ToolCallRecorderMiddleware, recorder=recorder)

    def require_token(value: str | None) -> None:
        if not controller.authorized({TOKEN_HEADER: value or ""}):
            raise HTTPException(status_code=403, detail="forbidden")

    @app.get(f"{CONTROL_PREFIX}/health")
    async def e2e_health(x_hearthstone_e2e_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        host = controller._plugin_host()
        return {
            "status": "ok",
            "plugin_running": bool(host and getattr(host, "is_alive", lambda: False)()),
            "registered_tools": list(controller._registered_tools()),
        }

    @app.post(f"{CONTROL_PREFIX}/activate")
    async def e2e_activate(
        body: dict[str, Any],
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        try:
            activation = await controller.activate(
                str(body.get("case_id") or ""),
                str(body.get("checkpoint_copy") or ""),
                lane=str(body.get("lane") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail={
                    "reason": str(exc),
                    "readiness": list(controller._last_ready_details),
                },
            ) from exc
        return {
            "ok": True,
            "lifecycle_submitted": activation.lifecycle_submission_count > 0,
            "lifecycle_submission_count": activation.lifecycle_submission_count,
            "lifecycle_stage": activation.lifecycle_stage,
            "tool_fact_sha256": activation.tool_fact_sha256,
        }

    @app.post(f"{CONTROL_PREFIX}/prepare-edge")
    async def e2e_prepare_edge(
        body: dict[str, Any],
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        try:
            prepared = await controller.prepare_edge(
                str(body.get("case_id") or ""),
                str(body.get("checkpoint_copy") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        return {"ok": True, **prepared}

    @app.post(f"{CONTROL_PREFIX}/activate-edge")
    async def e2e_activate_edge(
        body: dict[str, Any],
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        try:
            activation = await controller.activate_edge(
                str(body.get("case_id") or ""),
                str(body.get("checkpoint_copy") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        return {
            "ok": True,
            "lifecycle_submitted": activation.lifecycle_submission_count > 0,
            "lifecycle_submission_count": activation.lifecycle_submission_count,
            "lifecycle_stage": activation.lifecycle_stage,
            "tool_fact_sha256": "",
            "pre_bytes": activation.pre_bytes,
            "post_bytes": activation.post_bytes,
            "appended_bytes": activation.appended_bytes,
        }

    @app.post(f"{CONTROL_PREFIX}/begin-epoch")
    async def e2e_begin_epoch(
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        return {"ok": True, "epoch": recorder.begin_epoch()}

    @app.get(f"{CONTROL_PREFIX}/routes")
    async def e2e_routes(
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        return {"ok": True, "routes": await controller._route_diagnostics()}

    @app.get(f"{CONTROL_PREFIX}/calls/{{epoch}}")
    async def e2e_calls(
        epoch: int,
        x_hearthstone_e2e_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        return {"ok": True, "epoch": epoch, "calls": recorder.calls_for(epoch)}

    @app.post(f"{CONTROL_PREFIX}/stop")
    async def e2e_stop(x_hearthstone_e2e_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        return {"ok": await controller.stop_plugin()}

    @app.post(f"{CONTROL_PREFIX}/shutdown")
    async def e2e_shutdown(x_hearthstone_e2e_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_hearthstone_e2e_token)
        if controller.server is not None:
            controller.server.should_exit = True
        return {"ok": True}

    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated official N.E.K.O plugin service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--inbox-root", type=Path, required=True)
    parser.add_argument("--active-log", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.host not in {"127.0.0.1", "localhost"} or not (1 <= args.port <= 65535):
        return 2
    if len(args.token) < 32:
        return 2

    _bootstrap_plugin_roots()
    inbox_root = args.inbox_root.resolve(strict=True)
    active_log = args.active_log.resolve(strict=True)
    if not inbox_root.is_dir() or not active_log.is_file():
        return 2

    import uvicorn

    recorder = ToolCallRecorder()
    controller = ProbeController(
        HostOptions(token=args.token, inbox_root=inbox_root, active_log=active_log),
        recorder,
    )
    app = _build_app(controller, recorder)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_config=None,
            access_log=False,
            log_level="warning",
        )
    )
    controller.server = server
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
