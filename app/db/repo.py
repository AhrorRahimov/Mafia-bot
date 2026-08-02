"""Repository layer: data access for Game / Player / UserStats."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Game, Player, UserPurchase, UserStats
from app.game.enums import GameStatus, Role, Winner


class GameRepo:
    """CRUD + domain queries for Game."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, chat_id: int, creator_id: int) -> Game:
        game = Game(chat_id=chat_id, creator_id=creator_id, status=GameStatus.LOBBY)
        self._session.add(game)
        await self._session.flush()
        return game

    async def get_active(self, chat_id: int) -> Optional[Game]:
        """Return the non-finished game for a chat, if any."""
        stmt = (
            select(Game)
            .where(Game.chat_id == chat_id, Game.status != GameStatus.FINISHED)
            .options(selectinload(Game.players))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get(self, game_id: int) -> Optional[Game]:
        stmt = select(Game).where(Game.id == game_id).options(selectinload(Game.players))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_status(self, game: Game, status: str) -> None:
        game.status = status
        await self._session.flush()

    async def finish(
        self, game: Game, winner: str, rounds_played: int
    ) -> None:
        game.status = GameStatus.FINISHED
        game.winner = winner
        game.rounds_played = rounds_played
        game.finished_at = datetime.now(timezone.utc)
        await self._session.flush()


class PlayerRepo:
    """CRUD + domain queries for Player."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        game_id: int,
        user_id: int,
        full_name: str,
    ) -> Player:
        player = Player(
            game_id=game_id,
            user_id=user_id,
            full_name=full_name,
        )
        self._session.add(player)
        await self._session.flush()
        return player

    async def get(self, game_id: int, user_id: int) -> Optional[Player]:
        stmt = select(Player).where(
            Player.game_id == game_id, Player.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_game(self, game_id: int) -> list[Player]:
        stmt = select(Player).where(Player.game_id == game_id).order_by(Player.id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def remove(self, player: Player) -> None:
        await self._session.delete(player)
        await self._session.flush()

    async def assign_role(self, player: Player, role: Role) -> None:
        player.role = role.value
        await self._session.flush()

    async def kill(self, player: Player) -> None:
        player.is_alive = False
        await self._session.flush()


class StatsRepo:
    """Persistent per-user statistics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_touch(
        self, user_id: int, full_name: str, username: str | None = None
    ) -> None:
        """Remember the player (and their @username, when we can see it).

        Storing the username is what lets admins type ``/ban @someone``
        instead of hunting for a numeric id.
        """
        username = (username or "").lstrip("@").lower()[:64]
        stats = await self._session.get(UserStats, user_id)
        if stats is None:
            stats = UserStats(
                user_id=user_id, full_name=full_name, username=username
            )
            self._session.add(stats)
        else:
            stats.full_name = full_name
            if username:
                stats.username = username
            stats.last_seen_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def find_by_username(self, username: str) -> Optional[UserStats]:
        """Look a player up by @username (case-insensitive)."""
        handle = (username or "").lstrip("@").lower()
        if not handle:
            return None
        result = await self._session.execute(
            select(UserStats).where(func.lower(UserStats.username) == handle)
        )
        return result.scalars().first()

    async def record_result(
        self, user_id: int, full_name: str, *, won: bool
    ) -> None:
        await self.upsert_touch(user_id, full_name)
        stats = await self._session.get(UserStats, user_id)
        assert stats is not None  # noqa: S101 — upsert guarantees it
        stats.games_played += 1
        if won:
            stats.wins += 1
        else:
            stats.losses += 1
        await self._session.flush()

    async def get(self, user_id: int) -> Optional[UserStats]:
        return await self._session.get(UserStats, user_id)

    # --- currency ------------------------------------------------------

    async def get_coins(self, user_id: int) -> int:
        """Current coin balance (0 for users who never played)."""
        stats = await self._session.get(UserStats, user_id)
        if stats is None:
            return 0
        return int(stats.coins or 0)

    async def clear_dm(self, user_id: int) -> None:
        """Forget that the user is reachable in DM.

        Called when a broadcast hits "bot was blocked": keeping the flag
        would make every future broadcast waste a request on them, and
        the lobby DM-gate would let them join a game it cannot notify.
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is not None:
            stats.has_dm = False
            await self._session.flush()

    async def add_coins(self, user_id: int, amount: int) -> int:
        """Credit ``amount`` coins and return the new balance.

        Never creates a row on its own: coins are always awarded right
        after ``record_result``, which upserts the row first.
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is None:
            return 0
        stats.coins = int(stats.coins or 0) + int(amount)
        await self._session.flush()
        return stats.coins

    async def spend_coins(self, user_id: int, amount: int) -> bool:
        """Debit ``amount`` coins if the balance allows it.

        Returns ``False`` (and changes nothing) when the user cannot
        afford the purchase, so the caller can show a friendly error.
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is None or int(stats.coins or 0) < int(amount):
            return False
        stats.coins = int(stats.coins or 0) - int(amount)
        await self._session.flush()
        return True

    async def top(self, limit: int = 10) -> list[UserStats]:
        """Leaderboard: most wins first, then best win rate, then games.

        Only players who actually finished a game are listed.
        """
        stmt = (
            select(UserStats)
            .where(UserStats.games_played > 0)
            .order_by(
                UserStats.wins.desc(),
                UserStats.games_played.asc(),
                UserStats.user_id.asc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_language(self, user_id: int) -> str:
        """Return the user's language code, defaulting to ``ru``.

        Read-only: never creates a row. Callers that must persist the
        language (e.g. on first launch / language picker) should use
        ``set_language`` instead.
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is None or not stats.language:
            return "ru"
        return stats.language

    async def mark_dm_ready(self, user_id: int, full_name: str) -> None:
        """Record that the user has opened the bot in a private chat.

        Upserts the row and sets ``has_dm = True`` so the user can join
        lobbies (the bot can now DM them their secret role).
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is None:
            stats = UserStats(
                user_id=user_id, full_name=full_name, has_dm=True
            )
            self._session.add(stats)
        else:
            stats.has_dm = True
            if full_name:
                stats.full_name = full_name
        await self._session.flush()

    async def is_dm_ready(self, user_id: int) -> bool:
        """True if the user has started the bot in a private chat."""
        stats = await self._session.get(UserStats, user_id)
        return bool(stats is not None and stats.has_dm)

    async def is_known(self, user_id: int) -> bool:
        """True if a ``user_stats`` row exists for this user.

        Used to detect a brand-new user on first ``/start`` so we can
        force the language picker.
        """
        stats = await self._session.get(UserStats, user_id)
        return stats is not None

    async def set_language(
        self, user_id: int, language: str, full_name: str | None = None
    ) -> None:
        """Persist the user's preferred language. Upserts the row.

        ``full_name`` (if given) is stored so the placeholder
        ``"User {id}"`` is replaced with the real Telegram name.
        """
        stats = await self._session.get(UserStats, user_id)
        if stats is None:
            stats = UserStats(
                user_id=user_id,
                full_name=full_name or f"User {user_id}",
                language=language,
            )
            self._session.add(stats)
        else:
            stats.language = language
            if full_name:
                stats.full_name = full_name
        await self._session.flush()


class ShopRepo:
    """Ownership of cosmetic shop items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def owned_items(self, user_id: int) -> set[str]:
        """Ids of every item the user already owns."""
        stmt = select(UserPurchase.item_id).where(
            UserPurchase.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def has_item(self, user_id: int, item_id: str) -> bool:
        stmt = select(UserPurchase.id).where(
            UserPurchase.user_id == user_id,
            UserPurchase.item_id == item_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def grant(self, user_id: int, item_id: str, price_paid: int) -> None:
        """Record the purchase. Callers debit the coins beforehand."""
        self._session.add(
            UserPurchase(
                user_id=user_id, item_id=item_id, price_paid=price_paid
            )
        )
        await self._session.flush()


# Re-export winner enum alias to avoid circular imports in callers.
__all__ = ["GameRepo", "PlayerRepo", "ShopRepo", "StatsRepo", "Winner"]
