from __future__ import annotations

import gzip
import json
import os
import socket
import threading
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import BattlegroundsSnapshot

_PROVIDER = "hsbg.cards"
_ORIGIN = "https://hsbg.cards"
_API_ROOT = f"{_ORIGIN}/api/v1"
_ATTRIBUTION_URL = f"{_ORIGIN}/about"
_TERMS_URL = f"{_ORIGIN}/terms"
_CACHE_SCHEMA = 2
_MAX_CACHE_BYTES = 12 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CARDS = 5_000
_MAX_CHILD_RELATIONSHIPS = 32
_PAGE_SIZE = 100
_USER_AGENT = (
    "NEKO-Hearthstone-Companion/0.3.1 "
    "(+https://github.com/Arcobalenorf/n.e.k.o_plugin_hearthstone_companion)"
)


class CatalogError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _FetchAttempt:
    def __init__(self, checked_at: float) -> None:
        self.checked_at = checked_at
        self.done = threading.Event()
        self.prepared: dict[str, Any] | None = None
        self.staged_cache: Path | None = None
        self.cache_error = ""
        self.error_code = ""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _RulesTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li"}:
            self.parts.append(" ")


def clean_rules_text(value: Any, *, maximum: int = 600) -> str:
    parser = _RulesTextParser()
    try:
        parser.feed(str(value or "")[: maximum * 8])
        parser.close()
    except Exception:
        return ""
    return _plain_text("".join(parser.parts), maximum)


def _text(value: Any, maximum: int) -> str:
    return _plain_text(str(value or ""), maximum)


def _plain_text(value: str, maximum: int) -> str:
    without_controls = "".join(character if ord(character) >= 32 else " " for character in value)
    return " ".join(without_controls.split())[:maximum]


def _integer(value: Any, *, minimum: int = 0, maximum: int = 10_000_000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if minimum <= result <= maximum else 0


def _string_list(value: Any, *, maximum_items: int, maximum_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:maximum_items]:
        normalized = _text(item, maximum_length)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _relationship_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return _integer(value)


def _relationship_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) > _MAX_CHILD_RELATIONSHIPS:
        return []
    result: list[int] = []
    for item in value:
        identifier = _relationship_id(item)
        if not identifier or identifier in result:
            return []
        result.append(identifier)
    return result


def project_card(value: Mapping[str, Any]) -> dict[str, Any] | None:
    provider_id = _integer(value.get("id"))
    external_id = _text(value.get("externalId"), 100)
    name = _text(value.get("name"), 160)
    if not provider_id or not external_id or not name:
        return None
    card_type = _text(value.get("cardType"), 32).lower()
    provider_current_pool = bool(value.get("pool"))
    categories = _string_list(value.get("categories"), maximum_items=16, maximum_length=40)
    return {
        "provider_card_id": provider_id,
        "external_id": external_id,
        "name": name,
        "rules_text": clean_rules_text(value.get("text")),
        "golden_rules_text": clean_rules_text(value.get("textGold")),
        "card_type": card_type,
        "tavern_tier": _integer(value.get("tier"), maximum=7),
        "tribes": _string_list(value.get("minionTypes"), maximum_items=12, maximum_length=40),
        "mechanics": _string_list(value.get("keywords"), maximum_items=24, maximum_length=60),
        "categories": categories,
        "provider_current_pool": provider_current_pool,
        "direct_tavern_pool": (
            provider_current_pool
            and card_type in {"minion", "spell"}
            and any(category.lower() == "tavern" for category in categories)
        ),
        "duos_only": bool(value.get("isDuosOnly")),
        "solos_only": bool(value.get("isSolosOnly")),
        "golden_provider_card_id": _integer(value.get("dbfIdGold")),
        "child_provider_ids": _relationship_ids(value.get("childIds")),
        "parent_provider_id": _relationship_id(value.get("parentId")),
        "is_hero": value.get("isHero") is True,
        "is_hero_skin": value.get("isHeroSkin") is True,
    }


def _epoch_or_zero(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if 0.0 < result < 10_000_000_000.0 else 0.0


class BattlegroundsCardCatalog:
    """Cached public Battlegrounds facts; it never computes recommendations."""

    def __init__(
        self,
        cache_file: Path,
        logger: Any,
        *,
        network_enabled: bool = True,
        refresh_hours: float = 24.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_file = Path(cache_file)
        self.logger = logger
        self._network_enabled = bool(network_enabled)
        self._refresh_hours = max(6.0, min(168.0, float(refresh_hours)))
        self._now = now
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._force_refresh = False
        self._retry_delay_seconds = 0.0
        self._cards: tuple[dict[str, Any], ...] = ()
        self._by_identifier: dict[str, tuple[dict[str, Any], bool]] = {}
        self._hero_power_by_hero_provider_id: dict[int, dict[str, Any]] = {}
        self._patch = ""
        self._season = ""
        self._fetched_at = 0.0
        self._checked_at = 0.0
        self._last_error_code = ""
        self._load_cache()

    def configure(self, *, network_enabled: bool, refresh_hours: float) -> None:
        with self._lifecycle_lock:
            with self._lock:
                was_enabled = self._network_enabled
                self._network_enabled = bool(network_enabled)
                self._refresh_hours = max(6.0, min(168.0, float(refresh_hours)))
                enabled = self._network_enabled
                stop_event = self._stop_event
                wake_event = self._wake_event
                ready_event = self._ready_event
            if enabled and not was_enabled:
                self.start()
            else:
                wake_event.set()
            if not enabled:
                stop_event.set()
                ready_event.set()

    def start(self) -> bool:
        with self._lifecycle_lock:
            with self._lock:
                if (
                    self._thread is not None
                    and self._thread.is_alive()
                    and not self._stop_event.is_set()
                ):
                    return False
                if not self._network_enabled:
                    self._ready_event.set()
                    return False
                self._generation += 1
                generation = self._generation
                stop_event = threading.Event()
                wake_event = threading.Event()
                ready_event = threading.Event()
                self._stop_event = stop_event
                self._wake_event = wake_event
                self._ready_event = ready_event
                if self._cards:
                    ready_event.set()
                thread = threading.Thread(
                    target=self._run,
                    args=(generation, stop_event, wake_event, ready_event),
                    name="hearthstone-card-catalog",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
                return True

    def stop(self, timeout: float = 8.0) -> bool:
        with self._lifecycle_lock:
            with self._lock:
                stop_event = self._stop_event
                wake_event = self._wake_event
                thread = self._thread
                stop_event.set()
                wake_event.set()
        if thread is not None and thread.is_alive():
            thread.join(max(0.1, timeout))
        return thread is None or not thread.is_alive()

    def wait_ready(self, timeout: float = 1.5) -> bool:
        with self._lock:
            ready_event = self._ready_event
        ready_event.wait(max(0.0, timeout))
        with self._lock:
            return bool(self._cards)

    def request_refresh(self) -> None:
        with self._lock:
            self._force_refresh = True
            wake_event = self._wake_event
        wake_event.set()

    def _seconds_until_refresh(self) -> float:
        with self._lock:
            if self._force_refresh:
                return 0.0
            interval = (
                self._retry_delay_seconds
                if self._last_error_code and self._retry_delay_seconds > 0
                else self._refresh_hours * 3600.0
            )
            checked_at = self._checked_at
        return max(0.0, interval - (self._now() - checked_at))

    def _run(
        self,
        generation: int,
        stop_event: threading.Event,
        wake_event: threading.Event,
        ready_event: threading.Event,
    ) -> None:
        owner_thread = threading.current_thread()
        try:
            while not stop_event.is_set():
                with self._lock:
                    if generation != self._generation:
                        return
                    enabled = self._network_enabled
                if not enabled:
                    return
                elif self._seconds_until_refresh() <= 0.0:
                    with self._lock:
                        if generation != self._generation or stop_event.is_set():
                            return
                        self._force_refresh = False
                    attempt = self._start_fetch_attempt(
                        generation,
                        stop_event,
                        checked_at=self._now(),
                    )
                    while not attempt.done.wait(0.05):
                        if stop_event.is_set():
                            return
                        with self._lock:
                            if generation != self._generation or not self._network_enabled:
                                return
                    if not self._finish_fetch_attempt(generation, stop_event, attempt):
                        return
                    wait_seconds = min(max(0.1, self._seconds_until_refresh()), 3600.0)
                else:
                    wait_seconds = min(max(0.1, self._seconds_until_refresh()), 3600.0)
                ready_event.set()
                wake_event.wait(wait_seconds)
                wake_event.clear()
        finally:
            ready_event.set()
            with self._lifecycle_lock:
                if self._thread is owner_thread:
                    self._thread = None

    def _start_fetch_attempt(
        self,
        generation: int,
        stop_event: threading.Event,
        *,
        checked_at: float,
    ) -> _FetchAttempt:
        attempt = _FetchAttempt(checked_at)

        def fetch() -> None:
            try:
                payload = self._fetch_payload(checked_at, stop_event)
                attempt.prepared = self._prepare_install(payload)
                try:
                    attempt.staged_cache = self._stage_cache(payload)
                except OSError:
                    attempt.cache_error = "cache_write_failed"
            except CatalogError as exc:
                attempt.prepared = None
                attempt.error_code = exc.code
            except Exception as exc:
                attempt.prepared = None
                attempt.error_code = f"internal_{type(exc).__name__}"
                try:
                    self.logger.warning(
                        "Battlegrounds catalog fetch failed code=%s",
                        attempt.error_code,
                    )
                except Exception:
                    pass
            finally:
                with self._lock:
                    discard = (
                        generation != self._generation
                        or stop_event.is_set()
                        or not self._network_enabled
                    )
                if discard:
                    self._discard_staged_cache(attempt.staged_cache)
                    attempt.staged_cache = None
                    attempt.prepared = None
                attempt.done.set()

        threading.Thread(
            target=fetch,
            name=f"hearthstone-card-catalog-fetch-{generation}",
            daemon=True,
        ).start()
        return attempt

    def _finish_fetch_attempt(
        self,
        generation: int,
        stop_event: threading.Event,
        attempt: _FetchAttempt,
    ) -> bool:
        discard: Path | None = None
        committed = False
        with self._lock:
            if (
                generation != self._generation
                or stop_event.is_set()
                or not self._network_enabled
            ):
                discard = attempt.staged_cache
            elif attempt.prepared is None:
                self._record_refresh_failure_locked(
                    attempt.checked_at,
                    attempt.error_code or "empty_catalog",
                )
                return True
            else:
                cache_error = attempt.cache_error
                if attempt.staged_cache is not None:
                    try:
                        os.replace(attempt.staged_cache, self.cache_file)
                    except OSError:
                        cache_error = "cache_write_failed"
                    else:
                        attempt.staged_cache = None
                self._apply_install_locked(attempt.prepared, error_code=cache_error)
                self._retry_delay_seconds = 300.0 if cache_error else 0.0
                discard = attempt.staged_cache
                attempt.staged_cache = None
                committed = True
        self._discard_staged_cache(discard)
        return committed

    def refresh_once(self) -> bool:
        with self._lock:
            if not self._network_enabled:
                self._last_error_code = "network_disabled"
                self._checked_at = self._now()
                self._ready_event.set()
                return False
            generation = self._generation
            stop_event = self._stop_event
            ready_event = self._ready_event
        checked_at = self._now()
        staged_cache: Path | None = None
        try:
            payload = self._fetch_payload(checked_at, stop_event)
            prepared = self._prepare_install(payload)
            cache_error = ""
            try:
                staged_cache = self._stage_cache(payload)
            except OSError:
                cache_error = "cache_write_failed"
            with self._lock:
                if generation != self._generation or stop_event.is_set():
                    raise CatalogError("shutdown_requested")
                if not self._network_enabled:
                    raise CatalogError("network_disabled")
                if staged_cache is not None:
                    try:
                        os.replace(staged_cache, self.cache_file)
                    except OSError:
                        cache_error = "cache_write_failed"
                    else:
                        staged_cache = None
                self._apply_install_locked(prepared, error_code=cache_error)
                self._retry_delay_seconds = 300.0 if cache_error else 0.0
            return True
        except CatalogError as exc:
            with self._lock:
                if generation == self._generation:
                    self._record_refresh_failure_locked(checked_at, exc.code)
            return False
        finally:
            self._discard_staged_cache(staged_cache)
            ready_event.set()

    def _fetch_payload(
        self,
        checked_at: float,
        stop_event: threading.Event,
    ) -> dict[str, Any]:
        self._ensure_fetch_allowed(stop_event)
        patch, season = self._fetch_patch()
        self._ensure_fetch_allowed(stop_event)
        cards = self._fetch_current_cards(stop_event=stop_event)
        if not cards:
            raise CatalogError("empty_catalog")
        return {
            "schema_version": _CACHE_SCHEMA,
            "provider": _PROVIDER,
            "patch": patch,
            "season": season,
            "fetched_at": checked_at,
            "checked_at": checked_at,
            "cards": cards,
        }

    def _ensure_fetch_allowed(self, stop_event: threading.Event) -> None:
        with self._lock:
            if not self._network_enabled:
                raise CatalogError("network_disabled")
        if stop_event.is_set():
            raise CatalogError("shutdown_requested")

    def _record_refresh_failure_locked(self, checked_at: float, error_code: str) -> None:
        self._checked_at = checked_at
        self._last_error_code = error_code
        self._retry_delay_seconds = (
            300.0
            if self._retry_delay_seconds <= 0.0
            else min(3600.0, self._retry_delay_seconds * 3.0)
        )

    def _fetch_patch(self) -> tuple[str, str]:
        payload = self._request_json("/patches", {}, maximum_bytes=512 * 1024)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise CatalogError("invalid_patch_schema")
        patch = _text(rows[0].get("currentPatch"), 32)
        season = _text(rows[0].get("season"), 80)
        if not patch:
            raise CatalogError("invalid_patch_schema")
        return patch, season

    def _fetch_current_cards(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if stop_event is None:
            with self._lock:
                stop_event = self._stop_event
        offset = 0
        projected: list[dict[str, Any]] = []
        provider_ids: set[int] = set()
        external_ids: set[str] = set()
        total = 1
        while offset < total:
            self._ensure_fetch_allowed(stop_event)
            payload = self._request_json(
                "/cards",
                {"pool": "current", "limit": _PAGE_SIZE, "offset": offset, "sort": "id"},
            )
            rows = payload.get("data") if isinstance(payload, dict) else None
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not isinstance(pagination, dict):
                raise CatalogError("invalid_cards_schema")
            total = _integer(pagination.get("total"), maximum=_MAX_CARDS)
            if total <= 0 or total > _MAX_CARDS:
                raise CatalogError("catalog_size_invalid")
            for row in rows:
                if not isinstance(row, dict):
                    raise CatalogError("invalid_card_record")
                card = project_card(row)
                if card is None:
                    raise CatalogError("invalid_card_record")
                provider_id = int(card["provider_card_id"])
                external_id = str(card["external_id"])
                if provider_id in provider_ids or external_id in external_ids:
                    raise CatalogError("duplicate_card_record")
                provider_ids.add(provider_id)
                external_ids.add(external_id)
                projected.append(card)
            next_offset = pagination.get("nextOffset")
            if next_offset is None:
                if offset + len(rows) < total:
                    raise CatalogError("invalid_pagination")
                break
            parsed_next = _integer(next_offset, maximum=_MAX_CARDS)
            if parsed_next <= offset:
                raise CatalogError("invalid_pagination")
            offset = parsed_next
        if len(projected) > _MAX_CARDS:
            raise CatalogError("catalog_size_invalid")
        if len(projected) != total:
            raise CatalogError("incomplete_catalog")
        projected.sort(key=lambda item: (int(item["provider_card_id"]), str(item["external_id"])))
        return projected

    def _request_json(
        self,
        path: str,
        query: Mapping[str, Any],
        *,
        maximum_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> Any:
        with self._lock:
            if not self._network_enabled:
                raise CatalogError("network_disabled")
        if path not in {"/patches", "/cards"}:
            raise CatalogError("request_path_rejected")
        url = f"{_API_ROOT}{path}"
        if query:
            url = f"{url}?{urlencode({key: str(value) for key, value in query.items()})}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "hsbg.cards":
            raise CatalogError("request_origin_rejected")
        request = Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        opener = build_opener(_NoRedirects())
        try:
            with opener.open(request, timeout=6.0) as response:
                final = urlsplit(str(response.geturl()))
                if final.scheme != "https" or final.hostname != "hsbg.cards":
                    raise CatalogError("response_origin_rejected")
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        content_length = int(length)
                    except (TypeError, ValueError):
                        content_length = 0
                    if content_length > maximum_bytes:
                        raise CatalogError("response_too_large")
                raw = response.read(maximum_bytes + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise CatalogError("redirect_rejected") from None
            raise CatalogError(f"http_{exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise CatalogError("network_timeout") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise CatalogError("network_timeout") from None
            raise CatalogError("network_error") from None
        except OSError:
            raise CatalogError("network_error") from None
        if len(raw) > maximum_bytes:
            raise CatalogError("response_too_large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise CatalogError("invalid_json") from None

    def _stage_cache(self, payload: Mapping[str, Any]) -> Path:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_name(
            f".{self.cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            return temporary
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _discard_staged_cache(temporary: Path | None) -> None:
        if temporary is None:
            return
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_cache(self) -> None:
        try:
            with gzip.open(self.cache_file, "rb") as handle:
                raw = handle.read(_MAX_CACHE_BYTES + 1)
            if len(raw) > _MAX_CACHE_BYTES:
                raise CatalogError("cache_too_large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise CatalogError("cache_schema_invalid")
            self._install(payload)
        except FileNotFoundError:
            return
        except (
            CatalogError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            EOFError,
        ):
            self._last_error_code = "cache_invalid"

    def _prepare_install(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if _integer(payload.get("schema_version"), maximum=100) != _CACHE_SCHEMA:
            raise CatalogError("cache_schema_invalid")
        if payload.get("provider") != _PROVIDER:
            raise CatalogError("cache_provider_invalid")
        rows = payload.get("cards")
        if not isinstance(rows, list) or not 0 < len(rows) <= _MAX_CARDS:
            raise CatalogError("cache_cards_invalid")
        cards: list[dict[str, Any]] = []
        identifiers: dict[str, tuple[dict[str, Any], bool]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise CatalogError("cache_cards_invalid")
            card = project_card(
                {
                    "id": row.get("provider_card_id"),
                    "externalId": row.get("external_id"),
                    "name": row.get("name"),
                    "text": row.get("rules_text"),
                    "textGold": row.get("golden_rules_text"),
                    "cardType": row.get("card_type"),
                    "tier": row.get("tavern_tier"),
                    "minionTypes": row.get("tribes"),
                    "keywords": row.get("mechanics"),
                    "categories": row.get("categories"),
                    "pool": row.get("provider_current_pool"),
                    "isDuosOnly": row.get("duos_only"),
                    "isSolosOnly": row.get("solos_only"),
                    "dbfIdGold": row.get("golden_provider_card_id"),
                    "childIds": row.get("child_provider_ids"),
                    "parentId": row.get("parent_provider_id"),
                    "isHero": row.get("is_hero"),
                    "isHeroSkin": row.get("is_hero_skin"),
                }
            )
            if card is None:
                raise CatalogError("cache_cards_invalid")
            cards.append(card)
            identifiers[str(card["provider_card_id"])] = (card, False)
            identifiers[str(card["external_id"])] = (card, False)
            if not str(card["external_id"]).endswith("_G"):
                identifiers[f"{card['external_id']}_G"] = (card, True)
            golden_id = int(card["golden_provider_card_id"])
            if golden_id:
                identifiers[str(golden_id)] = (card, True)
        provider_cards = {int(card["provider_card_id"]): card for card in cards}
        hero_powers = _build_hero_power_index(cards, provider_cards)
        fetched_at = _epoch_or_zero(payload.get("fetched_at"))
        checked_at = _epoch_or_zero(payload.get("checked_at")) or fetched_at
        return {
            "cards": tuple(cards),
            "identifiers": identifiers,
            "hero_powers": hero_powers,
            "patch": _text(payload.get("patch"), 32),
            "season": _text(payload.get("season"), 80),
            "fetched_at": fetched_at,
            "checked_at": checked_at,
        }

    def _apply_install_locked(
        self,
        prepared: Mapping[str, Any],
        *,
        error_code: str = "",
    ) -> None:
        self._cards = prepared["cards"]
        self._by_identifier = prepared["identifiers"]
        self._hero_power_by_hero_provider_id = prepared["hero_powers"]
        self._patch = str(prepared["patch"])
        self._season = str(prepared["season"])
        self._fetched_at = float(prepared["fetched_at"])
        self._checked_at = float(prepared["checked_at"])
        self._last_error_code = error_code

    def _install(self, payload: Mapping[str, Any], *, error_code: str = "") -> None:
        prepared = self._prepare_install(payload)
        with self._lock:
            self._apply_install_locked(prepared, error_code=error_code)

    def _pool_summary(self, cards: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        types = Counter()
        tribes = Counter()
        tiers = Counter()
        for card in cards:
            if not card["provider_current_pool"]:
                continue
            card_type = str(card["card_type"] or "unknown")
            types[card_type] += 1
            if card["direct_tavern_pool"]:
                tier = int(card["tavern_tier"])
                if tier:
                    tiers[str(tier)] += 1
                if card_type == "minion":
                    for tribe in card["tribes"]:
                        tribes[str(tribe)] += 1
        return {
            "provider_current_catalog_count": len(cards),
            "provider_pool_counts_by_type": dict(sorted(types.items())),
            "direct_tavern_counts_by_tier": dict(sorted(tiers.items())),
            "direct_tavern_minion_counts_by_tribe": dict(sorted(tribes.items())),
            "is_lobby_specific_tribe_availability": False,
            "is_performance_or_win_rate_data": False,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            cards = self._cards
            fetched_at = self._fetched_at
            checked_at = self._checked_at
            refresh_hours = self._refresh_hours
            error_code = self._last_error_code
            patch = self._patch
            season = self._season
            network_enabled = self._network_enabled
            retry_delay_seconds = self._retry_delay_seconds
        stale = not fetched_at or self._now() - fetched_at > refresh_hours * 3600.0
        if not cards:
            degraded_reason = error_code or ("network_disabled" if not network_enabled else "loading")
        elif stale:
            degraded_reason = error_code or "refresh_overdue"
        else:
            degraded_reason = error_code
        return {
            "available": bool(cards),
            "dataset": {
                "provider": _PROVIDER,
                "patch": patch,
                "season": season,
                "fetched_at": fetched_at or None,
                "checked_at": checked_at or None,
                "stale": stale,
                "source_url": f"{_API_ROOT}/cards?pool=current",
                "attribution_url": _ATTRIBUTION_URL,
                "terms_url": _TERMS_URL,
                "network_enabled": network_enabled,
            },
            "card_count": len(cards),
            "active_pool_summary": self._pool_summary(cards),
            "degraded_reason": degraded_reason,
            "retry_delay_seconds": retry_delay_seconds or None,
            "next_refresh_in_seconds": round(self._seconds_until_refresh(), 3),
        }

    def facts_for(self, battlegrounds: BattlegroundsSnapshot | None) -> dict[str, Any]:
        status = self.status()
        if battlegrounds is None:
            return {
                **status,
                "coverage": {
                    "zone_ids": {},
                    "unique_observed_count": 0,
                    "queried_count": 0,
                    "resolved_count": 0,
                    "missing_ids": [],
                    "truncated_count": 0,
                    "truncated_ids": [],
                },
                "observed_card_facts": {},
            }
        mechanics = battlegrounds.mechanics
        quest = mechanics.get("quest") if isinstance(mechanics.get("quest"), Mapping) else {}
        zone_ids: dict[str, list[str]] = {
            "current_choice": [
                card.card_id
                for card in (
                    battlegrounds.current_choice.options
                    if battlegrounds.current_choice is not None
                    else ()
                )
                if card.card_id
            ],
            "hero_choices": [
                hero.card_id for hero in battlegrounds.hero_choices if hero.card_id
            ],
            "shop": [card.card_id for card in battlegrounds.shop if card.card_id],
            "hand": [card.card_id for card in battlegrounds.hand if card.card_id],
            "warband": [card.card_id for card in battlegrounds.warband if card.card_id],
            "mechanics": [
                str(value)
                for value in (
                    mechanics.get("anomaly_dbf_id"),
                    quest.get("reward_dbf_id") if isinstance(quest, Mapping) else None,
                    *list(mechanics.get("trinket_dbf_ids") or []),
                )
                if _integer(value) > 0
            ],
            "heroes": [player.hero_card_id for player in battlegrounds.lobby if player.hero_card_id],
            "observed_opponent_boards": [
                card.card_id
                for player in battlegrounds.lobby
                if not player.is_local and player.last_seen_round > 0
                for card in player.board_minions
                if card.card_id
            ],
        }
        ordered_ids: list[str] = []
        for values in zone_ids.values():
            for identifier in values:
                normalized = _text(identifier, 100)
                if normalized and normalized not in ordered_ids:
                    ordered_ids.append(normalized)
        with self._lock:
            index = dict(self._by_identifier)
            hero_power_index = dict(self._hero_power_by_hero_provider_id)
        facts: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        queried_ids = ordered_ids[:40]
        truncated_ids = ordered_ids[40:]
        for identifier in queried_ids:
            match = index.get(identifier)
            if match is None:
                missing.append(identifier)
                continue
            card, golden = match
            fact = {
                "catalog_ref": f"{_PROVIDER}:{card['provider_card_id']}",
                "name": card["name"],
                "rules_text": card["golden_rules_text"] if golden else card["rules_text"],
                "normal_rules_text": card["rules_text"] if golden else "",
                "card_type": card["card_type"],
                "tavern_tier": card["tavern_tier"],
                "tribes": list(card["tribes"]),
                "mechanics": list(card["mechanics"]),
                "categories": list(card["categories"]),
                "provider_current_pool": card["provider_current_pool"],
                "direct_tavern_pool": card["direct_tavern_pool"],
                "duos_only": card["duos_only"],
                "solos_only": card["solos_only"],
                "is_hero": card["is_hero"],
                "is_hero_skin": card["is_hero_skin"],
                "golden_observation": golden,
                "untrusted_reference_text": True,
            }
            hero_power = hero_power_index.get(int(card["provider_card_id"]))
            if hero_power is not None:
                fact["hero_power"] = {
                    "catalog_ref": f"{_PROVIDER}:{hero_power['provider_card_id']}",
                    "card_id": hero_power["external_id"],
                    "name": hero_power["name"],
                    "rules_text": hero_power["rules_text"],
                    "untrusted_reference_text": True,
                }
            facts[identifier] = fact
        return {
            **status,
            "coverage": {
                "zone_ids": zone_ids,
                "unique_observed_count": len(ordered_ids),
                "queried_count": len(queried_ids),
                "resolved_count": len(facts),
                "missing_ids": missing,
                "truncated_count": len(truncated_ids),
                "truncated_ids": truncated_ids[:20],
            },
            "observed_card_facts": facts,
        }


def _build_hero_power_index(
    cards: list[dict[str, Any]],
    provider_cards: Mapping[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for observed_hero in cards:
        if (
            observed_hero["card_type"] != "hero"
            or not observed_hero["is_hero"]
            or observed_hero["is_hero_skin"]
            or observed_hero["parent_provider_id"]
        ):
            continue
        base_id = int(observed_hero["provider_card_id"])
        candidates: list[dict[str, Any]] = []
        for child_id in observed_hero["child_provider_ids"]:
            child = provider_cards.get(int(child_id))
            if (
                child is not None
                and child["card_type"] == "hero_power"
                and not child["is_hero"]
                and not child["is_hero_skin"]
                and int(child["parent_provider_id"]) == base_id
                and child["rules_text"]
            ):
                candidates.append(child)
        if len(candidates) == 1:
            result[base_id] = candidates[0]
    return result


__all__ = ["BattlegroundsCardCatalog", "CatalogError", "clean_rules_text", "project_card"]
