"""Database session middleware.

Injects a fresh ``AsyncSession`` into handler ``data["session"]``
and ensures it is closed/committed/rolled-back consistently.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.db.session import get_session_factory


class DbSessionMiddleware(BaseMiddleware):
    """Provide each update with its own DB session under ``data["session"]``."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
