import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_pulse_bot.calendar_engine import build_schedule
from market_pulse_bot.config import load_exchanges
from market_pulse_bot.i18n import I18n
from market_pulse_bot.market_engine import (
    IncidentRecord,
    Phase,
    PhaseState,
    UpcomingEvent,
    build_upcoming_events,
    compute_phase_state,
)
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
    holiday = UpcomingEvent("Bolsa de Madrid", "Europe", "🇪🇸", "holiday", dt.date(2026, 9, 24), None)
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


MADRID = ZoneInfo("Europe/Madrid")


def _live_line(mic: str, local: dt.datetime, loop_mode: bool = False, incident=None) -> str:
    exchange = next(item for item in load_exchanges(Path("config/exchanges.yaml")) if item.mic == mic)
    now = local.astimezone(dt.UTC)
    state = compute_phase_state(exchange, build_schedule(exchange), now, incident, loop_mode)
    return render_exchange_line(exchange, state, I18n(Path("locales"), "es"), MADRID)


def test_line_shows_next_change_in_the_readers_time_today() -> None:
    line = _live_line("XMAD", dt.datetime(2026, 9, 29, 15, 26, tzinfo=MADRID))
    assert line.endswith("— Sesión regular → 🔵 17:30")


def test_next_change_on_another_day_names_the_weekday() -> None:
    # Tokyo at 15:40 Madrid: closed until Wednesday's 08:00 JST opening auction = 01:00 Madrid.
    line = _live_line("XTKS", dt.datetime(2026, 9, 29, 15, 40, tzinfo=MADRID))
    assert line.endswith("— Cerrado → 🔵 mié 01:00")


def test_next_change_more_than_a_week_away_includes_the_date() -> None:
    # China's National Day week: Shanghai is closed 1-7 Oct 2026 and reopens on the 8th.
    line = _live_line("XSHG", dt.datetime(2026, 9, 30, 12, 0, tzinfo=MADRID))
    assert line.endswith("— Cerrado → 🔵 jue 08/10 03:15")


def test_countdown_shows_only_the_readers_time() -> None:
    line = _live_line("XNYS", dt.datetime(2026, 9, 29, 15, 26, tzinfo=MADRID), loop_mode=True)
    assert line.endswith("— Apertura en 4 minutos, a las 15:30")
    assert "ET" not in line and "(" not in line.split("—", 1)[1]


def test_end_of_day_counts_down_to_the_close() -> None:
    line = _live_line("XMAD", dt.datetime(2026, 9, 29, 17, 41, tzinfo=MADRID), loop_mode=True)
    assert line == "🟣🔜⚫️ 🇪🇸 Bolsa de Madrid (\x02EUR\x03) — Cierre en 4 minutos, a las 17:45"


def test_incident_without_reopening_time_has_no_next_change() -> None:
    now = dt.datetime(2026, 9, 29, 15, 26, tzinfo=MADRID)
    halt = IncidentRecord("XLON", Phase.TECHNICAL_HALT, now.astimezone(dt.UTC), "manual")
    line = _live_line("XLON", now, incident=halt)
    assert line.endswith("— Interrupción técnica/operativa")
    assert "→" not in line


def test_no_country_time_anywhere_in_the_messages() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    now = dt.datetime(2026, 11, 26, 16, 0, tzinfo=dt.UTC)
    schedules = {e.mic: build_schedule(e) for e in exchanges}
    states = {e.mic: compute_phase_state(e, schedules[e.mic], now, None, True) for e in exchanges}
    events = build_upcoming_events(exchanges, schedules, now)
    assert any(event.event_type == "early_close" for event in events)
    payloads = build_all_payloads(exchanges, states, events, now, MADRID, I18n(Path("locales"), "es"), "telegram")
    text = "\n".join(payload.plain_text for payload in payloads)
    for label in ("CET/CEST", " ET)", "JST", "BRT", "AEST", "FJT", "IST", "HKT", "SGT", "KST"):
        assert label not in text
