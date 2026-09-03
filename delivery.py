from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import GameSnapshot

_SEMANTICALLY_VOLATILE_KEYS = frozenset({"observed_at", "revision"})
LIVE_STATE_WIRE_FORMAT = "hearthstone_live_segment_v2"


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
            "format": LIVE_STATE_WIRE_FORMAT,
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

    def _is_valid(self, valid: Callable[[], bool] | None) -> bool:
        if valid is None:
            return True
        try:
            return bool(valid())
        except Exception as exc:
            self._logger.warning(
                "Hearthstone live-state validity check failed code=%s",
                type(exc).__name__,
            )
            return False

    def _normalize_segments(
        self,
        built: Any,
    ) -> tuple[tuple[str, str], ...]:
        raw_segments = tuple(built)
        if not raw_segments:
            return ()

        segments: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw_segments:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("live-state segment must be a name/text pair")
            name, text = item
            if not isinstance(name, str) or not isinstance(text, str):
                raise ValueError("live-state segment name and text must be strings")
            if (
                not name
                or name != name.strip()
                or len(name) > 80
                or any(not (character.islower() or character.isdigit() or character == "_") for character in name)
            ):
                raise ValueError("live-state segment name is invalid")
            if name in seen:
                raise ValueError("live-state segment names must be unique")
            if not text.strip():
                raise ValueError("live-state segment text is empty")
            if len(text.encode("utf-8")) > self._max_prompt_bytes:
                raise ValueError("live-state segment exceeds max_prompt_bytes")
            seen.add(name)
            segments.append((name, text))

        if segments[0][0] != "core":
            raise ValueError("live-state core segment must be first")
        return tuple(segments)

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
                if self._cursor == cursor:
                    self._cursor = PublishedState(
                        target=cursor.target,
                        game_number=cursor.game_number,
                        fingerprint=cursor.fingerprint,
                        segments=cursor.segments,
                        published_at=cursor.published_at,
                        snapshot=cursor.snapshot,
                        complete=False,
                    )
                return False
        if self._cursor == cursor:
            self._cursor = None
        return True

    def _invalidate_publish(
        self,
        *,
        cursor: PublishedState | None,
        target: str,
        game_number: int,
        fingerprint: str,
        snapshot: GameSnapshot,
        published_at: float,
        segments: tuple[str, ...],
        reason: str,
    ) -> bool:
        owned_segments = tuple(
            dict.fromkeys(
                (
                    *(cursor.segments if cursor is not None else ()),
                    *segments,
                )
                or ("core",)
            )
        )
        invalidated = PublishedState(
            target=target,
            game_number=game_number,
            fingerprint=fingerprint,
            segments=owned_segments,
            published_at=published_at,
            snapshot=snapshot,
            complete=False,
        )
        self._cursor = invalidated
        return self._expire_cursor(invalidated, reason=reason)

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
        with self._lock:
            cursor = self._cursor
            if not self._is_valid(valid):
                if cursor is not None:
                    self._expire_cursor(cursor, reason="delivery_invalidated")
                return False

            try:
                fingerprint = semantic_snapshot_fingerprint(snapshot)
            except Exception as exc:
                self._logger.warning(
                    "Hearthstone live-state fingerprint failed code=%s",
                    type(exc).__name__,
                )
                if cursor is not None:
                    self._expire_cursor(cursor, reason="serialization_failed")
                return False

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
                segments = self._normalize_segments(built)
            except Exception as exc:
                self._logger.warning(
                    "Hearthstone live-state serialization failed code=%s",
                    type(exc).__name__,
                )
                if cursor is not None:
                    self._expire_cursor(cursor, reason="serialization_failed")
                return False

            segment_names = tuple(name for name, _text in segments)
            if not segment_names:
                if cursor is not None:
                    self._expire_cursor(cursor, reason="serialization_empty")
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
                            if self._cursor == cursor:
                                self._cursor = PublishedState(
                                    target=cursor.target,
                                    game_number=cursor.game_number,
                                    fingerprint=cursor.fingerprint,
                                    segments=cursor.segments,
                                    published_at=cursor.published_at,
                                    snapshot=cursor.snapshot,
                                    complete=False,
                                )
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

            for segment, text in segments:
                if not self._is_valid(valid):
                    if delivered_segments or cursor is not None:
                        self._invalidate_publish(
                            cursor=cursor,
                            target=clean_target,
                            game_number=snapshot.game_number,
                            fingerprint=fingerprint,
                            snapshot=snapshot,
                            published_at=current_time,
                            segments=segment_names,
                            reason="delivery_invalidated",
                        )
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
                        self._invalidate_publish(
                            cursor=cursor,
                            target=clean_target,
                            game_number=snapshot.game_number,
                            fingerprint=fingerprint,
                            snapshot=snapshot,
                            published_at=current_time,
                            segments=segment_names,
                            reason="partial_publish",
                        )
                    return False
                delivered_segments.append(segment)

            if not self._is_valid(valid):
                self._invalidate_publish(
                    cursor=cursor,
                    target=clean_target,
                    game_number=snapshot.game_number,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    published_at=current_time,
                    segments=segment_names,
                    reason="delivery_invalidated",
                )
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



__all__ = [
    "LiveStatePublisher",
    "PublishedState",
    "push_was_submitted",
    "semantic_snapshot_fingerprint",
]
