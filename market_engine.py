from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .calendar_engine import MarketSchedule, SessionInfo
from .config import ExchangeConfig


class Phase(StrEnum):
    CLOSED = "closed"
    EXTENDED = "extended"
    OPENING_AUCTION = "opening_auction"
    REGULAR = "regular"
    LUNCH = "lunch"
    CLOSING_AUCTION = "closing_auction"
    POST_HALT_REOPENING = "post_halt_reopening"


@dataclass(frozen=True)
class PhaseState:
    phase: Phase
    started_at: datetime | None
    target_at: datetime | None
    next_phase: Phase | None
    minutes_to_target: int | None


def _today_session(schedule: MarketSchedule, now: datetime) -> SessionInfo:
    return schedule.session_for(now.date())


def determine_phase(config: ExchangeConfig, schedule: MarketSchedule, now: datetime) -> PhaseState:
    local_now = now.astimezone(schedule.zone)
    session = _today_session(schedule, local_now)
    offsets = config.session_offsets

    if not session.is_open:
        return PhaseState(Phase.CLOSED, None, None, None, None)

    pre_start = session.open_at - timedelta(minutes=offsets.pre_market_minutes)
    auction_end = session.open_at + timedelta(minutes=offsets.opening_auction_minutes)
    close_auction_start = session.close_at - timedelta(minutes=offsets.closing_auction_minutes)
    post_end = session.close_at + timedelta(minutes=offsets.post_market_minutes)

    if local_now < pre_start:
        return PhaseState(Phase.CLOSED, None, pre_start, Phase.EXTENDED, _minutes_until(local_now, pre_start))
    if local_now < session.open_at:
        return PhaseState(Phase.EXTENDED, pre_start, session.open_at, Phase.OPENING_AUCTION if offsets.opening_auction_minutes else Phase.REGULAR, _minutes_until(local_now, session.open_at))
    if offsets.opening_auction_minutes and local_now < auction_end:
        return PhaseState(Phase.OPENING_AUCTION, session.open_at, auction_end, Phase.REGULAR, _minutes_until(local_now, auction_end))

    if config.lunch and session.open_at <= local_now < session.close_at:
        lunch_start = local_now.replace(hour=int(config.lunch.start[:2]), minute=int(config.lunch.start[3:]), second=0, microsecond=0)
        lunch_end = local_now.replace(hour=int(config.lunch.end[:2]), minute=int(config.lunch.end[3:]), second=0, microsecond=0)
        if lunch_start <= local_now < lunch_end:
            return PhaseState(Phase.LUNCH, lunch_start, lunch_end, Phase.REGULAR, _minutes_until(local_now, lunch_end))

    if local_now < close_auction_start:
        return PhaseState(Phase.REGULAR, auction_end if offsets.opening_auction_minutes else session.open_at, close_auction_start, Phase.CLOSING_AUCTION if offsets.closing_auction_minutes else Phase.EXTENDED, _minutes_until(local_now, close_auction_start))
    if offsets.closing_auction_minutes and local_now < session.close_at:
        return PhaseState(Phase.CLOSING_AUCTION, close_auction_start, session.close_at, Phase.EXTENDED, _minutes_until(local_now, session.close_at))
    if local_now < post_end:
        return PhaseState(Phase.EXTENDED, session.close_at, post_end, Phase.CLOSED, _minutes_until(local_now, post_end))
    return PhaseState(Phase.CLOSED, post_end, None, None, None)


def _minutes_until(now: datetime, target: datetime) -> int:
    seconds = (target - now).total_seconds()
    return max(0, int(seconds // 60))
