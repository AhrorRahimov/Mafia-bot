"""Rating, seasons and achievement unlocks.

Called once per finished game from the orchestrator. Everything here is
best-effort: a failure while awarding a badge must never prevent the
game-over message from being posted, so the caller wraps this module in a
try/except and logs.

MMR model
---------
A flat Elo-style ladder is overkill for a party game, so the reward is
role-weighted instead: the harder the role is to win with, the bigger the
swing. Losing costs roughly half of what winning pays, which keeps casual
players climbing slowly instead of tumbling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Season
from app.db.progress_repo import (
    AchievementRepo,
    LeaderboardRepo,
    RatingRepo,
    RoleStatRepo,
    SeasonRepo,
)
from app.db.repo import StatsRepo
from app.game.achievements import (
    ACHIEVEMENTS_BY_CODE,
    Achievement,
    PlayerOutcome,
    evaluate,
)
from app.game.enums import Role

logger = logging.getLogger(__name__)

# Base MMR paid for a win, per role. Third-party and information roles
# carry the most risk, plain citizens the least.
ROLE_WEIGHT: dict[Role, int] = {
    Role.CITIZEN: 20,
    Role.MAFIA: 24,
    Role.DETECTIVE: 28,
    Role.DOCTOR: 26,
    Role.DON: 30,
    Role.WHORE: 28,
    Role.SERGEANT: 26,
    Role.LAWYER: 30,
    Role.MANIAC: 36,
}

# A loss costs half of the win value: climbing should be easier than falling.
LOSS_RATIO = 0.5
# Small bonus for staying alive to the end - rewards careful play.
SURVIVOR_MMR = 4
# Coins paid to the top of the ladder when a season closes (1st..10th).
SEASON_REWARDS: tuple[int, ...] = (
    1000, 700, 500, 350, 250, 200, 150, 120, 100, 80,
)
DEFAULT_MMR = 1000


@dataclass
class ProgressResult:
    """What changed for one player after a game."""

    mmr_delta: int = 0
    mmr_total: int = DEFAULT_MMR
    unlocked: tuple[Achievement, ...] = ()
    coins_awarded: int = 0


def mmr_delta(role: Role, *, won: bool, survived: bool) -> int:
    """MMR change for a single player. Never returns zero."""
    base = ROLE_WEIGHT.get(role, 20)
    if won:
        return base + (SURVIVOR_MMR if survived else 0)
    return -max(1, int(round(base * LOSS_RATIO)))


async def current_season(session: AsyncSession) -> Season:
    """Return the open season, rolling over when the month changed."""
    repo = SeasonRepo(session)
    season = await repo.ensure_active()
    if repo.is_stale(season):
        await repo.close(season)
        season = await repo.start_new()
    return season


async def close_season_and_reward(
    session: AsyncSession,
) -> tuple[Optional[Season], list[tuple[int, str, int, int]]]:
    """Close the running season, pay the top 10 and open a fresh one.

    Returns the closed season and the payout table as
    ``(user_id, name, mmr, coins)`` so the caller can announce it.
    """
    repo = SeasonRepo(session)
    season = await repo.active()
    if season is None:
        return None, []
    winners = await RatingRepo(session).top(season.id, limit=len(SEASON_REWARDS))
    stats = StatsRepo(session)
    payouts: list[tuple[int, str, int, int]] = []
    for index, (row, name) in enumerate(winners):
        coins = SEASON_REWARDS[index]
        await stats.add_coins(row.user_id, coins)
        payouts.append((row.user_id, name, int(row.mmr or 0), coins))
    await repo.close(season)
    await repo.start_new()
    return season, payouts


async def rollover_if_needed(
    session: AsyncSession,
) -> tuple[Optional[Season], list[tuple[int, str, int, int]]]:
    """Close last month's season (paying the top 10) when it expired.

    Cheap to call: it only does work on the first game of a new month.
    """
    repo = SeasonRepo(session)
    season = await repo.active()
    if season is None or not repo.is_stale(season):
        return None, []
    return await close_season_and_reward(session)


async def apply_game_result(
    session: AsyncSession,
    *,
    user_id: int,
    outcome: PlayerOutcome,
) -> ProgressResult:
    """Persist MMR, role stats and any achievements for one player."""
    season = await current_season(session)
    delta = mmr_delta(outcome.role, won=outcome.won, survived=outcome.survived)
    total = await RatingRepo(session).apply(
        season.id, user_id, delta, won=outcome.won
    )
    await RoleStatRepo(session).record(
        user_id, outcome.role.value, won=outcome.won
    )

    achievements = AchievementRepo(session)
    owned = await achievements.codes(user_id)
    fresh = evaluate(outcome, owned)

    # "All roles" needs cross-role data, so it is resolved here rather than
    # by a per-game predicate.
    if "all_roles" not in owned:
        played_roles = await RoleStatRepo(session).distinct_roles(user_id)
        if played_roles >= {role.value for role in Role}:
            fresh.append(ACHIEVEMENTS_BY_CODE["all_roles"])

    stats = StatsRepo(session)
    coins = 0
    unlocked: list[Achievement] = []
    for achievement in fresh:
        if not await achievements.unlock(user_id, achievement.code):
            continue
        unlocked.append(achievement)
        if achievement.reward:
            await stats.add_coins(user_id, achievement.reward)
            coins += achievement.reward

    return ProgressResult(
        mmr_delta=delta,
        mmr_total=total,
        unlocked=tuple(unlocked),
        coins_awarded=coins,
    )


async def profile_snapshot(session: AsyncSession, user_id: int) -> dict:
    """Everything ``/me`` needs, in one call."""
    stats = await StatsRepo(session).get(user_id)
    season = await current_season(session)
    ratings = RatingRepo(session)
    rating_row = await ratings.get(season.id, user_id)
    return {
        "stats": stats,
        "season": season,
        "mmr": int(rating_row.mmr) if rating_row else DEFAULT_MMR,
        "season_games": int(rating_row.games) if rating_row else 0,
        "season_wins": int(rating_row.wins) if rating_row else 0,
        "rank": await ratings.rank_of(season.id, user_id),
        "roles": await RoleStatRepo(session).for_user(user_id),
        "achievements": await AchievementRepo(session).codes(user_id),
        "recent": await AchievementRepo(session).recent(user_id, limit=3),
    }


__all__ = [
    "DEFAULT_MMR",
    "LeaderboardRepo",
    "ProgressResult",
    "ROLE_WEIGHT",
    "SEASON_REWARDS",
    "apply_game_result",
    "close_season_and_reward",
    "current_season",
    "mmr_delta",
    "profile_snapshot",
]
