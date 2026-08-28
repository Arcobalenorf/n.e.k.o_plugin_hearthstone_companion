from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import GameSnapshot

_SEMANTICALLY_VOLATILE_KEYS = frozenset({"observed_at", "revision"})
_QUERY_DEDUP_SECONDS = 5.0
_QUERY_CORRELATION_SKEW_SECONDS = 2.0
_QUERY_SOURCE_LOOKBACK_SECONDS = 12.0


class _NullLogger:
    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _semantic_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _SEMANTICALLY_VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def semantic_snapshot_fingerprint(snapshot: GameSnapshot) -> str:
    """Hash only player-visible game semantics, not collection bookkeeping."""
    payload = _semantic_value(snapshot.to_public_dict())
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def push_was_submitted(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        if "submitted" in result:
            return bool(result.get("submitted"))
        if "ok" in result:
            return bool(result.get("ok"))
    submitted = getattr(result, "submitted", None)
    if submitted is not None:
        return bool(submitted)
    is_ok = getattr(result, "is_ok", None)
    if callable(is_ok):
        try:
            return bool(is_ok())
        except Exception:
            return False
    return False


@dataclass(frozen=True, slots=True)
class PublishedState:
    target: str
    game_number: int
    fingerprint: str
    segments: tuple[str, ...]
    published_at: float
    snapshot: GameSnapshot
    complete: bool = True


class LiveStatePublisher:
    """Own one replaceable, semantically deduplicated live-state context."""

    def __init__(
        self,
        *,
        push_message: Callable[..., Any],
        build_segments: Callable[..., tuple[tuple[str, str], ...]],
        logger: Any,
        max_prompt_bytes: int,
        refresh_seconds: float = 30.0,
        unresolved_refresh_seconds: float = 1.0,
        source: str = "hearthstone_companion",
    ) -> None:
        self._push_message = push_message
        self._build_segments = build_segments
        self._logger = logger if logger is not None else _NullLogger()
        self._max_prompt_bytes = max(256, int(max_prompt_bytes))
        self._refresh_seconds = max(5.0, float(refresh_seconds))
        self._unresolved_refresh_seconds = max(
            0.5, float(unresolved_refresh_seconds)
        )
        self._source = str(source or "hearthstone_companion")
        self._cursor: PublishedState | None = None
        self._lock = threading.RLock()

    @property
    def cursor(self) -> PublishedState | None:
        with self._lock:
            return self._cursor

    def restore(
        self,
        *,
        target: str,
        game_number: int,
        segments: tuple[str, ...],
        snapshot: GameSnapshot | None,
        published_at: float = 0.0,
    ) -> None:
        clean_target = str(target or "").strip()[:80]
        restored_snapshot = snapshot or GameSnapshot(
            game_number=max(0, int(game_number))
        )
        with self._lock:
            self._cursor = PublishedState(
                target=clean_target,
                game_number=max(0, int(game_number)),
                fingerprint=semantic_snapshot_fingerprint(restored_snapshot),
                segments=tuple(segments or ("core",)),
                published_at=float(published_at or 0.0),
                snapshot=restored_snapshot,
                complete=True,
            )

    @staticmethod
    def _key(target: str, segment: str) -> str:
        if target:
            owner = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
        else:
            owner = "active-session"
        return f"hearthstone:live-state:{owner}:{segment}"

    def _push(
        self,
        text: str,
        *,
        target: str,
        segment: str,
        game_number: int,
        fingerprint: str,
        expired: bool,
    ) -> bool:
        metadata = {
            "kind": "game_live_state_expired" if expired else "game_live_state",
            "context_type": "hearthstone_companion_live_state",
            "delivery_intent": "passive_context",
            "context_expired": expired,
            "privacy_scope": ("no_game_state_tombstone" if expired else "filtered_player_visible_live_state"),
            "format": "hearthstone_live_atomic_v1",
            "segment": segment,
            "match_id": game_number,
            "semantic_fingerprint": fingerprint[:16],
            "routing_scope": "configured_role" if target else "active_session_fallback",
        }
        kwargs: dict[str, Any] = {
            "visibility": [],
            "ai_behavior": "read",
            "parts": [{"type": "text", "text": text}],
            "source": self._source,
            "metadata": metadata,
            "priority": 0,
            "coalesce_key": self._key(target, segment),
        }
        if target:
            kwargs["target_lanlan"] = target
        try:
            return push_was_submitted(self._push_message(**kwargs))
        except Exception as exc:
            self._logger.warning(
                "Hearthstone live-state delivery failed code=%s",
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _tombstone(reason: str) -> str:
        clean_reason = " ".join(str(reason or "unavailable").split())[:48]
        return (
            "# 炉石实时公开状态已失效\n"
            f"reason={clean_reason};"
            "不得继续使用此前同一局和分段的局势；需要当前事实时调用 "
            "hearthstone_live_state。"
        )

    def _expire_cursor(self, cursor: PublishedState, *, reason: str) -> bool:
        text = self._tombstone(reason)
        for segment in cursor.segments or ("core",):
            if not self._push(
                text,
                target=cursor.target,
                segment=segment,
                game_number=cursor.game_number,
                fingerprint=cursor.fingerprint,
                expired=True,
            ):
                return False
        if self._cursor == cursor:
            self._cursor = None
        return True

    def expire(self, *, reason: str = "unavailable") -> bool:
        with self._lock:
            cursor = self._cursor
            if cursor is None:
                return True
            return self._expire_cursor(cursor, reason=reason)

    def publish(
        self,
        snapshot: GameSnapshot,
        *,
        target: str,
        now: float | None = None,
        observed_at: float | None = None,
        valid: Callable[[], bool] | None = None,
    ) -> bool:
        current_time = time.monotonic() if now is None else float(now)
        captured_at = time.time() if observed_at is None else float(observed_at)
        clean_target = str(target or "").strip()[:80]
        fingerprint = semantic_snapshot_fingerprint(snapshot)

        with self._lock:
            cursor = self._cursor
            if (
                cursor is not None
                and cursor.target == clean_target
                and cursor.game_number == snapshot.game_number
                and cursor.fingerprint == fingerprint
                and cursor.complete
                and current_time - cursor.published_at
                < (
                    self._refresh_seconds
                    if clean_target
                    else self._unresolved_refresh_seconds
                )
            ):
                return True

            if cursor is not None and not cursor.complete:
                if not self._expire_cursor(cursor, reason="partial_publish_retry"):
                    return False
                cursor = None

            if cursor is not None and (cursor.target != clean_target or cursor.game_number != snapshot.game_number):
                if not self._expire_cursor(cursor, reason="route_or_match_changed"):
                    return False
                cursor = None

            try:
                built = self._build_segments(
                    snapshot,
                    observed_at=captured_at,
                    max_prompt_bytes=self._max_prompt_bytes,
                )
            except Exception as exc:
                self._logger.warning(
                    "Hearthstone live-state serialization failed code=%s",
                    type(exc).__name__,
                )
                return False

            segments = tuple((str(name), str(text)) for name, text in built)
            segment_names = tuple(name for name, _text in segments)
            if not segment_names:
                return False

            if cursor is not None:
                next_segment_set = set(segment_names)
                removed = tuple(
                    name for name in cursor.segments if name not in next_segment_set
                )
                if removed:
                    text = self._tombstone("segment_set_changed")
                    for segment in removed:
                        if not self._push(
                            text,
                            target=cursor.target,
                            segment=segment,
                            game_number=cursor.game_number,
                            fingerprint=cursor.fingerprint,
                            expired=True,
                        ):
                            return False
                    retained = tuple(
                        name for name in cursor.segments if name in next_segment_set
                    )
                    if retained:
                        cursor = PublishedState(
                            target=cursor.target,
                            game_number=cursor.game_number,
                            fingerprint=cursor.fingerprint,
                            segments=retained,
                            published_at=cursor.published_at,
                            snapshot=cursor.snapshot,
                            complete=cursor.complete,
                        )
                        self._cursor = cursor
                    else:
                        self._cursor = None
                        cursor = None

            delivered_segments: list[str] = []

            def remember_partial() -> None:
                self._cursor = PublishedState(
                    target=clean_target,
                    game_number=snapshot.game_number,
                    fingerprint=fingerprint,
                    segments=segment_names,
                    published_at=current_time,
                    snapshot=snapshot,
                    complete=False,
                )

            for segment, text in segments:
                if valid is not None and not valid():
                    tombstone = self._tombstone("delivery_invalidated")
                    cleanup_ok = True
                    for cleanup_segment in delivered_segments:
                        if not self._push(
                            tombstone,
                            target=clean_target,
                            segment=cleanup_segment,
                            game_number=snapshot.game_number,
                            fingerprint=fingerprint,
                            expired=True,
                        ):
                            cleanup_ok = False
                    if delivered_segments and not cleanup_ok:
                        remember_partial()
                    return False
                if not self._push(
                    text,
                    target=clean_target,
                    segment=segment,
                    game_number=snapshot.game_number,
                    fingerprint=fingerprint,
                    expired=False,
                ):
                    if delivered_segments or cursor is not None:
                        tombstone = self._tombstone("partial_publish")
                        cleanup_ok = True
                        for cleanup_segment in segment_names:
                            if not self._push(
                                tombstone,
                                target=clean_target,
                                segment=cleanup_segment,
                                game_number=snapshot.game_number,
                                fingerprint=fingerprint,
                                expired=True,
                            ):
                                cleanup_ok = False
                        if cleanup_ok:
                            self._cursor = None
                        else:
                            remember_partial()
                    return False
                delivered_segments.append(segment)

            if valid is not None and not valid():
                tombstone = self._tombstone("delivery_invalidated")
                cleanup_ok = True
                for cleanup_segment in segment_names:
                    if not self._push(
                        tombstone,
                        target=clean_target,
                        segment=cleanup_segment,
                        game_number=snapshot.game_number,
                        fingerprint=fingerprint,
                        expired=True,
                    ):
                        cleanup_ok = False
                if cleanup_ok:
                    self._cursor = None
                else:
                    remember_partial()
                return False

            self._cursor = PublishedState(
                target=clean_target,
                game_number=snapshot.game_number,
                fingerprint=fingerprint,
                segments=segment_names,
                published_at=current_time,
                snapshot=snapshot,
                complete=True,
            )
            return True


@dataclass(slots=True)
class QueryClaim:
    signature: str
    text: str
    target: str
    intent: str
    source_timestamp: float
    observed_at: float
    claimed_by: str = ""
    claimed_at: float = 0.0
    committed_by: str = ""
    committed_at: float = 0.0


class QueryLedger:
    """Coordinate role-scoped Agent and fallback delivery without persistence."""

    def __init__(self, *, ttl_seconds: float = 90.0, max_items: int = 64) -> None:
        self._ttl_seconds = max(10.0, float(ttl_seconds))
        self._max_items = max(8, int(max_items))
        self._items: OrderedDict[str, QueryClaim] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def signature(text: str, target: str = "") -> str:
        normalized = " ".join(str(text or "").casefold().split())[:240]
        clean_target = str(target or "").strip().casefold()[:80]
        return hashlib.sha256(f"{clean_target}\x00{normalized}".encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        stale = [
            key
            for key, item in self._items.items()
            if max(item.observed_at, item.claimed_at, item.committed_at) < cutoff
        ]
        for key in stale:
            self._items.pop(key, None)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def observe(
        self,
        text: str,
        *,
        target: str,
        source_timestamp: float,
        intent: str = "",
        now: float | None = None,
    ) -> QueryClaim:
        current_time = time.time() if now is None else float(now)
        signature = self.signature(text, target)
        clean_text = " ".join(str(text or "").split())[:240]
        clean_target = str(target or "").strip()[:80]
        clean_intent = str(intent or "").strip().casefold()[:32]
        if not clean_text or not clean_target:
            raise ValueError("observed queries require non-empty text and target")
        with self._lock:
            self._prune(current_time)
            previous = self._items.get(signature)
            source_time = float(source_timestamp)
            if previous is not None and previous.source_timestamp > 0:
                if source_time <= previous.source_timestamp + 0.001:
                    return previous
                previous = None
            if previous is not None and previous.source_timestamp <= 0:
                correlation_start = previous.claimed_at - _QUERY_SOURCE_LOOKBACK_SECONDS
                correlation_end = previous.claimed_at + _QUERY_CORRELATION_SKEW_SECONDS
                if source_time < correlation_start:
                    # memory.get replays a recent window. An older identical
                    # record must not consume a newer role-scoped Agent claim.
                    return previous
                if source_time <= correlation_end:
                    previous.intent = clean_intent or previous.intent
                    previous.source_timestamp = source_time
                    previous.observed_at = current_time
                    self._items.move_to_end(signature)
                    return previous
            item = QueryClaim(
                signature=signature,
                text=clean_text,
                target=clean_target,
                intent=clean_intent,
                source_timestamp=source_time,
                observed_at=current_time,
            )
            self._items[signature] = item
            self._items.move_to_end(signature)
            return item

    def claim(
        self,
        *,
        owner: str,
        text: str = "",
        target: str = "",
        now: float | None = None,
        intent: str = "",
        preempt_owners: tuple[str, ...] = (),
        new_if_handled: bool = False,
    ) -> QueryClaim | None:
        current_time = time.time() if now is None else float(now)
        clean_target = str(target or "").strip()[:80]
        clean_owner = str(owner or "unknown")[:24]
        if not clean_target:
            return None
        with self._lock:
            self._prune(current_time)
            clean_intent = str(intent or "").strip().casefold()[:32]
            allowed_preemptions = {
                str(value or "")[:24] for value in preempt_owners
            }
            item: QueryClaim | None = None
            if text:
                direct = self._items.get(self.signature(text, clean_target))
                if (
                    direct is not None
                    and new_if_handled
                    and direct.claimed_by
                    and not direct.committed_by
                    and direct.claimed_by not in allowed_preemptions
                ):
                    # A trusted Agent invocation represents a new user-turn
                    # opportunity. Prefer answering a rapid same-text re-ask
                    # over suppressing it as a late duplicate without an
                    # official message id that could prove identity.
                    self._items.pop(direct.signature, None)
                    direct = None
                if (
                    direct is not None
                    and (
                        (
                            direct.committed_by
                            and current_time - direct.committed_at
                            > _QUERY_DEDUP_SECONDS
                        )
                        or (
                            direct.claimed_by
                            and not direct.committed_by
                            and current_time - direct.claimed_at
                            > _QUERY_DEDUP_SECONDS
                        )
                    )
                ):
                    # A claim deduplicates competing paths for one request, not
                    # every later utterance with the same wording. The memory
                    # poll can lag behind an Agent callback, so expire the
                    # claim window even when the earlier request was observed.
                    self._items.pop(direct.signature, None)
                    direct = None
                item = direct
            if item is None and not text:
                pending = [
                    candidate
                    for candidate in reversed(tuple(self._items.values()))
                    if not candidate.committed_by
                    if (
                        not candidate.claimed_by
                        or candidate.claimed_by in allowed_preemptions
                    )
                    and (
                        candidate.target.casefold() == clean_target.casefold()
                    )
                    and (
                        not clean_intent
                        or candidate.intent == clean_intent
                    )
                    and current_time - candidate.observed_at
                    <= _QUERY_DEDUP_SECONDS
                ]
                if len(pending) == 1:
                    item = pending[0]
            if item is None and text:
                signature = self.signature(text, clean_target)
                item = QueryClaim(
                    signature=signature,
                    text=" ".join(str(text).split())[:240],
                    target=clean_target,
                    intent=clean_intent,
                    source_timestamp=0.0,
                    observed_at=current_time,
                )
                self._items[signature] = item
            if item is None:
                return None
            if item.committed_by:
                return None
            if item.claimed_by:
                if item.claimed_by not in allowed_preemptions:
                    return None
            item.claimed_by = clean_owner
            item.claimed_at = current_time
            self._items.move_to_end(item.signature)
            return item

    def pending(self, *, now: float | None = None, min_age: float = 1.0) -> tuple[QueryClaim, ...]:
        current_time = time.time() if now is None else float(now)
        with self._lock:
            self._prune(current_time)
            return tuple(
                item
                for item in self._items.values()
                if item.source_timestamp > 0
                and not item.claimed_by
                and current_time - item.source_timestamp >= max(0.0, float(min_age))
            )

    def release(self, claim: QueryClaim, *, owner: str) -> bool:
        with self._lock:
            item = next(
                (candidate for candidate in self._items.values() if candidate is claim),
                None,
            )
            if (
                item is None
                or item.claimed_by != str(owner or "")
                or item.committed_by
            ):
                return False
            if item.source_timestamp <= 0:
                self._items.pop(item.signature, None)
                return True
            item.claimed_by = ""
            item.claimed_at = 0.0
            return True

    def owns(self, claim: QueryClaim, *, owner: str) -> bool:
        with self._lock:
            current = self._items.get(claim.signature)
            return bool(
                current is claim
                and current.claimed_by == str(owner or "")
                and not current.committed_by
            )

    def commit(
        self,
        claim: QueryClaim,
        *,
        owner: str,
        submit: Callable[[], bool],
        now: float | None = None,
    ) -> bool:
        """Atomically submit a claimed fallback and seal it against preemption."""
        current_time = time.time() if now is None else float(now)
        clean_owner = str(owner or "")[:24]
        with self._lock:
            current = next(
                (candidate for candidate in self._items.values() if candidate is claim),
                None,
            )
            if (
                current is None
                or current.claimed_by != clean_owner
                or current.committed_by
            ):
                return False
            submitted = bool(submit())
            if submitted:
                current.committed_by = clean_owner
                current.committed_at = current_time
            return submitted

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


__all__ = [
    "LiveStatePublisher",
    "PublishedState",
    "QueryClaim",
    "QueryLedger",
    "push_was_submitted",
    "semantic_snapshot_fingerprint",
]
