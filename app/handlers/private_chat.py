"""Private-chat relay: mafia night chat + a lynched player's last word.

Ordinary (non-command) text sent to the bot in a private chat is routed
here. Two features use it:

1. **Last word** — while a lynched player has the floor
   (``GamePhase.DAY_LAST_WORD``), their message is relayed to the group
   and the game then continues.
2. **Mafia night chat** — during the night, mafia-side players can talk
   to each other privately through the bot; each message is fanned out to
   their living teammates' DMs.

This router is registered LAST so it never shadows commands or the
inline-button callbacks handled by the other routers.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.game.enums import KILLER_MAFIA_ROLES, GamePhase
from app.i18n import Translator
from app.services.admin import KEY_FEATURE_DEAD_CHAT, runtime_config
from app.services.lobby import LobbyService
from app.services.orchestrator import handle_last_word
from app.services.session import GameSession
from app.services.timer import TimerManager

logger = logging.getLogger(__name__)
router = Router(name="private_chat")


def _find_session_for_user(
    games: LobbyService, user_id: int
) -> GameSession | None:
    """Locate the live game in which ``user_id`` is a player."""
    for session in games._sessions.values():  # noqa: SLF001 — registry access
        if user_id in session.players:
            return session
    return None


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def relay_private_text(
    message: Message,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Route free-form private text to the last-word or mafia-chat feature."""
    user_id = message.from_user.id
    game = _find_session_for_user(games, user_id)
    if game is None:
        return

    text = (message.text or "").strip()
    if not text:
        return

    # 1) Last word of a lynched player takes priority.
    if (
        game.phase is GamePhase.DAY_LAST_WORD
        and game.awaiting_last_word_from == user_id
    ):
        await handle_last_word(bot, games, timers, session, game, text, tracker=tracker)
        return

    speaker = game.get(user_id)
    if speaker is None:
        return

    # 2) Dead chat — eliminated players keep talking among themselves.
    if not speaker.is_alive:
        # Two switches guard the ghost chat: the per-lobby setting and the
        # global feature flag an admin can flip from /admin.
        if not game.settings.dead_chat:
            return
        if not await runtime_config.feature_enabled(
            session, KEY_FEATURE_DEAD_CHAT
        ):
            await message.answer(t("dead.chat_disabled"))
            return
        others = [
            p for p in game.dead_players if p.user_id != user_id
        ]
        if not others:
            await message.answer(t("dead.chat_nobody"))
            return
        payload = t("dead.chat_relay", name=speaker.full_name, text=text)
        for ghost in others:
            try:
                await bot.send_message(ghost.user_id, payload)
            except TelegramAPIError:
                logger.warning(
                    "Could not relay dead chat to %s.", ghost.user_id
                )
        return

    # 3) Mafia night chat — relay to living kill-voting teammates.
    # The lawyer is mafia-aligned but blind: he neither takes part in the
    # family chat nor receives it, otherwise his identity would leak.
    if game.phase is GamePhase.NIGHT:
        if speaker.role not in KILLER_MAFIA_ROLES:
            return
        payload = t("night.mafia_chat", name=speaker.full_name, text=text)
        for teammate in game.alive_mafia_killers():
            if teammate.user_id == user_id:
                continue
            try:
                await bot.send_message(teammate.user_id, payload)
            except TelegramAPIError:
                logger.warning(
                    "Could not relay mafia chat to %s.", teammate.user_id
                )
