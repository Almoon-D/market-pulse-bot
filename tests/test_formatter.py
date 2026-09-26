import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from market_pulse_bot.config import load_exchanges
from market_pulse_bot.i18n import I18n
from market_pulse_bot.market_engine import Phase, PhaseState
from market_pulse_bot.text_formatter import build_all_payloads, render_exchange_line


def test_mic_never_rendered() -> None:
    exchange = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == "XNYS")
    now = dt.datetime(2026, 9, 22, 13, 35, tzinfo=dt.UTC)
    state = PhaseState(
        Phase.REGULAR, None, None, None, None, None, None, False, False, False,
        now.astimezone(ZoneInfo(exchange.timezone)),
    )
    line = render_exchange_line(exchange, state, I18n(Path("locales"), "es"), ZoneInfo("Europe/Madrid"))
    assert "XNYS" not in line
    assert "♦️" not in line


def test_technical_halt_uses_diamond() -> None:
    exchange = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == "XNYS")
    now = dt.datetime(2026, 9, 22, 13, 35, tzinfo=dt.UTC)
    state = PhaseState(
        Phase.TECHNICAL_HALT, None, None, None, None, None, None, False, False, False,
        now.astimezone(ZoneInfo(exchange.timezone)),
    )
    line = render_exchange_line(exchange, state, I18n(Path("locales"), "es"), ZoneInfo("Europe/Madrid"))
    assert "♦️" in line
    assert chr(0x1F7E5) not in line


def test_all_three_payloads_are_built() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))[:4]
    now = dt.datetime(2026, 9, 22, 13, 35, tzinfo=dt.UTC)
    states = {
        item.mic: PhaseState(
            Phase.REGULAR, None, None, None, None, None, None, False, False, False,
            now.astimezone(ZoneInfo(item.timezone)),
        )
        for item in exchanges
    }
    payloads = build_all_payloads(exchanges, states, [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "es"), "telegram")
    assert len(payloads) == 3
    assert all(payload.plain_text for payload in payloads)


def test_new_americas_exchanges_are_in_americas_dashboard() -> None:
    exchanges = [item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic in {"XNAS", "XMEX", "XSGO"}]
    now = dt.datetime(2026, 9, 22, 15, 0, tzinfo=dt.UTC)
    states = {
        item.mic: PhaseState(Phase.REGULAR, None, None, None, None, None, None, False, False, False, now.astimezone(ZoneInfo(item.timezone)))
        for item in exchanges
    }
    payload = build_all_payloads(exchanges, states, [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "en"), "telegram")[1]
    for name in ("Nasdaq Stock Market", "Mexican Stock Exchange", "Santiago Stock Exchange"):
        assert name in payload.plain_text
