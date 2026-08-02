"""Game-related filters.

These filters reach into the injected ``LobbyService`` (key ``games`` in
handler ``data``) to introspect the running game in the chat and the
caller's role/alive status. They keep handlers free of boilerplate
``if not session: ...`` blocks.
"""
from __future__ import annotations

from typing import Any, Union

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from app.game.enums import GamePhase, Role
from app.services.lobby import LobbyService
from app.services.session import PlayerState


class _GameFilter(Filter):
    """Base helper: resolves the running ``GameSession`` for the chat."""

    async def __call__(self, event: Union[Message, CallbackQuery], **data: Any) -> bool:
        games: LobbyService = data["games"]
        chat_id = self._chat_id(event)
        session = games.get(chat_id)
        if session is None:
            return False
        data["session"] = session
        return True

    @staticmethod
    def _chat_id(event: Union[Message, CallbackQuery]) -> int:
        # Callback queries in private chats still carry the chat id.
        return event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id


class InLobby(_GameFilter):
    """Pass when there is an open lobby (status == LOBBY)."""


class GameActive(_GameFilter):
    """Pass when there is a running game session."""


class IsPlayer(_GameFilter):
    """Pass when the caller is a participant of the active game."""

    async def __call__(self, event: Union[Message, CallbackQuery], **data: Any) -> bool:
        if not await super().__call__(event, **data):
            return False
        session = data["session"]
        player = session.get(event.from_user.id)
        if player is None:
            return False
        data["player"] = player
        return True


class IsAlive(IsPlayer):
    """Pass when the caller is an alive participant."""

    async def __call__(self, event: Union[Message, CallbackQuery], **data: Any) -> bool:
        if not await super().__call__(event, **data):
            return False
        return data["player"].is_alive


class IsRole(IsPlayer):
    """Pass when the caller's role matches the configured one."""

    def __init__(self, role: Role) -> None:
        self.role = role

    async def __call__(self, event: Union[Message, CallbackQuery], **data: Any) -> bool:
        if not await super().__call__(event, **data):
            return False
        return data["player"].role is self.role


class IsPhase(_GameFilter):
    """Pass when the active game is in one of the configured phases."""

    def __init__(self, *phases: GamePhase) -> None:
        self.phases = set(phases)

    async def __call__(self, event: Union[Message, CallbackQuery], **data: Any) -> bool:
        if not await super().__call__(event, **data):
            return False
        return data["session"].phase in self.phases


__all__ = [
    "GameActive",
    "InLobby",
    "IsAlive",
    "IsPlayer",
    "IsRole",
    "IsPhase",
    "PlayerState",
]
