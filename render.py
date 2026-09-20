"""Message formatting: phase-change lines, incident alerts, dashboards."""

import datetime as dt

from .i18n import translate
from .state import Phase

FLAG_PAD = "\u2003"  # em-space separator after the flag emoji


def _fmt_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{rem:02d}" if rem else f"{hours}h"
    days, rem_h = divmod(hours, 24)
    return f"{days}d{rem_h}h" if rem_h else f"{days}d"


def render_phase_change(cfg, old: Phase, new: Phase,
                        next_dt: dt.datetime | None, now: dt.datetime,
                        language: str) -> str:
    line = f"{cfg.country_flag}{FLAG_PAD}**{cfg.name}** ({cfg.mic}) — {translate(language, f'phase.{new.value}')}"
    if new in (Phase.HALT_REGULATORY, Phase.HALT_TECHNICAL, Phase.EXCEPTIONAL):
        line += f" · {translate(language, 'reopening_unknown')}"
    elif next_dt is not None:
        minutes = int((next_dt - now).total_seconds() // 60)
        if minutes >= 0:
            line += f" · {translate(language, 'label.next')}: {_fmt_minutes(minutes)}"
    return line


def render_dashboard(events: list[tuple], language: str) -> str:
    """events: list of (flag, name, label_key, when_dt, minutes_until)."""
    if not events:
        return translate(language, "msg.no_events")
    lines = [f"📅 **{translate(language, 'label.next')}**"]
    for flag, name, label_key, _when, minutes in events:
        lines.append(f"{flag}{FLAG_PAD}{name} — {translate(language, label_key)} ({_fmt_minutes(minutes)})")
    return "\n".join(lines)
