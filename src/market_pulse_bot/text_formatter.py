"""Backend-neutral message text plus protocol-specific packaging."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from .config import Backend, ExchangeConfig
from .i18n import I18n
from .market_engine import Phase, PhaseState, TransitionKind, UpcomingEvent

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

MessageKind = Literal["events", "dashboard_amer_eu", "dashboard_asia_oc"]


class PayloadTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MessagePayload:
    kind: MessageKind
    plain_text: str
    slack_blocks: list[dict[str, object]]
    discord_embed: dict[str, object] | None


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
EARLY_CLOSE_EMOJI = "🌗"

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

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _time(value: dt.datetime) -> str:
    return value.strftime("%H:%M")


def _date(value: dt.date, i18n: I18n) -> str:
    return f"{i18n.t(f'weekday.{_WEEKDAYS[value.weekday()]}')}, {value.isoformat()}"


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
        symbol = f"{current}🔜{target}"
        key = TRANSITION_KEYS[state.transition_kind]
        if state.uncertain_transition:
            phrase = i18n.t(key)
        else:
            assert state.transition_time is not None and state.minutes_until is not None
            display_time = state.transition_time.astimezone(display_tz)
            phrase = i18n.t(
                key,
                minutes=state.minutes_until,
                time=_time(display_time),
                native_time=_time(state.transition_time),
                tz_label=exchange.tz_label,
            )
    else:
        symbol = current
        phrase = i18n.t(_phase_label_key(state))
    return (
        f"{early}{symbol} {exchange.country_flag} {exchange.name} ({exchange.currency})"
        f" — {phrase} ({_time(state.native_now)} {exchange.tz_label})"
    )


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
        native_time=_time(event.early_close_time),
        tz_label=event.tz_label,
    )


def _footer(now_utc: dt.datetime, display_tz: dt.tzinfo, i18n: I18n) -> str:
    return i18n.t(
        "footer.generated_at",
        time=now_utc.astimezone(display_tz).strftime("%Y-%m-%d %H:%M"),
    )


def _slack_blocks(text: str) -> list[dict[str, object]]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
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


def _discord_total(embed: dict[str, object]) -> int:
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


def _validate_discord_embed(embed: dict[str, object]) -> None:
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


def build_events_payload(events, now_utc, display_tz, i18n, backend) -> MessagePayload:
    title = i18n.t("header.events_title")
    lines = [render_event_line(event, display_tz, i18n) for event in events]
    body = "\n".join(lines) if lines else i18n.t("events.no_events")
    footer = _footer(now_utc, display_tz, i18n)
    plain = f"{title}\n\n{body}\n\n{footer}"
    _validate_text_limits(plain, backend, "events")
    embed = None
    if backend == "discord":
        fields: list[dict[str, object]] = []
        for day in sorted({event.date for event in events}):
            day_lines = [render_event_line(event, display_tz, i18n) for event in events if event.date == day]
            fields.extend(
                {"name": name, "value": value, "inline": False}
                for name, value in _split_field(_date(day, i18n), day_lines)
            )
        if not fields:
            fields = [{"name": i18n.t("events.no_events"), "value": "—", "inline": False}]
        embed = {"title": title, "fields": fields, "footer": {"text": footer}}
        _validate_discord_embed(embed)
    return MessagePayload("events", plain, _slack_blocks(plain) if backend == "slack" else [], embed)


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
    plain = f"{title}\n\n" + "\n\n".join(sections) + f"\n\n{footer}"
    _validate_text_limits(plain, backend, kind)

    embed = None
    if backend == "discord":
        fields: list[dict[str, object]] = []
        for region in regions:
            fields.extend(
                {"name": name, "value": value, "inline": False}
                for name, value in _split_field(labels[region], lines_by_region.get(region, []))
            )
        embed = {"title": title, "fields": fields, "footer": {"text": footer}}
        _validate_discord_embed(embed)

    return MessagePayload(kind, plain, _slack_blocks(plain) if backend == "slack" else [], embed)


def build_all_payloads(
    exchanges,
    phase_states,
    upcoming_events,
    now_utc,
    display_tz,
    i18n,
    backend,
):
    lines = {region: [] for region in ("America", "Europe", "Asia", "Oceania")}
    for exchange in exchanges:
        lines[exchange.region].append(render_exchange_line(exchange, phase_states[exchange.mic], i18n, display_tz))
    return (
        build_events_payload(upcoming_events, now_utc, display_tz, i18n, backend),
        build_dashboard_payload("dashboard_amer_eu", "header.dashboard_americas_eu_title", ("America", "Europe"), lines, now_utc, display_tz, i18n, backend),
        build_dashboard_payload("dashboard_asia_oc", "header.dashboard_asia_oceania_title", ("Asia", "Oceania"), lines, now_utc, display_tz, i18n, backend),
    )
