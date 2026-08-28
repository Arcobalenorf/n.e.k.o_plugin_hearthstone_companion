from __future__ import annotations

import argparse
import importlib
import json
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hearthstone_companion_probe"


def _load_package() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = str(PROJECT_ROOT / "__init__.py")
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(PROJECT_ROOT)]
    sys.modules[PACKAGE_NAME] = package


_load_package()

_commentary = importlib.import_module(
    f"{PACKAGE_NAME}.commentary"
)
build_atomic_live_state_segment = _commentary.build_atomic_live_state_segment
PowerLogParser = importlib.import_module(f"{PACKAGE_NAME}.powerlog").PowerLogParser
PowerLogLocator = importlib.import_module(f"{PACKAGE_NAME}.tailer").PowerLogLocator


def _card_summary(card: Any) -> dict[str, Any]:
    raw_keywords = getattr(card, "keywords", {}) or {}
    if isinstance(raw_keywords, dict):
        keywords = sorted(
            key for key, enabled in raw_keywords.items() if enabled is True
        )
    elif isinstance(raw_keywords, (list, tuple)):
        keywords = sorted(str(value) for value in raw_keywords)
    else:
        keywords = []
    return {
        "card_id": str(getattr(card, "card_id", "") or ""),
        "position": getattr(card, "position", getattr(card, "zone_position", None)),
        "type": getattr(card, "card_type", None),
        "cost": getattr(card, "current_cost", getattr(card, "cost", None)),
        "attack": getattr(card, "attack", None),
        "health": getattr(card, "health", None),
        "tier": getattr(card, "tier", None),
        "golden": getattr(card, "premium", None),
        "keywords": keywords,
    }


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": snapshot.mode,
        "phase": snapshot.phase,
        "match_id": snapshot.game_number,
        "round": snapshot.round,
        "action_turn": snapshot.turn,
        "active_side": snapshot.active_side,
        "result": snapshot.result,
    }
    if snapshot.constructed is not None:
        result["constructed"] = {
            "player_board": [_card_summary(card) for card in snapshot.constructed.player.board],
            "opponent_board": [
                _card_summary(card) for card in snapshot.constructed.opponent.board
            ],
            "known_hand_count": len(snapshot.constructed.player.known_hand),
            "hand_identities_complete": snapshot.constructed.player.hand_identities_complete,
        }
    if snapshot.battlegrounds is not None:
        bg = snapshot.battlegrounds
        result["battlegrounds"] = {
            "round": bg.round,
            "gold": bg.gold,
            "tier": bg.tavern_tier,
            "frozen": bg.frozen,
            "refresh_cost": bg.refresh_cost,
            "upgrade_cost": bg.upgrade_cost,
            "shop": [_card_summary(card) for card in bg.shop],
            "hand": [_card_summary(card) for card in bg.hand],
            "warband": [_card_summary(card) for card in bg.warband],
            "areas": {
                name: {
                    "complete": area.complete,
                    "revision": area.revision,
                    "round": area.round,
                    "phase": area.phase,
                }
                for name, area in bg.areas.items()
            },
        }
    return result


def _context_summary(snapshot: Any | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    segments = build_atomic_live_state_segment(
        snapshot,
        observed_at=1_780_000_000.0,
        max_prompt_bytes=4096,
    )
    encoded_lengths = [len(text.encode("utf-8")) for _name, text in segments]
    return {
        "mode": snapshot.mode,
        "match_id": snapshot.game_number,
        "atomic_context_count": len(segments),
        "context_names": [name for name, _text in segments],
        "max_context_bytes": max(encoded_lengths, default=0),
        "total_context_bytes": sum(encoded_lengths),
    }


def replay(path: Path, *, label: str) -> dict[str, Any]:
    parser = PowerLogParser()
    lines_seen = 0
    modes: list[str] = []
    last_revision = -1
    last_active = None
    richest_recruit = None
    richest_recruit_score: tuple[int, int, int] | None = None
    event_counts: Counter[str] = Counter()
    lifecycle_events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines_seen += 1
            events = parser.feed_line(line, now=1_780_000_000.0)
            event_counts.update(event.kind for event in events)
            for event in events:
                if event.kind not in {
                    "game_started",
                    "game_ended",
                    "battlegrounds_game_ended",
                }:
                    continue
                event_snapshot = parser.snapshot()
                lifecycle_events.append(
                    {
                        "kind": event.kind,
                        "match_id": event_snapshot.game_number,
                        "mode": event_snapshot.mode,
                        "phase": event_snapshot.phase,
                        "result": event.details.get("result"),
                        "placement": event.details.get("placement"),
                    }
                )
            revision = int(getattr(parser, "_public_revision", 0))
            if revision == last_revision:
                continue
            last_revision = revision
            snapshot = parser.snapshot()
            if not modes or modes[-1] != snapshot.mode:
                modes.append(snapshot.mode)
            if snapshot.game_number > 0 and snapshot.phase not in {"idle", "ended", "spectator"}:
                last_active = snapshot
            if snapshot.battlegrounds is not None and snapshot.phase == "recruit":
                bg = snapshot.battlegrounds
                shop_area = bg.areas.get("shop")
                if (
                    shop_area is None
                    or not shop_area.complete
                    or shop_area.round != bg.round
                    or shop_area.phase != "recruit"
                ):
                    continue
                evidence_score = (
                    len(bg.shop) * 100
                    + len(bg.hand) * 10
                    + len(bg.warband)
                    + sum(
                        value is not None
                        for value in (bg.gold, bg.refresh_cost, bg.upgrade_cost)
                    )
                )
                score = (evidence_score, bg.round, shop_area.revision)
                if richest_recruit_score is None or score > richest_recruit_score:
                    richest_recruit = snapshot
                    richest_recruit_score = score
    final = parser.snapshot()
    selected = last_active or richest_recruit
    context = _context_summary(selected)
    return {
        "label": label,
        "bytes_read": path.stat().st_size,
        "lines_seen": lines_seen,
        "mode_transitions": modes,
        "event_counts": dict(sorted(event_counts.items())),
        "lifecycle_events": lifecycle_events,
        "final": _snapshot_summary(final),
        "last_active": _snapshot_summary(last_active) if last_active is not None else None,
        "richest_recruit": (
            _snapshot_summary(richest_recruit) if richest_recruit is not None else None
        ),
        "live_context": context,
        "last_active_context": _context_summary(last_active),
        "richest_recruit_context": _context_summary(richest_recruit),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    resolved = PowerLogLocator().resolve()
    result = {
        "auto_discovery": {
            "resolved": resolved is not None,
            "is_requested_log": bool(
                resolved is not None
                and any(resolved.resolve() == path.resolve() for path in args.logs)
            ),
        },
        "replays": [
            replay(path.resolve(), label=f"log-{index}")
            for index, path in enumerate(args.logs, start=1)
        ],
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
