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
