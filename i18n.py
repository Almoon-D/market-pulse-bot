import logging
from pathlib import Path

import yaml

LOGGER = logging.getLogger(__name__)


class Translator:
    def __init__(self, locale_dir: Path, language: str) -> None:
        self.locale_dir = locale_dir
        self.language = language
        self._en = self._load("en")
        self._selected = self._load(language) if language != "en" else self._en

    def _load(self, language: str) -> dict[str, str]:
        path = self.locale_dir / f"{language}.yaml"
        if not path.exists():
            LOGGER.warning("locale %s missing; falling back to en", language)
            return self._en if language != "en" and hasattr(self, "_en") else {}
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def get(self, key: str, **values: object) -> str:
        value = self._selected.get(key)
        if value is None:
            value = self._en.get(key)
            LOGGER.warning("missing locale key %s for %s; falling back to en", key, self.language)
        if value is None:
            LOGGER.warning("missing locale key %s in en", key)
            return key
        return value.format(**values)
