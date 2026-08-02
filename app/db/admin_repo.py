"""Repository layer for the admin panel.

Kept in its own module (instead of growing ``repo.py``) because these
tables are orthogonal to gameplay: admins, audit trail, bans, warnings,
promo codes, runtime config and the analytics queries that read games.

All write helpers ``flush`` but never ``commit``: the DB middleware owns
the transaction boundary for the whole update.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminAudit,
    BotAdmin,
    BotConfig,
    ChatBan,
    Game,
    Player,
    PromoCode,
    PromoRedemption,
    UserBan,
    UserPurchase,
    UserStats,
    UserWarning,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a DB timestamp to an aware UTC datetime.

    SQLite gives back naive datetimes even for ``DateTime(timezone=True)``
    columns, and comparing naive to aware raises ``TypeError``. Every
    comparison in this module goes through here first.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class AdminRepo:
    """Runtime-granted admins (bot owners come from ``ADMIN_IDS``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_admins(self) -> Sequence[BotAdmin]:
        result = await self._session.execute(
            select(BotAdmin).order_by(BotAdmin.created_at)
        )
        return result.scalars().all()

    async def is_admin(self, user_id: int) -> bool:
        return await self._session.get(BotAdmin, user_id) is not None

    async def grant(
        self, user_id: int, full_name: str, granted_by: int
    ) -> bool:
        """Add an admin. Returns False when the user already had rights."""
        if await self._session.get(BotAdmin, user_id) is not None:
            return False
        self._session.add(
            BotAdmin(
                user_id=user_id, full_name=full_name, granted_by=granted_by
            )
        )
        await self._session.flush()
        return True

    async def revoke(self, user_id: int) -> bool:
        row = await self._session.get(BotAdmin, user_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


class AuditRepo:
    """Append-only trail of privileged actions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        admin_id: int,
        action: str,
        *,
        target: str = "",
        details: str = "",
    ) -> None:
        self._session.add(
            AdminAudit(
                admin_id=admin_id,
                action=action,
                target=str(target)[:64],
                details=str(details)[:2000],
            )
        )
        await self._session.flush()

    async def recent(self, limit: int = 20) -> Sequence[AdminAudit]:
        result = await self._session.execute(
            select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit)
        )
        return result.scalars().all()

    async def page(self, *, limit: int, offset: int = 0) -> Sequence[AdminAudit]:
        """One page of the journal, newest first."""
        result = await self._session.execute(
            select(AdminAudit)
            .order_by(AdminAudit.id.desc())
            .offset(max(0, offset))
            .limit(max(1, limit))
        )
        return result.scalars().all()

    async def count(self, cap: int = 0) -> int:
        """How many entries exist, optionally capped at ``cap``."""
        result = await self._session.execute(
            select(func.count()).select_from(AdminAudit)
        )
        total = int(result.scalar_one() or 0)
        return min(total, cap) if cap else total


class BanRepo:
    """Play bans, warnings and blacklisted chats."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- user bans -----------------------------------------------------

    async def ban_user(
        self,
        user_id: int,
        *,
        reason: str,
        banned_by: int,
        days: Optional[int] = None,
    ) -> UserBan:
        """Ban a user, or extend/replace an existing ban.

        ``days=None`` means permanent.
        """
        until = _utcnow() + timedelta(days=days) if days else None
        row = await self._session.get(UserBan, user_id)
        if row is None:
            row = UserBan(user_id=user_id)
            self._session.add(row)
        row.reason = reason[:256]
        row.banned_by = banned_by
        row.until = until
        await self._session.flush()
        return row

    async def unban_user(self, user_id: int) -> bool:
        row = await self._session.get(UserBan, user_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def get_ban(self, user_id: int) -> Optional[UserBan]:
        """Return the active ban, auto-clearing one that has expired."""
        row = await self._session.get(UserBan, user_id)
        if row is None:
            return None
        until = _aware(row.until)
        if until is not None and until <= _utcnow():
            await self._session.delete(row)
            await self._session.flush()
            return None
        return row

    async def is_banned(self, user_id: int) -> bool:
        return await self.get_ban(user_id) is not None

    async def list_bans(
        self, limit: int = 30, offset: int = 0
    ) -> Sequence[UserBan]:
        result = await self._session.execute(
            select(UserBan)
            .order_by(UserBan.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count_bans(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(UserBan)
        )
        return int(result.scalar() or 0)

    # --- warnings ------------------------------------------------------

    async def warn(
        self, user_id: int, *, reason: str, admin_id: int
    ) -> int:
        """Record a warning and return the new active warning count."""
        self._session.add(
            UserWarning(
                user_id=user_id, reason=reason[:256], admin_id=admin_id
            )
        )
        await self._session.flush()
        return await self.warn_count(user_id)

    async def warn_count(self, user_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(UserWarning)
            .where(
                UserWarning.user_id == user_id,
                UserWarning.active.is_(True),
            )
        )
        return int(result.scalar_one() or 0)

    async def clear_warnings(self, user_id: int) -> int:
        """Drop all warnings for a user; returns how many were removed."""
        count = await self.warn_count(user_id)
        await self._session.execute(
            delete(UserWarning).where(UserWarning.user_id == user_id)
        )
        await self._session.flush()
        return count

    # --- chat blacklist ------------------------------------------------

    async def ban_chat(
        self, chat_id: int, *, title: str, reason: str, banned_by: int
    ) -> None:
        row = await self._session.get(ChatBan, chat_id)
        if row is None:
            row = ChatBan(chat_id=chat_id)
            self._session.add(row)
        row.title = title[:128]
        row.reason = reason[:256]
        row.banned_by = banned_by
        await self._session.flush()

    async def unban_chat(self, chat_id: int) -> bool:
        row = await self._session.get(ChatBan, chat_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def is_chat_banned(self, chat_id: int) -> bool:
        return await self._session.get(ChatBan, chat_id) is not None

    async def list_chat_bans(self, limit: int = 30) -> Sequence[ChatBan]:
        result = await self._session.execute(
            select(ChatBan).order_by(ChatBan.created_at.desc()).limit(limit)
        )
        return result.scalars().all()


class PromoRepo:
    """Promo codes and their redemptions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        code: str,
        *,
        coins: int,
        item_id: Optional[str],
        max_uses: int,
        days: Optional[int],
        created_by: int,
    ) -> Optional[PromoCode]:
        """Create a code. Returns ``None`` if the code already exists."""
        code = code.strip().upper()[:32]
        if await self._session.get(PromoCode, code) is not None:
            return None
        row = PromoCode(
            code=code,
            coins=coins,
            item_id=item_id,
            max_uses=max_uses,
            expires_at=_utcnow() + timedelta(days=days) if days else None,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, code: str) -> Optional[PromoCode]:
        return await self._session.get(PromoCode, code.strip().upper())

    async def delete(self, code: str) -> bool:
        row = await self.get(code)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def list_codes(
        self, limit: int = 30, offset: int = 0
    ) -> Sequence[PromoCode]:
        result = await self._session.execute(
            select(PromoCode)
            .order_by(PromoCode.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count_codes(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(PromoCode)
        )
        return int(result.scalar() or 0)

    async def already_redeemed(self, code: str, user_id: int) -> bool:
        result = await self._session.execute(
            select(PromoRedemption.id).where(
                PromoRedemption.code == code.strip().upper(),
                PromoRedemption.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def redeem(self, code: str, user_id: int) -> None:
        """Mark the code used by this user and bump the counter.

        Callers must validate the code first (see ``check_redeemable``).
        """
        normalised = code.strip().upper()
        self._session.add(
            PromoRedemption(code=normalised, user_id=user_id)
        )
        row = await self._session.get(PromoCode, normalised)
        if row is not None:
            row.used_count = int(row.used_count or 0) + 1
        await self._session.flush()

    async def check_redeemable(
        self, code: str, user_id: int
    ) -> tuple[Optional[PromoCode], Optional[str]]:
        """Validate a code for a user.

        Returns ``(code_row, None)`` when it can be redeemed, or
        ``(None, error_key)`` with an i18n key describing the problem.
        """
        row = await self.get(code)
        if row is None:
            return None, "promo.invalid"
        expires = _aware(row.expires_at)
        if expires is not None and expires <= _utcnow():
            return None, "promo.expired"
        if row.max_uses > 0 and int(row.used_count or 0) >= row.max_uses:
            return None, "promo.exhausted"
        if await self.already_redeemed(row.code, user_id):
            return None, "promo.already_used"
        return row, None


class ConfigRepo:
    """Key/value runtime configuration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Optional[str]:
        row = await self._session.get(BotConfig, key)
        return None if row is None else row.value

    async def set(self, key: str, value: str, *, updated_by: int = 0) -> None:
        row = await self._session.get(BotConfig, key)
        if row is None:
            row = BotConfig(key=key)
            self._session.add(row)
        row.value = str(value)
        row.updated_by = updated_by
        row.updated_at = _utcnow()
        await self._session.flush()

    async def all(self) -> dict[str, str]:
        result = await self._session.execute(select(BotConfig))
        return {row.key: row.value for row in result.scalars().all()}


class AnalyticsRepo:
    """Read-only aggregate queries powering the stats screens."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def games_since(self, days: int) -> int:
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(func.count())
            .select_from(Game)
            .where(Game.created_at >= since)
        )
        return int(result.scalar_one() or 0)

    async def finished_since(self, days: int) -> int:
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(func.count())
            .select_from(Game)
            .where(Game.finished_at.is_not(None), Game.created_at >= since)
        )
        return int(result.scalar_one() or 0)

    async def active_users(self, days: int) -> int:
        """Distinct users seen in the given window (DAU / MAU)."""
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(func.count())
            .select_from(UserStats)
            .where(UserStats.last_seen_at >= since)
        )
        return int(result.scalar_one() or 0)

    async def total_users(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(UserStats)
        )
        return int(result.scalar_one() or 0)

    async def avg_players_per_game(self, days: int = 30) -> float:
        """Mean player count across games started in the window."""
        since = _utcnow() - timedelta(days=days)
        per_game = (
            select(func.count(Player.id).label("n"))
            .join(Game, Game.id == Player.game_id)
            .where(Game.created_at >= since)
            .group_by(Player.game_id)
            .subquery()
        )
        result = await self._session.execute(select(func.avg(per_game.c.n)))
        return float(result.scalar_one() or 0.0)

    async def avg_rounds(self, days: int = 30) -> float:
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(func.avg(Game.rounds_played)).where(
                Game.finished_at.is_not(None), Game.created_at >= since
            )
        )
        return float(result.scalar_one() or 0.0)

    async def winner_breakdown(self, days: int = 30) -> list[tuple[str, int]]:
        """How many finished games each side won."""
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(Game.winner, func.count())
            .where(Game.winner.is_not(None), Game.created_at >= since)
            .group_by(Game.winner)
            .order_by(func.count().desc())
        )
        return [(row[0] or "?", int(row[1])) for row in result.all()]

    async def role_winrates(self, days: int = 30) -> list[tuple[str, int, int]]:
        """Per-role ``(role, games, wins)`` over finished games.

        A role "wins" when its side matches the game's winner. The side
        mapping is resolved in Python (see ``app.game.enums``) so this
        query stays portable between SQLite and PostgreSQL.
        """
        since = _utcnow() - timedelta(days=days)
        result = await self._session.execute(
            select(Player.role, Game.winner, func.count())
            .join(Game, Game.id == Player.game_id)
            .where(
                Player.role.is_not(None),
                Game.winner.is_not(None),
                Game.created_at >= since,
            )
            .group_by(Player.role, Game.winner)
        )
        from app.game.enums import (
            MAFIA_SIDE_ROLES,
            THIRD_PARTY_ROLES,
            Role,
            Winner,
        )

        totals: dict[str, list[int]] = {}
        for role_value, winner_value, count in result.all():
            bucket = totals.setdefault(role_value, [0, 0])
            bucket[0] += int(count)
            try:
                role = Role(role_value)
            except ValueError:
                continue
            if role in THIRD_PARTY_ROLES:
                won = winner_value == Winner.MANIAC.value
            elif role in MAFIA_SIDE_ROLES:
                won = winner_value == Winner.MAFIA.value
            else:
                won = winner_value == Winner.CITY.value
            if won:
                bucket[1] += int(count)
        return sorted(
            ((role, data[0], data[1]) for role, data in totals.items()),
            key=lambda item: item[1],
            reverse=True,
        )

    async def top_chats(self, limit: int = 10) -> list[tuple[int, int]]:
        result = await self._session.execute(
            select(Game.chat_id, func.count())
            .group_by(Game.chat_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(int(row[0]), int(row[1])) for row in result.all()]

    async def economy_summary(self) -> tuple[int, int, int]:
        """``(coins_in_wallets, coins_spent, purchases)``."""
        held = await self._session.execute(select(func.sum(UserStats.coins)))
        spent = await self._session.execute(
            select(func.sum(UserPurchase.price_paid))
        )
        count = await self._session.execute(
            select(func.count()).select_from(UserPurchase)
        )
        return (
            int(held.scalar_one() or 0),
            int(spent.scalar_one() or 0),
            int(count.scalar_one() or 0),
        )

    async def top_purchases(self, limit: int = 10) -> list[tuple[str, int]]:
        result = await self._session.execute(
            select(UserPurchase.item_id, func.count())
            .group_by(UserPurchase.item_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(str(row[0]), int(row[1])) for row in result.all()]

    async def broadcast_audience(
        self, *, days: Optional[int] = None, language: Optional[str] = None
    ) -> list[int]:
        """User ids reachable in DM, optionally filtered.

        Only users with ``has_dm`` are returned: messaging anyone else is
        guaranteed to fail with "bot can't initiate conversation".
        """
        stmt = select(UserStats.user_id).where(UserStats.has_dm.is_(True))
        if days:
            stmt = stmt.where(
                UserStats.last_seen_at >= _utcnow() - timedelta(days=days)
            )
        if language:
            stmt = stmt.where(UserStats.language == language)
        result = await self._session.execute(stmt)
        return [int(uid) for uid in result.scalars().all()]


__all__ = [
    "AdminRepo",
    "AnalyticsRepo",
    "AuditRepo",
    "BanRepo",
    "ConfigRepo",
    "PromoRepo",
]
