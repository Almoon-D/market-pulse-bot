"""
Loads ``locales/{lang}.yaml`` and resolves ``{key}`` -> formatted string.

Every user-facing string lives in YAML, never as a literal in Python. A
missing key falls back to English with a logged warning rather than
crashing a live notification loop over one untranslated string.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FALLBACK_LANG = "en"


class MissingLocaleFileError(RuntimeError):
    pass


class I18n:
    def __init__(self, locales_dir: Path | str, language: str) -> None:
        self._dir = Path(locales_dir)
        self.language = language
        self._catalogs: dict[str, dict[str, str]] = {}
        self._load(language)
        if language != _FALLBACK_LANG:
            # Always have the fallback ready so a missing key never has to
            # hit the filesystem mid-render.
            self._load(_FALLBACK_LANG)

    def _load(self, lang: str) -> None:
        if lang in self._catalogs:
            return
        path = self._dir / f"{lang}.yaml"
        if not path.exists():
            raise MissingLocaleFileError(f"locale file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        flat = _flatten(data)
        self._catalogs[lang] = flat

    def t(self, key: str, **kwargs: Any) -> str:
        """Translate ``key`` for the configured language, formatting any
        ``{placeholder}`` values in ``kwargs``. Falls back to English (and
        finally to the raw key) rather than raising.
        """
        catalog = self._catalogs.get(self.language, {})
        template = catalog.get(key)
        if template is None:
            logger.warning(
                "missing i18n key %r for language %r — falling back to %r",
                key, self.language, _FALLBACK_LANG,
            )
            template = self._catalogs.get(_FALLBACK_LANG, {}).get(key)
        if template is None:
            logger.warning("missing i18n key %r in fallback language too", key)
            return key
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError) as exc:
            logger.warning("bad placeholder in i18n key %r: %s", key, exc)
            return template


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """``{"status": {"closed": "Cerrado"}}`` -> ``{"status.closed": "Cerrado"}``."""
    out: dict[str, str] = {}
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = str(v)
    return out
