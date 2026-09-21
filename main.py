#!/usr/bin/env python3
"""
Global market-status monitor — CLI entrypoint.

Single ``asyncio`` event loop, two independent clocks:
  * the render loop (this module, default 30s / ``--interval``) computes
    phase state and talks to the notification backend — the ONLY code
    that ever does so;
  * the incident-polling background task (``src/halt_detector.py``,
    default 90s / ``--incident-interval``, minimum 60s) only ever writes
    to the shared, lock-guarded ``IncidentStore``.

Usage:
    python main.py init-config
    python main.py                              # one-shot (for cron/systemd-timer)
    python main.py --loop --interval 30 --incident-interval 90
"""

import argparse
import asyncio
import datetime as dt
import logging
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

from src.calendar_engine import MarketSchedule, build_schedule
from src.config import ExchangeConfig, Settings, load_exchanges
from src.halt_detector import IncidentStore, run_incident_check_once, run_halt_detector_loop
from src.i18n import I18n
from src.market_engine import PhaseState, build_upcoming_events, compute_phase_state
from src.notification_backend import StateFile, StateStore, build_backend, publish_or_update
from src.text_formatter import PayloadTooLargeError, build_all_payloads

logger = logging.getLogger("mercados")


# --------------------------------------------------------------------------- #
# init-config: scaffold config/exchanges.yaml
# --------------------------------------------------------------------------- #

_DEFAULT_SESSION_OFFSETS = {
    "pre_market_minutes": 0,
    "opening_auction_minutes": 5,
    "closing_auction_minutes": 5,
    "post_market_minutes": 0,
}
_DEFAULT_INCIDENT_SOURCE = {"type": "manual", "scope": "market_wide"}


def _anchor_entries() -> list[dict]:
    """The four fully-worked anchors from Section 6 — identical to the
    shipped ``config/exchanges.yaml`` (kept here too so ``init-config``
    is self-contained and produces a working file from nothing).
    """
    return [
        {
            "name": "NYSE",
            "mic": "XNYS",
            "calendar_type": "exchange_calendars",
            "timezone": "America/New_York",
            "tz_label": "ET",
            "country_flag": "🇺🇸",
            "currency": "USD",
            "region": "America",
            "session_offsets": {
                "pre_market_minutes": 330,
                "opening_auction_minutes": 0,
                "closing_auction_minutes": 10,
                "post_market_minutes": 240,
            },
            "incident_source": {
                "type": "structured_feed",
                "url": "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
                "scope": "single_stock",
            },
        },
        {
            "name": "Bolsa de Madrid",
            "mic": "XMAD",
            "calendar_type": "exchange_calendars",
            "timezone": "Europe/Madrid",
            "tz_label": "CET/CEST",
            "country_flag": "🇪🇸",
            "currency": "EUR",
            "region": "Europe",
            "session_offsets": {
                "pre_market_minutes": 0,
                "opening_auction_minutes": 5,
                "closing_auction_minutes": 5,
                "post_market_minutes": 0,
            },
            "incident_source": {"type": "manual", "scope": "market_wide"},
        },
        {
            "name": "Tokyo Stock Exchange",
            "mic": "XTKS",
            "calendar_type": "exchange_calendars",
            "timezone": "Asia/Tokyo",
            "tz_label": "JST",
            "country_flag": "🇯🇵",
            "currency": "JPY",
            "region": "Asia",
            "session_offsets": {
                "pre_market_minutes": 0,
                "opening_auction_minutes": 5,
                "closing_auction_minutes": 5,
                "post_market_minutes": 0,
            },
            "incident_source": {"type": "manual", "scope": "market_wide"},
        },
        {
            "name": "South Pacific Stock Exchange",
            "mic": "XSPX",
            "calendar_type": "synthetic",
            "timezone": "Pacific/Fiji",
            "tz_label": "FJT",
            "country_flag": "🇫🇯",
            "currency": "FJD",
            "region": "Oceania",
            "session_offsets": {
                "pre_market_minutes": 0,
                "opening_auction_minutes": 0,
                "closing_auction_minutes": 0,
                "post_market_minutes": 0,
            },
            "incident_source": {"type": "manual", "scope": "market_wide"},
            "synthetic": {
                "open_time": "10:00",
                "close_time": "12:00",
                "lunch_start": None,
                "lunch_end": None,
                "trading_days": [0, 1, 2, 3, 4],
                "holidays": [],
            },
        },
    ]


# (name, mic, calendar_type, timezone, tz_label, country_flag, currency, region)
# MICs verified against exchange_calendars>=4.13.2's registry. Two — XSHE
# (Shenzhen) and XNSE (NSE India) — aren't in that registry at all, so
# they're scaffolded as calendar_type="synthetic" with best-effort standard
# trading hours; review before relying on them for holiday accuracy.
# Cboe Australia and Chi-X are scaffolded as synthetic too: Chi-X Australia
# rebranded to Cboe Australia in 2021, exchange_calendars carries neither
# under a dedicated calendar, and both trade the same hours as ASX.
_SCAFFOLD_SPEC: list[tuple[str, str, str, str, str, str, str, str]] = [
    ("Toronto Stock Exchange", "XTSE", "exchange_calendars", "America/Toronto", "ET", "🇨🇦", "CAD", "America"),
    ("B3", "BVMF", "exchange_calendars", "America/Sao_Paulo", "BRT", "🇧🇷", "BRL", "America"),
    ("London Stock Exchange", "XLON", "exchange_calendars", "Europe/London", "GMT/BST", "🇬🇧", "GBP", "Europe"),
    ("Deutsche Börse Xetra", "XETR", "exchange_calendars", "Europe/Berlin", "CET/CEST", "🇩🇪", "EUR", "Europe"),
    ("Euronext Paris", "XPAR", "exchange_calendars", "Europe/Paris", "CET/CEST", "🇫🇷", "EUR", "Europe"),
    ("Euronext Amsterdam", "XAMS", "exchange_calendars", "Europe/Amsterdam", "CET/CEST", "🇳🇱", "EUR", "Europe"),
    ("Euronext Milan", "XMIL", "exchange_calendars", "Europe/Rome", "CET/CEST", "🇮🇹", "EUR", "Europe"),
    ("SIX Swiss Exchange", "XSWX", "exchange_calendars", "Europe/Zurich", "CET/CEST", "🇨🇭", "CHF", "Europe"),
    ("Hong Kong Exchange", "XHKG", "exchange_calendars", "Asia/Hong_Kong", "HKT", "🇭🇰", "HKD", "Asia"),
    ("Shanghai Stock Exchange", "XSHG", "exchange_calendars", "Asia/Shanghai", "CST", "🇨🇳", "CNY", "Asia"),
    ("Shenzhen Stock Exchange", "XSHE", "synthetic", "Asia/Shanghai", "CST", "🇨🇳", "CNY", "Asia"),
    ("BSE India", "XBOM", "exchange_calendars", "Asia/Kolkata", "IST", "🇮🇳", "INR", "Asia"),
    ("National Stock Exchange of India", "XNSE", "synthetic", "Asia/Kolkata", "IST", "🇮🇳", "INR", "Asia"),
    ("Korea Exchange", "XKRX", "exchange_calendars", "Asia/Seoul", "KST", "🇰🇷", "KRW", "Asia"),
    ("Taiwan Stock Exchange", "XTAI", "exchange_calendars", "Asia/Taipei", "CST", "🇹🇼", "TWD", "Asia"),
    ("Singapore Exchange", "XSES", "exchange_calendars", "Asia/Singapore", "SGT", "🇸🇬", "SGD", "Asia"),
    ("Australian Securities Exchange", "XASX", "exchange_calendars", "Australia/Sydney", "AEST/AEDT", "🇦🇺", "AUD", "Oceania"),
    ("Cboe Australia", "CXAA", "synthetic", "Australia/Sydney", "AEST/AEDT", "🇦🇺", "AUD", "Oceania"),
    ("Chi-X Australia", "CHIA", "synthetic", "Australia/Sydney", "AEST/AEDT", "🇦🇺", "AUD", "Oceania"),
    ("New Zealand Exchange", "XNZE", "exchange_calendars", "Pacific/Auckland", "NZST/NZDT", "🇳🇿", "NZD", "Oceania"),
]

_SYNTHETIC_HOURS: dict[str, dict] = {
    "XSHE": {"open_time": "09:30", "close_time": "15:00", "lunch_start": "11:30", "lunch_end": "13:00", "trading_days": [0, 1, 2, 3, 4], "holidays": []},
    "XNSE": {"open_time": "09:15", "close_time": "15:30", "lunch_start": None, "lunch_end": None, "trading_days": [0, 1, 2, 3, 4], "holidays": []},
    "CXAA": {"open_time": "10:00", "close_time": "16:00", "lunch_start": None, "lunch_end": None, "trading_days": [0, 1, 2, 3, 4], "holidays": []},
    "CHIA": {"open_time": "10:00", "close_time": "16:00", "lunch_start": None, "lunch_end": None, "trading_days": [0, 1, 2, 3, 4], "holidays": []},
}


def _scaffold_entries() -> list[dict]:
    entries = []
    for name, mic, calendar_type, timezone, tz_label, flag, currency, region in _SCAFFOLD_SPEC:
        entry = {
            "name": name,
            "mic": mic,
            "calendar_type": calendar_type,
            "timezone": timezone,
            "tz_label": tz_label,
            "country_flag": flag,
            "currency": currency,
            "region": region,
            "session_offsets": dict(_DEFAULT_SESSION_OFFSETS),
            "incident_source": dict(_DEFAULT_INCIDENT_SOURCE),
        }
        if calendar_type == "synthetic":
            entry["synthetic"] = _SYNTHETIC_HOURS[mic]
        entries.append(entry)
    return entries


def cmd_init_config(path: Path) -> int:
    """Scaffold 'the remaining 20' (Section 6) onto whatever is already in
    ``exchanges.yaml`` — additive and idempotent, so running this against
    the shipped 4-anchor file adds the other 20 without touching them, and
    running it again afterwards is a no-op rather than an error.
    """
    file_already_existed = path.exists()
    if file_already_existed:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries: list[dict] = list(raw.get("exchanges", None) or [])
    else:
        entries = _anchor_entries()
    existing_mics = {str(e["mic"]).upper() for e in entries}

    added_mics = []
    for entry in _scaffold_entries():
        if entry["mic"] not in existing_mics:
            entries.append(entry)
            added_mics.append(entry["mic"])

    if not added_mics and file_already_existed:
        print(f"{path} already has all 24 exchanges — nothing to add.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# Generated/updated by 'python main.py init-config'.\n"
            "# The 4 anchors (XNYS, XMAD, XTKS, XSPX) are fully worked; the other 20 have\n"
            "# calendar_type/timezone/tz_label/currency/country_flag/region pre-filled and\n"
            "# generic session_offsets/incident_source you should review before relying on.\n"
        )
        yaml.dump({"exchanges": entries}, fh, sort_keys=False, allow_unicode=True, width=100)

    if file_already_existed:
        print(f"Added {len(added_mics)} exchanges to {path}: {', '.join(added_mics)}")
    else:
        print(f"Wrote {len(entries)} exchanges to {path}")
    return 0


# --------------------------------------------------------------------------- #
# Render + publish tick
# --------------------------------------------------------------------------- #


async def render_tick(
    exchanges: list[ExchangeConfig],
    schedules: dict[str, MarketSchedule],
    incident_store: IncidentStore,
    i18n: I18n,
    display_tz: ZoneInfo,
    settings: Settings,
    backend,
    state_store: StateStore,
    state: StateFile,
    loop_mode: bool,
) -> StateFile:
    now_utc = dt.datetime.now(dt.timezone.utc)
    incidents = await incident_store.get_all()
    phase_states: dict[str, PhaseState] = {
        exchange.mic: compute_phase_state(exchange, schedules[exchange.mic], now_utc, incidents.get(exchange.mic), loop_mode)
        for exchange in exchanges
    }
    upcoming_events = build_upcoming_events(exchanges, schedules, now_utc)

    try:
        events_payload, amer_eu_payload, asia_oc_payload = build_all_payloads(
            exchanges, phase_states, upcoming_events, now_utc, display_tz, i18n, settings.notification_backend
        )
    except PayloadTooLargeError:
        logger.exception("payload exceeded backend limits — skipping this tick, nothing was sent")
        return state

    state = await publish_or_update(backend, state_store, state, "events_message_ref", events_payload)
    state = await publish_or_update(backend, state_store, state, "dashboard_americas_eu_ref", amer_eu_payload)
    state = await publish_or_update(backend, state_store, state, "dashboard_asia_oceania_ref", asia_oc_payload)
    logger.info("tick complete: %d exchanges, %d upcoming events", len(exchanges), len(upcoming_events))
    return state


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def async_main(args: argparse.Namespace) -> int:
    settings = Settings()
    exchanges = load_exchanges(settings.exchanges_config_path)
    schedules = {exchange.mic: build_schedule(exchange) for exchange in exchanges}
    i18n = I18n(settings.locales_dir, settings.language)
    display_tz = ZoneInfo(settings.display_timezone)
    incident_store = IncidentStore()
    state_store = StateStore(settings.state_path)
    state = state_store.load(settings.notification_backend)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # not available on Windows; Ctrl+C still raises KeyboardInterrupt

    async with httpx.AsyncClient() as client:
        backend = build_backend(settings, client)

        if not args.loop:
            # One-shot: run a single incident-detection pass inline, then a
            # single render/publish tick, then exit. The cadence is entirely
            # the external scheduler's job (cron, a systemd timer, ...).
            await run_incident_check_once(exchanges, settings.manual_incidents_path, incident_store, client)
            await render_tick(exchanges, schedules, incident_store, i18n, display_tz, settings, backend, state_store, state, loop_mode=False)
            return 0

        detector_task = asyncio.create_task(
            run_halt_detector_loop(exchanges, settings.manual_incidents_path, incident_store, args.incident_interval, stop_event)
        )
        try:
            while not stop_event.is_set():
                state = await render_tick(
                    exchanges, schedules, incident_store, i18n, display_tz, settings, backend, state_store, state, loop_mode=True
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            stop_event.set()
            await detector_task
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Tracks global stock-exchange operational status and publishes it to Discord/Slack/Telegram.",
    )
    parser.add_argument(
        "command", nargs="?", default="run", choices=["run", "init-config"],
        help="'run' (default): render and publish. 'init-config': scaffold config/exchanges.yaml.",
    )
    parser.add_argument("--loop", action="store_true", help="daemon mode: keep running instead of a single one-shot pass")
    parser.add_argument("--interval", type=int, default=30, help="render loop interval in seconds, --loop mode only (default: 30)")
    parser.add_argument("--incident-interval", type=int, default=90, dest="incident_interval", help="incident-polling interval in seconds, minimum 60 (default: 90)")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.incident_interval < 60:
        parser.error("--incident-interval must be >= 60")

    if args.command == "init-config":
        return cmd_init_config(Path("config/exchanges.yaml"))

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
