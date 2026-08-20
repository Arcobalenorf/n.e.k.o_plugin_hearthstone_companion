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
) -> dict[str, Any]:
    return {
        "id": provider_id,
        "externalId": external_id,
        "name": name,
        "text": "<b>Battlecry:</b> Gain &lt;2&gt; Attack.<br>Then attack.",
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
    }


def _payload(cards: list[dict[str, Any]], *, fetched_at: float = 1000.0) -> dict[str, Any]:
    return {
        "schema_version": 1,
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
            ],
            fetched_at=1000.0,
        ),
    )
    catalog = BattlegroundsCardCatalog(cache, _logger(), network_enabled=False, now=lambda: 1001.0)
    snapshot = BattlegroundsSnapshot(
        hero_choices=(BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_CHOICE"),),
        shop=(BattlegroundsCardSnapshot(card_id="BG_TID_713", attack=9, health=8),),
        hand=(BattlegroundsCardSnapshot(card_id="BG_TID_713"),),
        warband=(BattlegroundsCardSnapshot(card_id="BG_TID_713_G", attack=20, health=20),),
        lobby=(BattlegroundsPlayerSnapshot(player_id=1, hero_card_id="BG_HERO"),),
    )

    result = catalog.facts_for(snapshot)

    assert result["coverage"]["zone_ids"] == {
        "hero_choices": ["BG_HERO_CHOICE"],
        "shop": ["BG_TID_713"],
        "hand": ["BG_TID_713"],
        "warband": ["BG_TID_713_G"],
        "heroes": ["BG_HERO"],
    }
    assert result["coverage"]["unique_observed_count"] == 4
    assert result["coverage"]["resolved_count"] == 4
    assert result["observed_card_facts"]["BG_HERO_CHOICE"]["card_type"] == "hero"
    assert result["observed_card_facts"]["BG_TID_713"]["golden_observation"] is False
    assert result["observed_card_facts"]["BG_TID_713_G"]["golden_observation"] is True
    assert result["observed_card_facts"]["BG_TID_713_G"]["normal_rules_text"]
    assert "attack" not in result["observed_card_facts"]["BG_TID_713"]


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


def test_background_refresh_is_non_blocking_and_shutdown_is_joined(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    catalog = BattlegroundsCardCatalog(tmp_path / "cache.gz", _logger())

    def refresh() -> bool:
        entered.set()
        release.wait(2.0)
        return False

    catalog.refresh_once = refresh  # type: ignore[method-assign]
    started_at = time.monotonic()
    assert catalog.start() is True
    assert time.monotonic() - started_at < 0.2
    assert entered.wait(1.0)
    release.set()
    assert catalog.stop(timeout=2.0) is True


def test_network_disabled_never_starts_worker(tmp_path: Path) -> None:
    catalog = BattlegroundsCardCatalog(
        tmp_path / "cache.gz", _logger(), network_enabled=False
    )

    assert catalog.start() is False
    assert catalog.wait_ready(0.01) is False
    assert catalog.status()["degraded_reason"] == "network_disabled"
