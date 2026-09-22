from pathlib import Path

from market_pulse_bot.config import load_exchanges, scaffold_config, validate_exchange_calendars


def test_config_has_24_unique_mics() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    mics = [exchange.mic for exchange in exchanges]
    assert len(exchanges) == 24
    assert len(mics) == len(set(mics))


def test_anchor_configuration() -> None:
    exchanges = {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}
    assert exchanges["XNYS"].calendar_type == "exchange_calendars"
    assert exchanges["XMAD"].calendar_type == "exchange_calendars"
    assert exchanges["XTKS"].calendar_type == "exchange_calendars"
    assert exchanges["XSPX"].calendar_type == "synthetic"
    assert exchanges["XSPX"].synthetic is not None
    assert exchanges["XSPX"].synthetic.open_time == "10:00"
    assert exchanges["XMAD"].mic == "XMAD"


def test_init_config_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "exchanges.yaml"
    path.write_text(Path("config/exchanges.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    assert scaffold_config(path) is True
    normalized = path.read_text(encoding="utf-8")
    assert scaffold_config(path) is False
    assert normalized == path.read_text(encoding="utf-8")


def test_calendar_mics_validate() -> None:
    validate_exchange_calendars(load_exchanges(Path("config/exchanges.yaml")))



def test_synthetic_time_format_accepts_valid_clock() -> None:
    exchanges = {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}
    synthetic = exchanges["XSPX"].synthetic
    assert synthetic is not None
    assert synthetic.open_time == "10:00"
    assert synthetic.close_time == "12:00"



def test_tmx_australia_uses_current_name_and_operating_mic() -> None:
    exchanges = {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}
    assert exchanges["CHIA"].name == "TMX Australia Exchange"
    assert exchanges["CHIA"].calendar_type == "synthetic"
