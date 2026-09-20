import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .config import ExchangeConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Incident:
    mic: str
    phase: str
    title: str
    detected_at: datetime
    source: str


class IncidentState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._incidents: dict[str, Incident] = {}

    async def replace(self, incident: Incident) -> None:
        async with self._lock:
            self._incidents[incident.mic] = incident

    async def clear_stale(self, now: datetime, max_age_seconds: int) -> None:
        async with self._lock:
            stale = [mic for mic, incident in self._incidents.items() if (now - incident.detected_at).total_seconds() > max_age_seconds]
            for mic in stale:
                self._incidents.pop(mic, None)

    async def snapshot(self) -> dict[str, Incident]:
        async with self._lock:
            return dict(self._incidents)


class HaltDetector:
    def __init__(self, exchanges: list[ExchangeConfig], state: IncidentState, interval: int) -> None:
        self.exchanges = exchanges
        self.state = state
        self.interval = interval
        self.client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def run_forever(self) -> None:
        try:
            while True:
                started = asyncio.get_running_loop().time()
                await self.poll_once()
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, self.interval - elapsed))
        finally:
            await self.close()

    async def poll_once(self) -> None:
        for exchange in self.exchanges:
            source = exchange.incident_source
            if source.type == "manual" or not source.url:
                continue
            try:
                incident = await self._poll_source(exchange)
                if incident is not None:
                    await self.state.replace(incident)
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                LOGGER.warning("incident source failed for %s: %s", exchange.mic, exc)
        await self.state.clear_stale(datetime.now().astimezone(), self.interval * 2)

    async def _poll_source(self, exchange: ExchangeConfig) -> Incident | None:
        response = await self.client.get(exchange.incident_source.url or "")
        response.raise_for_status()
        if exchange.incident_source.type == "rss_keyword":
            LOGGER.warning("unconfirmed candidate for %s: rss_keyword sources never set incidents autonomously", exchange.mic)
            return None
        if exchange.incident_source.scope != "market_wide":
            return None
        return _parse_structured_feed(exchange, response)


def _parse_structured_feed(exchange: ExchangeConfig, response: httpx.Response) -> Incident | None:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        data: Any = response.json()
        items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        for item in items:
            title = str(item.get("title", ""))
            scope = str(item.get("scope", ""))
            phase = str(item.get("phase", "technical"))
            if scope == "market_wide" and title:
                return Incident(exchange.mic, _incident_phase(phase), title, datetime.now().astimezone(), exchange.incident_source.type)
        return None

    root = ET.fromstring(response.text)
    texts: list[str] = []
    for item in root.iter():
        if item.tag.lower().endswith(("title", "description", "summary")) and item.text:
            texts.append(item.text.strip())
    market_wide_markers = ("market wide", "market-wide", "exchange closed", "trading halt", "circuit breaker", "all trading")
    for text in texts:
        if any(marker in text.lower() for marker in market_wide_markers):
            return Incident(exchange.mic, "technical", text, datetime.now().astimezone(), exchange.incident_source.type)
    return None


def _incident_phase(value: str) -> str:
    normalized = value.lower().replace(" ", "_")
    if normalized in {"regulatory", "regulatory_halt"}:
        return "regulatory"
    if normalized in {"exceptional", "closure", "market_wide_closure"}:
        return "exceptional"
    return "technical"
