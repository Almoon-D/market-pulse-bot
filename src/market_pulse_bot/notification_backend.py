"""Notification backends and durable three-slot message state."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import Backend, Settings
from .text_formatter import MessagePayload

logger = logging.getLogger(__name__)

SlotName = Literal["events_message_ref", "dashboard_americas_eu_ref", "dashboard_asia_oceania_ref"]


class DiscordRef(BaseModel):
    backend: Literal["discord"]
    message_id: str


class SlackRef(BaseModel):
    backend: Literal["slack"]
    channel_id: str
    ts: str


class TelegramRef(BaseModel):
    backend: Literal["telegram"]
    chat_id: str
    message_id: int


StoredReference = Annotated[DiscordRef | SlackRef | TelegramRef, Field(discriminator="backend")]


class StateFile(BaseModel):
    backend: Backend
    events_message_ref: StoredReference | None = None
    dashboard_americas_eu_ref: StoredReference | None = None
    dashboard_asia_oceania_ref: StoredReference | None = None

    @classmethod
    def empty(cls, backend: Backend) -> "StateFile":
        return cls(backend=backend)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, backend: Backend) -> StateFile:
        if not self.path.exists():
            return StateFile.empty(backend)
        try:
            state = StateFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning("invalid state file; starting fresh: %s", exc)
            self._quarantine()
            return StateFile.empty(backend)
        if state.backend != backend:
            logger.warning("stored backend=%s differs from configured backend=%s; refs discarded", state.backend, backend)
            return StateFile.empty(backend)
        return state

    def _quarantine(self) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, target)
        except OSError:
            logger.exception("failed to quarantine corrupt state")

    def save(self, state: StateFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


class MessageNotFoundError(RuntimeError):
    pass


async def request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 5,
    **kwargs,
) -> httpx.Response:
    delay = 1.0
    for attempt in range(retries):
        response = await client.request(method, url, **kwargs)
        if response.status_code != 429:
            return response
        if attempt == retries - 1:
            break
        raw = response.headers.get("Retry-After")
        try:
            wait = max(0.0, float(raw)) if raw is not None else delay
        except ValueError:
            wait = delay
        logger.warning("429 from %s; waiting %.1fs before retry %d/%d", url, wait, attempt + 1, retries)
        await asyncio.sleep(wait)
        delay = min(delay * 2, 30.0)
    raise RuntimeError(f"rate limit persisted after {retries} retries: {url}")


class NotificationBackend(ABC):
    @abstractmethod
    async def publish(self, payload: MessagePayload) -> StoredReference: ...

    @abstractmethod
    async def update(self, reference: StoredReference, payload: MessagePayload) -> StoredReference: ...


class DiscordBackend(NotificationBackend):
    def __init__(self, webhook_url: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(webhook_url.rstrip("/"))
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme != "https" or parsed.netloc not in {"discord.com", "discordapp.com"} or len(parts) < 4 or parts[-3] != "webhooks":
            raise ValueError("Discord webhook URL must contain /api/webhooks/{id}/{token}")
        self._webhook_url = webhook_url.rstrip("/")
        self._webhook_id = parts[-2]
        self._webhook_token = parts[-1]
        self._client = client

    async def publish(self, payload: MessagePayload) -> DiscordRef:
        if payload.discord_embed is None:
            raise ValueError("Discord payload missing embed")
        response = await request_with_backoff(
            self._client, "POST", f"{self._webhook_url}?wait=true",
            json={"embeds": [payload.discord_embed]}
        )
        response.raise_for_status()
        return DiscordRef(backend="discord", message_id=str(response.json()["id"]))

    async def update(self, reference: DiscordRef, payload: MessagePayload) -> DiscordRef:
        if payload.discord_embed is None:
            raise ValueError("Discord payload missing embed")
        url = f"https://discord.com/api/webhooks/{self._webhook_id}/{self._webhook_token}/messages/{reference.message_id}"
        response = await request_with_backoff(self._client, "PATCH", url, json={"embeds": [payload.discord_embed]})
        if response.status_code == 404:
            raise MessageNotFoundError(f"Discord message {reference.message_id} not found")
        response.raise_for_status()
        return reference


class SlackBackend(NotificationBackend):
    _BASE = "https://slack.com/api"

    def __init__(self, bot_token: str, channel_id: str, client: httpx.AsyncClient) -> None:
        self._token = bot_token
        self._channel_id = channel_id
        self._client = client

    async def _call(self, method: str, body: dict[str, object]) -> dict[str, object]:
        response = await request_with_backoff(
            self._client, "POST", f"{self._BASE}/{method}",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json; charset=utf-8"},
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown_error')}")
        return data

    async def publish(self, payload: MessagePayload) -> SlackRef:
        data = await self._call(
            "chat.postMessage",
            {"channel": self._channel_id, "text": payload.plain_text, "blocks": payload.slack_blocks},
        )
        return SlackRef(backend="slack", channel_id=str(data["channel"]), ts=str(data["ts"]))

    async def update(self, reference: SlackRef, payload: MessagePayload) -> SlackRef:
        try:
            await self._call(
                "chat.update",
                {"channel": reference.channel_id, "ts": reference.ts, "text": payload.plain_text, "blocks": payload.slack_blocks},
            )
        except RuntimeError as exc:
            if "message_not_found" in str(exc):
                raise MessageNotFoundError(str(exc)) from exc
            raise
        return reference


class TelegramBackend(NotificationBackend):
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._client = client

    async def _call(self, method: str, body: dict[str, object]) -> dict[str, object] | None:
        response = await request_with_backoff(self._client, "POST", f"{self._base}/{method}", json=body)
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} returned invalid JSON")
        if not data.get("ok"):
            error = str(data.get("description", "unknown_error"))
            if error.lower().startswith("bad request: message is not modified"):
                return None
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} failed: {error}")
        return data["result"]

    async def publish(self, payload: MessagePayload) -> TelegramRef:
        result = await self._call("sendMessage", {"chat_id": self._chat_id, "text": payload.plain_text})
        assert result is not None
        return TelegramRef(backend="telegram", chat_id=str(self._chat_id), message_id=int(result["message_id"]))

    async def update(self, reference: TelegramRef, payload: MessagePayload) -> TelegramRef:
        try:
            await self._call(
                "editMessageText",
                {"chat_id": reference.chat_id, "message_id": reference.message_id, "text": payload.plain_text},
            )
        except RuntimeError as exc:
            if "message to edit not found" in str(exc).lower():
                raise MessageNotFoundError(str(exc)) from exc
            raise
        return reference


def build_backend(settings: Settings, client: httpx.AsyncClient) -> NotificationBackend:
    if settings.notification_backend == "discord":
        assert settings.discord_webhook_url
        return DiscordBackend(settings.discord_webhook_url, client)
    if settings.notification_backend == "slack":
        assert settings.slack_bot_token and settings.slack_channel
        return SlackBackend(settings.slack_bot_token, settings.slack_channel, client)
    assert settings.telegram_bot_token and settings.telegram_chat_id
    return TelegramBackend(settings.telegram_bot_token, settings.telegram_chat_id, client)


async def publish_or_update(
    backend: NotificationBackend,
    store: StateStore,
    state: StateFile,
    slot: SlotName,
    payload: MessagePayload,
) -> StateFile:
    existing = getattr(state, slot)
    if existing is None:
        reference = await backend.publish(payload)
    else:
        try:
            reference = await backend.update(existing, payload)
        except MessageNotFoundError:
            logger.warning("%s missing; recreating only this slot", slot)
            reference = await backend.publish(payload)
    setattr(state, slot, reference)
    store.save(state)
    return state
