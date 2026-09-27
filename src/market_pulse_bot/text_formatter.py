"""Backend-neutral message text plus protocol-specific packaging."""
from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass
from typing import Any, Literal

from .config import Backend, ExchangeConfig
from .i18n import I18n
from .market_engine import INCIDENT_PHASES, Phase, PhaseState, TransitionKind, UpcomingEvent

DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_FIELD_NAME_LIMIT = 256
DISCORD_FIELD_VALUE_LIMIT = 1024
DISCORD_FOOTER_LIMIT = 2048
DISCORD_TOTAL_LIMIT = 6000
DISCORD_MAX_FIELDS = 25
SLACK_BLOCK_TEXT_LIMIT = 3000
SLACK_FALLBACK_TEXT_LIMIT = 4000
TELEGRAM_MESSAGE_LIMIT = 4096

MessageKind = Literal["legend", "alerts", "dashboard_amer_eu", "dashboard_asia_oc"]

# Text is built once with these neutral bold markers; _markup() turns them
# into each backend's own bold syntax.
BOLD_OPEN = "\x02"
BOLD_CLOSE = "\x03"


class PayloadTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MessagePayload:
    kind: MessageKind
    plain_text: str
    slack_blocks: list[dict[str, Any]]
    discord_embed: dict[str, Any] | None


PHASE_EMOJI = {
    Phase.CLOSED: "⚫️",
    Phase.EXTENDED_HOURS: "🟣",
    Phase.AUCTION: "🔵",
    Phase.REGULAR: "🟢",
    Phase.LUNCH: "🔘",
    Phase.HOLIDAY: "⚪️",
    Phase.REGULATORY_HALT: "🔶",
    Phase.TECHNICAL_HALT: "♦️",
    Phase.EXCEPTIONAL_CLOSURE: "🚨",
    Phase.POST_HALT_REOPENING: "🔷",
}

# Exchange states that also get listed in the alerts message.
ALERT_PHASES = INCIDENT_PHASES | {Phase.POST_HALT_REOPENING}

EARLY_CLOSE_EMOJI = "🌗"
TRANSITION_EMOJI = "🔜"
NEXT_CHANGE_ARROW = "→"

TRANSITION_KEYS = {
    TransitionKind.TO_PRE_MARKET: "transition.to_pre_market",
    TransitionKind.TO_OPENING_AUCTION: "transition.to_opening_auction",
    TransitionKind.TO_REGULAR_AFTER_AUCTION: "transition.to_regular_after_auction",
    TransitionKind.TO_LUNCH: "transition.to_lunch",
    TransitionKind.TO_REGULAR_AFTER_LUNCH: "transition.to_regular_after_lunch",
    TransitionKind.TO_CLOSING_AUCTION: "transition.to_closing_auction",
    TransitionKind.TO_POST_MARKET: "transition.to_post_market",
    TransitionKind.TO_CLOSED: "transition.to_closed",
    TransitionKind.TO_REGULAR_DIRECT: "transition.to_regular_direct",
    TransitionKind.TO_POST_HALT_REOPENING: "transition.to_post_halt_reopening",
    TransitionKind.TO_REGULAR_UNCERTAIN: "transition.to_regular_uncertain",
}

LegendSection = Literal["session", "incident", "annotation"]
LEGEND_SECTIONS: tuple[LegendSection, ...] = ("session", "incident", "annotation")

# One row per visible badge. Variant-specific dashboard states share their
# badge in the legend, while the dashboard itself keeps their exact labels.
LEGEND_ROWS: tuple[tuple[LegendSection, str, str, str], ...] = (
    ("session", PHASE_EMOJI[Phase.REGULAR], "phase.regular", "legend.regular_desc"),
    ("session", PHASE_EMOJI[Phase.AUCTION], "phase.auction", "legend.auction_desc"),
    ("session", PHASE_EMOJI[Phase.EXTENDED_HOURS], "phase.extended_hours", "legend.extended_hours_desc"),
    ("session", PHASE_EMOJI[Phase.LUNCH], "phase.lunch", "legend.lunch_desc"),
    ("session", PHASE_EMOJI[Phase.CLOSED], "phase.closed", "legend.closed_desc"),
    ("session", PHASE_EMOJI[Phase.HOLIDAY], "phase.holiday", "legend.holiday_desc"),
    ("incident", PHASE_EMOJI[Phase.REGULATORY_HALT], "phase.regulatory_halt", "legend.regulatory_halt_desc"),
    ("incident", PHASE_EMOJI[Phase.TECHNICAL_HALT], "phase.technical_halt", "legend.technical_halt_desc"),
    ("incident", PHASE_EMOJI[Phase.EXCEPTIONAL_CLOSURE], "phase.exceptional_closure", "legend.exceptional_closure_desc"),
    ("incident", PHASE_EMOJI[Phase.POST_HALT_REOPENING], "phase.post_halt_reopening", "legend.post_halt_reopening_desc"),
    ("annotation", EARLY_CLOSE_EMOJI, "legend.early_close_label", "legend.early_close_explainer"),
    ("annotation", TRANSITION_EMOJI, "legend.transition_label", "legend.arrow_explainer"),
    ("annotation", NEXT_CHANGE_ARROW, "legend.next_change_label", "legend.next_change_explainer"),
)

LEGEND_SECTION_KEYS: dict[LegendSection, str] = {
    "session": "legend.section_session",
    "incident": "legend.section_incident",
    "annotation": "legend.section_annotation",
}

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _bold(text: str) -> str:
    return f"{BOLD_OPEN}{text}{BOLD_CLOSE}"


def _markup(text: str, backend: Backend) -> str:
    if backend == "discord":
        return text.replace(BOLD_OPEN, "**").replace(BOLD_CLOSE, "**")
    if backend == "slack":
        return text.replace(BOLD_OPEN, "*").replace(BOLD_CLOSE, "*")
    # Telegram messages are sent with parse_mode=HTML.
    return html.escape(text, quote=False).replace(BOLD_OPEN, "<b>").replace(BOLD_CLOSE, "</b>")


def _time(value: dt.datetime) -> str:
    return value.strftime("%H:%M")


def _date(value: dt.date, i18n: I18n) -> str:
    return f"{i18n.t(f'weekday.{_WEEKDAYS[value.weekday()]}')}, {value.isoformat()}"


def _when(target: dt.datetime, now: dt.datetime, display_tz: dt.tzinfo, i18n: I18n) -> str:
    """When `target` happens, in the reader's time zone: "17:30" today,
    "mar 01:00" within the week, "jue 08/10 03:30" further out."""
    local = target.astimezone(display_tz)
    days = (local.date() - now.astimezone(display_tz).date()).days
    if days <= 0:
        return _time(local)
    weekday = i18n.t(f"weekday.{_WEEKDAYS[local.weekday()]}")
    if days < 7:
        return f"{weekday} {_time(local)}"
    return f"{weekday} {local:%d/%m} {_time(local)}"


def _phase_label_key(state: PhaseState) -> str:
    if state.current_phase == Phase.AUCTION:
        return "phase.opening_auction" if state.phase_variant == "opening_auction" else "phase.closing_auction"
    if state.current_phase == Phase.EXTENDED_HOURS:
        return "phase.pre_market" if state.phase_variant == "pre_market" else "phase.post_market"
    return f"phase.{state.current_phase.value}"


def render_exchange_line(
    exchange: ExchangeConfig,
    state: PhaseState,
    i18n: I18n,
    display_tz: dt.tzinfo,
) -> str:
    early = f"{EARLY_CLOSE_EMOJI} " if state.is_early_close_day and state.current_phase != Phase.HOLIDAY else ""
    current = PHASE_EMOJI[state.current_phase]
    if state.show_transition and state.next_phase and state.transition_kind:
        target = PHASE_EMOJI[state.next_phase]
        symbol = f"{current}{TRANSITION_EMOJI}{target}"
        key = TRANSITION_KEYS[state.transition_kind]
        if state.uncertain_transition:
            phrase = i18n.t(key)
        else:
            assert state.transition_time is not None and state.minutes_until is not None
            phrase = i18n.t(
                key,
                minutes=state.minutes_until,
                time=_time(state.transition_time.astimezone(display_tz)),
            )
    else:
        symbol = current
        phrase = i18n.t(_phase_label_key(state))
        if state.next_phase is not None and state.transition_time is not None:
            when = _when(state.transition_time, state.native_now, display_tz, i18n)
            phrase += f" {NEXT_CHANGE_ARROW} {PHASE_EMOJI[state.next_phase]} {when}"
    return f"{early}{symbol} {exchange.country_flag} {exchange.name} ({_bold(exchange.currency)}) — {phrase}"


def render_event_line(event: UpcomingEvent, display_tz: dt.tzinfo, i18n: I18n) -> str:
    if event.event_type == "holiday":
        return i18n.t(
            "events.holiday_line",
            flag=event.country_flag,
            name=event.exchange_name,
            date=_date(event.date, i18n),
        )
    assert event.early_close_time is not None
    display_time = event.early_close_time.astimezone(display_tz)
    return i18n.t(
        "events.early_close_line",
        flag=event.country_flag,
        name=event.exchange_name,
        date=_date(event.date, i18n),
        time=_time(display_time),
    )


def render_legend_line(badge: str, label_key: str, description_key: str, i18n: I18n) -> str:
    return f"{badge} {i18n.t(label_key)} — {i18n.t(description_key)}"


def legend_lines_by_section(i18n: I18n) -> dict[LegendSection, list[str]]:
    sections: dict[LegendSection, list[str]] = {section: [] for section in LEGEND_SECTIONS}
    for section, badge, label_key, description_key in LEGEND_ROWS:
        sections[section].append(render_legend_line(badge, label_key, description_key, i18n))
    return sections


def _footer(now_utc: dt.datetime, display_tz: dt.tzinfo, i18n: I18n) -> str:
    return i18n.t(
        "footer.generated_at",
        time=now_utc.astimezone(display_tz).strftime("%Y-%m-%d %H:%M"),
    )


def _slack_blocks(text: str) -> list[dict[str, Any]]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for raw_line in text.split("\n"):
        line_parts = [
            raw_line[index:index + SLACK_BLOCK_TEXT_LIMIT]
            for index in range(0, len(raw_line) or 1, SLACK_BLOCK_TEXT_LIMIT)
        ]
        for line in line_parts:
            projected = length + len(line) + (1 if current else 0)
            if current and projected > SLACK_BLOCK_TEXT_LIMIT:
                chunks.append("\n".join(current))
                current = []
                length = 0
            current.append(line)
            length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]


def _discord_total(embed: dict[str, Any]) -> int:
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    for field in embed.get("fields", []) or []:
        total += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    footer = embed.get("footer") or {}
    total += len(str(footer.get("text", "")))
    return total


def _split_field(label: str, lines: list[str]) -> list[tuple[str, str]]:
    if not lines:
        return [(label, "—")]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if len(line) > DISCORD_FIELD_VALUE_LIMIT:
            raise PayloadTooLargeError(f"line exceeds {DISCORD_FIELD_VALUE_LIMIT} characters")
        projected = length + len(line) + (1 if current else 0)
        if current and projected > DISCORD_FIELD_VALUE_LIMIT:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    total = len(chunks)
    return [(label, chunks[0])] if total == 1 else [
        (f"{label} ({index}/{total})", chunk)
        for index, chunk in enumerate(chunks, start=1)
    ]


def _validate_discord_embed(embed: dict[str, Any]) -> None:
    if len(str(embed.get("title", ""))) > DISCORD_TITLE_LIMIT:
        raise PayloadTooLargeError("Discord embed title exceeds 256 characters")
    if len(str(embed.get("description", ""))) > DISCORD_DESCRIPTION_LIMIT:
        raise PayloadTooLargeError("Discord embed description exceeds 4096 characters")
    footer = embed.get("footer") or {}
    if len(str(footer.get("text", ""))) > DISCORD_FOOTER_LIMIT:
        raise PayloadTooLargeError("Discord embed footer exceeds 2048 characters")
    fields = embed.get("fields", []) or []
    if len(fields) > DISCORD_MAX_FIELDS:
        raise PayloadTooLargeError("Discord embed exceeds 25 fields")
    for field in fields:
        if len(str(field.get("name", ""))) > DISCORD_FIELD_NAME_LIMIT:
            raise PayloadTooLargeError("Discord field name exceeds 256 characters")
        if len(str(field.get("value", ""))) > DISCORD_FIELD_VALUE_LIMIT:
            raise PayloadTooLargeError("Discord field value exceeds 1024 characters")
    if _discord_total(embed) > DISCORD_TOTAL_LIMIT:
        raise PayloadTooLargeError("Discord embed exceeds 6000 characters")


def _validate_text_limits(text: str, backend: Backend, kind: str) -> None:
    if backend == "telegram" and len(text) > TELEGRAM_MESSAGE_LIMIT:
        raise PayloadTooLargeError(f"{kind} exceeds Telegram's 4096 character limit")
    if backend == "slack" and len(text) > SLACK_FALLBACK_TEXT_LIMIT:
        raise PayloadTooLargeError(f"{kind} exceeds conservative Slack text budget")


def _discord_fields(label: str, lines: list[str]) -> list[dict[str, object]]:
    return [
        {"name": name, "value": value, "inline": False}
        for name, value in _split_field(label, [_markup(line, "discord") for line in lines])
    ]


def _package(
    kind: MessageKind,
    text: str,
    backend: Backend,
    embed: dict[str, Any] | None,
) -> MessagePayload:
    plain = _markup(text, backend)
    _validate_text_limits(plain, backend, kind)
    if embed is not None:
        _validate_discord_embed(embed)
    return MessagePayload(kind, plain, _slack_blocks(plain) if backend == "slack" else [], embed)


def build_legend_payload(i18n: I18n, backend: Backend) -> MessagePayload:
    """Message 1: what every badge means. Its text never changes, so
    sync_messages() only edits it again when the wording does."""
    title = i18n.t("legend.title")
    intro = i18n.t("legend.intro")
    lines = legend_lines_by_section(i18n)
    sections = [section for section in LEGEND_SECTIONS if lines[section]]
    text = f"{title}\n\n{intro}\n\n" + "\n\n".join(
        f"— {i18n.t(LEGEND_SECTION_KEYS[section])} —\n" + "\n".join(lines[section])
        for section in sections
    )
    embed: dict[str, Any] | None = None
    if backend == "discord":
        fields = [
            field
            for section in sections
            for field in _discord_fields(i18n.t(LEGEND_SECTION_KEYS[section]), lines[section])
        ]
        embed = {"title": title, "description": intro, "fields": fields}
    return _package("legend", text, backend, embed)


def build_alerts_payload(
    incident_lines: list[str],
    events: list[UpcomingEvent],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> MessagePayload:
    """Message 2: exchange-wide halts right now, then holidays and half days ahead."""
    title = i18n.t("header.alerts_title")
    footer = _footer(now_utc, display_tz, i18n)
    incidents_label = i18n.t("alerts.incidents_section")
    events_label = i18n.t("alerts.events_section")
    incidents = incident_lines or [i18n.t("alerts.no_incidents")]
    upcoming = [render_event_line(event, display_tz, i18n) for event in events] or [i18n.t("events.no_events")]
    text = (
        f"{title}\n\n— {incidents_label} —\n" + "\n".join(incidents)
        + f"\n\n— {events_label} —\n" + "\n".join(upcoming)
        + f"\n\n{footer}"
    )
    embed: dict[str, Any] | None = None
    if backend == "discord":
        fields = _discord_fields(incidents_label, incidents) + _discord_fields(events_label, upcoming)
        embed = {"title": title, "fields": fields, "footer": {"text": footer}}
    return _package("alerts", text, backend, embed)


def build_dashboard_payload(
    kind: MessageKind,
    title_key: str,
    regions: tuple[str, str],
    lines_by_region: dict[str, list[str]],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> MessagePayload:
    title = i18n.t(title_key)
    footer = _footer(now_utc, display_tz, i18n)
    labels = {region: i18n.t(f"region.{region.lower()}") for region in regions}
    sections = [
        f"— {labels[region]} —\n" + ("\n".join(lines_by_region.get(region, [])) or "—")
        for region in regions
    ]
    text = f"{title}\n\n" + "\n\n".join(sections) + f"\n\n{footer}"
    embed: dict[str, Any] | None = None
    if backend == "discord":
        fields = [
            field
            for region in regions
            for field in _discord_fields(labels[region], lines_by_region.get(region, []))
        ]
        embed = {"title": title, "fields": fields, "footer": {"text": footer}}
    return _package(kind, text, backend, embed)


def build_all_payloads(
    exchanges: list[ExchangeConfig],
    phase_states: dict[str, PhaseState],
    upcoming_events: list[UpcomingEvent],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> tuple[MessagePayload, MessagePayload, MessagePayload, MessagePayload]:
    """The four messages in channel order: legend, alerts, then the two dashboards."""
    lines: dict[str, list[str]] = {region: [] for region in ("America", "Europe", "Asia", "Oceania")}
    incident_lines: list[str] = []
    for exchange in exchanges:
        state = phase_states[exchange.mic]
        line = render_exchange_line(exchange, state, i18n, display_tz)
        lines[exchange.region].append(line)
        if state.current_phase in ALERT_PHASES:
            incident_lines.append(line)
    return (
        build_legend_payload(i18n, backend),
        build_alerts_payload(incident_lines, upcoming_events, now_utc, display_tz, i18n, backend),
        build_dashboard_payload("dashboard_amer_eu", "header.dashboard_americas_eu_title", ("America", "Europe"), lines, now_utc, display_tz, i18n, backend),
        build_dashboard_payload("dashboard_asia_oc", "header.dashboard_asia_oceania_title", ("Asia", "Oceania"), lines, now_utc, display_tz, i18n, backend),
    )
