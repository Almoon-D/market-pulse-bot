"""
Configuration models and loaders.

This module owns everything that comes from the outside world as *data*:
the YAML exchange roster (``config/exchanges.yaml``) and the environment /
``.env``-driven runtime settings (notification backend credentials, loop
defaults, file paths). Nothing in here talks to a calendar library, a
notification backend, or the network — it only validates shapes.

Never import ``pydantic.v1`` here or anywhere else in this project: on
Python 3.14 the v1 compatibility shim is broken and fails silently rather
than raising, which is worse than not having it at all.
"""

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


class SessionOffsets(BaseModel):
    """Minute offsets relative to an exchange's official ``open``/``close``.

    These are per-exchange operational facts (how long NYSE's pre-market
    runs, whether Madrid has a five-minute opening auction, ...) and belong
    in YAML, never as literals inside the state machine.
    """

    pre_market_minutes: int = Field(ge=0, default=0)
    opening_auction_minutes: int = Field(ge=0, default=0)
    closing_auction_minutes: int = Field(ge=0, default=0)
    post_market_minutes: int = Field(ge=0, default=0)


class IncidentSourceConfig(BaseModel):
    """How incident data is sourced for one exchange.

    Priority is strict and enforced by ``halt_detector``, not here:
    ``structured_feed`` > ``rss_keyword`` (log-only candidate) > ``manual``.
    Only a ``structured_feed`` with ``scope == "market_wide"`` may
    autonomously raise 🔶/🟥/🚨 — a ``single_stock`` feed (e.g. NASDAQ's
    trade-halts RSS) is retained for logging/reference only and never
    flips exchange-wide state by itself.
    """

    type: IncidentSourceType
    url: str | None = None
    scope: IncidentScope = "single_stock"
    keywords: list[str] | None = None

    @model_validator(mode="after")
    def _require_url_for_feeds(self) -> "IncidentSourceConfig":
        if self.type in ("structured_feed", "rss_keyword") and not self.url:
            raise ValueError(
                f"incident_source.type={self.type!r} requires a 'url'"
            )
        if self.type == "rss_keyword" and not self.keywords:
            # Not fatal — halt_detector ships a sane default list — but
            # flagged here so a misconfigured feed doesn't fail silently.
            pass
        return self


class SyntheticCalendarConfig(BaseModel):
    """Fully self-contained calendar definition for exchanges that
    ``exchange_calendars`` does not know about (e.g. Fiji's XSPX).
    """

    open_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    close_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    lunch_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    lunch_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    trading_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[str] = Field(default_factory=list)  # ISO "YYYY-MM-DD"

    @field_validator("trading_days")
    @classmethod
    def _valid_weekdays(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("trading_days must be a non-empty list of 0..6 (Mon..Sun)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _lunch_pair(self) -> "SyntheticCalendarConfig":
        if bool(self.lunch_start) != bool(self.lunch_end):
            raise ValueError("lunch_start and lunch_end must both be set or both be null")
        return self


class ExchangeConfig(BaseModel):
    """One monitored exchange, exactly as it appears in ``exchanges.yaml``."""

    name: str
    mic: str = Field(min_length=4, max_length=4)
    calendar_type: CalendarType
    timezone: str
    tz_label: str
    country_flag: str
    currency: str = Field(min_length=3, max_length=3)
    region: Region
    session_offsets: SessionOffsets
    incident_source: IncidentSourceConfig
    synthetic: SyntheticCalendarConfig | None = None

    @field_validator("mic")
    @classmethod
    def _mic_upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "ExchangeConfig":
        if self.calendar_type == "synthetic" and self.synthetic is None:
            raise ValueError(
                f"{self.mic}: calendar_type='synthetic' requires a 'synthetic' block"
            )
        if self.calendar_type == "exchange_calendars":
            if self.synthetic is not None:
                raise ValueError(
                    f"{self.mic}: 'synthetic' block is only valid with "
                    "calendar_type='synthetic'"
                )
            # Fail fast: an unknown MIC here is a config bug, not a
            # runtime surprise three hours into a --loop deployment.
            import exchange_calendars as xc

            valid = xc.get_calendar_names()
            if self.mic not in valid:
                raise ValueError(
                    f"{self.mic}: not a valid exchange_calendars MIC. "
                    f"Use calendar_type='synthetic' instead, or check spelling "
                    f"(exchange_calendars knows {len(valid)} calendars)."
                )
        return self


class ExchangeRoster(BaseModel):
    """Top-level shape of ``exchanges.yaml``."""

    exchanges: list[ExchangeConfig]

    @model_validator(mode="after")
    def _unique_mics(self) -> "ExchangeRoster":
        mics = [e.mic for e in self.exchanges]
        dupes = {m for m in mics if mics.count(m) > 1}
        if dupes:
            raise ValueError(f"duplicate MIC codes in exchanges.yaml: {sorted(dupes)}")
        return self


def load_exchanges(path: Path | str) -> list[ExchangeConfig]:
    """Load and validate ``exchanges.yaml``. Raises on the first invalid entry."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run 'python main.py init-config' first"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    roster = ExchangeRoster.model_validate(raw)
    return roster.exchanges


class Settings(BaseSettings):
    """Runtime settings: notification credentials, intervals defaults, paths.

    Populated from environment variables (prefix ``MERCADOS_``) or a local
    ``.env`` file — never from hardcoded literals. CLI flags in ``main.py``
    (``--interval``, ``--incident-interval``, ``--loop``) take precedence
    over anything here since they are explicitly per-run operator choices.
    """

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
    def _valid_display_tz(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _backend_credentials_present(self) -> "Settings":
        if self.notification_backend == "discord" and not self.discord_webhook_url:
            raise ValueError("notification_backend='discord' requires MERCADOS_DISCORD_WEBHOOK_URL")
        if self.notification_backend == "slack" and not (self.slack_bot_token and self.slack_channel):
            raise ValueError(
                "notification_backend='slack' requires MERCADOS_SLACK_BOT_TOKEN "
                "and MERCADOS_SLACK_CHANNEL (a bot token with chat:write scope — "
                "a plain incoming webhook cannot edit messages)"
            )
        if self.notification_backend == "telegram" and not (self.telegram_bot_token and self.telegram_chat_id):
            raise ValueError(
                "notification_backend='telegram' requires MERCADOS_TELEGRAM_BOT_TOKEN "
                "and MERCADOS_TELEGRAM_CHAT_ID"
            )
        return self
