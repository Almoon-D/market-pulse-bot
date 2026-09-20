import argparse
import asyncio
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from src.calendar_engine import build_schedules
from src.config import AppConfig, load_app_config
from src.halt_detector import HaltDetector, IncidentState
from src.i18n import Translator
from src.notification_backend import build_backend
from src.text_formatter import build_payloads

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
EXCHANGES_FILE = ROOT / "config" / "exchanges.yaml"
LOCALES = ROOT / "locales"
STATE_FILE = ROOT / "data" / "state.json"

SCAFFOLD = [
    {"name": "Toronto Stock Exchange", "mic": "XTSE", "timezone": "America/Toronto", "tz_label": "ET", "currency": "CAD", "country_flag": "🇨🇦", "region": "America"},
    {"name": "B3", "mic": "BVMF", "timezone": "America/Sao_Paulo", "tz_label": "BRT", "currency": "BRL", "country_flag": "🇧🇷", "region": "America"},
    {"name": "London Stock Exchange", "mic": "XLON", "timezone": "Europe/London", "tz_label": "GMT/BST", "currency": "GBP", "country_flag": "🇬🇧", "region": "Europe"},
    {"name": "Xetra", "mic": "XETR", "timezone": "Europe/Berlin", "tz_label": "CET/CEST", "currency": "EUR", "country_flag": "🇩🇪", "region": "Europe"},
    {"name": "Euronext Paris", "mic": "XPAR", "timezone": "Europe/Paris", "tz_label": "CET/CEST", "currency": "EUR", "country_flag": "🇫🇷", "region": "Europe"},
    {"name": "Euronext Amsterdam", "mic": "XAMS", "timezone": "Europe/Amsterdam", "tz_label": "CET/CEST", "currency": "EUR", "country_flag": "🇳🇱", "region": "Europe"},
    {"name": "Borsa Italiana", "mic": "XMIL", "timezone": "Europe/Rome", "tz_label": "CET/CEST", "currency": "EUR", "country_flag": "🇮🇹", "region": "Europe"},
    {"name": "SIX Swiss Exchange", "mic": "XSWX", "timezone": "Europe/Zurich", "tz_label": "CET/CEST", "currency": "CHF", "country_flag": "🇨🇭", "region": "Europe"},
    {"name": "Hong Kong Stock Exchange", "mic": "XHKG", "timezone": "Asia/Hong_Kong", "tz_label": "HKT", "currency": "HKD", "country_flag": "🇭🇰", "region": "Asia"},
    {"name": "Shanghai Stock Exchange", "mic": "XSHG", "timezone": "Asia/Shanghai", "tz_label": "CST", "currency": "CNY", "country_flag": "🇨🇳", "region": "Asia"},
    {"name": "Shenzhen Stock Exchange", "mic": "XSHE", "timezone": "Asia/Shanghai", "tz_label": "CST", "currency": "CNY", "country_flag": "🇨🇳", "region": "Asia"},
    {"name": "BSE India", "mic": "XBOM", "timezone": "Asia/Kolkata", "tz_label": "IST", "currency": "INR", "country_flag": "🇮🇳", "region": "Asia"},
    {"name": "NSE India", "mic": "XNSE", "timezone": "Asia/Kolkata", "tz_label": "IST", "currency": "INR", "country_flag": "🇮🇳", "region": "Asia"},
    {"name": "Korea Exchange", "mic": "XKRX", "timezone": "Asia/Seoul", "tz_label": "KST", "currency": "KRW", "country_flag": "🇰🇷", "region": "Asia"},
    {"name": "Taiwan Stock Exchange", "mic": "XTAI", "timezone": "Asia/Taipei", "tz_label": "CST", "currency": "TWD", "country_flag": "🇹🇼", "region": "Asia"},
    {"name": "Singapore Exchange", "mic": "XSES", "timezone": "Asia/Singapore", "tz_label": "SGT", "currency": "SGD", "country_flag": "🇸🇬", "region": "Asia"},
    {"name": "Australian Securities Exchange", "mic": "XASX", "timezone": "Australia/Sydney", "tz_label": "AEST/AEDT", "currency": "AUD", "country_flag": "🇦🇺", "region": "Oceania"},
    {"name": "Cboe Australia", "mic": "XCBO", "timezone": "Australia/Sydney", "tz_label": "AEST/AEDT", "currency": "AUD", "country_flag": "🇦🇺", "region": "Oceania"},
    {"name": "Chi-X Australia", "mic": "CHIA", "timezone": "Australia/Sydney", "tz_label": "AEST/AEDT", "currency": "AUD", "country_flag": "🇦🇺", "region": "Oceania"},
    {"name": "New Zealand Exchange", "mic": "XNZE", "timezone": "Pacific/Auckland", "tz_label": "NZST/NZDT", "currency": "NZD", "country_flag": "🇳🇿", "region": "Oceania"},
]


def init_config() -> None:
    anchors = yaml.safe_load(EXCHANGES_FILE.read_text(encoding="utf-8"))
    exchanges = anchors["exchanges"]
    existing = {item["mic"] for item in exchanges}
    for item in SCAFFOLD:
        if item["mic"] in existing:
            continue
        exchanges.append({**item, "calendar_type": "exchange_calendars", "session_offsets": {"pre_market_minutes": 0, "opening_auction_minutes": 0, "closing_auction_minutes": 0, "post_market_minutes": 0}, "incident_source": {"type": "manual", "scope": "market_wide"}})
    EXCHANGES_FILE.write_text(yaml.safe_dump({"exchanges": exchanges}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = {
        "language": "es",
        "display_timezone": "Europe/Madrid",
        "notification_backend": "discord",
        "credentials": {"discord_webhook_url": None, "slack_bot_token": None, "slack_channel_id": None, "telegram_bot_token": None, "telegram_chat_id": None},
        "loop_interval": 30,
        "incident_interval": 90,
        "notification_thresholds": {"opening_auction": 10, "regular_after_auction": 2, "lunch_break": 5, "lunch_reopening": 5, "closing_auction": 5, "extended_hours": 15, "closed": 5},
        "exchanges": exchanges,
    }
    DEFAULT_CONFIG.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"Wrote {EXCHANGES_FILE} and {DEFAULT_CONFIG}")


async def run_once(config: AppConfig) -> None:
    ZoneInfo(config.display_timezone)
    schedules = build_schedules(config.exchanges)
    translator = Translator(LOCALES, config.language)
    state = IncidentState()
    detector = HaltDetector(config.exchanges, state, config.incident_interval)
    backend = build_backend(config, STATE_FILE)
    try:
        await detector.poll_once()
        from datetime import datetime
        now = datetime.now().astimezone()
        payloads = build_payloads(config, schedules, translator, now, await state.snapshot())
        await backend.sync(payloads)
    finally:
        await detector.close()
        await backend.close()


async def run_loop(config: AppConfig) -> None:
    ZoneInfo(config.display_timezone)
    schedules = build_schedules(config.exchanges)
    translator = Translator(LOCALES, config.language)
    state = IncidentState()
    detector = HaltDetector(config.exchanges, state, config.incident_interval)
    backend = build_backend(config, STATE_FILE)
    incident_task = asyncio.create_task(detector.run_forever())
    try:
        while True:
            from datetime import datetime
            now = datetime.now().astimezone()
            payloads = build_payloads(config, schedules, translator, now, await state.snapshot())
            await backend.sync(payloads)
            await asyncio.sleep(config.loop_interval)
    finally:
        incident_task.cancel()
        await asyncio.gather(incident_task, return_exceptions=True)
        await detector.close()
        await backend.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Global exchange operational-state monitor")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init-config", help="create the full config scaffold from four operational anchors")
    init_parser.set_defaults(command="init-config")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--interval", type=int, default=None, help="loop interval in seconds; YAML remains authoritative when omitted")
    parser.add_argument("--incident-interval", type=int, default=None, help="incident polling interval; YAML remains authoritative when omitted")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="application YAML config path")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    if args.command == "init-config":
        init_config()
        return
    config = load_app_config(args.config)
    if args.interval is not None:
        config.loop_interval = args.interval
    if args.incident_interval is not None:
        config.incident_interval = args.incident_interval
    asyncio.run(run_loop(config) if args.loop else run_once(config))


if __name__ == "__main__":
    main()
