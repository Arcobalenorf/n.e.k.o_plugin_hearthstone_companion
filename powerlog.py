from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import (
    BattlegroundsAreaSnapshot,
    BattlegroundsCardSnapshot,
    BattlegroundsChoiceSnapshot,
    BattlegroundsEconomySnapshot,
    BattlegroundsHeroChoiceSnapshot,
    BattlegroundsPlayerSnapshot,
    BattlegroundsSnapshot,
    ChoiceSnapshot,
    ConstructedCardSnapshot,
    ConstructedHeroSnapshot,
    ConstructedSideSnapshot,
    ConstructedSnapshot,
    Entity,
    GameEvent,
    GameSnapshot,
    SideSnapshot,
)

_FULL_ENTITY_RE = re.compile(r"\bFULL_ENTITY - (?:Creating|Updating) ID=(\d+)\s+CardID=([^\s]*)")
_FULL_ENTITY_REF_RE = re.compile(
    r"\bFULL_ENTITY - Updating (?:Entity=)?(.+?)\s+CardID=([^\s]*)"
)
_SHOW_ENTITY_RE = re.compile(r"\b(SHOW_ENTITY|CHANGE_ENTITY) - Updating Entity=(.+?)\s+CardID=([^\s]*)")
_HIDE_ENTITY_RE = re.compile(r"\bHIDE_ENTITY - Entity=(.+?)(?:\s+tag=|$)")
_TAG_CHANGE_RE = re.compile(r"\bTAG_CHANGE Entity=(.+?)\s+tag=([^\s]+)\s+value=(.*?)\s*$")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)tag=([^\s]+)\s+value=(.*?)\s*$")
_INLINE_TAG_LINE_RE = re.compile(r"^\s*tag=[^\s]+\s+value=.*?\s*$")
_BLOCK_START_RE = re.compile(r"\bBLOCK_START BlockType=([^\s]+)\s+Entity=(.+?)(?:\s+EffectCardId=|\s*$)")
_PLAYER_RE = re.compile(r"\bPlayer EntityID=(\d+) PlayerID=(\d+)\b")
_GAME_ENTITY_RE = re.compile(r"\bGameEntity EntityID=(\d+)\b")
_ENTITY_ID_RE = re.compile(r"(?:^|\s)id=(\d+)(?:\s|\]|$)")
_CARD_ID_RE = re.compile(r"(?:^|\s)cardId=([^\s\]]*)")
_CONTROLLER_RE = re.compile(r"(?:^|\s)player=(\d+)(?:\s|\]|$)")
_ZONE_RE = re.compile(r"(?:^|\s)zone=([^\s\]]+)")
_NAME_RE = re.compile(r"(?:entityName|name)=(.*?)\s+id=\d+")
_GAME_TYPE_RE = re.compile(r"\bGameType=(GT_[A-Z0-9_]+)")
_GAME_INFO_PLAYER_RE = re.compile(
    r"\bGameState\.DebugPrintGame\(\)\s+-\s+PlayerID=(\d+),\s*PlayerName=(.*?)\s*$"
)
_CHOICE_HEADER_RE = re.compile(
    r"\bGameState\.DebugPrintEntityChoices\(\)\s+-\s+id=(\d+)\s+Player=(.*?)\s+"
    r"TaskList=.*?\s+ChoiceType=([^\s]+)\s+CountMin=(\d+)\s+CountMax=(\d+)\s*$"
)
_CHOICE_SOURCE_RE = re.compile(
    r"\bGameState\.DebugPrintEntityChoices\(\)\s+-\s+\s*Source=(.*?)\s*$"
)
_CHOICE_ENTITY_RE = re.compile(
    r"\bGameState\.DebugPrintEntityChoices\(\)\s+-\s+\s*Entities\[\d+\]=(.*?)\s*$"
)
_CHOSEN_HEADER_RE = re.compile(
    r"\bGameState\.DebugPrintEntitiesChosen\(\)\s+-\s+id=(\d+)\s+Player=.*?\s+"
    r"EntitiesCount=\d+\s*$"
)
_LOG_SOURCE_RE = re.compile(r"\b(GameState|PowerTaskList)\.DebugPrint(Power|Game)\(\)\s+-\s")
_GAMESTATE_STATIC_ENTITY_RE = re.compile(
    r"\b(?:GameEntity EntityID=|Player EntityID=|FULL_ENTITY - (?:Creating|Updating))"
)
_GAMESTATE_INLINE_TAG_RE = re.compile(
    r"\bGameState\.DebugPrintPower\(\)\s+-\s+\s*tag=[^\s]+\s+value="
)

_CARD_TYPES = {
    "1": "GAME",
    "2": "PLAYER",
    "3": "HERO",
    "4": "MINION",
    "5": "SPELL",
    "6": "ENCHANTMENT",
    "7": "WEAPON",
    "10": "HERO_POWER",
    "11": "LOCATION",  # legacy logs
    "39": "LOCATION",
    "42": "BATTLEGROUND_SPELL",
}
_ZONES = {
    "0": "INVALID",
    "1": "PLAY",
    "2": "DECK",
    "3": "HAND",
    "4": "GRAVEYARD",
    "5": "REMOVEDFROMGAME",
    "6": "SETASIDE",
    "7": "SECRET",
}
_PUBLIC_ZONES = frozenset({"PLAY", "GRAVEYARD", "REMOVEDFROMGAME"})
_END_RESULTS = frozenset({"WON", "LOST", "TIED", "CONCEDED"})
_SPECTATOR_START_MARKERS = ("Start Spectator Game", "Begin Spectating 1st player", "Begin Spectating 2nd player")
_SPECTATOR_END_MARKERS = ("End Spectator Mode", "End Spectator Game")
_BATTLEGROUNDS_GAME_TYPES = frozenset(
    {
        "GT_BATTLEGROUNDS",
        "GT_BATTLEGROUNDS_FRIENDLY",
        "GT_BATTLEGROUNDS_AI_VS_AI",
        "GT_BATTLEGROUNDS_PLAYER_VS_AI",
        "GT_BATTLEGROUNDS_DUO",
        "GT_BATTLEGROUNDS_DUO_VS_AI",
        "GT_BATTLEGROUNDS_DUO_FRIENDLY",
        "GT_BATTLEGROUNDS_DUO_AI_VS_AI",
        "GT_BATTLEGROUNDS_DUO_1_PLAYER_VS_AI",
    }
)
_BATTLEGROUNDS_DUO_GAME_TYPES = frozenset(value for value in _BATTLEGROUNDS_GAME_TYPES if "DUO" in value)
_CONSTRUCTED_FORMATS = {
    "GT_RANKED_STANDARD": "standard",
    "GT_CASUAL_STANDARD": "standard",
    "GT_RANKED_WILD": "wild",
    "GT_CASUAL_WILD": "wild",
    "GT_RANKED_TWIST": "twist",
    "GT_TWIST": "twist",
    "GT_ARENA": "arena",
    "GT_DRAFT": "arena",
    "GT_TAVERNBRAWL": "tavern_brawl",
    "GT_FSG_BRAWL": "tavern_brawl",
}
_CONSTRUCTED_VARIANTS = {
    "GT_RANKED": "ranked",
    "GT_RANKED_STANDARD": "ranked",
    "GT_RANKED_WILD": "ranked",
    "GT_RANKED_TWIST": "ranked",
    "GT_TWIST": "ranked",
    "GT_CASUAL": "casual",
    "GT_CASUAL_STANDARD": "casual",
    "GT_CASUAL_WILD": "casual",
    "GT_VS_FRIEND": "friendly",
    "GT_FRIENDLY": "friendly",
    "GT_VS_AI": "practice",
    "GT_TUTORIAL": "practice",
    "GT_ARENA": "arena",
    "GT_DRAFT": "arena",
    "GT_TAVERNBRAWL": "tavern_brawl",
    "GT_TB_1P_VS_AI": "tavern_brawl",
    "GT_TB_2P_COOP": "tavern_brawl",
    "GT_FSG_BRAWL": "tavern_brawl",
    "GT_FSG_BRAWL_1P_VS_AI": "tavern_brawl",
    "GT_FSG_BRAWL_2P_COOP": "tavern_brawl",
}
_CONSTRUCTED_KEYWORD_TAGS = (
    ("TAUNT", "taunt"),
    ("DIVINE_SHIELD", "divine_shield"),
    ("STEALTH", "stealth"),
    ("WINDFURY", "windfury"),
    ("MEGA_WINDFURY", "mega_windfury"),
    ("POISONOUS", "poisonous"),
    ("LIFESTEAL", "lifesteal"),
    ("REBORN", "reborn"),
    ("RUSH", "rush"),
    ("CHARGE", "charge"),
    ("DEATHRATTLE", "deathrattle"),
    ("BATTLECRY", "battlecry"),
    ("ELUSIVE", "elusive"),
)
_CONSTRUCTED_STATE_TAGS = (
    ("EXHAUSTED", "exhausted"),
    ("FROZEN", "frozen"),
    ("IMMUNE", "immune"),
    ("SILENCED", "silenced"),
    ("DORMANT", "dormant"),
)
_CONSTRUCTED_PLAYER_TAGS = frozenset(
    {
        "COMBO_ACTIVE",
        "CURRENT_PLAYER",
        "FATIGUE",
        "MULLIGAN_STATE",
        "NUM_CARDS_DRAWN_THIS_TURN",
        "NUM_CARDS_PLAYED_THIS_TURN",
        "NUM_FRIENDLY_MINIONS_THAT_ATTACKED_THIS_TURN",
        "NUM_MINIONS_PLAYED_THIS_TURN",
        "NUM_OPTIONS_PLAYED_THIS_TURN",
        "NUM_RESOURCES_SPENT_THIS_GAME",
        "NUM_SPELLS_PLAYED_THIS_GAME",
        "NUM_TURNS_IN_PLAY",
        "NUM_TURNS_LEFT",
        "OVERLOAD_LOCKED",
        "OVERLOAD_OWED",
        "PLAYSTATE",
        "RESOURCES",
        "RESOURCES_USED",
        "TEMP_RESOURCES",
        "TIMEOUT",
        "TURN",
    }
)
_CONSTRUCTED_PLAYER_ALIAS_TAGS = frozenset(
    {
        "CURRENT_PLAYER",
        "FATIGUE",
        "MULLIGAN_STATE",
        "OVERLOAD_LOCKED",
        "OVERLOAD_OWED",
        "PLAYSTATE",
        "RESOURCES",
        "RESOURCES_USED",
        "TEMP_RESOURCES",
        "TIMEOUT",
        "TURN",
    }
)
_CONSTRUCTED_PLAYER_WEAK_TAGS = frozenset(
    {
        "NUM_TURNS_LEFT",
        "PLAYSTATE",
        "TIMEOUT",
    }
)
_CONSTRUCTED_PLAYER_STRONG_TAGS = _CONSTRUCTED_PLAYER_TAGS - _CONSTRUCTED_PLAYER_WEAK_TAGS
_BATTLEGROUNDS_KEYWORD_TAGS = (
    ("TAUNT", "taunt"),
    ("DIVINE_SHIELD", "divine_shield"),
    ("REBORN", "reborn"),
    ("POISONOUS", "poisonous"),
    ("VENOMOUS", "venomous"),
    ("STEALTH", "stealth"),
    ("WINDFURY", "windfury"),
    ("MEGA_WINDFURY", "mega_windfury"),
    ("DEATHRATTLE", "deathrattle"),
    ("BATTLECRY", "battlecry"),
    ("MAGNETIC", "magnetic"),
    ("ELUSIVE", "elusive"),
)
_BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS = frozenset(
    {
        "FROZEN",
        "PREMIUM",
        *(tag for tag, _public_name in _BATTLEGROUNDS_KEYWORD_TAGS),
    }
)
_CARD_IDENTITY_TAGS = frozenset(
    {
        "CARDTYPE",
        "COST",
        "BACON_OVERRIDE_BG_COST",
        "INTERACTABLE_OBJECT_COST",
        "ATK",
        "HEALTH",
        "DAMAGE",
        "DURABILITY",
        "TECH_LEVEL",
        "BACON_CARD_TIER",
        *(tag for tag, _public_name in _CONSTRUCTED_KEYWORD_TAGS),
        *(tag for tag, _public_name in _CONSTRUCTED_STATE_TAGS),
        *_BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS,
    }
)
_BATTLEGROUNDS_REFRESH_COST_TAGS = (
    "BACON_REFRESH_COST",
    "BACON_REROLL_COST",
    "REFRESH_COST",
    "REROLL_COST",
    "ROLL_COST",
)
_BATTLEGROUNDS_UPGRADE_COST_TAGS = (
    "BACON_UPGRADE_COST",
    "PLAYER_TECH_LEVEL_UP_COST",
    "TECH_LEVEL_UP_COST",
    "TAVERN_UPGRADE_COST",
    "UPGRADE_COST",
)
_BATTLEGROUNDS_HINT_TAGS = frozenset(
    {
        "BACON_DUMMY_PLAYER",
        "BACON_HERO_CAN_BE_DRAFTED",
        "PLAYER_TECH_LEVEL",
        "PLAYER_LEADERBOARD_PLACE",
        "NEXT_OPPONENT_PLAYER_ID",
        "BACON_DUO_TEAM_ID",
        "BACON_TRINKETS_ACTIVE",
        "BACON_QUESTS_ACTIVE",
        "BACON_GLOBAL_ANOMALY_DBID",
        "BACON_BUDDY_ENABLED",
        "2022",
        "3533",
    }
)
_MODE_EVIDENCE_UNKNOWN = 0
_MODE_EVIDENCE_HEURISTIC = 1
_MODE_EVIDENCE_STRONG_TAG = 2
_MODE_EVIDENCE_GAME_TYPE = 3
_BATTLEGROUNDS_CARD_TYPES = frozenset({"MINION", "BATTLEGROUND_SPELL"})
_BATTLEGROUNDS_CONTROL_EVENTS = frozenset(
    {
        "battlegrounds_detected",
        "battlegrounds_round",
        "battlegrounds_recruit_started",
        "battlegrounds_combat_started",
        "battlegrounds_combat_result",
        "battlegrounds_hero_selected",
        "battlegrounds_tavern_upgraded",
        "battlegrounds_triple",
        "battlegrounds_game_ended",
        "game_ended",
    }
)
_BATTLEGROUNDS_UI_LABELS = frozenset({"drag to buy", "drag to sell", "drag to freeze"})
_BATTLEGROUNDS_UI_CARD_IDS = frozenset(
    {
        "tb_baconshop_dragbuy",
        "tb_baconshop_dragsell",
        "tb_baconshop_dragfreeze",
        "tb_baconshop_fxwatcher",
        "bacon_tagtransferplayere",
    }
)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any, *, limit: int = 100) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]


def _normalize_zone(value: Any) -> str:
    normalized = _clean(value, limit=40).upper()
    return _ZONES.get(normalized, normalized)


def _normalize_card_type(value: Any) -> str:
    normalized = _clean(value, limit=40).upper()
    return _CARD_TYPES.get(normalized, normalized)


@dataclass(slots=True)
class _EntityRef:
    entity_id: int | None = None
    card_id: str = ""
    controller: int | None = None
    zone: str = ""
    name: str = ""


@dataclass(slots=True)
class _BlockFrame:
    block_type: str
    entity_id: int | None
    controller: int | None
    started_at: float
    events: list[GameEvent] = field(default_factory=list)


@dataclass(slots=True)
class _ChoiceFrame:
    choice_id: int
    player_key: bytes | None
    controller: int | None
    choice_type: str
    count_min: int
    count_max: int
    source_entity_id: int | None = None
    option_entity_ids: list[int] = field(default_factory=list)
    observed_at: float = 0.0
    revision: int = 0
    observed_round: int = 0
    observed_phase: str = "unknown"


class PowerLogParser:
    """Incremental, privacy-minimizing parser for Hearthstone Power.log.

    The parser never retains raw lines, account identifiers, player names, or
    hidden opponent card names. Unknown packets and tags are tolerated.
    """

    def __init__(self, *, max_entities: int = 16384) -> None:
        self.max_entities = max(256, int(max_entities))
        self._player_ref_key = secrets.token_bytes(16)
        self.game_number = 0
        self.entities: dict[int, Entity] = {}
        self.player_entities: dict[int, int] = {}
        self.game_entity_id: int | None = None
        self.local_controller: int | None = None
        self.current_controller: int | None = None
        self.current_entity_id: int | None = None
        self._pending_realtime_baseline_entity: Entity | None = None
        self._pending_game_state_baseline_entity: Entity | None = None
        self._pending_game_state_baseline_card_id = ""
        self._game_state_current_entity_id: int | None = None
        self._game_state_current_entity_card_id: str | None = None
        self._pending_game_state_entities: dict[int, Entity] = {}
        self._pending_game_state_player_entities: dict[int, int] = {}
        self._pending_game_state_game_entity_id: int | None = None
        self._player_name_controllers: dict[bytes, int] = {}
        self._pending_player_name_controllers: dict[bytes, int] = {}
        self._task_player_name_controllers: dict[bytes, int] = {}
        self._choices: dict[int, _ChoiceFrame] = {}
        self._current_choice_id: int | None = None
        self._public_revision = 0
        self.turn = 0
        self._constructed_setup_done = False
        self._last_emitted_constructed_turn = 0
        self.phase = "idle"
        self.mode = "unknown"
        self._mode_evidence = _MODE_EVIDENCE_UNKNOWN
        self.game_type = ""
        self.battlegrounds_variant = "solo"
        self.battlegrounds_round = 0
        self.bob_controller: int | None = None
        self.next_opponent_player_id = 0
        self.current_opponent_player_id = 0
        self.last_opponent_player_id = 0
        self.last_opponent_round = 0
        self.result = ""
        self.spectating = False
        self._mulligan_announced = False
        self._block_stack: list[_BlockFrame] = []
        self._recent_cards: deque[dict[str, Any]] = deque(maxlen=8)
        self._observed_boards: dict[
            int, tuple[int, tuple[BattlegroundsCardSnapshot, ...]]
        ] = {}
        self._pending_game_type: str | None = None
        self._game_boundary_pending = False
        self._battlegrounds_hero_selection_complete = False
        self._battlegrounds_result_emitted = False
        self._combat_active_round = 0
        self._combat_result_emitted_round = 0
        self._combat_damage_taken = 0
        self._combat_damage_dealt = 0
        self._combat_bob_stale_entity_ids: set[int] = set()
        self._recruit_bob_stale_entity_ids: set[int] = set()
        self._shop_membership_key: tuple[int, int, str] | None = None
        self._shop_visible_entity_ids: frozenset[int] = frozenset()
        self._empty_shop_observation = BattlegroundsAreaSnapshot()
        self._last_recruit_warband: tuple[BattlegroundsCardSnapshot, ...] = ()
        self._last_recruit_warband_area = BattlegroundsAreaSnapshot()
        self._last_recruit_warband_observed_at = 0.0
        self._last_recruit_warband_revision = 0
        self._explicit_battlegrounds_phase_signal_seen = False
        self._battlegrounds_turn_phase_seen = False
        self._battlegrounds_fallback_phase_state_used = False
        self._battlegrounds_global_turn = 0
        self._battlegrounds_counter_highs: dict[tuple[int, str], int] = {}
        self.entity_capacity_exceeded = False
        self.entities_evicted = 0

    def reset_source(self) -> None:
        self.entities.clear()
        self.player_entities.clear()
        self.game_entity_id = None
        self.local_controller = None
        self.current_controller = None
        self.current_entity_id = None
        self._pending_realtime_baseline_entity = None
        self._pending_game_state_baseline_entity = None
        self._pending_game_state_baseline_card_id = ""
        self._game_state_current_entity_id = None
        self._game_state_current_entity_card_id = None
        self._pending_game_state_entities.clear()
        self._pending_game_state_player_entities.clear()
        self._pending_game_state_game_entity_id = None
        self._player_name_controllers.clear()
        self._pending_player_name_controllers.clear()
        self._task_player_name_controllers.clear()
        self._choices.clear()
        self._current_choice_id = None
        self._public_revision = 0
        self.turn = 0
        self._constructed_setup_done = False
        self._last_emitted_constructed_turn = 0
        self.phase = "idle"
        self.mode = "unknown"
        self._mode_evidence = _MODE_EVIDENCE_UNKNOWN
        self.game_type = ""
        self.battlegrounds_variant = "solo"
        self.battlegrounds_round = 0
        self.bob_controller = None
        self.next_opponent_player_id = 0
        self.current_opponent_player_id = 0
        self.last_opponent_player_id = 0
        self.last_opponent_round = 0
        self.result = ""
        self.spectating = False
        self._mulligan_announced = False
        self._block_stack.clear()
        self._recent_cards.clear()
        self._observed_boards.clear()
        self._pending_game_type = None
        self._game_boundary_pending = False
        self._battlegrounds_hero_selection_complete = False
        self._battlegrounds_result_emitted = False
        self._combat_active_round = 0
        self._combat_result_emitted_round = 0
        self._combat_damage_taken = 0
        self._combat_damage_dealt = 0
        self._combat_bob_stale_entity_ids.clear()
        self._recruit_bob_stale_entity_ids.clear()
        self._shop_membership_key = None
        self._shop_visible_entity_ids = frozenset()
        self._empty_shop_observation = BattlegroundsAreaSnapshot()
        self._last_recruit_warband = ()
        self._last_recruit_warband_area = BattlegroundsAreaSnapshot()
        self._last_recruit_warband_observed_at = 0.0
        self._last_recruit_warband_revision = 0
        self._explicit_battlegrounds_phase_signal_seen = False
        self._battlegrounds_turn_phase_seen = False
        self._battlegrounds_fallback_phase_state_used = False
        self._battlegrounds_global_turn = 0
        self._battlegrounds_counter_highs.clear()
        self.entity_capacity_exceeded = False
        self.entities_evicted = 0

    def feed_lines(self, lines: Iterable[str], *, now: float | None = None) -> list[GameEvent]:
        events: list[GameEvent] = []
        timestamp = time.time() if now is None else float(now)
        for line in lines:
            events.extend(self.feed_line(line, now=timestamp))
        return events

    def feed_line(self, line: str, *, now: float | None = None) -> list[GameEvent]:
        timestamp = time.time() if now is None else float(now)
        events = self._feed_line(line, now=timestamp)
        self._observe_battlegrounds_shop_membership(timestamp)
        return events

    def _feed_line(self, line: str, *, now: float | None = None) -> list[GameEvent]:
        if not isinstance(line, str) or not line or len(line) > 256 * 1024:
            return []
        timestamp = time.time() if now is None else float(now)
        source_match = _LOG_SOURCE_RE.search(line)
        source = source_match.group(1) if source_match else None
        source_kind = source_match.group(2) if source_match else None
        realtime_inline_tag = bool(
            source_match
            and source == "PowerTaskList"
            and source_kind == "Power"
            and _INLINE_TAG_LINE_RE.fullmatch(line[source_match.end() :])
        )
        game_state_inline_tag = bool(
            source_match
            and source == "GameState"
            and source_kind == "Power"
            and _INLINE_TAG_LINE_RE.fullmatch(line[source_match.end() :])
        )
        if (
            source == "PowerTaskList"
            and source_kind == "Power"
            and not realtime_inline_tag
        ):
            self._finalize_pending_realtime_baseline(timestamp)
        if source == "GameState" and source_kind == "Power" and not game_state_inline_tag:
            self._finalize_pending_game_state_baseline(timestamp)
        # PowerTaskList owns real-time entity/task updates. GameState Power is a
        # replay-style mirror, but its CREATE_GAME line is the earliest stable
        # boundary and precedes DebugPrintGame metadata in real logs.
        if source_kind == "Power" and source == "GameState":
            if "CREATE_GAME" in line:
                self._pending_game_type = None
                self._game_boundary_pending = True
                self._clear_game_state_inline_target()
                self._pending_game_state_entities.clear()
                self._pending_game_state_player_entities.clear()
                self._pending_game_state_game_entity_id = None
                self._pending_player_name_controllers.clear()
                self._choices.clear()
                self._current_choice_id = None
                return []
            terminal_match = _TAG_CHANGE_RE.search(line)
            is_terminal_signal = bool(
                not self._game_boundary_pending
                and terminal_match
                and self._is_game_state_terminal_tag(
                    terminal_match.group(2), terminal_match.group(3)
                )
            )
            is_battlegrounds_context = bool(
                self.mode == "battlegrounds"
                or self._pending_game_type in _BATTLEGROUNDS_GAME_TYPES
                or self._game_boundary_pending
            )
            if not is_terminal_signal and (
                not is_battlegrounds_context
                or not (
                    _GAMESTATE_STATIC_ENTITY_RE.search(line)
                    or _GAMESTATE_INLINE_TAG_RE.search(line)
                )
            ):
                self._clear_game_state_inline_target()
                return []

        player_info_match = _GAME_INFO_PLAYER_RE.search(line)
        if player_info_match:
            self._record_player_name(
                int(player_info_match.group(1)),
                player_info_match.group(2),
            )
            return []
        if self._consume_choice_line(line, timestamp):
            return []

        game_type_match = _GAME_TYPE_RE.search(line)
        if game_type_match:
            game_type = game_type_match.group(1)
            if self._game_boundary_pending:
                self._pending_game_type = game_type
                return []
            detected = self._apply_game_type(game_type, timestamp)
            if detected:
                return [detected]

        if any(marker in line for marker in _SPECTATOR_START_MARKERS):
            self.spectating = True
            self.phase = "spectator"
        elif any(marker in line for marker in _SPECTATOR_END_MARKERS):
            self.spectating = False
            if self.phase == "spectator":
                self.phase = "idle"

        # DebugPrintGame is metadata-only. Never let entity-shaped text from
        # this channel enter the Power packet parser.
        if source == "GameState" and source_kind == "Game":
            return []

        if "CREATE_GAME" in line:
            pending_game_type = self._pending_game_type
            pending_entities = dict(self._pending_game_state_entities)
            pending_player_entities = dict(self._pending_game_state_player_entities)
            pending_game_entity_id = self._pending_game_state_game_entity_id
            pending_player_names = dict(self._pending_player_name_controllers)
            self._start_game()
            self._commit_pending_game_state(
                pending_entities,
                pending_player_entities,
                pending_game_entity_id,
                pending_player_names,
            )
            events = [
                GameEvent(
                    "game_started",
                    7,
                    "新对局开始",
                    timestamp,
                    {"game_number": self.game_number},
                )
            ]
            if pending_game_type:
                detected = self._apply_game_type(pending_game_type, timestamp)
                if detected:
                    events.append(detected)
            return events

        match = _GAME_ENTITY_RE.search(line)
        if match:
            entity_id = int(match.group(1))
            if source == "GameState" and self._game_boundary_pending:
                self._pending_game_state_game_entity_id = entity_id
            elif source == "GameState":
                if self.game_entity_id is None:
                    self.game_entity_id = entity_id
            else:
                self.game_entity_id = entity_id
            entity = self._entity_for_source(entity_id, source)
            if entity:
                self._set_static_field(entity, "CARDTYPE", "GAME", source)
                self._touch_entity(entity, timestamp)
            self._set_current_entity(entity_id, source)
            return []

        match = _PLAYER_RE.search(line)
        if match:
            entity_id, player_id = int(match.group(1)), int(match.group(2))
            entity = self._entity_for_source(entity_id, source)
            if entity:
                self._set_static_field(entity, "CARDTYPE", "PLAYER", source)
                self._set_static_field(entity, "CONTROLLER", player_id, source)
                self._set_static_field(entity, "PLAYER_ID", str(player_id), source)
                self._touch_entity(entity, timestamp)
                if source == "GameState" and self._game_boundary_pending:
                    self._pending_game_state_player_entities[player_id] = entity_id
                elif source == "GameState":
                    self.player_entities.setdefault(player_id, entity_id)
                else:
                    self.player_entities[player_id] = entity_id
            self._set_current_entity(entity_id, source)
            return []

        match = _FULL_ENTITY_RE.search(line)
        if match:
            entity_id, card_id = int(match.group(1)), _clean(match.group(2), limit=80)
            entity = self._entity_for_source(entity_id, source)
            game_state_packet_matches = False
            if entity:
                identity_updated = source != "GameState" or (
                    "CARD_ID" not in entity.realtime_fields
                    and not entity.card_id
                    and not entity.visibility_revoked
                )
                if identity_updated:
                    entity.card_id = card_id
                    entity.hidden = not bool(card_id)
                    if source != "GameState":
                        entity.realtime_fields.update({"CARD_ID", "HIDDEN"})
                if entity.card_id and (source != "GameState" or not entity.visibility_revoked):
                    entity.visibility_revoked = False
                game_state_packet_matches = bool(
                    source == "GameState"
                    and self._game_state_packet_identity_matches(entity, card_id)
                )
                if source == "PowerTaskList":
                    self._begin_realtime_baseline(entity)
                elif game_state_packet_matches:
                    self._begin_game_state_baseline(entity, card_id)
                if source != "GameState" or game_state_packet_matches:
                    self._mark_battlegrounds_combat_identity(entity)
                    self._touch_entity(entity, timestamp)
            if source == "GameState":
                if entity is not None and game_state_packet_matches:
                    self._set_game_state_card_packet(entity, card_id)
                else:
                    self._clear_game_state_inline_target()
            else:
                self._set_current_entity(entity_id, source)
            return []

        match = _FULL_ENTITY_REF_RE.search(line)
        if match:
            ref = self._parse_entity_ref(match.group(1))
            card_id = _clean(match.group(2), limit=80)
            entity = (
                self._entity_for_source(ref.entity_id, source)
                if source == "GameState"
                else self._merge_ref(
                    ref,
                    source=source,
                    include_identity=True,
                    timestamp=timestamp,
                )
            )
            if entity:
                identity_updated = source != "GameState" or (
                    "CARD_ID" not in entity.realtime_fields
                    and not entity.card_id
                    and not entity.visibility_revoked
                )
                if identity_updated:
                    entity.card_id = card_id or entity.card_id
                    entity.hidden = not bool(card_id)
                    if source != "GameState":
                        entity.realtime_fields.update({"CARD_ID", "HIDDEN"})
                game_state_packet_matches = bool(
                    source == "GameState"
                    and self._game_state_packet_identity_matches(entity, card_id)
                )
                if game_state_packet_matches:
                    entity = self._merge_ref(
                        ref,
                        source=source,
                        include_identity=False,
                        timestamp=timestamp,
                    )
                    assert entity is not None
                can_enrich_name = bool(
                    source != "GameState"
                    or (
                        not entity.visibility_revoked
                        and "NAME" not in entity.realtime_fields
                        and not entity.name
                        and (not ref.card_id or ref.card_id == entity.card_id)
                        and (not card_id or card_id == entity.card_id)
                    )
                )
                if ref.name and card_id and can_enrich_name:
                    entity.name = ref.name
                    if source != "GameState":
                        entity.realtime_fields.add("NAME")
                if entity.card_id and (source != "GameState" or not entity.visibility_revoked):
                    entity.visibility_revoked = False
                if source == "PowerTaskList":
                    self._begin_realtime_baseline(entity)
                elif game_state_packet_matches:
                    self._begin_game_state_baseline(entity, card_id)
                if source != "GameState" or game_state_packet_matches:
                    self._mark_battlegrounds_combat_identity(entity)
                if source == "GameState":
                    if game_state_packet_matches:
                        self._set_game_state_card_packet(entity, card_id)
                    else:
                        self._clear_game_state_inline_target()
                else:
                    self._set_current_entity(entity.entity_id, source)
            elif source == "GameState":
                self._clear_game_state_inline_target()
            return []

        match = _SHOW_ENTITY_RE.search(line)
        if match:
            opcode = match.group(1)
            ref = self._parse_entity_ref(match.group(2))
            card_id = _clean(match.group(3), limit=80)
            entity = self._merge_ref(
                ref,
                source=source,
                include_identity=opcode != "CHANGE_ENTITY",
                timestamp=timestamp,
            )
            if entity:
                if opcode == "CHANGE_ENTITY":
                    if card_id and card_id != entity.card_id:
                        self._invalidate_card_identity(entity)
                    else:
                        self._invalidate_realtime_baseline(entity)
                can_reveal = not entity.visibility_revoked or (
                    opcode == "SHOW_ENTITY" and bool(card_id)
                )
                if can_reveal:
                    entity.card_id = card_id or entity.card_id
                    entity.revealed = True
                    entity.hidden = False
                    entity.visibility_revoked = False
                    entity.realtime_fields.update(
                        {"CARD_ID", "NAME", "REVEALED", "HIDDEN", "VISIBILITY"}
                    )
                    if ref.name and entity.card_id:
                        entity.name = ref.name
                if opcode == "SHOW_ENTITY":
                    self._infer_local_controller_from_show(entity)
                    if can_reveal and source == "PowerTaskList":
                        self._begin_realtime_baseline(entity)
                if can_reveal:
                    self._mark_battlegrounds_combat_identity(entity)
                self._set_current_entity(entity.entity_id, source)
            return []

        match = _HIDE_ENTITY_RE.search(line)
        if match:
            ref = self._parse_entity_ref(match.group(1))
            entity = self._merge_ref(ref, source=source, timestamp=timestamp)
            if entity:
                self._invalidate_realtime_baseline(entity)
                entity.revealed = False
                entity.hidden = True
                entity.visibility_revoked = True
                entity.name = ""
                entity.realtime_fields.update(
                    {"CARD_ID", "NAME", "REVEALED", "HIDDEN", "VISIBILITY"}
                )
            return []

        match = _BLOCK_START_RE.search(line)
        if match:
            if len(self._block_stack) >= 64:
                self._block_stack.clear()
            ref = self._parse_entity_ref(match.group(2))
            entity = self._merge_ref(ref, source=source, timestamp=timestamp)
            self._block_stack.append(
                _BlockFrame(
                    block_type=_clean(match.group(1), limit=32).upper(),
                    entity_id=entity.entity_id if entity else ref.entity_id,
                    controller=self._controller(entity, ref.controller),
                    started_at=timestamp,
                )
            )
            return []

        if "BLOCK_END" in line:
            return self._end_block(timestamp)

        match = _TAG_CHANGE_RE.search(line)
        if match:
            ref = self._parse_entity_ref(match.group(1))
            if source == "GameState":
                self._clear_game_state_inline_target()
                entity = self._terminal_entity(ref, match.group(2), match.group(1))
                if entity is None:
                    return []
                return self._apply_tag(
                    entity,
                    match.group(2),
                    match.group(3),
                    timestamp,
                    realtime=False,
                )
            entity = self._merge_ref(ref, source=source, timestamp=timestamp)
            if entity is None:
                entity = self._unresolved_tag_entity(match.group(1), match.group(2), match.group(3))
            if entity is None:
                return []
            self._remember_task_player_reference(match.group(1), match.group(2), entity)
            return self._apply_tag(
                entity,
                match.group(2),
                match.group(3),
                timestamp,
                realtime=True,
            )

        match = _INLINE_TAG_RE.search(line)
        inline_entity_id = (
            self._game_state_current_entity_id
            if source_kind == "Power" and source == "GameState"
            else self.current_entity_id
        )
        if match and inline_entity_id is not None:
            entity = self._entities_for_source(source).get(inline_entity_id)
            if entity:
                if source == "GameState":
                    expected_card_id = self._game_state_current_entity_card_id
                    if expected_card_id is not None and entity.card_id != expected_card_id:
                        self._invalidate_game_state_packet(entity)
                        return []
                    self._apply_enrichment_tag(entity, match.group(1), match.group(2))
                    self._touch_entity(entity, timestamp)
                    return []
                return self._apply_tag(
                    entity,
                    match.group(1),
                    match.group(2),
                    timestamp,
                    realtime=True,
                )
        return []

    def snapshot(self) -> GameSnapshot:
        active_side = "unknown"
        constructed_timeline_ready = (
            self.mode != "constructed" or self._constructed_setup_done
        )
        if self.current_controller is not None and constructed_timeline_ready:
            active_side = self._side_name(self.current_controller)
        phase = "spectator" if self.spectating else self.phase
        round_number = (
            (self.turn + 1) // 2
            if self.turn > 0 and constructed_timeline_ready
            else 0
        )
        return GameSnapshot(
            mode=self.mode,
            phase=phase,
            game_number=self.game_number,
            turn=self.turn,
            round=round_number,
            active_side=active_side,
            player=self._side_snapshot(self.local_controller),
            opponent=(
                SideSnapshot()
                if self.mode == "battlegrounds"
                else self._side_snapshot(self._opponent_controller())
            ),
            recent_cards=tuple(dict(item) for item in self._recent_cards),
            result=self.result,
            constructed=(
                self._constructed_snapshot() if self.mode == "constructed" else None
            ),
            battlegrounds=self._battlegrounds_snapshot() if self.mode == "battlegrounds" else None,
            choice=self._constructed_choice_snapshot() if self.mode == "constructed" else None,
        )

    def _start_game(self) -> None:
        next_game_number = self.game_number + 1
        spectating = self.spectating
        self.reset_source()
        self.game_number = next_game_number
        self.spectating = spectating
        self.phase = "spectator" if spectating else "starting"

    def _commit_pending_game_state(
        self,
        entities: dict[int, Entity],
        player_entities: dict[int, int],
        game_entity_id: int | None,
        player_names: dict[bytes, int],
    ) -> None:
        for entity_id, entity in entities.items():
            if len(self.entities) >= self.max_entities:
                self.entity_capacity_exceeded = True
                break
            self.entities[entity_id] = entity
        self.player_entities.update(
            {
                player_id: entity_id
                for player_id, entity_id in player_entities.items()
                if entity_id in self.entities
            }
        )
        if game_entity_id in self.entities:
            self.game_entity_id = game_entity_id
        self._player_name_controllers.update(
            {
                key: controller
                for key, controller in player_names.items()
                if controller in self.player_entities
            }
        )

    def _apply_game_type(self, game_type: str, timestamp: float) -> GameEvent | None:
        normalized = _clean(game_type, limit=80).upper()
        if not normalized.startswith("GT_"):
            return None
        if normalized in _BATTLEGROUNDS_GAME_TYPES:
            self.game_type = normalized
            first_detection = self.mode != "battlegrounds"
            self.mode = "battlegrounds"
            self._mode_evidence = _MODE_EVIDENCE_GAME_TYPE
            self.battlegrounds_variant = (
                "duos" if normalized in _BATTLEGROUNDS_DUO_GAME_TYPES else "solo"
            )
            if first_detection:
                self.phase = "hero_select" if not self.spectating else "spectator"
            self._hydrate_committed_game_state()
            if not first_detection:
                return None
            return GameEvent(
                "battlegrounds_detected",
                7,
                "已进入酒馆战棋",
                timestamp,
                {"variant": self.battlegrounds_variant},
            )
        if normalized not in _CONSTRUCTED_VARIANTS:
            return None
        first_detection = self.mode != "constructed"
        self.game_type = normalized
        self.mode = "constructed"
        self._mode_evidence = _MODE_EVIDENCE_GAME_TYPE
        if self.phase in {"idle", "hero_select"}:
            self.phase = "starting"
        self._hydrate_committed_game_state()
        if not first_detection:
            return None
        return GameEvent(
            "constructed_detected",
            7,
            "已进入普通对战",
            timestamp,
            {
                "variant": _CONSTRUCTED_VARIANTS[normalized],
                "format": _CONSTRUCTED_FORMATS.get(normalized, "unknown"),
            },
        )

    def _hydrate_committed_game_state(self) -> None:
        for player_id, entity_id in self.player_entities.items():
            entity = self.entities.get(entity_id)
            if entity and entity.tag_int("BACON_DUMMY_PLAYER") > 0:
                self.bob_controller = player_id
                break
        if not self.spectating and self.local_controller is None and self.bob_controller is not None:
            candidates = [
                player_id
                for player_id in self.player_entities
                if player_id != self.bob_controller
            ]
            if len(candidates) == 1:
                self._set_local_controller(candidates[0])
        self._reconcile_battlegrounds_hero_selection()
        if self.mode == "constructed" and self._constructed_setup_evidence_complete():
            self._constructed_setup_done = True
            if not self.spectating and self.phase != "ended":
                self.phase = "playing"

    def _constructed_setup_evidence_complete(self) -> bool:
        game_entity = self.entities.get(self.game_entity_id or -1)
        step = str(game_entity.tags.get("STEP") or "").upper() if game_entity else ""
        if step.startswith("MAIN_"):
            return True
        controllers = {
            controller
            for controller in self.player_entities
            if controller > 0 and controller != self.bob_controller
        }
        if len(controllers) < 2:
            return False
        return all(
            (
                player := self.entities.get(self.player_entities.get(controller, -1))
            )
            is not None
            and str(player.tags.get("MULLIGAN_STATE") or "").upper() == "DONE"
            for controller in controllers
        )

    def _complete_constructed_setup(self, timestamp: float) -> list[GameEvent]:
        if self.mode != "constructed" or self._constructed_setup_done:
            return []
        self._constructed_setup_done = True
        if not self.spectating and self.phase != "ended":
            self.phase = "playing"
        return self._emit_constructed_turn_if_ready(timestamp)

    def _emit_constructed_turn_if_ready(self, timestamp: float) -> list[GameEvent]:
        if (
            self.mode != "constructed"
            or not self._constructed_setup_done
            or self.turn <= self._last_emitted_constructed_turn
            or self.current_controller is None
            or self.phase in {"spectator", "ended"}
        ):
            return []
        self._last_emitted_constructed_turn = self.turn
        round_number = (self.turn + 1) // 2
        active_side = self._side_name(self.current_controller)
        return [
            GameEvent(
                "turn_started",
                3,
                f"第{round_number}轮，轮到{self._side_label(active_side)}",
                timestamp,
                {
                    "turn": self.turn,
                    "action_turn": self.turn,
                    "round": round_number,
                    "active_side": active_side,
                },
            )
        ]

    def _entity(self, entity_id: int | None) -> Entity | None:
        if entity_id is None or entity_id <= 0:
            return None
        entity = self.entities.get(entity_id)
        if entity is not None:
            return entity
        if len(self.entities) >= self.max_entities and not self._evict_stale_entity():
            self.entity_capacity_exceeded = True
            return None
        entity = Entity(entity_id=entity_id)
        self.entities[entity_id] = entity
        return entity

    def _next_public_revision(self) -> int:
        self._public_revision += 1
        return self._public_revision

    def _touch_entity(self, entity: Entity | None, timestamp: float) -> None:
        if entity is None:
            return
        entity.last_seen_at = max(entity.last_seen_at, timestamp)
        entity.last_revision = max(entity.last_revision, self._next_public_revision())
        if self.mode == "battlegrounds":
            entity.last_battlegrounds_round = self.battlegrounds_round
            entity.last_battlegrounds_phase = self.phase

    def _record_tag_observation(self, entity: Entity, tag: str) -> None:
        entity.tag_revisions[tag] = entity.last_revision
        entity.tag_observed_at[tag] = entity.last_seen_at
        if self.mode == "battlegrounds":
            entity.tag_battlegrounds_rounds[tag] = self.battlegrounds_round
            entity.tag_battlegrounds_phases[tag] = self.phase

    @staticmethod
    def _drop_entity_tag(
        entity: Entity,
        tag: str,
        *,
        discard_realtime: bool = False,
    ) -> None:
        entity.tags.pop(tag, None)
        entity.tag_revisions.pop(tag, None)
        entity.tag_observed_at.pop(tag, None)
        entity.tag_battlegrounds_rounds.pop(tag, None)
        entity.tag_battlegrounds_phases.pop(tag, None)
        if discard_realtime:
            entity.realtime_fields.discard(tag)

    def _begin_realtime_baseline(self, entity: Entity) -> None:
        self._invalidate_game_state_packet(entity)
        self._pending_realtime_baseline_entity = None
        entity.battlegrounds_realtime_boolean_baseline_complete = False
        entity.battlegrounds_game_state_boolean_baseline_complete = False
        for tag in _BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS:
            self._drop_entity_tag(entity, tag, discard_realtime=True)
        self._pending_realtime_baseline_entity = entity

    def _begin_game_state_baseline(self, entity: Entity, card_id: str) -> None:
        self._pending_game_state_baseline_entity = None
        self._pending_game_state_baseline_card_id = ""
        entity.battlegrounds_game_state_boolean_baseline_complete = False
        for tag in _BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS:
            if tag not in entity.realtime_fields:
                self._drop_entity_tag(entity, tag)
        self._pending_game_state_baseline_entity = entity
        self._pending_game_state_baseline_card_id = card_id

    @staticmethod
    def _game_state_packet_identity_matches(entity: Entity, card_id: str) -> bool:
        return bool(
            card_id
            and card_id == entity.card_id
            and not entity.hidden
            and not entity.visibility_revoked
        )

    def _clear_game_state_inline_target(self) -> None:
        self._game_state_current_entity_id = None
        self._game_state_current_entity_card_id = None

    def _set_game_state_card_packet(self, entity: Entity, card_id: str) -> None:
        self._game_state_current_entity_id = entity.entity_id
        self._game_state_current_entity_card_id = card_id

    def _invalidate_game_state_packet(self, entity: Entity) -> None:
        if self._pending_game_state_baseline_entity is entity:
            self._pending_game_state_baseline_entity = None
            self._pending_game_state_baseline_card_id = ""
        if self._game_state_current_entity_id == entity.entity_id:
            self._clear_game_state_inline_target()

    def _invalidate_realtime_baseline(self, entity: Entity) -> None:
        self._invalidate_game_state_packet(entity)
        if self._pending_realtime_baseline_entity is entity:
            self._pending_realtime_baseline_entity = None
        entity.battlegrounds_realtime_boolean_baseline_complete = False
        entity.battlegrounds_game_state_boolean_baseline_complete = False
        for tag in _BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS:
            self._drop_entity_tag(entity, tag)
        entity.realtime_fields.update(_BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS)

    def _invalidate_card_identity(self, entity: Entity) -> None:
        self._invalidate_realtime_baseline(entity)
        entity.name = ""
        entity.card_type = ""
        for tag in _CARD_IDENTITY_TAGS:
            self._drop_entity_tag(entity, tag, discard_realtime=True)

    def _finalize_pending_realtime_baseline(self, timestamp: float) -> None:
        entity = self._pending_realtime_baseline_entity
        self._pending_realtime_baseline_entity = None
        if (
            entity is None
            or entity.hidden
            or entity.visibility_revoked
            or not entity.card_id
        ):
            return
        for tag in _BATTLEGROUNDS_BOOLEAN_BASELINE_TAGS:
            if tag not in entity.realtime_fields:
                entity.tags.pop(tag, None)
                entity.realtime_fields.add(tag)
        entity.battlegrounds_realtime_boolean_baseline_complete = True
        self._touch_entity(entity, timestamp)

    def _finalize_pending_game_state_baseline(self, timestamp: float) -> None:
        entity = self._pending_game_state_baseline_entity
        expected_card_id = self._pending_game_state_baseline_card_id
        self._pending_game_state_baseline_entity = None
        self._pending_game_state_baseline_card_id = ""
        if (
            entity is None
            or not expected_card_id
            or entity.card_id != expected_card_id
            or entity.hidden
            or entity.visibility_revoked
            or not entity.card_id
        ):
            return
        entity.battlegrounds_game_state_boolean_baseline_complete = True
        self._touch_entity(entity, timestamp)

    def finalize_quiet_packet_baselines(
        self,
        *,
        now: float,
        quiet_seconds: float,
    ) -> bool:
        """Close a tail packet only after no further log line arrived for a while."""
        current_time = float(now)
        threshold = max(0.0, float(quiet_seconds))
        pending = tuple(
            entity
            for entity in (
                self._pending_realtime_baseline_entity,
                self._pending_game_state_baseline_entity,
            )
            if entity is not None
        )
        if not pending or any(
            current_time - entity.last_seen_at < threshold for entity in pending
        ):
            return False
        self._finalize_pending_realtime_baseline(current_time)
        self._finalize_pending_game_state_baseline(current_time)
        return True

    def _touch_choice(self, frame: _ChoiceFrame | None, timestamp: float) -> None:
        if frame is None:
            return
        frame.observed_at = max(frame.observed_at, timestamp)
        frame.revision = max(frame.revision, self._next_public_revision())
        if self.mode == "battlegrounds":
            frame.observed_round = self.battlegrounds_round
            frame.observed_phase = self.phase

    def _entities_for_source(self, source: str | None) -> dict[int, Entity]:
        if source == "GameState" and self._game_boundary_pending:
            return self._pending_game_state_entities
        return self.entities

    def _entity_for_source(self, entity_id: int | None, source: str | None) -> Entity | None:
        if source != "GameState" or not self._game_boundary_pending:
            return self._entity(entity_id)
        if entity_id is None or entity_id <= 0:
            return None
        entity = self._pending_game_state_entities.get(entity_id)
        if entity is not None:
            return entity
        if len(self._pending_game_state_entities) >= self.max_entities:
            self.entity_capacity_exceeded = True
            return None
        entity = Entity(entity_id=entity_id)
        self._pending_game_state_entities[entity_id] = entity
        return entity

    @staticmethod
    def _set_static_field(
        entity: Entity,
        field_name: str,
        value: str | int,
        source: str | None,
    ) -> None:
        realtime = source != "GameState"
        if not realtime and field_name in entity.realtime_fields:
            return
        if field_name == "CARDTYPE":
            if realtime or not entity.card_type:
                entity.card_type = _normalize_card_type(value)
        elif field_name == "CONTROLLER":
            controller = _int(value)
            if controller is not None and (realtime or entity.controller is None):
                entity.controller = controller
        elif field_name == "PLAYER_ID":
            if realtime or field_name not in entity.tags:
                entity.tags[field_name] = _clean(value, limit=40)
        if realtime:
            entity.realtime_fields.add(field_name)

    @staticmethod
    def _apply_enrichment_tag(entity: Entity, raw_tag: str, raw_value: str) -> None:
        tag = _clean(raw_tag, limit=60).upper()
        if tag in entity.realtime_fields or tag in entity.tags:
            return
        value = _clean(raw_value, limit=160)
        if tag == "CONTROLLER":
            controller = _int(value)
            if controller is not None and "CONTROLLER" not in entity.realtime_fields:
                entity.controller = controller
        elif tag == "CARDTYPE":
            value = _normalize_card_type(value)
            if "CARDTYPE" not in entity.realtime_fields:
                entity.card_type = value
        elif tag == "ZONE":
            value = _normalize_zone(value)
            if "ZONE" not in entity.realtime_fields:
                entity.zone = value
                if value in _PUBLIC_ZONES and not entity.hidden:
                    entity.revealed = True
        entity.tags[tag] = value

    def _set_current_entity(self, entity_id: int, source: str | None) -> None:
        if source == "GameState":
            self._game_state_current_entity_id = entity_id
            self._game_state_current_entity_card_id = None
        else:
            self.current_entity_id = entity_id

    @staticmethod
    def _is_game_state_terminal_tag(raw_tag: str, raw_value: str) -> bool:
        tag = _clean(raw_tag, limit=60).upper()
        value = _clean(raw_value, limit=160).upper()
        return (tag == "STATE" and value == "COMPLETE") or (
            tag == "PLAYSTATE" and value in _END_RESULTS
        )

    def _terminal_entity(
        self,
        ref: _EntityRef,
        raw_tag: str,
        raw_ref: str,
    ) -> Entity | None:
        tag = _clean(raw_tag, limit=60).upper()
        entity_id = ref.entity_id
        if tag == "STATE":
            if entity_id is not None:
                entity = self.entities.get(entity_id)
                if self.game_entity_id is not None and entity_id != self.game_entity_id:
                    return None
                if entity is None or entity.card_type != "GAME":
                    return None
                return entity
            if _clean(raw_ref, limit=80).casefold() != "gameentity":
                return None
            entity = self.entities.get(self.game_entity_id or -1)
            return entity or Entity(entity_id=-1, card_type="GAME")
        entity = self.entities.get(entity_id or -1)
        if entity is not None:
            return entity if entity.card_type == "PLAYER" else None
        local_entity = self.entities.get(
            self.player_entities.get(self.local_controller or -1, -1)
        )
        if (
            local_entity is not None
            and entity_id is None
            and self.local_controller is not None
            and ref.controller == self.local_controller
        ):
            return local_entity
        if (
            self.local_controller is not None
            and ref.controller is not None
            and ref.controller == self.local_controller
        ):
            return Entity(entity_id=entity_id or -1, controller=ref.controller)
        return None

    def _set_local_controller(self, controller: int | None) -> None:
        if controller is None or controller <= 0 or self.spectating:
            return
        self.local_controller = controller
        self._reconcile_battlegrounds_hero_selection()

    def _reconcile_battlegrounds_hero_selection(self) -> None:
        if (
            self.mode != "battlegrounds"
            or self.local_controller is None
            or self._battlegrounds_hero_selection_complete
        ):
            return
        if any(
            entity.tags.get("MULLIGAN_STATE", "").upper() == "DONE"
            and self._controller(entity) == self.local_controller
            for entity in self.entities.values()
        ):
            self._battlegrounds_hero_selection_complete = True
            if self.phase not in {"combat", "ended", "spectator"}:
                self.phase = "recruit"

    def _evict_stale_entity(self) -> bool:
        protected = {
            self.game_entity_id,
            self.current_entity_id,
            self._game_state_current_entity_id,
            *self.player_entities.values(),
            *(frame.entity_id for frame in self._block_stack),
        }
        victim: int | None = None
        victim_rank = 99
        for candidate_id, candidate in self.entities.items():
            if candidate_id in protected or self._is_hero(candidate):
                continue
            if candidate.zone in {"GRAVEYARD", "REMOVEDFROMGAME", "INVALID"}:
                rank = 0
            elif candidate.card_type == "ENCHANTMENT":
                rank = 1
            elif candidate.hidden and candidate.zone not in {"PLAY", "HAND", "SECRET"}:
                rank = 2
            else:
                continue
            if rank < victim_rank:
                victim = candidate_id
                victim_rank = rank
                if rank == 0:
                    break
        if victim is None:
            return False
        self.entities.pop(victim, None)
        self.entities_evicted += 1
        return True

    def _player_name_key(self, value: str) -> bytes | None:
        raw = str(value or "").strip()
        if not raw or len(raw) > 256:
            return None
        return hashlib.blake2b(
            raw.encode("utf-8", "replace"),
            key=self._player_ref_key,
            digest_size=16,
        ).digest()

    def _record_player_name(self, controller: int, value: str) -> None:
        if controller <= 0 or controller > 64:
            return
        key = self._player_name_key(value)
        if key is None:
            return
        target = (
            self._pending_player_name_controllers
            if self._game_boundary_pending
            else self._player_name_controllers
        )
        if len(target) < 64 or key in target:
            target[key] = controller

    def _controller_from_player_name(self, value: str) -> tuple[bytes | None, int | None]:
        key = self._player_name_key(value)
        if key is None:
            return None, None
        controller = self._task_player_name_controllers.get(key)
        if controller is None:
            controller = self._player_name_controllers.get(key)
        if controller is None:
            controller = self._pending_player_name_controllers.get(key)
        return key, controller

    def _bare_player_reference_key(self, value: str) -> bytes | None:
        raw = str(value or "").strip()
        if raw.isdigit() or raw.casefold() == "gameentity":
            return None
        if any(
            pattern.search(raw)
            for pattern in (
                _ENTITY_ID_RE,
                _CARD_ID_RE,
                _CONTROLLER_RE,
                _ZONE_RE,
                _NAME_RE,
            )
        ):
            return None
        return self._player_name_key(raw)

    def _remember_task_player_reference(
        self,
        raw_ref: str,
        raw_tag: str,
        entity: Entity,
    ) -> None:
        tag = _clean(raw_tag, limit=60).upper()
        if (
            self.mode != "constructed"
            or tag not in _CONSTRUCTED_PLAYER_STRONG_TAGS
            or entity.card_type != "PLAYER"
        ):
            return
        controller = self._controller(entity)
        if (
            controller is None
            or self.player_entities.get(controller) != entity.entity_id
        ):
            return
        key = self._bare_player_reference_key(raw_ref)
        if key is not None and (len(self._task_player_name_controllers) < 8 or key in self._task_player_name_controllers):
            self._task_player_name_controllers[key] = controller

    def _infer_constructed_player_entity(
        self,
        raw_ref: str,
        raw_tag: str,
    ) -> Entity | None:
        tag = _clean(raw_tag, limit=60).upper()
        if (
            self.mode != "constructed"
            or self.local_controller is None
            or tag in _CONSTRUCTED_PLAYER_WEAK_TAGS
            or tag not in _CONSTRUCTED_PLAYER_ALIAS_TAGS
        ):
            return None
        key = self._bare_player_reference_key(raw_ref)
        if key is None:
            return None
        registered = {
            controller
            for controller, entity_id in self.player_entities.items()
            if controller > 0
            and controller != self.bob_controller
            and (entity := self.entities.get(entity_id)) is not None
            and entity.card_type == "PLAYER"
        }
        if len(registered) != 2:
            return None
        task_controllers = set(self._task_player_name_controllers.values())
        if self.local_controller not in task_controllers:
            return None
        candidates = registered - {self.local_controller}
        if len(candidates) != 1:
            return None
        controller = candidates.pop()
        entity = self.entities.get(self.player_entities.get(controller, -1))
        if entity is None:
            return None
        if len(self._task_player_name_controllers) < 8:
            self._task_player_name_controllers[key] = controller
        return entity

    def _consume_choice_line(self, line: str, timestamp: float) -> bool:
        match = _CHOICE_HEADER_RE.search(line)
        if match:
            choice_id = int(match.group(1))
            player_key, controller = self._controller_from_player_name(match.group(2))
            if len(self._choices) >= 64 and choice_id not in self._choices:
                self._choices.pop(min(self._choices), None)
            self._choices[choice_id] = _ChoiceFrame(
                choice_id=choice_id,
                player_key=player_key,
                controller=controller,
                choice_type=_clean(match.group(3), limit=32).lower() or "unknown",
                count_min=max(0, _int(match.group(4)) or 0),
                count_max=max(0, _int(match.group(5)) or 0),
            )
            self._touch_choice(self._choices[choice_id], timestamp)
            self._current_choice_id = choice_id
            return True

        match = _CHOICE_SOURCE_RE.search(line)
        if match:
            frame = self._choices.get(self._current_choice_id or -1)
            if frame is not None:
                ref = self._parse_entity_ref(match.group(1))
                frame.source_entity_id = ref.entity_id
                self._touch_choice(frame, timestamp)
            return True

        match = _CHOICE_ENTITY_RE.search(line)
        if match:
            frame = self._choices.get(self._current_choice_id or -1)
            if frame is None:
                return True
            if frame.controller is None and frame.player_key is not None:
                frame.controller = self._player_name_controllers.get(frame.player_key)
            if (
                frame.controller is not None
                and self.local_controller is not None
                and frame.controller != self.local_controller
            ):
                return True
            ref = self._parse_entity_ref(match.group(1))
            if frame.controller is None and ref.controller is not None:
                frame.controller = ref.controller
            if ref.entity_id is not None and ref.entity_id not in frame.option_entity_ids:
                frame.option_entity_ids.append(ref.entity_id)
            if frame.controller == self.local_controller and self.local_controller is not None:
                self._merge_ref(ref, source="GameState", timestamp=timestamp)
            self._touch_choice(frame, timestamp)
            return True

        match = _CHOSEN_HEADER_RE.search(line)
        if match:
            choice_id = int(match.group(1))
            self._choices.pop(choice_id, None)
            if self._current_choice_id == choice_id:
                self._current_choice_id = None
            return True
        return "GameState.DebugPrintEntitiesChosen()" in line

    def _parse_entity_ref(self, text: str) -> _EntityRef:
        raw = str(text or "").strip()
        if raw.isdigit():
            return _EntityRef(entity_id=int(raw))
        if raw.casefold() == "gameentity" and self.game_entity_id is not None:
            return _EntityRef(entity_id=self.game_entity_id)
        entity_id_match = _ENTITY_ID_RE.search(raw)
        card_id_match = _CARD_ID_RE.search(raw)
        controller_match = _CONTROLLER_RE.search(raw)
        zone_match = _ZONE_RE.search(raw)
        name_match = _NAME_RE.search(raw)
        mapped_controller: int | None = None
        mapped_entity_id: int | None = None
        if not entity_id_match and not any(
            (card_id_match, controller_match, zone_match, name_match)
        ):
            _key, mapped_controller = self._controller_from_player_name(raw)
            if mapped_controller is not None:
                mapped_entity_id = self.player_entities.get(mapped_controller)
                if mapped_entity_id is None and self._game_boundary_pending:
                    mapped_entity_id = self._pending_game_state_player_entities.get(
                        mapped_controller
                    )
        return _EntityRef(
            entity_id=(
                int(entity_id_match.group(1)) if entity_id_match else mapped_entity_id
            ),
            card_id=_clean(card_id_match.group(1), limit=80) if card_id_match else "",
            controller=(
                int(controller_match.group(1))
                if controller_match
                else mapped_controller
            ),
            zone=_normalize_zone(zone_match.group(1)) if zone_match else "",
            name=_clean(name_match.group(1), limit=80) if name_match else "",
        )

    def _merge_ref(
        self,
        ref: _EntityRef,
        *,
        source: str | None,
        include_identity: bool = True,
        timestamp: float = 0.0,
    ) -> Entity | None:
        realtime = source != "GameState"
        entity = self._entity_for_source(ref.entity_id, source)
        if entity is None:
            return None
        if (
            include_identity
            and ref.card_id
            and not entity.hidden
            and not entity.visibility_revoked
            and (realtime or ("CARD_ID" not in entity.realtime_fields and not entity.card_id))
        ):
            entity.card_id = ref.card_id
            if realtime:
                entity.realtime_fields.add("CARD_ID")
        if ref.controller is not None and (
            (realtime and "CONTROLLER" not in entity.realtime_fields)
            or (not realtime and "CONTROLLER" not in entity.realtime_fields and entity.controller is None)
        ):
            entity.controller = ref.controller
            if realtime:
                entity.realtime_fields.add("CONTROLLER")
        if ref.zone and (
            (realtime and "ZONE" not in entity.realtime_fields)
            or (not realtime and "ZONE" not in entity.realtime_fields and not entity.zone)
        ):
            entity.zone = ref.zone
            if realtime:
                entity.realtime_fields.add("ZONE")
        if (
            include_identity
            and ref.name
            and ref.card_id
            and not entity.hidden
            and not entity.visibility_revoked
            and (realtime or ("NAME" not in entity.realtime_fields and not entity.name))
        ):
            entity.name = ref.name
            if realtime:
                entity.realtime_fields.add("NAME")
        if timestamp > 0:
            self._touch_entity(entity, timestamp)
        return entity

    def _apply_tag(
        self,
        entity: Entity,
        raw_tag: str,
        raw_value: str,
        timestamp: float,
        *,
        realtime: bool = True,
    ) -> list[GameEvent]:
        tag = _clean(raw_tag, limit=60).upper()
        value = _clean(raw_value, limit=160)
        old_value = entity.tags.get(tag)
        if realtime:
            entity.realtime_fields.add(tag)

        if tag == "CONTROLLER":
            controller = _int(value)
            if controller is not None:
                entity.controller = controller
        elif tag == "CARDTYPE":
            entity.card_type = _normalize_card_type(value)
            value = entity.card_type
        elif tag == "ZONE":
            value = _normalize_zone(value)

        entity.tags[tag] = value
        self._touch_entity(entity, timestamp)
        self._record_tag_observation(entity, tag)
        events: list[GameEvent] = []

        strong_battlegrounds_hint = tag in _BATTLEGROUNDS_HINT_TAGS
        weak_battlegrounds_hint = tag.startswith("BACON_")
        hint_evidence = (
            _MODE_EVIDENCE_STRONG_TAG
            if strong_battlegrounds_hint
            else _MODE_EVIDENCE_HEURISTIC
        )
        can_apply_battlegrounds_hint = (
            (strong_battlegrounds_hint or (weak_battlegrounds_hint and self.mode == "unknown"))
            and hint_evidence > self._mode_evidence
        )
        if can_apply_battlegrounds_hint:
            first_detection = self.mode != "battlegrounds"
            self.mode = "battlegrounds"
            self._mode_evidence = hint_evidence
            if first_detection:
                self.mode = "battlegrounds"
                self.phase = "hero_select" if not self.spectating else "spectator"
                self._reconcile_battlegrounds_hero_selection()
                events.append(
                    GameEvent(
                        "battlegrounds_detected",
                        7,
                        "已识别酒馆战棋对局",
                        timestamp,
                        {"variant": self.battlegrounds_variant},
                    )
                )
        if tag == "BACON_DUO_TEAM_ID" and self.mode == "battlegrounds" and (_int(value) or 0) > 0:
            self.battlegrounds_variant = "duos"
        elif self.mode == "unknown" and tag in {"MULLIGAN_STATE", "STEP"}:
            self.mode = "constructed"
            self._mode_evidence = _MODE_EVIDENCE_HEURISTIC

        if tag == "BACON_DUMMY_PLAYER" and str(value).upper() in {"1", "TRUE"}:
            self.bob_controller = self._controller(entity)
            if not self.spectating and self.local_controller is None:
                candidates = [player_id for player_id in self.player_entities if player_id != self.bob_controller]
                if len(candidates) == 1:
                    self._set_local_controller(candidates[0])

        if (
            tag == "MULLIGAN_STATE"
            and value.upper() == "INPUT"
            and not self.spectating
            and self.local_controller is None
        ):
            self._set_local_controller(self._controller(entity))

        if tag == "ZONE":
            old_zone = _normalize_zone(old_value)
            entity.zone = value
            if value in _PUBLIC_ZONES and not entity.hidden:
                entity.revealed = True
            if old_zone == "PLAY" and value == "GRAVEYARD" and self._is_minion(entity):
                side = self._side_name(entity.controller)
                card = self._visible_card_label(entity) or "随从"
                events.append(
                    GameEvent(
                        "minion_left_play",
                        2,
                        f"{self._side_label(side)}的{card}离场",
                        timestamp,
                        {"side": side, "card": card, "entity_id": entity.entity_id},
                    )
                )
            if value == "SECRET" and old_zone != "SECRET":
                side = self._side_name(entity.controller)
                events.append(
                    GameEvent(
                        "secret_entered_play",
                        4,
                        f"{self._side_label(side)}挂上了一个奥秘",
                        timestamp,
                        {"side": side, "secret_count": self._zone_count(entity.controller, "SECRET")},
                    )
                )

        elif tag == "DAMAGE":
            previous, current = _int(old_value), _int(value)
            if previous is not None and current is not None and previous != current and self._is_hero(entity):
                side = self._side_name(entity.controller)
                amount = abs(current - previous)
                health = entity.health
                if current > previous:
                    if self.mode == "battlegrounds":
                        player_id = self._battlegrounds_player_id(entity)
                        if self._is_local_battlegrounds_entity(entity):
                            self._combat_damage_taken += amount
                        elif player_id == self._active_opponent_player_id():
                            self._combat_damage_dealt += amount
                    priority = 8 if side == "player" and health is not None and health <= 10 else 5
                    if self.mode != "battlegrounds" or self._is_local_battlegrounds_entity(entity):
                        events.append(
                            GameEvent(
                                "hero_damaged",
                                priority,
                                f"{self._side_label(side)}英雄受到{amount}点伤害",
                                timestamp,
                                {"side": side, "amount": amount, "health": health, "armor": entity.armor},
                            )
                        )
                else:
                    if self.mode != "battlegrounds" or self._is_local_battlegrounds_entity(entity):
                        events.append(
                            GameEvent(
                                "hero_healed",
                                3,
                                f"{self._side_label(side)}英雄恢复了{amount}点生命",
                                timestamp,
                                {"side": side, "amount": amount, "health": health},
                            )
                        )

        elif tag == "ARMOR":
            previous, current = _int(old_value), _int(value)
            if (
                previous is not None
                and current is not None
                and current < previous
                and self._is_hero(entity)
            ):
                side = self._side_name(entity.controller)
                amount = previous - current
                if self.mode == "battlegrounds":
                    player_id = self._battlegrounds_player_id(entity)
                    if self._is_local_battlegrounds_entity(entity):
                        self._combat_damage_taken += amount
                    elif player_id == self._active_opponent_player_id():
                        self._combat_damage_dealt += amount
                if self.mode != "battlegrounds" or self._is_local_battlegrounds_entity(entity):
                    health = entity.health
                    effective_health = None if health is None else health + current
                    priority = (
                        8
                        if side == "player"
                        and effective_health is not None
                        and effective_health <= 10
                        else 5
                    )
                    events.append(
                        GameEvent(
                            "hero_damaged",
                            priority,
                            f"{self._side_label(side)}英雄护甲承受{amount}点伤害",
                            timestamp,
                            {
                                "side": side,
                                "amount": amount,
                                "health": entity.health,
                                "armor": current,
                            },
                        )
                    )
        elif tag == "TURN":
            turn = _int(value)
            is_global_game_turn = bool(
                turn is not None
                and turn > 0
                and entity.card_type == "GAME"
                and (
                    self.game_entity_id is None
                    or entity.entity_id == self.game_entity_id
                )
            )
            if self.mode == "battlegrounds" and is_global_game_turn:
                if turn > self._battlegrounds_global_turn:
                    fallback_state_used = (
                        not self._battlegrounds_turn_phase_seen
                        and self._battlegrounds_fallback_phase_state_used
                    )
                    fallback_ahead = (
                        not self._battlegrounds_turn_phase_seen and self.turn > turn
                    )
                    self._battlegrounds_global_turn = turn
                    self._battlegrounds_turn_phase_seen = True
                    if fallback_state_used:
                        # A non-GameEntity TURN is only a legacy fallback. Once
                        # the authoritative game turn arrives, discard phase
                        # bookkeeping derived from that fallback even when both
                        # signals carry the same turn number.
                        self.turn = turn
                        if fallback_ahead:
                            self.battlegrounds_round = 0
                        self._discard_battlegrounds_fallback_phase_state()
                        if not self.spectating and self.phase not in {
                            "spectator",
                            "ended",
                        }:
                            self.phase = "unknown"
                    else:
                        self.turn = max(self.turn, turn)
                    self._battlegrounds_fallback_phase_state_used = False
                    events.extend(
                        self._reconcile_battlegrounds_turn_phase(turn, timestamp)
                    )
            elif (
                self.mode == "battlegrounds"
                and not self._battlegrounds_turn_phase_seen
                and turn is not None
                and turn > self.turn
            ):
                self.turn = turn
                self._battlegrounds_fallback_phase_state_used = True
                next_round = (turn + 1) // 2
                if next_round > self.battlegrounds_round:
                    self.battlegrounds_round = next_round
                    events.append(
                        GameEvent(
                            "battlegrounds_round",
                            3,
                            f"酒馆第{self.battlegrounds_round}回合",
                            timestamp,
                            {"turn": turn, "round": self.battlegrounds_round},
                        )
                    )
            elif (
                self.mode != "battlegrounds"
                and turn is not None
                and turn > self.turn
            ):
                self.turn = turn
                if self.mode == "constructed" and self._constructed_setup_done:
                    if self.phase not in {"spectator", "ended"}:
                        self.phase = "playing"
                    events.extend(self._emit_constructed_turn_if_ready(timestamp))

        elif tag == "CURRENT_PLAYER":
            current_value = str(value).upper()
            current_entity_controller = self._controller(entity)
            if current_value in {"1", "TRUE"}:
                self.current_controller = current_entity_controller
                if (
                    self.mode == "battlegrounds"
                    and not self.spectating
                    and not self._battlegrounds_turn_phase_seen
                    and not self._explicit_battlegrounds_phase_signal_seen
                ):
                    if (
                        self.current_controller == self.local_controller
                        and (
                            self._battlegrounds_hero_selection_complete
                            or self.turn > 0
                        )
                    ):
                        previous = self.phase
                        self.phase = "recruit"
                        if previous != self.phase:
                            self._battlegrounds_fallback_phase_state_used = True
                        if previous == "combat":
                            events.extend(self._finish_battlegrounds_combat(timestamp))
                            events.append(
                                GameEvent(
                                    "battlegrounds_recruit_started",
                                    5,
                                    f"第{self.battlegrounds_round}回合开始招募",
                                    timestamp,
                                    {"round": self.battlegrounds_round},
                                )
                            )
                    elif (
                        self.current_controller == self.bob_controller
                        and self.phase == "recruit"
                        and self.battlegrounds_round > 0
                        and self.turn % 2 == 1
                    ):
                        self._battlegrounds_fallback_phase_state_used = True
                        self._cache_recruit_warband()
                        self.phase = "combat"
                        if self._begin_battlegrounds_combat():
                            events.append(
                                GameEvent(
                                    "battlegrounds_combat_started",
                                    6,
                                    f"第{self.battlegrounds_round}回合战斗开始",
                                    timestamp,
                                    {
                                        "round": self.battlegrounds_round,
                                        "opponent_player_id": self.current_opponent_player_id,
                                    },
                                )
                            )
                elif self.mode == "constructed" and self._constructed_setup_done:
                    events.extend(self._emit_constructed_turn_if_ready(timestamp))
            elif (
                current_value in {"0", "FALSE"}
                and current_entity_controller is not None
                and current_entity_controller == self.current_controller
            ):
                self.current_controller = None

        elif tag == "MULLIGAN_STATE":
            if self.mode == "battlegrounds":
                if self._is_local_battlegrounds_entity(entity):
                    selection_complete = value.upper() == "DONE"
                    was_complete = self._battlegrounds_hero_selection_complete
                    if selection_complete:
                        self._battlegrounds_hero_selection_complete = True
                    if self._battlegrounds_hero_selection_complete:
                        if self.phase not in {"combat", "ended", "spectator"}:
                            self.phase = "recruit"
                    else:
                        self.phase = "hero_select"
                    if selection_complete and not was_complete:
                        events.append(
                            GameEvent(
                                "battlegrounds_hero_selected",
                                6,
                                "英雄已选定",
                                timestamp,
                                {},
                            )
                        )
            elif value.upper() in {"INPUT", "DEALING", "DONE"}:
                if not self._constructed_setup_done:
                    self.phase = "mulligan"
                if (
                    value.upper() == "DONE"
                    and self._constructed_setup_evidence_complete()
                ):
                    events.extend(self._complete_constructed_setup(timestamp))
            if self.mode != "battlegrounds" and value.upper() == "INPUT" and not self._mulligan_announced:
                self._mulligan_announced = True
                events.append(GameEvent("mulligan", 4, "起手换牌阶段", timestamp, {}))

        elif tag == "STEP":
            normalized = value.upper()
            if normalized == "BEGIN_MULLIGAN" and self.mode != "battlegrounds":
                if not self._constructed_setup_done:
                    self.phase = "mulligan"
            elif normalized == "MAIN_READY" and self.mode != "battlegrounds":
                events.extend(self._complete_constructed_setup(timestamp))

        elif tag == "STATE":
            normalized = value.upper()
            if normalized == "COMPLETE":
                self.phase = "ended"
                if self.mode == "battlegrounds":
                    events.extend(self._finalize_battlegrounds(timestamp))

        elif tag == "PLAYSTATE":
            normalized = value.upper()
            controller = self._controller(entity)
            if normalized in _END_RESULTS and controller == self.local_controller and not self.result:
                if self.mode == "battlegrounds":
                    self.phase = "ended"
                    events.extend(self._finalize_battlegrounds(timestamp))
                else:
                    self.result = normalized.lower()
                    self.phase = "ended"
                    label = {"WON": "胜利", "LOST": "失败", "TIED": "平局", "CONCEDED": "已投降"}[normalized]
                    events.append(
                        GameEvent("game_ended", 10, f"本局{label}", timestamp, {"result": self.result})
                    )

        elif tag in {"2022", "3533"} and self.mode == "battlegrounds":
            expected = "3533" if self.battlegrounds_variant == "duos" else "2022"
            previous, current = _int(old_value), _int(value)
            if tag == expected and not self._battlegrounds_turn_phase_seen:
                self._explicit_battlegrounds_phase_signal_seen = True
            if (
                tag == expected
                and not self._battlegrounds_turn_phase_seen
                and previous is None
                and current == 1
                and self.phase not in {"combat", "ended", "spectator"}
            ):
                if self.phase != "recruit":
                    self._battlegrounds_fallback_phase_state_used = True
                self.phase = "recruit"
            elif (
                tag == expected
                and not self._battlegrounds_turn_phase_seen
                and previous is None
                and current == 0
                and self.battlegrounds_round > 0
                and self.phase not in {"combat", "ended", "spectator"}
            ):
                self._battlegrounds_fallback_phase_state_used = True
                self._cache_recruit_warband()
                self.phase = "combat"
                if self._begin_battlegrounds_combat():
                    events.append(
                        GameEvent(
                            "battlegrounds_combat_started",
                            6,
                            f"第{self.battlegrounds_round}回合战斗开始",
                            timestamp,
                            {
                                "round": self.battlegrounds_round,
                                "opponent_player_id": self.current_opponent_player_id,
                            },
                        )
                    )
            elif (
                tag == expected
                and not self._battlegrounds_turn_phase_seen
                and previous == 1
                and current == 0
                and self.phase != "combat"
            ):
                self._battlegrounds_fallback_phase_state_used = True
                self._cache_recruit_warband()
                self.phase = "combat"
                if self._begin_battlegrounds_combat():
                    events.append(
                        GameEvent(
                            "battlegrounds_combat_started",
                            6,
                            f"第{self.battlegrounds_round}回合战斗开始",
                            timestamp,
                            {
                                "round": self.battlegrounds_round,
                                "opponent_player_id": self.current_opponent_player_id,
                            },
                        )
                    )
            elif (
                tag == expected
                and not self._battlegrounds_turn_phase_seen
                and previous == 0
                and current == 1
            ):
                self._battlegrounds_fallback_phase_state_used = True
                was_combat = self.phase == "combat"
                events.extend(self._finish_battlegrounds_combat(timestamp))
                self.phase = "recruit"
                if was_combat:
                    events.append(
                        GameEvent(
                            "battlegrounds_recruit_started",
                            5,
                            f"第{self.battlegrounds_round}回合战斗结束，返回招募",
                            timestamp,
                            {"round": self.battlegrounds_round},
                        )
                    )

        elif tag == "NEXT_OPPONENT_PLAYER_ID" and self.mode == "battlegrounds":
            if self._controller(entity) == self.local_controller:
                self.next_opponent_player_id = self._sanitize_opponent_player_id(
                    _int(value) or 0
                )

        elif tag == "PLAYER_TECH_LEVEL" and self.mode == "battlegrounds":
            previous, current = _int(old_value), _int(value)
            if (
                current is not None
                and self._is_local_battlegrounds_entity(entity)
                and self._observe_battlegrounds_counter(entity, tag, current)
                and previous is not None
                and current > previous
            ):
                events.append(
                    GameEvent(
                        "battlegrounds_tavern_upgraded",
                        5,
                        f"酒馆升到{current}级",
                        timestamp,
                        {"tier": current, "round": self.battlegrounds_round},
                    )
                )

        elif tag == "PLAYER_TRIPLES" and self.mode == "battlegrounds":
            previous, current = _int(old_value), _int(value)
            if (
                current is not None
                and self._is_local_battlegrounds_entity(entity)
                and self._observe_battlegrounds_counter(entity, tag, current)
                and previous is not None
                and current > previous
            ):
                events.append(
                    GameEvent(
                        "battlegrounds_triple",
                        7,
                        "三连合成成功",
                        timestamp,
                        {"triples": current, "round": self.battlegrounds_round},
                    )
                )

        self._touch_entity(entity, timestamp)
        self._record_tag_observation(entity, tag)
        self._observe_battlegrounds_combat_marker(entity, tag, value)
        if any(event.kind in _BATTLEGROUNDS_CONTROL_EVENTS for event in events):
            self._block_stack.clear()
            return events
        return self._defer(events)

    def _end_block(self, timestamp: float) -> list[GameEvent]:
        if not self._block_stack:
            return []
        frame = self._block_stack.pop()
        if frame.block_type == "PLAY":
            play_event = self._build_play_event(frame, timestamp)
            if play_event:
                frame.events.insert(0, play_event)
        events = self._coalesce(frame.events, timestamp)
        if self._block_stack:
            self._block_stack[-1].events.extend(events)
            return []
        return events

    def _build_play_event(self, frame: _BlockFrame, timestamp: float) -> GameEvent | None:
        entity = self.entities.get(frame.entity_id or -1)
        if (
            self.mode == "battlegrounds"
            and entity is not None
            and not self._is_battlegrounds_gameplay_entity(entity)
        ):
            return None
        controller = self._controller(entity, frame.controller)
        side = self._side_name(controller)
        if entity and entity.zone == "SECRET" and side == "opponent":
            card = "奥秘"
        else:
            card = self._visible_card_label(entity) if entity else ""
            card = card or "一张牌"
        summary = f"{self._side_label(side)}打出{card}"
        recent = {"side": side, "card": card, "turn": self.turn}
        if entity and entity.card_id and card not in {"一张牌", "奥秘"}:
            recent["card_id"] = entity.card_id
        self._recent_cards.append(recent)
        return GameEvent(
            "card_played",
            3,
            summary,
            timestamp,
            {"side": side, "card": card, "turn": self.turn},
        )

    def _coalesce(self, events: list[GameEvent], timestamp: float) -> list[GameEvent]:
        combined: list[GameEvent] = []
        damage: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            if event.kind not in {"hero_damaged", "hero_healed"}:
                combined.append(event)
                continue
            key = (event.kind, str(event.details.get("side") or "unknown"))
            current = damage.setdefault(
                key,
                {
                    "amount": 0,
                    "priority": event.priority,
                    "health": event.details.get("health"),
                    "armor": event.details.get("armor", 0),
                },
            )
            current["amount"] += int(event.details.get("amount") or 0)
            current["priority"] = max(int(current["priority"]), event.priority)
            current["health"] = event.details.get("health")
            current["armor"] = event.details.get("armor", current.get("armor", 0))
        for (kind, side), data in damage.items():
            verb = "受到" if kind == "hero_damaged" else "恢复"
            suffix = "点伤害" if kind == "hero_damaged" else "点生命"
            combined.append(
                GameEvent(
                    kind,
                    int(data["priority"]),
                    f"{self._side_label(side)}英雄{verb}{data['amount']}{suffix}",
                    timestamp,
                    {"side": side, **data},
                )
            )
        return combined

    def _defer(self, events: list[GameEvent]) -> list[GameEvent]:
        if not events:
            return []
        if self._block_stack:
            self._block_stack[-1].events.extend(events)
            return []
        return events

    def _side_snapshot(self, controller: int | None) -> SideSnapshot:
        if controller is None:
            return SideSnapshot()
        entities = [entity for entity in self.entities.values() if self._controller(entity) == controller]
        hero = (
            self._battlegrounds_local_hero()
            if self.mode == "battlegrounds" and controller == self.local_controller
            else self._public_hero(controller)
        )
        player_entity_id = self.player_entities.get(controller)
        player_entity = self.entities.get(player_entity_id or -1)
        board = [
            entity
            for entity in entities
            if entity.zone == "PLAY"
            and self._is_minion(entity)
            and not entity.hidden
            and not entity.visibility_revoked
        ]
        board.sort(key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id))
        board_cards = tuple(
            name for entity in board if (name := self._visible_card_label(entity))
        )[:7]
        resources = _int(player_entity.tags.get("RESOURCES")) if player_entity else None
        used = player_entity.tag_int("RESOURCES_USED") if player_entity else 0
        temporary = player_entity.tag_int("TEMP_RESOURCES") if player_entity else 0
        if (
            resources is None
            or (
                self.mode == "constructed"
                and self.current_controller is not None
                and controller is not None
                and self.current_controller != controller
            )
        ):
            mana_available = 0 if resources is not None else None
        else:
            mana_available = max(0, resources - used + temporary)
        return SideSnapshot(
            health=hero.health if hero else None,
            armor=hero.armor if hero else 0,
            mana_available=mana_available,
            mana_max=resources,
            hand_count=sum(entity.zone == "HAND" for entity in entities),
            deck_count=sum(entity.zone == "DECK" for entity in entities),
            secret_count=sum(entity.zone == "SECRET" for entity in entities),
            board_count=len(board),
            board_attack=sum(entity.attack for entity in board),
            board_health=sum(entity.health or 0 for entity in board),
            board_cards=board_cards,
        )

    def _constructed_snapshot(self) -> ConstructedSnapshot:
        return ConstructedSnapshot(
            game_type=self.game_type,
            format=_CONSTRUCTED_FORMATS.get(self.game_type, "unknown"),
            variant=_CONSTRUCTED_VARIANTS.get(self.game_type, "unknown"),
            player=self._constructed_side_snapshot(self.local_controller, local=True),
            opponent=self._constructed_side_snapshot(self._opponent_controller(), local=False),
        )

    def _constructed_side_snapshot(
        self,
        controller: int | None,
        *,
        local: bool,
    ) -> ConstructedSideSnapshot:
        if controller is None:
            return ConstructedSideSnapshot()
        entities = [
            entity
            for entity in self.entities.values()
            if self._controller(entity) == controller
        ]
        player_entity = self.entities.get(self.player_entities.get(controller, -1))
        resources = _int(player_entity.tags.get("RESOURCES")) if player_entity else None
        used = player_entity.tag_int("RESOURCES_USED") if player_entity else 0
        temporary = player_entity.tag_int("TEMP_RESOURCES") if player_entity else 0
        if (
            resources is None
            or (
                self.current_controller is not None
                and controller is not None
                and self.current_controller != controller
            )
        ):
            mana_available = 0 if resources is not None else None
        else:
            mana_available = max(0, resources - used + temporary)

        hand_entities = [entity for entity in entities if entity.zone == "HAND"]
        known_hand = tuple(
            card
            for entity in sorted(
                hand_entities,
                key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id),
            )
            if (local or entity.revealed)
            and (card := self._constructed_card(entity)) is not None
        )
        board_entities = [
            entity
            for entity in entities
            if entity.zone == "PLAY"
            and self._is_minion(entity)
            and not entity.hidden
            and not entity.visibility_revoked
        ]
        board = tuple(
            card
            for entity in sorted(
                board_entities,
                key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id),
            )[:7]
            if (card := self._constructed_card(entity)) is not None
        )
        weapon = self._first_constructed_card(entities, "WEAPON")
        hero_power = self._first_constructed_card(entities, "HERO_POWER")
        locations = tuple(
            card
            for entity in sorted(
                (
                    item
                    for item in entities
                    if item.card_type == "LOCATION" and item.zone == "PLAY"
                ),
                key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id),
            )[:2]
            if (card := self._constructed_card(entity)) is not None
        )
        return ConstructedSideSnapshot(
            hero=self._constructed_hero(self._public_hero(controller)),
            mana_available=mana_available,
            mana_max=resources,
            overload_owed=(
                player_entity.tag_int("OVERLOAD_OWED")
                if player_entity
                else 0
            ),
            overload_locked=(
                player_entity.tag_int("OVERLOAD_LOCKED") if player_entity else 0
            ),
            hand_count=len(hand_entities),
            known_hand=known_hand[:10],
            hand_identities_complete=len(known_hand) == len(hand_entities),
            deck_count=sum(entity.zone == "DECK" for entity in entities),
            fatigue=player_entity.tag_int("FATIGUE") if player_entity else 0,
            cards_played_this_turn=(
                player_entity.tag_int("NUM_CARDS_PLAYED_THIS_TURN")
                if player_entity
                else 0
            ),
            secret_count=sum(entity.zone == "SECRET" for entity in entities),
            board=board,
            board_identities_complete=len(board) == len(board_entities),
            weapon=weapon,
            hero_power=hero_power,
            locations=locations,
        )

    def _first_constructed_card(
        self,
        entities: Iterable[Entity],
        card_type: str,
    ) -> ConstructedCardSnapshot | None:
        candidates = sorted(
            (
                entity
                for entity in entities
                if entity.card_type == card_type
                and entity.zone in {"PLAY", "SETASIDE"}
                and not entity.hidden
                and not entity.visibility_revoked
            ),
            key=lambda item: (item.zone == "PLAY", item.entity_id),
            reverse=True,
        )
        for entity in candidates:
            card = self._constructed_card(entity)
            if card is not None:
                return card
        return None

    def _constructed_card(self, entity: Entity) -> ConstructedCardSnapshot | None:
        if entity.hidden or entity.visibility_revoked:
            return None
        label = self._visible_card_label(entity)
        if not label:
            return None
        cost = _int(entity.tags.get("COST"))
        attack = entity.attack if "ATK" in entity.tags else None
        health = entity.health if "HEALTH" in entity.tags else None
        max_health = _int(entity.tags.get("HEALTH"))
        durability = _int(entity.tags.get("DURABILITY"))
        if durability is not None:
            durability = max(0, durability - entity.tag_int("DAMAGE"))
        elif entity.card_type in {"WEAPON", "LOCATION"}:
            durability = health
        exhausted = (
            entity.tag_int("EXHAUSTED") > 0 if "EXHAUSTED" in entity.tags else None
        )
        keywords = tuple(
            public_name
            for tag, public_name in _CONSTRUCTED_KEYWORD_TAGS
            if entity.tag_int(tag) > 0
        )
        states = tuple(
            public_name
            for tag, public_name in _CONSTRUCTED_STATE_TAGS
            if entity.tag_int(tag) > 0
        )
        return ConstructedCardSnapshot(
            card_id=entity.card_id[:80],
            name=label,
            card_type=entity.card_type,
            zone_position=max(0, entity.tag_int("ZONE_POSITION")),
            cost=cost,
            attack=attack,
            health=health,
            max_health=max_health,
            durability=durability,
            exhausted=exhausted,
            keywords=keywords,
            states=states,
        )

    def _constructed_choice_snapshot(self) -> ChoiceSnapshot | None:
        if self.local_controller is None:
            return None
        for choice_id in sorted(self._choices, reverse=True):
            frame = self._choices[choice_id]
            controller = frame.controller
            if controller is None and frame.player_key is not None:
                controller = self._player_name_controllers.get(frame.player_key)
            if controller != self.local_controller:
                continue
            source_entity = self.entities.get(frame.source_entity_id or -1)
            source = self._constructed_card(source_entity) if source_entity else None
            options = tuple(
                card
                for entity_id in frame.option_entity_ids[:16]
                if (entity := self.entities.get(entity_id)) is not None
                and (card := self._constructed_card(entity)) is not None
            )
            return ChoiceSnapshot(
                choice_type=frame.choice_type,
                count_min=frame.count_min,
                count_max=frame.count_max,
                source=source,
                options=options,
            )
        return None

    def _public_hero(self, controller: int | None) -> Entity | None:
        if controller is None:
            return None
        candidates = [
            entity
            for entity in self.entities.values()
            if self._controller(entity) == controller
            and self._is_hero(entity)
            and entity.zone in {"PLAY", "SETASIDE"}
            and not entity.hidden
            and not entity.visibility_revoked
        ]
        return max(
            candidates,
            key=lambda item: (
                item.zone == "PLAY",
                item.health is not None,
                item.entity_id,
            ),
            default=None,
        )

    def _constructed_hero(self, entity: Entity | None) -> ConstructedHeroSnapshot | None:
        if entity is None:
            return None
        label = self._visible_card_label(entity)
        if not label:
            return None
        states = tuple(
            public_name
            for tag, public_name in _CONSTRUCTED_STATE_TAGS
            if entity.tag_int(tag) > 0
        )
        return ConstructedHeroSnapshot(
            card_id=entity.card_id[:80],
            name=label,
            health=entity.health,
            armor=entity.armor,
            attack=entity.attack,
            states=states,
        )

    def _battlegrounds_snapshot(self) -> BattlegroundsSnapshot:
        local_player = self.entities.get(self.player_entities.get(self.local_controller or -1, -1))
        local_hero = self._battlegrounds_local_hero()
        resources = _int(local_player.tags.get("RESOURCES")) if local_player else None
        gold, gold_observation = self._battlegrounds_gold_state(local_player)
        max_gold = None
        if local_player and gold_observation.complete:
            max_gold = _int(local_player.tags.get("3148"))
            if max_gold is None:
                max_gold = resources

        shop_entities = self._battlegrounds_shop_entities()
        hand_entities = [
            entity
            for entity in self.entities.values()
            if self._controller(entity) == self.local_controller and entity.zone == "HAND"
            and self._is_battlegrounds_visible_card(entity, maximum_position=10)
            and not entity.hidden
        ]
        warband_entities = self._battlegrounds_board_entities(self.local_controller)
        shop_cards = self._battlegrounds_cards(
            shop_entities,
            observed_costs=self._battlegrounds_shop_action_costs(shop_entities),
        )
        hand_cards = self._battlegrounds_cards(hand_entities)
        current_warband = self._battlegrounds_cards(warband_entities, maximum=7)
        visible_warband = current_warband
        warband_area = self._battlegrounds_area(
            warband_entities,
            current_warband,
            allow_empty=self.phase in {"recruit", "combat", "ended"},
        )
        if not visible_warband and self.phase in {"combat", "ended"}:
            visible_warband = self._last_recruit_warband
            warband_area = self._last_recruit_warband_area

        current_choice, choice_area = self._battlegrounds_current_choice()
        economy = self._battlegrounds_economy(
            local_player,
            local_hero,
            gold_observation=gold_observation,
        )
        economy_observations = (
            economy.gold_observation,
            economy.refresh_observation,
            economy.upgrade_observation,
        )
        economy_complete = all(item.complete for item in economy_observations)
        economy_revisions = [item.revision for item in economy_observations if item.revision > 0]
        economy_observed_times = [
            item.observed_at
            for item in economy_observations
            if item.observed_at is not None and item.observed_at > 0
        ]
        shop_area = self._battlegrounds_area(
            shop_entities,
            shop_cards,
            allow_empty=False,
            require_current_observation=True,
        )
        if (
            not shop_entities
            and self._empty_shop_observation.complete
            and self._empty_shop_observation.round == self.battlegrounds_round
            and self._empty_shop_observation.phase == self.phase
        ):
            shop_area = self._empty_shop_observation

        areas = {
            "shop": shop_area,
            "hand": self._battlegrounds_area(
                hand_entities,
                hand_cards,
                allow_empty=local_player is not None and self.phase == "recruit",
                fallback=local_player,
            ),
            "warband": warband_area,
            "choice": choice_area,
            "economy": BattlegroundsAreaSnapshot(
                complete=economy_complete,
                revision=min(economy_revisions) if economy_complete else 0,
                observed_at=(
                    min(economy_observed_times)
                    if economy_complete and len(economy_observed_times) == 3
                    else None
                ),
                round=self.battlegrounds_round if economy_complete else 0,
                phase=self.phase if economy_complete else "unknown",
            ),
        }

        mechanics: dict[str, Any] = {}
        game = self.entities.get(self.game_entity_id or -1)
        if game:
            for public_name, tag in (
                ("quests_active", "BACON_QUESTS_ACTIVE"),
                ("trinkets_active", "BACON_TRINKETS_ACTIVE"),
                ("buddies_enabled", "BACON_BUDDY_ENABLED"),
            ):
                if tag in game.tags:
                    mechanics[public_name] = game.tag_int(tag) > 0
            anomaly = game.tag_int("BACON_GLOBAL_ANOMALY_DBID")
            if anomaly > 0:
                mechanics["anomaly_dbf_id"] = anomaly
        quest_progress = self._local_tag_max("QUEST_PROGRESS")
        quest_total = self._local_tag_max("QUEST_PROGRESS_TOTAL")
        quest_reward = self._local_tag_max("QUEST_REWARD_DATABASE_ID")
        if quest_progress or quest_total or quest_reward:
            mechanics["quest"] = {
                "progress": quest_progress,
                "total": quest_total,
                "reward_dbf_id": quest_reward,
            }
        first_trinket = self._local_tag_max("BACON_FIRST_TRINKET_DATABASE_ID")
        second_trinket = self._local_tag_max("BACON_SECOND_TRINKET_DATABASE_ID")
        if first_trinket or second_trinket:
            mechanics["trinket_dbf_ids"] = [value for value in (first_trinket, second_trinket) if value]

        return BattlegroundsSnapshot(
            variant=self.battlegrounds_variant,
            round=self.battlegrounds_round,
            phase=self.phase,
            gold=gold,
            max_gold=max_gold,
            refresh_cost=economy.refresh_cost,
            upgrade_cost=economy.upgrade_cost,
            tavern_tier=self._battlegrounds_tavern_tier(local_hero, local_player),
            frozen=(
                True
                if any(card.frozen is True for card in shop_cards)
                else False
                if shop_cards and all(card.frozen is False for card in shop_cards)
                else None
            ),
            next_opponent_player_id=self._resolved_next_opponent_player_id(),
            current_opponent_player_id=self.current_opponent_player_id,
            last_opponent_player_id=self.last_opponent_player_id,
            last_opponent_round=self.last_opponent_round,
            placement=local_hero.tag_int("PLAYER_LEADERBOARD_PLACE") if local_hero else 0,
            hero_choices=self._battlegrounds_hero_choices(),
            shop=shop_cards,
            hand=hand_cards,
            warband=visible_warband,
            lobby=self._battlegrounds_lobby(),
            current_choice=current_choice,
            economy=economy,
            areas=areas,
            mechanics=mechanics,
        )

    @staticmethod
    def _is_battlegrounds_visible_card(
        entity: Entity,
        *,
        maximum_position: int,
    ) -> bool:
        if entity.card_type in _BATTLEGROUNDS_CARD_TYPES:
            return True
        if entity.card_type or not entity.card_id:
            return False
        position = entity.tag_int("ZONE_POSITION")
        if not 1 <= position <= maximum_position:
            return False
        if (
            entity.tag_int("BACON_HERO_CAN_BE_DRAFTED") > 0
            or entity.tag_int("BACON_SKIN") > 0
        ):
            return False
        has_stats = "ATK" in entity.tags and "HEALTH" in entity.tags
        has_cost = any(
            tag in entity.tags
            for tag in (
                "BACON_OVERRIDE_BG_COST",
                "INTERACTABLE_OBJECT_COST",
                "COST",
            )
        )
        return has_stats or has_cost

    def _battlegrounds_shop_entities(self) -> list[Entity]:
        if self.phase != "recruit" or self.bob_controller is None:
            return []
        candidates = [
            entity
            for entity in self.entities.values()
            if self._controller(entity) == self.bob_controller
            and entity.zone == "PLAY"
            and self._is_battlegrounds_visible_card(entity, maximum_position=10)
            and self._is_battlegrounds_gameplay_entity(entity)
            and not entity.hidden
            and entity.entity_id not in self._recruit_bob_stale_entity_ids
        ]
        positioned: dict[int, Entity] = {}
        unpositioned: list[Entity] = []
        for entity in candidates:
            position = entity.tag_int("ZONE_POSITION")
            if 1 <= position <= 10:
                current = positioned.get(position)
                # GameState can retain older shop copies in PLAY. Entity IDs are
                # monotonic within one game, so the newest public copy owns the slot.
                if current is None or entity.entity_id > current.entity_id:
                    positioned[position] = entity
            else:
                unpositioned.append(entity)
        if positioned:
            return [positioned[position] for position in sorted(positioned)]
        return sorted(unpositioned, key=lambda item: item.entity_id)

    def _observe_battlegrounds_shop_membership(self, timestamp: float) -> None:
        key = (self.game_number, self.battlegrounds_round, self.phase)
        if (
            self.mode != "battlegrounds"
            or self.phase != "recruit"
            or self.bob_controller is None
        ):
            self._shop_membership_key = None
            self._shop_visible_entity_ids = frozenset()
            self._empty_shop_observation = BattlegroundsAreaSnapshot()
            return

        visible_ids = frozenset(
            entity.entity_id for entity in self._battlegrounds_shop_entities()
        )
        if key != self._shop_membership_key:
            self._shop_membership_key = key
            self._shop_visible_entity_ids = visible_ids
            self._empty_shop_observation = BattlegroundsAreaSnapshot()
            return
        if visible_ids:
            self._shop_visible_entity_ids = visible_ids
            self._empty_shop_observation = BattlegroundsAreaSnapshot()
            return
        if self._shop_visible_entity_ids:
            self._empty_shop_observation = BattlegroundsAreaSnapshot(
                complete=True,
                revision=max(1, self._public_revision),
                observed_at=timestamp if timestamp > 0 else None,
                round=self.battlegrounds_round,
                phase=self.phase,
            )
        self._shop_visible_entity_ids = visible_ids

    def _battlegrounds_card_cost(self, entity: Entity) -> int | None:
        for tag in (
            "BACON_OVERRIDE_BG_COST",
            "INTERACTABLE_OBJECT_COST",
            "COST",
        ):
            value = _int(entity.tags.get(tag))
            if value is None:
                continue
            observation = self._battlegrounds_tag_observation(
                entity,
                tag,
                value_observed=True,
            )
            if observation.complete:
                return max(0, value)
            if (
                tag == "COST"
                and entity.battlegrounds_game_state_boolean_baseline_complete
                and entity.last_revision > 0
                and entity.last_seen_at > 0
                and entity.last_battlegrounds_round == self.battlegrounds_round
                and entity.last_battlegrounds_phase == self.phase
            ):
                return max(0, value)
        return None

    def _battlegrounds_boolean_baseline_complete(self, entity: Entity) -> bool:
        return bool(
            entity.battlegrounds_realtime_boolean_baseline_complete
            or (
                entity.battlegrounds_game_state_boolean_baseline_complete
                and self._pending_realtime_baseline_entity is not entity
            )
        )

    def _battlegrounds_cards(
        self,
        entities: Iterable[Entity],
        *,
        maximum: int = 10,
        observed_costs: Mapping[int, int] | None = None,
    ) -> tuple[BattlegroundsCardSnapshot, ...]:
        cards: list[BattlegroundsCardSnapshot] = []
        ordered = sorted(entities, key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id))
        for entity in ordered:
            if entity.hidden:
                continue
            label = self._visible_card_label(entity)
            if not label and not entity.card_id:
                continue
            current_cost = self._battlegrounds_card_cost(entity)
            if observed_costs is not None and entity.entity_id in observed_costs:
                current_cost = observed_costs[entity.entity_id]
            baseline_complete = self._battlegrounds_boolean_baseline_complete(entity)
            missing_boolean = False if baseline_complete else None
            premium = (
                entity.tag_int("PREMIUM") > 0
                if "PREMIUM" in entity.tags
                else missing_boolean
            )
            frozen = (
                entity.tag_int("FROZEN") > 0
                if "FROZEN" in entity.tags
                else missing_boolean
            )
            keywords = {
                public_name: (
                    entity.tag_int(tag) > 0 if tag in entity.tags else missing_boolean
                )
                for tag, public_name in _BATTLEGROUNDS_KEYWORD_TAGS
            }
            cards.append(
                BattlegroundsCardSnapshot(
                    card_id=entity.card_id[:80],
                    name=label,
                    card_type=entity.card_type or None,
                    attack=entity.attack,
                    health=entity.health,
                    tier=max(entity.tag_int("TECH_LEVEL"), entity.tag_int("BACON_CARD_TIER")),
                    frozen=frozen,
                    position=entity.tag_int("ZONE_POSITION"),
                    premium=premium,
                    current_cost=(
                        max(0, current_cost) if current_cost is not None else None
                    ),
                    keywords=keywords,
                )
            )
        return tuple(cards[: max(0, maximum)])

    def _battlegrounds_shop_action_costs(
        self,
        shop_entities: Iterable[Entity],
    ) -> dict[int, int]:
        shop_ids = {entity.entity_id for entity in shop_entities}
        observed: dict[int, tuple[int, int]] = {}
        for entity in self.entities.values():
            if (
                not entity.card_id.lower().startswith("tb_baconshop_dragbuy")
                or entity.hidden
                or entity.zone != "PLAY"
                or self._controller(entity) != self.local_controller
            ):
                continue
            target_candidates = [
                (value, observation)
                for tag in ("CARD_TARGET", "2442")
                if (value := _int(entity.tags.get(tag))) is not None
                and (
                    observation := self._battlegrounds_tag_observation(
                        entity,
                        tag,
                        value_observed=True,
                    )
                ).complete
            ]
            target = max(
                target_candidates,
                key=lambda item: item[1].revision,
                default=None,
            )
            if target is None or target[0] not in shop_ids:
                continue
            target_id, target_observation = target
            cost = next(
                (
                    (value, observation)
                    for tag in (
                        "BACON_OVERRIDE_BG_COST",
                        "INTERACTABLE_OBJECT_COST",
                        "COST",
                    )
                    if (value := _int(entity.tags.get(tag))) is not None
                    and (
                        observation := self._battlegrounds_tag_observation(
                            entity,
                            tag,
                            value_observed=True,
                        )
                    ).complete
                    and observation.revision >= target_observation.revision
                ),
                None,
            )
            if cost is None:
                continue
            current_cost, cost_observation = cost
            candidate = (
                max(target_observation.revision, cost_observation.revision),
                max(0, current_cost),
            )
            if target_id not in observed or candidate[0] > observed[target_id][0]:
                observed[target_id] = candidate
        return {target_id: cost for target_id, (_revision, cost) in observed.items()}

    def _battlegrounds_current_choice(
        self,
    ) -> tuple[BattlegroundsChoiceSnapshot | None, BattlegroundsAreaSnapshot]:
        if self.local_controller is None:
            return None, BattlegroundsAreaSnapshot(round=self.battlegrounds_round)
        for choice_id in sorted(self._choices, reverse=True):
            frame = self._choices[choice_id]
            controller = frame.controller
            if controller is None and frame.player_key is not None:
                controller = self._player_name_controllers.get(frame.player_key)
            if controller != self.local_controller:
                continue
            option_entities = [
                entity
                for entity_id in frame.option_entity_ids[:16]
                if (entity := self.entities.get(entity_id)) is not None
                and not entity.hidden
            ]
            options = self._battlegrounds_cards(option_entities, maximum=16)
            source_entity = self.entities.get(frame.source_entity_id or -1)
            source_cards = (
                self._battlegrounds_cards((source_entity,), maximum=1)
                if source_entity is not None
                else ()
            )
            area = BattlegroundsAreaSnapshot(
                complete=bool(options) and len(options) == len(frame.option_entity_ids[:16]),
                revision=max(
                    (frame.revision, *(entity.last_revision for entity in option_entities)),
                    default=0,
                ),
                observed_at=max(
                    (frame.observed_at, *(entity.last_seen_at for entity in option_entities)),
                    default=0.0,
                )
                or None,
                round=max(
                    (
                        frame.observed_round,
                        *(entity.last_battlegrounds_round for entity in option_entities),
                    ),
                    default=self.battlegrounds_round,
                ),
                phase=(
                    max(
                        (
                            (frame.observed_at, frame.observed_phase),
                            *(
                                (entity.last_seen_at, entity.last_battlegrounds_phase)
                                for entity in option_entities
                            ),
                        ),
                        default=(0.0, "unknown"),
                    )[1]
                    or "unknown"
                ),
            )
            return (
                BattlegroundsChoiceSnapshot(
                    choice_type=frame.choice_type,
                    count_min=frame.count_min,
                    count_max=frame.count_max,
                    source=source_cards[0] if source_cards else None,
                    options=options,
                ),
                area,
            )
        return None, BattlegroundsAreaSnapshot(round=self.battlegrounds_round, phase=self.phase)

    def _battlegrounds_economy(
        self,
        local_player: Entity | None,
        local_hero: Entity | None,
        *,
        gold_observation: BattlegroundsAreaSnapshot,
    ) -> BattlegroundsEconomySnapshot:
        scoped_entities = [
            entity
            for entity in self.entities.values()
            if entity is local_player
            or entity is local_hero
            or self._controller(entity) in {self.local_controller, self.bob_controller}
        ]

        def tagged_cost(
            tags: tuple[str, ...],
        ) -> tuple[int | None, BattlegroundsAreaSnapshot]:
            candidates: list[tuple[int, BattlegroundsAreaSnapshot]] = []
            for entity in scoped_entities:
                for tag in tags:
                    if tag not in entity.tags:
                        continue
                    value = _int(entity.tags.get(tag))
                    if value is not None:
                        candidates.append(
                            (
                                max(0, value),
                                self._battlegrounds_tag_observation(
                                    entity,
                                    tag,
                                    value_observed=True,
                                ),
                            )
                        )
            return self._select_economy_observation(candidates)

        refresh_button = self._battlegrounds_action_button_cost("refresh")
        upgrade_button = self._battlegrounds_action_button_cost("upgrade")
        refresh_tagged = tagged_cost(_BATTLEGROUNDS_REFRESH_COST_TAGS)
        upgrade_tagged = tagged_cost(_BATTLEGROUNDS_UPGRADE_COST_TAGS)
        refresh_cost, refresh_observation = self._select_economy_observation(
            [refresh_button, refresh_tagged]
        )
        upgrade_cost, upgrade_observation = self._select_economy_observation(
            [upgrade_button, upgrade_tagged]
        )
        if not refresh_observation.complete:
            free_refresh_candidates = []
            for entity in scoped_entities:
                tag = "BACON_FREE_REFRESH_COUNT"
                if entity.tag_int(tag) <= 0:
                    continue
                free_refresh_candidates.append(
                    (
                        0,
                        self._battlegrounds_tag_observation(
                            entity,
                            tag,
                            value_observed=True,
                        ),
                    )
                )
            refresh_cost, refresh_observation = self._select_economy_observation(
                [(refresh_cost, refresh_observation), *free_refresh_candidates]
            )
        observations = (gold_observation, refresh_observation, upgrade_observation)
        revisions = [item.revision for item in observations if item.revision > 0]
        observed_times = [
            item.observed_at
            for item in observations
            if item.observed_at is not None and item.observed_at > 0
        ]
        return BattlegroundsEconomySnapshot(
            upgrade_cost=upgrade_cost if upgrade_observation.complete else None,
            refresh_cost=refresh_cost if refresh_observation.complete else None,
            revision=max(revisions, default=0),
            observed_at=max(observed_times, default=0.0) or None,
            gold_observation=gold_observation,
            upgrade_observation=upgrade_observation,
            refresh_observation=refresh_observation,
        )

    def _battlegrounds_tag_observation(
        self,
        entity: Entity | None,
        tag: str,
        *,
        value_observed: bool,
    ) -> BattlegroundsAreaSnapshot:
        if entity is None:
            return BattlegroundsAreaSnapshot()
        revision = int(entity.tag_revisions.get(tag, 0) or 0)
        observed_at = float(entity.tag_observed_at.get(tag, 0.0) or 0.0)
        observed_round = int(entity.tag_battlegrounds_rounds.get(tag, 0) or 0)
        observed_phase = str(entity.tag_battlegrounds_phases.get(tag, "") or "unknown")
        complete = bool(
            value_observed
            and revision > 0
            and observed_at > 0
            and observed_round == self.battlegrounds_round
            and observed_phase == self.phase
        )
        return BattlegroundsAreaSnapshot(
            complete=complete,
            revision=revision,
            observed_at=observed_at or None,
            round=observed_round,
            phase=observed_phase,
        )

    def _battlegrounds_gold_state(
        self,
        entity: Entity | None,
    ) -> tuple[int | None, BattlegroundsAreaSnapshot]:
        if entity is None:
            return None, BattlegroundsAreaSnapshot()
        resources = _int(entity.tags.get("RESOURCES"))
        resource_observation = self._battlegrounds_tag_observation(
            entity,
            "RESOURCES",
            value_observed=resources is not None,
        )
        if resources is None or not resource_observation.complete:
            return None, resource_observation

        observations = [resource_observation]
        components = {"RESOURCES_USED": 0, "TEMP_RESOURCES": 0}
        for tag in components:
            if tag not in entity.tags:
                continue
            observation = self._battlegrounds_tag_observation(
                entity,
                tag,
                value_observed=_int(entity.tags.get(tag)) is not None,
            )
            if not observation.complete:
                continue
            observations.append(observation)
            components[tag] = max(0, _int(entity.tags.get(tag)) or 0)

        gold = max(
            0,
            resources
            + components["TEMP_RESOURCES"]
            - components["RESOURCES_USED"],
        )
        return gold, BattlegroundsAreaSnapshot(
            complete=True,
            revision=min(item.revision for item in observations),
            observed_at=min(
                item.observed_at
                for item in observations
                if item.observed_at is not None
            ),
            round=self.battlegrounds_round,
            phase=self.phase,
        )

    @staticmethod
    def _select_economy_observation(
        candidates: Iterable[tuple[int | None, BattlegroundsAreaSnapshot]],
    ) -> tuple[int | None, BattlegroundsAreaSnapshot]:
        usable = [
            (value, observation)
            for value, observation in candidates
            if value is not None or observation.revision > 0
        ]
        if not usable:
            return None, BattlegroundsAreaSnapshot()
        return max(
            usable,
            key=lambda item: (item[1].complete, item[1].revision),
        )

    def _battlegrounds_action_button_cost(
        self,
        action: str,
    ) -> tuple[int | None, BattlegroundsAreaSnapshot]:
        slot = {"refresh": 2, "upgrade": 3}.get(action)
        needles = {
            "refresh": ("reroll", "refresh"),
            "upgrade": ("techup", "tavern tier", "tavernupgrade", "tavern_upgrade"),
        }.get(action, ())
        candidates: list[tuple[int, BattlegroundsAreaSnapshot]] = []
        for entity in self.entities.values():
            if (
                entity.card_type != "GAME_MODE_BUTTON"
                or entity.zone != "PLAY"
                or entity.hidden
                or self._controller(entity) != self.local_controller
            ):
                continue
            identifier = f"{entity.card_id} {entity.public_name()}".casefold()
            slot_matches = bool(
                slot is not None and entity.tag_int("GAME_MODE_BUTTON_SLOT") == slot
            )
            if not slot_matches and needles and not any(
                needle in identifier for needle in needles
            ):
                continue
            for tag in ("BACON_OVERRIDE_BG_COST", "COST"):
                if tag in entity.tags and (value := _int(entity.tags.get(tag))) is not None:
                    cost_observation = self._battlegrounds_tag_observation(
                        entity,
                        tag,
                        value_observed=True,
                    )
                    if tag == "COST" and not cost_observation.complete:
                        current_tags = tuple(
                            observed_tag
                            for observed_tag in entity.tag_revisions
                            if entity.tag_battlegrounds_rounds.get(observed_tag)
                            == self.battlegrounds_round
                            and entity.tag_battlegrounds_phases.get(observed_tag)
                            == self.phase
                            and entity.tag_observed_at.get(observed_tag, 0.0) > 0
                        )
                        current_tag_revision = max(
                            (
                                entity.tag_revisions[observed_tag]
                                for observed_tag in current_tags
                            ),
                            default=0,
                        )
                        current_tag_observed_at = max(
                            (
                                entity.tag_observed_at[observed_tag]
                                for observed_tag in current_tags
                            ),
                            default=0.0,
                        )
                        entity_is_current = bool(
                            entity.last_revision > 0
                            and entity.last_seen_at > 0
                            and entity.last_battlegrounds_round
                            == self.battlegrounds_round
                            and entity.last_battlegrounds_phase == self.phase
                        )
                        current_game_state_baseline = bool(
                            entity.battlegrounds_game_state_boolean_baseline_complete
                            and entity_is_current
                        )
                        if current_tags or current_game_state_baseline:
                            cost_observation = BattlegroundsAreaSnapshot(
                                complete=True,
                                revision=max(
                                    cost_observation.revision,
                                    current_tag_revision,
                                    entity.last_revision
                                    if current_game_state_baseline
                                    else 0,
                                ),
                                observed_at=max(
                                    cost_observation.observed_at or 0.0,
                                    current_tag_observed_at,
                                    entity.last_seen_at
                                    if current_game_state_baseline
                                    else 0.0,
                                )
                                or None,
                                round=self.battlegrounds_round,
                                phase=self.phase,
                            )
                    candidates.append(
                        (
                            max(0, value),
                            cost_observation,
                        )
                    )
        return self._select_economy_observation(candidates)

    def _battlegrounds_area(
        self,
        entities: Iterable[Entity],
        cards: tuple[BattlegroundsCardSnapshot, ...],
        *,
        allow_empty: bool,
        fallback: Entity | None = None,
        require_current_observation: bool = False,
    ) -> BattlegroundsAreaSnapshot:
        visible_entities = [entity for entity in entities if not entity.hidden]
        observed_entities = [*visible_entities]
        if fallback is not None:
            observed_entities.append(fallback)
        positions = [entity.tag_int("ZONE_POSITION") for entity in visible_entities]
        positioned_complete = not positions or (
            all(position > 0 for position in positions)
            and len(set(positions)) == len(positions)
        )
        identities_complete = all(
            bool(entity.card_id or entity.public_name()) and bool(entity.card_type)
            for entity in visible_entities
        )
        boolean_baselines_complete = all(
            self._battlegrounds_boolean_baseline_complete(entity)
            for entity in visible_entities
        )
        observation_current = bool(
            not require_current_observation
            or (
                observed_entities
                and all(
                    entity.last_battlegrounds_round == self.battlegrounds_round
                    and entity.last_battlegrounds_phase == self.phase
                    for entity in observed_entities
                )
            )
        )
        complete = bool(
            (visible_entities or allow_empty)
            and len(cards) == len(visible_entities)
            and positioned_complete
            and identities_complete
            and boolean_baselines_complete
            and observation_current
        )
        revisions = [entity.last_revision for entity in observed_entities]
        observed_times = [entity.last_seen_at for entity in observed_entities]
        return BattlegroundsAreaSnapshot(
            complete=complete,
            revision=min(revisions, default=0),
            observed_at=min(observed_times, default=0.0) or None,
            round=self.battlegrounds_round if observation_current else 0,
            phase=self.phase if observation_current else "unknown",
        )

    def _battlegrounds_hero_choices(self) -> tuple[BattlegroundsHeroChoiceSnapshot, ...]:
        if (
            self.local_controller is None
            or self._battlegrounds_hero_selection_complete
            or self.phase in {"combat", "ended", "spectator"}
        ):
            return ()
        choices = [
            entity
            for entity in self.entities.values()
            if self._is_battlegrounds_hero_choice_entity(entity)
        ]
        ordered = sorted(
            choices,
            key=lambda item: (item.tag_int("ZONE_POSITION", 99), item.entity_id),
        )
        return tuple(
            BattlegroundsHeroChoiceSnapshot(
                card_id=entity.card_id[:80],
                name=entity.public_name(),
            )
            for entity in ordered[:8]
            if entity.card_id or entity.public_name()
        )

    def _is_battlegrounds_hero_choice_entity(self, entity: Entity) -> bool:
        return (
            self.local_controller is not None
            and self._controller(entity) == self.local_controller
            and self._is_hero(entity)
            and not entity.hidden
            and (
                entity.tag_int("BACON_HERO_CAN_BE_DRAFTED") > 0
                or entity.tag_int("BACON_SKIN") > 0
            )
            and entity.tag_int("BACON_LOCKED_MULLIGAN_HERO") <= 0
            and entity.zone not in {"GRAVEYARD", "REMOVEDFROMGAME", "INVALID"}
        )

    def _battlegrounds_board_entities(
        self,
        controller: int | None,
        *,
        excluded_entity_ids: set[int] | None = None,
    ) -> list[Entity]:
        if controller is None:
            return []
        excluded = excluded_entity_ids or set()
        candidates = [
            entity
            for entity in self.entities.values()
            if entity.entity_id not in excluded
            and self._controller(entity) == controller
            and entity.zone == "PLAY"
            and self._is_battlegrounds_minion(entity)
            and self._is_battlegrounds_gameplay_entity(entity)
            and not entity.hidden
        ]
        positioned: dict[int, Entity] = {}
        unpositioned: list[Entity] = []
        for entity in candidates:
            position = entity.tag_int("ZONE_POSITION")
            if 1 <= position <= 7:
                current = positioned.get(position)
                if current is None or entity.entity_id > current.entity_id:
                    positioned[position] = entity
            else:
                unpositioned.append(entity)
        if positioned:
            return [positioned[position] for position in sorted(positioned)][:7]
        if any("ZONE_POSITION" in entity.tags for entity in candidates):
            return []
        return sorted(unpositioned, key=lambda item: item.entity_id)[:7]

    def _cache_recruit_warband(self) -> None:
        entities = self._battlegrounds_board_entities(self.local_controller)
        cards = self._battlegrounds_cards(entities, maximum=7)
        self._last_recruit_warband = cards
        self._last_recruit_warband_area = self._battlegrounds_area(
            entities,
            cards,
            allow_empty=True,
        )

    @staticmethod
    def _is_battlegrounds_gameplay_entity(entity: Entity) -> bool:
        label = entity.public_name().strip().casefold()
        card_id = entity.card_id.strip().casefold()
        if (
            label in _BATTLEGROUNDS_UI_LABELS
            or card_id in _BATTLEGROUNDS_UI_CARD_IDS
            or "drag_to_" in card_id
            or "drag to " in label
        ):
            return False
        return entity.health is not None or bool(entity.card_id)

    def _battlegrounds_hero_entities(self) -> dict[int, Entity]:
        heroes: dict[int, Entity] = {}
        for entity in self.entities.values():
            if entity.hidden or entity.visibility_revoked or not self._is_hero(entity):
                continue
            if (
                not self._battlegrounds_hero_selection_complete
                and self._is_battlegrounds_hero_choice_entity(entity)
            ):
                continue
            player_id = entity.tag_int("PLAYER_ID")
            if player_id <= 0 or player_id == self.bob_controller:
                continue
            previous = heroes.get(player_id)
            score = self._battlegrounds_hero_score(entity)
            if previous is None or score > self._battlegrounds_hero_score(previous):
                heroes[player_id] = entity
        return heroes

    def _battlegrounds_hero_score(self, entity: Entity) -> tuple[int, int, int, int]:
        return (
            int(self._controller(entity) == self.local_controller),
            int("PLAYER_LEADERBOARD_PLACE" in entity.tags),
            int(entity.zone in {"PLAY", "SETASIDE"}),
            -entity.entity_id,
        )

    def _battlegrounds_local_hero(self) -> Entity | None:
        heroes = self._battlegrounds_hero_entities()
        direct = heroes.get(self.local_controller or -1)
        if direct is not None:
            return direct
        for hero in heroes.values():
            if hero.tag_int("PLAYER_ID") <= 0 and self._controller(hero) == self.local_controller:
                return hero
        return None

    def _battlegrounds_lobby(self) -> tuple[BattlegroundsPlayerSnapshot, ...]:
        result: list[BattlegroundsPlayerSnapshot] = []
        next_opponent_player_id = self._resolved_next_opponent_player_id()
        current_opponent_player_id = self.current_opponent_player_id
        last_opponent_player_id = self.last_opponent_player_id
        local_team_id = self._battlegrounds_team_id(
            self.local_controller or 0,
            self._battlegrounds_local_hero(),
        )
        for player_id, hero in self._battlegrounds_hero_entities().items():
            is_local = self._is_local_battlegrounds_entity(hero)
            observed = self._observed_boards.get(player_id)
            if is_local:
                board_entities = self._battlegrounds_board_entities(self.local_controller)
                if board_entities:
                    board_minions = self._battlegrounds_cards(board_entities, maximum=7)
                else:
                    board_minions = self._last_recruit_warband
                last_seen_round = self.battlegrounds_round
            elif observed:
                last_seen_round, board_minions = observed
            else:
                last_seen_round, board_minions = 0, ()
            board_cards = tuple(card.name or card.card_id for card in board_minions)
            board_count = len(board_minions)
            board_attack = sum(card.attack for card in board_minions)
            board_health = sum(card.health or 0 for card in board_minions)
            placement = hero.tag_int("PLAYER_LEADERBOARD_PLACE")
            eliminated = hero.health == 0 or hero.tag_int("BACON_DIED_LAST_COMBAT") > 0
            if hero.tags.get("PLAYSTATE", "").upper() in {"LOST", "CONCEDED"}:
                eliminated = True
            result.append(
                BattlegroundsPlayerSnapshot(
                    player_id=player_id,
                    is_local=is_local,
                    hero_card_id=hero.card_id[:80],
                    hero_name=hero.public_name(),
                    health=hero.health,
                    armor=hero.armor,
                    tavern_tier=self._battlegrounds_tavern_tier(hero, None),
                    triples=hero.tag_int("PLAYER_TRIPLES"),
                    placement=placement,
                    eliminated=eliminated,
                    next_opponent=player_id == next_opponent_player_id,
                    current_opponent=player_id == current_opponent_player_id,
                    last_opponent=player_id == last_opponent_player_id,
                    is_teammate=(
                        not is_local
                        and self.battlegrounds_variant == "duos"
                        and local_team_id > 0
                        and self._battlegrounds_team_id(player_id, hero) == local_team_id
                    ),
                    last_seen_round=last_seen_round,
                    board_count=board_count,
                    board_attack=board_attack,
                    board_health=board_health,
                    board_cards=board_cards,
                    board_minions=board_minions,
                )
            )
        return tuple(sorted(result, key=lambda item: (item.placement or 99, item.player_id))[:8])

    def _capture_observed_opponent_board(self) -> None:
        player_id = self._active_opponent_player_id()
        if player_id <= 0:
            return
        direct_entities = self._battlegrounds_board_entities(player_id)
        bob_entities: list[Entity] = []
        if self.bob_controller is not None:
            bob_entities = self._battlegrounds_board_entities(
                self.bob_controller,
                excluded_entity_ids=self._combat_bob_stale_entity_ids,
            )
        combined = [*direct_entities, *bob_entities]
        positioned: dict[int, Entity] = {}
        for entity in combined:
            position = entity.tag_int("ZONE_POSITION")
            if 1 <= position <= 7:
                positioned.setdefault(position, entity)
        if positioned:
            entities = [positioned[position] for position in sorted(positioned)]
        else:
            # Without positions there is no safe way to distinguish duplicate
            # views from legitimate copies of the same minion.
            entities = direct_entities or bob_entities
        cards = self._battlegrounds_cards(entities, maximum=7)
        if not cards:
            return
        previous = self._observed_boards.get(player_id)
        if previous and previous[0] == self.battlegrounds_round:
            return
        self._observed_boards[player_id] = (self.battlegrounds_round, cards)

    def _observe_battlegrounds_combat_marker(
        self,
        entity: Entity,
        tag: str,
        value: str,
    ) -> None:
        if (
            self.phase != "combat"
            or tag not in {"ATTACKING", "DEFENDING"}
            or value.upper() not in {"1", "TRUE"}
        ):
            return
        controller = self._controller(entity)
        if self.bob_controller is not None and controller == self.bob_controller:
            if entity.entity_id in self._combat_bob_stale_entity_ids:
                self._combat_bob_stale_entity_ids.clear()
        opponent_player_id = self._active_opponent_player_id()
        if controller != self.bob_controller and controller != opponent_player_id:
            return
        self._capture_observed_opponent_board()

    def _reconcile_battlegrounds_turn_phase(
        self,
        turn: int,
        timestamp: float,
    ) -> list[GameEvent]:
        events: list[GameEvent] = []
        next_round = (turn + 1) // 2
        recruit_turn = turn % 2 == 1
        phase_locked = self.spectating or self.phase in {"spectator", "ended"}
        previous_phase = self.phase

        if recruit_turn and previous_phase == "combat" and not phase_locked:
            events.extend(self._finish_battlegrounds_combat(timestamp))

        if next_round > self.battlegrounds_round:
            self.battlegrounds_round = next_round
            events.append(
                GameEvent(
                    "battlegrounds_round",
                    3,
                    f"酒馆第{self.battlegrounds_round}回合",
                    timestamp,
                    {"turn": turn, "round": self.battlegrounds_round},
                )
            )

        if phase_locked:
            return events
        if recruit_turn:
            self.phase = "recruit"
            if previous_phase == "combat":
                events.append(
                    GameEvent(
                        "battlegrounds_recruit_started",
                        5,
                        f"第{self.battlegrounds_round}回合开始招募",
                        timestamp,
                        {"round": self.battlegrounds_round},
                    )
                )
            return events

        if previous_phase != "combat":
            if previous_phase == "recruit":
                self._cache_recruit_warband()
            self.phase = "combat"
            if self._begin_battlegrounds_combat():
                events.append(
                    GameEvent(
                        "battlegrounds_combat_started",
                        6,
                        f"第{self.battlegrounds_round}回合战斗开始",
                        timestamp,
                        {
                            "round": self.battlegrounds_round,
                            "opponent_player_id": self.current_opponent_player_id,
                        },
                    )
                )
        return events

    def _discard_battlegrounds_fallback_phase_state(self) -> None:
        fallback_opponent_player_id = self.current_opponent_player_id
        if (
            fallback_opponent_player_id <= 0
            and self._combat_result_emitted_round > 0
            and self.last_opponent_round == self._combat_result_emitted_round
        ):
            fallback_opponent_player_id = self.last_opponent_player_id
        if self.next_opponent_player_id <= 0 and fallback_opponent_player_id > 0:
            self.next_opponent_player_id = fallback_opponent_player_id
        self.current_opponent_player_id = 0
        if (
            self._combat_result_emitted_round > 0
            and self.last_opponent_round == self._combat_result_emitted_round
        ):
            self.last_opponent_player_id = 0
            self.last_opponent_round = 0
        self._combat_active_round = 0
        self._combat_result_emitted_round = 0
        self._combat_damage_taken = 0
        self._combat_damage_dealt = 0
        self._combat_bob_stale_entity_ids.clear()
        self._recruit_bob_stale_entity_ids.clear()
        self._last_recruit_warband = ()
        self._last_recruit_warband_area = BattlegroundsAreaSnapshot()
        self._last_recruit_warband_observed_at = 0.0
        self._last_recruit_warband_revision = 0

    def _begin_battlegrounds_combat(self) -> bool:
        round_number = max(1, self.battlegrounds_round)
        if (
            self._combat_active_round == round_number
            or self._combat_result_emitted_round == round_number
        ):
            return False
        self._combat_active_round = round_number
        self.current_opponent_player_id = self._resolved_next_opponent_player_id()
        self.next_opponent_player_id = 0
        self._combat_damage_taken = 0
        self._combat_damage_dealt = 0
        self._combat_bob_stale_entity_ids = {
            entity.entity_id
            for entity in self.entities.values()
            if self._controller(entity) == self.bob_controller
            and entity.zone == "PLAY"
            and self._is_battlegrounds_minion(entity)
            and self._is_battlegrounds_gameplay_entity(entity)
        }
        return True

    def _finish_battlegrounds_combat(self, timestamp: float) -> list[GameEvent]:
        round_number = self._combat_active_round
        if round_number <= 0 or self._combat_result_emitted_round == round_number:
            return []
        if self.current_opponent_player_id > 0:
            self.last_opponent_player_id = self.current_opponent_player_id
            self.last_opponent_round = round_number
        self.current_opponent_player_id = 0
        self._recruit_bob_stale_entity_ids = {
            entity.entity_id
            for entity in self.entities.values()
            if self._controller(entity) == self.bob_controller
            and entity.zone == "PLAY"
            and self._is_battlegrounds_gameplay_entity(entity)
        }
        self._combat_result_emitted_round = round_number
        if self._combat_damage_taken > 0 and self._combat_damage_dealt == 0:
            outcome, summary = "lost", f"第{round_number}回合战斗失利"
        elif self._combat_damage_dealt > 0 and self._combat_damage_taken == 0:
            outcome, summary = "won", f"第{round_number}回合战斗获胜"
        elif self._combat_damage_dealt == 0 and self._combat_damage_taken == 0:
            outcome, summary = "tied", f"第{round_number}回合战斗打平"
        else:
            outcome, summary = "mixed", f"第{round_number}回合战斗结束"
        local_hero = self._battlegrounds_local_hero()
        health = local_hero.health if local_hero else None
        armor = local_hero.armor if local_hero else 0
        priority = 8 if outcome == "lost" and health is not None and health + armor <= 10 else 7
        if outcome == "tied":
            priority = 5
        elif outcome == "mixed":
            priority = 4
        self._combat_active_round = 0
        return [
            GameEvent(
                "battlegrounds_combat_result",
                priority,
                summary,
                timestamp,
                {
                    "round": round_number,
                    "outcome": outcome,
                    "damage_taken": self._combat_damage_taken,
                    "damage_dealt": self._combat_damage_dealt,
                    "health": health,
                    "armor": armor,
                    "variant": self.battlegrounds_variant,
                },
            )
        ]

    def _finalize_battlegrounds(self, timestamp: float) -> list[GameEvent]:
        if self._battlegrounds_result_emitted:
            return []
        combat_events = self._finish_battlegrounds_combat(timestamp)
        hero = self._battlegrounds_local_hero()
        placement = hero.tag_int("PLAYER_LEADERBOARD_PLACE") if hero else 0
        if placement <= 0:
            return combat_events
        self._battlegrounds_result_emitted = True
        self.result = "won" if placement == 1 else "placed"
        return combat_events + [
            GameEvent(
                "battlegrounds_game_ended",
                10,
                f"本局酒馆战棋第{placement}名",
                timestamp,
                {
                    "placement": placement,
                    "variant": self.battlegrounds_variant,
                    "round": self.battlegrounds_round,
                    "hero_card_id": hero.card_id[:80] if hero else "",
                    "hero_name": hero.public_name() if hero else "",
                },
            )
        ]

    def _battlegrounds_tavern_tier(self, hero: Entity | None, player: Entity | None) -> int:
        return max(
            hero.tag_int("PLAYER_TECH_LEVEL") if hero else 0,
            player.tag_int("PLAYER_TECH_LEVEL") if player else 0,
        )

    def _battlegrounds_team_id(self, player_id: int, hero: Entity | None) -> int:
        values: set[int] = set()
        player = self.entities.get(self.player_entities.get(player_id, -1))
        for entity in (player, hero):
            if entity is None:
                continue
            value = entity.tag_int("BACON_DUO_TEAM_ID")
            if value > 0:
                values.add(value)
        return values.pop() if len(values) == 1 else 0

    def _mark_battlegrounds_combat_identity(self, entity: Entity) -> None:
        if (
            self.phase == "recruit"
            and entity.card_id
            and self._controller(entity) == self.bob_controller
        ):
            self._recruit_bob_stale_entity_ids.discard(entity.entity_id)
        if (
            self.phase == "combat"
            and entity.card_id
            and self._controller(entity) == self.bob_controller
        ):
            self._combat_bob_stale_entity_ids.discard(entity.entity_id)

    def _local_tag_max(self, tag: str) -> int:
        return max(
            (
                entity.tag_int(tag)
                for entity in self.entities.values()
                if self._is_local_battlegrounds_entity(entity)
            ),
            default=0,
        )

    def _is_local_battlegrounds_entity(self, entity: Entity) -> bool:
        if self.local_controller is None:
            return False
        player_id = entity.tag_int("PLAYER_ID")
        if player_id > 0:
            return player_id == self.local_controller
        return self._controller(entity) == self.local_controller

    def _battlegrounds_player_id(self, entity: Entity) -> int:
        return entity.tag_int("PLAYER_ID") or self._controller(entity) or 0

    def _resolved_next_opponent_player_id(self) -> int:
        if self.next_opponent_player_id > 0:
            return self._sanitize_opponent_player_id(self.next_opponent_player_id)
        if self.current_opponent_player_id > 0 or self.last_opponent_player_id > 0:
            return 0
        local_player = self.entities.get(
            self.player_entities.get(self.local_controller or -1, -1)
        )
        player_id = local_player.tag_int("NEXT_OPPONENT_PLAYER_ID") if local_player else 0
        return self._sanitize_opponent_player_id(player_id)

    def _active_opponent_player_id(self) -> int:
        if self.current_opponent_player_id > 0:
            return self.current_opponent_player_id
        return self._resolved_next_opponent_player_id()

    def _sanitize_opponent_player_id(self, player_id: int) -> int:
        if player_id <= 0 or player_id in {
            self.local_controller,
            self.bob_controller,
        }:
            return 0
        return player_id

    def _observe_battlegrounds_counter(self, entity: Entity, tag: str, current: int) -> bool:
        player_id = self._battlegrounds_player_id(entity)
        if player_id <= 0:
            return False
        key = (player_id, tag)
        highest = self._battlegrounds_counter_highs.get(key)
        if highest is not None and current <= highest:
            return False
        self._battlegrounds_counter_highs[key] = current
        return True

    def _unresolved_tag_entity(self, raw_ref: str, raw_tag: str, raw_value: str) -> Entity | None:
        constructed_player = self._infer_constructed_player_entity(raw_ref, raw_tag)
        if constructed_player is not None:
            return constructed_player
        if self.mode != "battlegrounds":
            return None
        tag = _clean(raw_tag, limit=60).upper()
        value = _clean(raw_value, limit=160).upper()
        local_player = self.entities.get(self.player_entities.get(self.local_controller or -1, -1))
        bob_player = self.entities.get(self.player_entities.get(self.bob_controller or -1, -1))
        if tag == "CURRENT_PLAYER" and value in {"1", "TRUE"}:
            if "BOB'S TAVERN" in str(raw_ref).upper():
                return bob_player
            return local_player
        if tag in {
            "NEXT_OPPONENT_PLAYER_ID",
            "RESOURCES",
            "RESOURCES_USED",
            "TEMP_RESOURCES",
            "PLAYER_TECH_LEVEL",
            "PLAYER_TRIPLES",
            "PLAYSTATE",
            "MULLIGAN_STATE",
        }:
            return local_player
        return None

    def _visible_card_label(self, entity: Entity | None) -> str:
        if entity is None or entity.hidden or entity.visibility_revoked:
            return ""
        controller = self._controller(entity)
        if controller != self.local_controller and not entity.revealed and entity.zone not in _PUBLIC_ZONES:
            return ""
        if controller != self.local_controller and entity.zone == "SECRET":
            return ""
        return entity.public_name()

    def _infer_local_controller_from_show(self, entity: Entity) -> None:
        if self.local_controller is not None or self.spectating or entity.zone != "HAND":
            return
        self._set_local_controller(self._controller(entity))

    def _controller(self, entity: Entity | None, fallback: int | None = None) -> int | None:
        if entity is None:
            return fallback
        if entity.controller is not None:
            return entity.controller
        return _int(entity.tags.get("CONTROLLER")) or _int(entity.tags.get("PLAYER_ID")) or fallback

    def _opponent_controller(self) -> int | None:
        if self.local_controller is None:
            return None
        registered = {
            controller
            for controller in self.player_entities
            if controller > 0
            and controller != self.local_controller
            and controller != self.bob_controller
        }
        if registered:
            return min(registered)
        fallback = {
            controller
            for entity in self.entities.values()
            if (
                (controller := self._controller(entity)) is not None
                and controller > 0
                and controller != self.local_controller
                and controller != self.bob_controller
                and (entity.card_type == "PLAYER" or self._is_hero(entity))
            )
        }
        return min(fallback) if fallback else None

    def _zone_count(self, controller: int | None, zone: str) -> int:
        return sum(
            entity.zone == zone and self._controller(entity) == controller for entity in self.entities.values()
        )

    @staticmethod
    def _is_hero(entity: Entity) -> bool:
        card_id = entity.card_id.upper()
        return bool(
            entity.card_type == "HERO"
            or card_id.startswith(("HERO_", "TB_BACONSHOP_HERO_"))
            or entity.tag_int("BACON_HERO_CAN_BE_DRAFTED") > 0
            or entity.tag_int("BACON_SKIN") > 0
        )

    @staticmethod
    def _is_minion(entity: Entity) -> bool:
        return entity.card_type == "MINION"

    def _is_battlegrounds_minion(self, entity: Entity) -> bool:
        if self._is_minion(entity):
            return True
        if self.mode != "battlegrounds" or not entity.card_id or self._is_hero(entity):
            return False
        if entity.card_type:
            return False
        position = entity.tag_int("ZONE_POSITION")
        return (1 <= position <= 7) or (
            "ATK" in entity.tags and "HEALTH" in entity.tags
        )

    def _side_name(self, controller: int | None) -> str:
        if controller is None or self.local_controller is None:
            return "unknown"
        return "player" if controller == self.local_controller else "opponent"

    @staticmethod
    def _side_label(side: str) -> str:
        return {"player": "我方", "opponent": "对手"}.get(side, "场上")


__all__ = ["PowerLogParser"]
