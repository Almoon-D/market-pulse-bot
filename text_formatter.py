"""
Turns ``PhaseState``/``UpcomingEvent`` objects into the three static text
payloads, pre-validated against whichever backend is active.

All three messages share this one formatting path — Section 5 is explicit
that backends must differ only in HTTP mechanics, never in wording — so
this module has no branch that changes *what* is said, only how the result
is packaged (a Discord embed vs. a Slack block list vs. a plain Telegram
string).
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from src.config import Backend, ExchangeConfig
from src.i18n import I18n
from src.market_engine import Phase, PhaseState, TransitionKind, UpcomingEvent

# Hard backend limits (Section 4 / Section 2). These are protocol facts,
# not deployment knobs, so they live here as named constants rather than
# in exchanges.yaml.
DISCORD_FIELD_LIMIT = 1024
DISCORD_TOTAL_LIMIT = 6000
DISCORD_MAX_FIELDS_PER_EMBED = 25
SLACK_MESSAGE_LIMIT = 4000
SLACK_BLOCK_LIMIT = 3000
TELEGRAM_MESSAGE_LIMIT = 4096

_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

MessageKind = Literal["events", "dashboard_amer_eu", "dashboard_asia_oc"]


class PayloadTooLargeError(RuntimeError):
    """Raised instead of silently truncating or sending a request the
    backend would reject outright (Telegram gives no truncation grace).
    """


@dataclass
class MessagePayload:
    kind: MessageKind
    plain_text: str  # Telegram body; also the Slack "text" fallback/preview
    slack_blocks: list[dict]  # Slack Block Kit body
    discord_embed: dict | None  # only populated when backend == "discord"


def _fmt_time(moment: dt.datetime) -> str:
    return moment.strftime("%H:%M")


def _fmt_date(day: dt.date, i18n: I18n) -> str:
    weekday_label = i18n.t(f"weekday.{_WEEKDAY_KEYS[day.weekday()]}")
    return f"{weekday_label}, {day.isoformat()}"


def _steady_label_key(phase: PhaseState) -> str:
    """Which static ``phase.*`` label applies when there is no arrow to
    render. Two phases (AUCTION, EXTENDED_HOURS) are ambiguous on their
    own — the outgoing ``transition_kind`` (always populated, even when
    ``show_transition`` is False) disambiguates opening/closing and
    pre/post-market.
    """
    p, kind = phase.current_phase, phase.transition_kind
    if p == Phase.AUCTION:
        return "phase.opening_auction" if kind == TransitionKind.TO_REGULAR_AFTER_AUCTION else "phase.closing_auction"
    if p == Phase.EXTENDED_HOURS:
        return "phase.post_market" if kind == TransitionKind.TO_CLOSED else "phase.pre_market"
    return f"phase.{p.value}"


_PHASE_EMOJI: dict[Phase, str] = {
    Phase.CLOSED: "⚫️",
    Phase.EXTENDED_HOURS: "🟣",
    Phase.AUCTION: "🔵",
    Phase.REGULAR: "🟢",
    Phase.LUNCH: "🔘",
    Phase.HOLIDAY: "⚪️",
    Phase.REGULATORY_HALT: "🔶",
    Phase.TECHNICAL_HALT: "🟥",
    Phase.EXCEPTIONAL_CLOSURE: "🚨",
    Phase.POST_HALT_REOPENING: "🔷",
}
_EARLY_CLOSE_EMOJI = "🌗"

_TRANSITION_KEY: dict[TransitionKind, str] = {
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


def render_exchange_line(exchange: ExchangeConfig, phase: PhaseState, i18n: I18n, display_tz: dt.tzinfo) -> str:
    """One line of message 2/3: ``[emoji(s)] flag name (currency) — phrase (native time)``."""
    early_marker = f"{_EARLY_CLOSE_EMOJI} " if phase.is_early_close_day and phase.current_phase != Phase.HOLIDAY else ""
    current_emoji = _PHASE_EMOJI[phase.current_phase]

    if phase.show_transition and phase.next_phase is not None and phase.transition_kind is not None:
        next_emoji = _PHASE_EMOJI[phase.next_phase]
        symbol = f"{current_emoji}🔜{next_emoji}"
        key = _TRANSITION_KEY[phase.transition_kind]
        if phase.transition_kind == TransitionKind.TO_REGULAR_UNCERTAIN:
            phrase = i18n.t(key)
        else:
            assert phase.transition_time is not None and phase.minutes_until is not None
            display_time = phase.transition_time.astimezone(display_tz)
            phrase = i18n.t(
                key,
                minutes=phase.minutes_until,
                time=_fmt_time(display_time),
                native_time=_fmt_time(phase.transition_time),
                tz_label=exchange.tz_label,
            )
    else:
        symbol = current_emoji
        phrase = i18n.t(_steady_label_key(phase))

    native_clock = f" ({_fmt_time(phase.native_now)} {exchange.tz_label})"
    return f"{early_marker}{symbol} {exchange.country_flag} {exchange.name} ({exchange.currency}) — {phrase}{native_clock}"


def render_event_line(event: UpcomingEvent, display_tz: dt.tzinfo, i18n: I18n) -> str:
    if event.event_type == "holiday":
        return i18n.t(
            "events.holiday_line", flag=event.country_flag, name=event.exchange_name, date=_fmt_date(event.date, i18n)
        )
    assert event.early_close_time is not None
    display_time = event.early_close_time.astimezone(display_tz)
    return i18n.t(
        "events.early_close_line",
        flag=event.country_flag,
        name=event.exchange_name,
        date=_fmt_date(event.date, i18n),
        time=_fmt_time(display_time),
        native_time=_fmt_time(event.early_close_time),
        tz_label=event.tz_label,
    )


def _footer(now_utc: dt.datetime, display_tz: dt.tzinfo, i18n: I18n) -> str:
    local = now_utc.astimezone(display_tz)
    return i18n.t("footer.generated_at", time=local.strftime("%Y-%m-%d %H:%M"))


def _slack_blocks_from_text(text: str) -> list[dict]:
    """Chunk ``text`` on line boundaries into Slack section blocks, each
    under ``SLACK_BLOCK_LIMIT``.
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added = len(line) + 1
        if current and current_len + added > SLACK_BLOCK_LIMIT:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += added
    if current:
        chunks.append("\n".join(current))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]


def _split_region_for_discord(label: str, lines: list[str]) -> list[tuple[str, str]]:
    """Discord fields: try one field; split into '(1/2)'/'(2/2)' if the
    joined text would exceed ``DISCORD_FIELD_LIMIT``. Never more than two —
    a region that still doesn't fit in two fields is a genuine config
    problem (far too many exchanges in one region) and should raise, not
    silently drop entries.
    """
    joined = "\n".join(lines)
    if len(joined) <= DISCORD_FIELD_LIMIT:
        return [(label, joined)]

    part1: list[str] = []
    length = 0
    i = 0
    for i, line in enumerate(lines):
        added = len(line) + 1
        if part1 and length + added > DISCORD_FIELD_LIMIT:
            break
        part1.append(line)
        length += added
    else:
        i += 1  # consumed every line into part1 (shouldn't happen given the len check above)

    part2 = lines[len(part1):]
    text1, text2 = "\n".join(part1), "\n".join(part2)
    if len(text1) > DISCORD_FIELD_LIMIT or len(text2) > DISCORD_FIELD_LIMIT:
        raise PayloadTooLargeError(
            f"region {label!r} does not fit in two {DISCORD_FIELD_LIMIT}-char Discord fields "
            f"({len(lines)} exchanges) — split the region or move exchanges to another message"
        )
    return [(f"{label} (1/2)", text1), (f"{label} (2/2)", text2)]


def _discord_embed_char_count(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    for f in embed.get("fields", []):
        total += len(f.get("name", "")) + len(f.get("value", ""))
    footer = embed.get("footer") or {}
    total += len(footer.get("text", ""))
    return total


def build_events_payload(
    events: list[UpcomingEvent],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> MessagePayload:
    title = i18n.t("header.events_title")
    body = "\n".join(render_event_line(e, display_tz, i18n) for e in events) or i18n.t("events.no_events")
    footer = _footer(now_utc, display_tz, i18n)
    plain_text = f"{title}\n\n{body}\n\n{footer}"

    if backend == "telegram" and len(plain_text) > TELEGRAM_MESSAGE_LIMIT:
        raise PayloadTooLargeError(f"events message is {len(plain_text)} chars, Telegram limit is {TELEGRAM_MESSAGE_LIMIT}")
    if backend == "slack" and len(plain_text) > SLACK_MESSAGE_LIMIT:
        raise PayloadTooLargeError(f"events message is {len(plain_text)} chars, Slack limit is {SLACK_MESSAGE_LIMIT}")

    discord_embed = None
    if backend == "discord":
        discord_embed = {"title": title, "description": body, "fields": [], "footer": {"text": footer}, "color": 0x2F3136}
        if _discord_embed_char_count(discord_embed) > DISCORD_TOTAL_LIMIT:
            raise PayloadTooLargeError(f"events embed exceeds Discord's {DISCORD_TOTAL_LIMIT}-char total budget")

    return MessagePayload(
        kind="events",
        plain_text=plain_text,
        slack_blocks=_slack_blocks_from_text(plain_text) if backend == "slack" else [],
        discord_embed=discord_embed,
    )


def build_dashboard_payload(
    kind: MessageKind,
    title_key: str,
    region_a: str,
    region_b: str,
    lines_by_region: dict[str, list[str]],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> MessagePayload:
    title = i18n.t(title_key)
    footer = _footer(now_utc, display_tz, i18n)

    region_a_label = i18n.t(f"region.{region_a.lower()}")
    region_b_label = i18n.t(f"region.{region_b.lower()}")
    section_a = "\n".join(lines_by_region.get(region_a, [])) or "—"
    section_b = "\n".join(lines_by_region.get(region_b, [])) or "—"

    plain_text = (
        f"{title}\n\n— {region_a_label} —\n{section_a}\n\n— {region_b_label} —\n{section_b}\n\n{footer}"
    )

    if backend == "telegram" and len(plain_text) > TELEGRAM_MESSAGE_LIMIT:
        raise PayloadTooLargeError(f"{kind} message is {len(plain_text)} chars, Telegram limit is {TELEGRAM_MESSAGE_LIMIT}")
    if backend == "slack" and len(plain_text) > SLACK_MESSAGE_LIMIT:
        raise PayloadTooLargeError(f"{kind} message is {len(plain_text)} chars, Slack limit is {SLACK_MESSAGE_LIMIT}")

    discord_embed = None
    if backend == "discord":
        fields: list[tuple[str, str]] = []
        fields += _split_region_for_discord(region_a_label, lines_by_region.get(region_a, []) or ["—"])
        fields += _split_region_for_discord(region_b_label, lines_by_region.get(region_b, []) or ["—"])
        if len(fields) > DISCORD_MAX_FIELDS_PER_EMBED:
            raise PayloadTooLargeError(f"{kind} embed would need {len(fields)} fields, Discord allows {DISCORD_MAX_FIELDS_PER_EMBED}")
        discord_embed = {
            "title": title,
            "fields": [{"name": n, "value": v, "inline": False} for n, v in fields],
            "footer": {"text": footer},
            "color": 0x2F3136,
        }
        if _discord_embed_char_count(discord_embed) > DISCORD_TOTAL_LIMIT:
            raise PayloadTooLargeError(f"{kind} embed exceeds Discord's {DISCORD_TOTAL_LIMIT}-char total budget")

    return MessagePayload(
        kind=kind,
        plain_text=plain_text,
        slack_blocks=_slack_blocks_from_text(plain_text) if backend == "slack" else [],
        discord_embed=discord_embed,
    )


def build_all_payloads(
    exchanges: list[ExchangeConfig],
    phase_states: dict[str, PhaseState],
    upcoming_events: list[UpcomingEvent],
    now_utc: dt.datetime,
    display_tz: dt.tzinfo,
    i18n: I18n,
    backend: Backend,
) -> tuple[MessagePayload, MessagePayload, MessagePayload]:
    """Build all three payloads in one call — the shape the main loop wants."""
    lines_by_region: dict[str, list[str]] = {"America": [], "Europe": [], "Asia": [], "Oceania": []}
    for exchange in exchanges:
        lines_by_region[exchange.region].append(render_exchange_line(exchange, phase_states[exchange.mic], i18n, display_tz))

    events_payload = build_events_payload(upcoming_events, now_utc, display_tz, i18n, backend)
    amer_eu_payload = build_dashboard_payload(
        "dashboard_amer_eu", "header.dashboard_americas_eu_title", "America", "Europe",
        {"America": lines_by_region["America"], "Europe": lines_by_region["Europe"]},
        now_utc, display_tz, i18n, backend,
    )
    asia_oc_payload = build_dashboard_payload(
        "dashboard_asia_oc", "header.dashboard_asia_oceania_title", "Asia", "Oceania",
        {"Asia": lines_by_region["Asia"], "Oceania": lines_by_region["Oceania"]},
        now_utc, display_tz, i18n, backend,
    )
    return events_payload, amer_eu_payload, asia_oc_payload
