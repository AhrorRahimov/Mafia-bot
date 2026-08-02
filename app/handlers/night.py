"""Night-action callbacks (private chat) + fallback slash commands.

Each active role gets a private message with targets during the night.
Selecting a target triggers the corresponding inline callback here.
We also accept text commands ``/kill``, ``/heal``, ``/check`` as a
fallback for users whose inline buttons did not arrive (e.g. they
blocked the bot in DM and unblocked later) or who simply prefer typing.

The callback resolves the right ``GameSession`` regardless of which
chat the update came from — we look it up by the acting user.

Robustness: ``end_night`` reuses the request session only inside the
handler; elsewhere (timers) it opens a fresh session.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.game.exceptions import GameError
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction, parse_callback
from app.services.lobby import LobbyService
from app.services.night import NightService
from app.services.orchestrator import end_night
from app.services.session import GameSession
from app.services.timer import TimerManager

logger = logging.getLogger(__name__)
router = Router(name="night")


def _find_session_for_user(
    games: LobbyService, user_id: int
) -> GameSession | None:
    """Locate the live game in which ``user_id`` is a player."""
    for session in games._sessions.values():  # noqa: SLF001 — registry access
        if user_id in session.players:
            return session
    return None


def _resolve_target_by_name(game: GameSession, name: str) -> int | None:
    """Find an alive player by case-insensitive name prefix.

    Used by the ``/kill``, ``/heal``, ``/check`` text commands.
    Returns the matched ``user_id`` or ``None``.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    # Exact match first.
    for player in game.alive_players:
        if player.full_name.lower() == needle:
            return player.user_id
    # Prefix match.
    matches = [
        p for p in game.alive_players
        if p.full_name.lower().startswith(needle)
    ]
    if len(matches) == 1:
        return matches[0].user_id
    return None


# --- Mafia kill (inline button) ---------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.MAFIA_KILL}:"))
async def cb_mafia_kill(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_mafia_kill(bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker)
    await query.answer()


async def _perform_mafia_kill(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.mafia_kill(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(bot, actor_id, t("night.mafia_done_pm", footer=t("night.action_done")))
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Detective check (inline button) ----------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.DETECTIVE_CHECK}:"))
async def cb_detective_check(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_detective_check(bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker)
    await query.answer(t("night.detective_toast_pending"))


async def _perform_detective_check(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.detective_check(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(bot, actor_id, t("night.detective_done_pm", footer=t("night.action_done")))
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Detective shot (inline button) -----------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.DETECTIVE_SHOOT}:"))
async def cb_detective_shoot(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_detective_shoot(
        bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker
    )
    await query.answer()


async def _perform_detective_shoot(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.detective_shoot(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(
        bot, actor_id,
        t("night.detective_shoot_done_pm", footer=t("night.action_done")),
    )
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Doctor heal (inline button) --------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.DOCTOR_HEAL}:"))
async def cb_doctor_heal(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_doctor_heal(bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker)
    await query.answer()


async def _perform_doctor_heal(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.doctor_heal(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(bot, actor_id, t("night.doctor_done_pm", footer=t("night.action_done")))
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Whore block (inline button) --------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.WHORE_BLOCK}:"))
async def cb_whore_block(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_whore_block(bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker)
    await query.answer()


async def _perform_whore_block(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.whore_block(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(bot, actor_id, t("night.whore_done_pm", footer=t("night.action_done")))
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)




# --- Don search for the detective (inline button) ---------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.DON_SEARCH}:"))
async def cb_don_search(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_don_search(
        bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker
    )
    await query.answer()


async def _perform_don_search(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.don_search(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(bot, actor_id, t("night.don_search_done_pm"))
    # The search is optional and never blocks the night, so we do not call
    # _maybe_close_night here.


# --- Lawyer defence (inline button) -----------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.LAWYER_DEFEND}:"))
async def cb_lawyer_defend(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_lawyer_defend(
        bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker
    )
    await query.answer()


async def _perform_lawyer_defend(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.lawyer_defend(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(
        bot, actor_id,
        t("night.lawyer_done_pm", footer=t("night.action_done")),
    )
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Maniac kill (inline button) --------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.MANIAC_KILL}:"))
async def cb_maniac_kill(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    game = _find_session_for_user(games, query.from_user.id)
    if game is None or game.phase.value != "night":
        await query.answer(t("errors.wrong_time_action"), show_alert=True)
        return

    _, target_id = parse_callback(query.data)
    await _perform_maniac_kill(
        bot, games, timers, game, query.from_user.id, target_id, t, tracker=tracker
    )
    await query.answer()


async def _perform_maniac_kill(
    bot: Bot, games: LobbyService, timers: TimerManager,
    game: GameSession,
    actor_id: int, target_id: int, t: Translator, *, tracker=None,
) -> None:
    service = NightService(game)
    try:
        async with game.lock:
            service.maniac_kill(actor_id, target_id)
    except GameError as exc:
        await _safe_alert(bot, actor_id, f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await _safe_pm(
        bot, actor_id,
        t("night.maniac_done_pm", footer=t("night.action_done")),
    )
    await _maybe_close_night(bot, games, timers, game, tracker=tracker)


# --- Fallback slash commands ------------------------------------------

@router.message(Command("kill"))
async def cmd_kill(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/kill <name>`` for the mafia during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value in ("mafia", "don"),
        perform=_perform_mafia_kill,
        action_key="night.mafia_prompt",
    )


@router.message(Command("check"))
async def cmd_check(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/check <name>`` for the detective during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "detective",
        perform=_perform_detective_check,
        action_key="night.detective_prompt",
    )


@router.message(Command("shoot"))
async def cmd_shoot(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/shoot <name>`` for the detective's bullet."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "detective",
        perform=_perform_detective_shoot,
        action_key="night.detective_shoot_prompt",
    )


@router.message(Command("heal"))
async def cmd_heal(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/heal <name>`` for the doctor during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "doctor",
        perform=_perform_doctor_heal,
        action_key="night.doctor_prompt",
    )


@router.message(Command("block"))
async def cmd_block(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/block <name>`` for the whore during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "whore",
        perform=_perform_whore_block,
        action_key="night.whore_prompt",
    )




@router.message(Command("maniac"))
async def cmd_maniac(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/maniac <name>`` for the maniac during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "maniac",
        perform=_perform_maniac_kill,
        action_key="night.maniac_prompt",
    )


@router.message(Command("defend"))
async def cmd_defend(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/defend <name>`` for the lawyer during the night."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "lawyer",
        perform=_perform_lawyer_defend,
        action_key="night.lawyer_prompt",
    )


@router.message(Command("search"))
async def cmd_search(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Fallback: ``/search <name>`` for the don hunting the detective."""
    await _text_action(
        message, command, games, timers, bot, t, tracker,
        role_check=lambda p: p.role.value == "don",
        perform=_perform_don_search,
        action_key="night.don_search_prompt",
    )


async def _text_action(
    message: Message,
    command: CommandObject,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
    *,
    role_check,
    perform,
    action_key: str,
) -> None:
    """Shared dispatcher for ``/kill`` / ``/check`` / ``/heal`` / ``/block``."""
    game = _find_session_for_user(games, message.from_user.id)
    if game is None:
        await message.answer(t("commands.no_game"))
        return
    if game.phase.value != "night":
        await message.answer(t("errors.wrong_time_action"))
        return
    player = game.get(message.from_user.id)
    if player is None or not player.is_alive or not role_check(player):
        await message.answer(t("errors.wrong_role"))
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.answer(t(action_key))
        return

    target_id = _resolve_target_by_name(game, arg)
    if target_id is None:
        await message.answer(t("commands.target_not_found_name", name=arg))
        return

    await perform(bot, games, timers, game, message.from_user.id, target_id, t, tracker=tracker)


# --- Helpers -----------------------------------------------------------

async def _maybe_close_night(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    game: GameSession,
    *,
    tracker=None,
) -> None:
    """If every required role has acted, resolve the night immediately.

    Opens a fresh DB session because the request session that triggered
    the action may be gone before ``end_night`` finishes its DB work.
    """
    if not NightService(game).all_required_acted():
        return
    timers.cancel(game.game_id)
    factory = get_session_factory()
    async with factory() as fresh_session:
        try:
            await end_night(bot, games, timers, fresh_session, game, tracker=tracker)
            await fresh_session.commit()
        except Exception:
            await fresh_session.rollback()
            raise


async def _safe_pm(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        logger.warning("Could not DM user %s.", user_id)


async def _safe_alert(bot: Bot, user_id: int, text: str) -> None:
    """Send a private message — used by action error paths.

    ``bot`` is accepted for call-site symmetry with ``_safe_pm``; the
    message is delivered via the same private-chat channel.
    """
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        logger.warning("Could not DM user %s.", user_id)
