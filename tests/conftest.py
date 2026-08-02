"""Shared fixtures: build in-memory ``GameSession`` instances without DB/Telegram.

These tests exercise the *rules*, not the I/O: they construct ``GameSession``
objects directly and drive ``NightService`` / ``DayService`` by hand, so they
run anywhere with no bot token and no database.
"""
from __future__ import annotations

import pytest

from app.game.enums import GamePhase, Role
from app.game.settings import GameSettings
from app.services.session import GameSession, PlayerState


def _player(uid: int, role: Role, *, alive: bool = True, name: str | None = None) -> PlayerState:
    p = PlayerState(user_id=uid, full_name=name or f"User{uid}", role=role)
    p.is_alive = alive
    return p


def make_session(
    roles: dict[int, Role],
    *,
    settings: GameSettings | None = None,
    alive: set[int] | None = None,
    round_number: int = 1,
) -> GameSession:
    """Build a GameSession with the given ``user_id -> role`` mapping.

    ``alive`` optionally marks a subset as dead (default: everyone alive).
    """
    players = {
        uid: _player(uid, role, alive=(alive is None or uid in alive))
        for uid, role in roles.items()
    }
    session = GameSession(
        game_id=1,
        chat_id=-100,
        creator_id=next(iter(roles)),
        players=players,
    )
    session.settings = settings or GameSettings()
    session.round_number = round_number
    session.phase = GamePhase.NIGHT
    return session


@pytest.fixture
def build():
    """Return the session factory for use inside tests."""
    return make_session
