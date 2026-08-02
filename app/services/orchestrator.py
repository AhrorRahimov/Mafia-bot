"""Game orchestrator: high-level transitions between phases.

This module glues services (lobby, night, day, timer) together and
talks to Telegram on behalf of handlers. Handlers stay thin: they
delegate phase transitions here so the flow logic lives in one place.

Phase loop::

    start_game  -> start_night
    night done  -> end_night  -> start_discussion
    discussion  -> start_vote
    vote done   -> end_vote   -> check_winner -> start_night OR end_game

Language resolution:
  * Group announcements use the **creator's** language.
  * Private messages (role prompts, vote, detective result) use the
    **recipient's** own language.

Robustness:
  * All group sends go through ``_safe_group_send``, which catches
    ``TelegramAPIError`` (the parent class) so a kicked bot or a chat
    that disappeared can never strand the game in a half-advanced phase
    — the historical cause of "the bot freezes after night".
  * Night/day end is wrapped in ``try/finally`` so ``_unmute_all`` runs
    even if a downstream send raises; nobody is left muted forever.
  * All DMs catch ``TelegramAPIError`` too so a user who never started
    the bot in private chat no longer aborts the whole night.
  * Timer callbacks open a **fresh** DB session; they cannot reuse the
    request-scoped session that was closed long before the timer fires.
  * Transient group announcements are deleted at the next transition
    and a sliding window keeps only the most recent bot messages, so
    the group chat is not flooded.

A ``MessageTracker`` is now threaded through every transition; if none
is supplied (legacy callers), behaviour degrades to "track nothing",
which is the old pre-cleanup behaviour.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import GameRepo, PlayerRepo, StatsRepo
from app.db.session import get_session_factory
from app.game.constants import (
    COINS_PER_GAME,
    COINS_PER_WIN,
    COINS_SURVIVOR_BONUS,
    PHASE_REMINDER_SECONDS,
)
from app.game.enums import (
    MAFIA_SIDE_ROLES,
    THIRD_PARTY_ROLES,
    GamePhase,
    Role,
    Winner,
)
from app.i18n import Translator, get_i18n
from app.keyboards.inline import (
    detective_shoot_kb,
    detective_targets_kb,
    doctor_targets_kb,
    don_search_kb,
    lawyer_targets_kb,
    maniac_targets_kb,
    mafia_targets_kb,
    nominate_kb,
    rematch_kb,
    vote_kb,
    whore_targets_kb,
)
from app.game.achievements import PlayerOutcome
from app.services.progress import apply_game_result, rollover_if_needed
from app.services.admin import (
    KEY_FEATURE_DETECTIVE_SHOOT,
    runtime_config,
)
from app.services.cleanup import MessageTracker
from app.services.day import DayService
from app.services.lobby import LobbyService
from app.services.night import NightService
from app.services.session import GameSession
from app.services.timer import TimerManager
from app.texts import (
    detective_result,
    don_search_result,
    game_over,
    night_killed,
    role_reveal_header,
    role_reveal_line,
    sergeant_report,
    vote_result_lynch,
    vote_result_no_lynch,
    vote_result_skipped,
)

logger = logging.getLogger(__name__)

# Labels for ``GameSession.phase_message_ids`` — each is removed when the
# phase that produced it ends, keeping the group tidy.
MSG_NIGHT = "night"            # the "city falls asleep" line at the start of night
MSG_MORNING = "morning"        # the "morning / X died / nobody died" dawn notice
MSG_DISCUSSION = "discussion"  # the "discussion" line, removed when voting starts
MSG_VOTE = "vote"              # the "voting started" line, removed when voting ends
MSG_NOMINATION = "nomination"  # the "nomination started" line, removed when voting starts
MSG_REMINDER = "reminder"      # the most recent "time left" reminder (single slot)


# --- Fresh DB session helper ------------------------------------------

async def _fresh_session() -> AsyncSession:
    """Return a brand-new async DB session (for timer callbacks)."""
    factory = get_session_factory()
    return factory()


# --- Language helpers --------------------------------------------------

async def _user_t(session: AsyncSession, user_id: int) -> Translator:
    """Translator bound to the recipient's language (for private chat)."""
    lang = await StatsRepo(session).get_language(user_id)
    return get_i18n().translator_for(lang)


async def _group_t(session: AsyncSession, game: GameSession) -> Translator:
    """Translator bound to the game creator's language (for group chat)."""
    return await _user_t(session, game.creator_id)


# --- Safe send / delete / tracking ------------------------------------

async def _safe_group_send(
    bot: Bot,
    game: GameSession,
    text: str,
    *,
    tracker: Optional[MessageTracker] = None,
    reply_markup=None,
) -> Optional[int]:
    """Send ``text`` to the game group, never raising.

    Returns the resulting ``message_id`` (or ``None`` on failure / when
    Telegram gives no id). On success the message is registered with the
    sliding-window ``tracker`` so older overflow is auto-deleted.

    This wrapper is THE fix for the "bot freezes after night" bug: every
    group announcement goes through it, so a transient Telegram error
    can never strand the game in a half-advanced phase.
    """
    try:
        msg: Message = await bot.send_message(
            game.chat_id, text, reply_markup=reply_markup
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Group send failed in chat %s: %s", game.chat_id, exc
        )
        return None
    msg_id = getattr(msg, "message_id", None)
    if msg_id is not None and tracker is not None:
        tracker.register(game.chat_id, msg_id)
    return msg_id


async def _safe_delete(bot: Bot, chat_id: int, message_id: Optional[int]) -> None:
    """Best-effort delete; never raises."""
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def _drop_phase_message(
    bot: Bot, game: GameSession, label: str
) -> None:
    """Delete and forget the stored phase message for ``label``."""
    msg_id = game.phase_message_ids.pop(label, None)
    await _safe_delete(bot, game.chat_id, msg_id)


# --- Countdown reminders ----------------------------------------------

def _phase_reminders(
    bot: Bot, game: GameSession, t_group: Translator, duration: float,
    tracker: Optional[MessageTracker] = None,
):
    """Build the reminder list for a phase timer.

    Sends a single "time left" nudge to the group ``PHASE_REMINDER_SECONDS``
    before the phase ends, but only when the phase is long enough for the
    reminder to be meaningful. The previous reminder (if still alive) is
    deleted first so only the latest one clutters the chat.
    """
    reminders: list[tuple[float, object]] = []
    if duration > PHASE_REMINDER_SECONDS + 5:
        async def _remind() -> None:
            # Remove any earlier reminder before posting the fresh one.
            await _drop_phase_message(bot, game, MSG_REMINDER)
            msg_id = await _safe_group_send(
                bot, game,
                t_group("phase.time_left", seconds=PHASE_REMINDER_SECONDS),
                tracker=tracker,
            )
            if msg_id is not None:
                game.phase_message_ids[MSG_REMINDER] = msg_id
        reminders.append((float(PHASE_REMINDER_SECONDS), _remind))
    return reminders


# --- Mute helpers ------------------------------------------------------

async def _mute_all(bot: Bot, game: GameSession, t_group: Translator) -> None:
    """Mute every alive player in the group for the night.

    Best-effort: if the bot is not an admin (or lacks the restrict
    privilege), we surface a warning once and disable muting for the
    rest of this game.
    """
    permissions = ChatPermissions(can_send_messages=False)
    for player in game.alive_players:
        try:
            await bot.restrict_chat_member(
                game.chat_id, player.user_id, permissions,
                use_independent_chat_permissions=True,
            )
            game.muted_user_ids.add(player.user_id)
        except TelegramAPIError as exc:
            logger.warning(
                "Cannot mute user %s in chat %s (%s). Disabling mute for game.",
                player.user_id, game.chat_id, exc,
            )
            try:
                await bot.send_message(
                    game.chat_id, t_group("night.mute_failed_admin")
                )
            except TelegramAPIError:
                pass
            game.mute_enabled = bool(game.muted_user_ids)
            return
    game.mute_enabled = bool(game.muted_user_ids)
    if game.muted_user_ids:
        logger.info("Muted %d players for the night in chat %s.",
                    len(game.muted_user_ids), game.chat_id)


async def _mute_dead(bot: Bot, game: GameSession, victims) -> None:
    """Silence eliminated players in the group for the rest of the game.

    Dead players must not be able to talk to the living, so they are moved
    from the per-night ``muted_user_ids`` set to ``permanently_muted``,
    which ``_unmute_all`` deliberately skips. They are released only when
    the game ends (or is cancelled).
    """
    permissions = ChatPermissions(can_send_messages=False)
    for player in victims:
        try:
            await bot.restrict_chat_member(
                game.chat_id, player.user_id, permissions,
                use_independent_chat_permissions=True,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Cannot mute eliminated player %s in chat %s: %s",
                player.user_id, game.chat_id, exc,
            )
            continue
        game.permanently_muted.add(player.user_id)
        game.muted_user_ids.discard(player.user_id)


async def _unmute_all(
    bot: Bot, game: GameSession, *, include_dead: bool = False
) -> None:
    """Restore messaging permissions at the end of a night.

    Eliminated players (``permanently_muted``) are skipped: they stay
    silenced until the game is over. Pass ``include_dead=True`` at
    game end / cancellation to release everyone.
    """
    targets = set(game.muted_user_ids)
    if include_dead:
        targets |= set(game.permanently_muted)
    else:
        targets -= set(game.permanently_muted)
    if not targets:
        game.mute_enabled = bool(game.permanently_muted)
        return
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    for user_id in sorted(targets):
        try:
            await bot.restrict_chat_member(
                game.chat_id, user_id, permissions,
                use_independent_chat_permissions=True,
            )
        except TelegramAPIError as exc:
            logger.warning("Cannot unmute user %s: %s", user_id, exc)
        finally:
            game.muted_user_ids.discard(user_id)
            if include_dead:
                game.permanently_muted.discard(user_id)
    game.mute_enabled = bool(game.permanently_muted)


# ---------------------------------------------------------------------------
# Night
# ---------------------------------------------------------------------------

async def start_night(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Open the night phase: mute chat, announce, DM prompts, schedule timer."""
    game.begin_night()
    t_group = await _group_t(session, game)

    await _mute_all(bot, game, t_group)

    # The previous day's "morning" notice has now lived a full day; drop it.
    await _drop_phase_message(bot, game, MSG_MORNING)

    msg_id = await _safe_group_send(
        bot, game, t_group("night.started"), tracker=tracker
    )
    if msg_id is not None:
        game.phase_message_ids[MSG_NIGHT] = msg_id

    # DM each role its targets in the recipient's language.
    await _prompt_mafia(bot, session, game)
    await _prompt_detective(bot, session, game)
    await _prompt_detective_shoot(bot, session, game)
    await _prompt_doctor(bot, session, game)
    await _prompt_whore(bot, session, game)
    await _prompt_maniac(bot, session, game)
    await _prompt_lawyer(bot, session, game)
    await _prompt_don_search(bot, session, game)

    duration = game.settings.night_duration
    timers.schedule(
        game.game_id,
        duration,
        _on_night_timeout(bot, games, timers, game, tracker),
        reminders=_phase_reminders(bot, game, t_group, duration, tracker),
    )


def _on_night_timeout(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    game: GameSession,
    tracker: Optional[MessageTracker] = None,
):
    """Build a timer callback that opens its own fresh DB session."""
    async def _cb() -> None:
        session = await _fresh_session()
        try:
            await end_night(bot, games, timers, session, game, tracker=tracker)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
    return _cb


async def end_night(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Resolve the night, announce death, then move to discussion.

    ``_unmute_all`` is guaranteed in ``finally`` so a failed send can
    never leave players muted forever (the historical "stuck after night"
    failure mode).
    """
    if games.get(game.chat_id) is not game:
        return
    # Guard against double resolution: the phase timer and the early-close
    # handler (when everyone has acted) can both call end_night. The first
    # one flips the phase; the second returns immediately. Safe because
    # asyncio runs this synchronously up to the next await.
    if game.phase is not GamePhase.NIGHT:
        return
    game.phase = GamePhase.DAY_ANNOUNCE

    timers.cancel(game.game_id)
    # The night's "time left" nudge is stale now that the phase is over.
    await _drop_phase_message(bot, game, MSG_REMINDER)
    try:
        outcome = NightService(game).resolve()
        t_group = await _group_t(session, game)

        # Reveal detective result privately in the detective's language.
        if outcome.detective_suspect is not None and outcome.detective_is_mafia is not None:
            detective = next(
                (p for p in game.alive_players if p.role is Role.DETECTIVE), None
            )
            if detective is not None:
                t_det = await _user_t(session, detective.user_id)
                try:
                    await bot.send_message(
                        detective.user_id,
                        detective_result(
                            t_det,
                            outcome.detective_suspect.full_name,
                            outcome.detective_is_mafia,
                        ),
                    )
                except TelegramAPIError:
                    logger.warning("Could not DM detective result.")

        # The sergeant shadows every check the detective makes.
        if (
            outcome.detective_suspect is not None
            and outcome.detective_is_mafia is not None
        ):
            for aide in game.alive_of(Role.SERGEANT):
                t_sgt = await _user_t(session, aide.user_id)
                try:
                    await bot.send_message(
                        aide.user_id,
                        sergeant_report(
                            t_sgt,
                            outcome.detective_suspect.full_name,
                            outcome.detective_is_mafia,
                        ),
                    )
                except TelegramAPIError:
                    logger.warning("Could not DM sergeant report.")

        # Tell the don whether his search found the detective.
        if outcome.don_check is not None:
            searched, found = outcome.don_check
            for don in game.alive_of(Role.DON):
                t_don = await _user_t(session, don.user_id)
                try:
                    await bot.send_message(
                        don.user_id,
                        don_search_result(t_don, searched.full_name, found),
                    )
                except TelegramAPIError:
                    logger.warning("Could not DM don search result.")

        # Update AFK counters while this night's actions are still known.
        await _handle_afk(bot, session, game)

        # Announce the victims (or lack thereof) in the group.
        if outcome.deaths:
            victim_names = []
            for victim in outcome.deaths:
                killed_row = await _kill_player(
                    session, game.game_id, victim.user_id
                )
                victim.is_alive = False
                victim_names.append(
                    killed_row.full_name if killed_row else victim.full_name
                )
            # The dead stay silent in the group for the rest of the game.
            await _mute_dead(bot, game, outcome.deaths)
            morning_id = await _safe_group_send(
                bot, game,
                night_killed(t_group, ", ".join(victim_names)),
                tracker=tracker,
            )
        else:
            morning_id = await _safe_group_send(
                bot, game, t_group("night.nobody_died"), tracker=tracker
            )
        if morning_id is not None:
            game.phase_message_ids[MSG_MORNING] = morning_id

        # Role succession (new don / new detective) after the deaths.
        await _notify_promotions(bot, session, game)

        winner = game.evaluate_winner()
        if winner is not None:
            await end_game(bot, games, timers, session, game, winner, tracker=tracker)
            return

        await start_discussion(bot, games, timers, session, game, tracker=tracker)
    finally:
        # Always unmute — daytime (or game-over's own unmute) must be able
        # to chat again even if a send above raised.
        await _unmute_all(bot, game)


# ---------------------------------------------------------------------------
# Day discussion + vote
# ---------------------------------------------------------------------------

async def start_discussion(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    game.phase = GamePhase.DAY_DISCUSSION
    t_group = await _group_t(session, game)
    # The "night started" line has served its purpose; remove it now.
    await _drop_phase_message(bot, game, MSG_NIGHT)
    msg_id = await _safe_group_send(
        bot, game, t_group("day.discussion"), tracker=tracker
    )
    if msg_id is not None:
        game.phase_message_ids[MSG_DISCUSSION] = msg_id
    duration = game.settings.discussion_duration
    timers.schedule(
        game.game_id,
        duration,
        _on_discussion_timeout(bot, games, timers, session, game, tracker),
        reminders=_phase_reminders(bot, game, t_group, duration, tracker),
    )


def _on_discussion_timeout(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    tracker: Optional[MessageTracker] = None,
):
    """Discussion->vote transition. Always opens a fresh session: the
    discussion timer fires ~60s after start_discussion, by which time the
    request session that scheduled it is long closed."""
    async def _cb() -> None:
        fresh = await _fresh_session()
        try:
            await start_nomination_or_vote(
                bot, games, timers, fresh, game, tracker=tracker
            )
            await fresh.commit()
        except Exception:
            await fresh.rollback()
            raise
        finally:
            await fresh.close()
    return _cb


async def start_vote(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    if games.get(game.chat_id) is not game:
        return
    if game.phase not in (GamePhase.DAY_DISCUSSION, GamePhase.DAY_NOMINATION):
        return
    timers.cancel(game.game_id)
    # Snapshot the candidates BEFORE begin_vote() clears the ballots.
    day_service = DayService(game)
    restricted = bool(game.settings.nomination_mode and game.day.nominations)
    candidates = day_service.candidates() if restricted else None
    game.begin_vote()
    await _drop_phase_message(bot, game, MSG_NOMINATION)

    # Discussion is over — remove its banner before posting the vote line.
    await _drop_phase_message(bot, game, MSG_DISCUSSION)
    await _drop_phase_message(bot, game, MSG_REMINDER)

    t_group = await _group_t(session, game)
    msg_id = await _safe_group_send(
        bot, game, t_group("day.vote_started"), tracker=tracker
    )
    if msg_id is not None:
        game.phase_message_ids[MSG_VOTE] = msg_id

    # Send a private vote keyboard to every alive player (in their language).
    for player in game.alive_players:
        t_user = await _user_t(session, player.user_id)
        try:
            await bot.send_message(
                player.user_id,
                t_user("day.vote_prompt_pm"),
                reply_markup=vote_kb(game, player.user_id, candidates),
            )
        except TelegramAPIError:
            logger.warning(
                "Could not DM vote keyboard to user %s.", player.user_id
            )

    duration = game.settings.vote_duration
    timers.schedule(
        game.game_id,
        duration,
        _on_vote_timeout(bot, games, timers, game, tracker),
        reminders=_phase_reminders(bot, game, t_group, duration, tracker),
    )


def _on_vote_timeout(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    game: GameSession,
    tracker: Optional[MessageTracker] = None,
):
    async def _cb() -> None:
        fresh = await _fresh_session()
        try:
            await end_vote(bot, games, timers, fresh, game, tracker=tracker)
            await fresh.commit()
        except Exception:
            await fresh.rollback()
            raise
        finally:
            await fresh.close()
    return _cb


async def end_vote(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    if games.get(game.chat_id) is not game:
        return
    # Guard against double resolution (vote timer + everyone-voted early close).
    if game.phase is not GamePhase.DAY_VOTE:
        return
    game.phase = GamePhase.DAY_ANNOUNCE
    timers.cancel(game.game_id)

    t_group = await _group_t(session, game)
    result = DayService(game).resolve()
    if result.lynched is not None:
        killed_row = await _kill_player(session, game.game_id, result.lynched.user_id)
        result.lynched.is_alive = False
        game.lynched_ids.add(result.lynched.user_id)
        await _mute_dead(bot, game, [result.lynched])
        victim_name = killed_row.full_name if killed_row else result.lynched.full_name
        await _safe_group_send(
            bot, game,
            vote_result_lynch(
                t_group,
                victim_name,
                Role(result.lynched.role),
                reveal=game.settings.reveal_roles,
            ),
            tracker=tracker,
        )
        await _notify_promotions(bot, session, game)

        # Give the lynched player a brief window for their last words before
        # the game continues. The win-check + next night run afterwards.
        if game.settings.last_word_duration > 0:
            await _begin_last_word(
                bot, games, timers, session, game, result.lynched, tracker=tracker
            )
            return
    else:
        text = (
            vote_result_skipped(t_group) if result.skipped
            else vote_result_no_lynch(t_group)
        )
        await _safe_group_send(bot, game, text, tracker=tracker)

    winner = game.evaluate_winner()
    if winner is not None:
        await end_game(bot, games, timers, session, game, winner, tracker=tracker)
        return

    await start_night(bot, games, timers, session, game, tracker=tracker)


# ---------------------------------------------------------------------------
# Nomination (optional "who goes on trial" stage)
# ---------------------------------------------------------------------------

async def start_nomination_or_vote(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Route the end of the discussion to the right next phase."""
    if game.settings.nomination_mode:
        await start_nomination(bot, games, timers, session, game, tracker=tracker)
        return
    await start_vote(bot, games, timers, session, game, tracker=tracker)


async def start_nomination(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Open the nomination stage: only nominated players can be voted for."""
    if games.get(game.chat_id) is not game:
        return
    if game.phase is not GamePhase.DAY_DISCUSSION:
        return
    timers.cancel(game.game_id)
    game.begin_nomination()

    await _drop_phase_message(bot, game, MSG_DISCUSSION)
    await _drop_phase_message(bot, game, MSG_REMINDER)

    t_group = await _group_t(session, game)
    duration = game.settings.nomination_duration
    msg_id = await _safe_group_send(
        bot, game,
        t_group("day.nomination_started", seconds=duration),
        tracker=tracker,
    )
    if msg_id is not None:
        game.phase_message_ids[MSG_NOMINATION] = msg_id

    for player in game.alive_players:
        t_user = await _user_t(session, player.user_id)
        try:
            await bot.send_message(
                player.user_id,
                t_user("day.nomination_prompt_pm"),
                reply_markup=nominate_kb(game, player.user_id),
            )
        except TelegramAPIError:
            logger.warning(
                "Could not DM nomination keyboard to user %s.", player.user_id
            )

    timers.schedule(
        game.game_id,
        duration,
        _on_nomination_timeout(bot, games, timers, game, tracker),
        reminders=_phase_reminders(bot, game, t_group, duration, tracker),
    )


def _on_nomination_timeout(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    game: GameSession,
    tracker: Optional[MessageTracker] = None,
):
    async def _cb() -> None:
        fresh = await _fresh_session()
        try:
            await start_vote(bot, games, timers, fresh, game, tracker=tracker)
            await fresh.commit()
        except Exception:
            await fresh.rollback()
            raise
        finally:
            await fresh.close()
    return _cb


# ---------------------------------------------------------------------------
# Last word (a lynched player's final message)
# ---------------------------------------------------------------------------

async def _begin_last_word(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    lynched,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Open the last-word window: prompt the lynched player, schedule end."""
    game.phase = GamePhase.DAY_LAST_WORD
    game.awaiting_last_word_from = lynched.user_id
    duration = game.settings.last_word_duration
    t_group = await _group_t(session, game)

    t_user = await _user_t(session, lynched.user_id)
    try:
        await bot.send_message(
            lynched.user_id, t_user("day.last_word_prompt", seconds=duration)
        )
    except TelegramAPIError:
        # If we cannot DM them, there is no point waiting the full window.
        pass
    await _safe_group_send(
        bot, game,
        t_group("day.last_word_wait", name=lynched.full_name, seconds=duration),
        tracker=tracker,
    )
    timers.schedule(
        game.game_id,
        duration,
        _on_last_word_timeout(bot, games, timers, game, tracker),
    )


def _on_last_word_timeout(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    game: GameSession,
    tracker: Optional[MessageTracker] = None,
):
    async def _cb() -> None:
        fresh = await _fresh_session()
        try:
            await _finish_lynch(bot, games, timers, fresh, game, tracker=tracker)
            await fresh.commit()
        except Exception:
            await fresh.rollback()
            raise
        finally:
            await fresh.close()
    return _cb


async def handle_last_word(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    text: str,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Relay the lynched player's final message, then continue the game.

    Called from the private-chat handler when the awaited player types.
    """
    if (
        game.phase is not GamePhase.DAY_LAST_WORD
        or game.awaiting_last_word_from is None
    ):
        return
    speaker = game.get(game.awaiting_last_word_from)
    name = speaker.full_name if speaker is not None else "?"
    t_group = await _group_t(session, game)
    await _safe_group_send(
        bot, game,
        t_group("day.last_word", name=name, text=text),
        tracker=tracker,
    )
    timers.cancel(game.game_id)
    await _finish_lynch(bot, games, timers, session, game, tracker=tracker)


async def _finish_lynch(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Close the last-word window and advance (win-check or next night)."""
    if games.get(game.chat_id) is not game:
        return
    # Guard against double advance (timer + early relay both call this).
    if game.phase is not GamePhase.DAY_LAST_WORD:
        return
    game.phase = GamePhase.DAY_ANNOUNCE
    game.awaiting_last_word_from = None
    timers.cancel(game.game_id)

    winner = game.evaluate_winner()
    if winner is not None:
        await end_game(bot, games, timers, session, game, winner, tracker=tracker)
        return
    await start_night(bot, games, timers, session, game, tracker=tracker)


# ---------------------------------------------------------------------------
# End of game
# ---------------------------------------------------------------------------

async def end_game(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    winner: Winner,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    timers.cancel(game.game_id)
    game.phase = GamePhase.ENDED
    t_group = await _group_t(session, game)

    # Persist final state.
    game_row = await GameRepo(session).get(game.game_id)
    if game_row is not None:
        await GameRepo(session).finish(
            game_row,
            winner=winner.value,
            rounds_played=game.round_number,
        )

    # A new month means: close the old season, pay the top 10, open a
    # fresh one. Best effort - a payout hiccup must not eat the results.
    try:
        closed, payouts = await rollover_if_needed(session)
        if closed is not None:
            await _announce_season(bot, session, closed, payouts)
    except Exception:  # pragma: no cover - season payout is best effort
        logger.exception("Season rollover failed.")

    # Update per-user stats (win/loss depending on role + winner).
    stats = StatsRepo(session)
    # Global payout multiplier: lets admins run "double coins" weekends
    # without a redeploy. 1.0 is the normal economy.
    multiplier = await runtime_config.coin_multiplier(session)
    for player in game.players.values():
        role = Role(player.role)
        if role in THIRD_PARTY_ROLES:
            won = winner is Winner.MANIAC
        elif role in MAFIA_SIDE_ROLES:
            won = winner is Winner.MAFIA
        else:
            won = winner is Winner.CITY
        await stats.record_result(player.user_id, player.full_name, won=won)
        # Pay out the in-game currency: everyone gets a base reward, the
        # winning side gets a bonus, and survivors get a small extra.
        reward = COINS_PER_GAME + (COINS_PER_WIN if won else 0)
        if player.is_alive:
            reward += COINS_SURVIVOR_BONUS
        reward = int(round(reward * multiplier))
        if reward:
            await stats.add_coins(player.user_id, reward)

        # Season rating, per-role history and achievements. Never let a
        # progression hiccup swallow the game-over announcement.
        try:
            row = await stats.get(player.user_id)
            outcome = PlayerOutcome(
                role=role,
                winner=winner,
                won=won,
                survived=player.is_alive,
                rounds=game.round_number,
                players_total=len(game.players),
                games_played=int(getattr(row, "games_played", 0) or 0),
                wins=int(getattr(row, "wins", 0) or 0),
                win_streak=int(getattr(row, "win_streak", 0) or 0),
                coins=int(getattr(row, "coins", 0) or 0),
                role_games=0,
                role_wins=0,
                correct_checks=game.stat_correct_checks.get(player.user_id, 0),
                first_check_found_mafia=(
                    player.user_id in game.first_check_hits
                ),
                heals_landed=game.stat_heals.get(player.user_id, 0),
                kills=game.stat_kills.get(player.user_id, 0),
                blocked_actions=game.stat_blocks.get(player.user_id, 0),
                was_lynched=player.user_id in game.lynched_ids,
                last_alive_town=(
                    won and player.is_alive
                    and role not in MAFIA_SIDE_ROLES
                    and role not in THIRD_PARTY_ROLES
                    and len(game.alive_players) == 1
                ),
            )
            progress = await apply_game_result(
                session, user_id=player.user_id, outcome=outcome
            )
            await _notify_progress(bot, session, player.user_id, progress)
        except Exception:  # pragma: no cover - progression is best effort
            logger.exception(
                "Progression update failed for %s.", player.user_id
            )

    games.remove(game.chat_id)

    # Public announcement + role reveal in the group language.
    reveal = "\n".join(
        role_reveal_line(t_group, p.full_name, Role(p.role)) for p in game.players.values()
    )
    try:
        await bot.send_message(
            game.chat_id,
            f"{game_over(t_group, winner)}\n\n{role_reveal_header(t_group)}\n{reveal}"
            f"\n\n{t_group('shop.rewards_note', base=COINS_PER_GAME, win=COINS_PER_WIN, survive=COINS_SURVIVOR_BONUS)}",
            reply_markup=rematch_kb(t_group),
        )
    except TelegramAPIError:
        logger.warning("Could not post game-over to chat %s.", game.chat_id)
    finally:
        # Always restore chat permissions, even if the reveal send failed.
        # ``include_dead`` releases the eliminated players too.
        await _unmute_all(bot, game, include_dead=True)
        if tracker is not None:
            tracker.forget(game.chat_id)


async def _announce_season(bot: Bot, session, season, payouts) -> None:
    """DM every season winner their placement and coin reward."""
    for place, (user_id, name, mmr, coins) in enumerate(payouts, start=1):
        t_user = await _user_t(session, user_id)
        try:
            await bot.send_message(
                user_id,
                t_user(
                    "season.reward",
                    season=season.name,
                    place=place,
                    mmr=mmr,
                    coins=coins,
                ),
            )
        except TelegramAPIError:
            logger.debug("Could not DM the season reward to %s.", user_id)


async def _notify_progress(
    bot: Bot, session: AsyncSession, user_id: int, progress
) -> None:
    """DM the rating change and any freshly unlocked achievements."""
    if progress is None:
        return
    t_user = await _user_t(session, user_id)
    lines = [
        t_user(
            "rating.changed",
            delta=f"{progress.mmr_delta:+d}",
            total=progress.mmr_total,
        )
    ]
    for achievement in progress.unlocked:
        lines.append(
            t_user(
                "achv.unlocked",
                name=t_user(f"achv.{achievement.code}.name"),
                desc=t_user(f"achv.{achievement.code}.desc"),
                reward=achievement.reward,
            )
        )
    try:
        await bot.send_message(user_id, "\n".join(lines))
    except TelegramAPIError:
        logger.debug("Could not DM progression to %s.", user_id)


# ---------------------------------------------------------------------------
# Cancel hook (called by /cancel during a running game)
# ---------------------------------------------------------------------------

async def cancel_running_game(
    bot: Bot, game: GameSession, *, tracker: Optional[MessageTracker] = None
) -> None:
    """Restore chat permissions when a game is cancelled mid-flight."""
    await _unmute_all(bot, game, include_dead=True)
    if tracker is not None:
        tracker.forget(game.chat_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _kill_player(
    session: AsyncSession, game_id: int, user_id: int
) -> Optional[object]:
    player = await PlayerRepo(session).get(game_id, user_id)
    if player is not None:
        await PlayerRepo(session).kill(player)
    return player


async def _handle_afk(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    """Update anti-AFK strike counters and notify players.

    Called once at the end of every night while the night's ``acted`` set
    is still intact. Players who had something to do but did not act gain
    a strike: the first strike earns a private warning, the second (per
    ``AFK_STRIKES_LIMIT``) flags them as AFK so subsequent nights no
    longer wait for them, and the group is told they are being skipped.
    """
    newly_afk = game.record_night_activity()
    # Private warning to anyone who just got their first strike.
    for player in game.alive_players:
        strikes = game.afk_strikes.get(player.user_id, 0)
        if strikes == 1:
            t_user = await _user_t(session, player.user_id)
            try:
                await bot.send_message(player.user_id, t_user("afk.warning"))
            except TelegramAPIError:
                pass
    # Public notice for players who just crossed the skip threshold.
    for player in newly_afk:
        t_group = await _group_t(session, game)
        await _safe_group_send(
            bot, game, t_group("afk.skipped", name=player.full_name)
        )


async def _notify_promotions(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    """Run role succession and privately tell each promoted player.

    Called after deaths are applied (both night and lynch). Returns
    nothing; promotions are reflected on the live ``GameSession`` so the
    next night's prompts reach the right roles.
    """
    promoted = game.apply_succession()
    for player, new_role in promoted:
        t_user = await _user_t(session, player.user_id)
        key = (
            "role.promoted_don" if new_role is Role.DON
            else "role.promoted_detective"
        )
        try:
            await bot.send_message(player.user_id, t_user(key))
        except TelegramAPIError:
            logger.warning(
                "Could not DM promotion to user %s.", player.user_id
            )


async def _prompt_role(
    bot: Bot,
    session: AsyncSession,
    game: GameSession,
    *,
    role: Role,
    prompt_key: str,
    keyboard,
    log_name: str,
    only_first: bool = True,
) -> None:
    """Send the night-action prompt + targets keyboard to every alive ``role``.

    Single-role prompters (detective, doctor, …) pass ``only_first=True``
    since the rules guarantee at most one alive holder; mafia passes
    ``False`` so the whole kill-voting family is prompted. ``keyboard`` is
    the keyboard-builder callable taking ``(game, user_id)``.
    """
    # The lawyer is mafia-aligned but does NOT know the family and must not
    # receive the mafia kill prompt; only the kill-voting roles (mafia + don)
    # are prompted here. The lawyer gets his own separate prompt.
    players = (
        game.alive_mafia_killers() if role is Role.MAFIA else game.alive_of(role)
    )
    if not players:
        return
    targets = players[:1] if only_first and role is not Role.MAFIA else players
    for member in targets:
        t_user = await _user_t(session, member.user_id)
        try:
            await bot.send_message(
                member.user_id,
                t_user(prompt_key),
                reply_markup=keyboard(game, member.user_id),
            )
        except TelegramAPIError:
            logger.warning(
                "Could not DM %s prompt to %s.", log_name, member.user_id
            )


async def _prompt_mafia(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    # The whole family sees each other's kill votes; prompt every member.
    await _prompt_role(
        bot, session, game,
        role=Role.MAFIA, prompt_key="night.mafia_prompt",
        keyboard=mafia_targets_kb, log_name="mafia", only_first=False,
    )


async def _prompt_detective(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    await _prompt_role(
        bot, session, game,
        role=Role.DETECTIVE, prompt_key="night.detective_prompt",
        keyboard=detective_targets_kb, log_name="detective",
    )


async def _prompt_detective_shoot(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    """Offer the detective his gun as an alternative to the check.

    Sent as a separate message so he sees two keyboards and picks one;
    whichever he presses first consumes his night action.
    """
    if not game.settings.detective_can_shoot:
        return
    # An admin can disable the gun globally without touching per-game
    # settings (e.g. while its balance is being re-tuned).
    if not await runtime_config.feature_enabled(
        session, KEY_FEATURE_DETECTIVE_SHOOT
    ):
        return
    await _prompt_role(
        bot, session, game,
        role=Role.DETECTIVE, prompt_key="night.detective_shoot_prompt",
        keyboard=detective_shoot_kb, log_name="detective shot",
    )


async def _prompt_doctor(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    await _prompt_role(
        bot, session, game,
        role=Role.DOCTOR, prompt_key="night.doctor_prompt",
        keyboard=doctor_targets_kb, log_name="doctor",
    )


async def _prompt_whore(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    await _prompt_role(
        bot, session, game,
        role=Role.WHORE, prompt_key="night.whore_prompt",
        keyboard=whore_targets_kb, log_name="whore",
    )


async def _prompt_maniac(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    await _prompt_role(
        bot, session, game,
        role=Role.MANIAC, prompt_key="night.maniac_prompt",
        keyboard=maniac_targets_kb, log_name="maniac",
    )


async def _prompt_lawyer(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    await _prompt_role(
        bot, session, game,
        role=Role.LAWYER, prompt_key="night.lawyer_prompt",
        keyboard=lawyer_targets_kb, log_name="lawyer",
    )


async def _prompt_don_search(
    bot: Bot, session: AsyncSession, game: GameSession
) -> None:
    # The don's search is an OPTIONAL secondary action (it does not block
    # the night and does not count toward ``acted``); the keyboard is only
    # sent while no search has been recorded yet this night.
    if game.night.don_check_target is not None:
        return
    await _prompt_role(
        bot, session, game,
        role=Role.DON, prompt_key="night.don_search_prompt",
        keyboard=don_search_kb, log_name="don search",
    )


# ---------------------------------------------------------------------------
# Admin panel hooks
# ---------------------------------------------------------------------------
#
# These are the only supported way for the admin panel to interfere with a
# running game. They deliberately reuse the normal phase machinery (the very
# same ``_on_*_timeout`` callbacks the timer fires) instead of poking at the
# session directly, so an admin-triggered transition is indistinguishable
# from a natural one and cannot desynchronise the state.


def _phase_timeout_callback(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    tracker: Optional[MessageTracker],
):
    """Return the callback the timer would fire for the current phase.

    ``None`` means the phase has no timed transition (announcements and
    finished games), in which case there is nothing to skip or extend.
    """
    phase = game.phase
    if phase is GamePhase.NIGHT:
        return _on_night_timeout(bot, games, timers, game, tracker)
    if phase is GamePhase.DAY_DISCUSSION:
        return _on_discussion_timeout(
            bot, games, timers, session, game, tracker
        )
    if phase is GamePhase.DAY_NOMINATION:
        return _on_nomination_timeout(bot, games, timers, game, tracker)
    if phase is GamePhase.DAY_VOTE:
        return _on_vote_timeout(bot, games, timers, game, tracker)
    if phase is GamePhase.DAY_LAST_WORD:
        return _on_last_word_timeout(bot, games, timers, game, tracker)
    return None


async def admin_skip_phase(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> bool:
    """End the current phase immediately, as if its timer had elapsed.

    Returns ``False`` when the current phase has no timed transition.
    """
    callback = _phase_timeout_callback(
        bot, games, timers, session, game, tracker
    )
    if callback is None:
        return False
    timers.cancel(game.game_id)
    await callback()
    return True


async def admin_extend_phase(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    seconds: int,
    *,
    tracker: Optional[MessageTracker] = None,
) -> bool:
    """Restart the current phase timer with ``seconds`` from now.

    Note this sets the remaining time rather than adding to it: the
    manager stores no deadline, and re-deriving one would be guesswork.
    Phase reminders are re-armed for the new duration.
    """
    callback = _phase_timeout_callback(
        bot, games, timers, session, game, tracker
    )
    if callback is None:
        return False
    t_group = await _group_t(session, game)
    # A stale "10 seconds left" notice from the old timer must not survive
    # into the extended phase.
    await _drop_phase_message(bot, game, MSG_REMINDER)
    timers.schedule(
        game.game_id,
        seconds,
        callback,
        reminders=_phase_reminders(bot, game, t_group, seconds, tracker),
    )
    return True


async def admin_force_end(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    *,
    tracker: Optional[MessageTracker] = None,
) -> None:
    """Terminate a stuck game: reveal roles, unmute everyone, clean up.

    Recorded as ``Winner.NONE`` so nobody gets a win credited and no
    coins are paid for a game that never actually finished.
    """
    timers.cancel(game.game_id)
    await end_game(
        bot, games, timers, session, game, Winner.NONE, tracker=tracker
    )


async def admin_kick_player(
    bot: Bot,
    games: LobbyService,
    timers: TimerManager,
    session: AsyncSession,
    game: GameSession,
    user_id: int,
    *,
    tracker: Optional[MessageTracker] = None,
) -> bool:
    """Remove a player mid-game, treating them as eliminated.

    The victory condition is re-evaluated afterwards, so kicking the last
    mafioso ends the game properly instead of leaving it unwinnable.
    """
    player = game.get(user_id)
    if player is None or not player.is_alive:
        return False
    player.is_alive = False
    await _kill_player(session, game.game_id, user_id)
    await _mute_dead(bot, game, [player])

    winner = game.evaluate_winner()
    if winner is not Winner.NONE:
        await end_game(
            bot, games, timers, session, game, winner, tracker=tracker
        )
    return True
