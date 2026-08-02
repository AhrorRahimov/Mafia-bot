"""Seasons, MMR ratings, per-role history and achievements."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Game,
    Player,
    Season,
    UserAchievement,
    UserRating,
    UserRoleStat,
    UserStats,
)
from app.game.enums import (
    GameStatus,
    MAFIA_SIDE_ROLES,
    Role,
    TOWN_ROLES,
    Winner,
)

DEFAULT_MMR = 1000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def season_name(moment: Optional[datetime] = None) -> str:
    """Seasons are monthly, so the name is simply ``YYYY-MM``."""
    return (moment or _utcnow()).strftime("%Y-%m")


class SeasonRepo:
    """The monthly rating season."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active(self) -> Optional[Season]:
        result = await self._session.execute(
            select(Season)
            .where(Season.is_active.is_(True))
            .order_by(Season.id.desc())
        )
        return result.scalars().first()

    async def start_new(self) -> Season:
        row = Season(name=season_name(), is_active=True)
        self._session.add(row)
        await self._session.flush()
        return row

    async def ensure_active(self) -> Season:
        """Return the open season, creating the very first one if needed."""
        season = await self.active()
        return season if season is not None else await self.start_new()

    def is_stale(self, season: Season) -> bool:
        """True when the season belongs to an earlier month."""
        return (season.name or "") != season_name()

    async def close(self, season: Season) -> None:
        season.is_active = False
        season.finished_at = _utcnow()
        await self._session.flush()

    async def history(self, limit: int = 12) -> Sequence[Season]:
        result = await self._session.execute(
            select(Season).order_by(Season.id.desc()).limit(limit)
        )
        return result.scalars().all()


class RatingRepo:
    """MMR per player per season."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, season_id: int, user_id: int) -> Optional[UserRating]:
        result = await self._session.execute(
            select(UserRating).where(
                UserRating.season_id == season_id,
                UserRating.user_id == user_id,
            )
        )
        return result.scalars().first()

    async def apply(
        self, season_id: int, user_id: int, delta: int, *, won: bool
    ) -> int:
        """Add ``delta`` MMR and return the new value (never below zero)."""
        row = await self.get(season_id, user_id)
        if row is None:
            row = UserRating(
                season_id=season_id, user_id=user_id, mmr=DEFAULT_MMR
            )
            self._session.add(row)
        row.mmr = max(0, int(row.mmr or DEFAULT_MMR) + int(delta))
        row.games = int(row.games or 0) + 1
        row.wins = int(row.wins or 0) + (1 if won else 0)
        row.updated_at = _utcnow()
        await self._session.flush()
        return int(row.mmr)

    async def top(
        self, season_id: int, *, limit: int = 10
    ) -> list[tuple[UserRating, str]]:
        """Best players of the season as ``(rating_row, display name)``."""
        result = await self._session.execute(
            select(UserRating, UserStats.full_name)
            .join(UserStats, UserStats.user_id == UserRating.user_id, isouter=True)
            .where(UserRating.season_id == season_id)
            .order_by(UserRating.mmr.desc(), UserRating.wins.desc())
            .limit(limit)
        )
        return [
            (row, name or str(row.user_id)) for row, name in result.all()
        ]

    async def rank_of(self, season_id: int, user_id: int) -> int:
        """1-based place in the season ladder, or 0 when unranked."""
        row = await self.get(season_id, user_id)
        if row is None:
            return 0
        result = await self._session.execute(
            select(func.count()).select_from(UserRating).where(
                UserRating.season_id == season_id,
                UserRating.mmr > row.mmr,
            )
        )
        return int(result.scalar_one() or 0) + 1


class RoleStatRepo:
    """How each player performs with each role, across all seasons."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, user_id: int, role: str, *, won: bool) -> None:
        result = await self._session.execute(
            select(UserRoleStat).where(
                UserRoleStat.user_id == user_id, UserRoleStat.role == role
            )
        )
        row = result.scalars().first()
        if row is None:
            row = UserRoleStat(user_id=user_id, role=role)
            self._session.add(row)
        row.games = int(row.games or 0) + 1
        row.wins = int(row.wins or 0) + (1 if won else 0)
        await self._session.flush()

    async def for_user(self, user_id: int) -> list[UserRoleStat]:
        result = await self._session.execute(
            select(UserRoleStat)
            .where(UserRoleStat.user_id == user_id)
            .order_by(UserRoleStat.games.desc())
        )
        return list(result.scalars().all())

    async def distinct_roles(self, user_id: int) -> set[str]:
        rows = await self.for_user(user_id)
        return {row.role for row in rows if int(row.games or 0) > 0}


class AchievementRepo:
    """Unlocked achievements (one row per player per code)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def codes(self, user_id: int) -> set[str]:
        result = await self._session.execute(
            select(UserAchievement.code).where(
                UserAchievement.user_id == user_id
            )
        )
        return set(result.scalars().all())

    async def unlock(self, user_id: int, code: str) -> bool:
        """Grant an achievement. False when the player already had it."""
        existing = await self._session.execute(
            select(UserAchievement.id).where(
                UserAchievement.user_id == user_id,
                UserAchievement.code == code,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False
        self._session.add(UserAchievement(user_id=user_id, code=code))
        await self._session.flush()
        return True

    async def recent(
        self, user_id: int, limit: int = 3
    ) -> list[UserAchievement]:
        result = await self._session.execute(
            select(UserAchievement)
            .where(UserAchievement.user_id == user_id)
            .order_by(UserAchievement.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count(self, user_id: int) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(UserAchievement).where(
                UserAchievement.user_id == user_id
            )
        )
        return int(result.scalar_one() or 0)


class LeaderboardRepo:
    """All-time boards that do not depend on the current season."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_wins(self, limit: int = 10) -> list[UserStats]:
        result = await self._session.execute(
            select(UserStats)
            .order_by(UserStats.wins.desc(), UserStats.games_played.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def by_coins(self, limit: int = 10) -> list[UserStats]:
        result = await self._session.execute(
            select(UserStats).order_by(UserStats.coins.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def by_streak(self, limit: int = 10) -> list[UserStats]:
        result = await self._session.execute(
            select(UserStats)
            .order_by(UserStats.best_streak.desc(), UserStats.wins.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def by_winrate(
        self, limit: int = 10, *, min_games: int = 5
    ) -> list[UserStats]:
        """Winrate board, ignoring accounts with too few games."""
        result = await self._session.execute(
            select(UserStats).where(UserStats.games_played >= min_games)
        )
        rows = list(result.scalars().all())
        rows.sort(
            key=lambda row: (
                (row.wins or 0) / max(row.games_played or 1, 1),
                row.games_played or 0,
            ),
            reverse=True,
        )
        return rows[:limit]


class ChatLeaderboardRepo:
    """Board scoped to a single group chat.

    The global boards live in ``user_stats``, which deliberately has no
    chat column - a player's totals follow them everywhere. A per-chat
    board therefore has to be computed from the game history: every
    finished game knows its ``chat_id``, and every ``players`` row knows
    the role that player held, which is enough to decide who won.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_chat(
        self, chat_id: int, *, limit: int = 10, min_games: int = 1
    ) -> list[dict]:
        """Rows of ``{name, played, wins, winrate}`` for one chat.

        Sorted by wins, then by games played, so an active regular ranks
        above someone who won once and never came back.
        """
        # A player won when their role belongs to the side that won.
        won_case = case(
            (
                and_(
                    Game.winner == Winner.CITY.value,
                    Player.role.in_([role.value for role in TOWN_ROLES]),
                ),
                1,
            ),
            (
                and_(
                    Game.winner == Winner.MAFIA.value,
                    Player.role.in_(
                        [role.value for role in MAFIA_SIDE_ROLES]
                    ),
                ),
                1,
            ),
            (
                and_(
                    Game.winner == Winner.MANIAC.value,
                    Player.role == Role.MANIAC.value,
                ),
                1,
            ),
            else_=0,
        )

        result = await self._session.execute(
            select(
                Player.user_id,
                func.max(Player.full_name).label("full_name"),
                func.count(Player.id).label("played"),
                func.sum(won_case).label("wins"),
            )
            .join(Game, Game.id == Player.game_id)
            .where(
                Game.chat_id == chat_id,
                Game.status == GameStatus.FINISHED.value,
                # Abandoned games have no winner and would drag the
                # winrate of everyone at that table down unfairly.
                Game.winner.is_not(None),
                Game.winner != Winner.NONE.value,
            )
            .group_by(Player.user_id)
            .having(func.count(Player.id) >= min_games)
            .order_by(
                func.sum(won_case).desc(), func.count(Player.id).desc()
            )
            .limit(limit)
        )

        rows = []
        for user_id, full_name, played, wins in result.all():
            played = int(played or 0)
            wins = int(wins or 0)
            rows.append(
                {
                    "user_id": int(user_id),
                    "full_name": full_name or str(user_id),
                    "played": played,
                    "wins": wins,
                }
            )
        return rows

    async def games_in(self, chat_id: int) -> int:
        """How many finished games this chat has on record."""
        result = await self._session.execute(
            select(func.count(Game.id)).where(
                Game.chat_id == chat_id,
                Game.status == GameStatus.FINISHED.value,
            )
        )
        return int(result.scalar() or 0)
