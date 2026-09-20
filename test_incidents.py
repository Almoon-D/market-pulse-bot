"""Tests for market-wide closure detection."""

from src.incidents import _is_market_wide_symbol_set, MARKET_WIDE_RSS_KEYWORDS


def test_single_symbol_not_market_wide():
    assert not _is_market_wide_symbol_set(["ACME"])


def test_two_non_contiguous_symbols_market_wide():
    assert _is_market_wide_symbol_set(["ACME", "XYZL"])


def test_rss_keywords_multilingual():
    assert "halt de mercado" in MARKET_WIDE_RSS_KEYWORDS
    assert "全市場停止" in MARKET_WIDE_RSS_KEYWORDS
