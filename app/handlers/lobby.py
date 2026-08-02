"""Lobby commands and inline buttons: /newgame, /join, /leave, /startgame, /extend, /shorten."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import GameRepo, PlayerRepo, StatsRepo
from app.game.constants import (
    DISCUSSION_DURATION_PRESETS,
    LOBBY_EXTEND_STEP,
    LOBBY_MAX_TIMEOUT,
    LOBBY_MIN_TIMEOUT,
    LOBBY_TIMEOUT,
    MAX_PLAYERS,
    MIN_PLAYERS,
    NIGHT_DURATION_PRESETS,
    VOTE_DURATION_PRESETS,
)
from app.game.exceptions import CREATOR_LEFT, GameError
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction
from app.keyboards.inline import lobby_kb, settings_kb
from app.db.admin_repo import BanRepo
from app.services.admin import runtime_config
from app.services.lobby import LobbyService
from app.services.orchestrator import start_night
from app.services.session import GameSession
from app.services.timer import TimerManager
from app.texts import lobby_opened, mafia_extra_for, your_role

logger = logging.getLogger(__name__)
router = Router(name="lobby")


async def _entry_blocked(
    session: AsyncSession, t: Translator, *, user_id: int, chat_id: int
) -> str:
    """Return a refusal text, or an empty string when play is allowed.

    Checked at both entry points into a game (creating a lobby and
    joining one) so a banned player cannot slip in through the button
    when the command is blocked, or vice versa.
    """
    if await runtime_config.is_maintenance(session):
        return t("admin.maintenance_notice")
    bans = BanRepo(session)
    if await bans.is_chat_banned(chat_id):
        return t("admin.chat_blocked")
    ban = await bans.get_ban(user_id)
    if ban is not None:
        return t(
            "admin.you_are_banned",
            until=ban.until.strftime("%Y-%m-%d") if ban.until
            else t("admin.ban_forever"),
            reason=ban.reason or "-",
        )
    return ""


# --- Helpers -----------------------------------------------------------

def _display_name(message: Message | CallbackQuery) -> str:
    user = message.from_user
    return user.full_name or f"User {user.id}"


async def _dm_required(bot: Bot, reply, t: Translator) -> None:
    """Tell a user they must open the bot in DM (press /start) before joining.

    Adds a deep-link button to the bot's private chat when the username is
    available. The bot username is cached per-bot after the first lookup so
    we do not hit the API on every ``/join``.
    """
    username = getattr(bot, "_mafia_username_cache", None)
    if username is None and not getattr(bot, "_mafia_username_miss", False):
        try:
            me = await bot.get_me()
            username = me.username
            bot._mafia_username_cache = username  # type: ignore[attr-defined]
        except TelegramAPIError:
            # Avoid retrying on every join if the lookup keeps failing.
            bot._mafia_username_miss = True  # type: ignore[attr-defined]
    reply_markup = None
    if username:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t("button.open_bot"), url=f"https://t.me/{username}?start=1"
        )
        reply_markup = builder.as_markup()
    await reply(t("errors.dm_required"), reply_markup=reply_markup)


def _cycle(presets: tuple[int, ...], current: int) -> int:
    """Return the next value in a preset tuple, wrapping around."""
    try:
        idx = presets.index(current)
    except ValueError:
        return presets[0]
    return presets[(idx + 1) % len(presets)]


async def _get_lobby_row(session: AsyncSession, chat_id: int):
    return await GameRepo(session).get_active(chat_id)


async def _refresh_lobby_card(
    bot: Bot,
    chat_id: int,
    game_id: int,
    creator_name: str,
    players_names: list[str],
    t: Translator,
) -> None:
    """Post a fresh lobby card. Used by /newgame."""
    await bot.send_message(
        chat_id,
        lobby_opened(t, creator_name, players_names),
        reply_markup=lobby_kb(game_id, t),
    )


# --- /newgame ----------------------------------------------------------

@router.message(Command("newgame", "game", "mafia"))
async def cmd_newgame(
    message: Message,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
) -> None:
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return

    blocked = await _entry_blocked(
        session, t, user_id=message.from_user.id, chat_id=message.chat.id
    )
    if blocked:
        await message.answer(blocked)
        return

    # The creator must be reachable in DM (secret role delivery).
    if not await StatsRepo(session).is_dm_ready(message.from_user.id):
        await _dm_required(bot, message.answer, t)
        return

    chat_id = message.chat.id
    creator_name = _display_name(message)

    # Post the lobby card FIRST so we have a message_id to edit in place
    # during the gathering countdown. Roll back the message on failure.
    card = await message.answer(
        lobby_opened(t, creator_name, [creator_name]),
        reply_markup=lobby_kb(0, t),  # game_id patched after creation
    )
    card_message_id = getattr(card, "message_id", None)

    try:
        game = await games.create_lobby(
            db=session, chat_id=chat_id,
            creator_id=message.from_user.id, creator_name=creator_name,
            bot=bot, card_message_id=card_message_id,
            username=message.from_user.username,
        )
    except GameError as exc:
        # Remove the premature card; the lobby was not created.
        try:
            await card.delete()
        except TelegramAPIError:
            pass
        await message.answer(f"⚠️ {t(exc.key, **exc.kwargs)}")
        return

    # Patch the keyboard with the real game_id (button callbacks carry it).
    players = await PlayerRepo(session).list_by_game(game.id)
    names = [p.full_name for p in players]
    from app.texts import lobby_opened_countdown
    meta = games._lobby_meta.get(chat_id)  # noqa: SLF001
    remaining = (
        max(0, int(meta.deadline - monotonic())) if meta is not None else LOBBY_TIMEOUT
    )
    try:
        await card.edit_text(
            lobby_opened_countdown(t, creator_name, names, remaining),
            reply_markup=lobby_kb(game.id, t),
        )
    except TelegramAPIError:
        pass


# --- /join -------------------------------------------------------------

@router.message(Command("join"))
async def cmd_join(
    message: Message,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
) -> None:
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return

    await _do_join(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        full_name=_display_name(message),
        username=message.from_user.username,
        games=games,
        session=session,
        t=t,
        bot=bot,
        reply=message.answer,
    )


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.JOIN}:"))
async def cb_join(
    query: CallbackQuery,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
) -> None:
    await _do_join(
        chat_id=query.message.chat.id,
        user_id=query.from_user.id,
        full_name=_display_name(query),
        username=query.from_user.username,
        games=games,
        session=session,
        t=t,
        bot=bot,
        reply=lambda text, **kw: query.message.answer(text, **kw),
    )
    await query.answer()


async def _do_join(
    *,
    chat_id: int,
    user_id: int,
    full_name: str,
    username: str | None = None,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
    reply,
) -> None:
    blocked = await _entry_blocked(
        session, t, user_id=user_id, chat_id=chat_id
    )
    if blocked:
        await reply(blocked)
        return

    # Players must have opened the bot in DM so we can send them their role.
    if not await StatsRepo(session).is_dm_ready(user_id):
        await _dm_required(bot, reply, t)
        return
    try:
        game = await games.join(
            db=session, chat_id=chat_id, user_id=user_id,
            full_name=full_name, username=username,
        )
    except GameError as exc:
        await reply(f"⚠️ {t(exc.key, **exc.kwargs)}")
        return

    players = await PlayerRepo(session).list_by_game(game.id)
    await reply(
        t("lobby.joined", name=full_name, count=len(players), min=MIN_PLAYERS, max=MAX_PLAYERS),
    )


# --- /leave ------------------------------------------------------------

@router.message(Command("leave"))
async def cmd_leave(
    message: Message,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
) -> None:
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return
    try:
        await games.leave(session, message.chat.id, message.from_user.id)
    except GameError as exc:
        if exc.key == CREATOR_LEFT:
            await message.answer(t("lobby.creator_left_dissolved"))
            return
        await message.answer(f"⚠️ {t(exc.key, **exc.kwargs)}")
        return
    await message.answer(t("lobby.left", name=_display_name(message)))


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.LEAVE}:"))
async def cb_leave(
    query: CallbackQuery,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
) -> None:
    try:
        await games.leave(session, query.message.chat.id, query.from_user.id)
    except GameError as exc:
        if exc.key == CREATOR_LEFT:
            await query.message.answer(t("lobby.dissolved_creator_left"))
            try:
                await query.message.edit_text(t("lobby.closed"))
            except TelegramAPIError:
                pass
            await query.answer()
            return
        await query.answer(f"⚠️ {t(exc.key, **exc.kwargs)}", show_alert=True)
        return
    await query.answer(t("lobby.left", name=_display_name(query)))


# --- Settings menu (creator-only) -------------------------------------

@router.message(Command("settings"))
async def cmd_settings(
    message: Message,
    games: LobbyService,
    t: Translator,
) -> None:
    """Open the lobby settings menu without hunting for the lobby card."""
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return

    chat_id = message.chat.id
    creator_id = games._lobby_creators.get(chat_id)  # noqa: SLF001
    if creator_id is None:
        await message.answer(t("lobby.no_lobby_here"))
        return
    if message.from_user.id != creator_id:
        await message.answer(t("lobby.not_creator_settings"))
        return

    await message.answer(
        t("settings.title"),
        reply_markup=settings_kb(games.get_settings(chat_id), t),
    )


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.SETTINGS}:"))
async def cb_settings(
    query: CallbackQuery,
    games: LobbyService,
    t: Translator,
) -> None:
    """Open / mutate / close the creator-only lobby settings menu.

    Callback args are string keywords, so this handler parses the payload
    manually rather than using ``parse_callback`` (which int()s the arg).
    """
    chat_id = query.message.chat.id
    _, _, arg = query.data.partition(":")

    creator_id = games._lobby_creators.get(chat_id)  # noqa: SLF001
    if creator_id is None:
        await query.answer(t("lobby.no_lobby_here"), show_alert=True)
        return
    if query.from_user.id != creator_id:
        await query.answer(t("lobby.not_creator_settings"), show_alert=True)
        return

    settings = games.get_settings(chat_id)

    if arg == "open":
        await query.message.answer(
            t("settings.title"), reply_markup=settings_kb(settings, t)
        )
        await query.answer()
        return
    if arg == "close":
        try:
            await query.message.edit_text(t("settings.closed"))
        except TelegramAPIError:
            pass
        await query.answer()
        return

    if arg == "night":
        settings.night_duration = _cycle(
            NIGHT_DURATION_PRESETS, settings.night_duration
        )
    elif arg == "disc":
        settings.discussion_duration = _cycle(
            DISCUSSION_DURATION_PRESETS, settings.discussion_duration
        )
    elif arg == "vote":
        settings.vote_duration = _cycle(
            VOTE_DURATION_PRESETS, settings.vote_duration
        )
    elif arg == "don":
        settings.enable_don = not settings.enable_don
    elif arg == "whore":
        settings.enable_whore = not settings.enable_whore
    elif arg == "sergeant":
        settings.enable_sergeant = not settings.enable_sergeant
    elif arg == "maniac":
        settings.enable_maniac = not settings.enable_maniac
    elif arg == "lawyer":
        settings.enable_lawyer = not settings.enable_lawyer
    elif arg == "shoot":
        settings.detective_can_shoot = not settings.detective_can_shoot
    elif arg == "mafia_count":
        # None (automatic) -> 1 -> 2 -> 3 -> None
        cap = max(1, (MAX_PLAYERS - 1) // 2)
        current = settings.mafia_count
        if current is None:
            settings.mafia_count = 1
        elif current >= cap:
            settings.mafia_count = None
        else:
            settings.mafia_count = current + 1
    elif arg == "reveal":
        settings.reveal_roles = not settings.reveal_roles
    elif arg == "nomination":
        settings.nomination_mode = not settings.nomination_mode
    elif arg == "skip":
        settings.allow_skip_vote = not settings.allow_skip_vote
    elif arg == "dead_chat":
        settings.dead_chat = not settings.dead_chat
    elif arg == "afk":
        settings.afk_autoskip = not settings.afk_autoskip
    else:
        await query.answer()
        return

    try:
        await query.message.edit_reply_markup(
            reply_markup=settings_kb(settings, t)
        )
    except TelegramAPIError:
        pass
    await query.answer(t("settings.saved"))


# --- Rematch -----------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.REMATCH}:"))
async def cb_rematch(
    query: CallbackQuery,
    games: LobbyService,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
) -> None:
    """Open a fresh lobby right after a finished game."""
    chat_id = query.message.chat.id
    starter_id = query.from_user.id
    starter_name = _display_name(query)

    if not await StatsRepo(session).is_dm_ready(starter_id):
        await query.answer(t("errors.dm_required"), show_alert=True)
        return

    # Drop the rematch button so the lobby cannot be opened twice.
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    card = await query.message.answer(
        lobby_opened(t, starter_name, [starter_name]),
        reply_markup=lobby_kb(0, t),
    )
    card_message_id = getattr(card, "message_id", None)

    try:
        game = await games.create_lobby(
            db=session, chat_id=chat_id,
            creator_id=starter_id, creator_name=starter_name,
            bot=bot, card_message_id=card_message_id,
            username=query.from_user.username,
        )
    except GameError as exc:
        try:
            await card.delete()
        except TelegramAPIError:
            pass
        await query.answer(t(exc.key, **exc.kwargs), show_alert=True)
        return

    players = await PlayerRepo(session).list_by_game(game.id)
    names = [p.full_name for p in players]
    from app.texts import lobby_opened_countdown
    meta = games._lobby_meta.get(chat_id)  # noqa: SLF001
    remaining = (
        max(0, int(meta.deadline - monotonic())) if meta is not None else LOBBY_TIMEOUT
    )
    try:
        await card.edit_text(
            lobby_opened_countdown(t, starter_name, names, remaining),
            reply_markup=lobby_kb(game.id, t),
        )
    except TelegramAPIError:
        pass

    await query.message.answer(t("lobby.rematch_started"))
    await query.answer()


# --- /startgame --------------------------------------------------------

@router.message(Command("startgame"))
async def cmd_startgame(
    message: Message,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
    tracker,
) -> None:
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return
    await _do_start(
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        games=games,
        timers=timers,
        session=session,
        t=t,
        bot=bot,
        tracker=tracker,
        reply=message.answer,
    )


@router.callback_query(lambda c: c.data and c.data.startswith(f"{CallbackAction.START}:"))
async def cb_start(
    query: CallbackQuery,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
    tracker,
) -> None:
    await _do_start(
        chat_id=query.message.chat.id,
        actor_id=query.from_user.id,
        games=games,
        timers=timers,
        session=session,
        t=t,
        bot=bot,
        tracker=tracker,
        reply=lambda text, **kw: query.message.answer(text, **kw),
    )
    await query.answer()


async def _do_start(
    *,
    chat_id: int,
    actor_id: int,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    t: Translator,
    bot: Bot,
    tracker,
    reply,
) -> None:
    # Only the lobby creator can start the game.
    active = await _get_lobby_row(session, chat_id)
    if active is None or active.status != "lobby":
        await reply(t("lobby.no_lobby_here"))
        return
    if active.creator_id != actor_id:
        await reply(t("lobby.not_creator_start"))
        return

    try:
        game_session = await games.start(db=session, chat_id=chat_id)
    except GameError as exc:
        await reply(f"⚠️ {t(exc.key, **exc.kwargs)}")
        return

    # ``start()`` already tears down the lobby timer + card refresh.
    # Notify the group…
    await reply(
        t("lobby.game_started", count=len(game_session.players)),
    )
    # …and DM each player their role (in their own language).
    await _send_roles(bot, session, game_session)

    # Kick off the first night. Use a fresh session for safety so the
    # request-scoped session isn't held across the (best-effort) DMs.
    from app.db.session import get_session_factory
    async with get_session_factory()() as night_session:
        try:
            await start_night(bot, games, timers, night_session, game_session, tracker=tracker)
            await night_session.commit()
        except Exception:
            await night_session.rollback()
            raise


# --- /extend & /shorten (lobby gathering time) ------------------------

@router.message(Command("extend"))
async def cmd_extend(
    message: Message,
    games: LobbyService,
    t: Translator,
) -> None:
    """Add LOBBY_EXTEND_STEP seconds to the gathering countdown."""
    await _do_adjust(message, games, t, +LOBBY_EXTEND_STEP)


@router.message(Command("shorten"))
async def cmd_shorten(
    message: Message,
    games: LobbyService,
    t: Translator,
) -> None:
    """Remove LOBBY_EXTEND_STEP seconds from the gathering countdown."""
    await _do_adjust(message, games, t, -LOBBY_EXTEND_STEP)


async def _do_adjust(
    message: Message,
    games: LobbyService,
    t: Translator,
    delta: int,
) -> None:
    """Shared body for /extend (+delta) and /shorten (-delta).

    Any lobby participant may use them. The reply is auto-deleted after a
    short delay so it does not clutter the chat — the live card already
    shows the new countdown.
    """
    if message.chat.type == "private":
        await message.answer(t("errors.not_in_group"))
        return
    chat_id = message.chat.id
    remaining = games.adjust_deadline(chat_id, delta)
    if remaining is None:
        await message.answer(t("lobby.no_lobby_here"))
        return
    # Cap messages: distinguish "hit the ceiling/floor" from a normal nudge.
    if delta > 0 and remaining >= LOBBY_MAX_TIMEOUT:
        note = t("lobby.extend_max", seconds=LOBBY_MAX_TIMEOUT)
    elif delta < 0 and remaining <= LOBBY_MIN_TIMEOUT:
        note = t("lobby.shorten_min", seconds=LOBBY_MIN_TIMEOUT)
    else:
        note = t(
            "lobby.extend_added" if delta > 0 else "lobby.shortened",
            seconds=abs(delta),
            remaining=remaining,
        )
    sent = await message.answer(note)
    # Best-effort auto-delete of the transient confirmation after 8s.
    asyncio.create_task(_auto_delete(message.bot, chat_id, sent.message_id, 8.0))


async def _auto_delete(bot, chat_id: int, message_id: int, delay: float) -> None:
    """Delete a bot message after ``delay`` seconds (best-effort)."""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass
    except Exception:  # noqa: BLE001
        pass


# --- Role DMs ----------------------------------------------------------

async def _send_roles(
    bot: Bot, session: AsyncSession, game_session: GameSession
) -> None:
    """DM each player their role in their own language. Best-effort."""
    from app.db.repo import StatsRepo
    from app.i18n import get_i18n

    i18n = get_i18n()
    stats_repo = StatsRepo(session)

    card_results = getattr(game_session, "card_results", {}) or {}

    for user_id, player in game_session.players.items():
        lang = await stats_repo.get_language(user_id)
        t = i18n.translator_for(lang)
        extra = mafia_extra_for(t, game_session, user_id)
        # Tell card owners whether their card worked or came back to them.
        if user_id in card_results:
            note = t(
                "card.honoured" if card_results[user_id] else "card.refunded"
            )
            extra = f"{extra}\n\n{note}" if extra else note
        try:
            await bot.send_message(user_id, your_role(t, player.role, extra))
        except TelegramAPIError:
            logger.warning(
                "Could not DM user %s their role (bot blocked?).", user_id
            )
