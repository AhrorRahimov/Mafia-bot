"""Day vote callback (private chat).

Votes arrive in the bot's private chat via inline keyboards, just like
night actions. We keep them in a dedicated module for conceptual
clarity even though the wiring is identical.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.game.exceptions import GameError
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction, parse_callback
from app.services.day import DayService
from app.services.lobby import LobbyService
from app.services.orchestrator import end_vote, start_vote
from app.services.session import GameSession
from app.services.timer import TimerManager

logger = logging.getLogger(__name__)
router = Router(name="day")


def _find_session_for_user(
    games: LobbyService, user_id: int
) -> GameSession | None:
    """Locate the live game in which ``user_id`` is a player."""
    for session in games._sessions.values():  # noqa: SLF001 — registry access
        if user_id in session.players:
            return session
    return None


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.VOTE}:"))
async def cb_vote(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "day_vote":
        await query.answer(t("errors.wrong_time_vote"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    service = DayService(game)
    try:
        async with game.lock:
            service.cast_vote(query.from_user.id, target_id)
    except GameError as exc:
        await query.answer(f"⚠️ {t(exc.key, **exc.kwargs)}", show_alert=True)
        return

    await query.answer()
    try:
        await query.message.edit_text(t("day.vote_done_pm"))
    except TelegramAPIError:
        pass

    # If every alive player has voted, resolve the vote immediately.
    if service.all_required_voted():
        timers.cancel(game.game_id)
        # Open a fresh session: the request session may be closed before
        # end_vote finishes its DB work (kills, stats, end-of-game).
        factory = get_session_factory()
        async with factory() as fresh_session:
            try:
                await end_vote(bot, games, timers, fresh_session, game, tracker=tracker)
                await fresh_session.commit()
            except Exception:
                await fresh_session.rollback()
                raise



@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.NOMINATE}:"))
async def cb_nominate(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Register a nomination during the optional "who stands trial" stage."""
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "day_nomination":
        await query.answer(t("errors.wrong_time_vote"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    service = DayService(game)
    try:
        async with game.lock:
            service.nominate(query.from_user.id, target_id)
    except GameError as exc:
        await query.answer(f"⚠️ {t(exc.key, **exc.kwargs)}", show_alert=True)
        return

    await query.answer()
    try:
        await query.message.edit_text(t("day.nomination_done_pm"))
    except TelegramAPIError:
        pass

    # Everyone nominated -> move on to the vote without waiting for the timer.
    if service.all_required_nominated():
        timers.cancel(game.game_id)
        factory = get_session_factory()
        async with factory() as fresh_session:
            try:
                await start_vote(
                    bot, games, timers, fresh_session, game, tracker=tracker
                )
                await fresh_session.commit()
            except Exception:
                await fresh_session.rollback()
                raise
