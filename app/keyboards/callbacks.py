"""Callback-data schema used across inline keyboards.

We use a ``<action>:<arg>`` payload format. ``arg`` is usually the
``user_id`` of the chosen target, or the ``game_id`` for lobby actions.
Keeping actions in an enum prevents typos in magic strings scattered
throughout the handlers.
"""
from __future__ import annotations

from enum import StrEnum


class CallbackAction(StrEnum):
    """All inline-button callback actions."""

    # Lobby
    JOIN = "join"
    LEAVE = "leave"
    START = "start"
    REMATCH = "rematch"

    # Night
    MAFIA_KILL = "mafia_kill"
    DETECTIVE_CHECK = "detective_check"
    DETECTIVE_SHOOT = "detective_shoot"
    DOCTOR_HEAL = "doctor_heal"
    WHORE_BLOCK = "whore_block"
    DON_SEARCH = "don_search"
    LAWYER_DEFEND = "lawyer_defend"
    MANIAC_KILL = "maniac_kill"

    # Day
    NOMINATE = "nominate"
    VOTE = "vote"

    # Shop
    # Argument is the INDEX of the item in ``app.game.shop.SHOP_ITEMS``,
    # so the payload stays inside Telegram's 64-byte callback limit.
    SHOP_BUY = "shop_buy"
    # Inventory. The argument is the item INDEX in ``SHOP_ITEMS`` as well,
    # so activating a role card stays inside the 64-byte payload limit.
    # Shop navigation: the argument is "<category>:<page>" (e.g. "card:1"),
    # so it is parsed manually and must NOT go through ``parse_callback``.
    SHOP_PAGE = "shop_pg"
    INV_USE = "inv_use"
    INV_CANCEL = "inv_cancel"

    # Leaderboard switcher. The argument is a board name ("season",
    # "coins", ...), so it is parsed manually and must NOT go through
    # ``parse_callback`` (which int()s the argument).
    TOP = "top"

    # Admin panel. The argument is a string route such as "games" or
    # "endgame:-100123", so these callbacks are parsed manually by
    # ``parse_admin_route`` and must NOT go through ``parse_callback``.
    ADMIN = "adm"

    # Settings
    SET_LANG = "set_lang"
    # Lobby settings menu. NOTE: its argument is a string keyword
    # (e.g. "night", "don"), so these callbacks are parsed manually in the
    # handler and must NOT be passed through ``parse_callback`` (which int()s
    # the argument).
    SETTINGS = "settings"


def parse_callback(data: str) -> tuple[CallbackAction, int]:
    """Parse ``<action>:<arg>`` into ``(CallbackAction, int)``.

    Raises:
        ValueError: if the payload is malformed.
    """
    action_str, _, arg_str = data.partition(":")
    return CallbackAction(action_str), int(arg_str)


def parse_admin_route(data: str) -> tuple[str, list[str]]:
    """Split an ``adm:<section>[:<arg>...]`` payload.

    Returns ``(section, args)``. An empty payload yields the root menu,
    so a truncated or stale button can never raise in the handler.

    >>> parse_admin_route("adm:games")
    ('games', [])
    >>> parse_admin_route("adm:endgame:-1001")
    ('endgame', ['-1001'])
    """
    _, _, rest = data.partition(":")
    if not rest:
        return "menu", []
    parts = rest.split(":")
    return parts[0], parts[1:]


def parse_shop_page(data: str) -> tuple[str, int]:
    """Split a ``shop_pg:<category>:<page>`` payload.

    Unknown or malformed payloads fall back to the first page of the
    cosmetics tab, so a stale button can never raise in the handler.

    >>> parse_shop_page("shop_pg:card:2")
    ('card', 2)
    >>> parse_shop_page("shop_pg:junk")
    ('cos', 0)
    """
    parts = data.split(":")
    if len(parts) != 3:
        return "cos", 0
    category = parts[1] if parts[1] in ("cos", "card") else "cos"
    try:
        page = max(0, int(parts[2]))
    except ValueError:
        page = 0
    return category, page
