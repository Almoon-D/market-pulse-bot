import asyncio
from pathlib import Path

import httpx
import pytest

from market_pulse_bot.notification_backend import (
    SLOT_ORDER,
    DiscordBackend,
    DiscordRef,
    MessageNotFoundError,
    NotificationBackend,
    SlackBackend,
    SlackRef,
    StateFile,
    StateStore,
    TelegramBackend,
    TelegramRef,
    sync_messages,
)
from market_pulse_bot.text_formatter import MessagePayload

PAYLOADS = [
    MessagePayload(kind, kind, [], {"title": kind})
    for kind in ("legend", "alerts", "dashboard_amer_eu", "dashboard_asia_oc")
]


class FakeBackend(NotificationBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.next_id = 10
        self.missing: set[str] = set()
        self.broken: set[str] = set()

    async def publish(self, payload):
        self.next_id += 1
        self.calls.append(("publish", payload.kind))
        return DiscordRef(backend="discord", message_id=str(self.next_id))

    async def update(self, reference, payload):
        self.calls.append(("update", reference.message_id))
        if reference.message_id in self.broken:
            raise RuntimeError("network down")
        if reference.message_id in self.missing:
            raise MessageNotFoundError("gone")
        return reference

    async def delete(self, reference):
        self.calls.append(("delete", reference.message_id))


def _full_state() -> StateFile:
    return StateFile(
        backend="discord",
        **{slot: DiscordRef(backend="discord", message_id=str(index)) for index, slot in enumerate(SLOT_ORDER, 1)},
    )


def _ids(state: StateFile) -> list[str | None]:
    return [getattr(state, slot).message_id if getattr(state, slot) else None for slot in SLOT_ORDER]


def _sync(backend, store, state, last_sent=None):
    return asyncio.run(sync_messages(backend, store, state, PAYLOADS, {} if last_sent is None else last_sent))


def test_backend_mismatch_discards_all_refs(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(StateFile(backend="discord", alerts_message_ref=DiscordRef(backend="discord", message_id="1")))
    state = store.load("telegram")
    assert state.backend == "telegram"
    assert state.alerts_message_ref is None


def test_existing_messages_are_only_edited(tmp_path: Path) -> None:
    backend = FakeBackend()
    state, failures = _sync(backend, StateStore(tmp_path / "state.json"), _full_state())
    assert failures == 0
    assert backend.calls == [("update", "1"), ("update", "2"), ("update", "3"), ("update", "4")]
    assert _ids(state) == ["1", "2", "3", "4"]


def test_unchanged_payloads_are_not_sent_again(tmp_path: Path) -> None:
    backend = FakeBackend()
    store = StateStore(tmp_path / "state.json")
    last_sent: dict = {}
    state, _ = _sync(backend, store, _full_state(), last_sent)
    backend.calls.clear()
    _sync(backend, store, state, last_sent)
    assert backend.calls == []


def test_missing_middle_message_reposts_it_and_everything_after_it(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.missing = {"2"}
    store = StateStore(tmp_path / "state.json")
    state, failures = _sync(backend, store, _full_state())
    assert failures == 0
    assert backend.calls == [
        ("update", "1"), ("update", "2"),
        ("delete", "3"), ("delete", "4"),
        ("publish", "alerts"), ("publish", "dashboard_amer_eu"), ("publish", "dashboard_asia_oc"),
    ]
    assert _ids(state) == ["1", "11", "12", "13"]
    assert _ids(store.load("discord")) == ["1", "11", "12", "13"]


def test_legacy_three_slot_state_is_rebuilt_in_order(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        '{"backend":"discord",'
        '"events_message_ref":{"backend":"discord","message_id":"101"},'
        '"dashboard_americas_eu_ref":{"backend":"discord","message_id":"102"},'
        '"dashboard_asia_oceania_ref":{"backend":"discord","message_id":"103"}}',
        encoding="utf-8",
    )
    store = StateStore(path)
    state = store.load("discord")
    assert state.alerts_message_ref.message_id == "101"
    backend = FakeBackend()
    state, failures = _sync(backend, store, state)
    assert failures == 0
    assert backend.calls == [
        ("delete", "101"), ("delete", "102"), ("delete", "103"),
        ("publish", "legend"), ("publish", "alerts"), ("publish", "dashboard_amer_eu"), ("publish", "dashboard_asia_oc"),
    ]
    saved = path.read_text(encoding="utf-8")
    assert "events_message_ref" not in saved
    assert _ids(store.load("discord")) == ["11", "12", "13", "14"]


def test_wrong_slot_reference_backend_is_reposted(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _full_state()
    state.dashboard_asia_oceania_ref = TelegramRef(backend="telegram", chat_id="-1001", message_id=1)
    backend = FakeBackend()
    state, _ = _sync(backend, StateStore(path), state)
    assert backend.calls[-1] == ("publish", "dashboard_asia_oc")
    assert state.dashboard_asia_oceania_ref.backend == "discord"


def test_transient_update_error_does_not_block_the_other_messages(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.broken = {"2"}
    state, failures = _sync(backend, StateStore(tmp_path / "state.json"), _full_state())
    assert failures == 1
    assert [call for call in backend.calls if call[0] == "update"] == [
        ("update", "1"), ("update", "2"), ("update", "3"), ("update", "4"),
    ]
    assert _ids(state) == ["1", "2", "3", "4"]


def test_failed_publish_resumes_in_order_on_the_next_pass(tmp_path: Path) -> None:
    class FlakyPublish(FakeBackend):
        fail_on = "dashboard_amer_eu"

        async def publish(self, payload):
            if payload.kind == self.fail_on:
                self.fail_on = ""
                raise RuntimeError("network down")
            return await super().publish(payload)

    backend = FlakyPublish()
    store = StateStore(tmp_path / "state.json")
    state, failures = _sync(backend, store, StateFile(backend="discord"))
    assert failures == 1
    assert _ids(state) == ["11", "12", None, None]
    state, failures = _sync(backend, store, state)
    assert failures == 0
    assert _ids(state) == ["11", "12", "13", "14"]


class FixtureDiscordBackend(DiscordBackend):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._webhook_url = "https://fixture.invalid/webhook"
        self._webhook_id = "123"
        self._webhook_token = "fixture"
        self._client = client


def _discord(handler) -> tuple[DiscordBackend, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FixtureDiscordBackend(client), client


@pytest.mark.parametrize("status", [204, 404])
def test_discord_delete_treats_missing_message_as_deleted(status: int) -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(status)

    async def run() -> None:
        backend, client = _discord(handler)
        async with client:
            await backend.delete(DiscordRef(backend="discord", message_id="55"))

    asyncio.run(run())
    assert seen == [("DELETE", "/api/webhooks/123/fixture/messages/55")]


def test_discord_delete_raises_on_server_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run() -> None:
        backend, client = _discord(handler)
        async with client:
            await backend.delete(DiscordRef(backend="discord", message_id="55"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


def test_slack_delete_ignores_message_not_found() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat.delete")
        return httpx.Response(200, json={"ok": False, "error": "message_not_found"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SlackBackend("test-slack-bot-token", "C123", client).delete(
                SlackRef(backend="slack", channel_id="C123", ts="1.1")
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "description",
    ["Bad Request: message to delete not found", "Bad Request: message can't be deleted"],
)
def test_telegram_delete_tolerates_gone_or_undeletable_messages(description: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("deleteMessage")
        return httpx.Response(400, json={"ok": False, "description": description})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await TelegramBackend("test-telegram-bot-token", "-100123", client).delete(
                TelegramRef(backend="telegram", chat_id="-100123", message_id=7)
            )

    asyncio.run(run())


def test_telegram_messages_are_sent_as_html() -> None:
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend("test-telegram-bot-token", "-100123", client)
            reference = await backend.publish(PAYLOADS[0])
            await backend.update(reference, PAYLOADS[0])

    asyncio.run(run())
    assert len(bodies) == 2
    assert all(b'"parse_mode":"HTML"' in body for body in bodies)


def test_telegram_edit_of_a_deleted_message_is_reported_as_missing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: message to edit not found"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await TelegramBackend("test-telegram-bot-token", "-100123", client).update(
                TelegramRef(backend="telegram", chat_id="-100123", message_id=7), PAYLOADS[0]
            )

    with pytest.raises(MessageNotFoundError):
        asyncio.run(run())
