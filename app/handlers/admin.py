"""Admin panel: ``/admin`` entry point, ``/adminhelp`` and text commands.

Two interfaces over the same logic:

* **In a private chat** ``/admin`` opens an inline-button panel
  (rendered in ``app.handlers.admin_panel``).
* **Anywhere** the same operations are available as text commands, which
  is what you want in a group during a live game (``/forceend`` etc.)
  and for anything that needs arguments (``/ban 123 7 flooding``).

Every privileged action is written to ``admin_audit`` before it replies,
so the trail survives even if the confirmation message fails to send.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.admin_repo import (
    AdminRepo,
    AnalyticsRepo,
    AuditRepo,
    BanRepo,
    PromoRepo,
)
from app.db.inventory_repo import InventoryRepo, RoleClaimRepo
from app.db.repo import ShopRepo, StatsRepo
from app.game.constants import WARN_BAN_DAYS, WARN_BAN_THRESHOLD
from app.game.shop import (
    ROLE_CARDS,
    SHOP_ITEMS,
    card_for_role,
    get_item,
)
from app.i18n import Translator, get_i18n
from app.keyboards.inline import (
    admin_confirm_kb,
    admin_flags_kb,
    admin_menu_kb,
)
from app.services import admin as admin_service
from app.services.admin import (
    ALL_FLAG_KEYS,
    FEATURE_FLAGS,
    PENDING_BROADCASTS,
    KEY_COIN_MULTIPLIER,
    KEY_MAINTENANCE,
    is_admin,
    is_group_admin,
    flag_default,
    is_owner,
    resolve_flag_key,
    runtime_config,
)
from app.services.audit_view import (
    AUDIT_PAGE_SIZE,
    AUDIT_PAGE_TOTAL,
    admin_ids_in,
    page_count,
    render_page,
)
from app.services.lobby import LobbyService
from app.services.orchestrator import (
    admin_extend_phase,
    admin_force_end,
    admin_kick_player,
    admin_skip_phase,
)
from app.services.timer import TimerManager

logger = logging.getLogger(__name__)
router = Router(name="admin")


# --- shared helpers ----------------------------------------------------

async def _deny(message: Message, t: Translator) -> None:
    await message.answer(t("admin.denied"))


async def _guard(message: Message, session: AsyncSession, t: Translator) -> bool:
    """Reply with a refusal and return False when the user is not an admin."""
    if await is_admin(session, message.from_user.id):
        return True
    await _deny(message, t)
    return False


async def _guard_owner(message: Message, t: Translator) -> bool:
    if is_owner(message.from_user.id):
        return True
    await message.answer(t("admin.owner_only"))
    return False


async def _audit(
    session: AsyncSession,
    message: Message,
    action: str,
    *,
    target: object = "",
    details: str = "",
) -> None:
    await AuditRepo(session).log(
        message.from_user.id, action, target=str(target), details=details
    )


def _args(command: CommandObject) -> list[str]:
    return (command.args or "").split()


def _target_user(message: Message, args: list[str]) -> Optional[int]:
    """Resolve the target user from a reply or a numeric first argument.

    Replying to a message is the ergonomic path in groups; the numeric id
    always works. For ``@username`` use :func:`_target_user_async`, which
    can also hit the database.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if args:
        try:
            return int(args[0])
        except ValueError:
            return None
    return None


async def _target_user_async(
    message: Message, args: list[str], session: AsyncSession
) -> Optional[int]:
    """Same as :func:`_target_user`, but also resolves ``@username``.

    Telegram gives bots no username lookup API, so this only works for
    players the bot has already seen (their handle is stored on first
    contact in ``user_stats.username``).
    """
    direct = _target_user(message, args)
    if direct is not None:
        return direct
    if args and args[0].startswith("@"):
        row = await StatsRepo(session).find_by_username(args[0])
        if row is not None:
            return int(row.user_id)
    return None


LIST_PAGE_SIZE = 10


def _page_arg(args: list[str]) -> int:
    """``/banlist 3`` -> page index 2 (0-based, never negative)."""
    if not args:
        return 0
    try:
        return max(0, int(args[0]) - 1)
    except ValueError:
        return 0


def _pages(total: int, size: int = LIST_PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def _int_or(value: str, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _notify_user(bot: Bot, user_id: int, text: str) -> None:
    """Best-effort DM to a moderated user (they may have blocked us)."""
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        logger.debug("Could not notify user %s.", user_id)


def _resolve_game(games: LobbyService, args: list[str], message: Message):
    """Pick the game by explicit chat id, or the one in the current chat."""
    if args:
        chat_id = _int_or(args[0])
        if chat_id is not None:
            return games.get(chat_id)
    return games.get(message.chat.id)


# --- entry points ------------------------------------------------------

@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    session: AsyncSession,
    games: LobbyService,
    bot: Bot,
    t: Translator,
) -> None:
    """Open the panel (buttons in DM, short status card in a group)."""
    user_id = message.from_user.id
    admin = await is_admin(session, user_id)

    if message.chat.type == "private":
        if not admin:
            await _deny(message, t)
            return
        maintenance = await runtime_config.is_maintenance(session)
        await message.answer(
            t(
                "admin.panel_header",
                games=len(games._sessions),
                mode=t("admin.mode_maintenance")
                if maintenance
                else t("admin.mode_normal"),
            ),
            reply_markup=admin_menu_kb(t, is_owner=is_owner(user_id)),
        )
        return

    # In a group: bot admins and this chat's Telegram admins may act on
    # the local game only.
    if not admin and not await is_group_admin(bot, message.chat.id, user_id):
        await _deny(message, t)
        return
    game = games.get(message.chat.id)
    if game is None:
        await message.answer(t("admin.no_game_here"))
        return
    await message.answer(
        t(
            "admin.game_card",
            chat=game.chat_id,
            phase=game.phase.value,
            round=game.round_number,
            alive=len(game.alive_players),
            total=len(game.players),
        )
    )


@router.message(Command("adminhelp"))
async def cmd_adminhelp(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Full command reference, split into sections."""
    if not await _guard(message, session, t):
        return
    # Sent as several messages: the full reference exceeds Telegram's
    # 4096-character limit for a single message.
    for key in (
        "admin.help.games",
        "admin.help.moderation",
        "admin.help.economy",
        "admin.help.analytics",
        "admin.help.system",
    ):
        await message.answer(t(key))


# --- games -------------------------------------------------------------

@router.message(Command("games"))
async def cmd_games(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    t: Translator,
) -> None:
    """``/games [page]`` - every live game, 10 per page."""
    if not await _guard(message, session, t):
        return
    sessions = list(games._sessions.values())
    if not sessions:
        await message.answer(t("admin.games_empty"))
        return
    total = len(sessions)
    page = min(_page_arg(_args(command)), _pages(total) - 1)
    window = sessions[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]
    lines = [t("admin.games_header", count=total)]
    for game in window:
        lines.append(
            t(
                "admin.games_line",
                chat=game.chat_id,
                phase=game.phase.value,
                round=game.round_number,
                alive=len(game.alive_players),
                total=len(game.players),
            )
        )
    lines.append(
        t("admin.list_footer", page=page + 1, page_next=page + 2,
          pages=_pages(total),
          total=total, command="/games")
    )
    await message.answer("\n".join(lines))


@router.message(Command("forceend"))
async def cmd_forceend(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Terminate a stuck game in any chat."""
    if not await _guard(message, session, t):
        return
    game = _resolve_game(games, _args(command), message)
    if game is None:
        await message.answer(t("admin.game_not_found"))
        return
    await admin_force_end(
        bot, games, timers, session, game, tracker=tracker
    )
    await _audit(session, message, "game.force_end", target=game.chat_id)
    await message.answer(t("admin.game_ended", chat=game.chat_id))


@router.message(Command("skipphase"))
async def cmd_skipphase(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """End the current phase right now."""
    if not await _guard(message, session, t):
        return
    game = _resolve_game(games, _args(command), message)
    if game is None:
        await message.answer(t("admin.game_not_found"))
        return
    phase = game.phase.value
    ok = await admin_skip_phase(
        bot, games, timers, session, game, tracker=tracker
    )
    if not ok:
        await message.answer(t("admin.phase_not_skippable", phase=phase))
        return
    await _audit(session, message, "game.skip_phase", target=game.chat_id,
                 details=phase)
    await message.answer(t("admin.phase_skipped", phase=phase))


@router.message(Command("extend"))
async def cmd_extend(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Give the current phase a fresh countdown: ``/extend [chat_id] <sec>``.

    ``/extend`` is also a player command that pushes the lobby countdown,
    and this router runs first. Anything that is not an admin call with a
    number is therefore handed over to the lobby handler instead of being
    swallowed with an "access denied".
    """
    args = _args(command)
    seconds = _int_or(args[-1]) if args else None
    if seconds is None or not await is_admin(session, message.from_user.id):
        from app.handlers.lobby import cmd_extend as lobby_extend

        await lobby_extend(message, games, t)
        return
    if seconds <= 0 or seconds > 600:
        await message.answer(t("admin.extend_usage"))
        return
    game = _resolve_game(games, args[:-1], message)
    if game is None:
        await message.answer(t("admin.game_not_found"))
        return
    ok = await admin_extend_phase(
        bot, games, timers, session, game, seconds, tracker=tracker
    )
    if not ok:
        await message.answer(
            t("admin.phase_not_skippable", phase=game.phase.value)
        )
        return
    await _audit(session, message, "game.extend", target=game.chat_id,
                 details=str(seconds))
    await message.answer(t("admin.phase_extended", seconds=seconds))


@router.message(Command("kickplayer"))
async def cmd_kickplayer(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """``/kickplayer <chat_id> <user_id>`` - eliminate a player mid-game."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if len(args) < 2:
        await message.answer(t("admin.kick_usage"))
        return
    chat_id, user_id = _int_or(args[0]), _int_or(args[1])
    if chat_id is None or user_id is None:
        await message.answer(t("admin.kick_usage"))
        return
    game = games.get(chat_id)
    if game is None:
        await message.answer(t("admin.game_not_found"))
        return
    ok = await admin_kick_player(
        bot, games, timers, session, game, user_id, tracker=tracker
    )
    if not ok:
        await message.answer(t("admin.player_not_found"))
        return
    await _audit(session, message, "game.kick_player", target=user_id,
                 details=str(chat_id))
    await message.answer(t("admin.player_kicked", user=user_id))


@router.message(Command("gameinfo"))
async def cmd_gameinfo(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    t: Translator,
) -> None:
    """Reveal the full role layout of a running game.

    Owner-only and always audited: this is hidden information, and being
    able to read it silently would let a moderator cheat.
    """
    if not await _guard_owner(message, t):
        return
    game = _resolve_game(games, _args(command), message)
    if game is None:
        await message.answer(t("admin.game_not_found"))
        return
    lines = [t("admin.roles_header", chat=game.chat_id)]
    for player in game.players.values():
        lines.append(
            t(
                "admin.roles_line",
                name=player.full_name,
                user=player.user_id,
                role=player.role,
                state=t("admin.alive") if player.is_alive else t("admin.dead"),
            )
        )
    await _audit(session, message, "game.reveal_roles", target=game.chat_id)
    await message.answer("\n".join(lines))


# Approximate process start: this module is imported during bootstrap.
_STARTED_AT = time.monotonic()


def _uptime() -> str:
    """Human-readable process uptime for the health screen."""
    seconds = int(time.monotonic() - _STARTED_AT)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"


# --- moderation --------------------------------------------------------

@router.message(Command("ban"))
async def cmd_ban(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
) -> None:
    """``/ban <user_id> [days] [reason]`` - block a user from playing."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    if user_id is None:
        await message.answer(t("admin.ban_usage"))
        return
    # When the target came from a reply the args still start at the days.
    rest = args if message.reply_to_message else args[1:]
    days = _int_or(rest[0]) if rest else None
    reason_parts = rest[1:] if days is not None else rest
    reason = " ".join(reason_parts) or t("admin.no_reason")

    await BanRepo(session).ban_user(
        user_id, reason=reason, banned_by=message.from_user.id, days=days
    )
    await _audit(session, message, "user.ban", target=user_id,
                 details=f"days={days} reason={reason}")
    await _notify_user(
        bot, user_id,
        t("admin.ban_notice",
          duration=t("admin.ban_days", days=days) if days
          else t("admin.ban_forever"),
          reason=reason),
    )
    await message.answer(
        t("admin.ban_done", user=user_id,
          duration=t("admin.ban_days", days=days) if days
          else t("admin.ban_forever"))
    )


@router.message(Command("unban"))
async def cmd_unban(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
) -> None:
    if not await _guard(message, session, t):
        return
    user_id = await _target_user_async(message, _args(command), session)
    if user_id is None:
        await message.answer(t("admin.ban_usage"))
        return
    removed = await BanRepo(session).unban_user(user_id)
    if not removed:
        await message.answer(t("admin.not_banned", user=user_id))
        return
    await _audit(session, message, "user.unban", target=user_id)
    await _notify_user(bot, user_id, t("admin.unban_notice"))
    await message.answer(t("admin.unban_done", user=user_id))


@router.message(Command("banlist"))
async def cmd_banlist(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/banlist [page]`` - 10 bans per page, newest first."""
    if not await _guard(message, session, t):
        return
    bans = BanRepo(session)
    total = await bans.count_bans()
    page = min(_page_arg(_args(command)), _pages(total) - 1)
    rows = await bans.list_bans(
        limit=LIST_PAGE_SIZE, offset=page * LIST_PAGE_SIZE
    )
    if not rows:
        await message.answer(t("admin.banlist_empty"))
        return
    lines = [t("admin.banlist_header")]
    for row in rows:
        lines.append(
            t("admin.banlist_line", user=row.user_id,
              until=row.until.strftime("%Y-%m-%d") if row.until
              else t("admin.ban_forever"),
              reason=row.reason)
        )
    lines.append(
        t("admin.list_footer", page=page + 1, page_next=page + 2,
          pages=_pages(total),
          total=total, command="/banlist")
    )
    await message.answer("\n".join(lines))


@router.message(Command("warn"))
async def cmd_warn(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
) -> None:
    """Warn a user; the third active warning auto-bans for a week."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    if user_id is None:
        await message.answer(t("admin.warn_usage"))
        return
    rest = args if message.reply_to_message else args[1:]
    reason = " ".join(rest) or t("admin.no_reason")

    bans = BanRepo(session)
    count = await bans.warn(user_id, reason=reason, admin_id=message.from_user.id)
    await _audit(session, message, "user.warn", target=user_id, details=reason)
    await _notify_user(
        bot, user_id, t("admin.warn_notice", count=count, reason=reason)
    )

    if count >= WARN_BAN_THRESHOLD:
        await bans.ban_user(
            user_id,
            reason=t("admin.warn_autoban_reason", count=count),
            banned_by=message.from_user.id,
            days=WARN_BAN_DAYS,
        )
        await bans.clear_warnings(user_id)
        await _audit(session, message, "user.autoban", target=user_id,
                     details=f"warnings={count}")
        await _notify_user(
            bot, user_id,
            t("admin.ban_notice",
              duration=t("admin.ban_days", days=WARN_BAN_DAYS),
              reason=t("admin.warn_autoban_reason", count=count)),
        )
        await message.answer(
            t("admin.warn_autoban", user=user_id, days=WARN_BAN_DAYS)
        )
        return
    await message.answer(t("admin.warn_done", user=user_id, count=count))


@router.message(Command("warns"))
async def cmd_warns(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    t: Translator,
) -> None:
    if not await _guard(message, session, t):
        return
    user_id = await _target_user_async(message, _args(command), session)
    if user_id is None:
        await message.answer(t("admin.warn_usage"))
        return
    count = await BanRepo(session).warn_count(user_id)
    await message.answer(t("admin.warns_count", user=user_id, count=count))


@router.message(Command("clearwarns"))
async def cmd_clearwarns(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    t: Translator,
) -> None:
    if not await _guard(message, session, t):
        return
    user_id = await _target_user_async(message, _args(command), session)
    if user_id is None:
        await message.answer(t("admin.warn_usage"))
        return
    removed = await BanRepo(session).clear_warnings(user_id)
    await _audit(session, message, "user.clear_warnings", target=user_id)
    await message.answer(
        t("admin.warns_cleared", user=user_id, count=removed)
    )


@router.message(Command("mute"))
async def cmd_mute(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
) -> None:
    """``/mute <user_id> <minutes>`` - silence someone in THIS group.

    Available to this chat's Telegram admins as well as bot admins: it
    only affects the chat the command was sent in.
    """
    if message.chat.type == "private":
        await message.answer(t("admin.group_only"))
        return
    if not await is_admin(session, message.from_user.id) and not await is_group_admin(
        bot, message.chat.id, message.from_user.id
    ):
        await _deny(message, t)
        return

    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    rest = args if message.reply_to_message else args[1:]
    minutes = _int_or(rest[0], 10) if rest else 10
    if user_id is None or minutes is None or minutes <= 0:
        await message.answer(t("admin.mute_usage"))
        return

    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            user_id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
            use_independent_chat_permissions=True,
        )
    except TelegramAPIError as exc:
        await message.answer(t("admin.mute_failed", error=str(exc)))
        return
    await _audit(session, message, "user.mute", target=user_id,
                 details=f"chat={message.chat.id} minutes={minutes}")
    await message.answer(
        t("admin.mute_done", user=user_id, minutes=minutes)
    )


@router.message(Command("banchat"))
async def cmd_banchat(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    games: LobbyService,
    bot: Bot,
    t: Translator,
) -> None:
    """Blacklist a group: no new games, and the bot leaves it."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    chat_id = _int_or(args[0]) if args else message.chat.id
    if chat_id is None:
        await message.answer(t("admin.banchat_usage"))
        return
    reason = " ".join(args[1:]) or t("admin.no_reason")

    await BanRepo(session).ban_chat(
        chat_id, title="", reason=reason, banned_by=message.from_user.id
    )
    games.remove(chat_id)
    await _audit(session, message, "chat.ban", target=chat_id, details=reason)
    try:
        await bot.leave_chat(chat_id)
    except TelegramAPIError:
        logger.info("Could not leave blacklisted chat %s.", chat_id)
    await message.answer(t("admin.banchat_done", chat=chat_id))


@router.message(Command("unbanchat"))
async def cmd_unbanchat(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    t: Translator,
) -> None:
    if not await _guard(message, session, t):
        return
    args = _args(command)
    chat_id = _int_or(args[0]) if args else message.chat.id
    if chat_id is None:
        await message.answer(t("admin.banchat_usage"))
        return
    removed = await BanRepo(session).unban_chat(chat_id)
    if not removed:
        await message.answer(t("admin.chat_not_banned", chat=chat_id))
        return
    await _audit(session, message, "chat.unban", target=chat_id)
    await message.answer(t("admin.unbanchat_done", chat=chat_id))


# --- economy -----------------------------------------------------------

async def _adjust_coins(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    *,
    sign: int,
) -> None:
    """Shared body of /givecoins and /takecoins."""
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    rest = args if message.reply_to_message else args[1:]
    amount = _int_or(rest[0]) if rest else None
    if user_id is None or amount is None or amount <= 0:
        await message.answer(t("admin.coins_usage"))
        return
    reason = " ".join(rest[1:]) or t("admin.no_reason")

    stats = StatsRepo(session)
    if sign < 0:
        ok = await stats.spend_coins(user_id, amount)
        if not ok:
            balance = await stats.get_coins(user_id)
            await message.answer(
                t("admin.coins_insufficient", user=user_id, balance=balance)
            )
            return
        balance = await stats.get_coins(user_id)
    else:
        balance = await stats.add_coins(user_id, amount)
        if balance == 0:
            # add_coins refuses to create a row for a user who never played.
            await message.answer(t("admin.user_unknown", user=user_id))
            return

    action = "coins.give" if sign > 0 else "coins.take"
    await _audit(session, message, action, target=user_id,
                 details=f"amount={amount} reason={reason}")
    await _notify_user(
        bot, user_id,
        t("admin.coins_notice_add" if sign > 0 else "admin.coins_notice_sub",
          amount=amount, balance=balance, reason=reason),
    )
    await message.answer(
        t("admin.coins_done", user=user_id, amount=amount * sign,
          balance=balance)
    )


@router.message(Command("givecoins"))
async def cmd_givecoins(
    message: Message, command: CommandObject, session: AsyncSession,
    bot: Bot, t: Translator,
) -> None:
    """``/givecoins <user_id> <amount> [reason]``."""
    if not await _guard(message, session, t):
        return
    await _adjust_coins(message, command, session, bot, t, sign=1)


@router.message(Command("takecoins"))
async def cmd_takecoins(
    message: Message, command: CommandObject, session: AsyncSession,
    bot: Bot, t: Translator,
) -> None:
    """``/takecoins <user_id> <amount> [reason]``."""
    if not await _guard(message, session, t):
        return
    await _adjust_coins(message, command, session, bot, t, sign=-1)


@router.message(Command("giveitem"))
async def cmd_giveitem(
    message: Message, command: CommandObject, session: AsyncSession,
    bot: Bot, t: Translator,
) -> None:
    """``/giveitem <user_id> <item_id>`` - grant a cosmetic for free."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    rest = args if message.reply_to_message else args[1:]
    if user_id is None or not rest:
        catalogue = ", ".join(item.item_id for item in SHOP_ITEMS)
        await message.answer(t("admin.giveitem_usage", items=catalogue))
        return
    item = get_item(rest[0])
    if item is None:
        catalogue = ", ".join(i.item_id for i in SHOP_ITEMS)
        await message.answer(t("admin.giveitem_usage", items=catalogue))
        return

    shop = ShopRepo(session)
    if await shop.has_item(user_id, item.item_id):
        await message.answer(t("admin.item_already_owned", user=user_id))
        return
    await shop.grant(user_id, item.item_id, 0)
    await _audit(session, message, "shop.grant", target=user_id,
                 details=item.item_id)
    name = t(f"shop.item.{item.item_id}.name")
    await _notify_user(bot, user_id, t("admin.item_notice", name=name))
    await message.answer(t("admin.item_granted", user=user_id, name=name))


@router.message(Command("giverole"))
async def cmd_giverole(
    message: Message, command: CommandObject, session: AsyncSession,
    bot: Bot, t: Translator,
) -> None:
    """``/giverole <user> <role> [count]`` - hand out a role card.

    The card lands in the player's inventory; they still have to activate
    it themselves before a game, so this never silently rigs a table.
    """
    if not await _guard(message, session, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    rest = args if message.reply_to_message else args[1:]
    roles = ", ".join(item.role for item in ROLE_CARDS if item.role)
    if user_id is None or not rest:
        await message.answer(t("admin.giverole_usage", roles=roles))
        return
    item = card_for_role(rest[0])
    if item is None:
        await message.answer(t("admin.giverole_usage", roles=roles))
        return
    qty = max(1, _int_or(rest[1], 1) or 1) if len(rest) > 1 else 1

    await InventoryRepo(session).add(user_id, item.item_id, qty)
    await _audit(session, message, "role.grant", target=user_id,
                 details=f"{item.item_id} x{qty}")
    name = t(f"shop.item.{item.item_id}.name")
    await _notify_user(bot, user_id, t("admin.role_notice", name=name, qty=qty))
    await message.answer(
        t("admin.role_granted", user=user_id, name=name, qty=qty)
    )


@router.message(Command("takerole"))
async def cmd_takerole(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/takerole <user> <role> [count]`` - take role cards back."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    rest = args if message.reply_to_message else args[1:]
    roles = ", ".join(item.role for item in ROLE_CARDS if item.role)
    if user_id is None or not rest:
        await message.answer(t("admin.giverole_usage", roles=roles))
        return
    item = card_for_role(rest[0])
    if item is None:
        await message.answer(t("admin.giverole_usage", roles=roles))
        return
    qty = max(1, _int_or(rest[1], 1) or 1) if len(rest) > 1 else 1

    name = t(f"shop.item.{item.item_id}.name")
    if not await InventoryRepo(session).take(user_id, item.item_id, qty):
        await message.answer(t("admin.role_missing", user=user_id, name=name))
        return
    await _audit(session, message, "role.revoke", target=user_id,
                 details=f"{item.item_id} x{qty}")
    await message.answer(t("admin.role_taken", user=user_id, name=name, qty=qty))


@router.message(Command("invof"))
async def cmd_invof(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/invof <user>`` - inspect somebody's inventory and active card."""
    if not await _guard(message, session, t):
        return
    user_id = await _target_user_async(message, _args(command), session)
    if user_id is None:
        await message.answer(t("admin.need_user"))
        return

    inventory = await InventoryRepo(session).items(user_id)
    owned = await ShopRepo(session).owned_items(user_id)
    claim = await RoleClaimRepo(session).active(user_id)

    lines = [t("admin.invof_header", user=user_id)]
    cards = [
        (item, inventory.get(item.item_id, 0))
        for item in ROLE_CARDS
        if inventory.get(item.item_id, 0) > 0
    ]
    if cards:
        for item, qty in cards:
            lines.append(
                t("admin.invof_line", emoji=item.emoji,
                  name=t(f"shop.item.{item.item_id}.name"), qty=qty)
            )
    else:
        lines.append(t("admin.invof_empty"))

    if owned:
        names = ", ".join(t(f"shop.item.{item_id}.name") for item_id in owned)
        lines.append(t("admin.invof_cosmetics", items=names))
    if claim is not None:
        lines.append(t("admin.invof_claim", role=t(f"role.{claim.role}.title")))
    else:
        lines.append(t("admin.invof_no_claim"))
    await message.answer("\n".join(lines))


@router.message(Command("multiplier"))
async def cmd_multiplier(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/multiplier <x>`` - global coin payout multiplier (0-10)."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if not args:
        current = await runtime_config.coin_multiplier(session)
        await message.answer(t("admin.multiplier_current", value=current))
        return
    try:
        value = float(args[0].replace(",", "."))
    except ValueError:
        await message.answer(t("admin.multiplier_usage"))
        return
    value = max(0.0, min(value, 10.0))
    await runtime_config.set(
        session, KEY_COIN_MULTIPLIER, str(value),
        admin_id=message.from_user.id,
    )
    await _audit(session, message, "economy.multiplier", details=str(value))
    await message.answer(t("admin.multiplier_set", value=value))


@router.message(Command("economy"))
async def cmd_economy(
    message: Message, session: AsyncSession, t: Translator
) -> None:
    """Coins in circulation, coins spent and the best-selling items."""
    if not await _guard(message, session, t):
        return
    analytics = AnalyticsRepo(session)
    held, spent, purchases = await analytics.economy_summary()
    multiplier = await runtime_config.coin_multiplier(session)
    lines = [
        t("admin.economy_header"),
        t("admin.economy_held", value=held),
        t("admin.economy_spent", value=spent),
        t("admin.economy_purchases", value=purchases),
        t("admin.economy_multiplier", value=multiplier),
    ]
    top = await analytics.top_purchases()
    if top:
        lines.append("")
        lines.append(t("admin.economy_top"))
        for item_id, count in top:
            lines.append(
                t("admin.economy_top_line",
                  name=t(f"shop.item.{item_id}.name"), count=count)
            )
    await message.answer("\n".join(lines))


# --- promo codes -------------------------------------------------------

@router.message(Command("promonew"))
async def cmd_promonew(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/promonew <CODE> <coins> [uses] [days] [item_id]``."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if len(args) < 2:
        await message.answer(t("admin.promo_usage"))
        return
    code = args[0]
    coins = _int_or(args[1])
    if coins is None or coins < 0:
        await message.answer(t("admin.promo_usage"))
        return
    uses = _int_or(args[2], 1) if len(args) > 2 else 1
    days = _int_or(args[3]) if len(args) > 3 else None
    item_id = args[4] if len(args) > 4 else None
    if item_id is not None and get_item(item_id) is None:
        # Also accept a bare role name ("detective") for role-card promos.
        card = card_for_role(item_id)
        if card is None:
            await message.answer(t("admin.promo_bad_item", item=item_id))
            return
        item_id = card.item_id

    row = await PromoRepo(session).create(
        code, coins=coins, item_id=item_id, max_uses=uses or 1, days=days,
        created_by=message.from_user.id,
    )
    if row is None:
        await message.answer(t("admin.promo_exists", code=code.upper()))
        return
    await _audit(session, message, "promo.create", target=row.code,
                 details=f"coins={coins} uses={uses} days={days}")
    await message.answer(
        t("admin.promo_created", code=row.code, coins=coins, uses=uses or 1)
    )


@router.message(Command("promolist"))
async def cmd_promolist(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/promolist [page]`` - 10 promo codes per page."""
    if not await _guard(message, session, t):
        return
    promos = PromoRepo(session)
    total = await promos.count_codes()
    page = min(_page_arg(_args(command)), _pages(total) - 1)
    rows = await promos.list_codes(
        limit=LIST_PAGE_SIZE, offset=page * LIST_PAGE_SIZE
    )
    if not rows:
        await message.answer(t("admin.promo_empty"))
        return
    lines = [t("admin.promo_header")]
    for row in rows:
        lines.append(
            t("admin.promo_line", code=row.code, coins=row.coins,
              used=row.used_count, max=row.max_uses)
        )
    lines.append(
        t("admin.list_footer", page=page + 1, page_next=page + 2,
          pages=_pages(total),
          total=total, command="/promolist")
    )
    await message.answer("\n".join(lines))


@router.message(Command("promodel"))
async def cmd_promodel(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if not args:
        await message.answer(t("admin.promo_usage"))
        return
    removed = await PromoRepo(session).delete(args[0])
    if not removed:
        await message.answer(t("admin.promo_not_found", code=args[0].upper()))
        return
    await _audit(session, message, "promo.delete", target=args[0].upper())
    await message.answer(t("admin.promo_deleted", code=args[0].upper()))


@router.message(Command("promo"))
async def cmd_promo(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """Player-facing: ``/promo <CODE>`` redeems coins and/or an item."""
    args = _args(command)
    if not args:
        await message.answer(t("promo.usage"))
        return
    user_id = message.from_user.id
    promos = PromoRepo(session)
    row, error = await promos.check_redeemable(args[0], user_id)
    if row is None:
        await message.answer(t(error or "promo.invalid"))
        return

    stats = StatsRepo(session)
    # Make sure the wallet row exists before crediting it.
    await stats.upsert_touch(user_id, message.from_user.full_name or str(user_id))
    balance = await stats.add_coins(user_id, row.coins) if row.coins else await stats.get_coins(user_id)

    granted_name = ""
    if row.item_id:
        granted = get_item(row.item_id)
        if granted is not None and granted.is_role_card:
            # Role cards stack in the inventory instead of being "owned".
            await InventoryRepo(session).add(user_id, row.item_id, 1)
        else:
            shop = ShopRepo(session)
            if not await shop.has_item(user_id, row.item_id):
                await shop.grant(user_id, row.item_id, 0)
        granted_name = t(f"shop.item.{row.item_id}.name")

    await promos.redeem(row.code, user_id)
    if granted_name:
        await message.answer(
            t("promo.redeemed_item", coins=row.coins, balance=balance,
              name=granted_name)
        )
    else:
        await message.answer(
            t("promo.redeemed", coins=row.coins, balance=balance)
        )


# --- analytics ---------------------------------------------------------

@router.message(Command("astats"))
async def cmd_astats(
    message: Message, command: CommandObject, session: AsyncSession,
    games: LobbyService, t: Translator,
) -> None:
    """``/astats [days]`` - overall usage numbers (default 30 days)."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    days = _int_or(args[0], 30) if args else 30
    days = max(1, min(days or 30, 365))

    analytics = AnalyticsRepo(session)
    started = await analytics.games_since(days)
    finished = await analytics.finished_since(days)
    lines = [
        t("admin.stats_header", days=days),
        t("admin.stats_games", started=started, finished=finished),
        t("admin.stats_abandoned", value=max(started - finished, 0)),
        t("admin.stats_live", value=len(games._sessions)),
        t("admin.stats_players",
          value=round(await analytics.avg_players_per_game(days), 1)),
        t("admin.stats_rounds",
          value=round(await analytics.avg_rounds(days), 1)),
        t("admin.stats_active", value=await analytics.active_users(days)),
        t("admin.stats_total", value=await analytics.total_users()),
    ]
    breakdown = await analytics.winner_breakdown(days)
    if breakdown:
        lines.append("")
        lines.append(t("admin.stats_winners"))
        for winner, count in breakdown:
            share = count * 100 // max(finished, 1)
            lines.append(
                t("admin.stats_winner_line", side=winner, count=count,
                  share=share)
            )
    await message.answer("\n".join(lines))


@router.message(Command("rolestats"))
async def cmd_rolestats(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/rolestats [days]`` - per-role winrate, i.e. the balance report."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    days = max(1, min(_int_or(args[0], 30) or 30, 365)) if args else 30
    rows = await AnalyticsRepo(session).role_winrates(days)
    if not rows:
        await message.answer(t("admin.stats_empty"))
        return
    lines = [t("admin.rolestats_header", days=days)]
    for role, played, wins in rows:
        lines.append(
            t("admin.rolestats_line", role=role, played=played, wins=wins,
              rate=wins * 100 // max(played, 1))
        )
    lines.append("")
    lines.append(t("admin.rolestats_hint"))
    await message.answer("\n".join(lines))


@router.message(Command("topchats"))
async def cmd_topchats(
    message: Message, session: AsyncSession, t: Translator
) -> None:
    if not await _guard(message, session, t):
        return
    rows = await AnalyticsRepo(session).top_chats()
    if not rows:
        await message.answer(t("admin.stats_empty"))
        return
    lines = [t("admin.topchats_header")]
    for index, (chat_id, count) in enumerate(rows, start=1):
        lines.append(
            t("admin.topchats_line", index=index, chat=chat_id, games=count)
        )
    await message.answer("\n".join(lines))


# --- broadcast ---------------------------------------------------------

@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/broadcast <text>`` - stage a DM blast, then confirm with a button.

    Never sends straight away: a mistyped broadcast cannot be recalled.
    """
    if not await _guard(message, session, t):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer(t("admin.broadcast_usage"))
        return
    audience = await AnalyticsRepo(session).broadcast_audience()
    if not audience:
        await message.answer(t("admin.broadcast_no_audience"))
        return
    PENDING_BROADCASTS[message.from_user.id] = text
    await message.answer(
        t("admin.broadcast_preview", count=len(audience), text=text),
        reply_markup=admin_confirm_kb(t, "broadcast:go"),
    )


# --- system ------------------------------------------------------------

@router.message(Command("maintenance"))
async def cmd_maintenance(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/maintenance on|off`` - block new games while you deploy."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if not args or args[0].lower() not in {"on", "off"}:
        state = await runtime_config.is_maintenance(session)
        await message.answer(
            t("admin.maintenance_state",
              state=t("admin.on") if state else t("admin.off"))
        )
        return
    enabled = args[0].lower() == "on"
    await runtime_config.set_bool(
        session, KEY_MAINTENANCE, enabled, admin_id=message.from_user.id
    )
    await _audit(session, message, "system.maintenance",
                 details="on" if enabled else "off")
    await message.answer(
        t("admin.maintenance_set",
          state=t("admin.on") if enabled else t("admin.off"))
    )


@router.message(Command("flags"))
async def cmd_flags(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/flags`` lists every switch with buttons, ``/flags <key>`` flips one.

    The key may be written in full (``feature.shop``), short (``shop``) or
    as a common alias (``cards``): admins should not have to memorise the
    internal namespace.
    """
    if not await _guard(message, session, t):
        return
    args = _args(command)
    if args:
        key = resolve_flag_key(args[0])
        if key is None:
            await message.answer(
                t("admin.flag_unknown", keys=", ".join(ALL_FLAG_KEYS))
            )
            return
        value = await runtime_config.toggle(
            session, key, default=flag_default(key),
            admin_id=message.from_user.id,
        )
        action = (
            "system.maintenance" if key == KEY_MAINTENANCE else "system.flag"
        )
        await _audit(session, message, action, target=key,
                     details="on" if value else "off")
        await message.answer(
            t("admin.flag_set", key=t(f"admin.flag.{key}"),
              state=t("admin.on") if value else t("admin.off"))
        )
        return

    # No argument: show the same button board as the panel, so the switches
    # can simply be tapped instead of retyped.
    flags = {KEY_MAINTENANCE: await runtime_config.is_maintenance(session)}
    for key, default in FEATURE_FLAGS.items():
        flags[key] = await runtime_config.get_bool(session, key, default)
    lines = [t("admin.flags_header")]
    for key, enabled in flags.items():
        lines.append(
            t("admin.flags_line", key=key,
              state=t("admin.on") if enabled else t("admin.off"))
        )
    await message.answer(
        "\n".join(lines), reply_markup=admin_flags_kb(flags, t)
    )


@router.message(Command("reloadlocales"))
async def cmd_reloadlocales(
    message: Message, session: AsyncSession, t: Translator
) -> None:
    """Re-read the JSON locale files without restarting the bot."""
    if not await _guard(message, session, t):
        return
    get_i18n.cache_clear()
    manager = get_i18n()
    await _audit(session, message, "system.reload_locales")
    await message.answer(
        t("admin.locales_reloaded",
          langs=", ".join(sorted(manager.available_languages)))
    )


@router.message(Command("health"))
async def cmd_health(
    message: Message, session: AsyncSession, games: LobbyService,
    timers: TimerManager, t: Translator,
) -> None:
    """Live process snapshot: uptime, games, timers, mode."""
    if not await _guard(message, session, t):
        return
    maintenance = await runtime_config.is_maintenance(session)
    await message.answer(
        t("admin.health",
          uptime=_uptime(),
          games=len(games._sessions),
          timers=len(getattr(timers, "_tasks", {}) or {}),
          mode=t("admin.mode_maintenance") if maintenance
          else t("admin.mode_normal"))
    )


# --- admin roster (owner only) ----------------------------------------

@router.message(Command("addadmin"))
async def cmd_addadmin(
    message: Message, command: CommandObject, session: AsyncSession,
    bot: Bot, t: Translator,
) -> None:
    """``/addadmin <user_id>`` - grant moderator rights at runtime."""
    if not await _guard_owner(message, t):
        return
    args = _args(command)
    user_id = await _target_user_async(message, args, session)
    if user_id is None:
        await message.answer(t("admin.addadmin_usage"))
        return
    name = ""
    if message.reply_to_message and message.reply_to_message.from_user:
        name = message.reply_to_message.from_user.full_name
    granted = await AdminRepo(session).grant(
        user_id, name, message.from_user.id
    )
    if not granted:
        await message.answer(t("admin.already_admin", user=user_id))
        return
    await _audit(session, message, "admin.grant", target=user_id)
    await _notify_user(bot, user_id, t("admin.granted_notice"))
    await message.answer(t("admin.admin_added", user=user_id))


@router.message(Command("deladmin"))
async def cmd_deladmin(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    if not await _guard_owner(message, t):
        return
    user_id = await _target_user_async(message, _args(command), session)
    if user_id is None:
        await message.answer(t("admin.addadmin_usage"))
        return
    if is_owner(user_id):
        # Owners come from ADMIN_IDS; only an env change can remove them.
        await message.answer(t("admin.cannot_revoke_owner"))
        return
    revoked = await AdminRepo(session).revoke(user_id)
    if not revoked:
        await message.answer(t("admin.not_admin", user=user_id))
        return
    await _audit(session, message, "admin.revoke", target=user_id)
    await message.answer(t("admin.admin_removed", user=user_id))


@router.message(Command("admins"))
async def cmd_admins(
    message: Message, session: AsyncSession, t: Translator
) -> None:
    if not await _guard_owner(message, t):
        return
    lines = [t("admin.admins_header")]
    for owner_id in sorted(admin_service.settings.admin_ids):
        lines.append(t("admin.admins_owner_line", user=owner_id))
    for row in await AdminRepo(session).list_admins():
        lines.append(
            t("admin.admins_line", user=row.user_id,
              name=row.full_name or "-")
        )
    await message.answer("\n".join(lines))


@router.message(Command("audit"))
async def cmd_audit(
    message: Message, command: CommandObject, session: AsyncSession,
    t: Translator,
) -> None:
    """``/audit [limit]`` - the last privileged actions, newest first."""
    if not await _guard(message, session, t):
        return
    args = _args(command)
    limit = (
        max(1, min(_int_or(args[0], AUDIT_PAGE_TOTAL) or AUDIT_PAGE_TOTAL,
                   AUDIT_PAGE_TOTAL))
        if args else AUDIT_PAGE_TOTAL
    )
    audits = AuditRepo(session)
    rows = await audits.page(limit=limit)
    if not rows:
        await message.answer(t("admin.audit_empty"))
        return

    # The journal is long, so it is sent as several readable chunks
    # instead of one wall of text.
    stats = StatsRepo(session)
    names: dict[int, str] = {}
    for admin_id in admin_ids_in(rows):
        row = await stats.get(admin_id)
        if row is not None and getattr(row, "full_name", ""):
            names[admin_id] = row.full_name

    total = len(rows)
    for page in range(page_count(total)):
        chunk = rows[page * AUDIT_PAGE_SIZE:(page + 1) * AUDIT_PAGE_SIZE]
        if not chunk:
            break
        await message.answer(
            render_page(chunk, t, page=page, total=total, names=names)
        )
