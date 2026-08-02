"""Inventory of stackable items and activated role cards."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoleClaim, UserInventory


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InventoryRepo:
    """Quantities of consumable items (role cards) per player."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, user_id: int, item_id: str) -> Optional[UserInventory]:
        result = await self._session.execute(
            select(UserInventory).where(
                UserInventory.user_id == user_id,
                UserInventory.item_id == item_id,
            )
        )
        return result.scalars().first()

    async def count(self, user_id: int, item_id: str) -> int:
        row = await self._row(user_id, item_id)
        return int(row.quantity) if row else 0

    async def add(self, user_id: int, item_id: str, quantity: int = 1) -> int:
        """Add items and return the new quantity."""
        row = await self._row(user_id, item_id)
        if row is None:
            row = UserInventory(user_id=user_id, item_id=item_id, quantity=0)
            self._session.add(row)
        row.quantity = int(row.quantity or 0) + int(quantity)
        row.updated_at = _utcnow()
        await self._session.flush()
        return row.quantity

    async def take(self, user_id: int, item_id: str, quantity: int = 1) -> bool:
        """Consume items. Returns False when the player does not have enough."""
        row = await self._row(user_id, item_id)
        if row is None or int(row.quantity or 0) < quantity:
            return False
        row.quantity = int(row.quantity) - int(quantity)
        row.updated_at = _utcnow()
        await self._session.flush()
        return True

    async def items(self, user_id: int) -> dict[str, int]:
        """Everything the player owns, ``item_id -> quantity`` (>0 only)."""
        result = await self._session.execute(
            select(UserInventory).where(
                UserInventory.user_id == user_id, UserInventory.quantity > 0
            )
        )
        return {row.item_id: int(row.quantity) for row in result.scalars().all()}


class RoleClaimRepo:
    """Role cards that were activated and await the next game."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active(self, user_id: int) -> Optional[RoleClaim]:
        result = await self._session.execute(
            select(RoleClaim)
            .where(RoleClaim.user_id == user_id, RoleClaim.is_active.is_(True))
            .order_by(RoleClaim.id.desc())
        )
        return result.scalars().first()

    async def active_for(self, user_ids: list[int]) -> dict[int, RoleClaim]:
        """Active claims for a whole lobby, oldest activation wins."""
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(RoleClaim)
            .where(RoleClaim.user_id.in_(user_ids), RoleClaim.is_active.is_(True))
            .order_by(RoleClaim.id.asc())
        )
        claims: dict[int, RoleClaim] = {}
        for row in result.scalars().all():
            claims.setdefault(row.user_id, row)
        return claims

    async def create(self, user_id: int, role: str, item_id: str) -> RoleClaim:
        claim = RoleClaim(user_id=user_id, role=role, item_id=item_id)
        self._session.add(claim)
        await self._session.flush()
        return claim

    async def consume(self, claim: RoleClaim) -> None:
        claim.is_active = False
        claim.used_at = _utcnow()
        await self._session.flush()

    async def cancel(self, claim: RoleClaim) -> None:
        """Drop a claim without marking it as used (card gets refunded)."""
        claim.is_active = False
        await self._session.flush()
