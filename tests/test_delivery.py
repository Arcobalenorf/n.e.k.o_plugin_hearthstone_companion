from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def test_publisher_deduplicates_until_refresh_lease_expires() -> None:
    calls: list[dict[str, Any]] = []
    snapshot = FakeSnapshot(1, {"mode": "constructed", "turn": 3})
    publisher = LiveStatePublisher(
        push_message=lambda **kwargs: calls.append(kwargs) or {"submitted": True},
        build_segments=lambda *_args, **_kwargs: (("core", "state"),),
        logger=None,
        max_prompt_bytes=900,
        refresh_seconds=30.0,
    )

    assert publisher.publish(snapshot, target="role-a", now=10.0)
    assert publisher.publish(snapshot, target="role-a", now=39.9)
    assert len(calls) == 1
    assert publisher.publish(snapshot, target="role-a", now=40.0)
    assert len(calls) == 2


def test_publisher_refuses_unresolved_target() -> None:
    calls: list[dict[str, Any]] = []
    publisher = LiveStatePublisher(
        push_message=lambda **kwargs: calls.append(kwargs) or {"submitted": True},
        build_segments=lambda *_args, **_kwargs: (("core", "state"),),
        logger=None,
        max_prompt_bytes=900,
    )

    assert not publisher.publish(FakeSnapshot(1, {"turn": 3}), target="")
    assert calls == []


def test_publisher_does_not_restore_unrouted_cursor() -> None:
    calls: list[dict[str, Any]] = []
    publisher = LiveStatePublisher(
        push_message=lambda **kwargs: calls.append(kwargs) or {"submitted": True},
        build_segments=lambda *_args, **_kwargs: (("core", "state"),),
        logger=None,
        max_prompt_bytes=900,
    )

    publisher.restore(
        target="",
        game_number=1,
        segments=("core",),
        snapshot=FakeSnapshot(1, {"turn": 3}),
    )

    assert publisher.cursor is None
    assert publisher.expire(reason="test")
    assert calls == []


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
        if snapshot.payload["shop"]:
            return (*core, ("shop", "shop=A"))
        return core

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


def test_query_ledger_provisional_tool_claim_blocks_fallback() -> None:
    ledger = QueryLedger()
    claim = ledger.claim(
        owner="tool",
        text="对面场上有什么随从",
        target="role-a",
        now=100.0,
    )
    assert claim is not None

    observed = ledger.observe(
        "对面场上有什么随从",
        target="role-a",
        source_timestamp=101.0,
        now=101.0,
    )

    assert observed.claimed_by == "tool"
    assert ledger.pending(now=103.0, min_age=1.0) == ()


def test_query_ledger_moves_unrouted_provisional_claim_to_observed_target() -> None:
    ledger = QueryLedger()
    provisional = ledger.claim(
        owner="tool",
        text="现在第几回合",
        target="",
        now=101.0,
    )
    assert provisional is not None

    observed = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.5,
        now=101.2,
    )

    assert observed.target == "role-a"
    assert observed.claimed_by == "tool"
    assert ledger.claim(
        owner="fallback",
        text="现在第几回合",
        target="role-a",
        now=102.0,
    ) is None
    assert ledger.pending(now=103.0, min_age=1.0) == ()


def test_query_ledger_releases_a_claim_after_target_migration_by_identity() -> None:
    ledger = QueryLedger()
    provisional = ledger.claim(
        owner="tool",
        text="现在第几回合",
        target="",
        now=101.0,
    )
    assert provisional is not None

    observed = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.5,
        now=101.2,
    )

    assert observed is provisional
    assert ledger.release(provisional, owner="tool")
    assert ledger.pending(now=103.0, min_age=1.0) == (observed,)


def test_query_ledger_old_claim_cannot_release_a_new_same_text_utterance() -> None:
    ledger = QueryLedger()
    old = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    assert ledger.claim(
        owner="tool", text=old.text, target=old.target, now=101.0
    ) is old
    current = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=110.0,
        now=110.0,
    )
    assert current is not old
    assert ledger.claim(
        owner="fallback", text=current.text, target=current.target, now=112.0
    ) is current

    assert not ledger.release(old, owner="tool")
    assert current.claimed_by == "fallback"


def test_query_ledger_unobserved_provisional_never_enters_fallback_queue() -> None:
    ledger = QueryLedger()
    provisional = ledger.claim(owner="tool", text="商店里有什么", now=100.0)
    assert provisional is not None

    assert ledger.release(provisional, owner="tool")
    assert ledger.pending(now=200.0, min_age=0.0) == ()


def test_query_ledger_new_provisional_wins_over_same_targets_old_utterance() -> None:
    ledger = QueryLedger()
    old = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    assert ledger.claim(
        owner="fallback",
        text=old.text,
        target=old.target,
        now=102.0,
    ) is not None
    assert ledger.claim(
        owner="tool",
        text="现在第几回合",
        target="",
        now=110.0,
    ) is not None

    current = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=109.5,
        now=110.2,
    )

    assert current.claimed_by == "tool"
    assert current.claimed_at == 110.0
    assert ledger.pending(now=112.0, min_age=1.0) == ()


def test_query_ledger_does_not_claim_another_roles_only_pending_question() -> None:
    ledger = QueryLedger()
    role_b = ledger.observe(
        "对面场上有什么随从",
        target="role-b",
        source_timestamp=100.0,
        now=100.0,
    )

    role_a = ledger.claim(
        owner="tool",
        text="我的手牌里有什么",
        target="role-a",
        now=101.0,
    )

    assert role_a is not None
    assert role_a.target == "role-a"
    assert ledger.pending(now=102.0, min_age=1.0) == (role_b,)


def test_query_ledger_does_not_guess_between_two_roles_with_same_question() -> None:
    ledger = QueryLedger()
    role_a = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    role_b = ledger.observe(
        "现在第几回合",
        target="role-b",
        source_timestamp=100.1,
        now=100.1,
    )

    assert ledger.claim(owner="tool", text="现在第几回合", target="", now=101.0) is None
    assert ledger.pending(now=102.0, min_age=1.0) == (role_a, role_b)


def test_query_ledger_does_not_claim_same_roles_different_pending_question() -> None:
    ledger = QueryLedger()
    first = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )

    second = ledger.claim(
        owner="tool",
        text="对面场上有什么随从",
        target="role-a",
        now=101.0,
    )

    assert second is not None
    assert second.text == "对面场上有什么随从"
    assert first.claimed_by == ""


def test_query_ledger_replaces_stale_unrouted_provisional_claim() -> None:
    ledger = QueryLedger()
    first = ledger.claim(
        owner="tool",
        text="商店里有什么",
        target="",
        now=100.0,
    )
    second = ledger.claim(
        owner="tool",
        text="商店里有什么",
        target="",
        now=106.0,
    )

    assert first is not None
    assert second is not None
    assert second is not first
    assert second.claimed_at == 106.0


def test_query_ledger_allows_later_identical_observed_question() -> None:
    ledger = QueryLedger()
    first = ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    assert ledger.claim(
        owner="agent",
        text=first.text,
        target=first.target,
        now=101.0,
    ) is first

    second = ledger.claim(
        owner="agent",
        text="现在第几回合",
        target="role-a",
        now=106.1,
    )

    assert second is not None
    assert second is not first
    assert second.claimed_by == "agent"
    assert second.source_timestamp == 0.0


def test_query_ledger_old_memory_replay_cannot_steal_later_identical_claim() -> None:
    for claim_target in ("role-a", ""):
        ledger = QueryLedger()
        old = ledger.observe(
            "现在第几回合",
            target="role-a",
            source_timestamp=100.0,
            now=100.0,
        )
        assert ledger.claim(
            owner="agent",
            text=old.text,
            target=old.target,
            now=101.0,
        ) is old

        current = ledger.claim(
            owner="tool" if not claim_target else "agent",
            text="现在第几回合",
            target=claim_target,
            now=106.1,
        )
        assert current is not None
        assert current is not old

        replayed_old = ledger.observe(
            "现在第几回合",
            target="role-a",
            source_timestamp=100.0,
            now=106.2,
        )
        observed_current = ledger.observe(
            "现在第几回合",
            target="role-a",
            source_timestamp=106.0,
            now=106.3,
        )

        assert replayed_old is current
        assert observed_current is current
        assert observed_current.claimed_by == ("tool" if not claim_target else "agent")
        assert ledger.pending(now=108.0, min_age=1.0) == ()


def test_query_ledger_releases_failed_fallback_for_retry() -> None:
    ledger = QueryLedger()
    observed = ledger.observe(
        "酒馆里有什么",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    claim = ledger.claim(owner="fallback", now=102.0)
    assert claim is not None
    assert claim.signature == observed.signature
    assert ledger.pending(now=103.0, min_age=1.0) == ()

    assert ledger.release(observed, owner="fallback")
    assert ledger.pending(now=103.0, min_age=1.0) == (observed,)


def test_query_ledger_clear_drops_pending_and_claimed_queries() -> None:
    ledger = QueryLedger()
    ledger.observe(
        "现在第几回合",
        target="role-a",
        source_timestamp=100.0,
        now=100.0,
    )
    ledger.claim(
        owner="tool",
        text="商店里有什么",
        target="role-a",
        now=100.0,
    )

    ledger.clear()

    assert ledger.pending(now=101.0, min_age=0.0) == ()
