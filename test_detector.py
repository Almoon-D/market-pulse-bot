"""Tests for phase detection, including lunch break."""

import datetime as dt
from zoneinfo import ZoneInfo

from src.calendars import build_engine
from src.config import load_settings
from src.detector import compute_phase
from src.state import ExchangeState, Phase


def _run(mic: str, when: dt.datetime):
    settings = load_settings("config/config.yaml")
    cfg = next(e for e in settings.exchanges if e.mic == mic)
    engine = build_engine(cfg)
    state = ExchangeState(cfg)
    return compute_phase(cfg, engine, state, when)


def test_bmex_lunch_break_phase():
    # BMEX session 08:00–18:00 local, lunch 14:30–16:30
    tz = ZoneInfo("America/Mexico_City")
    lunch_time = dt.datetime(2024, 6, 3, 15, 0, tzinfo=tz)  # Monday 15:00 local
    phase, _, _ = _run("BMEX", lunch_time)
    assert phase == Phase.LUNCH


def test_bmex_morning_regular():
    tz = ZoneInfo("America/Mexico_City")
    morning = dt.datetime(2024, 6, 3, 10, 0, tzinfo=tz)
    phase, _, _ = _run("BMEX", morning)
    assert phase == Phase.REGULAR


def test_xmad_closed_after_post_market():
    tz = ZoneInfo("Europe/Madrid")
    night = dt.datetime(2024, 6, 3, 23, 0, tzinfo=tz)
    phase, _, _ = _run("XMAD", night)
    assert phase == Phase.CLOSED
