"""Basic commands: /start /help /cancel /stats /lang /alive /role."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import GameRepo, StatsRepo
from app.game.enums import Winner
from app.game.exceptions import GameError
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction
from app.keyboards.inline import language_kb, top_kb
from app.services.lobby import LobbyService
from app.services.orchestrator import cancel_running_game
from app.services.timer import TimerManager
from app.db.progress_repo import (
    AchievementRepo,
    ChatLeaderboardRepo,
    LeaderboardRepo,
    RatingRepo,
)
from app.game.achievements import ACHIEVEMENTS
from app.services.progress import current_season, profile_snapshot
from app.texts import (
    CHAT_BOARD,
    GROUP_TOP_BOARDS,
    TOP_BOARDS,
    board_text,
    chat_board_text,
    leaderboard_text,
    profile_text,
    season_top_text,
    stats_text,
)

logger = logging.getLogger(__name__)
router = Router(name="basic")

# Language code -> human-readable name (used for the /lang confirmation).
_LANGUAGE_NAMES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "uz": "🇺🇿 O'zbekcha",
}


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    session: AsyncSession,
    i18n,
    t: Translator,
) -> None:
    """Greet the user.

    Brand-new users (no row in ``user_stats`` yet) are forced into the
    language picker so they can use the bot in a language they understand
    before seeing any localised text they may not read.
    """
    user_id = message.from_user.id
    stats_repo = StatsRepo(session)
    known = await stats_repo.is_known(user_id)

    # Opening the bot in a private chat makes the user reachable for secret
    # role delivery, which is a prerequisite for joining a lobby. Mark it
    # AFTER the is_known check so brand-new users still see the picker.
    if message.chat.type == "private":
        full_name = message.from_user.full_name or f"User {user_id}"
        await stats_repo.mark_dm_ready(user_id, full_name)

    if not known:
        # First launch — show a multilingual picker prompt.
        # The prompt is intentionally multilingual so any user can find
        # their language regardless of the current default.
        await message.answer(
            i18n.translator_for("ru")("start.welcome_choose_language"),
            reply_markup=language_kb(),
        )
        return

    # Returning user — normal greeting in their saved language.
    await message.answer(t("start.greeting"))


@router.message(Command("help"))
async def cmd_help(message: Message, t: Translator) -> None:
    from app.game.constants import MAX_PLAYERS, MIN_PLAYERS
    await message.answer(t("help.text", min=MIN_PLAYERS, max=MAX_PLAYERS))


@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    stats = await StatsRepo(session).get(message.from_user.id)
    await message.answer(stats_text(t, stats))


async def _render_board(
    session: AsyncSession,
    t: Translator,
    board: str,
    *,
    chat_id: int | None = None,
    chat_title: str = "",
) -> str:
    """Text for one leaderboard.

    "chat" is the board of the group the command was sent from and is
    counted from that chat's own game history; "season" is the MMR ladder
    of the running season; the rest are all-time totals that follow a
    player across every chat.
    """
    boards = LeaderboardRepo(session)
    if board == CHAT_BOARD:
        if chat_id is None:
            # No group to scope to (a private chat): fall back to the
            # global board rather than showing an error.
            return board_text(t, "wins", await boards.by_wins())
        rows = await ChatLeaderboardRepo(session).by_chat(chat_id)
        return chat_board_text(t, rows, title=chat_title)
    if board == "season":
        season = await current_season(session)
        rows = await RatingRepo(session).top(season.id, limit=10)
        if not rows:
            # A brand-new season has no ladder yet - show the classic
            # all-time board instead of an empty screen.
            return leaderboard_text(t, await StatsRepo(session).top(limit=10))
        return season_top_text(t, season.name, rows)
    if board == "coins":
        return board_text(t, board, await boards.by_coins())
    if board == "winrate":
        return board_text(t, board, await boards.by_winrate())
    if board == "streak":
        return board_text(t, board, await boards.by_streak())
    return board_text(t, "wins", await boards.by_wins())


@router.message(Command("top"))
async def cmd_top(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Leaderboards: this chat, season MMR, wins, coins, winrate, streak.

    In a group the chat's own board opens first - that is the ranking the
    people in the room actually argue about - with the global boards one
    tap away. In a private chat there is no group to scope to, so only
    the global boards are offered.
    """
    in_group = message.chat.type != "private"
    boards = GROUP_TOP_BOARDS if in_group else TOP_BOARDS
    active = CHAT_BOARD if in_group else "season"
    text = await _render_board(
        session,
        t,
        active,
        chat_id=message.chat.id if in_group else None,
        chat_title=message.chat.title or "",
    )
    await message.answer(text, reply_markup=top_kb(t, boards, active))


@router.callback_query(
    lambda c: c.data and c.data.startswith(f"{CallbackAction.TOP}:")
)
async def cb_top(
    query: CallbackQuery,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Switch between the boards in place."""
    board = (query.data or "").split(":", 1)[1]
    chat = query.message.chat if query.message else None
    in_group = chat is not None and chat.type != "private"
    boards = GROUP_TOP_BOARDS if in_group else TOP_BOARDS
    if board not in boards:
        await query.answer()
        return
    text = await _render_board(
        session,
        t,
        board,
        chat_id=chat.id if in_group else None,
        chat_title=(chat.title or "") if in_group else "",
    )
    try:
        await query.message.edit_text(
            text, reply_markup=top_kb(t, boards, board)
        )
    except TelegramAPIError:
        logger.debug("Could not switch the leaderboard to %s.", board)
    await query.answer()


@router.message(Command("me", "profile"))
async def cmd_me(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Personal profile: rating, streaks, per-role winrate, achievements."""
    user_id = message.from_user.id
    await StatsRepo(session).upsert_touch(
        user_id,
        message.from_user.full_name or str(user_id),
        message.from_user.username,
    )
    snapshot = await profile_snapshot(session, user_id)
    if snapshot["stats"] is None:
        await message.answer(t("profile.empty"))
        return
    await message.answer(
        profile_text(t, snapshot, achievements_total=len(ACHIEVEMENTS))
    )


@router.message(Command("lang"))
async def cmd_lang(message: Message, t: Translator) -> None:
    """Show language picker (works in private and group chats)."""
    await message.answer(t("lang.prompt"), reply_markup=language_kb())


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.SET_LANG}:"))
async def cb_set_lang(
    query: CallbackQuery,
    session: AsyncSession,
    i18n,
) -> None:
    """Persist the selected language and confirm on the new language."""
    _, lang_code = query.data.split(":", 1)
    if lang_code not in _LANGUAGE_NAMES:
        await query.answer("⚠️", show_alert=True)
        return

    full_name = query.from_user.full_name or f"User {query.from_user.id}"
    stats_repo = StatsRepo(session)
    await stats_repo.set_language(
        query.from_user.id, lang_code, full_name=full_name
    )
    # If the picker was used in the bot's DM, the user is now reachable.
    if query.message is not None and query.message.chat.type == "private":
        await stats_repo.mark_dm_ready(query.from_user.id, full_name)

    t = i18n.translator_for(lang_code)
    try:
        await query.message.edit_text(
            t("lang.changed", lang_name=_LANGUAGE_NAMES[lang_code]),
        )
    except TelegramAPIError:
        # If the message can't be edited (e.g. it had no keyboard), send fresh.
        try:
            await query.message.answer(
                t("lang.changed", lang_name=_LANGUAGE_NAMES[lang_code]),
            )
        except TelegramAPIError:
            pass
    await query.answer()


@router.message(Command("alive"))
async def cmd_alive(
    message: Message,
    games: LobbyService,
    t: Translator,
) -> None:
    """List alive players of the current game (if any)."""
    if message.chat.type == "private":
        # In private, look up any game the user is in.
        game = None
        for s in games._sessions.values():  # noqa: SLF001
            if message.from_user.id in s.players:
                game = s
                break
    else:
        game = games.get(message.chat.id)

    if game is None:
        await message.answer(t("commands.no_game"))
        return

    alive = game.alive_players
    if not alive:
        await message.answer(t("commands.alive_empty"))
        return

    names = "\n".join(f"• {p.full_name}" for p in alive)
    await message.answer(
        t("commands.alive_header", count=len(alive)) + "\n" + names
    )


@router.message(Command("role"))
async def cmd_role(
    message: Message,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Re-send the caller their role (private chat only, during a game)."""
    if message.chat.type != "private":
        await message.answer(t("commands.role_private_only"))
        return

    game = None
    for s in games._sessions.values():  # noqa: SLF001
        if message.from_user.id in s.players:
            game = s
            break
    if game is None:
        await message.answer(t("commands.role_no_game"))
        return

    player = game.get(message.from_user.id)
    if player is None:
        await message.answer(t("commands.role_no_game"))
        return

    from app.texts import your_role
    await message.answer(your_role(t, player.role))


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
    tracker,
) -> None:
    """Cancel the current lobby or running game (creator only)."""
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return

    chat_id = message.chat.id
    active = games.get(chat_id)

    # 1) Active running game — stop timers and tear down the session.
    if active is not None:
        if active.creator_id != message.from_user.id:
            await message.answer(t("errors.only_creator_cancel_game"))
            return
        timers.cancel(active.game_id)
        # Restore chat permissions before removing the game state.
        await cancel_running_game(bot, active, tracker=tracker)
        games.remove(chat_id)
        game_row = await GameRepo(session).get(active.game_id)
        if game_row is not None:
            await GameRepo(session).finish(
                game_row,
                winner=Winner.NONE.value,
                rounds_played=active.round_number,
            )
        await message.answer(t("errors.game_cancelled_by_creator"))
        return

    # 2) Otherwise try to cancel an open lobby.
    try:
        await games.cancel(session, chat_id, message.from_user.id)
    except GameError:  # LobbyError variants all mean "nothing to cancel"
        await message.answer(t("errors.no_active_game"))
        return
    await message.answer(t("lobby.cancelled_by_creator"))
