from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from hearthstone_companion_under_test.stats import SCHEMA_VERSION, BattlegroundsStats


def test_empty_stats_are_json_compatible() -> None:
    stats = BattlegroundsStats()

    assert stats.to_store_dict() == {"schema_version": SCHEMA_VERSION, "seasons": {}}
    assert stats.to_public_dict() == {"schema_version": SCHEMA_VERSION, "seasons": {}}
    assert json.loads(json.dumps(stats.to_store_dict())) == stats.to_store_dict()


def test_aggregates_seasons_modes_and_heroes_without_game_history() -> None:
    stats = BattlegroundsStats()
    stats.record_game(season="S10", mode="solo", placement=1, hero_id="TB_BaconShop_HERO_01")
    stats.record_game(season="S10", mode="solo", placement=5, hero_id="TB_BaconShop_HERO_01")
    stats.record_game(season="S10", mode="solo", placement=3, hero_id="TB_BaconShop_HERO_02")
    stats.record_game(season="S10", mode="duos", placement=2, hero_id="TB_BaconShop_HERO_01")
    stats.record_game(season="S10", mode="duos", placement=4, hero_id="TB_BaconShop_HERO_01")
    stats.record_game(season="S11", mode="solo", placement=8, hero_id="TB_BaconShop_HERO_03")

    public = stats.to_public_dict()["seasons"]
    assert public["S10"]["solo"] == {
        "games": 3,
        "top4": 2,
        "top4_rate": 66.7,
        "first": 1,
        "first_rate": 33.3,
        "average_placement": 3.0,
        "heroes": {
            "TB_BaconShop_HERO_01": {
                "games": 2,
                "top4": 1,
                "top4_rate": 50.0,
                "first": 1,
                "first_rate": 50.0,
                "average_placement": 3.0,
            },
            "TB_BaconShop_HERO_02": {
                "games": 1,
                "top4": 1,
                "top4_rate": 100.0,
                "first": 0,
                "first_rate": 0.0,
                "average_placement": 3.0,
            },
        },
    }
    assert public["S10"]["duos"]["top2"] == 1
    assert public["S10"]["duos"]["average_placement"] == 3.0
    assert public["S11"]["solo"]["average_placement"] == 8.0

    encoded = json.dumps(stats.to_store_dict())
    assert "history" not in encoded.lower()
    assert "player" not in encoded.lower()
    assert "account" not in encoded.lower()


def test_store_round_trip_is_exact_and_outputs_are_detached() -> None:
    stats = BattlegroundsStats()
    stats.record_game(season="S10", mode="solo", placement=2, hero_id="HERO_A")
    stats.record_game(season="S10", mode="solo", placement=7, hero_id="HERO_B")
    stored = json.loads(json.dumps(stats.to_store_dict()))

    restored = BattlegroundsStats.from_store_dict(stored)
    assert restored.to_store_dict() == stored
    assert restored.to_public_dict() == stats.to_public_dict()

    stored["seasons"]["S10"]["solo"]["games"] = 999
    public = restored.to_public_dict()
    public["seasons"]["S10"]["solo"]["heroes"]["HERO_A"]["games"] = 999
    assert restored.to_store_dict()["seasons"]["S10"]["solo"]["games"] == 2
    assert restored.to_public_dict()["seasons"]["S10"]["solo"]["heroes"]["HERO_A"]["games"] == 1


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"season": "", "mode": "solo", "placement": 1, "hero_id": "HERO"}, ValueError),
        ({"season": "S10", "mode": "ranked", "placement": 1, "hero_id": "HERO"}, ValueError),
        ({"season": "S10", "mode": "solo", "placement": 0, "hero_id": "HERO"}, ValueError),
        ({"season": "S10", "mode": "solo", "placement": 9, "hero_id": "HERO"}, ValueError),
        ({"season": "S10", "mode": "duos", "placement": 5, "hero_id": "HERO"}, ValueError),
        ({"season": "S10", "mode": "solo", "placement": True, "hero_id": "HERO"}, TypeError),
        ({"season": "S10", "mode": "solo", "placement": 1.0, "hero_id": "HERO"}, TypeError),
        ({"season": "S10", "mode": "solo", "placement": 1, "hero_id": ""}, ValueError),
        ({"season": "S\n10", "mode": "solo", "placement": 1, "hero_id": "HERO"}, ValueError),
    ],
)
def test_record_game_validates_inputs(kwargs: dict[str, object], error: type[Exception]) -> None:
    stats = BattlegroundsStats()

    with pytest.raises(error):
        stats.record_game(**kwargs)  # type: ignore[arg-type]

    assert stats.to_store_dict()["seasons"] == {}


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"schema_version": 99, "seasons": {}},
        {"schema_version": SCHEMA_VERSION, "seasons": []},
        {
            "schema_version": SCHEMA_VERSION,
            "seasons": {
                "S10": {
                    "solo": {
                        "games": 1,
                        "top_finishes": 2,
                        "first_places": 0,
                        "placement_sum": 1,
                        "heroes": {},
                    }
                }
            },
        },
        {
            "schema_version": SCHEMA_VERSION,
            "seasons": {
                "S10": {
                    "duos": {
                        "games": 1,
                        "top_finishes": 1,
                        "first_places": 0,
                        "placement_sum": 5,
                        "heroes": {
                            "HERO": {
                                "games": 1,
                                "top_finishes": 1,
                                "first_places": 0,
                                "placement_sum": 5,
                            }
                        },
                    }
                }
            },
        },
    ],
)
def test_restore_rejects_malformed_or_inconsistent_store_data(stored: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        BattlegroundsStats.from_store_dict(stored)  # type: ignore[arg-type]


def test_restore_rejects_aggregate_that_does_not_match_hero_totals() -> None:
    stats = BattlegroundsStats()
    stats.record_game(season="S10", mode="solo", placement=1, hero_id="HERO")
    stored = stats.to_store_dict()
    stored["seasons"]["S10"]["solo"]["games"] = 2
    stored["seasons"]["S10"]["solo"]["placement_sum"] = 2

    with pytest.raises(ValueError, match="does not match"):
        BattlegroundsStats.from_store_dict(stored)


def test_concurrent_recording_is_lossless() -> None:
    stats = BattlegroundsStats()

    def record_batch(worker: int) -> None:
        for index in range(500):
            stats.record_game(
                season="S10",
                mode="solo",
                placement=(index % 8) + 1,
                hero_id=f"HERO_{worker % 4}",
            )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(record_batch, range(12)))

    solo = stats.to_public_dict()["seasons"]["S10"]["solo"]
    assert solo["games"] == 6_000
    assert solo["top4"] == 3_024
    assert solo["first"] == 756
    assert sum(hero["games"] for hero in solo["heroes"].values()) == 6_000
    assert BattlegroundsStats.from_store_dict(stats.to_store_dict()).to_public_dict() == stats.to_public_dict()
