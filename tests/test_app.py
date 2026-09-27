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
