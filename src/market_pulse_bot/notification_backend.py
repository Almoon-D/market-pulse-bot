"""Notification backends and durable three-slot message state."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Annotated, Any, Literal, cast
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
    def empty(cls, backend: Backend) -> StateFile:
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
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
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
    **kwargs: Any,
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

    async def pin(self, reference: StoredReference) -> bool:
        """Pin a previously-published message, if this backend supports it.

        Returns True on success, False on any graceful non-fatal failure
        (missing permission/scope, or a backend that structurally cannot
        pin at all). Never raises for a permission/capability problem --
        pinning is a nice-to-have, not something that should take down
        an otherwise-successful publish.
        """
        return False


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

    async def update(self, reference: StoredReference, payload: MessagePayload) -> DiscordRef:
        if not isinstance(reference, DiscordRef):
            raise TypeError(f"Discord backend cannot update {type(reference).__name__} reference")
        if payload.discord_embed is None:
            raise ValueError("Discord payload missing embed")
        url = f"https://discord.com/api/webhooks/{self._webhook_id}/{self._webhook_token}/messages/{reference.message_id}"
        response = await request_with_backoff(self._client, "PATCH", url, json={"embeds": [payload.discord_embed]})
        if response.status_code == 404:
            raise MessageNotFoundError(f"Discord message {reference.message_id} not found")
        response.raise_for_status()
        return reference

    async def pin(self, reference: StoredReference) -> bool:
        # Structural, not a permission edge case: pinning requires the
        # PUT /channels/{id}/pins/{message.id} REST endpoint, which needs a
        # real bot token with Manage Messages in that channel. A webhook
        # token -- all this backend has, by deliberate design (Section 5)
        # -- cannot authenticate to that endpoint at all. There is no
        # request this backend could make that would ever succeed here.
        logger.info(
            "Discord webhook backend cannot pin messages (requires a bot token with "
            "Manage Messages, not a webhook token) -- skipping, this is expected"
        )
        return False


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
        data = cast(dict[str, object], response.json())
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown_error')}")
        return data

    async def publish(self, payload: MessagePayload) -> SlackRef:
        data = await self._call(
            "chat.postMessage",
            {"channel": self._channel_id, "text": payload.plain_text, "blocks": payload.slack_blocks},
        )
        return SlackRef(backend="slack", channel_id=str(data["channel"]), ts=str(data["ts"]))

    async def update(self, reference: StoredReference, payload: MessagePayload) -> SlackRef:
        if not isinstance(reference, SlackRef):
            raise TypeError(f"Slack backend cannot update {type(reference).__name__} reference")
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

    async def pin(self, reference: StoredReference) -> bool:
        if not isinstance(reference, SlackRef):
            return False
        try:
            await self._call("pins.add", {"channel": reference.channel_id, "timestamp": reference.ts})
            return True
        except Exception as exc:  # noqa: BLE001 - pinning is best-effort, never fatal
            # Common non-fatal cases: missing_scope (bot token lacks
            # pins:write), no_pin_permission (channel setting), already_pinned.
            logger.warning("Slack pins.add failed, continuing without pinning: %s", exc)
            return False


class TelegramBackend(NotificationBackend):
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._client = client

    async def _call(
        self, method: str, body: dict[str, object], *, expect_dict_result: bool = True
    ) -> dict[str, object] | None:
        response = await request_with_backoff(self._client, "POST", f"{self._base}/{method}", json=body)
        try:
            data = cast(dict[str, object], response.json())
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} returned invalid JSON") from None
        if not data.get("ok"):
            error = str(data.get("description", "unknown_error"))
            if error.lower().startswith("bad request: message is not modified"):
                return None
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} failed: {error}")
        if not expect_dict_result:
            # Some methods (pinChatMessage, unpinChatMessage, ...) return a
            # plain boolean `result` on success per the Telegram Bot API,
            # not an object -- there's nothing further to extract here.
            return None
        result = data["result"]
        if not isinstance(result, dict):
            raise TypeError(f"Telegram {method} returned an unexpected result payload")
        return cast(dict[str, object], result)

    async def publish(self, payload: MessagePayload) -> TelegramRef:
        result = await self._call("sendMessage", {"chat_id": self._chat_id, "text": payload.plain_text})
        assert result is not None
        message_id = result.get("message_id")
        if not isinstance(message_id, (int, str)):
            raise TypeError("Telegram sendMessage returned an invalid message_id")
        return TelegramRef(backend="telegram", chat_id=str(self._chat_id), message_id=int(message_id))

    async def update(self, reference: StoredReference, payload: MessagePayload) -> TelegramRef:
        if not isinstance(reference, TelegramRef):
            raise TypeError(f"Telegram backend cannot update {type(reference).__name__} reference")
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

    async def pin(self, reference: StoredReference) -> bool:
        if not isinstance(reference, TelegramRef):
            return False
        try:
            await self._call(
                "pinChatMessage",
                {"chat_id": reference.chat_id, "message_id": reference.message_id, "disable_notification": True},
                expect_dict_result=False,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - pinning is best-effort, never fatal
            # Common non-fatal case: the bot isn't an admin in this chat
            # (Telegram requires "Pin Messages" admin rights in groups/
            # channels; in a private 1:1 chat only the other user can pin).
            logger.warning("Telegram pinChatMessage failed, continuing without pinning: %s", exc)
            return False


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
    if existing is None or existing.backend != state.backend:
        if existing is not None:
            logger.warning(
                "%s has reference backend=%s while configured backend=%s; recreating only this slot",
                slot, existing.backend, state.backend,
            )
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
