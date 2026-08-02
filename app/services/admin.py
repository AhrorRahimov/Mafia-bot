"""Admin permissions, runtime feature switches and broadcasting.

Three levels of access:

1. **Owners** - ids listed in ``ADMIN_IDS`` (env). Full power, including
   granting and revoking admin rights. Cannot be demoted from the bot.
2. **Admins** - rows in ``bot_admins``, granted at runtime by an owner.
   Everything except managing other admins.
3. **Group admins** - Telegram administrators of a specific chat. They
   may only manage the game running in their own chat.

Runtime config lives in the ``bot_config`` table and is cached in-process
so the hot path (every ``/join``) does not hit the DB for feature flags.
The cache is invalidated on every write, and it is process-local: with a
single polling process (the deployment model here) that is exact.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.admin_repo import AdminRepo, ConfigRepo
from app.game.flags import (
    ALL_FLAG_KEYS,
    FEATURE_FLAGS,
    KEY_COIN_MULTIPLIER,
    KEY_FEATURE_DEAD_CHAT,
    KEY_FEATURE_DETECTIVE_SHOOT,
    KEY_FEATURE_ROLE_CARDS,
    KEY_FEATURE_SHOP,
    KEY_MAINTENANCE,
    flag_default,
    resolve_flag_key,
)

logger = logging.getLogger(__name__)

# --- runtime config keys ---------------------------------------------

# Switch keys live in app.game.flags so the tests (and any tool without
# aiogram installed) can import them. Re-exported here for callers.


def is_owner(user_id: int) -> bool:
    """True when the id is listed in ``ADMIN_IDS`` / ``ADMIN_ID``."""
    return user_id in get_settings().admin_ids


async def is_admin(session: AsyncSession, user_id: int) -> bool:
    """True for owners and for admins granted at runtime."""
    if is_owner(user_id):
        return True
    return await AdminRepo(session).is_admin(user_id)


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True when the user administers that specific Telegram chat.

    Network failures resolve to ``False``: refusing a moderation action
    is always safer than granting it on an unverified assumption.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in {"creator", "administrator"}


class RuntimeConfig:
    """Cached accessor over the ``bot_config`` table."""

    def __init__(self) -> None:
        self._cache: Optional[dict[str, str]] = None

    async def _load(self, session: AsyncSession) -> dict[str, str]:
        if self._cache is None:
            self._cache = await ConfigRepo(session).all()
        return self._cache

    def invalidate(self) -> None:
        self._cache = None

    async def get(
        self, session: AsyncSession, key: str, default: str = ""
    ) -> str:
        data = await self._load(session)
        return data.get(key, default)

    async def set(
        self, session: AsyncSession, key: str, value: str, *, admin_id: int = 0
    ) -> None:
        await ConfigRepo(session).set(key, value, updated_by=admin_id)
        self.invalidate()

    async def get_bool(
        self, session: AsyncSession, key: str, default: bool = False
    ) -> bool:
        raw = await self.get(session, key, "1" if default else "0")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    async def set_bool(
        self, session: AsyncSession, key: str, value: bool, *, admin_id: int = 0
    ) -> None:
        await self.set(session, key, "1" if value else "0", admin_id=admin_id)

    async def toggle(
        self, session: AsyncSession, key: str, *, default: bool, admin_id: int
    ) -> bool:
        """Flip a boolean switch and return its new value."""
        new_value = not await self.get_bool(session, key, default)
        await self.set_bool(session, key, new_value, admin_id=admin_id)
        return new_value

    # --- typed helpers -------------------------------------------------

    async def is_maintenance(self, session: AsyncSession) -> bool:
        return await self.get_bool(session, KEY_MAINTENANCE, False)

    async def feature_enabled(
        self, session: AsyncSession, key: str
    ) -> bool:
        return await self.get_bool(session, key, FEATURE_FLAGS.get(key, True))

    async def coin_multiplier(self, session: AsyncSession) -> float:
        """Global payout multiplier (1.0 = normal, 2.0 = double coins).

        Clamped to a sane range so a typo like ``x1000`` cannot wreck the
        economy in a single evening.
        """
        raw = await self.get(session, KEY_COIN_MULTIPLIER, "1")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return max(0.0, min(value, 10.0))


# Process-wide singleton, injected into handlers by ServicesMiddleware.
runtime_config = RuntimeConfig()


@dataclass
class BroadcastResult:
    """Outcome of a broadcast run."""

    total: int = 0
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    unreachable: list[int] = field(default_factory=list)


async def broadcast(
    bot: Bot,
    user_ids: Sequence[int],
    text: str,
    *,
    rate: int = 20,
    on_blocked: Optional[Callable[[int], Awaitable[None]]] = None,
) -> BroadcastResult:
    """Send ``text`` to every id, respecting Telegram rate limits.

    Telegram allows roughly 30 messages/second to different users; we
    stay under that with a short sleep between sends and honour the
    ``RetryAfter`` back-off when the API asks us to slow down.

    Users who blocked the bot are collected in ``unreachable`` so the
    caller can clear their ``has_dm`` flag and stop wasting sends.
    """
    result = BroadcastResult(total=len(user_ids))
    delay = 1.0 / max(rate, 1)
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            result.sent += 1
        except TelegramRetryAfter as exc:
            # Flood control: wait it out, then retry this same user once.
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.send_message(user_id, text)
                result.sent += 1
            except TelegramAPIError:
                result.failed += 1
        except TelegramAPIError as exc:
            message = str(exc).lower()
            if "blocked" in message or "chat not found" in message or "deactivated" in message:
                result.blocked += 1
                result.unreachable.append(user_id)
                if on_blocked is not None:
                    await on_blocked(user_id)
            else:
                result.failed += 1
                logger.warning("Broadcast to %s failed: %s", user_id, exc)
        await asyncio.sleep(delay)
    return result


__all__ = [
    "ALL_FLAG_KEYS",
    "BroadcastResult",
    "FEATURE_FLAGS",
    "KEY_COIN_MULTIPLIER",
    "KEY_FEATURE_DEAD_CHAT",
    "KEY_FEATURE_ROLE_CARDS",
    "KEY_FEATURE_DETECTIVE_SHOOT",
    "KEY_FEATURE_SHOP",
    "KEY_MAINTENANCE",
    "RuntimeConfig",
    "broadcast",
    "flag_default",
    "is_admin",
    "is_group_admin",
    "is_owner",
    "resolve_flag_key",
    "runtime_config",
]


# Broadcast drafts awaiting confirmation, keyed by admin id.
#
# Deliberately in-process and not persisted: a draft is only meaningful
# for the few seconds between /broadcast and the confirmation tap, and a
# restart should throw it away rather than fire it later.
PENDING_BROADCASTS: dict[int, str] = {}
