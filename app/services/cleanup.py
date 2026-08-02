"""Group-chat message tracking for the sliding-window cleanup policy.

Every message the bot posts to a group chat is registered here. The
tracker keeps only the most recent ``max_per_chat`` messages per chat;
anything older is requested for deletion via an injected deleter
callback. The lobby card is edited in place rather than reposted, so it
is deliberately NOT registered here.

The tracker holds no Telegram API knowledge of its own — the deleter is
supplied by the caller (the orchestrator) so this module stays free of
side effects and is trivial to reason about / test.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Default sliding-window size: keep this many recent bot messages in each
# group chat, delete the rest.
DEFAULT_MAX_PER_CHAT = 8

# Type of the async deleter callback: (chat_id, message_id) -> None.
# It must never raise — failures are the caller's concern (best-effort).
Deleter = Callable[[int, int], Awaitable[None]]


class MessageTracker:
    """Sliding window of recent bot messages per group chat.

    Thread-safety: intended for single-process asyncio usage. Because
    asyncio is cooperative, no lock is required as long as the tracker is
    only mutated between awaits (which it is — registration is sync).
    """

    def __init__(
        self,
        *,
        deleter: Deleter,
        max_per_chat: int = DEFAULT_MAX_PER_CHAT,
    ) -> None:
        self._deleter = deleter
        self._max = max_per_chat
        self._windows: dict[int, deque[int]] = {}

    def register(self, chat_id: int, message_id: int) -> None:
        """Record a freshly sent message and trim the window for its chat.

        Trimming schedules deletion of the oldest overflowing message but
        does NOT await it (best-effort): we create the task so a slow
        delete never blocks message sending.
        """
        window = self._windows.setdefault(
            chat_id, deque(maxlen=self._max + 1)
        )
        window.append(message_id)
        if len(window) > self._max:
            dropped = window.popleft()
            self._schedule_delete(chat_id, dropped)

    def forget(self, chat_id: int) -> None:
        """Drop all tracked message ids for ``chat_id`` (e.g. game ended).

        No deletion is performed — forget means "we no longer care".
        """
        self._windows.pop(chat_id, None)

    def _schedule_delete(self, chat_id: int, message_id: int) -> None:
        import asyncio

        async def _go() -> None:
            try:
                await self._deleter(chat_id, message_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.debug(
                    "Cleanup delete failed chat=%s msg=%s", chat_id, message_id
                )

        try:
            asyncio.create_task(_go(), name="mafia-cleanup-delete")
        except RuntimeError:
            # No running loop (e.g. tests) — nothing useful to do.
            pass
