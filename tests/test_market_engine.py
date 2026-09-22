import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from market_pulse_bot.calendar_engine import build_schedule
from market_pulse_bot.config import load_exchanges
from market_pulse_bot.market_engine import Phase, TransitionKind, compute_phase_state


UTC = dt.timezone.utc


def _configs():
    return {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}


def test_nyse_pre_market_to_regular() -> None:
    cfg = _configs()["XNYS"]
    schedule = build_schedule(cfg)
    pre = dt.datetime(2026, 9, 22, 9, 20, tzinfo=ZoneInfo("America/New_York"))
    state = compute_phase_state(cfg, schedule, pre.astimezone(UTC), None, True)
    assert state.current_phase == Phase.EXTENDED_HOURS
    assert state.next_phase == Phase.REGULAR
    assert state.transition_kind == TransitionKind.TO_REGULAR_DIRECT
    assert state.show_transition is True

    regular = dt.datetime(2026, 9, 22, 9, 35, tzinfo=ZoneInfo("America/New_York"))
    state = compute_phase_state(cfg, schedule, regular.astimezone(UTC), None, True)
    assert state.current_phase == Phase.REGULAR


def test_madrid_uses_xmad_and_opening_auction() -> None:
    cfg = _configs()["XMAD"]
    schedule = build_schedule(cfg)
    at_0845 = dt.datetime(2026, 9, 22, 8, 45, tzinfo=ZoneInfo("Europe/Madrid"))
    state = compute_phase_state(cfg, schedule, at_0845.astimezone(UTC), None, True)
    assert cfg.mic == "XMAD"
    assert state.current_phase == Phase.AUCTION
    assert state.phase_variant == "opening_auction"
    assert state.next_phase == Phase.REGULAR


def test_tokyo_lunch_break() -> None:
    cfg = _configs()["XTKS"]
    schedule = build_schedule(cfg)
    at_1145 = dt.datetime(2026, 9, 24, 11, 45, tzinfo=ZoneInfo("Asia/Tokyo"))
    state = compute_phase_state(cfg, schedule, at_1145.astimezone(UTC), None, False)
    assert state.current_phase == Phase.LUNCH
    assert state.next_phase == Phase.REGULAR


def test_synthetic_fiji_schedule() -> None:
    cfg = _configs()["XSPX"]
    schedule = build_schedule(cfg)
    local = dt.datetime(2026, 9, 22, 10, 30, tzinfo=ZoneInfo("Pacific/Fiji"))
    state = compute_phase_state(cfg, schedule, local.astimezone(UTC), None, False)
    assert state.current_phase == Phase.REGULAR
    assert state.native_now.tzinfo == ZoneInfo("Pacific/Fiji")


def test_two_minute_auction_threshold() -> None:
    cfg = _configs()["XMAD"]
    schedule = build_schedule(cfg)
    at_0858 = dt.datetime(2026, 9, 22, 8, 58, tzinfo=ZoneInfo("Europe/Madrid"))
    state = compute_phase_state(cfg, schedule, at_0858.astimezone(UTC), None, True)
    assert state.current_phase == Phase.AUCTION
    assert state.minutes_until == 2
    assert state.show_transition is True
