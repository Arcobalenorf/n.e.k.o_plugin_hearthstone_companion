from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import pytest
from hearthstone_companion_under_test.delivery import (
    LiveStatePublisher,
    QueryLedger,
    semantic_snapshot_fingerprint,
)


@dataclass(frozen=True)
class FakeSnapshot:
    game_number: int
    payload: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return self.payload


def _publisher(calls: list[dict[str, Any]], **kwargs: Any) -> LiveStatePublisher:
    return LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=lambda *_args, **_kwargs: (("core", "state"),),
        logger=None,
        max_prompt_bytes=900,
        **kwargs,
    )


def test_semantic_fingerprint_ignores_collection_bookkeeping() -> None:
    first = FakeSnapshot(
        1,
        {
            "mode": "battlegrounds",
            "shop": [{"card_id": "A", "current_cost": 3}],
            "area": {"revision": 4, "observed_at": 100.0, "complete": True},
        },
    )
    second = FakeSnapshot(
        1,
        {
            "mode": "battlegrounds",
            "shop": [{"card_id": "A", "current_cost": 3}],
            "area": {"revision": 9, "observed_at": 200.0, "complete": True},
        },
    )

    assert semantic_snapshot_fingerprint(first) == semantic_snapshot_fingerprint(second)


def test_semantic_fingerprint_changes_for_public_game_state() -> None:
    first = FakeSnapshot(1, {"shop": [{"card_id": "A", "current_cost": 3}]})
    second = FakeSnapshot(1, {"shop": [{"card_id": "A", "current_cost": 2}]})

    assert semantic_snapshot_fingerprint(first) != semantic_snapshot_fingerprint(second)


def test_publisher_deduplicates_targeted_state_until_lease_expires() -> None:
    calls: list[dict[str, Any]] = []
    snapshot = FakeSnapshot(1, {"mode": "constructed", "turn": 3})
    publisher = _publisher(calls, refresh_seconds=30.0)

    assert publisher.publish(snapshot, target="role-a", now=10.0)
    assert publisher.publish(snapshot, target="role-a", now=39.9)
    assert len(calls) == 1
    assert publisher.publish(snapshot, target="role-a", now=40.0)
    assert len(calls) == 2


def test_publisher_submits_unresolved_passive_state_with_short_lease() -> None:
    calls: list[dict[str, Any]] = []
    snapshot = FakeSnapshot(1, {"turn": 3})
    publisher = _publisher(calls, unresolved_refresh_seconds=1.0)

    assert publisher.publish(snapshot, target="", now=10.0)
    assert publisher.cursor is not None
    assert publisher.cursor.target == ""
    assert calls[0]["ai_behavior"] == "read"
    assert "target_lanlan" not in calls[0]
    assert calls[0]["coalesce_key"] == "hearthstone:live-state:active-session:core"
    assert calls[0]["metadata"]["routing_scope"] == "active_session_fallback"

    assert publisher.publish(snapshot, target="", now=10.9)
    assert len(calls) == 1
    assert publisher.publish(snapshot, target="", now=11.0)
    assert len(calls) == 2


def test_publisher_restores_and_expires_unresolved_cursor() -> None:
    calls: list[dict[str, Any]] = []
    publisher = _publisher(calls)
    publisher.restore(
        target="",
        game_number=1,
        segments=("core",),
        snapshot=FakeSnapshot(1, {"turn": 3}),
    )

    assert publisher.cursor is not None
    assert publisher.cursor.target == ""
    assert publisher.expire(reason="test")
    assert publisher.cursor is None
    assert calls[0]["metadata"]["context_expired"] is True
    assert calls[0]["coalesce_key"] == "hearthstone:live-state:active-session:core"
    assert "target_lanlan" not in calls[0]


def test_publisher_migrates_between_unresolved_and_targeted_routes() -> None:
    calls: list[dict[str, Any]] = []
    snapshot = FakeSnapshot(1, {"turn": 3})
    publisher = _publisher(calls)

    assert publisher.publish(snapshot, target="", now=10.0)
    assert publisher.publish(snapshot, target="role-a", now=10.1)
    assert [call["metadata"]["context_expired"] for call in calls] == [
        False,
        True,
        False,
    ]
    assert "target_lanlan" not in calls[0]
    assert "target_lanlan" not in calls[1]
    assert calls[2]["target_lanlan"] == "role-a"

    assert publisher.publish(snapshot, target="", now=10.2)
    assert calls[-2]["metadata"]["context_expired"] is True
    assert calls[-2]["target_lanlan"] == "role-a"
    assert calls[-1]["metadata"]["context_expired"] is False
    assert "target_lanlan" not in calls[-1]


def test_unresolved_submission_rejection_does_not_advance_cursor() -> None:
    calls: list[dict[str, Any]] = []
    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": False},
        build_segments=lambda *_args, **_kwargs: (("core", "state"),),
        logger=None,
        max_prompt_bytes=900,
    )

    assert not publisher.publish(FakeSnapshot(1, {"turn": 3}), target="")
    assert publisher.cursor is None
    assert len(calls) == 1


def test_partial_update_cleanup_does_not_leave_old_cursor_deduplicated() -> None:
    outcomes = iter([True, True, True, False, True, True, True, True])
    calls: list[dict[str, Any]] = []

    def push(**kwargs: Any) -> dict[str, bool]:
        calls.append(kwargs)
        return {"submitted": next(outcomes)}

    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})
    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=lambda snapshot, **_kwargs: (
            ("core", f"turn={snapshot.payload['turn']}"),
            ("board", f"board-turn={snapshot.payload['turn']}"),
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert publisher.publish(old, target="role-a", now=12.0)
    assert calls[-2]["parts"][0]["text"] == "turn=3"


def test_first_segment_update_rejection_tombstones_old_context() -> None:
    outcomes = iter([True, False, True, True])
    calls: list[dict[str, Any]] = []

    def push(**kwargs: Any) -> dict[str, bool]:
        calls.append(kwargs)
        return {"submitted": next(outcomes)}

    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})
    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=lambda snapshot, **_kwargs: (
            ("core", f"turn={snapshot.payload['turn']}"),
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True
    assert publisher.publish(old, target="role-a", now=12.0)


def test_segment_shrink_failure_clears_retained_segment_after_tombstone() -> None:
    outcomes = iter([True, True, True, False, True])
    calls: list[dict[str, Any]] = []

    def push(**kwargs: Any) -> dict[str, bool]:
        calls.append(kwargs)
        return {"submitted": next(outcomes)}

    old = FakeSnapshot(1, {"turn": 3, "shop": ["A"]})
    updated = FakeSnapshot(1, {"turn": 4, "shop": []})

    def segments(snapshot: FakeSnapshot, **_kwargs: Any) -> tuple[tuple[str, str], ...]:
        core = (("core", f"turn={snapshot.payload['turn']}"),)
        return (*core, ("shop", "shop=A")) if snapshot.payload["shop"] else core

    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=segments,
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True
    assert calls[-1]["metadata"]["segment"] == "core"


def test_partial_publish_cleanup_failure_is_retried_immediately() -> None:
    outcomes = iter([True, False, False, True, True, True, True, True])
    calls: list[dict[str, Any]] = []

    def push(**kwargs: Any) -> dict[str, bool]:
        calls.append(kwargs)
        return {"submitted": next(outcomes)}

    snapshot = FakeSnapshot(1, {"mode": "battlegrounds", "round": 3})
    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=lambda *_args, **_kwargs: (
            ("core", "core-state"),
            ("shop", "shop-state"),
        ),
        logger=None,
        max_prompt_bytes=900,
        refresh_seconds=30.0,
    )

    assert not publisher.publish(snapshot, target="role-a", now=10.0)
    assert publisher.cursor is not None
    assert publisher.cursor.complete is False
    assert publisher.publish(snapshot, target="role-a", now=10.1)
    assert publisher.cursor is not None
    assert publisher.cursor.complete is True
    assert len(calls) == 8


def test_query_ledger_requires_role_scope_for_observe_and_claim() -> None:
    ledger = QueryLedger()

    with pytest.raises(ValueError, match="non-empty text and target"):
        ledger.observe("现在第几回合", target="", source_timestamp=100.0)
    assert ledger.claim(owner="tool", text="现在第几回合", target="") is None


def test_role_scoped_agent_claim_correlates_with_late_memory() -> None:
    ledger = QueryLedger()
    provisional = ledger.claim(
        owner="agent",
        text="现在第几回合",
        target="role-a",
        now=105.0,
    )
    assert provisional is not None

    observed = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        intent="overview",
        now=105.1,
    )

    assert observed is provisional
    assert observed.claimed_by == "agent"
    assert observed.source_timestamp == 100.0
    assert ledger.pending(now=106.0, min_age=0.0) == ()


def test_roleless_tool_cannot_change_another_roles_pending_query() -> None:
    ledger = QueryLedger()
    role_a = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )

    assert ledger.claim(owner="tool", text=role_a.text, target="", now=101.0) is None
    assert role_a.claimed_by == ""
    assert ledger.pending(now=102.0, min_age=1.0) == (role_a,)


def test_claims_are_isolated_by_role_and_exact_question() -> None:
    ledger = QueryLedger()
    role_a = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    role_b = ledger.observe(
        "现在第几回合", target="role-b", source_timestamp=100.1, now=100.1
    )

    wrong_text = ledger.claim(
        owner="agent", text="商店里有什么", target="role-a", now=101.0
    )
    assert wrong_text is not None
    assert wrong_text is not role_a
    assert ledger.claim(
        owner="agent", text=role_b.text, target=role_b.target, now=101.1
    ) is role_b
    assert role_a.claimed_by == ""


def test_memory_replay_preserves_committed_claim_identity() -> None:
    ledger = QueryLedger()
    observed = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    fallback = ledger.claim(
        owner="fallback", text=observed.text, target=observed.target, now=101.0
    )
    assert fallback is observed
    assert ledger.commit(
        fallback, owner="fallback", submit=lambda: True, now=101.1
    )

    replayed = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=102.0
    )

    assert replayed is observed
    assert replayed.committed_by == "fallback"
    assert replayed.committed_at == 101.1
    assert ledger.pending(now=103.0, min_age=0.0) == ()


def test_new_memory_timestamp_is_a_new_same_text_utterance_within_five_seconds() -> None:
    ledger = QueryLedger()
    first = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    first_agent = ledger.claim(
        owner="agent", text=first.text, target=first.target, now=100.1
    )
    assert first_agent is first

    second = ledger.observe(
        first.text,
        target=first.target,
        source_timestamp=100.2,
        intent="overview",
        now=100.2,
    )
    second_agent = ledger.claim(
        owner="agent", text=second.text, target=second.target, now=100.3
    )

    assert second is not first
    assert second_agent is second
    assert second.claimed_by == "agent"
    assert second.intent == "overview"
    assert second.source_timestamp == 100.2
    assert ledger.pending(now=101.0, min_age=0.0) == ()


def test_trusted_agent_can_answer_same_text_again_before_memory_poll() -> None:
    ledger = QueryLedger()
    first = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    assert ledger.claim(
        owner="agent",
        text=first.text,
        target=first.target,
        now=100.1,
        new_if_handled=True,
    ) is first

    second = ledger.claim(
        owner="agent",
        text=first.text,
        target=first.target,
        now=100.2,
        new_if_handled=True,
    )

    assert second is not None
    assert second is not first
    assert second.source_timestamp == 0.0
    assert second.claimed_by == "agent"
    assert ledger.pending(now=100.3, min_age=0.0) == ()


def test_newer_same_text_observation_is_a_new_utterance() -> None:
    ledger = QueryLedger()
    old = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    claimed = ledger.claim(
        owner="agent", text=old.text, target=old.target, now=101.0
    )
    assert claimed is old

    current = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=110.0, now=110.0
    )

    assert current is not old
    assert current.claimed_by == ""
    assert ledger.pending(now=111.0, min_age=1.0) == (current,)


def test_old_memory_replay_cannot_consume_new_agent_claim() -> None:
    ledger = QueryLedger()
    old = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    assert ledger.claim(
        owner="agent", text=old.text, target=old.target, now=101.0
    ) is old
    current = ledger.claim(
        owner="agent", text=old.text, target=old.target, now=106.1
    )
    assert current is not None
    assert current is not old

    replayed_old = ledger.observe(
        old.text, target=old.target, source_timestamp=90.0, now=106.2
    )
    observed_current = ledger.observe(
        old.text, target=old.target, source_timestamp=106.0, now=106.3
    )

    assert replayed_old is current
    assert observed_current is current
    assert observed_current.claimed_by == "agent"


def test_trusted_agent_can_preempt_uncommitted_same_role_fallback() -> None:
    ledger = QueryLedger()
    observed = ledger.observe(
        "商店里有什么", target="role-a", source_timestamp=100.0, now=100.0
    )
    fallback = ledger.claim(
        owner="fallback", text=observed.text, target=observed.target, now=101.0
    )
    assert fallback is observed

    agent = ledger.claim(
        owner="agent",
        text=observed.text,
        target=observed.target,
        preempt_owners=("fallback",),
        now=101.1,
    )

    assert agent is observed
    assert observed.claimed_by == "agent"


def test_commit_serializes_against_agent_preemption() -> None:
    ledger = QueryLedger()
    observed = ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    fallback = ledger.claim(
        owner="fallback", text=observed.text, target=observed.target, now=101.0
    )
    assert fallback is observed

    submit_entered = threading.Event()
    allow_submit = threading.Event()
    agent_finished = threading.Event()
    commit_results: list[bool] = []
    agent_results: list[object | None] = []

    def submit() -> bool:
        submit_entered.set()
        assert allow_submit.wait(timeout=2.0)
        return True

    def commit_fallback() -> None:
        commit_results.append(
            ledger.commit(fallback, owner="fallback", submit=submit, now=101.1)
        )

    def claim_agent() -> None:
        agent_results.append(
            ledger.claim(
                owner="agent",
                text=observed.text,
                target=observed.target,
                preempt_owners=("fallback",),
                new_if_handled=True,
                now=101.2,
            )
        )
        agent_finished.set()

    commit_thread = threading.Thread(target=commit_fallback)
    agent_thread = threading.Thread(target=claim_agent)
    commit_thread.start()
    assert submit_entered.wait(timeout=2.0)
    agent_thread.start()
    assert not agent_finished.wait(timeout=0.05)
    allow_submit.set()
    commit_thread.join(timeout=2.0)
    agent_thread.join(timeout=2.0)

    assert not commit_thread.is_alive()
    assert not agent_thread.is_alive()
    assert commit_results == [True]
    assert agent_results == [None]
    assert observed.committed_by == "fallback"


def test_query_ledger_releases_failed_fallback_for_retry() -> None:
    ledger = QueryLedger()
    observed = ledger.observe(
        "酒馆里有什么", target="role-a", source_timestamp=100.0, now=100.0
    )
    claim = ledger.claim(
        owner="fallback", text=observed.text, target=observed.target, now=102.0
    )
    assert claim is observed
    assert ledger.release(observed, owner="fallback")
    assert ledger.pending(now=103.0, min_age=1.0) == (observed,)


def test_query_ledger_clear_drops_pending_and_claimed_queries() -> None:
    ledger = QueryLedger()
    ledger.observe(
        "现在第几回合", target="role-a", source_timestamp=100.0, now=100.0
    )
    ledger.claim(
        owner="agent", text="商店里有什么", target="role-a", now=100.0
    )

    ledger.clear()

    assert ledger.pending(now=101.0, min_age=0.0) == ()
