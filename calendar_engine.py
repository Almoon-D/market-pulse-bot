from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from .config import ExchangeConfig


@dataclass(frozen=True)
class SessionInfo:
    trading_date: date
    open_at: datetime
    close_at: datetime
    is_open: bool
    early_close: bool
    holiday: bool


class MarketSchedule:
    """Calendar abstraction shared by exchange-backed and synthetic schedules."""

    def __init__(self, config: ExchangeConfig) -> None:
        self.config = config
        self.zone = ZoneInfo(config.timezone)
        if config.calendar_type == "exchange_calendars":
            self._calendar = xcals.get_calendar(config.mic)
        else:
            self._calendar = None

    def session_for(self, trading_date: date) -> SessionInfo:
        if self.config.calendar_type == "exchange_calendars":
            return self._exchange_session(trading_date)
        return self._synthetic_session(trading_date)

    def forecast(self, today: date, business_days: int = 5) -> list[SessionInfo]:
        sessions: list[SessionInfo] = []
        cursor = today
        while len(sessions) < business_days:
            if cursor.weekday() < 5:
                sessions.append(self.session_for(cursor))
            cursor += timedelta(days=1)
        return sessions

    def _exchange_session(self, trading_date: date) -> SessionInfo:
        calendar = self._calendar
        if calendar is None:
            raise RuntimeError(f"calendar not initialized for {self.config.mic}")
        session_label = trading_date.isoformat()
        valid_sessions = calendar.sessions_in_range(session_label, session_label)
        if len(valid_sessions) == 0:
            return SessionInfo(trading_date, datetime.combine(trading_date, time.min, self.zone), datetime.combine(trading_date, time.min, self.zone), False, False, True)
        label = valid_sessions[0]
        open_utc = calendar.session_open(label)
        close_utc = calendar.session_close(label)
        open_local = open_utc.to_pydatetime().astimezone(self.zone)
        close_local = close_utc.to_pydatetime().astimezone(self.zone)
        return SessionInfo(
            trading_date=trading_date,
            open_at=open_local,
            close_at=close_local,
            is_open=True,
            early_close=label in calendar.early_closes,
            holiday=False,
        )

    def _synthetic_session(self, trading_date: date) -> SessionInfo:
        schedule = self.config.synthetic_schedule
        if schedule is None:
            raise ValueError(f"synthetic exchange {self.config.mic} requires synthetic_schedule")
        day_name = trading_date.strftime("%a").lower()
        if day_name not in {item.lower()[:3] for item in schedule.trading_days}:
            midnight = datetime.combine(trading_date, time.min, self.zone)
            return SessionInfo(trading_date, midnight, midnight, False, False, True)
        open_time = time.fromisoformat(schedule.open)
        close_time = time.fromisoformat(schedule.close)
        return SessionInfo(
            trading_date=trading_date,
            open_at=datetime.combine(trading_date, open_time, self.zone),
            close_at=datetime.combine(trading_date, close_time, self.zone),
            is_open=True,
            early_close=False,
            holiday=False,
        )


def build_schedules(exchanges: list[ExchangeConfig]) -> dict[str, MarketSchedule]:
    return {exchange.mic: MarketSchedule(exchange) for exchange in exchanges}
