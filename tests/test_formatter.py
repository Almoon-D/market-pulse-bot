import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_pulse_bot.config import load_exchanges
from market_pulse_bot.i18n import I18n
from market_pulse_bot.market_engine import Phase, PhaseState, UpcomingEvent
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


def _regular_states(exchanges, now):
    return {
        item.mic: PhaseState(
            Phase.REGULAR, None, None, None, None, None, None, False, False, False,
            now.astimezone(ZoneInfo(item.timezone)),
        )
        for item in exchanges
    }


def test_four_payloads_in_channel_order() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    now = dt.datetime(2026, 9, 22, 13, 35, tzinfo=dt.UTC)
    payloads = build_all_payloads(
        exchanges, _regular_states(exchanges, now), [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "es"), "discord"
    )
    assert [payload.kind for payload in payloads] == ["legend", "alerts", "dashboard_amer_eu", "dashboard_asia_oc"]
    assert all(payload.plain_text for payload in payloads)
    assert all(payload.discord_embed for payload in payloads)


def test_new_americas_are_visible_in_dashboard() -> None:
    exchanges = [e for e in load_exchanges(Path("config/exchanges.yaml")) if e.mic in {"XNAS", "XMEX", "XSGO"}]
    now = dt.datetime(2026, 9, 22, 15, tzinfo=dt.UTC)
    payload = build_all_payloads(exchanges, _regular_states(exchanges, now), [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "en"), "telegram")[2]
    for e in exchanges:
        assert e.name in payload.plain_text


def test_dashboard_follows_roster_order() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    now = dt.datetime(2026, 9, 22, 15, tzinfo=dt.UTC)
    payloads = build_all_payloads(
        exchanges, _regular_states(exchanges, now), [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "en"), "slack"
    )
    for payload, regions in ((payloads[2], ("America", "Europe")), (payloads[3], ("Asia", "Oceania"))):
        names = [e.name for e in exchanges if e.region in regions]
        positions = [payload.plain_text.index(f" {name} (") for name in names]
        assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("discord", "(**EUR**)"), ("slack", "(*EUR*)"), ("telegram", "(<b>EUR</b>)")],
)
def test_currency_is_bold_for_every_backend(backend: str, expected: str) -> None:
    exchanges = [e for e in load_exchanges(Path("config/exchanges.yaml")) if e.mic == "XMAD"]
    now = dt.datetime(2026, 9, 22, 10, tzinfo=dt.UTC)
    payload = build_all_payloads(
        exchanges, _regular_states(exchanges, now), [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "es"), backend
    )[2]
    assert expected in payload.plain_text
    assert "\x02" not in payload.plain_text and "\x03" not in payload.plain_text
    if backend == "discord":
        assert expected in payload.discord_embed["fields"][1]["value"]


def test_telegram_text_is_html_escaped() -> None:
    exchange = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == "XNYS")
    exchange = exchange.model_copy(update={"name": "S&P <Test>"})
    now = dt.datetime(2026, 9, 22, 15, tzinfo=dt.UTC)
    payload = build_all_payloads(
        [exchange], _regular_states([exchange], now), [], now, ZoneInfo("Europe/Madrid"), I18n(Path("locales"), "en"), "telegram"
    )[2]
    assert "S&amp;P &lt;Test&gt; (<b>USD</b>)" in payload.plain_text


def test_alerts_list_halted_exchanges_and_upcoming_holidays() -> None:
    exchanges = [e for e in load_exchanges(Path("config/exchanges.yaml")) if e.mic in {"XNYS", "XMAD"}]
    now = dt.datetime(2026, 9, 22, 15, tzinfo=dt.UTC)
    states = _regular_states(exchanges, now)
    states["XNYS"] = PhaseState(
        Phase.TECHNICAL_HALT, None, None, None, None, None, None, False, False, False,
        now.astimezone(ZoneInfo("America/New_York")),
    )
    holiday = UpcomingEvent("Bolsa de Madrid", "Europe", "🇪🇸", "CET/CEST", "holiday", dt.date(2026, 9, 24), None)
    i18n = I18n(Path("locales"), "en")
    alerts = build_all_payloads(exchanges, states, [holiday], now, ZoneInfo("Europe/Madrid"), i18n, "discord")[1]
    fields = alerts.discord_embed["fields"]
    assert [field["name"] for field in fields] == [i18n.t("alerts.incidents_section"), i18n.t("alerts.events_section")]
    assert "♦️" in fields[0]["value"] and "NYSE" in fields[0]["value"]
    assert "Bolsa de Madrid" not in fields[0]["value"]
    assert "Bolsa de Madrid — Official holiday" in fields[1]["value"]


def test_alerts_say_so_when_nothing_is_happening() -> None:
    exchanges = [e for e in load_exchanges(Path("config/exchanges.yaml")) if e.mic == "XNYS"]
    now = dt.datetime(2026, 9, 22, 15, tzinfo=dt.UTC)
    i18n = I18n(Path("locales"), "es")
    alerts = build_all_payloads(exchanges, _regular_states(exchanges, now), [], now, ZoneInfo("Europe/Madrid"), i18n, "telegram")[1]
    assert i18n.t("alerts.no_incidents") in alerts.plain_text
    assert i18n.t("events.no_events") in alerts.plain_text
