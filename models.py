from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Entity:
    entity_id: int
    card_id: str = ""
    name: str = ""
    controller: int | None = None
    zone: str = ""
    card_type: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    revealed: bool = False
    hidden: bool = False
    visibility_revoked: bool = False
    realtime_fields: set[str] = field(default_factory=set)
    last_seen_at: float = 0.0
    last_revision: int = 0
    last_battlegrounds_round: int = 0
    last_battlegrounds_phase: str = ""

    def tag_int(self, name: str, default: int = 0) -> int:
        value = _as_int(self.tags.get(name.upper()))
        return default if value is None else value

    @property
    def health(self) -> int | None:
        maximum = _as_int(self.tags.get("HEALTH"))
        if maximum is None:
            return None
        return max(0, maximum - self.tag_int("DAMAGE"))

    @property
    def armor(self) -> int:
        return max(0, self.tag_int("ARMOR"))

    @property
    def attack(self) -> int:
        return max(0, self.tag_int("ATK"))

    def public_name(self) -> str:
        if self.hidden or self.visibility_revoked:
            return ""
        normalized_name = " ".join(self.name.split()).strip()
        if normalized_name.upper().startswith("UNKNOWN ENTITY"):
            normalized_name = ""
        if normalized_name:
            return normalized_name[:80]
        return self.card_id[:80]


@dataclass(frozen=True, slots=True)
class GameEvent:
    kind: str
    priority: int
    summary: str
    timestamp: float
    details: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        stable = "|".join(f"{key}={self.details[key]}" for key in sorted(self.details))
        return f"{self.kind}|{stable}|{self.summary}"


@dataclass(frozen=True, slots=True)
class SideSnapshot:
    health: int | None = None
    armor: int = 0
    mana_available: int | None = None
    mana_max: int | None = None
    hand_count: int = 0
    deck_count: int = 0
    secret_count: int = 0
    board_count: int = 0
    board_attack: int = 0
    board_health: int = 0
    board_cards: tuple[str, ...] = ()

    @property
    def effective_health(self) -> int | None:
        return None if self.health is None else self.health + self.armor

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "armor": self.armor,
            "effective_health": self.effective_health,
            "mana_available": self.mana_available,
            "mana_max": self.mana_max,
            "hand_count": self.hand_count,
            "deck_count": self.deck_count,
            "secret_count": self.secret_count,
            "board": {
                "count": self.board_count,
                "attack": self.board_attack,
                "health": self.board_health,
                "cards": list(self.board_cards),
            },
        }


@dataclass(frozen=True, slots=True)
class ConstructedCardSnapshot:
    card_id: str = ""
    name: str = ""
    card_type: str = ""
    zone_position: int = 0
    cost: int | None = None
    attack: int | None = None
    health: int | None = None
    max_health: int | None = None
    durability: int | None = None
    exhausted: bool | None = None
    keywords: tuple[str, ...] = ()
    states: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "card_type": self.card_type,
            "zone_position": self.zone_position,
            "cost": self.cost,
            "attack": self.attack,
            "health": self.health,
            "max_health": self.max_health,
            "durability": self.durability,
            "exhausted": self.exhausted,
            "keywords": list(self.keywords),
            "states": list(self.states),
        }


@dataclass(frozen=True, slots=True)
class ConstructedHeroSnapshot:
    card_id: str = ""
    name: str = ""
    health: int | None = None
    armor: int = 0
    attack: int = 0
    states: tuple[str, ...] = ()

    @property
    def effective_health(self) -> int | None:
        return None if self.health is None else self.health + self.armor

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "health": self.health,
            "armor": self.armor,
            "effective_health": self.effective_health,
            "attack": self.attack,
            "states": list(self.states),
        }


@dataclass(frozen=True, slots=True)
class ConstructedSideSnapshot:
    hero: ConstructedHeroSnapshot | None = None
    mana_available: int | None = None
    mana_max: int | None = None
    overload_owed: int = 0
    overload_locked: int = 0
    hand_count: int = 0
    known_hand: tuple[ConstructedCardSnapshot, ...] = ()
    hand_identities_complete: bool = False
    deck_count: int = 0
    fatigue: int = 0
    cards_played_this_turn: int = 0
    secret_count: int = 0
    board: tuple[ConstructedCardSnapshot, ...] = ()
    weapon: ConstructedCardSnapshot | None = None
    hero_power: ConstructedCardSnapshot | None = None
    locations: tuple[ConstructedCardSnapshot, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "hero": self.hero.to_public_dict() if self.hero else None,
            "mana": {
                "available": self.mana_available,
                "maximum": self.mana_max,
                "overload_owed": self.overload_owed,
                "overload_locked": self.overload_locked,
            },
            "hand": {
                "count": self.hand_count,
                "known_cards": [card.to_public_dict() for card in self.known_hand],
                "identities_complete": self.hand_identities_complete,
            },
            "deck": {
                "count": self.deck_count,
                "fatigue": self.fatigue,
            },
            "cards_played_this_turn": self.cards_played_this_turn,
            "secrets": {"count": self.secret_count},
            "board": {
                "count": len(self.board),
                "attack": sum(card.attack or 0 for card in self.board),
                "health": sum(card.health or 0 for card in self.board),
                "minions": [card.to_public_dict() for card in self.board],
            },
            "weapon": self.weapon.to_public_dict() if self.weapon else None,
            "hero_power": self.hero_power.to_public_dict() if self.hero_power else None,
            "locations": [card.to_public_dict() for card in self.locations],
        }


@dataclass(frozen=True, slots=True)
class ConstructedSnapshot:
    game_type: str = ""
    format: str = "unknown"
    variant: str = "unknown"
    player: ConstructedSideSnapshot = field(default_factory=ConstructedSideSnapshot)
    opponent: ConstructedSideSnapshot = field(default_factory=ConstructedSideSnapshot)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "game_type": self.game_type,
            "format": self.format,
            "variant": self.variant,
            "player": self.player.to_public_dict(),
            "opponent": self.opponent.to_public_dict(),
            "capabilities": {
                "own_hand_identities_complete": self.player.hand_identities_complete,
                "complete_action_legality": False,
                "hidden_information": False,
                "automatic_gameplay": False,
            },
            "source": "power_log",
        }


@dataclass(frozen=True, slots=True)
class ChoiceSnapshot:
    choice_type: str = "unknown"
    count_min: int = 0
    count_max: int = 0
    source: ConstructedCardSnapshot | None = None
    options: tuple[ConstructedCardSnapshot, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "choice_type": self.choice_type,
            "count_min": self.count_min,
            "count_max": self.count_max,
            "source": self.source.to_public_dict() if self.source else None,
            "options": [card.to_public_dict() for card in self.options],
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsCardSnapshot:
    card_id: str = ""
    name: str = ""
    card_type: str | None = None
    attack: int = 0
    health: int | None = None
    tier: int = 0
    frozen: bool = False
    position: int = 0
    premium: bool | None = None
    current_cost: int | None = None
    keywords: dict[str, bool | None] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "card_type": self.card_type,
            "attack": self.attack,
            "health": self.health,
            "tier": self.tier,
            "frozen": self.frozen,
            "position": self.position,
            "premium": self.premium,
            "current_cost": self.current_cost,
            "keywords": dict(self.keywords),
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsHeroChoiceSnapshot:
    card_id: str = ""
    name: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsChoiceSnapshot:
    choice_type: str = "unknown"
    count_min: int = 0
    count_max: int = 0
    source: BattlegroundsCardSnapshot | None = None
    options: tuple[BattlegroundsCardSnapshot, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "choice_type": self.choice_type,
            "count_min": self.count_min,
            "count_max": self.count_max,
            "source": self.source.to_public_dict() if self.source else None,
            "options": [card.to_public_dict() for card in self.options],
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsAreaSnapshot:
    complete: bool = False
    revision: int = 0
    observed_at: float | None = None
    round: int = 0
    phase: str = "unknown"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "revision": self.revision,
            "observed_at": self.observed_at,
            "round": self.round,
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsEconomySnapshot:
    upgrade_cost: int | None = None
    refresh_cost: int | None = None
    revision: int = 0
    observed_at: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "upgrade_cost": self.upgrade_cost,
            "refresh_cost": self.refresh_cost,
            "revision": self.revision,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsPlayerSnapshot:
    player_id: int
    is_local: bool = False
    hero_card_id: str = ""
    hero_name: str = ""
    health: int | None = None
    armor: int = 0
    tavern_tier: int = 0
    triples: int = 0
    placement: int = 0
    eliminated: bool = False
    next_opponent: bool = False
    current_opponent: bool = False
    last_opponent: bool = False
    is_teammate: bool = False
    last_seen_round: int = 0
    board_count: int = 0
    board_attack: int = 0
    board_health: int = 0
    board_cards: tuple[str, ...] = ()
    board_minions: tuple[BattlegroundsCardSnapshot, ...] = ()

    @property
    def effective_health(self) -> int | None:
        return None if self.health is None else self.health + self.armor

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "is_local": self.is_local,
            "hero_card_id": self.hero_card_id,
            "hero_name": self.hero_name,
            "health": self.health,
            "armor": self.armor,
            "effective_health": self.effective_health,
            "tavern_tier": self.tavern_tier,
            "triples": self.triples,
            "placement": self.placement,
            "eliminated": self.eliminated,
            "next_opponent": self.next_opponent,
            "current_opponent": self.current_opponent,
            "last_opponent": self.last_opponent,
            "is_teammate": self.is_teammate,
            "last_seen_round": self.last_seen_round,
            "board": {
                "count": self.board_count,
                "attack": self.board_attack,
                "health": self.board_health,
                "cards": list(self.board_cards),
                "minions": [card.to_public_dict() for card in self.board_minions],
                "observed_in_combat": not self.is_local and self.last_seen_round > 0,
                "observed_round": self.last_seen_round if not self.is_local else 0,
            },
        }


@dataclass(frozen=True, slots=True)
class BattlegroundsSnapshot:
    variant: str = "solo"
    round: int = 0
    phase: str = "unknown"
    gold: int | None = None
    max_gold: int | None = None
    tavern_tier: int = 0
    frozen: bool = False
    next_opponent_player_id: int = 0
    current_opponent_player_id: int = 0
    last_opponent_player_id: int = 0
    last_opponent_round: int = 0
    placement: int = 0
    refresh_cost: int | None = None
    upgrade_cost: int | None = None
    hero_choices: tuple[BattlegroundsHeroChoiceSnapshot, ...] = ()
    shop: tuple[BattlegroundsCardSnapshot, ...] = ()
    hand: tuple[BattlegroundsCardSnapshot, ...] = ()
    warband: tuple[BattlegroundsCardSnapshot, ...] = ()
    lobby: tuple[BattlegroundsPlayerSnapshot, ...] = ()
    current_choice: BattlegroundsChoiceSnapshot | None = None
    economy: BattlegroundsEconomySnapshot = field(
        default_factory=BattlegroundsEconomySnapshot
    )
    areas: dict[str, BattlegroundsAreaSnapshot] = field(default_factory=dict)
    mechanics: dict[str, Any] = field(default_factory=dict)

    def _opponent_context(
        self,
        player_id: int,
        relationship: str,
        *,
        combat_round: int = 0,
    ) -> dict[str, Any] | None:
        if player_id <= 0:
            return None
        player = next(
            (
                candidate
                for candidate in self.lobby
                if candidate.player_id == player_id and not candidate.is_local
            ),
            None,
        )
        if player is None:
            return None
        payload = player.to_public_dict()
        return {
            "relationship": relationship,
            "player_id": player.player_id,
            "hero": {
                "card_id": player.hero_card_id,
                "name": player.hero_name,
            },
            "health": player.health,
            "armor": player.armor,
            "effective_health": player.effective_health,
            "tavern_tier": player.tavern_tier,
            "placement": player.placement,
            "eliminated": player.eliminated,
            "is_teammate": player.is_teammate,
            "combat_round": combat_round,
            "board": payload["board"],
        }

    def to_public_dict(self) -> dict[str, Any]:
        opponent_context = {
            "current": self._opponent_context(
                self.current_opponent_player_id,
                "current",
                combat_round=self.round if self.current_opponent_player_id else 0,
            ),
            "next": self._opponent_context(
                self.next_opponent_player_id,
                "next",
            ),
            "last": self._opponent_context(
                self.last_opponent_player_id,
                "last",
                combat_round=self.last_opponent_round,
            ),
        }
        return {
            "variant": self.variant,
            "round": self.round,
            "phase": self.phase,
            "gold": self.gold,
            "max_gold": self.max_gold,
            "tavern_tier": self.tavern_tier,
            "frozen": self.frozen,
            "next_opponent_player_id": self.next_opponent_player_id,
            "current_opponent_player_id": self.current_opponent_player_id,
            "last_opponent_player_id": self.last_opponent_player_id,
            "last_opponent_round": self.last_opponent_round,
            "opponents": opponent_context,
            "placement": self.placement,
            "refresh_cost": self.refresh_cost,
            "upgrade_cost": self.upgrade_cost,
            "hero_choices": [choice.to_public_dict() for choice in self.hero_choices],
            "shop": [card.to_public_dict() for card in self.shop],
            "hand": [card.to_public_dict() for card in self.hand],
            "warband": [card.to_public_dict() for card in self.warband],
            "lobby": [player.to_public_dict() for player in self.lobby],
            "current_choice": (
                self.current_choice.to_public_dict() if self.current_choice else None
            ),
            "economy": self.economy.to_public_dict(),
            "areas": {
                key: area.to_public_dict() for key, area in sorted(self.areas.items())
            },
            "mechanics": dict(self.mechanics),
            "source": "power_log",
        }


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    mode: str = "unknown"
    phase: str = "idle"
    game_number: int = 0
    turn: int = 0
    round: int = 0
    active_side: str = "unknown"
    player: SideSnapshot = field(default_factory=SideSnapshot)
    opponent: SideSnapshot = field(default_factory=SideSnapshot)
    recent_cards: tuple[dict[str, Any], ...] = ()
    result: str = ""
    constructed: ConstructedSnapshot | None = None
    battlegrounds: BattlegroundsSnapshot | None = None
    choice: ChoiceSnapshot | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "phase": self.phase,
            "game_number": self.game_number,
            "turn": self.turn,
            "round": self.round,
            "active_side": self.active_side,
            "player": self.player.to_public_dict(),
            "opponent": self.opponent.to_public_dict(),
            "recent_cards": [dict(item) for item in self.recent_cards],
            "result": self.result,
            "constructed": self.constructed.to_public_dict() if self.constructed else None,
            "battlegrounds": self.battlegrounds.to_public_dict() if self.battlegrounds else None,
            "choice": self.choice.to_public_dict() if self.choice else None,
        }


@dataclass(slots=True)
class RuntimeStatus:
    monitor_running: bool = False
    source_state: str = "waiting"
    resolved_log_path: str = ""
    lines_seen: int = 0
    events_seen: int = 0
    llm_submissions: int = 0
    source_modified_at: float = 0.0
    last_line_at: float = 0.0
    last_state_at: float = 0.0
    last_event_at: float = 0.0
    last_event_kind: str = ""
    last_error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitor_running": self.monitor_running,
            "source_state": self.source_state,
            "resolved_log_path": self.resolved_log_path,
            "lines_seen": self.lines_seen,
            "events_seen": self.events_seen,
            "llm_submissions": self.llm_submissions,
            "source_modified_at": self.source_modified_at,
            "last_line_at": self.last_line_at,
            "last_state_at": self.last_state_at,
            "last_event_at": self.last_event_at,
            "last_event_kind": self.last_event_kind,
            "last_error_code": self.last_error_code,
        }
