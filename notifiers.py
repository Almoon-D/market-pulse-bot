"""Notification backends (Discord/Slack/Telegram) with per-backend char limits."""

import abc
import httpx

from .config import AppSettings, BackendKind
from .logging import get_logger
from .i18n import translate

logger = get_logger()


class NotificationError(RuntimeError):
    pass


class Backend(abc.ABC):
    limit: int = 2000

    @abc.abstractmethod
    def send(self, content: str) -> None: ...


class DiscordBackend(Backend):
    limit = 2000

    def __init__(self, settings: AppSettings) -> None:
        dc = settings.notification_backend.discord
        if dc is None:
            raise NotificationError("Discord credentials missing")
        self.url = f"https://discord.com/api/webhooks/{dc.webhook_id}/{dc.webhook_token}"

    def send(self, content: str) -> None:
        resp = httpx.post(self.url, json={"content": content}, timeout=15.0)
        if resp.status_code >= 400:
            raise NotificationError(f"Discord webhook error {resp.status_code}")


class SlackBackend(Backend):
    limit = 40000

    def __init__(self, settings: AppSettings) -> None:
        sc = settings.notification_backend.slack
        if sc is None:
            raise NotificationError("Slack credentials missing")
        self.token = sc.bot_token

    def send(self, content: str) -> None:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"text": content},
            timeout=15.0,
        )
        data = resp.json()
        if not data.get("ok"):
            raise NotificationError(f"Slack error: {data.get('error')}")


class TelegramBackend(Backend):
    limit = 4096

    def __init__(self, settings: AppSettings) -> None:
        tc = settings.notification_backend.telegram
        if tc is None:
            raise NotificationError("Telegram credentials missing")
        self.url = f"https://api.telegram.org/bot{tc.bot_token}/sendMessage"
        self.chat_id = tc.chat_id

    def send(self, content: str) -> None:
        resp = httpx.post(self.url, json={
            "chat_id": self.chat_id, "text": content,
        }, timeout=15.0)
        if resp.status_code >= 400:
            raise NotificationError(f"Telegram error {resp.status_code}")


def build_backend(settings: AppSettings) -> Backend:
    kind = settings.notification_backend.backend
    if kind == BackendKind.DISCORD:
        return DiscordBackend(settings)
    if kind == BackendKind.SLACK:
        return SlackBackend(settings)
    return TelegramBackend(settings)


def guarded_send(backend: Backend, content: str, language: str) -> bool:
    """Send with char-limit guard; never raises past NotificationError logging."""
    if len(content) > backend.limit:
        logger.warning(translate(language, "msg.limit_exceeded"),
                       extra={"chars": len(content), "limit": backend.limit})
        return False
    try:
        backend.send(content)
        return True
    except NotificationError as exc:
        logger.error("Notification failed: %s", exc)
        return False
