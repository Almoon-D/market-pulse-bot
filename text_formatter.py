import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .calendar_engine import MarketSchedule
from .config import AppConfig, ExchangeConfig, NotificationThresholds
from .halt_detector import Incident
from .i18n import Translator
from .market_engine import Phase, PhaseState, determine_phase

BACKEND_LIMITS = {"discord": 6000, "slack": 4000, "telegram": 4096}
DISCORD_FIELD_LIMIT = 1024
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessagePayload:
    key: str
    text: str
    title: str
    fields: tuple[tuple[str, str], ...] = ()


def build_payloads(config: AppConfig, schedules: dict[str, MarketSchedule], translator: Translator, now: datetime, incidents: dict[str, Incident]) -> tuple[MessagePayload, MessagePayload, MessagePayload]:
    localized_now = now.astimezone(ZoneInfo(config.display_timezone))
    events = _events_payload(config, schedules, translator, localized_now)
    americas_europe = _dashboard_payload(config, schedules, translator, localized_now, incidents, {"America", "Europe"}, "dashboard_americas_eu")
    asia_oceania = _dashboard_payload(config, schedules, translator, localized_now, incidents, {"Asia", "Oceania"}, "dashboard_asia_oceania")
    return events, americas_europe, asia_oceania


def _events_payload(config: AppConfig, schedules: dict[str, MarketSchedule], t: Translator, now: datetime) -> MessagePayload:
    lines = [t.get("events_title"), t.get("generated_at", time=_display_time(now, config.display_timezone))]
    for exchange in config.exchanges:
        schedule = schedules[exchange.mic]
        for session in schedule.forecast(now.date(), 5):
            if not session.is_open:
                lines.append(t.get("forecast_closed", flag=exchange.country_flag, name=exchange.name, date=session.trading_date.strftime("%d/%m/%Y")))
            elif session.early_close:
                lines.append(t.get("forecast_early_close", flag=exchange.country_flag, name=exchange.name, date=session.trading_date.strftime("%d/%m/%Y"), time=session.close_at.strftime("%H:%M"), tz_label=exchange.tz_label))
    text = "\n".join(lines)
    payload = MessagePayload("events_message_ref", text, t.get("events_title"), tuple(_split_field(lines[1:], DISCORD_FIELD_LIMIT)))
    _validate_payload(payload, config.notification_backend)
    return payload


def _dashboard_payload(config: AppConfig, schedules: dict[str, MarketSchedule], t: Translator, now: datetime, incidents: dict[str, Incident], regions: set[str], key: str) -> MessagePayload:
    groups: dict[str, list[str]] = {region: [] for region in sorted(regions)}
    for exchange in config.exchanges:
        if exchange.region not in regions:
            continue
        state = determine_phase(exchange, schedules[exchange.mic], now)
        incident = incidents.get(exchange.mic)
        line = _render_exchange(exchange, state, incident, t, now, config.display_timezone, config.notification_thresholds)
        groups[exchange.region].append(line)

    fields: list[tuple[str, str]] = []
    for region in sorted(groups):
        chunks = _split_field(groups[region], DISCORD_FIELD_LIMIT)
        for index, chunk in enumerate(chunks, 1):
            suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
            fields.append((t.get(f"region_{region}") + suffix, chunk))
    text = "\n".join(f"{title}\n{value}" for title, value in fields)
    payload = MessagePayload(key, text, t.get("dashboard_title", regions=t.get("region_pair", regions=", ".join(sorted(regions)))), tuple(fields))
    _validate_payload(payload, config.notification_backend)
    return payload


def _render_exchange(exchange: ExchangeConfig, state: PhaseState, incident: Incident | None, t: Translator, now: datetime, display_timezone: str, thresholds: NotificationThresholds) -> str:
    if incident is not None:
        emoji = {"regulatory": "🔶", "technical": "🟥", "exceptional": "🚨"}.get(incident.phase, "🟥")
        return f"{emoji} {exchange.country_flag} {exchange.name} ({exchange.currency}) — {incident.title}"

    threshold_by_phase = {
        Phase.OPENING_AUCTION: thresholds.opening_auction,
        Phase.REGULAR: thresholds.regular_after_auction,
        Phase.LUNCH: thresholds.lunch_reopening,
        Phase.CLOSING_AUCTION: thresholds.closing_auction,
        Phase.EXTENDED: thresholds.extended_hours,
        Phase.CLOSED: thresholds.closed,
    }
    if state.target_at is not None and state.minutes_to_target is not None and state.next_phase is not None:
        threshold = threshold_by_phase.get(state.next_phase, thresholds.closed)
        if state.minutes_to_target > threshold:
            phase_label = t.get(f"phase_{state.phase}")
            return f"{_phase_emoji(state.phase)} {exchange.country_flag} {exchange.name} ({exchange.currency}) — {t.get('phase_now', phase=phase_label)}"

    emoji = _phase_emoji(state.phase)
    if state.target_at is None:
        return f"{emoji} {exchange.country_flag} {exchange.name} ({exchange.currency}) — {t.get('phase_now', phase=t.get(f'phase_{state.phase}'))}"
    native = state.target_at.astimezone(ZoneInfo(exchange.timezone))
    display = state.target_at.astimezone(ZoneInfo(display_timezone))
    minutes = state.minutes_to_target if state.minutes_to_target is not None else 0
    target_label = t.get("target_time", minutes=minutes, time=display.strftime("%H:%M"), tz_label=exchange.tz_label, native_time=native.strftime("%H:%M"), native_tz=exchange.tz_label)
    transition = f"{emoji}🔜{_target_emoji(state.next_phase)}"
    return f"{transition} {exchange.country_flag} {exchange.name} ({exchange.currency}) — {target_label}"


def _phase_emoji(phase: Phase) -> str:
    return {
        Phase.CLOSED: "⚫️",
        Phase.EXTENDED: "🟣",
        Phase.OPENING_AUCTION: "🔵",
        Phase.REGULAR: "🟢",
        Phase.LUNCH: "🔘",
        Phase.CLOSING_AUCTION: "🔵",
        Phase.POST_HALT_REOPENING: "🔷",
    }[phase]

def _target_emoji(phase: Phase | None) -> str:
    return {
        Phase.CLOSED: "⚫️",
        Phase.EXTENDED: "🟣",
        Phase.OPENING_AUCTION: "🔵",
        Phase.REGULAR: "🟢",
        Phase.LUNCH: "🔘",
        Phase.CLOSING_AUCTION: "🔵",
        Phase.POST_HALT_REOPENING: "🔷",
        None: "⚫️",
    }[phase]


def _split_field(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(line) > limit:
                raise ValueError(f"single exchange line exceeds Discord field limit of {limit} characters")
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def _validate_payload(payload: MessagePayload, backend: str) -> None:
    limit = BACKEND_LIMITS[backend]
    if len(payload.text) > limit:
        LOGGER.warning("%s payload is %d characters, exceeding %s limit of %d; notification will not be sent", payload.key, len(payload.text), backend, limit)
        raise ValueError(f"{payload.key} payload exceeds {backend} limit of {limit} characters")


def _display_time(value: datetime, timezone_name: str) -> str:
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
