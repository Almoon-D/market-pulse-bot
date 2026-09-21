"""
Notification backend abstraction (Section 5).

Every backend implements the same two operations — ``publish`` and
``update`` — and returns the same ``MessageRef`` shape. The three messages'
*content* is decided entirely by ``text_formatter``; nothing here ever
looks at wording. ``data/state.json`` is the durable record of which
external message ID backs each of the three named slots, written with an
atomic temp-file-then-rename so a crash mid-write can never corrupt it.
"""

import asyncio
import dataclasses
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from src.config import Settings
from src.text_formatter import MessagePayload

logger = logging.getLogger(__name__)

SlotName = Literal["events_message_ref", "dashboard_americas_eu_ref", "dashboard_asia_oceania_ref"]


class MessageNotFoundError(RuntimeError):
    """Raised internally when a backend reports the target message is
    gone (deleted by a user, channel purged, ...). Callers catch this to
    self-heal: purge the stale ref and re-publish fresh.
    """


@dataclass(frozen=True)
class MessageRef:
    backend: Literal["discord", "slack", "telegram"]
    external_id: str  # Discord message id / Slack 'ts' / Telegram message_id
    channel: str | None = None  # Slack channel or Telegram chat_id, for context

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MessageRef":
        return cls(backend=data["backend"], external_id=data["external_id"], channel=data.get("channel"))


@dataclass
class StateFile:
    backend: str
    events_message_ref: dict | None = None
    dashboard_americas_eu_ref: dict | None = None
    dashboard_asia_oceania_ref: dict | None = None

    @classmethod
    def empty(cls, backend: str) -> "StateFile":
        return cls(backend=backend)

    @classmethod
    def from_json(cls, text: str) -> "StateFile":
        data = json.loads(text)
        return cls(
            backend=data.get("backend", ""),
            events_message_ref=data.get("events_message_ref"),
            dashboard_americas_eu_ref=data.get("dashboard_americas_eu_ref"),
            dashboard_asia_oceania_ref=data.get("dashboard_asia_oceania_ref"),
        )

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, ensure_ascii=False)


class StateStore:
    """Atomic (temp-file + rename) persistence for ``data/state.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, backend: str) -> StateFile:
        if not self.path.exists():
            return StateFile.empty(backend)
        state = StateFile.from_json(self.path.read_text(encoding="utf-8"))
        if state.backend != backend:
            # Backend was switched since the last run: old refs point at
            # messages on a different platform and are meaningless here.
            logger.warning("state.json was for backend=%r, now running backend=%r — starting fresh", state.backend, backend)
            return StateFile.empty(backend)
        return state

    def save(self, state: StateFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX and on Windows (Python 3.3+)


async def _request_with_backoff(
    client: httpx.AsyncClient, method: str, url: str, *, max_retries: int = 5, **kwargs
) -> httpx.Response:
    """Exponential backoff on HTTP 429, honoring Retry-After when present."""
    delay = 1.0
    for attempt in range(max_retries):
        response = await client.request(method, url, **kwargs)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", delay))
            logger.warning("429 from %s, backing off %.1fs (attempt %d/%d)", url, retry_after, attempt + 1, max_retries)
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        return response
    raise RuntimeError(f"exhausted {max_retries} retries against {url} (still rate-limited)")


class NotificationBackend(ABC):
    @abstractmethod
    async def publish(self, payload: MessagePayload) -> MessageRef: ...

    @abstractmethod
    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef: ...


class DiscordBackend(NotificationBackend):
    """Webhook-based. Discord webhooks have no separate 'get channel'
    concept — everything is scoped to the webhook id/token pair embedded
    in the URL itself.
    """

    def __init__(self, webhook_url: str, client: httpx.AsyncClient) -> None:
        self._webhook_url = webhook_url.rstrip("/")
        self._client = client
        self._webhook_id, self._webhook_token = self._parse(self._webhook_url)

    @staticmethod
    def _parse(url: str) -> tuple[str, str]:
        parts = url.split("/")
        # .../webhooks/{id}/{token}
        return parts[-2], parts[-1]

    async def publish(self, payload: MessagePayload) -> MessageRef:
        assert payload.discord_embed is not None
        # ?wait=true is mandatory on first POST: without it Discord answers
        # 204 No Content and the message id is unrecoverable.
        url = f"{self._webhook_url}?wait=true"
        response = await _request_with_backoff(self._client, "POST", url, json={"embeds": [payload.discord_embed]})
        response.raise_for_status()
        data = response.json()
        return MessageRef(backend="discord", external_id=str(data["id"]))

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        assert payload.discord_embed is not None
        url = f"https://discord.com/api/webhooks/{self._webhook_id}/{self._webhook_token}/messages/{message_ref.external_id}"
        response = await _request_with_backoff(self._client, "PATCH", url, json={"embeds": [payload.discord_embed]})
        if response.status_code == 404:
            raise MessageNotFoundError(f"discord message {message_ref.external_id} no longer exists")
        response.raise_for_status()
        return message_ref


class SlackBackend(NotificationBackend):
    """Bot-token based (``chat:write``). A plain incoming webhook cannot
    edit messages, which is why this requires a bot token + channel
    rather than a webhook URL.
    """

    _BASE = "https://slack.com/api"

    def __init__(self, bot_token: str, channel: str, client: httpx.AsyncClient) -> None:
        self._token = bot_token
        self._channel = channel
        self._client = client

    async def _call(self, method: str, body: dict) -> dict:
        url = f"{self._BASE}/{method}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json; charset=utf-8"}
        response = await _request_with_backoff(self._client, "POST", url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error in ("message_not_found", "channel_not_found"):
                raise MessageNotFoundError(f"slack {method} failed: {error}")
            raise RuntimeError(f"slack {method} failed: {error}")
        return data

    async def publish(self, payload: MessagePayload) -> MessageRef:
        data = await self._call(
            "chat.postMessage", {"channel": self._channel, "text": payload.plain_text, "blocks": payload.slack_blocks}
        )
        return MessageRef(backend="slack", external_id=data["ts"], channel=data.get("channel", self._channel))

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        channel = message_ref.channel or self._channel
        await self._call(
            "chat.update",
            {"channel": channel, "ts": message_ref.external_id, "text": payload.plain_text, "blocks": payload.slack_blocks},
        )
        return message_ref


class TelegramBackend(NotificationBackend):
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._client = client

    async def _call(self, method: str, body: dict) -> dict:
        url = f"{self._base}/{method}"
        response = await _request_with_backoff(self._client, "POST", url, json=body)
        data = response.json()
        if not data.get("ok"):
            description = data.get("description", "unknown_error")
            if "message to edit not found" in description.lower() or "message can't be edited" in description.lower():
                raise MessageNotFoundError(f"telegram {method} failed: {description}")
            raise RuntimeError(f"telegram {method} failed: {description}")
        return data["result"]

    async def publish(self, payload: MessagePayload) -> MessageRef:
        result = await self._call("sendMessage", {"chat_id": self._chat_id, "text": payload.plain_text})
        return MessageRef(backend="telegram", external_id=str(result["message_id"]), channel=str(self._chat_id))

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        chat_id = message_ref.channel or self._chat_id
        await self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": int(message_ref.external_id), "text": payload.plain_text},
        )
        return message_ref


def build_backend(settings: Settings, client: httpx.AsyncClient) -> NotificationBackend:
    if settings.notification_backend == "discord":
        assert settings.discord_webhook_url is not None
        return DiscordBackend(settings.discord_webhook_url, client)
    if settings.notification_backend == "slack":
        assert settings.slack_bot_token is not None and settings.slack_channel is not None
        return SlackBackend(settings.slack_bot_token, settings.slack_channel, client)
    if settings.notification_backend == "telegram":
        assert settings.telegram_bot_token is not None and settings.telegram_chat_id is not None
        return TelegramBackend(settings.telegram_bot_token, settings.telegram_chat_id, client)
    raise ValueError(f"unknown notification_backend: {settings.notification_backend!r}")


async def publish_or_update(
    backend: NotificationBackend,
    store: StateStore,
    state: StateFile,
    slot: SlotName,
    payload: MessagePayload,
) -> StateFile:
    """Publish a fresh message if the slot is empty (or self-heals after a
    404 / not-found), otherwise update the existing one in place. Persists
    the resulting state atomically before returning it.
    """
    existing_dict = getattr(state, slot)
    if existing_dict is not None:
        ref = MessageRef.from_dict(existing_dict)
        try:
            ref = await backend.update(ref, payload)
        except MessageNotFoundError:
            logger.warning("slot %s: message gone, self-healing with a fresh publish", slot)
            ref = await backend.publish(payload)
    else:
        ref = await backend.publish(payload)

    setattr(state, slot, ref.to_dict())
    store.save(state)
    return state
