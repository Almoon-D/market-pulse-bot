"""Upcoming events aggregation for the dashboard (next N business days per exchange)."""

import datetime as dt

from .calendars import CalendarEngine
from .config import AppSettings
from .state import ExchangeState


def collect_upcoming_events(settings: AppSettings, engines: dict,
                            states: dict[str, ExchangeState],
                            now: dt.datetime, business_days: int) -> list[tuple]:
    events: list[tuple] = []
    for cfg in settings.exchanges:
        engine: CalendarEngine = engines[cfg.mic]
        state = states[cfg.mic]
        nxt = state.next_transition
        if nxt is None or nxt <= now:
            continue
        minutes = int((nxt - now).total_seconds() // 60)
        if minutes > business_days * 24 * 60:
            continue
        label = _label_for(state.current_phase)
        events.append((cfg.country_flag, cfg.name, label, nxt, minutes))
    events.sort(key=lambda e: e[3])  # by when
    return events


def _label_for(phase) -> str:
    from .state import Phase
    mapping = {
        Phase.PRE_MARKET: "phase.pre_market",
        Phase.OPENING_AUCTION: "phase.opening_auction",
        Phase.REGULAR: "phase.regular",
        Phase.LUNCH: "phase.lunch",
        Phase.CLOSING_AUCTION: "phase.closing_auction",
        Phase.POST_MARKET: "phase.post_market",
        Phase.CLOSED: "phase.closed",
        Phase.HOLIDAY: "phase.holiday",
    }
    return mapping.get(phase, "msg.next_session")
