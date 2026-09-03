from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from hearthstone_companion_under_test.delivery import (
    LiveStatePublisher,
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
    assert len(calls) == 7


def test_serialization_failure_tombstones_previous_complete_cursor() -> None:
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})

    def build(snapshot: FakeSnapshot, **_kwargs: Any) -> tuple[tuple[str, str], ...]:
        if snapshot is updated:
            raise ValueError("serialization failed")
        return (("core", "old-core"), ("board", "old-board"))

    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=build,
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert [call["metadata"]["context_expired"] for call in calls] == [
        False,
        False,
        True,
        True,
    ]
    assert [call["metadata"]["segment"] for call in calls[-2:]] == [
        "core",
        "board",
    ]


def test_empty_serialization_tombstones_previous_complete_cursor() -> None:
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})

    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=lambda snapshot, **_kwargs: (
            (("core", "old-core"),) if snapshot is old else ()
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True
    assert calls[-1]["metadata"]["segment"] == "core"


def test_failed_serialization_tombstone_leaves_partial_cursor_for_retry() -> None:
    outcomes = iter([True, False, True])
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})

    def push(**message: Any) -> dict[str, bool]:
        calls.append(message)
        return {"submitted": next(outcomes)}

    def build(snapshot: FakeSnapshot, **_kwargs: Any) -> tuple[tuple[str, str], ...]:
        if snapshot is updated:
            raise ValueError("serialization failed")
        return (("core", "old-core"),)

    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=build,
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is not None
    assert publisher.cursor.complete is False
    assert publisher.cursor.segments == ("core",)

    assert not publisher.publish(updated, target="role-a", now=11.1)
    assert publisher.cursor is None
    assert [call["metadata"]["context_expired"] for call in calls] == [
        False,
        True,
        True,
    ]


def test_delivery_invalidated_before_first_segment_expires_old_bundle() -> None:
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})
    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=lambda snapshot, **_kwargs: (
            ("core", f"core-{snapshot.payload['turn']}"),
            ("board", f"board-{snapshot.payload['turn']}"),
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(
        updated,
        target="role-a",
        now=11.0,
        valid=lambda: False,
    )

    assert publisher.cursor is None
    assert [call["metadata"]["segment"] for call in calls[-2:]] == [
        "core",
        "board",
    ]
    assert all(call["metadata"]["context_expired"] for call in calls[-2:])


def test_mid_publish_invalidation_tombstones_old_and_new_segment_union() -> None:
    calls: list[dict[str, Any]] = []
    validity = iter([True, False])
    old = FakeSnapshot(1, {"turn": 3, "shop": True})
    updated = FakeSnapshot(1, {"turn": 4, "shop": False})

    def segments(snapshot: FakeSnapshot, **_kwargs: Any) -> tuple[tuple[str, str], ...]:
        core = (("core", f"core-{snapshot.payload['turn']}"),)
        return (*core, ("shop", "old-shop")) if snapshot.payload["shop"] else core

    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=segments,
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(
        updated,
        target="role-a",
        now=11.0,
        valid=lambda: next(validity),
    )

    assert publisher.cursor is None
    tombstones = [call for call in calls if call["metadata"]["context_expired"]]
    assert [call["metadata"]["segment"] for call in tombstones] == [
        "shop",
        "core",
    ]


def test_mid_publish_invalidation_cleanup_failure_retries_full_bundle() -> None:
    outcomes = iter([True, True, True, False, True, True, True, True])
    calls: list[dict[str, Any]] = []
    validity = iter([True, False])
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})

    def push(**message: Any) -> dict[str, bool]:
        calls.append(message)
        return {"submitted": next(outcomes)}

    publisher = LiveStatePublisher(
        push_message=push,
        build_segments=lambda snapshot, **_kwargs: (
            ("core", f"core-{snapshot.payload['turn']}"),
            ("shop", f"shop-{snapshot.payload['turn']}"),
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(
        updated,
        target="role-a",
        now=11.0,
        valid=lambda: next(validity),
    )
    assert publisher.cursor is not None
    assert publisher.cursor.complete is False
    assert publisher.cursor.segments == ("core", "shop")

    assert publisher.publish(
        updated,
        target="role-a",
        now=11.1,
        valid=lambda: True,
    )
    assert publisher.cursor is not None
    assert publisher.cursor.complete is True
    assert len(calls) == 8


def test_validity_exception_expires_deduplicated_context() -> None:
    calls: list[dict[str, Any]] = []
    snapshot = FakeSnapshot(1, {"turn": 3})
    publisher = _publisher(calls, refresh_seconds=30.0)

    assert publisher.publish(snapshot, target="role-a", now=10.0)

    def invalid() -> bool:
        raise RuntimeError("source generation unavailable")

    assert not publisher.publish(
        snapshot,
        target="role-a",
        now=10.1,
        valid=invalid,
    )
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True


def test_fingerprint_failure_expires_previous_context() -> None:
    calls: list[dict[str, Any]] = []
    publisher = _publisher(calls)
    old = FakeSnapshot(1, {"turn": 3})

    class BrokenSnapshot:
        game_number = 1

        def to_public_dict(self) -> dict[str, Any]:
            raise ValueError("snapshot unavailable")

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(BrokenSnapshot(), target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True


@pytest.mark.parametrize(
    "segments",
    [
        (("board", "board-state"),),
        (("core", "core-state"), ("core", "duplicate")),
        (("", "core-state"),),
        (("core", ""),),
        (("core", None),),
        (("core", "x" * 901),),
        (("core", "core-state", "extra"),),
    ],
)
def test_invalid_segment_bundle_expires_previous_context(segments: Any) -> None:
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})
    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=lambda snapshot, **_kwargs: (
            (("core", "old-core"),) if snapshot is old else segments
        ),
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True


def test_segment_generator_failure_expires_previous_context() -> None:
    calls: list[dict[str, Any]] = []
    old = FakeSnapshot(1, {"turn": 3})
    updated = FakeSnapshot(1, {"turn": 4})

    def build(snapshot: FakeSnapshot, **_kwargs: Any) -> Any:
        if snapshot is old:
            return (("core", "old-core"),)

        def broken_segments() -> Any:
            yield ("core", "new-core")
            raise ValueError("late serialization failure")

        return broken_segments()

    publisher = LiveStatePublisher(
        push_message=lambda **message: calls.append(message) or {"submitted": True},
        build_segments=build,
        logger=None,
        max_prompt_bytes=900,
    )

    assert publisher.publish(old, target="role-a", now=10.0)
    assert not publisher.publish(updated, target="role-a", now=11.0)
    assert publisher.cursor is None
    assert calls[-1]["metadata"]["context_expired"] is True
