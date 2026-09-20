"""Calendar engines: synthetic (weekly pattern + overrides) and xcal-driven."""

import datetime as dt
from abc import ABC, abstractmethod

from .config import DayType, ExchangeConfig, Session


class TradingSession:
    __slots__ = ("date", "open_at", "close_at", "day_type")

    def __init__(self, date: dt.date, open_at: dt.datetime,
                 close_at: dt.datetime, day_type: DayType) -> None:
        self.date = date
        self.open_at = open_at
        self.close_at = close_at
        self.day_type = day_type


class CalendarEngine(ABC):
    def __init__(self, cfg: ExchangeConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def is_trading_day(self, day: dt.date) -> bool: ...

    @abstractmethod
    def session_for(self, day: dt.date) -> TradingSession | None: ...

    def early_close_days_in(self, start: dt.date, end: dt.date) -> dict[dt.date, dt.time]:
        out: dict[dt.date, dt.time] = {}
        for iso, hhmm in self.cfg.early_close_days.items():
            day = dt.date.fromisoformat(iso)
            if start <= day <= end:
                hh, mm = hhmm.split(":")
                out[day] = dt.time(int(hh), int(mm))
        return out


def _tz(name: str) -> dt.tzinfo:
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _at(day: dt.date, hhmm: str, tz: dt.tzinfo) -> dt.datetime:
    hh, mm = hhmm.split(":")
    return dt.datetime.combine(day, dt.time(int(hh), int(mm)), tzinfo=tz)


class SyntheticEngine(CalendarEngine):
    """Weekday-pattern calendar with holiday/override support. No external deps."""

    def _override(self, day: dt.date) -> DayType | None:
        raw = self.cfg.holidays.get(day.isoformat())
        if raw is None:
            return None
        try:
            return DayType(raw)
        except ValueError:
            return None

    def is_trading_day(self, day: dt.date) -> bool:
        override = self._override(day)
        if override is not None:
            return override in (DayType.FULL, DayType.EARLY_CLOSE)
        return day.weekday() in self.cfg.trading_weekdays

    def session_for(self, day: dt.date) -> TradingSession | None:
        if not self.is_trading_day(day):
            return None
        override = self._override(day)
        day_type = override if override is not None else DayType.FULL
        s = self.cfg.session
        if s is None:
            return None
        tz = _tz(self.cfg.tz)
        return TradingSession(
            date=day,
            open_at=_at(day, s.open, tz),
            close_at=_at(day, s.close, tz),
            day_type=day_type,
        )


class XcalEngine(CalendarEngine):
    """Calendar sourced from the `xcal` package (exchange MIC -> calendar)."""

    def __init__(self, cfg: ExchangeConfig) -> None:
        super().__init__(cfg)
        try:
            import xcal  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                f"xcal backend requested for {cfg.mic} but package 'xcal' is not installed"
            ) from exc
        self._xcal = xcal
        self._cal = xcal.get_calendar(cfg.mic)

    def is_trading_day(self, day: dt.date) -> bool:
        return self._cal.is_session(day)

    def session_for(self, day: dt.date) -> TradingSession | None:
        if not self.is_trading_day(day):
            return None
        s = self.cfg.session
        if s is None:
            return None
        times = self._cal.session_times(day)  # (open_dt, close_dt) tz-aware
        open_at, close_at = times
        override = self.cfg.holidays.get(day.isoformat())
        day_type = DayType(override) if override else DayType.FULL
        return TradingSession(date=day, open_at=open_at, close_at=close_at,
                              day_type=day_type)


def build_engine(cfg: ExchangeConfig) -> CalendarEngine:
    if cfg.calendar_type == CalendarType.XCAL:
        return XcalEngine(cfg)
    return SyntheticEngine(cfg)
