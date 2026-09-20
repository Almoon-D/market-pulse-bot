import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .config import AppConfig, save_json_atomic
from .text_formatter import MessagePayload

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageRef:
    backend: str
    message_id: str
    channel_id: str | None = None


class NotificationBackend(ABC):
    def __init__(self, config: AppConfig, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path
        self.client = httpx.AsyncClient(timeout=20.0)
        self.state = self._load_state()

    async def close(self) -> None:
        await self.client.aclose()

    @abstractmethod
    async def publish(self, payload: MessagePayload) -> MessageRef:
        ...

    @abstractmethod
    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        ...

    async def sync(self, payloads: tuple[MessagePayload, ...]) -> None:
        for payload in payloads:
            existing = self._get_ref(payload.key)
            if existing is None:
                ref = await self.publish(payload)
            else:
                try:
                    ref = await self.update(existing, payload)
                except MessageNotFound:
                    self._set_ref(payload.key, None)
                    ref = await self.publish(payload)
            self._set_ref(payload.key, ref)
            self._persist_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"backend": self.config.notification_backend, "events_message_ref": None, "dashboard_americas_eu_ref": None, "dashboard_asia_oceania_ref": None}
        import json
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("backend") != self.config.notification_backend:
                return {"backend": self.config.notification_backend, "events_message_ref": None, "dashboard_americas_eu_ref": None, "dashboard_asia_oceania_ref": None}
            return data
        except (OSError, ValueError):
            LOGGER.warning("invalid state file; starting with empty message references")
            return {"backend": self.config.notification_backend, "events_message_ref": None, "dashboard_americas_eu_ref": None, "dashboard_asia_oceania_ref": None}

    def _get_ref(self, key: str) -> MessageRef | None:
        raw = self.state.get(key)
        if not raw:
            return None
        return MessageRef(raw["backend"], raw["message_id"], raw.get("channel_id"))

    def _set_ref(self, key: str, ref: MessageRef | None) -> None:
        self.state[key] = None if ref is None else {"backend": ref.backend, "message_id": ref.message_id, "channel_id": ref.channel_id}

    def _persist_state(self) -> None:
        save_json_atomic(self.state_path, self.state)


class MessageNotFound(Exception):
    pass


class DiscordBackend(NotificationBackend):
    async def publish(self, payload: MessagePayload) -> MessageRef:
        url = _append_query(self.config.credentials.discord_webhook_url or "", {"wait": "true"})
        response = await _request_with_backoff(self.client, "POST", url, json=_discord_body(payload))
        response.raise_for_status()
        data = response.json()
        return MessageRef("discord", str(data["id"]))

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        webhook = self.config.credentials.discord_webhook_url or ""
        parsed = urlsplit(webhook)
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) < 4:
            raise ValueError("Discord webhook URL has an unexpected path")
        webhook_id, token = parts[-2], parts[-1]
        url = f"{parsed.scheme}://{parsed.netloc}/api/webhooks/{webhook_id}/{token}/messages/{message_ref.message_id}"
        response = await _request_with_backoff(self.client, "PATCH", url, json=_discord_body(payload))
        if response.status_code == 404:
            raise MessageNotFound
        response.raise_for_status()
        return message_ref


def _discord_body(payload: MessagePayload) -> dict[str, Any]:
    if fields:
        field_objects = []
        for index, value in enumerate(payload.fields, 1):
            name, field_value = value
            field_objects.append({"name": name or f"Section {index}", "value": field_value, "inline": False})
        return {"embeds": [{"title": payload.title, "fields": field_objects}]}
    return {"embeds": [{"title": payload.title, "description": payload.text}]}


class SlackBackend(NotificationBackend):
    async def publish(self, payload: MessagePayload) -> MessageRef:
        response = await _request_with_backoff(self.client, "POST", "https://slack.com/api/chat.postMessage", headers=self._headers(), json={"channel": self.config.credentials.slack_channel_id, "text": payload.text})
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack chat.postMessage failed: {data}")
        return MessageRef("slack", str(data["ts"]), self.config.credentials.slack_channel_id)

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        response = await _request_with_backoff(self.client, "POST", "https://slack.com/api/chat.update", headers=self._headers(), json={"channel": message_ref.channel_id, "ts": message_ref.message_id, "text": payload.text})
        data = response.json()
        if not data.get("ok"):
            if data.get("error") == "message_not_found":
                raise MessageNotFound
            raise RuntimeError(f"Slack chat.update failed: {data}")
        return message_ref

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.credentials.slack_bot_token}", "Content-Type": "application/json; charset=utf-8"}


class TelegramBackend(NotificationBackend):
    async def publish(self, payload: MessagePayload) -> MessageRef:
        url = f"https://api.telegram.org/bot{self.config.credentials.telegram_bot_token}/sendMessage"
        response = await _request_with_backoff(self.client, "POST", url, json={"chat_id": self.config.credentials.telegram_chat_id, "text": payload.text})
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {data}")
        return MessageRef("telegram", str(data["result"]["message_id"]), str(self.config.credentials.telegram_chat_id))

    async def update(self, message_ref: MessageRef, payload: MessagePayload) -> MessageRef:
        url = f"https://api.telegram.org/bot{self.config.credentials.telegram_bot_token}/editMessageText"
        response = await _request_with_backoff(self.client, "POST", url, json={"chat_id": message_ref.channel_id, "message_id": int(message_ref.message_id), "text": payload.text})
        data = response.json()
        if not data.get("ok"):
            description = str(data.get("description", ""))
            if "message is not modified" in description.lower() or "message to edit not found" in description.lower():
                return message_ref
            if "not found" in description.lower():
                raise MessageNotFound
            raise RuntimeError(f"Telegram editMessageText failed: {data}")
        return message_ref


async def _request_with_backoff(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    delay = 1.0
    for attempt in range(6):
        response = await client.request(method, url, **kwargs)
        if response.status_code != 429:
            return response
        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else delay
        await asyncio.sleep(wait)
        delay *= 2
    return response


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query.update(params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def build_backend(config: AppConfig, state_path: Path) -> NotificationBackend:
    if config.notification_backend == "discord":
        if not config.credentials.discord_webhook_url:
            raise ValueError("credentials.discord_webhook_url is required for Discord")
        return DiscordBackend(config, state_path)
    if config.notification_backend == "slack":
        if not config.credentials.slack_bot_token or not config.credentials.slack_channel_id:
            raise ValueError("Slack bot token and channel id are required")
        return SlackBackend(config, state_path)
    if not config.credentials.telegram_bot_token or not config.credentials.telegram_chat_id:
        raise ValueError("Telegram bot token and chat id are required")
    return TelegramBackend(config, state_path)
