"""Tests for ``app.services.session``: succession, win conditions, AFK."""
from __future__ import annotations

from app.game.enums import Role, Winner
from app.services.session import GameSession
from tests.conftest import make_session


# --- succession -------------------------------------------------------

def test_don_is_promoted_when_don_dies():
    game = make_session({1: Role.DON, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    game.players[1].is_alive = False
    promoted = game.apply_succession()
    new_roles = {p.user_id: p.role for p in game.alive_players}
    assert new_roles[2] is Role.DON
    assert any(r is Role.DON for _, r in promoted)


def test_no_don_promotion_if_setting_off_but_don_was_in_game():
    # Promotion is driven by "a don existed in THIS game", not by the settings
    # flag, so a dead don still hands the badge to a mafia.
    game = make_session({1: Role.DON, 2: Role.MAFIA, 3: Role.CITIZEN, 4: Role.CITIZEN})
    game.players[1].is_alive = False
    promoted = game.apply_succession()
    assert any(r is Role.DON for _, r in promoted)


def test_sergeant_inherits_detective_badge():
    game = make_session({1: Role.DETECTIVE, 2: Role.SERGEANT, 3: Role.MAFIA,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    game.players[1].is_alive = False
    promoted = game.apply_succession()
    new_roles = {p.user_id: p.role for p in game.alive_players}
    assert new_roles[2] is Role.DETECTIVE
    assert any(r is Role.DETECTIVE for _, r in promoted)


def test_no_succession_when_heir_dead_too():
    # Don dies but no plain mafia remains -> no promotion.
    game = make_session({1: Role.DON, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.CITIZEN})
    game.players[1].is_alive = False
    promoted = game.apply_succession()
    assert promoted == []


# --- win conditions ---------------------------------------------------

def test_city_wins_when_all_mafia_dead():
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN, 4: Role.CITIZEN})
    game.players[1].is_alive = False
    assert game.evaluate_winner() is Winner.CITY


def test_mafia_wins_at_parity():
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN})
    assert game.evaluate_winner() is Winner.MAFIA


def test_mafia_does_not_win_while_maniac_alive():
    # Mafia at parity with town but a maniac is still alive -> no mafia win yet.
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.MANIAC})
    assert game.evaluate_winner() is None


def test_maniac_wins_when_alone_with_one_townsman():
    game = make_session({1: Role.MANIAC, 2: Role.CITIZEN})
    assert game.evaluate_winner() is Winner.MANIAC


def test_maniac_does_not_win_with_two_alive_opponents():
    game = make_session({1: Role.MANIAC, 2: Role.CITIZEN, 3: Role.CITIZEN})
    assert game.evaluate_winner() is None


def test_no_winner_mid_game():
    game = make_session({1: Role.MAFIA, 2: Role.MAFIA, 3: Role.CITIZEN,
                         4: Role.CITIZEN, 5: Role.CITIZEN})
    assert game.evaluate_winner() is None


def test_lawyer_counts_as_mafia_side_for_win():
    # Lawyer + 1 town, mafia at parity -> mafia side wins (lawyer included).
    game = make_session({1: Role.LAWYER, 2: Role.CITIZEN})
    assert game.evaluate_winner() is Winner.MAFIA


# --- anti-AFK ---------------------------------------------------------

def test_afk_strikes_accumulate(monkeypatch):
    # Force the limit to 1 so a single miss flags the player as AFK.
    import app.game.constants as const
    monkeypatch.setattr(const, "AFK_STRIKES_LIMIT", 1)
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN,
                         4: Role.DETECTIVE})
    # Mafia player 1 did NOT act this night.
    afk = game.record_night_activity()
    assert any(p.user_id == 1 for p in afk)
    assert game.is_afk(1)


def test_acted_players_are_not_flagged_afk():
    game = make_session({1: Role.MAFIA, 2: Role.CITIZEN, 3: Role.CITIZEN,
                         4: Role.DETECTIVE})
    game.night.acted.add(1)
    game.night.acted.add(4)
    afk = game.record_night_activity()
    assert afk == []
