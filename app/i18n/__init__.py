"""Internationalisation (i18n) package.

Provides per-user language resolution and a lightweight JSON-backed
translation layer. Locales live under ``app/locales/<lang>.json``.
"""
from __future__ import annotations

from app.i18n.manager import I18nManager, Translator, get_i18n, plural_kwargs
from app.i18n.plurals import PluralForm

__all__ = ["I18nManager", "PluralForm", "Translator", "get_i18n", "plural_kwargs"]
