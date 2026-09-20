"""Next-events dashboard: holidays + early closes within 5 business days, per locale."""

import datetime as dt

from .calendars import CalendarEngine
from .config import ExchangeConfig
from .events import upcoming_events
from .i18n import translate

EVENTS_HORIZON_BUSINESS_DAYS = 5


class DashboardRenderer:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def render(self, cfg: ExchangeConfig, engine: CalendarEngine, today: dt.date) -> str:
        events = upcoming_events(cfg, engine, today, EVENTS_HORIZON_BUSINESS_DAYS)
        lines: list[str] = [translate(self.lang, "msg.events_title")]
        if not events:
            lines.append(translate(self.lang, "msg.no_events"))
            return "\n".join(lines)

        holidays = [e for e in events if e["type"] == "holiday"]
        early = [e for e in events if e["type"] == "early_close"]

        if holidays:
            lines.append(f"__{translate(self.lang, 'msg.holidays_sub')}__")
            for e in holidays:
                lines.append(translate(self.lang, "msg.line_holiday", name=cfg.name, date=e["date"]))
        if early:
            lines.append(f"__{translate(self.lang, 'msg.early_closes_sub')}__")
            for e in early:
                lines.append(translate(
                    self.lang, "msg.line_early_close",
                    name=cfg.name, date=e["date"], time=e["time"], tz_label=cfg.tz_label,
                ))
        return "\n".join(lines)
