"""Market-wide closure detection: structured feed, RSS keywords, manual escalation."""

import re

import httpx

from .config import ExchangeConfig, IncidentType, FeedScope
from .logging import get_logger
from .state import Phase

logger = get_logger()

MARKET_WIDE_RSS_KEYWORDS = {
    "en": ["market-wide halt", "trading halted", "market closed"],
    "es": ["halcón de mercado", "halt de mercado", "bolsa cerrada",
           "suspensión de cotización general"],
    "de": ["handelsaussetzung", "marktweite unterbrechung"],
    "fr": ["suspension des cotations", "marché fermé"],
    "ja": ["全市場停止", "売買停止"],
    "pt": ["suspensão de negociações", "mercado fechado"],
}

_MIN_NON_CONTIGUOUS_SYMBOLS = 2


def _is_market_wide_symbol_set(symbols: list[str]) -> bool:
    """≥2 non-contiguous symbols => suspected market-wide closure."""
    if len(symbols) < _MIN_NON_CONTIGUOUS_SYMBOLS:
        return False
    return len(set(symbols)) >= _MIN_NON_CONTIGUOUS_SYMBOLS


def _rss_matches(text: str, lang: str) -> bool:
    keywords = MARKET_WIDE_RSS_KEYWORDS.get(lang, MARKET_WIDE_RSS_KEYWORDS["en"])
    lowered = text.lower()
    return any(re.search(re.escape(k), lowered) for k in keywords)


def check_incident(cfg: ExchangeConfig, state, language: str) -> Phase | None:
    """Return an incident Phase if one is detected, else None."""
    src = cfg.incident_source

    if src.type == IncidentType.STRUCTURED_FEED and src.url:
        try:
            resp = httpx.get(src.url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Feed fetch failed for %s: %s", cfg.mic, exc)
            return None
        symbols = [item.get("symbol") for item in data.get("halts", [])
                   if item.get("symbol")]
        if src.scope == FeedScope.MARKET_WIDE and _is_market_wide_symbol_set(symbols):
            return Phase.EXCEPTIONAL
        return None

    if src.type == IncidentType.RSS_KEYWORD and src.url:
        try:
            resp = httpx.get(src.url, timeout=10.0)
            resp.raise_for_status()
            body = resp.text
        except httpx.HTTPError as exc:
            logger.warning("RSS fetch failed for %s: %s", cfg.mic, exc)
            return None
        if _rss_matches(body, language):
            return Phase.EXCEPTIONAL
        return None

    return None


def escalate_manual(state, now) -> None:
    """Operator escalation — applies immediately on next poll."""
    state.exceptional_since = now


def clear_manual(state) -> None:
    """Operator clears an incident; detector resumes normal phases."""
    state.exceptional_since = None
    state.active_halt_since = None
