"""Typed configuration loading and validation."""

from __future__ import annotations

import zoneinfo
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Region = Literal["America", "Europe", "Asia", "Oceania"]
CalendarType = Literal["exchange_calendars", "synthetic"]
IncidentSourceType = Literal["structured_feed", "rss_keyword", "manual"]
IncidentScope = Literal["single_stock", "market_wide"]
Backend = Literal["discord", "slack", "telegram"]
Language = Literal["es", "en", "de", "fr"]
WindowPhase = Literal["pre_market", "opening_auction", "closing_auction", "post_market"]
WindowAnchor = Literal["session_open", "session_close"]


class PhaseWindowConfig(BaseModel):
    phase: WindowPhase
    anchor: WindowAnchor
    start_offset_minutes: int
    end_offset_minutes: int

    @model_validator(mode="after")
    def validate_window(self) -> "PhaseWindowConfig":
        if self.end_offset_minutes <= self.start_offset_minutes:
            raise ValueError(f"{self.phase}: end offset must be greater than start offset")
        expected_anchor = "session_open" if self.phase in {"pre_market", "opening_auction"} else "session_close"
        if self.anchor != expected_anchor:
            raise ValueError(f"{self.phase}: anchor must be {expected_anchor!r}")
        if self.phase == "pre_market" and self.end_offset_minutes > 0:
            raise ValueError("pre_market must end no later than the official session open")
        if self.phase == "opening_auction" and self.end_offset_minutes > 0:
            raise ValueError("opening_auction must end no later than the official session open")
        if self.phase == "closing_auction" and self.start_offset_minutes > 0:
            raise ValueError("closing_auction must start no later than the official session close")
        if self.phase == "post_market" and self.start_offset_minutes < 0:
            raise ValueError("post_market must start at or after the official session close")
        return self


class IncidentSourceConfig(BaseModel):
    type: IncidentSourceType
    url: str | None = None
    scope: IncidentScope = "single_stock"
    keywords: list[str] | None = None
    field_map: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "IncidentSourceConfig":
        if self.type in {"structured_feed", "rss_keyword"} and not self.url:
            raise ValueError(f"{self.type} requires a URL")
        if self.scope == "market_wide" and self.type == "rss_keyword":
            raise ValueError("rss_keyword cannot be market_wide")
        return self


class SyntheticCalendarConfig(BaseModel):
    open_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    close_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    lunch_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    lunch_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    trading_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[str] = Field(default_factory=list)

    @field_validator("trading_days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        if not value or any(day < 0 or day > 6 for day in value):
            raise ValueError("trading_days must contain weekday numbers 0..6")
        return sorted(set(value))

    @model_validator(mode="after")
    def valid_lunch(self) -> "SyntheticCalendarConfig":
        if bool(self.lunch_start) != bool(self.lunch_end):
            raise ValueError("lunch_start and lunch_end must be both set or both null")
        return self


class ExchangeConfig(BaseModel):
    name: str
    mic: str = Field(min_length=4, max_length=4)
    calendar_type: CalendarType
    timezone: str
    tz_label: str
    country_flag: str
    currency: str = Field(min_length=3, max_length=3)
    region: Region
    phase_windows: list[PhaseWindowConfig] = Field(default_factory=list)
    incident_source: IncidentSourceConfig
    synthetic: SyntheticCalendarConfig | None = None

    @field_validator("mic")
    @classmethod
    def upper_mic(cls, value: str) -> str:
        return value.upper()

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def cross_field_checks(self) -> "ExchangeConfig":
        if self.calendar_type == "synthetic" and self.synthetic is None:
            raise ValueError(f"{self.mic}: synthetic calendar requires synthetic block")
        if self.calendar_type == "exchange_calendars" and self.synthetic is not None:
            raise ValueError(f"{self.mic}: synthetic block is not valid for exchange_calendars")
        phases = [window.phase for window in self.phase_windows]
        if len(phases) != len(set(phases)):
            raise ValueError(f"{self.mic}: phase_windows cannot repeat a phase")
        return self


class ExchangeRoster(BaseModel):
    exchanges: list[ExchangeConfig]

    @model_validator(mode="after")
    def unique_mics(self) -> "ExchangeRoster":
        mics = [exchange.mic for exchange in self.exchanges]
        duplicates = sorted({mic for mic in mics if mics.count(mic) > 1})
        if duplicates:
            raise ValueError(f"duplicate MIC codes: {duplicates}")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERCADOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    notification_backend: Backend
    display_timezone: str = "Europe/Madrid"
    language: Language = "es"
    discord_webhook_url: str | None = None
    slack_bot_token: str | None = None
    slack_channel: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    exchanges_config_path: Path = Path("config/exchanges.yaml")
    state_path: Path = Path("data/state.json")
    manual_incidents_path: Path = Path("data/manual_incidents.yaml")
    locales_dir: Path = Path("locales")

    @field_validator("display_timezone")
    @classmethod
    def valid_display_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def validate_backend_credentials(self) -> "Settings":
        if self.notification_backend == "discord" and not self.discord_webhook_url:
            raise ValueError("Discord backend requires MERCADOS_DISCORD_WEBHOOK_URL")
        if self.notification_backend == "slack" and not (self.slack_bot_token and self.slack_channel):
            raise ValueError("Slack backend requires MERCADOS_SLACK_BOT_TOKEN and MERCADOS_SLACK_CHANNEL")
        if self.notification_backend == "telegram" and not (self.telegram_bot_token and self.telegram_chat_id):
            raise ValueError("Telegram backend requires MERCADOS_TELEGRAM_BOT_TOKEN and MERCADOS_TELEGRAM_CHAT_ID")
        return self


def load_exchanges(path: Path) -> list[ExchangeConfig]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ExchangeRoster.model_validate(raw).exchanges


def validate_exchange_calendars(exchanges: list[ExchangeConfig]) -> None:
    import exchange_calendars as xc

    canonical = set(xc.get_calendar_names(include_aliases=False))
    for exchange in exchanges:
        if exchange.calendar_type == "exchange_calendars" and exchange.mic not in canonical:
            raise ValueError(
                f"{exchange.mic} is not a canonical exchange_calendars calendar; "
                "use a supported canonical MIC or a synthetic schedule"
            )


def scaffold_config(path: Path) -> bool:
    """Normalize the tracked canonical roster; safe to run repeatedly."""
    exchanges = load_exchanges(path)
    normalized = yaml.safe_dump(
        {"exchanges": [exchange.model_dump(mode="json") for exchange in exchanges]},
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    current = path.read_text(encoding="utf-8")
    if current == normalized:
        return False
    path.write_text(normalized, encoding="utf-8")
    return True
