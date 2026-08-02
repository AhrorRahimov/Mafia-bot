"""Tests for ``app.game.balance``: composition, headcount and parity safety."""
from __future__ import annotations

import pytest

from app.game.balance import assign_roles, get_setup, shuffle_roles
from app.game.enums import Role
from app.game.settings import GameSettings


def test_base_setup_preserves_headcount_for_all_sizes():
    for n in range(4, 11):
        setup = get_setup(n)
        assert setup.total == n, f"headcount broken for {n} players"
        # Detective + doctor always present in the base table.
        assert setup.detective == 1
        assert setup.doctor == 1
        # Town must strictly outnumber mafia at the start.
        town = setup.detective + setup.doctor + setup.citizen
        assert setup.mafia_side < town


@pytest.mark.parametrize("n", range(4, 11))
def test_assign_roles_one_role_per_player(n):
    ids = list(range(1000, 1000 + n))
    roles = assign_roles(ids)
    assert set(roles.keys()) == set(ids)
    assert all(isinstance(r, Role) for r in roles.values())


def test_enable_don_replaces_a_mafia_not_adds():
    s = get_setup(8, GameSettings(enable_don=True))
    # Don replaces exactly one mafia slot; mafia-side headcount unchanged.
    assert s.don == 1
    # mafia_side (mafia+don+lawyer) still equals the base 2.
    assert s.mafia_side == 2
    assert s.total == 8


def test_enable_lawyer_replaces_a_mafia():
    s = get_setup(8, GameSettings(enable_lawyer=True))
    assert s.lawyer == 1
    assert s.mafia_side == 2  # lawyer counts on the mafia side for the win check
    assert s.total == 8


def test_enable_don_and_lawyer_replace_two_mafia():
    s = get_setup(9, GameSettings(enable_don=True, enable_lawyer=True))
    assert s.don == 1
    assert s.lawyer == 1
    assert s.mafia_side == 3  # base 3 mafia -> don + lawyer + 1 plain
    assert s.total == 9


def test_don_and_lawyer_cannot_both_replace_when_only_one_mafia():
    # With 4 players there is only a single mafia slot: enabling both don and
    # lawyer must not corrupt the headcount (the second simply cannot apply).
    s = get_setup(4, GameSettings(enable_don=True, enable_lawyer=True))
    assert s.total == 4
    assert s.mafia_side == 1


def test_town_specialists_replace_citizens():
    s = get_setup(10, GameSettings(enable_whore=True, enable_sergeant=True, enable_maniac=True))
    assert s.whore == 1
    assert s.sergeant == 1
    assert s.maniac == 1
    assert s.total == 10
    # Town lost three citizens but the third-party maniac also appeared.
    assert s.citizen == 5 - 3


def test_sergeant_requires_detective():
    # The sergeant only makes sense while a detective exists; the base table
    # always has one, so enabling him is fine. Sanity check: he is present.
    s = get_setup(6, GameSettings(enable_sergeant=True))
    assert s.sergeant == 1
    assert s.detective == 1


def test_explicit_mafia_count_is_clamped():
    # Town must remain the majority, so a huge requested mafia is clamped.
    s = get_setup(8, GameSettings(mafia_count=10))
    max_allowed = (8 - 1) // 2
    assert s.mafia_side <= max_allowed


def test_shuffle_preserves_multiset():
    setup = get_setup(7)
    roles = shuffle_roles(setup)
    assert sorted(r.value for r in roles) == sorted(
        r.value for r in setup.to_list()
    )


def test_unsupported_player_count_raises():
    with pytest.raises(ValueError):
        get_setup(3)
    with pytest.raises(ValueError):
        get_setup(11)
