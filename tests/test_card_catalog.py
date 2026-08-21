from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hearthstone_companion_under_test import card_catalog as catalog_module
from hearthstone_companion_under_test.card_catalog import (
    BattlegroundsCardCatalog,
    CatalogError,
    clean_rules_text,
    project_card,
)
from hearthstone_companion_under_test.models import (
    BattlegroundsCardSnapshot,
    BattlegroundsChoiceSnapshot,
    BattlegroundsHeroChoiceSnapshot,
    BattlegroundsPlayerSnapshot,
    BattlegroundsSnapshot,
)


def _logger() -> SimpleNamespace:
    return SimpleNamespace(warning=lambda *args, **kwargs: None)


def _raw_card(
    provider_id: int,
    external_id: str,
    name: str,
    *,
    card_type: str = "minion",
    pool: bool = True,
    tier: int = 3,
    tribes: list[str] | None = None,
    golden_id: int = 0,
    categories: list[str] | None = None,
    text: str = "<b>Battlecry:</b> Gain &lt;2&gt; Attack.<br>Then attack.",
    child_ids: list[int] | None = None,
    parent_id: int | None = None,
    is_hero: bool | None = None,
    is_hero_skin: bool = False,
) -> dict[str, Any]:
    return {
        "id": provider_id,
        "externalId": external_id,
        "name": name,
        "text": text,
        "textGold": "<b>Battlecry:</b> Gain 4 Attack.",
        "cardType": card_type,
        "tier": tier,
        "minionTypes": tribes or [],
        "keywords": ["Battlecry"],
        "categories": ["tavern"] if categories is None else categories,
        "pool": pool,
        "isDuosOnly": False,
        "isSolosOnly": False,
        "dbfIdGold": golden_id,
        "childIds": child_ids or [],
        "parentId": parent_id,
        "isHero": card_type == "hero" if is_hero is None else is_hero,
        "isHeroSkin": is_hero_skin,
    }


def _payload(cards: list[dict[str, Any]], *, fetched_at: float = 1000.0) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "provider": "hsbg.cards",
        "patch": "36.2.2",
        "season": "Season 14",
        "fetched_at": fetched_at,
        "checked_at": fetched_at,
        "cards": [project_card(card) for card in cards],
    }


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def test_rules_text_is_plain_bounded_reference_data() -> None:
    value = "<b>Battlecry:</b> Gain &lt;2&gt;\x00 Attack.<br>" + " x" * 500

    cleaned = clean_rules_text(value, maximum=80)

    assert cleaned.startswith("Battlecry: Gain <2> Attack.")
    assert "<b>" not in cleaned
    assert "\x00" not in cleaned
    assert len(cleaned) == 80


def test_card_projection_labels_direct_pool_without_treating_tokens_as_removed() -> None:
    pooled = project_card(_raw_card(1, "BG_TEST", "Pooled", tribes=["Mech", "Beast"]))
    token = project_card(
        _raw_card(2, "BGS_115t", "Water Droplet", pool=True, tier=1, categories=["token"])
    )
    hero_power = project_card(
        _raw_card(
            3,
            "TB_BaconShop_HP_033t",
            "Amalgam",
            card_type="hero_power",
            pool=True,
            tier=1,
            categories=["heroPower"],
        )
    )
    hero = project_card(_raw_card(4, "BG_HERO", "Hero", card_type="hero", pool=True, tier=0))

    assert pooled is not None and pooled["direct_tavern_pool"] is True
    assert pooled["tribes"] == ["Mech", "Beast"]
    assert token is not None and token["provider_current_pool"] is True
    assert token["direct_tavern_pool"] is False
    assert hero_power is not None and hero_power["direct_tavern_pool"] is False
    assert hero is not None and hero["provider_current_pool"] is True
    assert hero["direct_tavern_pool"] is False


def test_hero_choice_resolves_only_its_public_hero_power_relation(tmp_path: Path) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(
        cache,
        _payload(
            [
                _raw_card(
                    57633,
                    "TB_BaconShop_HERO_01",
                    "Edwin VanCleef",
                    card_type="hero",
                    tier=0,
                    text="",
                    child_ids=[57567, 88943],
                ),
                _raw_card(
                    57567,
                    "TB_BaconShop_HP_001",
                    "Sharpen Blades",
                    card_type="hero_power",
                    tier=0,
                    text="Give a minion +2/+2. Improves after you buy 4 cards.",
                    parent_id=57633,
                    categories=["heroPower"],
                ),
                _raw_card(
                    88943,
                    "BG20_HERO_201_Buddy",
                    "SI:7 Scout",
                    card_type="minion",
                    text="A public buddy rule that must not become the hero power.",
                    parent_id=57633,
                    categories=["buddy"],
                ),
            ],
        ),
    )
    catalog = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1001.0)
    snapshot = BattlegroundsSnapshot(
        hero_choices=(
            BattlegroundsHeroChoiceSnapshot(
                card_id="TB_BaconShop_HERO_01",
                name="Edwin VanCleef",
            ),
        ),
    )

    result = catalog.facts_for(snapshot)

    fact = result["observed_card_facts"]["TB_BaconShop_HERO_01"]
    assert fact["rules_text"] == ""
    assert fact["is_hero"] is True
    assert fact["is_hero_skin"] is False
    assert fact["hero_power"] == {
        "catalog_ref": "hsbg.cards:57567",
        "card_id": "TB_BaconShop_HP_001",
        "name": "Sharpen Blades",
        "rules_text": "Give a minion +2/+2. Improves after you buy 4 cards.",
        "untrusted_reference_text": True,
    }


def test_hero_skin_does_not_inherit_unavailable_provider_relationships(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(
        cache,
        _payload(
            [
                _raw_card(
                    57944,
                    "TB_BaconShop_HERO_16",
                    "A. F. Kay",
                    card_type="hero",
                    tier=0,
                    text="",
                    child_ids=[59891, 102495],
                ),
                _raw_card(
                    59891,
                    "TB_BaconShop_HP_044",
                    "Procrastinate",
                    card_type="hero_power",
                    tier=0,
                    text="Skip your first 2 turns. Start with 2 minions from Tavern Tier 3.",
                    parent_id=57944,
                    categories=["heroPower"],
                ),
                _raw_card(
                    102495,
                    "TB_BaconShop_HERO_16_SKIN_F",
                    "Study Kay",
                    card_type="hero",
                    tier=0,
                    text="",
                    parent_id=57944,
                    is_hero=True,
                    is_hero_skin=True,
                ),
            ],
        ),
    )
    catalog = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1001.0)
    snapshot = BattlegroundsSnapshot(
        hero_choices=(
            BattlegroundsHeroChoiceSnapshot(card_id="TB_BaconShop_HERO_16_SKIN_F"),
        ),
    )

    fact = catalog.facts_for(snapshot)["observed_card_facts"][
        "TB_BaconShop_HERO_16_SKIN_F"
    ]

    assert fact["is_hero_skin"] is True
    assert "hero_power" not in fact


@pytest.mark.parametrize(
    ("hero", "related"),
    [
        (
            _raw_card(
                1,
                "BG_HERO_MISSING_POWER",
                "Missing Power",
                card_type="hero",
                text="",
                child_ids=[999],
            ),
            [],
        ),
        (
            _raw_card(
                2,
                "BG_HERO_BUDDY_ONLY",
                "Buddy Only",
                card_type="hero",
                text="",
                child_ids=[20],
            ),
            [
                _raw_card(
                    20,
                    "BG_BUDDY_TOKEN",
                    "Not a Power",
                    card_type="minion",
                    parent_id=2,
                )
            ],
        ),
        (
            _raw_card(
                3,
                "BG_HERO_WRONG_PARENT",
                "Wrong Parent",
                card_type="hero",
                text="",
                child_ids=[30],
            ),
            [
                _raw_card(
                    30,
                    "BG_HP_WRONG_PARENT",
                    "Wrong Parent Power",
                    card_type="hero_power",
                    parent_id=999,
                )
            ],
        ),
        (
            _raw_card(
                4,
                "BG_HERO_AMBIGUOUS",
                "Ambiguous",
                card_type="hero",
                text="",
                child_ids=[40, 41],
            ),
            [
                _raw_card(40, "BG_HP_A", "Power A", card_type="hero_power", parent_id=4),
                _raw_card(41, "BG_HP_B", "Power B", card_type="hero_power", parent_id=4),
            ],
        ),
        (
            _raw_card(
                5,
                "BG_HERO_SKIN_ORPHAN",
                "Orphan Skin",
                card_type="hero",
                text="",
                parent_id=500,
                is_hero=True,
                is_hero_skin=True,
            ),
            [],
        ),
        (
            _raw_card(
                6,
                "BG_HERO_FALSE_BASE",
                "False Base",
                card_type="hero",
                text="",
                child_ids=[60],
                parent_id=600,
            ),
            [
                _raw_card(
                    60,
                    "BG_HP_FALSE_BASE",
                    "False Base Power",
                    card_type="hero_power",
                    parent_id=6,
                )
            ],
        ),
    ],
)
def test_missing_or_untrusted_hero_relationships_fail_closed(
    tmp_path: Path,
    hero: dict[str, Any],
    related: list[dict[str, Any]],
) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(cache, _payload([hero, *related]))
    catalog = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1001.0)
    snapshot = BattlegroundsSnapshot(
        hero_choices=(BattlegroundsHeroChoiceSnapshot(card_id=hero["externalId"]),),
    )

    fact = catalog.facts_for(snapshot)["observed_card_facts"][hero["externalId"]]

    assert "hero_power" not in fact


def test_malformed_or_oversized_relationship_fields_are_discarded() -> None:
    malformed = _raw_card(1, "BG_HERO_BAD_RELATIONS", "Bad Relations", card_type="hero")
    malformed["childIds"] = [2, True]
    malformed["parentId"] = "2"
    oversized = _raw_card(2, "BG_HERO_TOO_MANY", "Too Many", card_type="hero")
    oversized["childIds"] = list(range(1, 34))

    malformed_projection = project_card(malformed)
    oversized_projection = project_card(oversized)

    assert malformed_projection is not None
    assert malformed_projection["child_provider_ids"] == []
    assert malformed_projection["parent_provider_id"] == 0
    assert oversized_projection is not None
    assert oversized_projection["child_provider_ids"] == []


def test_valid_cache_is_loaded_and_bad_cache_degrades_without_raising(tmp_path: Path) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(cache, _payload([_raw_card(1, "BG_TEST", "Test")]))

    loaded = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1100.0)
    assert loaded.status()["available"] is True
    assert loaded.status()["dataset"]["patch"] == "36.2.2"

    cache.write_bytes(b"not-gzip")
    broken = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1100.0)
    assert broken.status()["available"] is False
    assert broken.status()["degraded_reason"] == "cache_invalid"


def test_previous_cache_schema_is_rejected_instead_of_losing_hero_relationships(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "catalog.json.gz"
    payload = _payload([_raw_card(1, "BG_OLD_SCHEMA", "Old Schema")])
    payload["schema_version"] = 1
    _write_cache(cache, payload)

    catalog = BattlegroundsCardCatalog(
        cache,
        _logger(),
        network_enabled=False,
        now=lambda: 1100.0,
    )

    assert catalog.status()["available"] is False
    assert catalog.status()["degraded_reason"] == "cache_invalid"


def test_refresh_pages_current_catalog_and_writes_atomic_cache(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(
        tmp_path / "catalog.json.gz", _logger(), now=lambda: 2000.0
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_request(path: str, query: dict[str, Any], **_: Any) -> dict[str, Any]:
        calls.append((path, query))
        if path == "/patches":
            return {"data": [{"currentPatch": "36.2.2", "season": "Season 14"}]}
        if query["offset"] == 0:
            return {
                "data": [_raw_card(1, "BG_A", "A")],
                "pagination": {"total": 2, "nextOffset": 1},
            }
        return {
            "data": [_raw_card(2, "BG_B", "B", card_type="spell", tribes=[])],
            "pagination": {"total": 2, "nextOffset": None},
        }

    catalog._request_json = fake_request  # type: ignore[method-assign]

    assert catalog.refresh_once() is True
    assert [path for path, _ in calls] == ["/patches", "/cards", "/cards"]
    assert all(call[1].get("pool") == "current" for call in calls[1:])
    assert catalog.status()["card_count"] == 2
    assert catalog.status()["active_pool_summary"]["provider_pool_counts_by_type"] == {
        "minion": 1,
        "spell": 1,
    }
    assert (tmp_path / "catalog.json.gz").is_file()


def test_replace_failure_degrades_without_leaking_staged_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "catalog.json.gz"
    catalog = BattlegroundsCardCatalog(cache, _logger(), now=lambda: 2000.0)
    payload = _payload([_raw_card(1, "BG_MEMORY_ONLY", "Memory Only")])
    catalog._fetch_payload = lambda *_args: payload  # type: ignore[method-assign]

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace unavailable")

    monkeypatch.setattr(catalog_module.os, "replace", fail_replace)

    assert catalog.refresh_once() is True
    assert catalog.status()["available"] is True
    assert catalog.status()["degraded_reason"] == "cache_write_failed"
    assert cache.exists() is False
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_refresh_retains_previous_cache_and_marks_reason(tmp_path: Path) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(cache, _payload([_raw_card(1, "BG_OLD", "Old")], fetched_at=1000.0))
    catalog = BattlegroundsCardCatalog(cache, _logger(), now=lambda: 5000.0)
    catalog._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        CatalogError("network_timeout")
    )

    assert catalog.refresh_once() is False
    status = catalog.status()
    assert status["available"] is True
    assert status["card_count"] == 1
    assert status["degraded_reason"] == "network_timeout"
    assert status["retry_delay_seconds"] == 300.0


def test_failed_refresh_uses_bounded_backoff_and_manual_refresh_bypasses_it(tmp_path: Path) -> None:
    now = [10_000.0]
    catalog = BattlegroundsCardCatalog(
        tmp_path / "catalog.json.gz", _logger(), now=lambda: now[0]
    )
    catalog._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        CatalogError("http_429")
    )

    assert catalog.refresh_once() is False
    assert catalog._seconds_until_refresh() == 300.0
    catalog.request_refresh()
    assert catalog._seconds_until_refresh() == 0.0
    catalog._force_refresh = False
    assert catalog.refresh_once() is False
    assert catalog._seconds_until_refresh() == 900.0


def test_observed_facts_are_deduplicated_and_keep_zone_order(tmp_path: Path) -> None:
    cache = tmp_path / "catalog.json.gz"
    _write_cache(
        cache,
        _payload(
            [
                _raw_card(1, "BG_TID_713", "A", tribes=["Mech"], golden_id=101),
                _raw_card(2, "BG_HERO", "Hero", card_type="hero", tier=0),
                _raw_card(3, "BG_HERO_CHOICE", "Choice", card_type="hero", tier=0),
                _raw_card(
                    4,
                    "BG_CHOICE_SPELL",
                    "Choice Spell",
                    card_type="spell",
                    tier=0,
                ),
            ],
            fetched_at=1000.0,
        ),
    )
    catalog = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1001.0)
    snapshot = BattlegroundsSnapshot(
        current_choice=BattlegroundsChoiceSnapshot(
            options=(BattlegroundsCardSnapshot(card_id="BG_CHOICE_SPELL"),),
        ),
        hero_choices=(BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_CHOICE"),),
        shop=(BattlegroundsCardSnapshot(card_id="BG_TID_713", attack=9, health=8),),
        hand=(BattlegroundsCardSnapshot(card_id="BG_TID_713"),),
        warband=(BattlegroundsCardSnapshot(card_id="BG_TID_713_G", attack=20, health=20),),
        lobby=(
            BattlegroundsPlayerSnapshot(player_id=1, hero_card_id="BG_HERO"),
            BattlegroundsPlayerSnapshot(
                player_id=2,
                last_seen_round=3,
                board_minions=(
                    BattlegroundsCardSnapshot(card_id="BG_TID_713"),
                ),
            ),
        ),
    )

    result = catalog.facts_for(snapshot)

    assert result["coverage"]["zone_ids"] == {
        "current_choice": ["BG_CHOICE_SPELL"],
        "hero_choices": ["BG_HERO_CHOICE"],
        "shop": ["BG_TID_713"],
        "hand": ["BG_TID_713"],
        "warband": ["BG_TID_713_G"],
        "mechanics": [],
        "heroes": ["BG_HERO"],
        "observed_opponent_boards": ["BG_TID_713"],
    }
    assert result["coverage"]["unique_observed_count"] == 5
    assert result["coverage"]["queried_count"] == 5
    assert result["coverage"]["resolved_count"] == 5
    assert result["observed_card_facts"]["BG_HERO_CHOICE"]["card_type"] == "hero"
    assert result["observed_card_facts"]["BG_TID_713"]["golden_observation"] is False
    assert result["observed_card_facts"]["BG_TID_713_G"]["golden_observation"] is True
    assert result["observed_card_facts"]["BG_TID_713_G"]["normal_rules_text"]
    assert "attack" not in result["observed_card_facts"]["BG_TID_713"]


def test_catalog_reports_bounded_query_truncation_and_mechanic_ids(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(
        tmp_path / "missing.json.gz",
        _logger(),
        network_enabled=False,
    )
    snapshot = BattlegroundsSnapshot(
        shop=tuple(
            BattlegroundsCardSnapshot(card_id=f"BG_UNKNOWN_{index:02d}")
            for index in range(41)
        ),
        mechanics={
            "anomaly_dbf_id": 9001,
            "quest": {"reward_dbf_id": 9002},
            "trinket_dbf_ids": [9003],
        },
    )

    coverage = catalog.facts_for(snapshot)["coverage"]

    assert coverage["unique_observed_count"] == 44
    assert coverage["queried_count"] == 40
    assert len(coverage["missing_ids"]) == 40
    assert coverage["truncated_count"] == 4
    assert coverage["truncated_ids"] == ["BG_UNKNOWN_40", "9001", "9002", "9003"]


def test_missing_catalog_does_not_hide_zone_coverage(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(
        tmp_path / "missing.json.gz", _logger(), network_enabled=False
    )
    snapshot = BattlegroundsSnapshot(shop=(BattlegroundsCardSnapshot(card_id="BG_UNKNOWN"),))

    result = catalog.facts_for(snapshot)

    assert result["available"] is False
    assert result["coverage"]["missing_ids"] == ["BG_UNKNOWN"]


class _Response:
    def __init__(self, body: bytes, *, url: str, length: str | None = None) -> None:
        self.body = body
        self.url = url
        self.headers = {} if length is None else {"Content-Length": length}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def test_http_request_is_fixed_origin_get_without_game_data(monkeypatch, tmp_path: Path) -> None:
    observed: list[Any] = []

    class Opener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            observed.append((request, timeout))
            return _Response(b'{"data":[]}', url=request.full_url)

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: Opener())
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())

    result = catalog._request_json("/cards", {"pool": "current", "offset": 0})

    request, timeout = observed[0]
    assert result == {"data": []}
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url.startswith("https://hsbg.cards/api/v1/cards?")
    assert "BG_" not in request.full_url
    assert timeout == 6.0


def test_http_rejects_large_or_cross_origin_response(monkeypatch, tmp_path: Path) -> None:
    class LargeOpener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            return _Response(b"{}", url=request.full_url, length=str(3 * 1024 * 1024))

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: LargeOpener())
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())
    with pytest.raises(CatalogError, match="response_too_large"):
        catalog._request_json("/cards", {})

    class RedirectedOpener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            return _Response(b"{}", url="https://example.invalid/cards")

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: RedirectedOpener())
    with pytest.raises(CatalogError, match="response_origin_rejected"):
        catalog._request_json("/cards", {})


def test_http_normalizes_invalid_json_timeout_and_server_error(monkeypatch, tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())

    class InvalidJsonOpener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            return _Response(b"not-json", url=request.full_url)

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: InvalidJsonOpener())
    with pytest.raises(CatalogError, match="invalid_json"):
        catalog._request_json("/cards", {})

    class TimeoutOpener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            raise catalog_module.URLError(catalog_module.socket.timeout())

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: TimeoutOpener())
    with pytest.raises(CatalogError, match="network_timeout"):
        catalog._request_json("/cards", {})

    class ServerErrorOpener:
        def open(self, request, timeout: float):  # type: ignore[no-untyped-def]
            raise catalog_module.HTTPError(request.full_url, 500, "error", {}, None)

    monkeypatch.setattr(catalog_module, "build_opener", lambda *_args: ServerErrorOpener())
    with pytest.raises(CatalogError, match="http_500"):
        catalog._request_json("/cards", {})


def test_incomplete_catalog_pagination_is_rejected(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())
    catalog._request_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "data": [_raw_card(1, "BG_A", "A")],
        "pagination": {"total": 2, "nextOffset": None},
    }

    with pytest.raises(CatalogError, match="invalid_pagination"):
        catalog._fetch_current_cards()


def test_disabling_network_stops_before_the_next_catalog_page(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())
    calls = 0

    def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        catalog.configure(network_enabled=False, refresh_hours=24.0)
        return {
            "data": [_raw_card(1, "BG_A", "A")],
            "pagination": {"total": 2, "nextOffset": 1},
        }

    catalog._request_json = fake_request  # type: ignore[method-assign]

    with pytest.raises(CatalogError, match="network_disabled"):
        catalog._fetch_current_cards()
    assert calls == 1


@pytest.mark.parametrize(
    "rows",
    [
        [_raw_card(1, "BG_A", "A"), {"id": 2, "name": "missing external id"}],
        [_raw_card(1, "BG_A", "A"), _raw_card(1, "BG_B", "B")],
        [_raw_card(1, "BG_A", "A"), _raw_card(2, "BG_A", "B")],
    ],
)
def test_schema_drift_or_duplicate_catalog_rows_are_rejected(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> None:
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())
    catalog._request_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "data": rows,
        "pagination": {"total": 2, "nextOffset": None},
    }

    with pytest.raises(CatalogError, match="invalid_card_record|duplicate_card_record"):
        catalog._fetch_current_cards()


def test_background_shutdown_does_not_wait_for_blocked_fetch_or_commit_afterward(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cache = tmp_path / "cache.gz"
    catalog = BattlegroundsCardCatalog(cache, _logger())

    def request(path: str, _query: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if path == "/patches":
            entered.set()
            release.wait(6.0)
            finished.set()
            return {"data": [{"currentPatch": "36.2.2", "season": "Season 14"}]}
        return {
            "data": [_raw_card(1, "BG_LATE", "Late")],
            "pagination": {"total": 1, "nextOffset": None},
        }

    catalog._request_json = request  # type: ignore[method-assign]
    started_at = time.monotonic()
    assert catalog.start() is True
    assert time.monotonic() - started_at < 0.2
    assert entered.wait(1.0)

    stopped_at = time.monotonic()
    assert catalog.stop(timeout=0.3) is True
    assert time.monotonic() - stopped_at < 0.4
    assert cache.exists() is False
    assert catalog.status()["available"] is False

    release.set()
    assert finished.wait(1.0)
    time.sleep(0.1)

    assert cache.exists() is False
    assert catalog.status()["available"] is False


def test_background_shutdown_discards_cache_staged_after_stop(tmp_path: Path) -> None:
    stage_entered = threading.Event()
    stage_release = threading.Event()
    fetch_finished = threading.Event()
    cache = tmp_path / "cache.gz"
    catalog = BattlegroundsCardCatalog(cache, _logger())
    payload = _payload([_raw_card(1, "BG_LATE_CACHE", "Late Cache")])
    original_stage = catalog._stage_cache

    catalog._fetch_payload = lambda *_args: payload  # type: ignore[method-assign]

    def blocked_stage(value: dict[str, Any]) -> Path:
        stage_entered.set()
        stage_release.wait(6.0)
        staged = original_stage(value)
        fetch_finished.set()
        return staged

    catalog._stage_cache = blocked_stage  # type: ignore[method-assign]

    assert catalog.start() is True
    assert stage_entered.wait(1.0)

    stopped_at = time.monotonic()
    assert catalog.stop(timeout=0.3) is True
    assert time.monotonic() - stopped_at < 0.4
    assert cache.exists() is False
    assert catalog.status()["available"] is False

    stage_release.set()
    assert fetch_finished.wait(1.0)
    time.sleep(0.1)

    assert cache.exists() is False
    assert catalog.status()["available"] is False
    assert list(tmp_path.glob(".*.tmp")) == []


def test_start_racing_with_stop_reliably_starts_a_new_owner(tmp_path: Path) -> None:
    fetch_entered = threading.Event()
    fetch_release = threading.Event()
    stop_set_entered = threading.Event()
    stop_set_release = threading.Event()
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())
    payload = _payload([_raw_card(1, "BG_RESTARTED", "Restarted")])

    def blocked_fetch(*_args: Any) -> dict[str, Any]:
        fetch_entered.set()
        fetch_release.wait(6.0)
        return payload

    catalog._fetch_payload = blocked_fetch  # type: ignore[method-assign]
    assert catalog.start() is True
    assert fetch_entered.wait(1.0)

    old_stop_event = catalog._stop_event
    original_set = old_stop_event.set

    def blocked_set() -> None:
        stop_set_entered.set()
        stop_set_release.wait(1.0)
        original_set()

    old_stop_event.set = blocked_set  # type: ignore[method-assign]
    stop_results: list[bool] = []
    start_results: list[bool] = []
    stop_thread = threading.Thread(
        target=lambda: stop_results.append(catalog.stop(timeout=0.3))
    )
    start_thread = threading.Thread(target=lambda: start_results.append(catalog.start()))

    stop_thread.start()
    assert stop_set_entered.wait(1.0)
    start_thread.start()
    time.sleep(0.05)
    stop_set_release.set()
    stop_thread.join(1.0)
    start_thread.join(1.0)

    assert stop_results == [True]
    assert start_results == [True]
    assert catalog._thread is not None and catalog._thread.is_alive()

    catalog.configure(network_enabled=False, refresh_hours=24.0)
    fetch_release.set()
    assert catalog.stop(timeout=0.3) is True


def test_restarted_owner_cannot_be_overwritten_by_previous_generation_fetch(
    tmp_path: Path,
) -> None:
    first_entered = threading.Event()
    first_release = threading.Event()
    first_finished = threading.Event()
    calls_lock = threading.Lock()
    calls = 0
    cache = tmp_path / "cache.gz"
    catalog = BattlegroundsCardCatalog(cache, _logger())
    old_payload = _payload([_raw_card(1, "BG_OLD", "Old")], fetched_at=1000.0)
    new_payload = _payload([_raw_card(2, "BG_NEW", "New")], fetched_at=2000.0)

    def fetch(_checked_at: float, _stop_event: threading.Event) -> dict[str, Any]:
        nonlocal calls
        with calls_lock:
            calls += 1
            attempt_number = calls
        if attempt_number == 1:
            first_entered.set()
            first_release.wait(6.0)
            first_finished.set()
            return old_payload
        return new_payload

    catalog._fetch_payload = fetch  # type: ignore[method-assign]

    assert catalog.start() is True
    assert first_entered.wait(1.0)
    assert catalog.stop(timeout=0.3) is True
    assert catalog.start() is True
    assert catalog.wait_ready(1.0) is True
    assert catalog.facts_for(
        BattlegroundsSnapshot(shop=(BattlegroundsCardSnapshot(card_id="BG_NEW"),))
    )["coverage"]["resolved_count"] == 1

    first_release.set()
    assert first_finished.wait(1.0)
    time.sleep(0.1)

    assert catalog.facts_for(
        BattlegroundsSnapshot(shop=(BattlegroundsCardSnapshot(card_id="BG_NEW"),))
    )["coverage"]["resolved_count"] == 1
    assert catalog.facts_for(
        BattlegroundsSnapshot(shop=(BattlegroundsCardSnapshot(card_id="BG_OLD"),))
    )["coverage"]["resolved_count"] == 0
    with gzip.open(cache, "rt", encoding="utf-8") as handle:
        cached = json.load(handle)
    assert [card["external_id"] for card in cached["cards"]] == ["BG_NEW"]
    assert catalog.stop(timeout=0.3) is True


def test_disabling_network_discards_inflight_background_fetch(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cache = tmp_path / "cache.gz"
    catalog = BattlegroundsCardCatalog(cache, _logger())

    def request(path: str, _query: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if path == "/patches":
            entered.set()
            release.wait(6.0)
            finished.set()
            return {"data": [{"currentPatch": "36.2.2", "season": "Season 14"}]}
        return {
            "data": [_raw_card(1, "BG_DISABLED", "Disabled")],
            "pagination": {"total": 1, "nextOffset": None},
        }

    catalog._request_json = request  # type: ignore[method-assign]

    assert catalog.start() is True
    assert entered.wait(1.0)
    catalog.configure(network_enabled=False, refresh_hours=24.0)
    assert catalog.stop(timeout=0.3) is True

    release.set()
    assert finished.wait(1.0)
    time.sleep(0.1)

    assert cache.exists() is False
    assert catalog.status()["available"] is False
    assert catalog.status()["degraded_reason"] == "network_disabled"


def test_network_disabled_never_starts_worker(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(
        tmp_path / "cache.gz", _logger(), network_enabled=False
    )

    assert catalog.start() is False
    assert catalog.wait_ready(0.01) is False
    assert catalog.status()["degraded_reason"] == "network_disabled"
