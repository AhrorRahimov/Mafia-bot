"""Slavic-style plural-form selection.

Three plural forms suffice for Russian/Uzbek and cover English with a
simple one/many split. Form index meaning:

* ``ONE``   — exactly 1 item (ru/uz: 1 игрок; en: 1 player)
* ``FEW``   — 2–4 items   (ru: 2 игрока; uz/en: rolled into MANY)
* ``MANY``  — 5+ items, or 11–14 (ru: 5 игроков; en: 2 players)

Uzbek, like English, has no Slavic-style ``FEW`` form — we map any
count greater than one to ``MANY``.
"""
from __future__ import annotations

from enum import IntEnum


class PluralForm(IntEnum):
    ONE = 0
    FEW = 1
    MANY = 2


def plural_form(lang: str, n: int) -> PluralForm:
    """Return the plural-form index for ``n`` items in ``lang``."""
    n = abs(n)
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return PluralForm.ONE
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            return PluralForm.FEW
        return PluralForm.MANY
    # English, Uzbek and any other language: ONE / MANY only.
    return PluralForm.ONE if n == 1 else PluralForm.MANY
