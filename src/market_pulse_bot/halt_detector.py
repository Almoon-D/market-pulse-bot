"""Incident ingestion with strict market-wide safety boundaries."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import ExchangeConfig, IncidentSourceConfig
from .market_engine import INCIDENT_PHASES, IncidentRecord, Phase

logger = logging.getLogger(__name__)

DEFAULT_RSS_KEYWORDS = (
    "trading halt",
    "market-wide halt",
    "circuit breaker",
    "exchange closed",
    "trading suspended",
    "market closure",
)

SOURCE_PRIORITY = {"structured_feed": 50, "manual": 100}


class IncidentStore:
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
            existing = self._records.get(record.mic)
            if existing and SOURCE_PRIORITY.get(existing.source_type, 0) > SOURCE_PRIORITY.get(record.source_type, 0):
                return
            self._records[record.mic] = record

    async def clear_if_source(self, mic: str, source_type: str) -> None:
        async with self._lock:
            existing = self._records.get(mic)
            if existing and existing.source_type == source_type:
                self._records.pop(mic, None)


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_manual_incidents(path: Path) -> dict[str, IncidentRecord]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: dict[str, IncidentRecord] = {}
    now = dt.datetime.now(dt.timezone.utc)
    for entry in raw.get("incidents", []) or []:
        mic = str(entry["mic"]).upper()
        phase = Phase(str(entry["phase"]))
        if phase not in INCIDENT_PHASES and phase != Phase.POST_HALT_REOPENING:
            raise ValueError(f"manual incident {mic}: invalid phase {phase.value}")
        records[mic] = IncidentRecord(
            mic=mic,
            phase=phase,
            detected_at=now,
            source_type="manual",
            note=entry.get("note"),
            reopening_time=_parse_datetime(entry.get("reopening_time")),
        )
    return records


def _flatten_element(element: ET.Element) -> dict[str, str]:
    return {
        child.tag.split("}")[-1]: (child.text or "").strip()
        for child in list(element)
    }


def _structured_items(content: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(content)
        items = decoded.get("items", decoded.get("data", [])) if isinstance(decoded, dict) else decoded
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return items
    except json.JSONDecodeError:
        pass
    root = ET.fromstring(content)
    return [
        _flatten_element(item)
        for item in root.iter()
        if item.tag.split("}")[-1] == "item"
    ]


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "active", "open"}


def parse_structured_market_wide(
    content: str,
    field_map: dict[str, str] | None = None,
) -> tuple[Phase, dt.datetime | None, str | None] | None:
    mapping = {
        "active": "active",
        "phase": "phase",
        "reopening_time": "reopening_time",
        "note": "note",
    }
    mapping.update(field_map or {})
    items = _structured_items(content)
    for item in reversed(items):
        if not _truthy(item.get(mapping["active"])):
            continue
        try:
            phase = Phase(str(item[mapping["phase"]]))
        except (KeyError, ValueError):
            continue
        if phase not in INCIDENT_PHASES:
            continue
        return (
            phase,
            _parse_datetime(item.get(mapping["reopening_time"])),
            str(item.get(mapping["note"]) or "") or None,
        )
    return None


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, timeout=15.0)
    response.raise_for_status()
    return response.text


async def run_incident_check_once(
    exchanges: list[ExchangeConfig],
    manual_incidents_path: Path,
    store: IncidentStore,
    client: httpx.AsyncClient,
) -> None:
    manual = load_manual_incidents(manual_incidents_path)
    existing = await store.get_all()

    for record in manual.values():
        await store.set(record)

    for mic, record in existing.items():
        if record.source_type == "manual" and mic not in manual:
            await store.clear_if_source(mic, "manual")

    for exchange in exchanges:
        source: IncidentSourceConfig = exchange.incident_source
        current = await store.get(exchange.mic)
        if current and current.source_type == "manual":
            continue

        if source.type == "manual":
            continue

        if source.type == "structured_feed":
            if source.scope != "market_wide":
                logger.debug(
                    "structured feed for %s is single-stock scoped; never changes market state",
                    exchange.mic,
                )
                continue
            assert source.url is not None
            try:
                parsed = parse_structured_market_wide(
                    await _fetch_text(client, source.url),
                    source.field_map,
                )
            except (httpx.HTTPError, ET.ParseError, ValueError, json.JSONDecodeError):
                logger.warning("structured incident feed failed for %s", exchange.mic, exc_info=True)
                continue

            if parsed is None:
                await store.clear_if_source(exchange.mic, "structured_feed")
            else:
                phase, reopening_time, note = parsed
                await store.set(
                    IncidentRecord(
                        mic=exchange.mic,
                        phase=phase,
                        detected_at=dt.datetime.now(dt.timezone.utc),
                        source_type="structured_feed",
                        note=note,
                        reopening_time=reopening_time,
                    )
                )
            continue

        if source.type == "rss_keyword":
            assert source.url is not None
            try:
                content = await _fetch_text(client, source.url)
            except httpx.HTTPError:
                logger.warning("RSS candidate feed failed for %s", exchange.mic, exc_info=True)
                continue
            keywords = source.keywords or list(DEFAULT_RSS_KEYWORDS)
            hits = [keyword for keyword in keywords if keyword.lower() in content.lower()]
            if hits:
                logger.warning(
                    "unconfirmed incident candidate for %s: %s; rss_keyword never changes market state",
                    exchange.mic,
                    ", ".join(hits),
                )


async def run_halt_detector_loop(
    exchanges: list[ExchangeConfig],
    manual_incidents_path: Path,
    store: IncidentStore,
    interval_seconds: int,
    stop_event: asyncio.Event,
    client: httpx.AsyncClient,
) -> None:
    while not stop_event.is_set():
        try:
            await run_incident_check_once(exchanges, manual_incidents_path, store, client)
        except Exception:
            logger.exception("incident detector cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
