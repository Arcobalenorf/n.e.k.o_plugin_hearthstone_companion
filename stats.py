from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_SAFE_COUNTER = 9_007_199_254_740_991
MAX_SEASONS = 128
MAX_HEROES_PER_MODE = 512

_MODE_RULES = {
    "solo": {"maximum_placement": 8, "top_threshold": 4, "top_key": "top4"},
    "duos": {"maximum_placement": 4, "top_threshold": 2, "top_key": "top2"},
}


def _normalize_label(value: object, *, field_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name} must be at most {maximum_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} must not contain control characters")
    return normalized


def _normalize_mode(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("mode must be a string")
    if value not in _MODE_RULES:
        raise ValueError("mode must be 'solo' or 'duos'")
    return value


def _validate_counter(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if not 0 <= value <= MAX_SAFE_COUNTER:
        raise ValueError(f"{field_name} must be between 0 and {MAX_SAFE_COUNTER}")
    return value


@dataclass(slots=True)
class _Counters:
    games: int = 0
    top_finishes: int = 0
    first_places: int = 0
    placement_sum: int = 0

    def validate_increment(self, placement: int) -> None:
        if self.games >= MAX_SAFE_COUNTER or self.placement_sum > MAX_SAFE_COUNTER - placement:
            raise OverflowError("statistics counters exceed the JSON-safe integer range")

    def record(self, placement: int, *, top_threshold: int) -> None:
        self.games += 1
        self.placement_sum += placement
        if placement <= top_threshold:
            self.top_finishes += 1
        if placement == 1:
            self.first_places += 1

    def to_store_dict(self) -> dict[str, int]:
        return {
            "games": self.games,
            "top_finishes": self.top_finishes,
            "first_places": self.first_places,
            "placement_sum": self.placement_sum,
        }

    def to_public_dict(self, *, top_key: str) -> dict[str, int | float | None]:
        average = round(self.placement_sum / self.games, 2) if self.games else None
        top_rate = round(self.top_finishes * 100 / self.games, 1) if self.games else None
        first_rate = round(self.first_places * 100 / self.games, 1) if self.games else None
        return {
            "games": self.games,
            top_key: self.top_finishes,
            f"{top_key}_rate": top_rate,
            "first": self.first_places,
            "first_rate": first_rate,
            "average_placement": average,
        }


@dataclass(slots=True)
class _ModeStats:
    aggregate: _Counters = field(default_factory=_Counters)
    heroes: dict[str, _Counters] = field(default_factory=dict)


class BattlegroundsStats:
    """Thread-safe, aggregate-only local Battlegrounds statistics."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._seasons: dict[str, dict[str, _ModeStats]] = {}

    @classmethod
    def from_store_dict(cls, value: Mapping[str, Any] | None) -> BattlegroundsStats:
        stats = cls()
        if value is None:
            return stats
        if not isinstance(value, Mapping):
            raise TypeError("stored statistics must be a mapping")

        schema_version = _validate_counter(value.get("schema_version"), field_name="schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported statistics schema_version: {schema_version}")
        raw_seasons = value.get("seasons")
        if not isinstance(raw_seasons, Mapping):
            raise TypeError("seasons must be a mapping")
        if len(raw_seasons) > MAX_SEASONS:
            raise ValueError(f"seasons must contain at most {MAX_SEASONS} entries")

        restored: dict[str, dict[str, _ModeStats]] = {}
        for raw_season, raw_modes in raw_seasons.items():
            season = _normalize_label(raw_season, field_name="season", maximum_length=64)
            if season != raw_season:
                raise ValueError("stored season keys must not have leading or trailing whitespace")
            if not isinstance(raw_modes, Mapping):
                raise TypeError(f"season {season!r} must contain a mode mapping")

            modes: dict[str, _ModeStats] = {}
            for raw_mode, raw_mode_stats in raw_modes.items():
                mode = _normalize_mode(raw_mode)
                if not isinstance(raw_mode_stats, Mapping):
                    raise TypeError(f"statistics for {season!r}/{mode} must be a mapping")
                modes[mode] = cls._restore_mode(raw_mode_stats, mode=mode)
            restored[season] = modes

        stats._seasons = restored
        return stats

    @staticmethod
    def _restore_mode(value: Mapping[str, Any], *, mode: str) -> _ModeStats:
        rules = _MODE_RULES[mode]
        aggregate = BattlegroundsStats._restore_counters(
            value,
            field_name=f"{mode} aggregate",
            maximum_placement=rules["maximum_placement"],
        )
        raw_heroes = value.get("heroes")
        if not isinstance(raw_heroes, Mapping):
            raise TypeError(f"{mode} heroes must be a mapping")
        if len(raw_heroes) > MAX_HEROES_PER_MODE:
            raise ValueError(f"{mode} heroes must contain at most {MAX_HEROES_PER_MODE} entries")

        heroes: dict[str, _Counters] = {}
        for raw_hero_id, raw_counters in raw_heroes.items():
            hero_id = _normalize_label(raw_hero_id, field_name="hero_id", maximum_length=128)
            if hero_id != raw_hero_id:
                raise ValueError("stored hero_id keys must not have leading or trailing whitespace")
            if not isinstance(raw_counters, Mapping):
                raise TypeError(f"hero statistics for {hero_id!r} must be a mapping")
            heroes[hero_id] = BattlegroundsStats._restore_counters(
                raw_counters,
                field_name=f"hero {hero_id!r}",
                maximum_placement=rules["maximum_placement"],
            )

        if aggregate.games == 0:
            raise ValueError(f"stored {mode} statistics must contain at least one game")
        for field_name in ("games", "top_finishes", "first_places", "placement_sum"):
            hero_total = sum(getattr(counters, field_name) for counters in heroes.values())
            if hero_total != getattr(aggregate, field_name):
                raise ValueError(f"{mode} aggregate {field_name} does not match its hero totals")
        return _ModeStats(aggregate=aggregate, heroes=heroes)

    @staticmethod
    def _restore_counters(
        value: Mapping[str, Any],
        *,
        field_name: str,
        maximum_placement: int,
    ) -> _Counters:
        counters = _Counters(
            games=_validate_counter(value.get("games"), field_name=f"{field_name}.games"),
            top_finishes=_validate_counter(
                value.get("top_finishes"), field_name=f"{field_name}.top_finishes"
            ),
            first_places=_validate_counter(value.get("first_places"), field_name=f"{field_name}.first_places"),
            placement_sum=_validate_counter(value.get("placement_sum"), field_name=f"{field_name}.placement_sum"),
        )
        if counters.top_finishes > counters.games:
            raise ValueError(f"{field_name}.top_finishes must not exceed games")
        if counters.first_places > counters.top_finishes:
            raise ValueError(f"{field_name}.first_places must not exceed top_finishes")
        if counters.games == 0:
            if counters != _Counters():
                raise ValueError(f"empty {field_name} counters must all be zero")
        elif not counters.games <= counters.placement_sum <= counters.games * maximum_placement:
            raise ValueError(f"{field_name}.placement_sum is outside the valid placement range")
        return counters

    def record_game(self, *, season: str, mode: str, placement: int, hero_id: str) -> None:
        normalized_season = _normalize_label(season, field_name="season", maximum_length=64)
        normalized_mode = _normalize_mode(mode)
        normalized_hero_id = _normalize_label(hero_id, field_name="hero_id", maximum_length=128)
        if isinstance(placement, bool) or not isinstance(placement, int):
            raise TypeError("placement must be an integer")

        rules = _MODE_RULES[normalized_mode]
        maximum_placement = rules["maximum_placement"]
        if not 1 <= placement <= maximum_placement:
            raise ValueError(f"placement for {normalized_mode} must be between 1 and {maximum_placement}")

        with self._lock:
            if normalized_season not in self._seasons and len(self._seasons) >= MAX_SEASONS:
                raise OverflowError(f"statistics cannot contain more than {MAX_SEASONS} seasons")
            season_stats = self._seasons.setdefault(normalized_season, {})
            mode_stats = season_stats.setdefault(normalized_mode, _ModeStats())
            if (
                normalized_hero_id not in mode_stats.heroes
                and len(mode_stats.heroes) >= MAX_HEROES_PER_MODE
            ):
                raise OverflowError(
                    f"statistics cannot contain more than {MAX_HEROES_PER_MODE} heroes per mode"
                )
            hero_stats = mode_stats.heroes.setdefault(normalized_hero_id, _Counters())
            mode_stats.aggregate.validate_increment(placement)
            hero_stats.validate_increment(placement)
            mode_stats.aggregate.record(placement, top_threshold=rules["top_threshold"])
            hero_stats.record(placement, top_threshold=rules["top_threshold"])

    def clear(self) -> None:
        with self._lock:
            self._seasons.clear()

    def to_store_dict(self) -> dict[str, Any]:
        with self._lock:
            seasons: dict[str, Any] = {}
            for season, modes in sorted(self._seasons.items()):
                seasons[season] = {}
                for mode, mode_stats in sorted(modes.items()):
                    seasons[season][mode] = {
                        **mode_stats.aggregate.to_store_dict(),
                        "heroes": {
                            hero_id: counters.to_store_dict()
                            for hero_id, counters in sorted(mode_stats.heroes.items())
                        },
                    }
            return {"schema_version": SCHEMA_VERSION, "seasons": seasons}

    def to_public_dict(self) -> dict[str, Any]:
        with self._lock:
            seasons: dict[str, Any] = {}
            for season, modes in sorted(self._seasons.items()):
                seasons[season] = {}
                for mode, mode_stats in sorted(modes.items()):
                    top_key = _MODE_RULES[mode]["top_key"]
                    seasons[season][mode] = {
                        **mode_stats.aggregate.to_public_dict(top_key=top_key),
                        "heroes": {
                            hero_id: counters.to_public_dict(top_key=top_key)
                            for hero_id, counters in sorted(mode_stats.heroes.items())
                        },
                    }
            return {"schema_version": SCHEMA_VERSION, "seasons": seasons}


__all__ = ["BattlegroundsStats", "SCHEMA_VERSION"]
