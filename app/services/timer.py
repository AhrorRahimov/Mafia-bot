"""Async timer manager for phase transitions.

Each game owns a single ``asyncio.Task`` that fires a callback once
the configured delay elapses. The manager guarantees only one task
per game is pending at any time: scheduling a new one cancels the old.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class TimerManager:
    """Track and cancel per-game timer tasks."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}
        # Lobby gathering uses a SEPARATE task namespace keyed by chat_id so
        # it never collides with the one-game-timer guarantee used in play.
        self._lobby_tasks: dict[int, asyncio.Task[None]] = {}

    def schedule(
        self,
        game_id: int,
        delay: float,
        callback: Callable[[], Awaitable[None]],
        *,
        reminders: list[tuple[float, Callable[[], Awaitable[None]]]] | None = None,
    ) -> None:
        """Run ``callback`` after ``delay`` seconds.

        Any previously scheduled task for the same game is cancelled.

        ``reminders`` is an optional list of ``(seconds_remaining, cb)``
        pairs. Each ``cb`` is awaited when that many seconds are left in
        the phase (e.g. ``(10, notify)`` fires 10s before ``callback``).
        A single task handles both the reminders and the final callback,
        preserving the one-timer-per-game guarantee. A failing reminder
        is logged and never blocks the final callback.
        """
        self.cancel(game_id)

        async def _runner() -> None:
            try:
                # Convert "seconds remaining" into absolute elapsed offsets
                # and fire them in order before the final callback.
                fire_points = sorted(
                    (
                        (delay - seconds_left, cb)
                        for seconds_left, cb in (reminders or [])
                        if 0 < delay - seconds_left < delay
                    ),
                    key=lambda item: item[0],
                )
                elapsed = 0.0
                for at, reminder_cb in fire_points:
                    await asyncio.sleep(at - elapsed)
                    elapsed = at
                    try:
                        await reminder_cb()
                    except Exception:  # noqa: BLE001 — one bad reminder must not abort the phase
                        logger.exception(
                            "Reminder callback failed for game %s.", game_id
                        )
                await asyncio.sleep(delay - elapsed)
                await callback()
            except asyncio.CancelledError:
                logger.debug("Timer for game %s cancelled.", game_id)
                raise
            except Exception:  # noqa: BLE001 — log and swallow to keep loop alive
                logger.exception("Timer callback failed for game %s.", game_id)
            finally:
                self._tasks.pop(game_id, None)

        self._tasks[game_id] = asyncio.create_task(
            _runner(), name=f"mafia-timer-{game_id}"
        )

    def cancel(self, game_id: int) -> None:
        """Cancel the pending timer for ``game_id`` if any.

        Phase handlers such as ``start_vote`` call this defensively as their
        first step, but they are themselves usually invoked *from* the timer
        callback. Cancelling the currently running task would raise
        ``CancelledError`` at the handler's next ``await`` and silently freeze
        the game, so in that case we only detach the bookkeeping entry and let
        the callback run to completion.
        """
        task = self._tasks.pop(game_id, None)
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()

    def cancel_all(self) -> None:
        """Cancel every pending timer (used on shutdown)."""
        for game_id in list(self._tasks.keys()):
            self.cancel(game_id)
        for chat_id in list(self._lobby_tasks.keys()):
            self.cancel_lobby(chat_id)

    # --- Lobby timers ----------------------------------------------------
    # Lobby gathering uses a SEPARATE task namespace keyed by chat_id so it
    # never collides with the one-game-timer guarantee used during play.

    def schedule_lobby(
        self,
        chat_id: int,
        delay: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Run ``callback`` after ``delay`` seconds (lobby gathering).

        Replaces any previously scheduled lobby task for ``chat_id``.
        Unlike phase timers, this is a plain single-shot delay.
        """
        self.cancel_lobby(chat_id)

        async def _lobby_runner() -> None:
            try:
                await asyncio.sleep(delay)
                await callback()
            except asyncio.CancelledError:
                logger.debug("Lobby timer for chat %s cancelled.", chat_id)
                raise
            except Exception:  # noqa: BLE001 — keep loop alive
                logger.exception("Lobby timer callback failed for chat %s.", chat_id)
            finally:
                self._lobby_tasks.pop(chat_id, None)

        self._lobby_tasks[chat_id] = asyncio.create_task(
            _lobby_runner(), name=f"mafia-lobby-{chat_id}"
        )

    def reschedule_lobby(
        self,
        chat_id: int,
        delay: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Alias for ``schedule_lobby`` — semantically "change the wait"."""
        self.schedule_lobby(chat_id, delay, callback)

    def cancel_lobby(self, chat_id: int) -> None:
        """Cancel the pending lobby timer for ``chat_id`` if any.

        Self-cancellation is a no-op for the same reason as :meth:`cancel`:
        the lobby countdown callback (auto-start) cancels its own timer.
        """
        task = self._lobby_tasks.pop(chat_id, None)
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()
