"""Middleware that injects shared services into handler ``data``.

Services (``LobbyService``, ``TimerManager``, ``MessageTracker``) are
process-wide singletons that manage all games. Wiring them here means
handlers can declare ``games: LobbyService`` as a parameter without
touching global state.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.cleanup import MessageTracker
from app.services.lobby import LobbyService
from app.services.timer import TimerManager


class ServicesMiddleware(BaseMiddleware):
    """Expose shared services under stable keys in handler ``data``."""

    def __init__(
        self,
        games: LobbyService,
        timers: TimerManager,
        tracker: MessageTracker,
    ) -> None:
        self._games = games
        self._timers = timers
        self._tracker = tracker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["games"] = self._games
        data["timers"] = self._timers
        data["tracker"] = self._tracker
        return await handler(event, data)
