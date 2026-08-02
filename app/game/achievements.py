"""Achievement catalogue.

Achievements are pure data plus a predicate evaluated against a
``PlayerOutcome`` snapshot built at the end of every game. Keeping them
here (instead of scattered ``if`` statements in the orchestrator) means a
new achievement is one tuple, and the unlock logic stays testable without
a bot, a database or a running game.

Each unlock pays out coins once - re-unlocking is impossible because
``user_achievements`` holds one row per (user, code).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.game.enums import MAFIA_SIDE_ROLES, THIRD_PARTY_ROLES, Role, Winner


@dataclass(frozen=True)
class PlayerOutcome:
    """Everything an achievement may look at for one player, one game."""

    role: Role
    winner: Winner
    won: bool
    survived: bool
    rounds: int
    players_total: int
    # Lifetime counters *including* the game that just finished.
    games_played: int
    wins: int
    win_streak: int
    coins: int
    role_games: int
    role_wins: int
    # Per-game facts collected by the orchestrator.
    correct_checks: int = 0
    first_check_found_mafia: bool = False
    heals_landed: int = 0
    kills: int = 0
    blocked_actions: int = 0
    was_lynched: bool = False
    voted_with_majority: int = 0
    last_alive_town: bool = False


@dataclass(frozen=True)
class Achievement:
    """A single unlockable badge."""

    code: str
    reward: int
    check: Callable[[PlayerOutcome], bool] = field(repr=False)
    secret: bool = False


def _is_mafia_side(role: Role) -> bool:
    return role in MAFIA_SIDE_ROLES


# The catalogue. ``code`` doubles as the locale key suffix:
# ``achv.<code>.name`` / ``achv.<code>.desc``.
ACHIEVEMENTS: tuple[Achievement, ...] = (
    # --- first steps ---------------------------------------------------
    Achievement("first_game", 25, lambda o: o.games_played >= 1),
    Achievement("first_win", 50, lambda o: o.won and o.wins >= 1),
    Achievement("veteran_10", 75, lambda o: o.games_played >= 10),
    Achievement("veteran_50", 200, lambda o: o.games_played >= 50),
    Achievement("veteran_100", 500, lambda o: o.games_played >= 100),
    # --- streaks ---------------------------------------------------------
    Achievement("streak_3", 60, lambda o: o.win_streak >= 3),
    Achievement("streak_5", 150, lambda o: o.win_streak >= 5),
    Achievement("streak_10", 400, lambda o: o.win_streak >= 10),
    # --- role wins -------------------------------------------------------
    Achievement("win_citizen", 30, lambda o: o.won and o.role is Role.CITIZEN),
    Achievement("win_mafia", 30, lambda o: o.won and o.role is Role.MAFIA),
    Achievement("win_detective", 40, lambda o: o.won and o.role is Role.DETECTIVE),
    Achievement("win_doctor", 40, lambda o: o.won and o.role is Role.DOCTOR),
    Achievement("win_don", 60, lambda o: o.won and o.role is Role.DON),
    Achievement("win_whore", 60, lambda o: o.won and o.role is Role.WHORE),
    Achievement("win_sergeant", 60, lambda o: o.won and o.role is Role.SERGEANT),
    Achievement("win_lawyer", 70, lambda o: o.won and o.role is Role.LAWYER),
    Achievement("win_maniac", 150, lambda o: o.won and o.role is Role.MANIAC),
    Achievement(
        "all_roles",
        250,
        lambda o: False,  # granted by the service: needs cross-role data
    ),
    # --- skill -----------------------------------------------------------
    Achievement(
        "sharp_eye", 80, lambda o: o.first_check_found_mafia
    ),
    Achievement(
        "profiler", 120, lambda o: o.role is Role.DETECTIVE and o.correct_checks >= 3
    ),
    Achievement(
        "field_surgeon", 100, lambda o: o.heals_landed >= 2
    ),
    Achievement(
        "guardian_angel", 200, lambda o: o.heals_landed >= 3 and o.won
    ),
    Achievement(
        "serial_killer", 150, lambda o: o.role is Role.MANIAC and o.kills >= 3
    ),
    Achievement(
        "godfather", 180, lambda o: o.role is Role.DON and o.won and o.survived
    ),
    Achievement(
        "nightlife", 90, lambda o: o.blocked_actions >= 2
    ),
    Achievement(
        "survivor", 70, lambda o: o.survived and o.rounds >= 4
    ),
    Achievement(
        "last_hope",
        220,
        lambda o: o.last_alive_town and o.won,
    ),
    Achievement(
        "clean_sweep",
        160,
        lambda o: o.won and _is_mafia_side(o.role) and o.rounds <= 2,
    ),
    Achievement(
        "long_night", 90, lambda o: o.rounds >= 7
    ),
    Achievement(
        "full_table", 60, lambda o: o.players_total >= 10
    ),
    Achievement(
        "underdog",
        140,
        lambda o: o.won and o.role in THIRD_PARTY_ROLES,
    ),
    # --- misfortune (still worth a badge) ---------------------------------
    Achievement(
        "scapegoat", 40, lambda o: o.was_lynched and not _is_mafia_side(o.role)
    ),
    Achievement(
        "martyr", 80, lambda o: o.was_lynched and o.won
    ),
    # --- economy ----------------------------------------------------------
    Achievement("rich_1000", 100, lambda o: o.coins >= 1000),
    Achievement("rich_5000", 300, lambda o: o.coins >= 5000),
)

ACHIEVEMENTS_BY_CODE: dict[str, Achievement] = {a.code: a for a in ACHIEVEMENTS}

# Codes handled by the service rather than a per-game predicate.
SPECIAL_CODES = frozenset({"all_roles"})


def evaluate(outcome: PlayerOutcome, unlocked: set[str]) -> list[Achievement]:
    """Return achievements newly unlocked by this outcome.

    ``unlocked`` is the set of codes the player already owns; predicates
    are never re-run for those, so an achievement can never pay twice.
    """
    fresh: list[Achievement] = []
    for achievement in ACHIEVEMENTS:
        if achievement.code in unlocked or achievement.code in SPECIAL_CODES:
            continue
        try:
            if achievement.check(outcome):
                fresh.append(achievement)
        except Exception:  # noqa: BLE001 - a broken predicate must not
            # break the end-of-game flow for everyone else.
            continue
    return fresh
