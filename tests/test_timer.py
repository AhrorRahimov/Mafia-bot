"""Regression tests for :class:`TimerManager`.

These cover the "phase freezes right after the 10-seconds-left reminder"
bug: phase handlers such as ``start_vote`` call ``timers.cancel(game_id)``
as their first step, but they run *inside* the timer task, so cancelling
killed the very coroutine that was supposed to advance the game.
"""
from __future__ import annotations

import asyncio

from app.services.timer import TimerManager


def test_callback_may_cancel_its_own_timer():
    """A phase callback that cancels its own timer must still finish."""
    log = []

    async def scenario():
        timers = TimerManager()
        game_id = 1

        async def callback():
            log.append("entered")
            timers.cancel(game_id)   # what start_vote() does first
            await asyncio.sleep(0)   # previously raised CancelledError here
            log.append("finished")

        timers.schedule(game_id, 0.05, callback)
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert log == ["entered", "finished"]


def test_reminder_fires_then_phase_advances():
    """The countdown reminder must not be the last thing that happens."""
    log = []

    async def scenario():
        timers = TimerManager()
        game_id = 2

        async def remind():
            log.append("reminder")

        async def callback():
            timers.cancel(game_id)
            await asyncio.sleep(0)
            log.append("next_phase")

        timers.schedule(game_id, 0.2, callback, reminders=[(0.1, remind)])
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert log == ["reminder", "next_phase"]


def test_external_cancel_still_stops_the_timer():
    """Cancelling from outside the task must keep working."""
    log = []

    async def scenario():
        timers = TimerManager()

        async def callback():
            log.append("should-not-run")

        timers.schedule(3, 0.2, callback)
        await asyncio.sleep(0.05)
        timers.cancel(3)
        await asyncio.sleep(0.4)

    asyncio.run(scenario())
    assert log == []


def test_scheduling_replaces_the_previous_timer():
    """Only one timer per game may ever be pending."""
    log = []

    async def scenario():
        timers = TimerManager()

        async def first():
            log.append("first")

        async def second():
            log.append("second")

        timers.schedule(4, 0.2, first)
        await asyncio.sleep(0.05)
        timers.schedule(4, 0.2, second)
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert log == ["second"]


def test_failing_reminder_does_not_block_the_phase():
    """A broken reminder must never strand the game."""
    log = []

    async def scenario():
        timers = TimerManager()

        async def bad_reminder():
            raise RuntimeError("telegram is down")

        async def callback():
            log.append("next_phase")

        timers.schedule(5, 0.2, callback, reminders=[(0.1, bad_reminder)])
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert log == ["next_phase"]


def test_lobby_callback_may_cancel_its_own_timer():
    """Lobby auto-start cancels its own countdown; it must still run."""
    log = []

    async def scenario():
        timers = TimerManager()

        async def callback():
            timers.cancel_lobby(7)
            await asyncio.sleep(0)
            log.append("game_started")

        timers.schedule_lobby(7, 0.05, callback)
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert log == ["game_started"]
