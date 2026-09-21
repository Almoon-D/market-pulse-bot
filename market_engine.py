"""
Pure, offline session-phase state machine.

Given an exchange's config, its ``MarketSchedule``, a point in time, and
(optionally) a live incident record, this module deterministically derives
"what phase is this exchange in right now, and what's next". It performs
no I/O of any kind — not the network, not the clock (the caller supplies
``now_utc`` explicitly), not even a lock. That makes it trivial to unit
test and safe to call from both the main render loop and any future
tooling without worrying about concurrency.
"""

import datetime as dt
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from src.calendar_engine import MarketSchedule
from src.config import ExchangeConfig


class Phase(str, Enum):
    """The full status palette (Section 1.A)."""

    CLOSED = "closed"                              # ⚫️
    EXTENDED_HOURS = "extended_hours"               # 🟣
    AUCTION = "auction"                             # 🔵
    REGULAR = "regular"                             # 🟢
    LUNCH = "lunch"                                 # 🔘
    HOLIDAY = "holiday"                             # ⚪️
    REGULATORY_HALT = "regulatory_halt"             # 🔶
    TECHNICAL_HALT = "technical_halt"               # 🟥
    EXCEPTIONAL_CLOSURE = "exceptional_closure"     # 🚨
    POST_HALT_REOPENING = "post_halt_reopening"     # 🔷


INCIDENT_PHASES = frozenset(
    {Phase.REGULATORY_HALT, Phase.TECHNICAL_HALT, Phase.EXCEPTIONAL_CLOSURE}
)


class TransitionKind(str, Enum):
    """Every named transition from Section 1.B, each with its own
    pre-notice threshold and its own phrasing in the locale files — two
    transitions can share a target ``Phase`` (auction is both an opening
    and a closing event) but never share a threshold or a sentence.
    """

    TO_PRE_MARKET = "to_pre_market"                       # 15 min
    TO_OPENING_AUCTION = "to_opening_auction"             # 10 min
    TO_REGULAR_AFTER_AUCTION = "to_regular_after_auction"  # 2 min
    TO_LUNCH = "to_lunch"                                 # 5 min
    TO_REGULAR_AFTER_LUNCH = "to_regular_after_lunch"     # 5 min
    TO_CLOSING_AUCTION = "to_closing_auction"             # 5 min
    TO_POST_MARKET = "to_post_market"                     # 15 min
    TO_CLOSED = "to_closed"                               # 5 min
    TO_REGULAR_DIRECT = "to_regular_direct"               # 5 min (no auction/pre-market configured)
    TO_POST_HALT_REOPENING = "to_post_halt_reopening"     # incident override, no threshold gate
    TO_REGULAR_UNCERTAIN = "to_regular_uncertain"         # incident override, "≥5 min, possible extension"


# Section 1.B, verbatim. These are fixed properties of the notation system
# itself (not a per-exchange operational fact), so — unlike session
# offsets — they are a named constant table here rather than YAML.
PRENOTICE_THRESHOLD_MINUTES: dict[TransitionKind, int] = {
    TransitionKind.TO_PRE_MARKET: 15,
    TransitionKind.TO_OPENING_AUCTION: 10,
    TransitionKind.TO_REGULAR_AFTER_AUCTION: 2,
    TransitionKind.TO_LUNCH: 5,
    TransitionKind.TO_REGULAR_AFTER_LUNCH: 5,
    TransitionKind.TO_CLOSING_AUCTION: 5,
    TransitionKind.TO_POST_MARKET: 15,
    TransitionKind.TO_CLOSED: 5,
    TransitionKind.TO_REGULAR_DIRECT: 5,
}


@dataclass(frozen=True)
class IncidentRecord:
    """One live incident, as written into shared state by ``halt_detector``."""

    mic: str
    phase: Phase  # one of REGULATORY_HALT, TECHNICAL_HALT, EXCEPTIONAL_CLOSURE, POST_HALT_REOPENING
    detected_at: dt.datetime  # tz-aware UTC
    source_type: Literal["structured_feed", "rss_keyword", "manual"]
    note: str | None = None
    reopening_time: dt.datetime | None = None  # tz-aware UTC


@dataclass(frozen=True)
class PhaseState:
    """The fully-resolved answer for one exchange at one instant."""

    current_phase: Phase
    next_phase: Phase | None
    transition_kind: TransitionKind | None
    transition_time: dt.datetime | None  # tz-aware, exchange-local
    minutes_until: int | None
    show_transition: bool
    uncertain_transition: bool  # True only for the "≥5 min, possible extension" case
    is_early_close_day: bool
    native_now: dt.datetime  # 'now', in the exchange's own local tz


@dataclass(frozen=True)
class UpcomingEvent:
    """One entry in the 5-business-day forecast (message 1)."""

    exchange_name: str
    region: str
    country_flag: str
    tz_label: str
    event_type: Literal["holiday", "early_close"]
    date: dt.date
    early_close_time: dt.datetime | None  # exchange-local, only for "early_close"


def _minutes_until(now: dt.datetime, target: dt.datetime) -> int:
    # Floored per Section 2. Clamped at zero: by construction every caller
    # in this module recomputes the bucket now falls into from scratch on
    # every call (there is no incrementally-updated countdown anywhere),
    # so a target time is never more than a rounding hair in the past —
    # the clamp is pure defensive insurance, not a normal code path.
    return max(0, math.floor((target - now).total_seconds() / 60))


def _next_open_phase(
    exchange: ExchangeConfig, schedule: MarketSchedule, session: dt.date
) -> tuple[Phase, dt.datetime, TransitionKind]:
    """What phase does ``session`` start with, and when."""
    offs = exchange.session_offsets
    open_ = schedule.session_open(session)
    if offs.pre_market_minutes:
        return (
            Phase.EXTENDED_HOURS,
            open_ - dt.timedelta(minutes=offs.pre_market_minutes),
            TransitionKind.TO_PRE_MARKET,
        )
    if offs.opening_auction_minutes:
        return (
            Phase.AUCTION,
            open_ - dt.timedelta(minutes=offs.opening_auction_minutes),
            TransitionKind.TO_OPENING_AUCTION,
        )
    return Phase.REGULAR, open_, TransitionKind.TO_REGULAR_DIRECT


def _scheduled_phase(
    exchange: ExchangeConfig, schedule: MarketSchedule, now_local: dt.datetime
) -> tuple[Phase, Phase | None, dt.datetime | None, TransitionKind | None]:
    """The calendar-driven phase, ignoring any live incident override."""
    today = now_local.date()
    offs = exchange.session_offsets

    if not schedule.is_session(today):
        current = (
            Phase.HOLIDAY if schedule.is_normal_business_weekday(today) else Phase.CLOSED
        )
        next_session = schedule.next_session(today + dt.timedelta(days=1))
        nxt_phase, nxt_time, kind = _next_open_phase(exchange, schedule, next_session)
        return current, nxt_phase, nxt_time, kind

    open_ = schedule.session_open(today)
    close_ = schedule.session_close(today)
    pre_start = open_ - dt.timedelta(minutes=offs.pre_market_minutes) if offs.pre_market_minutes else None
    auc_open_start = (
        open_ - dt.timedelta(minutes=offs.opening_auction_minutes) if offs.opening_auction_minutes else None
    )
    auc_close_start = (
        close_ - dt.timedelta(minutes=offs.closing_auction_minutes) if offs.closing_auction_minutes else None
    )
    post_end = close_ + dt.timedelta(minutes=offs.post_market_minutes) if offs.post_market_minutes else None
    has_break = schedule.has_break(today)
    lunch_start = schedule.session_break_start(today) if has_break else None
    lunch_end = schedule.session_break_end(today) if has_break else None

    day_start = pre_start or auc_open_start or open_

    def after_close() -> tuple[Phase, Phase | None, dt.datetime | None, TransitionKind | None]:
        next_session = schedule.next_session(today + dt.timedelta(days=1))
        nxt_phase, nxt_time, kind = _next_open_phase(exchange, schedule, next_session)
        return Phase.CLOSED, nxt_phase, nxt_time, kind

    if now_local < day_start:
        nxt_phase, nxt_time, kind = _next_open_phase(exchange, schedule, today)
        return Phase.CLOSED, nxt_phase, nxt_time, kind

    if pre_start is not None and pre_start <= now_local < (auc_open_start or open_):
        if auc_open_start is not None:
            return Phase.EXTENDED_HOURS, Phase.AUCTION, auc_open_start, TransitionKind.TO_OPENING_AUCTION
        return Phase.EXTENDED_HOURS, Phase.REGULAR, open_, TransitionKind.TO_REGULAR_DIRECT

    if auc_open_start is not None and auc_open_start <= now_local < open_:
        return Phase.AUCTION, Phase.REGULAR, open_, TransitionKind.TO_REGULAR_AFTER_AUCTION

    morning_end = lunch_start or auc_close_start or close_
    if open_ <= now_local < morning_end:
        if lunch_start is not None:
            return Phase.REGULAR, Phase.LUNCH, lunch_start, TransitionKind.TO_LUNCH
        if auc_close_start is not None:
            return Phase.REGULAR, Phase.AUCTION, auc_close_start, TransitionKind.TO_CLOSING_AUCTION
        if post_end is not None:
            return Phase.REGULAR, Phase.EXTENDED_HOURS, close_, TransitionKind.TO_POST_MARKET
        return Phase.REGULAR, Phase.CLOSED, close_, TransitionKind.TO_CLOSED

    if lunch_start is not None and lunch_end is not None and lunch_start <= now_local < lunch_end:
        return Phase.LUNCH, Phase.REGULAR, lunch_end, TransitionKind.TO_REGULAR_AFTER_LUNCH

    if lunch_end is not None:
        afternoon_end = auc_close_start or close_
        if lunch_end <= now_local < afternoon_end:
            if auc_close_start is not None:
                return Phase.REGULAR, Phase.AUCTION, auc_close_start, TransitionKind.TO_CLOSING_AUCTION
            if post_end is not None:
                return Phase.REGULAR, Phase.EXTENDED_HOURS, close_, TransitionKind.TO_POST_MARKET
            return Phase.REGULAR, Phase.CLOSED, close_, TransitionKind.TO_CLOSED

    if auc_close_start is not None and auc_close_start <= now_local < close_:
        if post_end is not None:
            return Phase.AUCTION, Phase.EXTENDED_HOURS, close_, TransitionKind.TO_POST_MARKET
        return Phase.AUCTION, Phase.CLOSED, close_, TransitionKind.TO_CLOSED

    if post_end is not None and close_ <= now_local < post_end:
        return Phase.EXTENDED_HOURS, Phase.CLOSED, post_end, TransitionKind.TO_CLOSED

    return after_close()


def compute_phase_state(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    now_utc: dt.datetime,
    incident: IncidentRecord | None,
    loop_mode: bool,
) -> PhaseState:
    """The single entry point the render loop calls, once per exchange
    per tick. ``now_utc`` must be tz-aware.
    """
    now_local = now_utc.astimezone(schedule.tz)
    today = now_local.date()
    is_early_close_day = schedule.is_session(today) and schedule.is_early_close(today)

    if incident is not None and incident.phase in INCIDENT_PHASES:
        if incident.reopening_time is not None and now_utc < incident.reopening_time:
            return PhaseState(
                current_phase=incident.phase,
                next_phase=Phase.POST_HALT_REOPENING,
                transition_kind=TransitionKind.TO_POST_HALT_REOPENING,
                transition_time=incident.reopening_time.astimezone(schedule.tz),
                minutes_until=_minutes_until(now_utc, incident.reopening_time),
                show_transition=True,
                uncertain_transition=False,
                is_early_close_day=is_early_close_day,
                native_now=now_local,
            )
        if incident.reopening_time is not None and now_utc >= incident.reopening_time:
            # Announced time has arrived; auto-promote without waiting on
            # the operator (or the feed) to flip the record explicitly.
            return PhaseState(
                current_phase=Phase.POST_HALT_REOPENING,
                next_phase=Phase.REGULAR,
                transition_kind=TransitionKind.TO_REGULAR_UNCERTAIN,
                transition_time=None,
                minutes_until=None,
                show_transition=True,
                uncertain_transition=True,
                is_early_close_day=is_early_close_day,
                native_now=now_local,
            )
        # Active halt, no reopening time announced yet: shown alone, per
        # Section 1.B ("never preceded by scheduled 🔜").
        return PhaseState(
            current_phase=incident.phase,
            next_phase=None,
            transition_kind=None,
            transition_time=None,
            minutes_until=None,
            show_transition=False,
            uncertain_transition=False,
            is_early_close_day=is_early_close_day,
            native_now=now_local,
        )

    if incident is not None and incident.phase == Phase.POST_HALT_REOPENING:
        return PhaseState(
            current_phase=Phase.POST_HALT_REOPENING,
            next_phase=Phase.REGULAR,
            transition_kind=TransitionKind.TO_REGULAR_UNCERTAIN,
            transition_time=None,
            minutes_until=None,
            show_transition=True,
            uncertain_transition=True,
            is_early_close_day=is_early_close_day,
            native_now=now_local,
        )

    current, next_phase, next_time, kind = _scheduled_phase(exchange, schedule, now_local)
    if next_phase is None or next_time is None or kind is None:
        return PhaseState(
            current_phase=current,
            next_phase=None,
            transition_kind=None,
            transition_time=None,
            minutes_until=None,
            show_transition=False,
            uncertain_transition=False,
            is_early_close_day=is_early_close_day,
            native_now=now_local,
        )

    minutes_until = _minutes_until(now_local, next_time)
    threshold = PRENOTICE_THRESHOLD_MINUTES[kind]
    show = loop_mode and minutes_until <= threshold
    return PhaseState(
        current_phase=current,
        next_phase=next_phase,
        transition_kind=kind,
        transition_time=next_time,
        minutes_until=minutes_until,
        show_transition=show,
        uncertain_transition=False,
        is_early_close_day=is_early_close_day,
        native_now=now_local,
    )


def _nth_normal_weekday_end(start: dt.date, n: int, schedule: MarketSchedule) -> dt.date:
    """Calendar date such that exactly ``n`` normal business weekdays
    (independent of holidays — that's the whole point) fall in
    ``[start, result]``.
    """
    count = 0
    d = start
    while True:
        if schedule.is_normal_business_weekday(d):
            count += 1
            if count == n:
                return d
        d += dt.timedelta(days=1)


def build_upcoming_events(
    exchanges: list[ExchangeConfig],
    schedules: dict[str, MarketSchedule],
    now_utc: dt.datetime,
    horizon_business_days: int = 5,
) -> list[UpcomingEvent]:
    """Message 1: holidays and early closes over the next N business days,
    per exchange, all regions combined.
    """
    events: list[UpcomingEvent] = []
    for exchange in exchanges:
        schedule = schedules[exchange.mic]
        today = now_utc.astimezone(schedule.tz).date()
        end = _nth_normal_weekday_end(today, horizon_business_days, schedule)
        d = today
        while d <= end:
            if schedule.is_normal_business_weekday(d):
                if not schedule.is_session(d):
                    events.append(
                        UpcomingEvent(
                            exchange.name, exchange.region, exchange.country_flag,
                            exchange.tz_label, "holiday", d, None,
                        )
                    )
                elif schedule.is_early_close(d):
                    events.append(
                        UpcomingEvent(
                            exchange.name,
                            exchange.region,
                            exchange.country_flag,
                            exchange.tz_label,
                            "early_close",
                            d,
                            schedule.session_close(d),
                        )
                    )
            d += dt.timedelta(days=1)
    events.sort(key=lambda e: (e.date, e.region, e.exchange_name))
    return events
