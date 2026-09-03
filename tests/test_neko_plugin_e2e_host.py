from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import neko_plugin_e2e_host as host
import pytest

TOKEN = "t" * 32


def _controller(tmp_path: Path) -> host.ProbeController:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    active_log = tmp_path / "runtime" / "Power.log"
    active_log.parent.mkdir()
    active_log.write_bytes(b"old")
    return host.ProbeController(
        host.HostOptions(token=TOKEN, inbox_root=inbox, active_log=active_log),
        host.ToolCallRecorder(),
    )


def _record(
    recorder: host.ToolCallRecorder,
    epoch: int,
    *,
    name: str = "hearthstone_live_state",
) -> None:
    recorder.record(
        epoch=epoch,
        name=name,
        call_id=f"call-{epoch}",
        argument_fields=("query",),
        started_at=1.0,
        completed_at=2.0,
        status_code=200,
        response={"is_error": False},
    )


def _turn(
    mode: str,
    round_number: int,
    *,
    phase: str,
    action_turn: int | None = None,
) -> dict[str, Any]:
    return {
        "format": "hearthstone_current_turn_v1",
        "available": True,
        "mode": mode,
        "phase": phase,
        "round": round_number,
        "action_turn": action_turn,
    }


def _state(
    mode: str,
    focus: str,
    serialized: str,
    *,
    evidence: str | None = None,
    checklist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = checklist or {}
    result: dict[str, Any] = {
        "summary": serialized,
        "format": "hearthstone_compact_v1",
        "available": True,
        "mode": mode,
        "focus": focus,
        "views": [{"focus": focus, "state": serialized}],
        "evidence": {},
        "answer_checklist": {
            "authority": "canonical_top_level_fields",
            "answer_policy": "cover_summary_and_every_card_group",
            "requested_focus": focus,
        },
    }
    current = source.get("current")
    if isinstance(current, dict):
        result["current"] = current
    economy = source.get("economy")
    if isinstance(economy, dict):
        result["economy"] = economy
        result["summary"] = f"当前金币=5，升本实际费用={economy.get('upgrade_actual_cost')}。"
    areas = source.get("areas")
    if isinstance(areas, dict) and areas:
        area_name, area = next(iter(areas.items()))
        groups = list(area.get("groups") or []) if isinstance(area, dict) else []
        canonical_groups = []
        for group in groups:
            canonical = dict(group)
            for old, new in (
                ("current_cost", "actual_cost"),
                ("premium", "golden"),
                ("active_keywords", "current_keywords"),
            ):
                if old in canonical:
                    canonical[new] = canonical.pop(old)
            canonical_groups.append(canonical)
        result.update(
            {
                "area": area_name,
                "source_complete": area.get("source_complete") is True,
                "slot_count": area.get("slot_count"),
                "group_count": area.get("group_count"),
                "required_card_ids": [
                    str(group.get("card_id") or "")
                    for group in canonical_groups
                    for _ in range(int(group.get("count") or 0))
                ],
                "card_groups": canonical_groups,
            }
        )
        result["summary"] = " ".join(result["required_card_ids"])
    if evidence is not None:
        result["evidence"] = {evidence: {"available": True}}
    return result


def _opponent_state(*, complete: bool = True) -> dict[str, Any]:
    groups = [
        {
            "ordinal": f"{index}/2",
            "positions": [index],
            "count": 1,
            "card_id": card_id,
            "attack": index * 2 - 1,
            "health": index * 2,
            "keywords_complete": True,
            "active_keywords": [],
        }
        for index, card_id in enumerate(("CARD_A", "CARD_B"), start=1)
    ]
    result = _state(
        "constructed",
        "opponent",
        (
            "HS_C;round=11;turn=21;active=opponent;hp=30+0;"
            f"q={1 if complete else 0};"
            "opponent_board[id/atk/hp/pos/kw/state]="
            "CARD_A/1/2/1/-/-,CARD_B/3/4/2/t/-"
        ),
        checklist={
            "authority": "canonical_final_field",
            "mode": "constructed",
            "requested_focus": "opponent",
            "current": {
                "round": 11,
                "action_turn": 21,
                "action_turn_is_not_round": True,
                "active_side": "opponent",
                "phase": "playing",
            },
            "areas": {
                "opponent_board": {
                    "source_complete": complete,
                    "delivery": "full" if complete else "missing_evidence",
                    "slot_count": 2 if complete else None,
                    "group_count": 2 if complete else None,
                    "groups": groups if complete else [],
                }
            },
        },
    )
    result["evidence"] = {"opponent_board_identities_complete": complete}
    return result


def _shop_state(*, include_fourth: bool = True) -> dict[str, Any]:
    cards = [
        "BG_A/M/3/1/2/1/1/d",
        "BG_SPELL/BS/1/?/?/1/0/-",
        "BG_B/M/3/3/4/2/0/t",
    ]
    if include_fourth:
        cards.append("BG_C/M/3/5/6/3/0/r")
    groups = [
        {
            "ordinal": "1/4",
            "positions": [1],
            "count": 1,
            "card_id": "BG_A",
            "card_type": "MINION",
            "current_cost": 3,
            "premium": True,
            "keywords_complete": True,
            "active_keywords": ["圣盾"],
        },
        {
            "ordinal": "2/4",
            "positions": [2],
            "count": 1,
            "card_id": "BG_SPELL",
            "card_type": "BATTLEGROUND_SPELL",
            "current_cost": 1,
            "premium": False,
            "keywords_complete": True,
            "active_keywords": [],
        },
        {
            "ordinal": "3/4",
            "positions": [3],
            "count": 1,
            "card_id": "BG_B",
            "card_type": "MINION",
            "current_cost": 3,
            "premium": False,
            "keywords_complete": True,
            "active_keywords": ["嘲讽"],
        },
    ]
    if include_fourth:
        groups.append(
            {
                "ordinal": "4/4",
                "positions": [4],
                "count": 1,
                "card_id": "BG_C",
                "card_type": "MINION",
                "current_cost": 3,
                "premium": False,
                "keywords_complete": True,
                "active_keywords": ["复生"],
            }
        )
    return _state(
        "battlegrounds",
        "shop",
        (
            "HS_BG;r=2;g=4/4;t=1;f=0;rf=1;up=5;q=1;K=d:shield;"
            "shop[id/type/cost/atk/hp/tier/golden/kw]=" + ",".join(cards)
        ),
        checklist={
            "authority": "canonical_final_field",
            "mode": "battlegrounds",
            "requested_focus": "shop",
            "current": {"round": 2, "phase": "recruit"},
            "areas": {
                "shop": {
                    "source_complete": True,
                    "delivery": "full",
                    "slot_count": len(groups),
                    "group_count": len(groups),
                    "groups": groups,
                }
            },
        },
    )


def _economy_state(upgrade_cost: int) -> dict[str, Any]:
    affordable = upgrade_cost <= 5
    return _state(
        "battlegrounds",
        "economy",
        f"HS_BG;r=3;phase=recruit;g=5/5;t=2;f=0;rf=1;up={upgrade_cost}",
        evidence="upgrade_affordability",
        checklist={
            "authority": "canonical_final_field",
            "mode": "battlegrounds",
            "requested_focus": "economy",
            "current": {"round": 3, "phase": "recruit"},
            "economy": {
                "source_complete": True,
                "gold": 5,
                "refresh_actual_cost": 1,
                "upgrade_actual_cost": upgrade_cost,
                "upgrade_evidence_complete": True,
                "can_upgrade": affordable,
                "remaining_after_upgrade": 5 - upgrade_cost if affordable else None,
                "shortfall_for_upgrade": upgrade_cost - 5 if not affordable else None,
                "remaining_status": ("applicable" if affordable else "not_applicable_insufficient_gold"),
            },
        },
    )


def test_checkpoint_copy_rejects_directory_escape(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"private")

    with pytest.raises(ValueError, match="invalid_checkpoint_copy"):
        controller._replace_active_log("../outside.log")
    with pytest.raises(ValueError, match="invalid_checkpoint_copy"):
        controller._replace_active_log(str(outside.resolve()))
    with pytest.raises(ValueError, match="invalid_checkpoint_copy"):
        controller._replace_active_log("missing.log")

    assert controller.options.active_log.read_bytes() == b"old"


def test_checkpoint_copy_accepts_only_inbox_file(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"new")

    assert controller._replace_active_log("checkpoint.log") is True

    assert controller.options.active_log.read_bytes() == b"new"


def test_identical_checkpoint_is_replayed_to_create_a_fresh_monitor_generation(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"same-prefix")
    assert controller._replace_active_log("checkpoint.log") is True

    assert controller._replace_active_log("checkpoint.log") is True
    assert controller.options.active_log.read_bytes() == b"same-prefix"


def test_cached_checkpoint_is_replaced_when_active_log_changed(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"expected-prefix")
    assert controller._replace_active_log("checkpoint.log") is True
    controller.options.active_log.write_bytes(b"unexpected")

    assert controller._replace_active_log("checkpoint.log") is True
    assert controller.options.active_log.read_bytes() == b"expected-prefix"


def test_lifecycle_edge_append_accepts_only_the_exact_active_prefix(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    prefix = b"first\r\nsecond\n"
    suffix = b"third\r\nfourth\n"
    controller.options.active_log.write_bytes(prefix)
    checkpoint = controller.options.inbox_root / "edge.log"
    checkpoint.write_bytes(prefix + suffix)

    assert controller._append_active_log(checkpoint.name) == {
        "pre_bytes": len(prefix),
        "post_bytes": len(prefix + suffix),
        "appended_bytes": len(suffix),
    }
    assert controller.options.active_log.read_bytes() == prefix + suffix


def test_lifecycle_edge_append_rejects_prefix_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    original = b"first\nexpected\n"
    controller.options.active_log.write_bytes(original)
    checkpoint = controller.options.inbox_root / "edge.log"
    checkpoint.write_bytes(b"first\nunexpected\npost\n")

    with pytest.raises(ValueError, match="checkpoint_edge_prefix_mismatch"):
        controller._append_active_log(checkpoint.name)

    assert controller.options.active_log.read_bytes() == original


def test_started_edge_prepare_requires_zero_lifecycle_submissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "started-pre.log"
    checkpoint.write_bytes(b"pre\n")
    phase_requests: list[set[str]] = []
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 7, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 7, "focus": "resumed"}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del timeout
        phase_requests.append(phases)
        return True

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(host.asyncio, "sleep", no_sleep)

    result = asyncio.run(controller.prepare_edge("constructed_started_v1", checkpoint.name))

    assert result == {
        "pre_submission_count": 0,
        "pre_stage": "",
        "pre_bytes": len(b"pre\n"),
    }
    assert phase_requests == [{"idle"}]


def test_started_edge_prepare_rejects_any_lifecycle_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "started-pre.log"
    checkpoint.write_bytes(b"pre\n")
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 7, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 8, "focus": "started"}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del timeout
        assert phases == {"idle"}
        return True

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(host.asyncio, "sleep", no_sleep)

    with pytest.raises(TimeoutError, match="checkpoint_edge_prepare_failed"):
        asyncio.run(controller.prepare_edge("constructed_started_v1", checkpoint.name))


def test_ended_edge_prepare_requires_one_resumed_submission_at_playing_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "ended-pre.log"
    checkpoint.write_bytes(b"pre\n")
    phase_requests: list[set[str]] = []
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 3, "focus": ""}},
            {"lifecycle": {"submitted_count": 4, "focus": "resumed"}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del timeout
        phase_requests.append(phases)
        return True

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 3
        return 1

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    result = asyncio.run(controller.prepare_edge("constructed_ended_v1", checkpoint.name))

    assert result == {
        "pre_submission_count": 1,
        "pre_stage": "resumed",
        "pre_bytes": len(b"pre\n"),
    }
    assert phase_requests == [{"playing"}]


def test_ended_edge_prepare_rejects_batched_resumed_submissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "ended-pre.log"
    checkpoint.write_bytes(b"pre\n")
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 3, "focus": ""}},
            {"lifecycle": {"submitted_count": 5, "focus": "resumed"}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del timeout
        assert phases == {"playing"}
        return True

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 3
        return 2

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    with pytest.raises(TimeoutError, match="checkpoint_edge_prepare_failed"):
        asyncio.run(controller.prepare_edge("constructed_ended_v1", checkpoint.name))


@pytest.mark.parametrize(
    ("case_id", "stage", "phase"),
    (
        ("constructed_started_v1", "started", "playing"),
        ("constructed_ended_v1", "ended", "ended"),
    ),
)
def test_lifecycle_edge_activation_requires_the_exact_post_stage_and_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    stage: str,
    phase: str,
) -> None:
    controller = _controller(tmp_path)
    prefix = b"pre\n"
    controller.options.active_log.write_bytes(prefix)
    checkpoint = controller.options.inbox_root / "post.log"
    checkpoint.write_bytes(prefix + b"post\n")
    controller._prepared_edge_case = case_id
    phase_requests: list[set[str]] = []
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 5, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 6, "focus": stage}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del timeout
        phase_requests.append(phases)
        return True

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 5
        return 1

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    activation = asyncio.run(controller.activate_edge(case_id, checkpoint.name))

    assert activation == host.ActivationEvidence(
        1,
        stage,
        "",
        pre_bytes=len(prefix),
        post_bytes=len(prefix + b"post\n"),
        appended_bytes=len(b"post\n"),
    )
    assert phase_requests == [{phase}]
    assert controller.options.active_log.read_bytes() == prefix + b"post\n"


@pytest.mark.parametrize(
    ("case_id", "stage"),
    (
        ("constructed_started_v1", "started"),
        ("constructed_ended_v1", "ended"),
    ),
)
def test_lifecycle_edge_activation_rejects_multiple_stage_submissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    stage: str,
) -> None:
    controller = _controller(tmp_path)
    prefix = b"pre\n"
    controller.options.active_log.write_bytes(prefix)
    checkpoint = controller.options.inbox_root / "post.log"
    checkpoint.write_bytes(prefix + b"post\n")
    controller._prepared_edge_case = case_id
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 5, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 7, "focus": stage}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del phases, timeout
        return True

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 5
        return 2

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    with pytest.raises(TimeoutError):
        asyncio.run(controller.activate_edge(case_id, checkpoint.name))


@pytest.mark.parametrize(
    ("case_id", "wrong_stage"),
    (
        ("constructed_started_v1", "resumed"),
        ("constructed_ended_v1", "started"),
    ),
)
def test_lifecycle_edge_activation_rejects_an_imprecise_post_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    wrong_stage: str,
) -> None:
    controller = _controller(tmp_path)
    prefix = b"pre\n"
    controller.options.active_log.write_bytes(prefix)
    checkpoint = controller.options.inbox_root / "post.log"
    checkpoint.write_bytes(prefix + b"post\n")
    controller._prepared_edge_case = case_id
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 5, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 6, "focus": wrong_stage}},
        )
    )

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_phase(*, phases: set[str], timeout: float = 15.0) -> bool:
        del phases, timeout
        return True

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 5
        return 1

    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_snapshot_phase", wait_phase)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    with pytest.raises(TimeoutError, match="checkpoint_lifecycle_stage_mismatch"):
        asyncio.run(controller.activate_edge(case_id, checkpoint.name))


def test_activation_refreshes_passive_context_before_opening_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"checkpoint")
    readiness_checks: list[str] = []
    triggers: list[tuple[str, dict[str, Any]]] = []

    async def ready(case_id: str, timeout: float = 15.0) -> bool:
        del timeout
        readiness_checks.append(case_id)
        return True

    async def trigger(entry_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        triggers.append((entry_id, arguments))
        return {"refreshed": True}

    async def routes() -> dict[str, dict[str, object]]:
        return {
            "lifecycle": {
                "observed_at": 10.0,
                "submitted_count": 4,
                "focus": "resumed",
            }
        }

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 4
        return 1

    async def tool_fact_expectation(_case_id: str) -> str:
        return "a" * 64

    monkeypatch.setattr(controller, "_wait_case_ready", ready)
    monkeypatch.setattr(controller, "_trigger_entry", trigger)
    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)
    monkeypatch.setattr(controller, "_tool_fact_expectation", tool_fact_expectation)

    activation = asyncio.run(controller.activate("bg_shop_v1", checkpoint.name, lane="query"))

    assert activation == host.ActivationEvidence(1, "resumed", "a" * 64)
    assert controller.recorder.current_epoch() == 0
    assert readiness_checks == ["bg_shop_v1", "bg_shop_v1"]
    assert triggers == [("live_state_context_refresh", {})]


def test_normal_checkpoint_activation_rejects_multiple_resumed_submissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"checkpoint")
    route_results = iter(
        (
            {"lifecycle": {"submitted_count": 4, "focus": "resumed"}},
            {"lifecycle": {"submitted_count": 6, "focus": "resumed"}},
        )
    )

    async def ready(_case_id: str, timeout: float = 15.0) -> bool:
        del timeout
        return True

    async def trigger(_entry_id: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"refreshed": True}

    async def routes() -> dict[str, dict[str, object]]:
        return next(route_results)

    async def wait_lifecycle(*, after_count: int) -> int:
        assert after_count == 4
        return 2

    monkeypatch.setattr(controller, "_wait_case_ready", ready)
    monkeypatch.setattr(controller, "_trigger_entry", trigger)
    monkeypatch.setattr(controller, "_route_diagnostics", routes)
    monkeypatch.setattr(controller, "_wait_lifecycle_submission", wait_lifecycle)

    with pytest.raises(TimeoutError):
        asyncio.run(controller.activate("bg_shop_v1", checkpoint.name, lane="lifecycle"))


def test_activation_fails_closed_when_passive_context_refresh_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"checkpoint")

    async def ready(_case_id: str, timeout: float = 15.0) -> bool:
        del timeout
        return True

    async def trigger(_entry_id: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(controller, "_wait_case_ready", ready)
    monkeypatch.setattr(controller, "_trigger_entry", trigger)

    with pytest.raises(TimeoutError, match="checkpoint_context_refresh_failed"):
        asyncio.run(controller.activate("bg_shop_v1", checkpoint.name, lane="query"))


def test_failed_log_replace_removes_staging_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    checkpoint = controller.options.inbox_root / "checkpoint.log"
    checkpoint.write_bytes(b"new")
    staging = controller.options.active_log.with_suffix(".next")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(host.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        controller._replace_active_log("checkpoint.log")

    assert not staging.exists()
    assert controller.options.active_log.read_bytes() == b"old"


def test_tool_call_recorder_preserves_and_separates_epochs() -> None:
    recorder = host.ToolCallRecorder()
    first = recorder.begin_epoch()
    _record(recorder, first, name="hearthstone_current_turn")
    second = recorder.begin_epoch()
    _record(recorder, second)

    assert [call["name"] for call in recorder.calls_for(first)] == ["hearthstone_current_turn"]
    assert [call["name"] for call in recorder.calls_for(second)] == ["hearthstone_live_state"]


def test_tool_call_recorder_fingerprints_only_canonical_fact_line() -> None:
    recorder = host.ToolCallRecorder()
    epoch = recorder.begin_epoch()
    recorder.record(
        epoch=epoch,
        name="hearthstone_live_state",
        call_id="call-1",
        argument_fields=("query",),
        started_at=1.0,
        completed_at=2.0,
        status_code=200,
        response={
            "is_error": False,
            "output": "final_answer=当前有5金币。\nanswer_rule=private",
        },
    )

    contract = recorder.calls_for(epoch)[0]["output_contract"]
    assert contract["fact_sha256"] == host._canonical_fact_fingerprint("当前有5金币。")
    assert contract["fact_chars"] > 0
    assert "private" not in repr(contract)


def test_middleware_freezes_epoch_when_callback_starts() -> None:
    async def exercise() -> tuple[int, int, host.ToolCallRecorder]:
        recorder = host.ToolCallRecorder()
        first = recorder.begin_epoch()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            del scope
            await receive()
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200})
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"is_error":false}',
                }
            )

        middleware = host.ToolCallRecorderMiddleware(app, recorder)
        received = False

        async def receive() -> dict[str, Any]:
            nonlocal received
            assert not received
            received = True
            return {
                "type": "http.request",
                "body": b'{"arguments":{},"call_id":"call-frozen"}',
                "more_body": False,
            }

        async def send(_message: dict[str, Any]) -> None:
            return None

        task = asyncio.create_task(
            middleware(
                {
                    "type": "http",
                    "path": ("/api/llm-tools/callback/hearthstone_companion/hearthstone_current_turn"),
                },
                receive,
                send,
            )
        )
        await entered.wait()
        second = recorder.begin_epoch()
        release.set()
        await task
        return first, second, recorder

    first, second, recorder = asyncio.run(exercise())

    assert len(recorder.calls_for(first)) == 1
    assert recorder.calls_for(second) == []


def test_middleware_records_only_allowlisted_shape() -> None:
    async def exercise() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        recorder = host.ToolCallRecorder()
        epoch = recorder.begin_epoch()

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            del scope
            await receive()
            await send({"type": "http.response.start", "status": 200})
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"is_error": False, "private_response": "response-secret"}).encode(),
                }
            )

        middleware = host.ToolCallRecorderMiddleware(app, recorder)

        async def invoke(name: str) -> None:
            pending = [
                {
                    "type": "http.request",
                    "body": json.dumps(
                        {
                            "arguments": {
                                "query": "question-secret",
                                "private-field-secret": "argument-secret",
                            }
                        }
                    ).encode(),
                    "more_body": False,
                }
            ]

            async def receive() -> dict[str, Any]:
                return pending.pop()

            async def send(_message: dict[str, Any]) -> None:
                return None

            await middleware(
                {
                    "type": "http",
                    "path": f"/api/llm-tools/callback/{host.PLUGIN_ID}/{name}",
                },
                receive,
                send,
            )

        await invoke("hearthstone_live_state")
        allowed = recorder.calls_for(epoch)
        await invoke("private-path-secret")
        return allowed, recorder.calls_for(epoch)

    allowed, after_unknown = asyncio.run(exercise())

    assert allowed == after_unknown
    assert allowed[0]["argument_fields"] == ["query"]
    serialized = repr(allowed)
    for secret in (
        "question-secret",
        "argument-secret",
        "private-field-secret",
        "response-secret",
        "private-path-secret",
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    ("case_id", "turn", "state"),
    [
        (
            "constructed_round_v1",
            _turn("constructed", 11, phase="playing", action_turn=21),
            {},
        ),
        (
            "constructed_opponent_v1",
            _turn("constructed", 11, phase="playing", action_turn=21),
            _opponent_state(),
        ),
        (
            "bg_shop_v1",
            _turn("battlegrounds", 2, phase="recruit"),
            _shop_state(),
        ),
        (
            "bg_upgrade_blocked_v1",
            _turn("battlegrounds", 3, phase="recruit"),
            _economy_state(6),
        ),
        (
            "bg_upgrade_affordable_v1",
            _turn("battlegrounds", 3, phase="recruit"),
            _economy_state(3),
        ),
    ],
)
def test_all_five_checkpoint_gates_accept_exact_facts(
    case_id: str,
    turn: dict[str, Any],
    state: dict[str, Any],
) -> None:
    assert host.ProbeController._ready_for_case(case_id, turn, state)


@pytest.mark.parametrize(
    ("case_id", "turn", "state"),
    [
        (
            "constructed_round_v1",
            _turn("constructed", 11, phase="playing", action_turn=20),
            {},
        ),
        (
            "constructed_opponent_v1",
            _turn("constructed", 11, phase="playing", action_turn=21),
            _opponent_state(complete=False),
        ),
        (
            "bg_shop_v1",
            _turn("battlegrounds", 2, phase="recruit"),
            _shop_state(include_fourth=False),
        ),
        (
            "bg_upgrade_blocked_v1",
            _turn("battlegrounds", 3, phase="recruit"),
            _economy_state(3),
        ),
        (
            "bg_upgrade_affordable_v1",
            _turn("battlegrounds", 3, phase="recruit"),
            _economy_state(6),
        ),
    ],
)
def test_checkpoint_gates_reject_wrong_or_incomplete_facts(
    case_id: str,
    turn: dict[str, Any],
    state: dict[str, Any],
) -> None:
    assert not host.ProbeController._ready_for_case(case_id, turn, state)


def test_readiness_mismatch_reports_only_stable_fact_codes() -> None:
    reasons = host.ProbeController._readiness_mismatches(
        "constructed_opponent_v1",
        _turn("constructed", 11, phase="playing", action_turn=21),
        _opponent_state(complete=False),
    )

    assert "opponent_board_incomplete" in reasons
    assert all(reason.replace("_", "").isalnum() for reason in reasons)


def test_evidence_gate_accepts_constructed_boolean_and_battlegrounds_object() -> None:
    assert host.ProbeController._evidence_available(
        {"evidence": {"opponent_board_identities_complete": True}},
        "opponent_board_identities_complete",
    )
    assert host.ProbeController._evidence_available(
        {"evidence": {"upgrade_affordability": {"available": True}}},
        "upgrade_affordability",
    )
    assert not host.ProbeController._evidence_available(
        {"evidence": {"opponent_board_identities_complete": False}},
        "opponent_board_identities_complete",
    )


def test_constructed_opponent_readiness_queries_opponent_focus(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []
    controller._registered_tools = lambda: host.TOOL_NAMES  # type: ignore[method-assign]

    async def trigger(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "hearthstone_current_turn":
            return _turn("constructed", 11, phase="playing", action_turn=21)
        return _opponent_state()

    controller._trigger = trigger  # type: ignore[method-assign]

    assert asyncio.run(controller._wait_case_ready("constructed_opponent_v1", timeout=0.1))
    assert calls[-1] == (
        "hearthstone_live_state",
        {"mode": "constructed", "focus": "opponent"},
    )


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ({}, "checkpoint_monitor_stopped"),
        ({"monitor_running": True}, "checkpoint_log_not_found"),
        (
            {"monitor_running": True, "log_found": True},
            "checkpoint_log_unread",
        ),
        (
            {"monitor_running": True, "log_found": True, "lines_seen": 1},
            "checkpoint_source_not_watching",
        ),
        (
            {
                "monitor_running": True,
                "log_found": True,
                "lines_seen": 1,
                "source_state": "watching",
            },
            "checkpoint_snapshot_empty",
        ),
        (
            {
                "monitor_running": True,
                "log_found": True,
                "lines_seen": 1,
                "source_state": "watching",
                "snapshot_revision": 1,
            },
            "checkpoint_no_live_game_state",
        ),
    ],
)
def test_monitor_unready_reason_is_sanitized_and_specific(
    tmp_path: Path,
    runtime: dict[str, Any],
    expected: str,
) -> None:
    controller = _controller(tmp_path)
    assert (
        controller._monitor_unready_reason(
            {
                "data": {
                    "settings": {"log_path": str(controller.options.active_log)},
                    "runtime": runtime,
                    "game": {"private": "not inspected"},
                }
            }
        )
        == expected
    )


def test_monitor_unready_reason_rejects_different_configured_log(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    assert (
        controller._monitor_unready_reason(
            {
                "settings": {"log_path": str(tmp_path / "different.log")},
                "runtime": {"monitor_running": True},
            }
        )
        == "checkpoint_log_path_mismatch"
    )


def test_authorization_uses_constant_time_digest_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    comparisons: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return left == right

    monkeypatch.setattr(host.hmac, "compare_digest", compare)

    assert controller.authorized({host.TOKEN_HEADER: TOKEN})
    assert not controller.authorized({host.TOKEN_HEADER: "wrong"})
    assert not controller.authorized({})
    assert len(comparisons) == 2


def test_every_control_route_rejects_missing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        def __init__(self, *, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail

    class FakeApp:
        def __init__(self) -> None:
            self.routes: dict[tuple[str, str], Any] = {}

        def add_middleware(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def get(self, path: str) -> Any:
            return self._route("GET", path)

        def post(self, path: str) -> Any:
            return self._route("POST", path)

        def _route(self, method: str, path: str) -> Any:
            def decorate(function: Any) -> Any:
                self.routes[(method, path)] = function
                return function

            return decorate

    app = FakeApp()
    fastapi = types.ModuleType("fastapi")
    fastapi.Header = lambda default=None: default
    fastapi.HTTPException = FakeHTTPException
    plugin = types.ModuleType("plugin")
    plugin.__path__ = []
    server = types.ModuleType("plugin.server")
    server.__path__ = []
    http_app = types.ModuleType("plugin.server.http_app")
    http_app.build_plugin_server_app = lambda **_kwargs: app
    plugin.server = server
    server.http_app = http_app
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "plugin", plugin)
    monkeypatch.setitem(sys.modules, "plugin.server", server)
    monkeypatch.setitem(sys.modules, "plugin.server.http_app", http_app)

    controller = _controller(tmp_path)
    host._build_app(controller, controller.recorder)
    invocations = (
        (app.routes[("GET", f"{host.CONTROL_PREFIX}/health")], (None,)),
        (app.routes[("POST", f"{host.CONTROL_PREFIX}/activate")], ({}, None)),
        (app.routes[("POST", f"{host.CONTROL_PREFIX}/begin-epoch")], (None,)),
        (app.routes[("GET", f"{host.CONTROL_PREFIX}/routes")], (None,)),
        (
            app.routes[("GET", f"{host.CONTROL_PREFIX}/calls/{{epoch}}")],
            (1, None),
        ),
        (app.routes[("POST", f"{host.CONTROL_PREFIX}/stop")], (None,)),
        (app.routes[("POST", f"{host.CONTROL_PREFIX}/shutdown")], (None,)),
    )

    async def exercise() -> None:
        for route, arguments in invocations:
            with pytest.raises(FakeHTTPException) as caught:
                await route(*arguments)
            assert caught.value.status_code == 403
            assert caught.value.detail == "forbidden"

    asyncio.run(exercise())
