"""Pydantic models + YAML loading; every exchange fully self-contained."""

import enum
import pathlib

import yaml
from pydantic import BaseModel, Field


class BackendKind(str, enum.Enum):
    DISCORD = "discord"
    SLACK = "slack"
    TELEGRAM = "telegram"


class IncidentType(str, enum.Enum):
    STRUCTURED_FEED = "structured_feed"
    RSS_KEYWORD = "rss_keyword"
    MANUAL_ONLY = "manual_only"


class CalendarType(str, enum.Enum):
    XCAL = "xcal"
    SYNTHETIC = "synthetic"


class FeedScope(str, enum.Enum):
    MARKET_WIDE = "market_wide"
    SINGLE_STOCK = "single_stock"


class DiscordCreds(BaseModel):
    webhook_id: str
    webhook_token: str


class SlackCreds(BaseModel):
    bot_token: str


class TelegramCreds(BaseModel):
    bot_token: str
    chat_id: str


class BackendConfig(BaseModel):
    backend: BackendKind = BackendKind.DISCORD
    discord: DiscordCreds | None = None
    slack: SlackCreds | None = None
    telegram: TelegramCreds | None = None


class LunchBreak(BaseModel):
    start: str  # "14:30"
    end: str    # "16:30"


class SyntheticSchedule(BaseModel):
    trading_days: list[int] = Field(..., description="ISO weekdays: 1=Mon .. 7=Sun")
    open: str
    close: str


class SessionTimes(BaseModel):
    pre_market: str | None = None
    opening_auction: str | None = None
    open: str
    close: str
    closing_auction: str | None = None
    post_market: str | None = None


class IncidentSource(BaseModel):
    type: IncidentType = IncidentType.MANUAL_ONLY
    url: str | None = None
    scope: FeedScope = FeedScope.SINGLE_STOCK


class ExchangeConfig(BaseModel):
    mic: str
    name: str
    country_flag: str
    currency: str
    tz_label: str
    tz: str
    calendar_type: CalendarType
    xcal_code: str | None = None          # e.g. "XMAD", "BVMF"
    synthetic_schedule: SyntheticSchedule | None = None
    observed_holidays: list[str] = []     # extra ISO dates treated as holidays
    early_close_days: dict[str, str] = {} # ISO date -> close time "HH:MM"
    session: SessionTimes | None = None
    lunch_break: LunchBreak | None = None
    incident_source: IncidentSource = IncidentSource()

    def __init__(self, **data) -> None:
        super().__init__(**data)
        src = data.get("incident_source")
        if isinstance(src, dict):
            self.incident_source = IncidentSource(**src)


class AppSettings(BaseModel):
    language: str = "es"
    default_timezone: str = "UTC"
    poll_interval_seconds: int = 30
    data_dir: str = "data"
    notification_backend: BackendConfig = BackendConfig()
    exchanges: list[ExchangeConfig]


def load_settings(path: str) -> AppSettings:
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    return AppSettings(**raw)
