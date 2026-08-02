"""Tests for ``app.services.night``: night-action validation and resolution."""
from __future__ import annotations

import pytest

from app.game.enums import Role
from app.game.exceptions import RoleError, TargetError
from app.services.night import NightService
from tests.conftest import make_session


# --- role-gating ------------------------------------------------------

def test_mafia_cannot_kill_teammate(build):
    game = build({1: Role.MAFIA, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.DETECTIVE})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.mafia_kill(1, 2)


def test_mafia_cannot_target_dead(build):
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.DETECTIVE},
                  alive={1, 2, 4})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.mafia_kill(1, 3)


def test_detective_cannot_check_self(build):
    game = build({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.DOCTOR})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.detective_check(1, 1)


def test_doctor_self_heal_only_once(build):
    game = build({1: Role.DOCTOR, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    svc = NightService(game)
    svc.doctor_heal(1, 1)  # first self-heal is allowed
    outcome = svc.resolve()
    # After resolution the doctor used his one self-heal.
    assert game.doctor_self_heal_used
    _ = outcome
    # Next night:
    game.begin_night()
    svc2 = NightService(game)
    with pytest.raises(TargetError):
        svc2.doctor_heal(1, 1)


def test_doctor_cannot_heal_same_target_twice(build):
    game = build({1: Role.DOCTOR, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    svc = NightService(game)
    svc.doctor_heal(1, 3)
    svc.resolve()
    game.begin_night()
    svc2 = NightService(game)
    with pytest.raises(TargetError):
        svc2.doctor_heal(1, 3)  # same target as last night


def test_whore_cannot_block_same_target_twice(build):
    game = build({1: Role.WHORE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.whore_block(1, 2)
    svc.resolve()
    game.begin_night()
    svc2 = NightService(game)
    with pytest.raises(TargetError):
        svc2.whore_block(1, 2)


def test_maniac_cannot_kill_self(build):
    game = build({1: Role.MANIAC, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.maniac_kill(1, 1)


def test_lawyer_cannot_defend_self(build):
    game = build({1: Role.LAWYER, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.lawyer_defend(1, 1)


def test_don_cannot_search_self(build):
    game = build({1: Role.DON, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    with pytest.raises(TargetError):
        svc.don_search(1, 1)


def test_double_action_same_night_rejected(build):
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.DETECTIVE})
    svc = NightService(game)
    svc.mafia_kill(1, 2)
    with pytest.raises(RoleError):
        svc.mafia_kill(1, 3)


# --- resolution -------------------------------------------------------

def test_mafia_kill_resolves_in_death(build):
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.DETECTIVE})
    svc = NightService(game)
    svc.mafia_kill(1, 2)
    outcome = svc.resolve()
    assert [p.user_id for p in outcome.deaths] == [2]


def test_doctor_save_prevents_death(build):
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.DOCTOR,
                  5: Role.DETECTIVE})
    svc = NightService(game)
    svc.mafia_kill(1, 2)
    svc.doctor_heal(4, 2)
    outcome = svc.resolve()
    assert outcome.deaths == []
    assert outcome.healed is not None and outcome.healed.user_id == 2


def test_detective_verdict_mafia(build):
    game = build({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    svc = NightService(game)
    svc.detective_check(1, 2)
    outcome = svc.resolve()
    assert outcome.detective_suspect.user_id == 2
    assert outcome.detective_is_mafia is True


def test_detective_verdict_clean(build):
    game = build({1: Role.DETECTIVE, 2: Role.CITIZEN, 3: Role.MAFIA, 4: Role.CITIZEN})
    svc = NightService(game)
    svc.detective_check(1, 2)
    outcome = svc.resolve()
    assert outcome.detective_is_mafia is False


def test_lawyer_disguise_flips_verdict(build):
    # The lawyer masks a mafia member so the detective reads "clean".
    game = build({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.LAWYER, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.detective_check(1, 2)
    svc.lawyer_defend(3, 2)
    outcome = svc.resolve()
    assert outcome.detective_is_mafia is False  # masked


def test_whore_blocks_detective(build):
    # The whore blocks the detective -> no verdict is produced.
    game = build({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.WHORE, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.detective_check(1, 2)
    svc.whore_block(3, 1)
    outcome = svc.resolve()
    assert outcome.detective_suspect is None


def test_whore_blocks_mafia_voter(build):
    # Only one mafia votes; if blocked, no mafia kill happens.
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.WHORE})
    svc = NightService(game)
    svc.mafia_kill(1, 2)
    svc.whore_block(5, 1)
    outcome = svc.resolve()
    assert outcome.deaths == []  # blocked mafia -> no kill


def test_maniac_and_mafia_same_target_counted_once(build):
    game = build({1: Role.MAFIA, 2: Role.MANIAC, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.mafia_kill(1, 3)
    svc.maniac_kill(2, 3)
    outcome = svc.resolve()
    assert [p.user_id for p in outcome.deaths] == [3]  # single death, not two


def test_maniac_and_mafia_two_targets_two_deaths(build):
    game = build({1: Role.MAFIA, 2: Role.MANIAC, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.mafia_kill(1, 3)
    svc.maniac_kill(2, 4)
    outcome = svc.resolve()
    deaths = sorted(p.user_id for p in outcome.deaths)
    assert deaths == [3, 4]


def test_don_decides_family_target(build):
    # Don + mafia disagree; the don's pick wins.
    game = build({1: Role.DON, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.mafia_kill(2, 4)   # plain mafia votes 4
    svc.mafia_kill(1, 3)   # don votes 3 -> decisive
    outcome = svc.resolve()
    assert [p.user_id for p in outcome.deaths] == [3]


def test_don_search_finds_detective(build):
    game = build({1: Role.DON, 2: Role.DETECTIVE, 3: Role.CITIZEN, 4: Role.CITIZEN,
                  5: Role.CITIZEN})
    svc = NightService(game)
    svc.don_search(1, 2)
    outcome = svc.resolve()
    assert outcome.don_check is not None
    searched, found = outcome.don_check
    assert searched.user_id == 2
    assert found is True


def test_all_required_acted_false_then_true(build):
    game = build({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.DETECTIVE})
    svc = NightService(game)
    assert svc.all_required_acted() is False
    svc.mafia_kill(1, 2)
    assert svc.all_required_acted() is False  # detective still owes a check
    svc.detective_check(4, 1)
    assert svc.all_required_acted() is True
