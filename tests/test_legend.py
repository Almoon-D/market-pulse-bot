"""Tests for the legend, the first of the four managed messages."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_pulse_bot.config import load_exchanges
from market_pulse_bot.i18n import I18n
from market_pulse_bot.market_engine import Phase, PhaseState
from market_pulse_bot.text_formatter import build_all_payloads, build_legend_payload

LOCALES_DIR = Path("locales")


@pytest.mark.parametrize("lang", ["en", "es", "de", "fr"])
@pytest.mark.parametrize("backend", ["discord", "slack", "telegram"])
def test_legend_renders_for_every_language_and_backend(lang: str, backend: str) -> None:
    i18n = I18n(LOCALES_DIR, lang)
    payload = build_legend_payload(i18n, backend)

    assert payload.kind == "legend"
    assert payload.plain_text
    if backend == "discord":
        assert payload.discord_embed is not None
    if backend == "slack":
        assert payload.slack_blocks


def test_legend_lists_every_phase_exactly_once() -> None:
    i18n = I18n(LOCALES_DIR, "en")
    payload = build_legend_payload(i18n, "telegram")

    for emoji in ("⚫️", "🟣", "🔵", "🟢", "🔘", "⚪️", "🔶", "♦️", "🚨", "🔷", "🌗"):
        assert payload.plain_text.count(emoji) == 1, f"{emoji} should appear exactly once"


def test_legend_never_shows_the_retired_red_square() -> None:
    i18n = I18n(LOCALES_DIR, "en")

    for backend in ("discord", "slack", "telegram"):
        assert chr(0x1F7E5) not in build_legend_payload(i18n, backend).plain_text


def test_legend_is_the_first_message_and_does_not_change_over_time() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    i18n = I18n(LOCALES_DIR, "es")
    legends = []
    for now in (dt.datetime(2026, 9, 22, 8, tzinfo=dt.UTC), dt.datetime(2026, 9, 23, 20, tzinfo=dt.UTC)):
        states = {
            item.mic: PhaseState(
                Phase.REGULAR, None, None, None, None, None, None, False, False, False,
                now.astimezone(ZoneInfo(item.timezone)),
            )
            for item in exchanges
        }
        legends.append(build_all_payloads(exchanges, states, [], now, ZoneInfo("Europe/Madrid"), i18n, "discord")[0])
    assert legends[0].kind == "legend"
    assert legends[0] == legends[1] == build_legend_payload(i18n, "discord")
