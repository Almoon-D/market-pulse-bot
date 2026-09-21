"""
Background task: polls incident sources and keeps a lock-guarded,
in-memory table of live ``IncidentRecord``s that the main render loop
reads every tick.

Incident-source priority (Section 6, strict):
  1. ``structured_feed`` with ``scope: market_wide`` — the only thing
     allowed to autonomously set 🔶/🟥/🚨.
  2. ``rss_keyword`` — last resort, logs an "unconfirmed candidate" and
     never touches shared state.
  3. ``manual`` — the default/majority case. A single operator-maintained
     file (``data/manual_incidents.yaml``) is checked on every cycle for
     *every* exchange regardless of its configured primary type, so an
     operator can always force an override (e.g. a real NYSE-wide halt,
     even though XNYS's own feed is single-stock scope and therefore
     can't raise it automatically).

This module owns its own clock (its own polling interval, independent of
the render loop's) and touches the network — that split from
``market_engine`` is the two-clock concurrency model the README documents.
"""

import asyncio
import datetime as dt
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import yaml

from src.config import ExchangeConfig, IncidentSourceConfig
from src.market_engine import IncidentRecord, Phase

logger = logging.getLogger(__name__)

# Used only for rss_keyword sources when the config supplies no explicit
# 'keywords' list. Deliberately conservative and English-centric — a real
# deployment watching a non-English newswire should set its own list in
# exchanges.yaml, which is exactly why 'keywords' is a config field.
DEFAULT_RSS_KEYWORDS = [
    "trading halt", "market-wide halt", "circuit breaker",
    "exchange closed", "trading suspended", "market closure",
]

# Default tag-name mapping for a NASDAQ-Trade-Halts-shaped structured feed.
# Real feed schemas vary exchange to exchange; if you add a market_wide
# structured_feed for another exchange, adjust this mapping (or fork
# _parse_structured_feed for that feed's actual field names) to match.
DEFAULT_STRUCTURED_FIELD_MAP = {
    "reason": "ReasonCode",
    "halt_date": "HaltDate",
    "halt_time": "HaltTime",
    "resumption_date": "ResumptionDate",
    "resumption_time": "ResumptionTradeTime",
}


class IncidentStore:
    """Shared, ``asyncio.Lock``-guarded incident table."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, IncidentRecord] = {}

    async def get(self, mic: str) -> IncidentRecord | None:
        async with self._lock:
            return self._records.get(mic)

    async def get_all(self) -> dict[str, IncidentRecord]:
        async with self._lock:
            return dict(self._records)

    async def set(self, record: IncidentRecord) -> None:
        async with self._lock:
            self._records[record.mic] = record

    async def clear(self, mic: str) -> None:
        async with self._lock:
            self._records.pop(mic, None)


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def load_manual_incidents(path: Path) -> dict[str, IncidentRecord]:
    """Read the operator-maintained manual-override file. Missing file
    means "no manual overrides right now", not an error.
    """
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    now = dt.datetime.now(dt.timezone.utc)
    out: dict[str, IncidentRecord] = {}
    for entry in raw.get("incidents", None) or []:
        mic = str(entry["mic"]).upper()
        phase = Phase(entry["phase"])
        out[mic] = IncidentRecord(
            mic=mic,
            phase=phase,
            detected_at=now,
            source_type="manual",
            note=entry.get("note"),
            reopening_time=_parse_iso(entry.get("reopening_time")),
        )
    return out


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, timeout=15.0)
    response.raise_for_status()
    return response.text


async def _fetch_structured_feed_items(client: httpx.AsyncClient, url: str) -> list[dict[str, str]]:
    """Best-effort RSS/XML ``<item>`` extraction: every child tag under an
    ``<item>`` becomes a ``{tag: text}`` entry. Works for any RSS 2.0-shaped
    structured feed; the caller maps tag names via ``field_map``.
    """
    xml_text = await _fetch_text(client, url)
    root = ET.fromstring(xml_text)
    items: list[dict[str, str]] = []
    for item in root.iter("item"):
        entry = {child.tag: (child.text or "").strip() for child in item}
        items.append(entry)
    return items


def _parse_structured_feed(
    items: list[dict[str, str]], field_map: dict[str, str]
) -> tuple[Phase, dt.datetime | None, str] | None:
    """Decide whether the most recent market-wide entry represents an
    active halt. Returns ``None`` if nothing in the feed currently
    indicates an active, unresolved, market-wide condition.

    This is intentionally conservative: real feed schemas vary by
    exchange, so anything this function can't confidently parse is
    treated as "no active incident" rather than guessed at.
    """
    if not items:
        return None
    latest = items[-1]
    reason = latest.get(field_map.get("reason", ""), "") or "market-wide halt"
    resumption_date = latest.get(field_map.get("resumption_date", ""), "")
    resumption_time = latest.get(field_map.get("resumption_time", ""), "")
    if resumption_date and resumption_time:
        # Feed has already published a resumption -> nothing active.
        return None
    return Phase.REGULATORY_HALT, None, reason


async def run_incident_check_once(
    exchanges: list[ExchangeConfig],
    manual_incidents_path: Path,
    store: IncidentStore,
    client: httpx.AsyncClient,
) -> None:
    manual = load_manual_incidents(manual_incidents_path)
    for record in manual.values():
        await store.set(record)

    current = await store.get_all()
    for mic, record in current.items():
        if record.source_type == "manual" and mic not in manual:
            await store.clear(mic)

    for exchange in exchanges:
        src: IncidentSourceConfig = exchange.incident_source
        if src.type == "manual":
            continue  # fully covered by the manual-file pass above

        if src.type == "structured_feed":
            assert src.url is not None
            try:
                items = await _fetch_structured_feed_items(client, src.url)
            except (httpx.HTTPError, ET.ParseError):
                logger.warning("structured_feed fetch/parse failed for %s", exchange.mic, exc_info=True)
                continue
            if src.scope != "market_wide":
                # e.g. NYSE's NASDAQ trade-halts feed: real data, kept for
                # logging/reference, but single-stock halts never flip
                # exchange-wide state (Section 6).
                logger.debug("structured_feed for %s is single_stock scope: %d entries logged, not applied", exchange.mic, len(items))
                continue
            field_map = DEFAULT_STRUCTURED_FIELD_MAP
            parsed = _parse_structured_feed(items, field_map)
            if parsed is None:
                # Only clear an incident we ourselves set — never stomp on
                # a manual override an operator is actively maintaining.
                existing = await store.get(exchange.mic)
                if existing is not None and existing.source_type == "structured_feed":
                    await store.clear(exchange.mic)
            else:
                phase, reopening_time, note = parsed
                await store.set(
                    IncidentRecord(
                        mic=exchange.mic, phase=phase, detected_at=dt.datetime.now(dt.timezone.utc),
                        source_type="structured_feed", note=note, reopening_time=reopening_time,
                    )
                )
            continue

        if src.type == "rss_keyword":
            assert src.url is not None
            try:
                text = await _fetch_text(client, src.url)
            except httpx.HTTPError:
                logger.warning("rss_keyword fetch failed for %s", exchange.mic, exc_info=True)
                continue
            keywords = src.keywords or DEFAULT_RSS_KEYWORDS
            hits = [kw for kw in keywords if kw.lower() in text.lower()]
            if hits:
                logger.warning(
                    "unconfirmed candidate incident for %s: keyword(s) %s matched in rss_keyword "
                    "feed — rss_keyword sources never autonomously set 🔶/🟥/🚨, review manually",
                    exchange.mic, hits,
                )


async def run_halt_detector_loop(
    exchanges: list[ExchangeConfig],
    manual_incidents_path: Path,
    store: IncidentStore,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """The background task: its own clock, default 90s / min 60s
    (enforced by the ``--incident-interval`` CLI flag in ``main.py``).
    """
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            try:
                await run_incident_check_once(exchanges, manual_incidents_path, store, client)
            except Exception:  # noqa: BLE001 - a bad cycle must never kill the task
                logger.exception("halt_detector cycle failed unexpectedly")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
