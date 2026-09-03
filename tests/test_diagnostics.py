from __future__ import annotations

import json

from hearthstone_companion_under_test.diagnostics import (
    DiagnosticTracker,
    canonical_fact_fingerprint,
)


def test_canonical_fact_fingerprint_ignores_transport_instructions() -> None:
    answer = "当前是第11回合。"

    plain = canonical_fact_fingerprint(answer)
    direct = canonical_fact_fingerprint(f"final_answer={answer}\nanswer_rule=private")
    facts = canonical_fact_fingerprint(f"facts[round]={answer}\ncapabilities=private")

    assert plain == direct == facts
    assert len(plain) == 64
    assert canonical_fact_fingerprint("") == ""


def test_diagnostic_tracker_keeps_only_bounded_stable_codes() -> None:
    tracker = DiagnosticTracker()

    tracker.record_tool_registration(
        {
            "healthy": False,
            "reason": "registration_pending",
            "missing": ["hearthstone_live_state", "hearthstone_current_turn"],
            "error_code": "TimeoutError",
        },
        observed_at=100.0,
    )
    tracker.record_route(
        "agent",
        status="rejected",
        reason="原始问题不应进入诊断",
        mode="constructed",
        focus="opponent",
        fact_sha256="a" * 64,
        observed_at=101.0,
    )

    snapshot = tracker.snapshot()
    encoded = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["tool_registration"] == {
        "status": "unhealthy",
        "reason": "registration_pending",
        "checked_at": 100.0,
        "missing": ("hearthstone_live_state", "hearthstone_current_turn"),
        "recovered_count": 0,
        "error_code": "TimeoutError",
    }
    assert snapshot["routes"]["agent"]["reason"] == "invalid_code"
    assert snapshot["routes"]["agent"]["sequence"] == 1
    assert snapshot["routes"]["agent"]["submitted_count"] == 0
    assert snapshot["routes"]["agent"]["fact_sha256"] == "a" * 64
    assert "原始问题" not in encoded


def test_diagnostic_tracker_snapshots_are_independent_copies() -> None:
    tracker = DiagnosticTracker()
    first = tracker.snapshot()
    first["routes"]["agent"]["status"] = "mutated"

    assert tracker.snapshot()["routes"]["agent"]["status"] == "never"


def test_transient_registration_skip_does_not_replace_last_health_result() -> None:
    tracker = DiagnosticTracker()
    tracker.record_tool_registration(
        {
            "healthy": False,
            "reason": "registration_pending",
            "missing": ["hearthstone_live_state"],
        },
        observed_at=100.0,
    )

    tracker.record_tool_registration(
        {"skipped": True, "reason": "retry_backoff"},
        observed_at=105.0,
    )

    diagnostic = tracker.snapshot()["tool_registration"]
    assert diagnostic["status"] == "unhealthy"
    assert diagnostic["reason"] == "registration_pending"
    assert diagnostic["checked_at"] == 100.0
