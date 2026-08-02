"""Aggregates all handler routers into one, in priority order."""
from __future__ import annotations

from aiogram import Router

from app.handlers import (
    admin,
    admin_panel,
    basic,
    day,
    lobby,
    night,
    private_chat,
    shop,
)


def build_root_router() -> Router:
    """Return the root router with all sub-routers included.

    Order matters: more specific routers (lobby, night, day) come
    before the generic ``basic`` router so that callback actions are
    not shadowed by catch-all handlers.
    """
    root = Router(name="root")
    # Admin routers go first: their commands (/admin, /ban, ...) must not
    # be swallowed by any generic handler, and every one of them checks
    # permissions itself before doing anything.
    root.include_router(admin.router)
    root.include_router(admin_panel.router)
    root.include_router(basic.router)
    root.include_router(lobby.router)
    root.include_router(night.router)
    root.include_router(day.router)
    root.include_router(shop.router)
    # Registered last: a catch-all for ordinary private-chat text (mafia
    # night chat + a lynched player's last word). Must not shadow the
    # commands / inline callbacks handled by the routers above.
    root.include_router(private_chat.router)
    return root
