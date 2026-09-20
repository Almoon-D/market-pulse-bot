"""Tests for line rendering."""

from src.config import load_settings
from src.render import render_line
from src.state import ExchangeState, Phase


def test_state_line_es():
    settings = load_settings("config/config.yaml")
    cfg = settings.exchanges[0]
    state = ExchangeState(cfg)
    state.current_phase = Phase.REGULAR
    line = render_line(cfg, state, "es")
    assert "🇪🇸" in line
    assert "Bolsa de Madrid" in line
    assert "EUR" in line


def test_reopen_unknown_append():
    settings = load_settings("config/config.yaml")
    cfg = settings.exchanges[0]
    state = ExchangeState(cfg)
    state.current_phase = Phase.REOPENING
    line = render_line(cfg, state, "es")
    assert "hora de reapertura desconocida" in line
