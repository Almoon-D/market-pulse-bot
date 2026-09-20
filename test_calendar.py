"""Tests for calendar engines."""

import datetime as dt

from src.calendars import build_engine
from src.config import load_settings


def test_xmad_weekend_not_trading():
    settings = load_settings("config/config.yaml")
    xmad = next(e for e in settings.exchanges if e.mic == "XMAD")
    engine = build_engine(xmad)
    assert not engine.is_trading_day(dt.date(2024, 1, 6))   # Saturday
    assert engine.is_trading_day(dt.date(2024, 1, 2))       # Tuesday


def test_xmad_holiday_detected():
    settings = load_settings("config/config.yaml")
    xmad = next(e for e in settings.exchanges if e.mic == "XMAD")
    engine = build_engine(xmad)
    holidays = engine.holidays_in(dt.date(2024, 12, 20), dt.date(2024, 12, 31))
    assert dt.date(2024, 12, 25) in holidays


def test_fiji_synthetic():
    settings = load_settings("config/config.yaml")
    fiji = next(e for e in settings.exchanges if e.mic == "XFJI")
    engine = build_engine(fiji)
    assert not engine.is_trading_day(dt.date(2024, 3, 1))   # Friday — not in Mon-Thu
    assert engine.is_trading_day(dt.date(2024, 3, 4))       # Monday
    sess = engine.session_for(dt.date(2024, 3, 4))
    assert sess is not None
    assert sess.open_at.hour == 8 and sess.open_at.minute == 30
    assert sess.close_at.hour == 13 and sess.close_at.minute == 0
