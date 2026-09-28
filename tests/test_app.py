import pytest

from market_pulse_bot import app


def test_max_runtime_defaults_to_unlimited() -> None:
    args = app.build_parser().parse_args(["--loop"])
    assert args.max_runtime == 0


@pytest.mark.parametrize(
    "argv",
    [["--max-runtime", "60"], ["--loop", "--max-runtime", "-1"], ["send-legend"]],
)
def test_invalid_command_lines_are_rejected(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["market-pulse-bot", *argv])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 2


def test_httpx_request_logging_is_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx logs full request URLs at INFO, and webhook URLs contain secrets."""
    import logging

    monkeypatch.setattr("sys.argv", ["market-pulse-bot", "--max-runtime", "60"])
    with pytest.raises(SystemExit):
        app.main()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_log_formatter_masks_tokens_in_messages_and_tracebacks() -> None:
    import logging

    formatter = app.RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("Client error for url 'https://discord.com/api/v10/webhooks/123/tok-EN_1/messages/9'")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "PATCH %s", ("https://api.telegram.org/bot123:ABC/editMessageText",),
            sys.exc_info(),
        )
    text = formatter.format(record)
    assert "tok-EN_1" not in text and "123:ABC" not in text
    assert "/webhooks/123/***/messages/9" in text and "/bot***/editMessageText" in text



def test_loop_stops_with_exit_code_1_when_the_webhook_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import argparse
    import asyncio
    import os

    from market_pulse_bot.notification_backend import NotificationBackend, WebhookGoneError

    class GoneBackend(NotificationBackend):
        calls = 0

        async def publish(self, payload):
            GoneBackend.calls += 1
            raise WebhookGoneError("the Discord webhook no longer exists; update MPB_DISCORD_WEBHOOK_URL")

        async def update(self, reference, payload):
            raise AssertionError("no stored messages in this test")

        async def delete(self, reference):
            raise AssertionError("no stored messages in this test")

    for key in list(os.environ):
        if key.startswith("MPB_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MPB_NOTIFICATION_BACKEND", "discord")
    monkeypatch.setenv("MPB_DISCORD_WEBHOOK_URL", "fixture")
    monkeypatch.setenv("MPB_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("MPB_MANUAL_INCIDENTS_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(app, "build_backend", lambda settings, client: GoneBackend())

    args = argparse.Namespace(loop=True, interval=1, incident_interval=60, max_runtime=0)
    with caplog.at_level("ERROR"):
        assert asyncio.run(app.async_run(args)) == 1
    assert GoneBackend.calls == 1
    assert "stopping: the Discord webhook no longer exists" in caplog.text
