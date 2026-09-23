"""Calendar abstraction backed by exchange_calendars or explicit synthetic YAML."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xc

from .config import ExchangeConfig, SyntheticCalendarConfig


class MarketSchedule(ABC):
    tz: ZoneInfo

    @abstractmethod
    def is_session(self, session: dt.date) -> bool: ...

    @abstractmethod
    def next_session(self, after: dt.date) -> dt.date: ...

    @abstractmethod
    def session_open(self, session: dt.date) -> dt.datetime: ...

    @abstractmethod
    def session_close(self, session: dt.date) -> dt.datetime: ...

    @abstractmethod
    def has_break(self, session: dt.date) -> bool: ...

    @abstractmethod
    def session_break_start(self, session: dt.date) -> dt.datetime | None: ...

    @abstractmethod
    def session_break_end(self, session: dt.date) -> dt.datetime | None: ...

    @abstractmethod
    def is_early_close(self, session: dt.date) -> bool: ...

    @abstractmethod
    def is_normal_business_weekday(self, session: dt.date) -> bool: ...

    @abstractmethod
    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]: ...


class ExchangeCalendarsSchedule(MarketSchedule):
    def __init__(self, mic: str, timezone: str) -> None:
        self._calendar = xc.get_calendar(mic)
        self._early_close_dates = {value.date() for value in self._calendar.early_closes}
        self.tz = ZoneInfo(timezone)

    def _local(self, value: Any) -> dt.datetime:
        return value.to_pydatetime().astimezone(self.tz)

    def is_session(self, session: dt.date) -> bool:
        return bool(self._calendar.is_session(session))

    def next_session(self, after: dt.date) -> dt.date:
        return self._calendar.date_to_session(after, direction="next").date()

    def session_open(self, session: dt.date) -> dt.datetime:
        return self._local(self._calendar.session_open(session))

    def session_close(self, session: dt.date) -> dt.datetime:
        return self._local(self._calendar.session_close(session))

    def has_break(self, session: dt.date) -> bool:
        return bool(self._calendar.session_has_break(session))

    def session_break_start(self, session: dt.date) -> dt.datetime | None:
        if not self.has_break(session):
            return None
        return self._local(self._calendar.session_break_start(session))

    def session_break_end(self, session: dt.date) -> dt.datetime | None:
        if not self.has_break(session):
            return None
        return self._local(self._calendar.session_break_end(session))

    def is_early_close(self, session: dt.date) -> bool:
        return session in self._early_close_dates

    def is_normal_business_weekday(self, session: dt.date) -> bool:
        return self._calendar.weekmask[session.weekday()] == "1"

    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]:
        return [value.date() for value in self._calendar.sessions_in_range(start, end)]


class SyntheticSchedule(MarketSchedule):
    def __init__(self, cfg: SyntheticCalendarConfig, timezone: str) -> None:
        self.tz = ZoneInfo(timezone)
        self._open = self._parse(cfg.open_time)
        self._close = self._parse(cfg.close_time)
        self._lunch_start = self._parse(cfg.lunch_start) if cfg.lunch_start else None
        self._lunch_end = self._parse(cfg.lunch_end) if cfg.lunch_end else None
        self._trading_days = set(cfg.trading_days)
        self._holidays = {dt.date.fromisoformat(day) for day in cfg.holidays}

    @staticmethod
    def _parse(value: str) -> tuple[int, int]:
        hour, minute = map(int, value.split(":"))
        return hour, minute

    def _at(self, day: dt.date, value: tuple[int, int]) -> dt.datetime:
        return dt.datetime(day.year, day.month, day.day, value[0], value[1], tzinfo=self.tz)

    def is_session(self, session: dt.date) -> bool:
        return session.weekday() in self._trading_days and session not in self._holidays

    def next_session(self, after: dt.date) -> dt.date:
        for offset in range(3661):
            candidate = after + dt.timedelta(days=offset)
            if self.is_session(candidate):
                return candidate
        raise RuntimeError(f"no synthetic session found within 10 years after {after}")

    def session_open(self, session: dt.date) -> dt.datetime:
        return self._at(session, self._open)

    def session_close(self, session: dt.date) -> dt.datetime:
        return self._at(session, self._close)

    def has_break(self, session: dt.date) -> bool:
        return self._lunch_start is not None

    def session_break_start(self, session: dt.date) -> dt.datetime | None:
        return self._at(session, self._lunch_start) if self._lunch_start else None

    def session_break_end(self, session: dt.date) -> dt.datetime | None:
        return self._at(session, self._lunch_end) if self._lunch_end else None

    def is_early_close(self, session: dt.date) -> bool:
        return False

    def is_normal_business_weekday(self, session: dt.date) -> bool:
        return session.weekday() in self._trading_days

    def sessions_in_range(self, start: dt.date, end: dt.date) -> list[dt.date]:
        result: list[dt.date] = []
        current = start
        while current <= end:
            if self.is_session(current):
                result.append(current)
            current += dt.timedelta(days=1)
        return result


def build_schedule(exchange: ExchangeConfig) -> MarketSchedule:
    if exchange.calendar_type == "exchange_calendars":
        return ExchangeCalendarsSchedule(exchange.mic, exchange.timezone)
    assert exchange.synthetic is not None
    return SyntheticSchedule(exchange.synthetic, exchange.timezone)
