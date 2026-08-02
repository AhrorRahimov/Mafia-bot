"""Tests for role secrecy: the lawyer must never learn the mafia family,
and the mafia must never be told the lawyer's identity.

These guard the True-Mafia rule that the Адвокат is mafia-aligned but blind.
"""
from __future__ import annotations

from app.game.enums import KILLER_MAFIA_ROLES, MAFIA_SIDE_ROLES, Role
from app.i18n import get_i18n
from app.services.session import GameSession
from app.texts import mafia_extra_for
from tests.conftest import make_session


def _t():
    """A Russian translator (any locale works; we inspect names, not wording)."""
    return get_i18n().translator_for("ru")


def test_lawyer_not_in_killer_mafia():
    """The lawyer must not be a kill-voting role."""
    assert Role.LAWYER in MAFIA_SIDE_ROLES
    assert Role.LAWYER not in KILLER_MAFIA_ROLES


def test_alive_mafia_killers_excludes_lawyer():
    game = make_session({1: Role.MAFIA, 2: Role.DON, 3: Role.LAWYER,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    killers = {p.user_id for p in game.alive_mafia_killers()}
    assert killers == {1, 2}  # lawyer (3) excluded
    assert 3 not in killers


def test_lawyer_role_dm_has_no_teammates_block():
    game = make_session({1: Role.MAFIA, 2: Role.DON, 3: Role.LAWYER,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    extra = mafia_extra_for(_t(), game, 3)
    assert extra == ""  # lawyer learns nobody


def test_mafia_dm_lists_only_killers_not_lawyer():
    game = make_session({1: Role.MAFIA, 2: Role.DON, 3: Role.LAWYER,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    extra = mafia_extra_for(_t(), game, 1)  # player 1 is plain mafia
    # The lawyer's name must NOT appear in the teammates block.
    assert "User3" not in extra
    # The don's name SHOULD appear.
    assert "User2" in extra


def test_don_dm_lists_only_killers_not_lawyer():
    game = make_session({1: Role.MAFIA, 2: Role.DON, 3: Role.LAWYER,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    extra = mafia_extra_for(_t(), game, 2)  # player 2 is the don
    assert "User3" not in extra  # lawyer hidden
    assert "User1" in extra      # plain mafia shown


def test_no_teammates_block_when_solo_mafia():
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN,
                         4: Role.CITIZEN})
    extra = mafia_extra_for(_t(), game, 1)
    assert extra == ""  # nobody to list
