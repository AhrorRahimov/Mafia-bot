"""Tests for ``app.services.day``: nomination + lynching vote resolution."""
from __future__ import annotations

import pytest

from app.game.constants import SKIP_VOTE_ID
from app.game.enums import GamePhase, Role
from app.game.exceptions import VoteError
from app.game.settings import GameSettings
from app.services.day import DayService
from tests.conftest import make_session


def _to_vote(game) -> None:
    game.phase = GamePhase.DAY_VOTE


def test_simple_majority_lynches(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    _to_vote(game)
    svc = DayService(game)
    svc.cast_vote(1, 3)
    svc.cast_vote(2, 3)
    svc.cast_vote(4, 3)
    svc.cast_vote(3, 1)  # mafia votes back
    result = svc.resolve()
    assert result.lynched is not None and result.lynched.user_id == 3
    assert not result.skipped and not result.tied


def test_tied_vote_nobody_lynched(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.MAFIA})
    _to_vote(game)
    svc = DayService(game)
    svc.cast_vote(1, 3)
    svc.cast_vote(2, 4)
    svc.cast_vote(3, 1)
    svc.cast_vote(4, 2)
    result = svc.resolve()
    assert result.lynched is None
    assert result.tied is True


def test_skip_wins_when_skip_has_majority(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    _to_vote(game)
    svc = DayService(game)
    svc.cast_vote(1, SKIP_VOTE_ID)
    svc.cast_vote(2, SKIP_VOTE_ID)
    svc.cast_vote(3, 4)
    result = svc.resolve()
    assert result.skipped is True
    assert result.lynched is None


def test_skip_disabled_rejects_skip(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    game.settings = GameSettings(allow_skip_vote=False)
    _to_vote(game)
    svc = DayService(game)
    with pytest.raises(VoteError):
        svc.cast_vote(1, SKIP_VOTE_ID)


def test_cannot_vote_self(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    _to_vote(game)
    svc = DayService(game)
    with pytest.raises(VoteError):
        svc.cast_vote(1, 1)


def test_cannot_vote_dead_target(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN},
                  alive={1, 2, 3})
    _to_vote(game)
    svc = DayService(game)
    with pytest.raises(VoteError):
        svc.cast_vote(1, 4)


def test_nomination_restricts_vote(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    game.settings = GameSettings(nomination_mode=True)
    # Nominate player 3 first.
    game.phase = GamePhase.DAY_NOMINATION
    DayService(game).nominate(1, 3)
    # Move to vote.
    _to_vote(game)
    game.day.nominations = {1: 3}  # retained through the transition
    svc = DayService(game)
    # Voting for a non-nominated player must be rejected.
    with pytest.raises(VoteError):
        svc.cast_vote(2, 4)
    # Voting for the nominated player is fine.
    svc.cast_vote(2, 3)


def test_vote_can_be_changed_until_phase_ends(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    _to_vote(game)
    svc = DayService(game)
    svc.cast_vote(1, 3)
    svc.cast_vote(1, 2)  # change of mind
    result = svc.resolve()
    # Only player 1's vote counted and it ended on player 2.
    assert result.lynched is None or result.lynched.user_id == 2


def test_all_required_voted(build):
    game = build({1: Role.CITIZEN, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    _to_vote(game)
    svc = DayService(game)
    assert svc.all_required_voted() is False
    svc.cast_vote(1, 3)
    svc.cast_vote(2, 3)
    svc.cast_vote(3, 1)
    svc.cast_vote(4, 3)
    assert svc.all_required_voted() is True
