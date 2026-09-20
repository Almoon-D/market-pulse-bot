import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CalendarType = Literal["exchange_calendars", "synthetic"]
IncidentType = Literal["structured_feed", "rss_keyword", "manual"]
IncidentScope = Literal["single_stock", "market_wide"]
Region = Literal["America", "Europe", "Asia", "Oceania"]
Language = Literal["es", "en", "de", "fr"]
BackendName = Literal["discord", "slack", "telegram"]


class SessionOffsets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pre_market_minutes: int = Field(default=0, ge=0)
    opening_auction_minutes: int = Field(default=0, ge=0)
    closing_auction_minutes: int = Field(default=0, ge=0)
    post_market_minutes: int = Field(default=0, ge=0)


class LunchBreak(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def validate_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("time must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("time must use a valid 24-hour HH:MM value")
        return value


class SyntheticSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open: str
    close: str
    trading_days: list[str] = Field(min_length=1)

    @field_validator("open", "close")
    @classmethod
    def validate_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("time must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("time must use a valid 24-hour HH:MM value")
        return value


class IncidentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: IncidentType
    url: str | None = None
    scope: IncidentScope = "market_wide"
    keywords: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("incident source url must be an HTTP(S) URL")
        return value


class ExchangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    mic: str
    calendar_type: CalendarType
    timezone: str
    tz_label: str
    country_flag: str
    currency: str = Field(min_length=3, max_length=3)
    region: Region
    session_offsets: SessionOffsets
    incident_source: IncidentSource
    lunch: LunchBreak | None = None
    synthetic_schedule: SyntheticSchedule | None = None

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class NotificationCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord_webhook_url: str | None = None
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class NotificationThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_auction: int = Field(default=10, ge=0)
    regular_after_auction: int = Field(default=2, ge=0)
    lunch_break: int = Field(default=5, ge=0)
    lunch_reopening: int = Field(default=5, ge=0)
    closing_auction: int = Field(default=5, ge=0)
    extended_hours: int = Field(default=15, ge=0)
    closed: int = Field(default=5, ge=0)

class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Language = "en"
    display_timezone: str = "Europe/Madrid"
    notification_backend: BackendName = "discord"
    credentials: NotificationCredentials
    loop_interval: int = Field(default=30, ge=1)
    incident_interval: int = Field(default=90, ge=60)
    notification_thresholds: NotificationThresholds = Field(default_factory=NotificationThresholds)
    exchanges: list[ExchangeConfig] = Field(min_length=1)

    @field_validator("exchanges")
    @classmethod
    def unique_mics(cls, value: list[ExchangeConfig]) -> list[ExchangeConfig]:
        mics = [exchange.mic for exchange in value]
        if len(mics) != len(set(mics)):
            raise ValueError("exchange MIC values must be unique")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MARKET_MONITOR_", extra="ignore")
    config_path: Path = Path("config/config.yaml")


def load_exchanges(path: Path) -> list[ExchangeConfig]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return [ExchangeConfig.model_validate(item) for item in raw.get("exchanges", [])]


def load_app_config(path: Path) -> AppConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


def save_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
