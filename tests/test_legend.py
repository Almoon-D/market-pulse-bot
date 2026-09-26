"""Tests for the --send-legend command across languages and backends."""
from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from market_pulse_bot.app import send_legend
from market_pulse_bot.config import Settings
from market_pulse_bot.i18n import I18n
from market_pulse_bot.notification_backend import DiscordBackend, DiscordRef
from market_pulse_bot.text_formatter import build_legend_payload

LOCALES_DIR = Path("locales")
TEST_SLACK_BOT_TOKEN = "test-slack-bot-token"
TEST_TELEGRAM_BOT_TOKEN = "test-telegram-bot-token"


class FixtureDiscordBackend(DiscordBackend):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._webhook_url = "https://fixture.invalid/webhook"
        self._webhook_id = "fixture"
        self._webhook_token = "fixture"
        self._client = client


@pytest.mark.parametrize("lang", ["en", "es", "de", "fr"])
@pytest.mark.parametrize("backend", ["discord", "slack", "telegram"])
def test_legend_renders_for_every_language_and_backend(lang: str, backend: str) -> None:
    i18n = I18n(LOCALES_DIR, lang)
    payload = build_legend_payload(i18n, backend)

    assert payload.kind == "legend"
    assert payload.plain_text
    if backend == "discord":
        assert payload.discord_embed is not None
    if backend == "slack":
        assert payload.slack_blocks


def test_legend_lists_every_phase_exactly_once() -> None:
    i18n = I18n(LOCALES_DIR, "en")
    payload = build_legend_payload(i18n, "telegram")

    for emoji in ("⚫️", "🟣", "🔵", "🟢", "🔘", "⚪️", "🔶", "♦️", "🚨", "🔷", "🌗"):
        assert payload.plain_text.count(emoji) == 1, f"{emoji} should appear exactly once"


def test_legend_never_shows_the_retired_red_square() -> None:
    i18n = I18n(LOCALES_DIR, "en")

    for backend in ("discord", "slack", "telegram"):
        assert "🟥" not in build_legend_payload(i18n, backend).plain_text


def test_legend_does_not_reuse_a_dashboard_state_slot() -> None:
    """The legend must not share one of the three persistent dashboard slots."""
    source = inspect.getsource(send_legend)

    assert "publish_or_update" not in source
    assert "state_store" not in source


async def _run_send_legend_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> tuple[int, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return await handler(request)

    original_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    for key in list(os.environ):
        if key.startswith("MPB_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    return await send_legend(Settings()), calls


def test_discord_publishes_but_never_attempts_to_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "999"})

    async def run() -> tuple[int, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []

        async def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            return await handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(wrapped)) as client:
            monkeypatch.setattr(
                "market_pulse_bot.app.build_backend",
                lambda settings, _client: FixtureDiscordBackend(client),
            )
            settings = Settings(notification_backend="discord", discord_webhook_url="fixture")
            return await send_legend(settings), calls

    rc, calls = asyncio.run(run())
    assert rc == 0
    assert len(calls) == 1


def test_slack_publishes_and_pins_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("pins.add"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "ts": "1.1", "channel": "C123"})

    rc, calls = asyncio.run(
        _run_send_legend_with_transport(
            monkeypatch,
            {
                "MPB_NOTIFICATION_BACKEND": "slack",
                "MPB_SLACK_BOT_TOKEN": TEST_SLACK_BOT_TOKEN,
                "MPB_SLACK_CHANNEL": "C123",
            },
            handler,
        )
    )

    assert rc == 0
    assert len(calls) == 2


def test_slack_missing_pin_scope_does_not_fail_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("pins.add"):
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        return httpx.Response(200, json={"ok": True, "ts": "1.1", "channel": "C123"})

    rc, _calls = asyncio.run(
        _run_send_legend_with_transport(
            monkeypatch,
            {
                "MPB_NOTIFICATION_BACKEND": "slack",
                "MPB_SLACK_BOT_TOKEN": TEST_SLACK_BOT_TOKEN,
                "MPB_SLACK_CHANNEL": "C123",
            },
            handler,
        )
    )

    assert rc == 0


def test_telegram_pin_uses_boolean_result_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for Telegram's boolean pinChatMessage result."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "pinChatMessage" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})

    rc, calls = asyncio.run(
        _run_send_legend_with_transport(
            monkeypatch,
            {
                "MPB_NOTIFICATION_BACKEND": "telegram",
                "MPB_TELEGRAM_BOT_TOKEN": TEST_TELEGRAM_BOT_TOKEN,
                "MPB_TELEGRAM_CHAT_ID": "-100123",
            },
            handler,
        )
    )

    assert rc == 0
    assert len(calls) == 2


def test_telegram_not_admin_does_not_fail_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "pinChatMessage" in str(request.url):
            return httpx.Response(
                200,
                json={"ok": False, "description": "Bad Request: not enough rights to pin a message"},
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})

    rc, _calls = asyncio.run(
        _run_send_legend_with_transport(
            monkeypatch,
            {
                "MPB_NOTIFICATION_BACKEND": "telegram",
                "MPB_TELEGRAM_BOT_TOKEN": TEST_TELEGRAM_BOT_TOKEN,
                "MPB_TELEGRAM_CHAT_ID": "-100123",
            },
            handler,
        )
    )

    assert rc == 0


def test_discord_pin_never_makes_a_network_call_even_if_attempted() -> None:
    """Discord webhook credentials cannot authenticate to the pins endpoint."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("DiscordBackend.pin() must never make an HTTP request")

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = FixtureDiscordBackend(client)
            return await backend.pin(DiscordRef(backend="discord", message_id="123"))

    assert asyncio.run(run()) is False
