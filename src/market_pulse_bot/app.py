"""Application orchestration and CLI."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import signal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .calendar_engine import MarketSchedule, build_schedule
from .config import Settings, load_exchanges, scaffold_config, validate_exchange_calendars
from .halt_detector import IncidentStore, run_halt_detector_loop, run_incident_check_once
from .i18n import I18n
from .market_engine import PhaseState, build_upcoming_events, compute_phase_state
from .notification_backend import NotificationBackend, StateFile, StateStore, build_backend, publish_or_update
from .text_formatter import PayloadTooLargeError, build_all_payloads

logger = logging.getLogger("market_pulse_bot")


async def render_tick(
    exchanges,
    schedules: dict[str, MarketSchedule],
    incident_store: IncidentStore,
    i18n: I18n,
    display_tz: ZoneInfo,
    settings: Settings,
    backend: NotificationBackend,
    state_store: StateStore,
    state: StateFile,
    loop_mode: bool,
) -> tuple[StateFile, int]:
    now_utc = dt.datetime.now(dt.timezone.utc)
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

    failures = 0
    for slot, payload in (
        ("events_message_ref", payloads[0]),
        ("dashboard_americas_eu_ref", payloads[1]),
        ("dashboard_asia_oceania_ref", payloads[2]),
    ):
        try:
            state = await publish_or_update(backend, state_store, state, slot, payload)
        except Exception:
            failures += 1
            logger.exception("notification update failed for %s", slot)
    return state, failures


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
                backend, state_store, state, loop_mode=False
            )
            return 1 if failures else 0

        detector = asyncio.create_task(
            run_halt_detector_loop(
                exchanges, settings.manual_incidents_path, incidents,
                args.incident_interval, stop_event, client
            )
        )
        try:
            while not stop_event.is_set():
                state, _ = await render_tick(
                    exchanges, schedules, incidents, i18n, display_tz, settings,
                    backend, state_store, state, loop_mode=True
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
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
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    if args.incident_interval < 60:
        parser.error("--incident-interval must be >= 60")
    if args.interval < 1:
        parser.error("--interval must be >= 1")
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
