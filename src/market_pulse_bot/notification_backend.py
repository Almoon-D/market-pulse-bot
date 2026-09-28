"""Notification backends and durable four-slot message state."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from .config import Backend, Settings
from .text_formatter import MessagePayload

logger = logging.getLogger(__name__)

SlotName = Literal[
    "legend_message_ref", "alerts_message_ref", "dashboard_americas_eu_ref", "dashboard_asia_oceania_ref"
]
# Channel order, top to bottom.
SLOT_ORDER: tuple[SlotName, ...] = (
    "legend_message_ref", "alerts_message_ref", "dashboard_americas_eu_ref", "dashboard_asia_oceania_ref"
)


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
    legend_message_ref: StoredReference | None = None
    # Older state files called this slot events_message_ref.
    alerts_message_ref: StoredReference | None = Field(
        default=None, validation_alias=AliasChoices("alerts_message_ref", "events_message_ref")
    )
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


class WebhookGoneError(RuntimeError):
    """The webhook itself is deleted or its token is invalid: retrying cannot help."""


# Longest single rate-limit pause. The render loop runs every 30 s, so a
# longer block would only stall it; the next pass retries instead.
MAX_RATE_LIMIT_WAIT = 60.0


def _header_seconds(response: httpx.Response, name: str) -> float | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return min(max(0.0, float(raw)), MAX_RATE_LIMIT_WAIT)
    except ValueError:
        return None


def _host(url: str) -> str:
    """Only the host: webhook and bot URLs carry secret tokens in their path."""
    return urlsplit(url).netloc


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
            # Discord announces an exhausted bucket up front; wait it out so
            # the next request (e.g. the next of the four messages) isn't a 429.
            if response.headers.get("X-RateLimit-Remaining") == "0":
                wait = _header_seconds(response, "X-RateLimit-Reset-After")
                if wait:
                    await asyncio.sleep(wait)
            return response
        if attempt == retries - 1:
            break
        # Prefer Discord's X-RateLimit-Reset-After (always seconds) over
        # Retry-After, which some APIs report in milliseconds.
        wait = _header_seconds(response, "X-RateLimit-Reset-After")
        if wait is None:
            wait = _header_seconds(response, "Retry-After")
        if wait is None:
            wait = delay
        logger.warning("429 from %s; waiting %.1fs before retry %d/%d", _host(url), wait, attempt + 1, retries)
        await asyncio.sleep(wait)
        delay = min(delay * 2, 30.0)
    raise RuntimeError(f"rate limit persisted after {retries} retries: {_host(url)}")


class NotificationBackend(ABC):
    @abstractmethod
    async def publish(self, payload: MessagePayload) -> StoredReference: ...

    @abstractmethod
    async def update(self, reference: StoredReference, payload: MessagePayload) -> StoredReference: ...

    @abstractmethod
    async def delete(self, reference: StoredReference) -> None:
        """Delete a published message. A message that is already gone counts as deleted."""


DISCORD_UNKNOWN_WEBHOOK = 10015


def _raise_if_webhook_gone(response: httpx.Response) -> None:
    """Tell a deleted webhook or a bad token apart from a deleted message,
    which Discord also answers with 404."""
    if response.status_code in {401, 403}:
        raise WebhookGoneError("Discord rejected the webhook token; update MPB_DISCORD_WEBHOOK_URL")
    if response.status_code == 404:
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            return
        if code == DISCORD_UNKNOWN_WEBHOOK:
            raise WebhookGoneError("the Discord webhook no longer exists; update MPB_DISCORD_WEBHOOK_URL")


class DiscordBackend(NotificationBackend):
    def __init__(self, webhook_url: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(webhook_url.rstrip("/"))
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme != "https" or parsed.netloc not in {"discord.com", "discordapp.com"} or len(parts) < 4 or parts[-3] != "webhooks":
            raise ValueError("Discord webhook URL must contain /api/webhooks/{id}/{token}")
        self._webhook_id = parts[-2]
        self._webhook_token = parts[-1]
        self._client = client

    async def publish(self, payload: MessagePayload) -> DiscordRef:
        if payload.discord_embed is None:
            raise ValueError("Discord payload missing embed")
        response = await request_with_backoff(
            self._client, "POST", f"{self._base_url}?wait=true",
            json={"embeds": [payload.discord_embed]}
        )
        _raise_if_webhook_gone(response)
        response.raise_for_status()
        return DiscordRef(backend="discord", message_id=str(response.json()["id"]))

    async def update(self, reference: StoredReference, payload: MessagePayload) -> DiscordRef:
        if not isinstance(reference, DiscordRef):
            raise TypeError(f"Discord backend cannot update {type(reference).__name__} reference")
        if payload.discord_embed is None:
            raise ValueError("Discord payload missing embed")
        url = self._message_url(reference.message_id)
        response = await request_with_backoff(self._client, "PATCH", url, json={"embeds": [payload.discord_embed]})
        _raise_if_webhook_gone(response)
        if response.status_code == 404:
            raise MessageNotFoundError(f"Discord message {reference.message_id} not found")
        response.raise_for_status()
        return reference

    async def delete(self, reference: StoredReference) -> None:
        if not isinstance(reference, DiscordRef):
            raise TypeError(f"Discord backend cannot delete {type(reference).__name__} reference")
        response = await request_with_backoff(self._client, "DELETE", self._message_url(reference.message_id))
        _raise_if_webhook_gone(response)
        if response.status_code != 404:
            response.raise_for_status()

    def _message_url(self, message_id: str) -> str:
        return f"{self._base_url}/messages/{message_id}"

    @property
    def _base_url(self) -> str:
        # Always the versioned API, whatever form the configured URL has.
        return f"https://discord.com/api/v10/webhooks/{self._webhook_id}/{self._webhook_token}"


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

    async def delete(self, reference: StoredReference) -> None:
        if not isinstance(reference, SlackRef):
            raise TypeError(f"Slack backend cannot delete {type(reference).__name__} reference")
        try:
            await self._call("chat.delete", {"channel": reference.channel_id, "ts": reference.ts})
        except RuntimeError as exc:
            if "message_not_found" not in str(exc):
                raise


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
            # Telegram reports errors as HTTP 4xx with a JSON description;
            # keep the description so callers can recognise specific errors.
            error = str(data.get("description", "unknown_error"))
            if error.lower().startswith("bad request: message is not modified"):
                return None
            raise RuntimeError(f"Telegram {method} failed ({response.status_code}): {error}")
        if not expect_dict_result:
            # Some methods (deleteMessage, pinChatMessage, ...) return a
            # plain boolean `result` on success per the Telegram Bot API,
            # not an object -- there's nothing further to extract here.
            return None
        result = data["result"]
        if not isinstance(result, dict):
            raise TypeError(f"Telegram {method} returned an unexpected result payload")
        return cast(dict[str, object], result)

    async def publish(self, payload: MessagePayload) -> TelegramRef:
        result = await self._call(
            "sendMessage", {"chat_id": self._chat_id, "text": payload.plain_text, "parse_mode": "HTML"}
        )
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
                {
                    "chat_id": reference.chat_id,
                    "message_id": reference.message_id,
                    "text": payload.plain_text,
                    "parse_mode": "HTML",
                },
            )
        except RuntimeError as exc:
            if "message to edit not found" in str(exc).lower():
                raise MessageNotFoundError(str(exc)) from exc
            raise
        return reference

    async def delete(self, reference: StoredReference) -> None:
        if not isinstance(reference, TelegramRef):
            raise TypeError(f"Telegram backend cannot delete {type(reference).__name__} reference")
        try:
            await self._call(
                "deleteMessage",
                {"chat_id": reference.chat_id, "message_id": reference.message_id},
                expect_dict_result=False,
            )
        except RuntimeError as exc:
            error = str(exc).lower()
            if "message to delete not found" in error:
                return
            if "message can't be deleted" in error:
                # Telegram refuses to delete messages older than 48 hours;
                # nothing more can be done about this one.
                logger.warning("Telegram could not delete message %s: %s", reference.message_id, exc)
                return
            raise


def build_backend(settings: Settings, client: httpx.AsyncClient) -> NotificationBackend:
    if settings.notification_backend == "discord":
        assert settings.discord_webhook_url
        return DiscordBackend(settings.discord_webhook_url, client)
    if settings.notification_backend == "slack":
        assert settings.slack_bot_token and settings.slack_channel
        return SlackBackend(settings.slack_bot_token, settings.slack_channel, client)
    assert settings.telegram_bot_token and settings.telegram_chat_id
    return TelegramBackend(settings.telegram_bot_token, settings.telegram_chat_id, client)


async def sync_messages(
    backend: NotificationBackend,
    store: StateStore,
    state: StateFile,
    payloads: Sequence[MessagePayload],
    last_sent: dict[SlotName, MessagePayload],
) -> tuple[StateFile, int]:
    """Keep the four messages published, in SLOT_ORDER, and up to date.

    Existing messages are edited in place; an edit is skipped when the
    payload equals what this process last sent. When a message is missing,
    every message after it is deleted and re-posted along with it, so the
    channel always reads legend, alerts, then the two dashboards. A failed
    delete or publish stops the pass; the next call picks it up again.
    Returns the state and the number of failed operations.
    """
    failures = 0
    first_missing: int | None = None
    for index, (slot, payload) in enumerate(zip(SLOT_ORDER, payloads, strict=True)):
        reference = getattr(state, slot)
        if reference is None or reference.backend != state.backend:
            if reference is not None:
                logger.warning(
                    "%s has reference backend=%s while configured backend=%s; re-posting it",
                    slot, reference.backend, state.backend,
                )
            first_missing = index
            break
        if last_sent.get(slot) == payload:
            continue
        try:
            await backend.update(reference, payload)
        except MessageNotFoundError:
            logger.warning("%s missing; re-posting it and every message after it", slot)
            first_missing = index
            break
        except WebhookGoneError:
            raise
        except Exception:
            failures += 1
            logger.exception("notification update failed for %s", slot)
            continue
        last_sent[slot] = payload
    if first_missing is None:
        return state, failures

    for slot in SLOT_ORDER[first_missing + 1:]:
        reference = getattr(state, slot)
        if reference is not None and reference.backend == state.backend:
            try:
                await backend.delete(reference)
            except WebhookGoneError:
                raise
            except Exception:
                logger.exception("could not delete %s; retrying on the next update", slot)
                return state, failures + 1
        setattr(state, slot, None)
        last_sent.pop(slot, None)
        store.save(state)

    for slot, payload in zip(SLOT_ORDER[first_missing:], payloads[first_missing:], strict=True):
        try:
            reference = await backend.publish(payload)
        except WebhookGoneError:
            raise
        except Exception:
            logger.exception("could not publish %s; retrying on the next update", slot)
            return state, failures + 1
        setattr(state, slot, reference)
        last_sent[slot] = payload
        store.save(state)
    return state, failures
