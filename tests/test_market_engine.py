import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_pulse_bot.calendar_engine import build_schedule
from market_pulse_bot.config import load_exchanges
from market_pulse_bot.market_engine import Phase, TransitionKind, compute_phase_state


def _configs():
    return {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}


def test_nyse_pre_market_to_regular() -> None:
    cfg = _configs()["XNYS"]
    schedule = build_schedule(cfg)
    pre = dt.datetime(2026, 9, 22, 9, 26, tzinfo=ZoneInfo("America/New_York"))
    state = compute_phase_state(cfg, schedule, pre.astimezone(dt.UTC), None, True)
    assert state.current_phase == Phase.EXTENDED_HOURS
    assert state.next_phase == Phase.REGULAR
    assert state.transition_kind == TransitionKind.TO_REGULAR_DIRECT
    assert state.show_transition is True

    regular = dt.datetime(2026, 9, 22, 9, 35, tzinfo=ZoneInfo("America/New_York"))
    state = compute_phase_state(cfg, schedule, regular.astimezone(dt.UTC), None, True)
    assert state.current_phase == Phase.REGULAR


def test_madrid_uses_xmad_and_opening_auction() -> None:
    cfg = _configs()["XMAD"]
    schedule = build_schedule(cfg)
    at_0845 = dt.datetime(2026, 9, 22, 8, 45, tzinfo=ZoneInfo("Europe/Madrid"))
    state = compute_phase_state(cfg, schedule, at_0845.astimezone(dt.UTC), None, True)
    assert cfg.mic == "XMAD"
    assert state.current_phase == Phase.AUCTION
    assert state.phase_variant == "opening_auction"
    assert state.next_phase == Phase.REGULAR


def test_tokyo_lunch_break() -> None:
    cfg = _configs()["XTKS"]
    schedule = build_schedule(cfg)
    at_1145 = dt.datetime(2026, 9, 24, 11, 45, tzinfo=ZoneInfo("Asia/Tokyo"))
    state = compute_phase_state(cfg, schedule, at_1145.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.LUNCH
    assert state.next_phase == Phase.REGULAR


def test_synthetic_fiji_schedule() -> None:
    cfg = _configs()["XSPX"]
    schedule = build_schedule(cfg)
    local = dt.datetime(2026, 9, 22, 10, 30, tzinfo=ZoneInfo("Pacific/Fiji"))
    state = compute_phase_state(cfg, schedule, local.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.REGULAR
    assert state.native_now.tzinfo == ZoneInfo("Pacific/Fiji")


def test_two_minute_auction_threshold() -> None:
    cfg = _configs()["XMAD"]
    schedule = build_schedule(cfg)
    at_0858 = dt.datetime(2026, 9, 22, 8, 58, tzinfo=ZoneInfo("Europe/Madrid"))
    state = compute_phase_state(cfg, schedule, at_0858.astimezone(dt.UTC), None, True)
    assert state.current_phase == Phase.AUCTION
    assert state.minutes_until == 2
    assert state.show_transition is True



def test_nyse_known_early_close_is_detected() -> None:
    cfg = _configs()["XNYS"]
    schedule = build_schedule(cfg)
    assert schedule.is_early_close(dt.date(2026, 11, 27)) is True



def test_madrid_closing_auction_occurs_after_continuous_session() -> None:
    cfg = _configs()["XMAD"]
    schedule = build_schedule(cfg)
    at_1732 = dt.datetime(2026, 9, 22, 17, 32, tzinfo=ZoneInfo("Europe/Madrid"))
    state = compute_phase_state(cfg, schedule, at_1732.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.AUCTION
    assert state.phase_variant == "closing_auction"
    assert state.next_phase_variant == "post_market"


def test_nyse_post_market_starts_at_official_close() -> None:
    cfg = _configs()["XNYS"]
    schedule = build_schedule(cfg)
    at_1601 = dt.datetime(2026, 9, 22, 16, 1, tzinfo=ZoneInfo("America/New_York"))
    state = compute_phase_state(cfg, schedule, at_1601.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.EXTENDED_HOURS
    assert state.phase_variant == "post_market"


def test_hkex_pre_market_lunch_break_and_after_close_auction() -> None:
    """HKEX: 30-min pre-market before the 09:30 open, a native (exchange_calendars-
    provided) 12:00-13:00 lunch break, and a closing auction that runs AFTER the
    reported 16:00 close (HKEX's own randomized-close window), not before it.
    """
    cfg = _configs()["XHKG"]
    schedule = build_schedule(cfg)

    pre_market = dt.datetime(2026, 9, 22, 9, 15, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    state = compute_phase_state(cfg, schedule, pre_market.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.EXTENDED_HOURS
    assert state.phase_variant == "pre_market"

    lunch = dt.datetime(2026, 9, 22, 12, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    state = compute_phase_state(cfg, schedule, lunch.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.LUNCH  # native exchange_calendars break, not phase_windows

    after_close = dt.datetime(2026, 9, 22, 16, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    state = compute_phase_state(cfg, schedule, after_close.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.AUCTION
    assert state.phase_variant == "closing_auction"


def test_euronext_paris_long_opening_call_and_trading_at_last() -> None:
    """Euronext's opening call period is a real ~1h45m (07:15-09:00), unlike the
    short opening auctions at most other venues; the closing auction (17:30-17:35)
    is followed by a Trading At Last session mapped onto the existing post_market
    phase per the reuse-first design (no new Phase was introduced for TAL).
    """
    cfg = _configs()["XPAR"]
    schedule = build_schedule(cfg)

    pre_open = dt.datetime(2026, 9, 22, 7, 30, tzinfo=ZoneInfo("Europe/Paris"))
    state = compute_phase_state(cfg, schedule, pre_open.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.AUCTION
    assert state.phase_variant == "opening_auction"

    tal = dt.datetime(2026, 9, 22, 17, 37, tzinfo=ZoneInfo("Europe/Paris"))
    state = compute_phase_state(cfg, schedule, tal.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.EXTENDED_HOURS
    assert state.phase_variant == "post_market"


def test_taiwan_has_no_pre_or_post_market_phase_modeled() -> None:
    """TWSE: only a 5-minute closing call auction (13:25-13:30) is reliably
    documented; no market-wide pre-market phase was found, so none was modeled
    (zero-hallucination -- absence of data is left as absence, not guessed at).
    """
    cfg = _configs()["XTAI"]
    variants = {w.phase for w in cfg.phase_windows}
    assert variants == {"closing_auction"}


@pytest.mark.xfail(
    reason=(
        "Known architecture gap, not yet fixed: SGX has a real, official market-wide "
        "midday break (12:00-13:00, SGX Rulebook 'Mid-Day Break') but "
        "exchange_calendars.get_calendar('XSES').session_has_break() reports False, "
        "so ExchangeCalendarsSchedule.has_break() also reports False for XSES and the "
        "engine shows REGULAR straight through lunch instead of LUNCH. phase_windows "
        "cannot fix this -- its Phase literal has no 'lunch' option by design, and "
        "lunch detection is intentionally separate from it. Needs an explicit "
        "lunch-break override field (mirroring SyntheticCalendarConfig's lunch_start/"
        "lunch_end) usable on exchange_calendars-type exchanges too."
    ),
    strict=True,
)
def test_sgx_midday_break_is_not_currently_detected() -> None:
    cfg = _configs()["XSES"]
    schedule = build_schedule(cfg)
    lunch = dt.datetime(2026, 9, 22, 12, 30, tzinfo=ZoneInfo("Asia/Singapore"))
    state = compute_phase_state(cfg, schedule, lunch.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.LUNCH


@pytest.mark.parametrize(
    ("mic", "local_time", "timezone"),
    [
        ("XNAS", (2026, 9, 22, 10, 0), "America/New_York"),
        ("XMEX", (2026, 9, 22, 10, 0), "America/Mexico_City"),
        ("XSGO", (2026, 9, 22, 12, 0), "America/Santiago"),
    ],
)
def test_new_americas_calendars_load_and_report_regular_session(
    mic: str, local_time: tuple[int, int, int, int, int], timezone: str
) -> None:
    cfg = _configs()[mic]
    schedule = build_schedule(cfg)
    local = dt.datetime(*local_time, tzinfo=ZoneInfo(timezone))
    state = compute_phase_state(cfg, schedule, local.astimezone(dt.UTC), None, False)
    assert state.current_phase == Phase.REGULAR
    assert state.native_now.tzinfo == ZoneInfo(timezone)


def test_nasdaq_is_distinct_exchange_with_shared_us_calendar() -> None:
    configs = _configs()
    assert configs["XNAS"].mic != configs["XNYS"].mic
    assert configs["XNAS"].calendar_name == configs["XNYS"].calendar_name == "XNYS"
