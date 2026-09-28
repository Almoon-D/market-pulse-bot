"""Application orchestration and CLI."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import re
import signal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .calendar_engine import MarketSchedule, build_schedule
from .config import ExchangeConfig, Settings, load_exchanges, scaffold_config, validate_exchange_calendars
from .halt_detector import IncidentStore, run_halt_detector_loop, run_incident_check_once
from .i18n import I18n
from .market_engine import PhaseState, build_upcoming_events, compute_phase_state
from .notification_backend import NotificationBackend, SlotName, StateFile, StateStore, build_backend, sync_messages
from .text_formatter import MessagePayload, PayloadTooLargeError, build_all_payloads

logger = logging.getLogger("market_pulse_bot")

# Discord webhook and Telegram bot URLs carry their secret token in the path.
_SECRET_PATTERNS = (
    (re.compile(r"(/webhooks/\d+/)[^/?\s'\"]+"), r"\1***"),
    (re.compile(r"(/bot)[^/\s'\"]+"), r"\1***"),
)


class RedactingFormatter(logging.Formatter):
    """Masks webhook and bot tokens anywhere in a log line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


async def render_tick(
    exchanges: list[ExchangeConfig],
    schedules: dict[str, MarketSchedule],
    incident_store: IncidentStore,
    i18n: I18n,
    display_tz: ZoneInfo,
    settings: Settings,
    backend: NotificationBackend,
    state_store: StateStore,
    state: StateFile,
    last_sent: dict[SlotName, MessagePayload],
    loop_mode: bool,
) -> tuple[StateFile, int]:
    now_utc = dt.datetime.now(dt.UTC)
    incidents = await incident_store.get_all()
    states: dict[str, PhaseState] = {
        exchange.mic: compute_phase_state(
            exchange, schedules[exchange.mic], now_utc, incidents.get(exchange.mic), loop_mode
        )
        for exchange in exchanges
    }
    events = build_upcoming_events(exchanges, schedules, now_utc)
    try:
        payloads = build_all_payloads(
            exchanges, states, events, now_utc, display_tz, i18n, settings.notification_backend
        )
    except PayloadTooLargeError:
        logger.exception("payload construction failed; no messages sent")
        return state, 1
    return await sync_messages(backend, state_store, state, payloads, last_sent)


async def async_run(args: argparse.Namespace) -> int:
    settings = Settings()
    exchanges = load_exchanges(settings.exchanges_config_path)
    validate_exchange_calendars(exchanges)
    schedules = {exchange.mic: build_schedule(exchange) for exchange in exchanges}
    i18n = I18n(settings.locales_dir, settings.language)
    display_tz = ZoneInfo(settings.display_timezone)
    incidents = IncidentStore()
    state_store = StateStore(settings.state_path)
    state = state_store.load(settings.notification_backend)
    last_sent: dict[SlotName, MessagePayload] = {}
    stop_event = asyncio.Event()

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            running_loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        backend = build_backend(settings, client)
        if not args.loop:
            await run_incident_check_once(exchanges, settings.manual_incidents_path, incidents, client)
            _, failures = await render_tick(
                exchanges, schedules, incidents, i18n, display_tz, settings,
                backend, state_store, state, last_sent, loop_mode=False
            )
            return 1 if failures else 0

        detector = asyncio.create_task(
            run_halt_detector_loop(
                exchanges, settings.manual_incidents_path, incidents,
                args.incident_interval, stop_event, client
            )
        )
        deadline = running_loop.time() + args.max_runtime if args.max_runtime else None
        try:
            while not stop_event.is_set():
                state, _ = await render_tick(
                    exchanges, schedules, incidents, i18n, display_tz, settings,
                    backend, state_store, state, last_sent, loop_mode=True
                )
                timeout = float(args.interval)
                if deadline is not None:
                    remaining = deadline - running_loop.time()
                    if remaining <= 0:
                        logger.info("--max-runtime reached; stopping")
                        break
                    timeout = min(timeout, remaining)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        finally:
            stop_event.set()
            await detector
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-pulse-bot")
    parser.add_argument("command", nargs="?", choices=("run", "init-config"), default="run")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--incident-interval", type=int, default=90)
    parser.add_argument(
        "--max-runtime", type=int, default=0,
        help="with --loop: stop cleanly after this many seconds (0 = run until stopped)",
    )
    return parser


def main() -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx logs every request URL at INFO; not useful, and the URLs are secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args()
    if args.incident_interval < 60:
        parser.error("--incident-interval must be >= 60")
    if args.interval < 1:
        parser.error("--interval must be >= 1")
    if args.max_runtime < 0:
        parser.error("--max-runtime must be >= 0")
    if args.max_runtime and not args.loop:
        parser.error("--max-runtime requires --loop")
    if args.command == "init-config":
        path = Path("config/exchanges.yaml")
        if not path.exists():
            raise SystemExit("config/exchanges.yaml is missing")
        changed = scaffold_config(path)
        print("config/exchanges.yaml normalized" if changed else "config/exchanges.yaml already normalized")
        return 0
    try:
        return asyncio.run(async_run(args))
    except KeyboardInterrupt:
        return 0
