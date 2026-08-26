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
        source: str = "hearthstone_companion",
    ) -> None:
        self._push_message = push_message
        self._build_segments = build_segments
        self._logger = logger if logger is not None else _NullLogger()
        self._max_prompt_bytes = max(256, int(max_prompt_bytes))
        self._refresh_seconds = max(5.0, float(refresh_seconds))
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
        if not clean_target:
            with self._lock:
                self._cursor = None
            return
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
            "format": "hearthstone_live_segments_v2",
            "segment": segment,
            "match_id": game_number,
            "semantic_fingerprint": fingerprint[:16],
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
        if not clean_target:
            return False
        fingerprint = semantic_snapshot_fingerprint(snapshot)

        with self._lock:
            cursor = self._cursor
            if (
                cursor is not None
                and cursor.target == clean_target
                and cursor.game_number == snapshot.game_number
                and cursor.fingerprint == fingerprint
                and cursor.complete
                and current_time - cursor.published_at < self._refresh_seconds
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
    source_timestamp: float
    observed_at: float
    claimed_by: str = ""
    claimed_at: float = 0.0


class QueryLedger:
    """Coordinate tool, Agent, and memory fallback without persisting utterances."""

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
        stale = [key for key, item in self._items.items() if max(item.observed_at, item.claimed_at) < cutoff]
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
        now: float | None = None,
    ) -> QueryClaim:
        current_time = time.time() if now is None else float(now)
        signature = self.signature(text, target)
        clean_text = " ".join(str(text or "").split())[:240]
        clean_target = str(target or "").strip()[:80]
        with self._lock:
            self._prune(current_time)
            previous = self._items.get(signature)
            provisional_signature = ""
            provisional: QueryClaim | None = None
            if clean_target:
                provisional_signature = self.signature(text, "")
                provisional = self._items.get(provisional_signature)
            source_time = float(source_timestamp)
            active_unobserved_claims = tuple(
                item
                for item in (previous, provisional)
                if item is not None
                and item.source_timestamp <= 0
                and item.claimed_by
                and current_time - item.claimed_at <= _QUERY_DEDUP_SECONDS
            )
            newest_unobserved_claim = max(
                active_unobserved_claims,
                key=lambda item: item.claimed_at,
                default=None,
            )
            if (
                newest_unobserved_claim is not None
                and source_time
                < newest_unobserved_claim.claimed_at
                - _QUERY_CORRELATION_SKEW_SECONDS
            ):
                # memory.get returns a recent window, not only the newest item.
                # Do not let an older identical record consume the provisional
                # claim for a later utterance in the same poll batch.
                return newest_unobserved_claim
            is_new_utterance = bool(
                previous is None
                or source_time > previous.source_timestamp + 0.001
            )
            keep_direct_claim = bool(
                previous is not None
                and previous.source_timestamp <= 0
                and previous.claimed_by
                and current_time - previous.claimed_at <= _QUERY_DEDUP_SECONDS
                and abs(previous.claimed_at - source_time)
                <= _QUERY_CORRELATION_SKEW_SECONDS
            )
            keep_provisional_claim = bool(
                provisional is not None
                and provisional.source_timestamp <= 0
                and provisional.claimed_by
                and current_time - provisional.claimed_at <= _QUERY_DEDUP_SECONDS
                and abs(provisional.claimed_at - source_time)
                <= _QUERY_CORRELATION_SKEW_SECONDS
            )
            if keep_direct_claim:
                claim_source = previous
            elif keep_provisional_claim:
                claim_source = provisional
            else:
                claim_source = previous
            keep_claim = keep_direct_claim or keep_provisional_claim or not is_new_utterance
            if claim_source is not None and keep_claim:
                item = claim_source
                item.signature = signature
                item.text = clean_text
                item.target = clean_target
                item.source_timestamp = source_time
                item.observed_at = current_time
            else:
                item = QueryClaim(
                    signature=signature,
                    text=clean_text,
                    target=clean_target,
                    source_timestamp=source_time,
                    observed_at=current_time,
                )
            if provisional_signature and provisional_signature != signature:
                self._items.pop(provisional_signature, None)
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
    ) -> QueryClaim | None:
        current_time = time.time() if now is None else float(now)
        with self._lock:
            self._prune(current_time)
            item: QueryClaim | None = None
            if text:
                direct = self._items.get(self.signature(text, target))
                if direct is None and target:
                    direct = self._items.get(self.signature(text, ""))
                if direct is None and not target:
                    normalized = " ".join(str(text).casefold().split())[:240]
                    matches = [
                        candidate
                        for candidate in self._items.values()
                        if " ".join(candidate.text.casefold().split())[:240]
                        == normalized
                        and (
                            (
                                candidate.source_timestamp > 0
                                and current_time - candidate.source_timestamp
                                <= _QUERY_DEDUP_SECONDS
                            )
                            or (
                                candidate.source_timestamp <= 0
                                and candidate.claimed_at > 0
                                and current_time - candidate.claimed_at
                                <= _QUERY_DEDUP_SECONDS
                            )
                        )
                    ]
                    if len(matches) == 1:
                        direct = matches[0]
                    elif len(matches) > 1:
                        return None
                if (
                    direct is not None
                    and direct.claimed_by
                    and current_time - direct.claimed_at > _QUERY_DEDUP_SECONDS
                ):
                    # A claim deduplicates competing paths for one request, not
                    # every later utterance with the same wording. The memory
                    # poll can lag behind an Agent/tool callback, so expire the
                    # claim window even when the earlier request was observed.
                    self._items.pop(direct.signature, None)
                    direct = None
                item = direct
            if item is None and not text:
                pending = [
                    candidate
                    for candidate in reversed(tuple(self._items.values()))
                    if not candidate.claimed_by
                    and (
                        not target
                        or candidate.target.casefold()
                        == str(target).strip().casefold()[:80]
                    )
                    and current_time - candidate.observed_at
                    <= _QUERY_DEDUP_SECONDS
                ]
                if len(pending) == 1:
                    item = pending[0]
            if item is None and text:
                signature = self.signature(text, target)
                item = QueryClaim(
                    signature=signature,
                    text=" ".join(str(text).split())[:240],
                    target=str(target or "").strip()[:80],
                    source_timestamp=0.0,
                    observed_at=current_time,
                )
                self._items[signature] = item
            if item is None or item.claimed_by:
                return None
            item.claimed_by = str(owner or "unknown")[:24]
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
            if item is None or item.claimed_by != str(owner or ""):
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
            return current is claim and current.claimed_by == str(owner or "")

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
