from pathlib import Path

from market_pulse_bot.config import load_exchanges, scaffold_config, validate_exchange_calendars


def test_config_has_26_unique_mics() -> None:
    """Previously 23, not the originally-specified 24: Chi-X Australia and Cboe
    Australia are the same legal entity since the Feb 2022 rebrand
    (completed its tech migration in Mar 2023) -- listing both as
    separate exchanges would double-count one real venue under two
    names/MICs, unlike Euronext Paris/Amsterdam/Milan, which really are
    distinct trading venues.
    """
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    mics = [exchange.mic for exchange in exchanges]
    assert len(exchanges) == 26
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



def test_cboe_australia_uses_current_name_and_operating_mic() -> None:
    """Chi-X Australia rebranded to Cboe Australia in Feb 2022 (completed
    its technology migration in Mar 2023), but its ISO 10383 *operating*
    MIC stayed 'CHIA' -- 'CXA' is only an informal acronym, and 'TMX
    Australia' was never a real entity at all.
    """
    exchanges = {item.mic: item for item in load_exchanges(Path("config/exchanges.yaml"))}
    assert exchanges["CHIA"].name == "Cboe Australia"
    assert exchanges["CHIA"].calendar_type == "synthetic"


def test_americas_calendar_identities() -> None:
    exchanges = {e.mic: e for e in load_exchanges(Path("config/exchanges.yaml"))}
    assert {"XNYS", "XNAS", "XMEX", "XSGO"} <= exchanges.keys()
    assert exchanges["XNAS"].mic == "XNAS"
    assert exchanges["XNAS"].timezone == "America/New_York"
    assert exchanges["XMEX"].currency == "MXN"
    assert exchanges["XSGO"].timezone == "America/Santiago"


def test_nasdaq_owns_the_structured_halt_feed() -> None:
    exchanges = {e.mic: e for e in load_exchanges(Path("config/exchanges.yaml"))}
    assert exchanges["XNAS"].incident_source.type == "structured_feed"
    assert exchanges["XNAS"].incident_source.scope == "single_stock"
    assert exchanges["XNYS"].incident_source.type == "manual"


def test_roster_order_and_display_names() -> None:
    exchanges = load_exchanges(Path("config/exchanges.yaml"))
    by_region: dict[str, list[str]] = {}
    for exchange in exchanges:
        by_region.setdefault(exchange.region, []).append(exchange.name)
    assert list(by_region) == ["America", "Europe", "Asia", "Oceania"]
    assert [exchange.region for exchange in exchanges] == sorted(
        (exchange.region for exchange in exchanges), key=list(by_region).index
    )
    assert by_region["America"] == [
        "NYSE", "Nasdaq", "Toronto Stock Ex.", "B3 Bolsa do Brasil", "Bolsa de Méjico", "Bolsa de Santiago",
    ]
    assert by_region["Europe"] == [
        "London Stock Ex.", "Bolsa de Madrid", "Deutsche Börse Xetra", "Euronext Paris",
        "Euronext Amsterdam", "Euronext Milán", "SIX Swiss Ex.",
    ]
    assert by_region["Asia"] == [
        "Tokyo Stock Ex.", "Hong Kong Ex.", "Shanghai Stock Ex.", "Shenzhen Stock Ex.",
        "BSE India", "NSE India", "Korea Ex.", "Taiwan Stock Ex.", "Singapore Ex.",
    ]
    assert by_region["Oceania"] == [
        "Australian Securities Ex.", "Cboe Australia", "New Zealand", "South Pacific Stock Ex.",
    ]
