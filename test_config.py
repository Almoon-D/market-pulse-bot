"""Tests for YAML configuration loading."""

from src.config import load_settings


def test_load_real_config(tmp_path):
    settings = load_settings("config/config.yaml")
    assert settings.language == "es"
    assert settings.default_timezone == "UTC"
    assert settings.poll_interval_seconds == 30
    assert len(settings.exchanges) == 5

    mics = [e.mic for e in settings.exchanges]
    assert mics == ["XMAD", "BVMF", "BMEX", "XSPX", "XFJI"]


def test_synthetic_schedule_requires_trading_days():
    settings = load_settings("config/config.yaml")
    fiji = next(e for e in settings.exchanges if e.mic == "XFJI")
    assert fiji.calendar_type.value == "synthetic"
    assert set(fiji.synthetic_schedule.trading_days) == {1, 2, 3, 4}
    assert fiji.synthetic_schedule.open == "08:30"
    assert fiji.synthetic_schedule.close == "13:00"


def test_bmex_lunch_break():
    settings = load_settings("config/config.yaml")
    bmex = next(e for e in settings.exchanges if e.mic == "BMEX")
    assert bmex.lunch_break is not None
    assert bmex.lunch_break.start == "14:30"
    assert bmex.lunch_break.end == "16:30"
