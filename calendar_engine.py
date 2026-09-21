"""
Calendar abstraction: turns a config entry into answers to four questions
for a given calendar date — is it a trading day, when does it open/close,
does it have a lunch break, and is it an early close — without the rest of
the app caring whether that came from ``exchange_calendars`` or a hand-written
YAML block.

No network I/O and no notion of "now" lives here: every method takes an
explicit ``datetime.date`` and returns timezone-aware ``datetime`` objects
in the exchange's own timezone. ``market_engine.py`` is the only module
that combines this with a live clock.
"""

import datetime as dt
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pandas as pd

from src.config import ExchangeConfig, SyntheticCalendarConfig


class MarketSchedule(ABC):
    """Calendar facts for one exchange, independent of source."""

    tz: ZoneInfo

    @abstractmethod
    def is_session(self, session: dt.date) -> bool:
        """True if ``session`` is a trading day."""

    @abstractmethod
    def next_session(self, after: dt.date) -> dt.date:
        """The next trading day on or after ``after`` (inclusive)."""

    @abstractmethod
    def session_open(self, session: dt.date) -> dt.datetime:
        """Official open, tz-aware, in the exchange's own timezone."""

    @abstractmethod
    def session_close(self, session: dt.date) -> dt.datetime:
        """Official close (already reflecting an early close if any)."""

    @abstractmethod
    def has_break(self, session: dt.date) -> bool: ...

    @abstractmethod
    def session_break_start(self, session: dt.date) -> dt.datetime | None: ...

    @abstractmethod
    def session_break_end(self, session: dt.date) -> dt.datetime | None: ...

    @abstractmethod
    def is_early_close(self, session: dt.date) -> bool: ...

    @abstractmethod
    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]: ...

    @abstractmethod
    def is_normal_business_weekday(self, session: dt.date) -> bool:
        """True if this weekday is normally a trading day (independent of
        holidays). Lets ``market_engine`` tell an official holiday (⚪️,
        a would-be business day the market skipped) apart from a plain
        weekend (⚫️, never a business day to begin with).
        """


class ExchangeCalendarsSchedule(MarketSchedule):
    """Backed by the ``exchange_calendars`` package (MIC-validated fail-fast
    at config-load time in ``src/config.py`` — by the time this class is
    constructed the MIC is known-good).
    """

    def __init__(self, mic: str, timezone: str) -> None:
        self._cal = xc.get_calendar(mic)
        self.tz = ZoneInfo(timezone)

    @staticmethod
    def _ts(session: dt.date) -> pd.Timestamp:
        return pd.Timestamp(session)

    def is_session(self, session: dt.date) -> bool:
        return bool(self._cal.is_session(self._ts(session)))

    def next_session(self, after: dt.date) -> dt.date:
        return self._cal.date_to_session(self._ts(after), direction="next").date()

    def _to_local(self, ts: pd.Timestamp) -> dt.datetime:
        return ts.to_pydatetime().astimezone(self.tz)

    def session_open(self, session: dt.date) -> dt.datetime:
        return self._to_local(self._cal.session_open(self._ts(session)))

    def session_close(self, session: dt.date) -> dt.datetime:
        return self._to_local(self._cal.session_close(self._ts(session)))

    def has_break(self, session: dt.date) -> bool:
        return bool(self._cal.session_has_break(self._ts(session)))

    def session_break_start(self, session: dt.date) -> dt.datetime | None:
        if not self.has_break(session):
            return None
        return self._to_local(self._cal.session_break_start(self._ts(session)))

    def session_break_end(self, session: dt.date) -> dt.datetime | None:
        if not self.has_break(session):
            return None
        return self._to_local(self._cal.session_break_end(self._ts(session)))

    def is_early_close(self, session: dt.date) -> bool:
        ts = self._ts(session)
        return ts in self._cal.early_closes

    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]:
        sessions = self._cal.sessions_in_range(self._ts(start), self._ts(end))
        return [s.date() for s in sessions]

    def is_normal_business_weekday(self, session: dt.date) -> bool:
        weekmask = self._cal.weekmask  # e.g. "1111100", index 0 = Monday
        return weekmask[session.weekday()] == "1"


class SyntheticSchedule(MarketSchedule):
    """Backed entirely by a YAML ``synthetic`` block — for exchanges
    ``exchange_calendars`` doesn't cover (e.g. XSPX / Fiji).
    """

    _MAX_LOOKAHEAD_DAYS = 3660  # ~10 years; a safety bound, not a real limit

    def __init__(self, cfg: SyntheticCalendarConfig, timezone: str) -> None:
        self.tz = ZoneInfo(timezone)
        self._open_h, self._open_m = self._parse_hm(cfg.open_time)
        self._close_h, self._close_m = self._parse_hm(cfg.close_time)
        self._lunch_start = self._parse_hm(cfg.lunch_start) if cfg.lunch_start else None
        self._lunch_end = self._parse_hm(cfg.lunch_end) if cfg.lunch_end else None
        self._trading_days = set(cfg.trading_days)
        self._holidays = {dt.date.fromisoformat(d) for d in cfg.holidays}

    @staticmethod
    def _parse_hm(value: str) -> tuple[int, int]:
        h, m = value.split(":")
        return int(h), int(m)

    def _combine(self, session: dt.date, hm: tuple[int, int]) -> dt.datetime:
        return dt.datetime(session.year, session.month, session.day, hm[0], hm[1], tzinfo=self.tz)

    def is_session(self, session: dt.date) -> bool:
        return session.weekday() in self._trading_days and session not in self._holidays

    def next_session(self, after: dt.date) -> dt.date:
        candidate = after
        for _ in range(self._MAX_LOOKAHEAD_DAYS):
            if self.is_session(candidate):
                return candidate
            candidate += dt.timedelta(days=1)
        raise RuntimeError(f"no trading day found within {self._MAX_LOOKAHEAD_DAYS} days of {after}")

    def session_open(self, session: dt.date) -> dt.datetime:
        return self._combine(session, (self._open_h, self._open_m))

    def session_close(self, session: dt.date) -> dt.datetime:
        return self._combine(session, (self._close_h, self._close_m))

    def has_break(self, session: dt.date) -> bool:
        return self._lunch_start is not None

    def session_break_start(self, session: dt.date) -> dt.datetime | None:
        return self._combine(session, self._lunch_start) if self._lunch_start else None

    def session_break_end(self, session: dt.date) -> dt.datetime | None:
        return self._combine(session, self._lunch_end) if self._lunch_end else None

    def is_early_close(self, session: dt.date) -> bool:
        # Synthetic calendars have no separate early-close concept: a
        # different close time on a given day would just be a different
        # 'close_time', which this simple model does not express per-date.
        return False

    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]:
        out: list[dt.date] = []
        d = start
        while d <= end:
            if self.is_session(d):
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def is_normal_business_weekday(self, session: dt.date) -> bool:
        return session.weekday() in self._trading_days


def build_schedule(exchange: ExchangeConfig) -> MarketSchedule:
    """Factory: dispatch on ``calendar_type``, exactly as Section 6 requires."""
    if exchange.calendar_type == "exchange_calendars":
        return ExchangeCalendarsSchedule(exchange.mic, exchange.timezone)
    if exchange.calendar_type == "synthetic":
        assert exchange.synthetic is not None  # enforced in src/config.py
        return SyntheticSchedule(exchange.synthetic, exchange.timezone)
    raise ValueError(f"unknown calendar_type: {exchange.calendar_type!r}")
