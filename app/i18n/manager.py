"""I18n manager: loads JSON locales and serves translations.

Design goals
------------
* No external runtime dependencies beyond the standard library.
* JSON files are loaded once at startup and cached.
* Translations support ``str.format`` interpolation::

      t("lobby.count", count=5)   # "Игроков: 5"

* Pluralised strings are encoded in the JSON as a pipe-separated list
  of three forms (ONE | FEW | MANY); English and Uzbek simply repeat
  the MANY form for the unused FEW slot::

      "players_count": "{count} игрок|{count} игрока|{count} игроков"

  Usage: ``t("players_count", count=5)`` picks the right form.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from app.i18n.plurals import PluralForm, plural_form

logger = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
_PLURAL_SEPARATOR = "|"


class Translator:
    """Callable translator bound to a fixed language.

    Exposed to handlers as ``data["t"]`` so they can call ``t(key, **kw)``
    without worrying about the user's language — it is already baked in.
    """

    __slots__ = ("_manager", "_lang")

    def __init__(self, manager: "I18nManager", lang: str) -> None:
        self._manager = manager
        self._lang = lang

    @property
    def lang(self) -> str:
        return self._lang

    def __call__(self, key: str, /, **kwargs: object) -> str:
        """Translate ``key``, interpolating ``kwargs`` into the string.

        ``key`` is positional-only on purpose: locale strings are free to
        contain a ``{key}`` placeholder (the feature-flag listings do),
        and without this a call like ``t("admin.flags_line", key=...)``
        would raise "got multiple values for argument 'key'" instead of
        rendering. The same protects ``{lang}`` and ``{self}``.
        """
        return self._manager.translate(self._lang, key, **kwargs)


class I18nManager:
    """Loads JSON locales and serves translations with fallback."""

    def __init__(
        self, locales_dir: Path, default_lang: str = "ru"
    ) -> None:
        self.default_lang = default_lang
        self._translations: dict[str, dict[str, str]] = {}
        self._load(locales_dir)

    def _load(self, locales_dir: Path) -> None:
        if not locales_dir.exists():
            logger.warning("Locales directory not found: %s", locales_dir)
            return
        for path in sorted(locales_dir.glob("*.json")):
            lang = path.stem
            try:
                with path.open(encoding="utf-8") as fh:
                    self._translations[lang] = json.load(fh)
                logger.info("Loaded locale '%s' (%d keys)",
                            lang, len(self._translations[lang]))
            except (json.JSONDecodeError, OSError):
                logger.exception("Failed to load locale file %s", path)

    @property
    def available_languages(self) -> list[str]:
        """Sorted list of language codes that have a locale file."""
        return sorted(self._translations.keys())

    def translate(self, lang: str, key: str, /, **kwargs: object) -> str:
        """Return the localised string for ``key``.

        Fallback chain: requested lang -> default lang -> key itself.
        """
        raw = self._lookup(lang, key)
        if raw is None:
            # Fall back to default lang, then to the key itself.
            if lang != self.default_lang:
                raw = self._lookup(self.default_lang, key)
            if raw is None:
                logger.warning("i18n key not found: %s", key)
                return key
        return self._render(raw, lang, kwargs)

    # --- internal -----------------------------------------------------

    def _lookup(self, lang: str, key: str) -> str | None:
        bundle = self._translations.get(lang) or self._translations.get(
            self.default_lang
        )
        if bundle is None:
            return None
        return bundle.get(key)

    @staticmethod
    def _render(
        raw: str, lang: str, kwargs: dict[str, object]
    ) -> str:
        """Apply pluralisation (if requested) and ``str.format``.

        Pluralisation is opt-in: a key passed via ``plural_with=...`` asks
        for a specific count to drive form selection. Most strings do
        not need it.
        """
        # Detect a plural request: caller used a reserved kwarg.
        plural_key = kwargs.pop("__plural_key__", None)
        plural_count = kwargs.pop("__plural_count__", None)
        if plural_key is not None and plural_count is not None and _PLURAL_SEPARATOR in raw:
            form = plural_form(lang, int(plural_count))
            forms = raw.split(_PLURAL_SEPARATOR)
            # Clamp to available forms (some locales have only 2).
            index = min(int(form), len(forms) - 1)
            raw = forms[index].strip()
        try:
            return raw.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.exception("i18n interpolation error for raw=%r", raw)
            return raw

    def translator_for(self, lang: str) -> Translator:
        """Return a ``Translator`` bound to ``lang``."""
        return Translator(self, lang)


@lru_cache(maxsize=1)
def get_i18n() -> I18nManager:
    """Return the process-wide ``I18nManager`` singleton."""
    return I18nManager(_LOCALES_DIR, default_lang="ru")


def plural_kwargs(field: str, count: int) -> dict[str, object]:
    """Helper to request plural-form selection for ``field``.

    Pass the returned dict as ``**plural_kwargs("count", n)`` to ``t(...)``.
    """
    return {"__plural_key__": field, "__plural_count__": count}


__all__ = [
    "I18nManager",
    "Translator",
    "get_i18n",
    "plural_kwargs",
    "PluralForm",
]
