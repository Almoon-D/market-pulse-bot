"""Pure deterministic market phase state machine."""

from __future__ import annotations

import datetime as dt
import itertools
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .calendar_engine import MarketSchedule
from .config import ExchangeConfig


class Phase(str, Enum):
    CLOSED = "closed"
    EXTENDED_HOURS = "extended_hours"
    AUCTION = "auction"
    REGULAR = "regular"
    LUNCH = "lunch"
    HOLIDAY = "holiday"
    REGULATORY_HALT = "regulatory_halt"
    TECHNICAL_HALT = "technical_halt"
    EXCEPTIONAL_CLOSURE = "exceptional_closure"
    POST_HALT_REOPENING = "post_halt_reopening"


INCIDENT_PHASES = frozenset(
    {Phase.REGULATORY_HALT, Phase.TECHNICAL_HALT, Phase.EXCEPTIONAL_CLOSURE}
)


class TransitionKind(str, Enum):
    TO_PRE_MARKET = "to_pre_market"
    TO_OPENING_AUCTION = "to_opening_auction"
    TO_REGULAR_AFTER_AUCTION = "to_regular_after_auction"
    TO_LUNCH = "to_lunch"
    TO_REGULAR_AFTER_LUNCH = "to_regular_after_lunch"
    TO_CLOSING_AUCTION = "to_closing_auction"
    TO_POST_MARKET = "to_post_market"
    TO_CLOSED = "to_closed"
    TO_REGULAR_DIRECT = "to_regular_direct"
    TO_POST_HALT_REOPENING = "to_post_halt_reopening"
    TO_REGULAR_UNCERTAIN = "to_regular_uncertain"


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
    mic: str
    phase: Phase
    detected_at: dt.datetime
    source_type: Literal["structured_feed", "rss_keyword", "manual"]
    note: str | None = None
    reopening_time: dt.datetime | None = None


@dataclass(frozen=True)
class PhaseState:
    current_phase: Phase
    phase_variant: str | None
    next_phase: Phase | None
    next_phase_variant: str | None
    transition_kind: TransitionKind | None
    transition_time: dt.datetime | None
    minutes_until: int | None
    show_transition: bool
    uncertain_transition: bool
    is_early_close_day: bool
    native_now: dt.datetime


@dataclass(frozen=True)
class UpcomingEvent:
    exchange_name: str
    region: str
    country_flag: str
    tz_label: str
    event_type: Literal["holiday", "early_close"]
    date: dt.date
    early_close_time: dt.datetime | None


@dataclass(frozen=True)
class _Interval:
    phase: Phase
    variant: str | None
    start: dt.datetime
    end: dt.datetime


def _minutes_until(now: dt.datetime, target: dt.datetime) -> int:
    return max(0, math.floor((target - now).total_seconds() / 60))


def _custom_intervals(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    session: dt.date,
) -> list[_Interval]:
    open_time = schedule.session_open(session)
    close_time = schedule.session_close(session)
    intervals: list[_Interval] = []
    for window in exchange.phase_windows:
        anchor = open_time if window.anchor == "session_open" else close_time
        intervals.append(
            _Interval(
                Phase.EXTENDED_HOURS if window.phase in {"pre_market", "post_market"} else Phase.AUCTION,
                window.phase,
                anchor + dt.timedelta(minutes=window.start_offset_minutes),
                anchor + dt.timedelta(minutes=window.end_offset_minutes),
            )
        )
    intervals.sort(key=lambda item: (item.start, item.end))
    for left, right in itertools.pairwise(intervals):
        if right.start < left.end:
            raise ValueError(f"{exchange.mic}: configured phase windows overlap")
    return intervals


def _session_intervals(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    session: dt.date,
) -> list[_Interval]:
    open_time = schedule.session_open(session)
    close_time = schedule.session_close(session)
    custom = _custom_intervals(exchange, schedule, session)

    base_core: list[_Interval]
    break_start = schedule.session_break_start(session)
    break_end = schedule.session_break_end(session)
    if break_start and break_end:
        base_core = [
            _Interval(Phase.REGULAR, None, open_time, break_start),
            _Interval(Phase.LUNCH, None, break_start, break_end),
            _Interval(Phase.REGULAR, None, break_end, close_time),
        ]
    else:
        base_core = [_Interval(Phase.REGULAR, None, open_time, close_time)]

    segmented_core: list[_Interval] = []
    for block in base_core:
        cursor = block.start
        blockers = [item for item in custom if item.start < block.end and item.end > block.start]
        for blocker in sorted(blockers, key=lambda item: item.start):
            if blocker.start > cursor:
                end = min(blocker.start, block.end)
                if end > cursor:
                    segmented_core.append(_Interval(block.phase, block.variant, cursor, end))
            cursor = max(cursor, blocker.end)
            if cursor >= block.end:
                break
        if cursor < block.end:
            segmented_core.append(_Interval(block.phase, block.variant, cursor, block.end))

    intervals = sorted(custom + segmented_core, key=lambda item: (item.start, item.end))
    return intervals


def _first_phase(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    session: dt.date,
) -> tuple[Phase, str | None, dt.datetime, TransitionKind]:
    intervals = _session_intervals(exchange, schedule, session)
    if not intervals:
        raise RuntimeError(f"{exchange.mic}: session produced no intervals")
    first = intervals[0]
    if first.variant == "pre_market":
        kind = TransitionKind.TO_PRE_MARKET
    elif first.variant == "opening_auction":
        kind = TransitionKind.TO_OPENING_AUCTION
    else:
        kind = TransitionKind.TO_REGULAR_DIRECT
    return first.phase, first.variant, first.start, kind


def _transition_kind(current: _Interval, target: _Interval) -> TransitionKind:
    if target.variant == "pre_market":
        return TransitionKind.TO_PRE_MARKET
    if target.variant == "opening_auction":
        return TransitionKind.TO_OPENING_AUCTION
    if target.phase == Phase.REGULAR and current.phase == Phase.AUCTION:
        return TransitionKind.TO_REGULAR_AFTER_AUCTION
    if target.phase == Phase.LUNCH:
        return TransitionKind.TO_LUNCH
    if target.phase == Phase.REGULAR and current.phase == Phase.LUNCH:
        return TransitionKind.TO_REGULAR_AFTER_LUNCH
    if target.variant == "closing_auction":
        return TransitionKind.TO_CLOSING_AUCTION
    if target.variant == "post_market":
        return TransitionKind.TO_POST_MARKET
    if target.phase == Phase.REGULAR and current.phase == Phase.EXTENDED_HOURS:
        return TransitionKind.TO_REGULAR_DIRECT
    if target.phase == Phase.CLOSED:
        return TransitionKind.TO_CLOSED
    return TransitionKind.TO_CLOSED


def _next_session_transition(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    after: dt.date,
) -> tuple[Phase, str | None, dt.datetime, TransitionKind]:
    session = schedule.next_session(after)
    return _first_phase(exchange, schedule, session)


def _scheduled_state(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    now_local: dt.datetime,
) -> tuple[Phase, str | None, Phase | None, str | None, dt.datetime | None, TransitionKind | None]:
    today = now_local.date()

    if not schedule.is_session(today):
        current = Phase.HOLIDAY if schedule.is_normal_business_weekday(today) else Phase.CLOSED
        next_phase, next_variant, next_time, kind = _next_session_transition(exchange, schedule, today + dt.timedelta(days=1))
        return current, None, next_phase, next_variant, next_time, kind

    intervals = _session_intervals(exchange, schedule, today)
    for index, interval in enumerate(intervals):
        if interval.start <= now_local < interval.end:
            if index + 1 < len(intervals):
                target = intervals[index + 1]
                return (
                    interval.phase,
                    interval.variant,
                    target.phase,
                    target.variant,
                    target.start,
                    _transition_kind(interval, target),
                )
            next_phase, next_variant, next_time, kind = _next_session_transition(
                exchange, schedule, today + dt.timedelta(days=1)
            )
            return interval.phase, interval.variant, next_phase, next_variant, next_time, kind

    if now_local < intervals[0].start:
        first = intervals[0]
        if first.variant == "pre_market":
            kind = TransitionKind.TO_PRE_MARKET
        elif first.variant == "opening_auction":
            kind = TransitionKind.TO_OPENING_AUCTION
        else:
            kind = TransitionKind.TO_REGULAR_DIRECT
        return Phase.CLOSED, None, first.phase, first.variant, first.start, kind

    next_phase, next_variant, next_time, kind = _next_session_transition(
        exchange, schedule, today + dt.timedelta(days=1)
    )
    return Phase.CLOSED, None, next_phase, next_variant, next_time, kind


def compute_phase_state(
    exchange: ExchangeConfig,
    schedule: MarketSchedule,
    now_utc: dt.datetime,
    incident: IncidentRecord | None,
    loop_mode: bool,
) -> PhaseState:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_utc = now_utc.astimezone(dt.UTC)
    now_local = now_utc.astimezone(schedule.tz)
    is_early = schedule.is_session(now_local.date()) and schedule.is_early_close(now_local.date())

    if incident and incident.phase in INCIDENT_PHASES:
        if incident.reopening_time and now_utc < incident.reopening_time:
            target = incident.reopening_time.astimezone(schedule.tz)
            return PhaseState(
                incident.phase, None, Phase.POST_HALT_REOPENING, None,
                TransitionKind.TO_POST_HALT_REOPENING, target,
                _minutes_until(now_utc, incident.reopening_time), True, False, is_early, now_local
            )
        if incident.reopening_time and now_utc >= incident.reopening_time:
            return PhaseState(
                Phase.POST_HALT_REOPENING, None, Phase.REGULAR, None,
                TransitionKind.TO_REGULAR_UNCERTAIN, None, None, True, True, is_early, now_local
            )
        return PhaseState(
            incident.phase, None, None, None, None, None, None, False, False, is_early, now_local
        )

    if incident and incident.phase == Phase.POST_HALT_REOPENING:
        return PhaseState(
            Phase.POST_HALT_REOPENING, None, Phase.REGULAR, None,
            TransitionKind.TO_REGULAR_UNCERTAIN, None, None, True, True, is_early, now_local
        )

    current, variant, next_phase, next_variant, next_time, kind = _scheduled_state(
        exchange, schedule, now_local
    )
    if next_time is None or next_phase is None or kind is None:
        return PhaseState(current, variant, None, None, None, None, None, False, False, is_early, now_local)

    minutes = _minutes_until(now_local, next_time)
    threshold = PRENOTICE_THRESHOLD_MINUTES.get(kind)
    show = loop_mode and threshold is not None and minutes <= threshold
    return PhaseState(
        current, variant, next_phase, next_variant, kind, next_time, minutes,
        show, False, is_early, now_local
    )


def _nth_business_weekday(start: dt.date, count: int, schedule: MarketSchedule) -> dt.date:
    seen = 0
    current = start
    while True:
        if schedule.is_normal_business_weekday(current):
            seen += 1
            if seen == count:
                return current
        current += dt.timedelta(days=1)


def build_upcoming_events(
    exchanges: list[ExchangeConfig],
    schedules: dict[str, MarketSchedule],
    now_utc: dt.datetime,
    horizon_business_days: int = 5,
) -> list[UpcomingEvent]:
    result: list[UpcomingEvent] = []
    for exchange in exchanges:
        schedule = schedules[exchange.mic]
        today = now_utc.astimezone(schedule.tz).date()
        end = _nth_business_weekday(today, horizon_business_days, schedule)
        current = today
        while current <= end:
            if schedule.is_normal_business_weekday(current):
                if not schedule.is_session(current):
                    result.append(UpcomingEvent(
                        exchange.name, exchange.region, exchange.country_flag,
                        exchange.tz_label, "holiday", current, None
                    ))
                elif schedule.is_early_close(current):
                    result.append(UpcomingEvent(
                        exchange.name, exchange.region, exchange.country_flag,
                        exchange.tz_label, "early_close", current, schedule.session_close(current)
                    ))
            current += dt.timedelta(days=1)
    return sorted(result, key=lambda event: (event.date, event.region, event.exchange_name))
