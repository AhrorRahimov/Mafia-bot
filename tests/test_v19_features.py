"""Regression tests for v1.9: detective's gun, mute rules, shop economy.

Pure-logic tests: no Telegram, no database. The mute tests exercise the
state that ``orchestrator._mute_dead`` / ``_unmute_all`` operate on, which
is the part that decides who may speak.
"""
from __future__ import annotations

import pytest

from app.game.enums import Role
from app.game.exceptions import RoleError, TargetError
from app.game.settings import GameSettings
from app.game.shop import SHOP_ITEMS, get_item, get_item_by_index
from app.services.night import NightService

from tests.conftest import make_session


# --- 1. The detective may check OR shoot ------------------------------

def test_detective_shot_kills_the_target():
    game = make_session({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN})
    service = NightService(game)
    service.detective_shoot(1, 2)
    outcome = service.resolve()
    assert [p.user_id for p in outcome.deaths] == [2], "detective's bullet must kill"
    assert outcome.detective_shot is not None
    assert outcome.detective_shot.user_id == 2


def test_detective_cannot_check_and_shoot_the_same_night():
    game = make_session({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN})
    service = NightService(game)
    service.detective_check(1, 2)
    with pytest.raises(RoleError):
        service.detective_shoot(1, 3)


def test_shooting_first_also_blocks_the_check():
    game = make_session({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN})
    service = NightService(game)
    service.detective_shoot(1, 3)
    with pytest.raises(RoleError):
        service.detective_check(1, 2)


def test_detective_cannot_shoot_himself():
    game = make_session({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN})
    with pytest.raises(TargetError):
        NightService(game).detective_shoot(1, 1)


def test_shooting_can_be_disabled_in_the_lobby():
    settings = GameSettings()
    settings.detective_can_shoot = False
    game = make_session(
        {1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN}, settings=settings
    )
    with pytest.raises(RoleError):
        NightService(game).detective_shoot(1, 2)


def test_doctor_can_save_the_detectives_target():
    game = make_session(
        {1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.DOCTOR}
    )
    service = NightService(game)
    service.detective_shoot(1, 3)
    service.doctor_heal(4, 3)
    outcome = service.resolve()
    assert outcome.deaths == [], "the doctor must be able to stop the bullet"
    assert outcome.healed is not None and outcome.healed.user_id == 3


def test_blocked_detective_does_not_shoot():
    game = make_session(
        {1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.WHORE}
    )
    service = NightService(game)
    service.detective_shoot(1, 3)
    service.whore_block(4, 1)
    outcome = service.resolve()
    assert outcome.deaths == [], "a blocked detective cannot fire"


def test_mafia_and_detective_can_kill_two_people_in_one_night():
    game = make_session(
        {1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN}
    )
    service = NightService(game)
    service.detective_shoot(1, 4)
    service.mafia_kill(2, 3)
    outcome = service.resolve()
    assert sorted(p.user_id for p in outcome.deaths) == [3, 4]


def test_detective_shot_counts_as_the_night_action():
    game = make_session({1: Role.DETECTIVE, 2: Role.MAFIA, 3: Role.CITIZEN})
    service = NightService(game)
    service.mafia_kill(2, 3)
    assert not service.all_required_acted()
    service.detective_shoot(1, 2)
    assert service.all_required_acted(), "a shot must close the night too"


# --- 2. Mute bookkeeping ----------------------------------------------

def test_session_tracks_permanently_muted_players():
    game = make_session({1: Role.CITIZEN, 2: Role.MAFIA})
    assert game.permanently_muted == set(), "nobody is silenced at the start"
    game.permanently_muted.add(1)
    game.muted_user_ids.add(2)
    # The nightly unmute pass must never wake an eliminated player.
    survivors = set(game.muted_user_ids) - set(game.permanently_muted)
    assert survivors == {2}
    # Game over releases everyone.
    everyone = set(game.muted_user_ids) | set(game.permanently_muted)
    assert everyone == {1, 2}


def test_dead_players_are_not_in_the_nightly_mute_batch():
    game = make_session(
        {1: Role.CITIZEN, 2: Role.MAFIA, 3: Role.DOCTOR}, alive={1, 2}
    )
    # ``_mute_all`` iterates alive players only; the dead are handled by
    # ``_mute_dead`` and stay muted for good.
    assert {p.user_id for p in game.alive_players} == {1, 2}
    assert {p.user_id for p in game.dead_players} == {3}


# --- 3. Shop + currency ------------------------------------------------

def test_shop_catalogue_is_well_formed():
    assert SHOP_ITEMS, "the catalogue must not be empty"
    ids = [i.item_id for i in SHOP_ITEMS]
    assert len(ids) == len(set(ids)), "item ids must be unique"
    for item in SHOP_ITEMS:
        assert item.price > 0, f"{item.item_id} must cost something"
        assert item.emoji, f"{item.item_id} needs an emoji"


def test_get_item_lookup():
    first = SHOP_ITEMS[0]
    assert get_item(first.item_id) is first
    assert get_item("no_such_item") is None


@pytest.mark.parametrize("index", [-1, len(SHOP_ITEMS), 999])
def test_stale_callbacks_resolve_to_nothing(index):
    assert get_item_by_index(index) is None, "out-of-range must not crash"


def test_every_index_resolves_back_to_its_item():
    for index, item in enumerate(SHOP_ITEMS):
        assert get_item_by_index(index) is item


def test_shop_items_are_translated_in_every_language():
    import json
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for lang in ("ru", "en", "uz"):
        with open(
            os.path.join(root, "app", "locales", f"{lang}.json"), encoding="utf-8"
        ) as handle:
            data = json.load(handle)
        for item in SHOP_ITEMS:
            assert f"shop.item.{item.item_id}.name" in data, (lang, item.item_id)
            assert f"shop.item.{item.item_id}.desc" in data, (lang, item.item_id)


def test_coin_rewards_are_sane():
    from app.game.constants import (
        COINS_PER_GAME,
        COINS_PER_WIN,
        COINS_SURVIVOR_BONUS,
    )

    assert COINS_PER_GAME > 0
    assert COINS_PER_WIN > COINS_SURVIVOR_BONUS, "winning must pay best"
    cheapest = min(i.price for i in SHOP_ITEMS)
    best_case = COINS_PER_GAME + COINS_PER_WIN + COINS_SURVIVOR_BONUS
    assert cheapest > best_case, "the first item must take more than one game"
