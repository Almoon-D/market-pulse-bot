"""Per-exchange state machine with phase transitions and countdowns."""

import abc
import datetime as dt
import enum


class Phase(enum.Enum):
    CLOSED = "closed"
    PRE_MARKET = "pre_market"
    OPENING_AUCTION = "opening_auction"
    REGULAR = "regular"
    LUNCH = "lunch"
    CLOSING_AUCTION = "closing_auction"
    POST_MARKET = "post_market"
    EARLY_CLOSE = "early_close"
    HOLIDAY = "holiday"
    HALT_REGULATORY = "halt_regulatory"
    HALT_TECHNICAL = "halt_technical"
    REOPENING = "reopening"
    EXCEPTIONAL = "exceptional"


class EventKind(enum.Enum):
    OPEN = "open"
    CLOSE = "close"
    LUNCH_START = "lunch_start"
    LUNCH_END = "lunch_end"
    AUCTION_START = "auction_start"
    AUCTION_END = "auction_end"
    EARLY_CLOSE = "early_close"
    HOLIDAY = "holiday"
    HALT = "halt"
    REOPEN = "reopen"


_TRANSITIONS_ALLOWED: dict[Phase, set[Phase]] = {
    Phase.CLOSED: {Phase.PRE_MARKET, Phase.OPENING_AUCTION, Phase.REGULAR, Phase.HOLIDAY},
    Phase.PRE_MARKET: {Phase.OPENING_AUCTION, Phase.REGULAR, Phase.CLOSED, Phase.HOLIDAY},
    Phase.OPENING_AUCTION: {Phase.REGULAR, Phase.CLOSED, Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL},
    Phase.REGULAR: {Phase.LUNCH, Phase.CLOSING_AUCTION, Phase.CLOSED, Phase.EARLY_CLOSE,
                    Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL, Phase.EXCEPTIONAL},
    Phase.LUNCH: {Phase.REGULAR, Phase.CLOSING_AUCTION, Phase.CLOSED,
                  Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL},
    Phase.CLOSING_AUCTION: {Phase.CLOSED, Phase.POST_MARKET, Phase.EARLY_CLOSE,
                            Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL},
    Phase.POST_MARKET: {Phase.CLOSED},
    Phase.EARLY_CLOSE: {Phase.CLOSED, Phase.POST_MARKET},
    Phase.HOLIDAY: {Phase.CLOSED},
    Phase.HALT_REGULATORY: {Phase.REOPENING, Phase.CLOSED, Phase.EXCEPTIONAL},
    Phase.HALT_TECHNICAL: {Phase.REOPENING, Phase.CLOSED, Phase.EXCEPTIONAL},
    Phase.REOPENING: {Phase.REGULAR, Phase.CLOSING_AUCTION, Phase.CLOSED},
    Phase.EXCEPTIONAL: {Phase.CLOSED, Phase.REOPENING},
}


class BaseState(abc.ABC):
    """Abstract base for per-market states."""

    @abc.abstractmethod
    def set_phase(self, new: "Phase", now: dt.datetime) -> bool:
        """Apply transition; return True if the phase actually changed."""

    @abc.abstractmethod
    def minutes_until(self, target: dt.datetime, ref: dt.datetime) -> int | None:
        """Whole minutes from ref to target; None if target is in the past."""


class ExchangeState(BaseState):
    """Concrete state holder for one exchange."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.current_phase: Phase = Phase.CLOSED
        self.prev_phase: Phase | None = None
        self.last_transition: dt.datetime | None = None
        self.next_transition: dt.datetime | None = None
        self.next_transition_phase: Phase | None = None
        self.active_halt_since: dt.datetime | None = None
        self.exceptional_since: dt.datetime | None = None
        self.last_seen_half_day_close: dt.date | None = None
        self.last_holiday_seen: dt.date | None = None

    def was_in_regular(self) -> bool:
        return self.current_phase in (Phase.REGULAR, Phase.LUNCH, Phase.CLOSING_AUCTION)

    def set_phase(self, new: Phase, now: dt.datetime) -> bool:
        if new == self.current_phase:
            return False
        allowed = _TRANSITIONS_ALLOWED.get(self.current_phase, set())
        if new not in allowed:
            # forced transitions (incidents, restarts) bypass the table but are recorded
            if new not in (Phase.EXCEPTIONAL, Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL,
                           Phase.REOPENING, Phase.HOLIDAY, Phase.EARLY_CLOSE):
                return False
        self.prev_phase = self.current_phase
        self.current_phase = new
        self.last_transition = now
        return True

    def minutes_until(self, target: dt.datetime, ref: dt.datetime) -> int | None:
        if target <= ref:
            return None
        return int((target - ref).total_seconds() // 60)
