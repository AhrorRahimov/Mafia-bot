"""Runtime switch keys, their aliases and a forgiving resolver.

Kept free of aiogram/SQLAlchemy imports on purpose: both the handlers and
the test-suite need these names, and the tests must run without the bot
dependencies installed.

The resolver exists because the raw keys are namespaced (``feature.shop``)
while admins naturally type the short word (``/flags shop``). Rejecting
the short form made the command look broken.
"""
from __future__ import annotations

from typing import Optional

KEY_MAINTENANCE = "maintenance"
KEY_COIN_MULTIPLIER = "coin_multiplier"
KEY_FEATURE_SHOP = "feature.shop"
KEY_FEATURE_DEAD_CHAT = "feature.dead_chat"
KEY_FEATURE_DETECTIVE_SHOOT = "feature.detective_shoot"
KEY_FEATURE_ROLE_CARDS = "feature.role_cards"

# Feature flags exposed in the panel: key -> default (all on).
FEATURE_FLAGS: dict[str, bool] = {
    KEY_FEATURE_SHOP: True,
    KEY_FEATURE_DEAD_CHAT: True,
    KEY_FEATURE_DETECTIVE_SHOOT: True,
    KEY_FEATURE_ROLE_CARDS: True,
}

# Everything an admin may flip, maintenance mode included.
ALL_FLAG_KEYS: tuple[str, ...] = (KEY_MAINTENANCE, *FEATURE_FLAGS)

# Spellings people actually type.
FLAG_ALIASES: dict[str, str] = {
    "tech": KEY_MAINTENANCE,
    "maint": KEY_MAINTENANCE,
    "cards": KEY_FEATURE_ROLE_CARDS,
    "rolecards": KEY_FEATURE_ROLE_CARDS,
    "deadchat": KEY_FEATURE_DEAD_CHAT,
    "dead": KEY_FEATURE_DEAD_CHAT,
    "shoot": KEY_FEATURE_DETECTIVE_SHOOT,
    "detective": KEY_FEATURE_DETECTIVE_SHOOT,
    "detectiveshoot": KEY_FEATURE_DETECTIVE_SHOOT,
}


def resolve_flag_key(raw: str) -> Optional[str]:
    """Return the canonical switch key for whatever the admin typed.

    Accepts the full key, the short word, a few obvious aliases, any
    casing, dashes instead of underscores and a stray leading slash.
    Returns ``None`` when nothing matches.

    >>> resolve_flag_key("feature.shop")
    'feature.shop'
    >>> resolve_flag_key("Shop")
    'feature.shop'
    >>> resolve_flag_key("/dead-chat")
    'feature.dead_chat'
    >>> resolve_flag_key("cards")
    'feature.role_cards'
    >>> resolve_flag_key("maintenance")
    'maintenance'
    >>> resolve_flag_key("nonsense") is None
    True
    """
    key = (raw or "").strip().lstrip("/").lower().replace("-", "_")
    if not key:
        return None
    if key == KEY_MAINTENANCE:
        return KEY_MAINTENANCE
    if key in FEATURE_FLAGS:
        return key
    prefixed = f"feature.{key}"
    if prefixed in FEATURE_FLAGS:
        return prefixed
    return FLAG_ALIASES.get(key.replace("_", "").replace(".", ""))


def flag_default(key: str) -> bool:
    """Default state of a switch (maintenance is off, features are on)."""
    if key == KEY_MAINTENANCE:
        return False
    return FEATURE_FLAGS.get(key, True)


__all__ = [
    "ALL_FLAG_KEYS",
    "FEATURE_FLAGS",
    "FLAG_ALIASES",
    "KEY_COIN_MULTIPLIER",
    "KEY_FEATURE_DEAD_CHAT",
    "KEY_FEATURE_DETECTIVE_SHOOT",
    "KEY_FEATURE_ROLE_CARDS",
    "KEY_FEATURE_SHOP",
    "KEY_MAINTENANCE",
    "flag_default",
    "resolve_flag_key",
]
