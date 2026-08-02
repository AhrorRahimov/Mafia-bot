"""Domain-specific exceptions for clean error handling in handlers.

Exceptions carry an i18n **key** and interpolation kwargs instead of
a pre-baked message, so handlers can localise them via ``t(exc.key, **exc.kwargs)``.
The ``CREATOR_LEFT`` sentinel is a special marker (not user-facing) used
to signal that the lobby was dissolved because its creator left.
"""
from __future__ import annotations

from typing import Any

CREATOR_LEFT = "_creator_left_"


class GameError(Exception):
    """Base error for any game-related failure.

    The message is an i18n key; kwargs are interpolation parameters.
    """

    def __init__(self, key: str, **kwargs: Any) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class LobbyError(GameError):
    """Lobby-related precondition failed (full, already joined, etc)."""


class PhaseError(GameError):
    """Action is not allowed in the current phase."""


class RoleError(GameError):
    """Player's role does not permit this action."""


class TargetError(GameError):
    """Selected target is invalid (dead, self, same team, etc)."""


class VoteError(GameError):
    """Voting-related failure (already voted, etc)."""
