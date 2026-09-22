"""Locale catalog loading and startup completeness validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, str]:
    output: dict[str, str] = {}
    for key, item in value.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            output.update(_flatten(item, full_key))
        else:
            output[full_key] = str(item)
    return output


class MissingLocaleFileError(RuntimeError):
    pass


class LocaleValidationError(RuntimeError):
    pass


class I18n:
    def __init__(self, directory: Path, language: str) -> None:
        self.directory = directory
        self.language = language
        self.catalogs: dict[str, dict[str, str]] = {}
        self._load(language)
        if language != "en":
            self._load("en")
        self.validate_complete()

    def _load(self, language: str) -> None:
        path = self.directory / f"{language}.yaml"
        if not path.exists():
            raise MissingLocaleFileError(f"missing locale file: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise LocaleValidationError(f"locale {language} must be a YAML mapping")
        self.catalogs[language] = _flatten(data)

    def validate_complete(self) -> None:
        english = self.catalogs.get("en", {})
        if not english:
            raise LocaleValidationError("English locale is empty")
        for language, catalog in self.catalogs.items():
            missing = sorted(set(english) - set(catalog))
            if missing:
                raise LocaleValidationError(f"locale {language} missing keys: {missing}")

    def t(self, key: str, **kwargs: object) -> str:
        template = self.catalogs.get(self.language, {}).get(key) or self.catalogs["en"].get(key)
        if template is None:
            raise KeyError(key)
        try:
            return template.format(**kwargs)
        except KeyError as exc:
            raise LocaleValidationError(f"missing placeholder {exc} for locale key {key}") from exc
