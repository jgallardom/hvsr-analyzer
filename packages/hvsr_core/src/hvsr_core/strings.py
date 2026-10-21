"""Centralized user-facing strings (spec 13.1).

Every page text, figure label, footer, and user message lives in `locales/<lang>.json`; code and
templates refer to keys only, so a Spanish catalog can be added without touching them. A missing
key raises instead of falling back to an inline literal.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

DEFAULT_LANG = "en"


@cache
def catalog(lang: str = DEFAULT_LANG) -> dict[str, str]:
    text = resources.files("hvsr_core.locales").joinpath(f"{lang}.json").read_text(encoding="utf-8")
    return json.loads(text)


def t(key: str, lang: str = DEFAULT_LANG, **values) -> str:
    text = catalog(lang)[key]
    return text.format(**values) if values else text


def format_coordinate(value: float | None) -> str:
    """Decimal degrees as stored, at most 6 decimals, no trailing zeros."""
    if value is None:
        return t("value.unknown")
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def results_title(record_code: str | None, latitude: float | None, longitude: float | None) -> str:
    """The principal results title `RECORD_CODE (latitude, longitude)`."""
    if record_code is None:
        return t("title.unregistered")
    return t(
        "title.record",
        code=record_code,
        lat=format_coordinate(latitude),
        lon=format_coordinate(longitude),
    )
