import asyncio
from pathlib import Path

from market_pulse_bot.notification_backend import (
    DiscordRef,
    MessageNotFoundError,
    NotificationBackend,
    StateFile,
    StateStore,
    publish_or_update,
)
from market_pulse_bot.text_formatter import MessagePayload


class FakeBackend(NotificationBackend):
    def __init__(self) -> None:
        self.published = 0
        self.fail_next = False
    async def publish(self, payload):
        self.published += 1
        return DiscordRef(backend="discord", message_id=str(self.published + 10))
    async def update(self, reference, payload):
        if self.fail_next:
            self.fail_next = False
            raise MessageNotFoundError("gone")
        return reference


def test_backend_mismatch_discards_all_refs(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(StateFile(backend="discord", events_message_ref=DiscordRef(backend="discord", message_id="1")))
    state = store.load("telegram")
    assert state.backend == "telegram"
    assert state.events_message_ref is None


def test_404_recreates_only_one_slot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = StateFile(
        backend="discord",
        events_message_ref=DiscordRef(backend="discord", message_id="1"),
        dashboard_americas_eu_ref=DiscordRef(backend="discord", message_id="2"),
        dashboard_asia_oceania_ref=DiscordRef(backend="discord", message_id="3"),
    )
    backend = FakeBackend()
    backend.fail_next = True
    payload = MessagePayload("events", "x", [], {"title": "x"})
    state = asyncio.run(publish_or_update(backend, store, state, "events_message_ref", payload))
    assert backend.published == 1
    assert state.events_message_ref.message_id == "11"
    assert state.dashboard_americas_eu_ref.message_id == "2"
    assert state.dashboard_asia_oceania_ref.message_id == "3"



def test_wrong_slot_reference_backend_is_recreated_only_for_that_slot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        '{"backend":"discord","events_message_ref":{"backend":"telegram","chat_id":"-1001","message_id":1}}',
        encoding="utf-8",
    )
    store = StateStore(path)
    state = store.load("discord")
    backend = FakeBackend()
    payload = MessagePayload("events", "x", [], {"title": "x"})
    state = asyncio.run(publish_or_update(backend, store, state, "events_message_ref", payload))
    assert backend.published == 1
    assert state.events_message_ref.backend == "discord"
