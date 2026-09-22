import asyncio
from pathlib import Path

from market_pulse_bot.config import load_exchanges
from market_pulse_bot.halt_detector import IncidentStore, parse_structured_market_wide, run_incident_check_once
from market_pulse_bot.market_engine import Phase


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
    async def get(self, url: str, timeout: int = 15):
        return FakeResponse(self.text)


def test_rss_keyword_never_changes_state(tmp_path: Path) -> None:
    source = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == "XNAS").model_copy(update={
        "incident_source": {"type": "rss_keyword", "url": "https://example.invalid/rss", "scope": "single_stock", "keywords": ["market closure"]}
    })
    store = IncidentStore()
    asyncio.run(run_incident_check_once([source], tmp_path / "missing.yaml", store, FakeClient("market closure")))
    assert asyncio.run(store.get("XNAS")) is None


def test_manual_override_wins(tmp_path: Path) -> None:
    source = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == "XNAS").model_copy(update={
        "incident_source": {"type": "structured_feed", "url": "https://example.invalid/feed", "scope": "market_wide"}
    })
    manual = tmp_path / "manual.yaml"
    manual.write_text("incidents:\n  - mic: XNAS\n    phase: technical_halt\n", encoding="utf-8")
    content = '{"items":[{"active":true,"phase":"regulatory_halt","note":"feed"}]}'
    store = IncidentStore()
    asyncio.run(run_incident_check_once([source], manual, store, FakeClient(content)))
    record = asyncio.run(store.get("XNAS"))
    assert record is not None
    assert record.source_type == "manual"
    assert record.phase == Phase.TECHNICAL_HALT


def test_structured_parser_requires_explicit_incident_phase() -> None:
    assert parse_structured_market_wide('{"items":[{"active":true,"phase":"single_stock"}]}') is None
    parsed = parse_structured_market_wide('{"items":[{"active":true,"phase":"technical_halt","reopening_time":"2026-09-22T18:00:00Z"}]}')
    assert parsed is not None
    assert parsed[0] == Phase.TECHNICAL_HALT
