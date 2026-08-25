from __future__ import annotations

import argparse
import json
import sys
import types
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

from hearthstone_companion_probe.commentary import build_live_state_segments  # noqa: E402
from hearthstone_companion_probe.powerlog import PowerLogParser  # noqa: E402
from hearthstone_companion_probe.tailer import PowerLogLocator  # noqa: E402


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


def replay(path: Path, *, label: str) -> dict[str, Any]:
    parser = PowerLogParser()
    lines_seen = 0
    modes: list[str] = []
    last_revision = -1
    last_active = None
    richest_recruit = None
    richest_recruit_score = -1
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines_seen += 1
            parser.feed_line(line, now=1_780_000_000.0)
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
                score = len(bg.shop) * 100 + len(bg.hand) * 10 + len(bg.warband)
                score += sum(value is not None for value in (bg.gold, bg.refresh_cost, bg.upgrade_cost))
                if score > richest_recruit_score:
                    richest_recruit = snapshot
                    richest_recruit_score = score
    final = parser.snapshot()
    selected = richest_recruit or last_active
    context = None
    if selected is not None:
        segments = build_live_state_segments(
            selected,
            observed_at=1_780_000_000.0,
            max_prompt_bytes=900,
        )
        encoded_lengths = [len(text.encode("utf-8")) for _name, text in segments]
        context = {
            "mode": selected.mode,
            "match_id": selected.game_number,
            "segment_count": len(segments),
            "segment_names": [name for name, _text in segments],
            "max_segment_bytes": max(encoded_lengths, default=0),
            "total_bytes": sum(encoded_lengths),
        }
    return {
        "label": label,
        "bytes_read": path.stat().st_size,
        "lines_seen": lines_seen,
        "mode_transitions": modes,
        "final": _snapshot_summary(final),
        "last_active": _snapshot_summary(last_active) if last_active is not None else None,
        "richest_recruit": (
            _snapshot_summary(richest_recruit) if richest_recruit is not None else None
        ),
        "live_context": context,
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
