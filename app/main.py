"""Bot bootstrap: dispatcher, middleware, polling loop.

Middleware ordering matters:

1. ``ServicesMiddleware`` (outer) — injects shared singletons
   (``games``, ``timers``).
2. ``DbSessionMiddleware`` — opens a fresh DB session per update and
   exposes it as ``data["session"]``.
3. ``I18nMiddleware`` — reads the user's language from the DB and
   exposes ``data["i18n"]``, ``data["user_lang"]``, ``data["t"]``.

On Render (and other PaaS that require binding to a port), a small
HTTP health server runs alongside the polling loop so the platform's
health check passes and the free-tier service is not put to sleep by
external cron pings.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.exc import SQLAlchemyError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    ErrorEvent,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from app.config import settings
from app.db.admin_repo import AuditRepo
from app.db.session import dispose_db, get_session_factory, init_db
from app.handlers.router import build_root_router
from app.i18n import get_i18n
from app.i18n.middleware import I18nMiddleware
from app.middlewares.db import DbSessionMiddleware
from app.middlewares.services import ServicesMiddleware
from app.services.cleanup import MessageTracker
from app.services.lobby import LobbyService
from app.services.timer import TimerManager
from app.web import start_health_server

logger = logging.getLogger(__name__)

# Command menus shown when a user types "/". Three scopes:
#   * groups - only what makes sense at the table;
#   * private chats - the full player toolbox;
#   * each admin's private chat - the same plus the service commands,
#     so ordinary players never even see them.
GROUP_COMMANDS: tuple[tuple[str, str], ...] = (
    # Telegram matches menu entries literally, so an alias that is not
    # listed here simply does not exist as far as the "/" hint is
    # concerned - /newgame has to be spelled out even though /game works.
    ("newgame", "🎲 Собрать игру"),
    ("game", "🎲 Собрать игру (короткая форма)"),
    ("join", "➕ Присоединиться"),
    ("leave", "➖ Выйти из лобби"),
    ("startgame", "▶️ Начать игру"),
    ("extend", "⏱️ Продлить сбор"),
    ("shorten", "⏩ Сократить сбор"),
    ("settings", "⚙️ Настройки лобби"),
    ("alive", "🧍 Живые игроки"),
    ("top", "🏆 Таблица лидеров"),
    ("cancel", "🛑 Отменить игру"),
    ("help", "❓ Помощь"),
)

PRIVATE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "👋 Запустить бота"),
    ("help", "❓ Все команды"),
    ("me", "👤 Мой профиль"),
    ("profile", "👤 Мой профиль (алиас)"),
    ("stats", "📊 Моя статистика"),
    ("top", "🏆 Таблица лидеров"),
    ("shop", "🛒 Магазин"),
    ("inventory", "🎒 Инвентарь"),
    ("balance", "🪙 Баланс монет"),
    ("promo", "🎟️ Промокод"),
    ("role", "🎭 Моя роль"),
    ("lang", "🌐 Язык"),
)

ADMIN_COMMANDS: tuple[tuple[str, str], ...] = PRIVATE_COMMANDS + (
    ("admin", "🛡️ Панель админа"),
    ("adminhelp", "📖 Справка админа"),
    ("games", "🎲 Активные игры"),
    ("gameinfo", "🔍 Подробно об игре"),
    ("forceend", "🛑 Завершить игру"),
    ("skipphase", "⏭️ Пропустить фазу"),
    ("kickplayer", "👟 Выгнать игрока"),
    ("ban", "🚫 Забанить"),
    ("unban", "♻️ Разбанить"),
    ("warn", "⚠️ Вынести предупреждение"),
    ("mute", "🔇 Замутить"),
    ("givecoins", "💰 Начислить монеты"),
    ("multiplier", "✖️ Множитель монет"),
    ("promonew", "🎟️ Создать промокод"),
    ("rolestats", "🎭 Статистика ролей"),
    ("topchats", "💬 Активные чаты"),
    ("admins", "👑 Список админов"),
    ("reloadlocales", "♻️ Перезагрузить тексты"),
    ("audit", "📜 Журнал действий"),
    ("astats", "📈 Аналитика"),
    ("maintenance", "🚧 Тех-работы"),
    ("flags", "🎛️ Переключатели функций"),
    ("health", "❤️ Состояние бота"),
    ("banlist", "🚫 Список банов"),
    ("promolist", "🎫 Промокоды"),
    ("economy", "🪙 Экономика"),
    ("broadcast", "📢 Рассылка"),
)


def _commands(pairs: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in pairs]


async def setup_commands(bot: Bot) -> None:
    """Publish the "/" menus. Never fatal: a failure only costs hints."""
    try:
        await bot.set_my_commands(
            _commands(GROUP_COMMANDS), scope=BotCommandScopeAllGroupChats()
        )
        await bot.set_my_commands(
            _commands(PRIVATE_COMMANDS),
            scope=BotCommandScopeAllPrivateChats(),
        )
    except TelegramAPIError:
        logger.warning("Could not publish the command menu.", exc_info=True)

    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                _commands(ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except TelegramAPIError:
            # Usually just means the admin never opened the bot in DM.
            logger.debug("No command menu for admin %s.", admin_id)


async def _record_error(event: ErrorEvent, query, message) -> None:
    """Append the crash to the audit trail so ``/audit`` really shows it.

    The failing handler's own session was rolled back, so a fresh one is
    opened here. Telling the user "this was written down" is only worth
    doing if an admin can actually read it back afterwards.
    """
    source = query or message
    user = getattr(source, "from_user", None)
    text = getattr(message, "text", None) or getattr(query, "data", "") or ""
    exception = event.exception
    details = f"{type(exception).__name__}: {exception}"
    try:
        async with get_session_factory()() as session:
            await AuditRepo(session).log(
                getattr(user, "id", 0) or 0,
                "system.error",
                target=text[:64],
                details=details[:256],
            )
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Could not write the error to the audit trail.")


async def on_error(event: ErrorEvent) -> None:
    """Last-resort handler: log the crash and tell the user about it.

    Without this an exception inside a handler is logged deep in aiogram
    and the user simply sees nothing happen, which is indistinguishable
    from a missing command. A visible reply makes such bugs reportable.
    """
    logger.exception(
        "Unhandled error while processing an update",
        exc_info=event.exception,
    )
    update = event.update
    query = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)
    await _record_error(event, query, message)
    i18n = get_i18n()
    notice = i18n.translate(i18n.default_lang, "errors.unexpected")
    try:
        if query is not None:
            await query.answer(notice, show_alert=True)
        elif message is not None:
            await message.answer(notice)
    except TelegramAPIError:
        logger.debug("Could not deliver the error notice.")


async def main() -> None:
    """Wire everything up and start long-polling + health server."""
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Drop pending updates so the bot doesn't replay old messages on restart.
    await bot.delete_webhook(drop_pending_updates=True)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Shared process-wide services.
    games = LobbyService()
    timers = TimerManager()
    # Sliding-window cleanup for group messages. The deleter is bound to
    # this bot instance and never raises (best-effort).
    async def _delete_message(chat_id: int, message_id: int) -> None:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass

    tracker = MessageTracker(deleter=_delete_message)
    # Let the lobby service drive the lobby card refresh / teardown timers.
    games.bind(timers, tracker)

    # Outer: shared services (no DB needed).
    dp.update.outer_middleware(ServicesMiddleware(games, timers, tracker))
    # Inner: DB session, then i18n (needs DB to look up user language).
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(I18nMiddleware(get_i18n()))

    dp.include_router(build_root_router())
    dp.errors.register(on_error)

    await init_db()

    # Publish the "/" hints (general, private and per-admin scopes).
    await setup_commands(bot)

    # Start the HTTP health-check server (required by Render Web Service).
    web_runner = await start_health_server(port=settings.web_port)

    logger.info("Starting bot polling…")
    try:
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        timers.cancel_all()
        await web_runner.cleanup()
        await dispose_db()
        await bot.session.close()
