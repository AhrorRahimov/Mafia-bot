"""Lobby operations and the process-local session registry.

The registry maps ``chat_id -> GameSession`` for any running game in
this process. Database rows (``Game``, ``Player``) mirror the data so
that finished games and stats survive restarts.

Lobby gathering (the wait for players after ``/newgame``) is governed
here too: a per-chat countdown auto-starts the game once enough players
join, or dissolves the lobby on timeout. The live lobby card is edited
in place every ``LOBBY_REFRESH_INTERVAL`` seconds and on every join /
leave so it stays visible at the top of the chat.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from time import monotonic
from typing import Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Game
from app.db.repo import GameRepo, PlayerRepo, StatsRepo
from app.db.inventory_repo import InventoryRepo, RoleClaimRepo
from app.game.balance import assign_roles
from app.game.constants import (
    LOBBY_MAX_TIMEOUT,
    LOBBY_MIN_TIMEOUT,
    LOBBY_REFRESH_INTERVAL,
    LOBBY_TIMEOUT,
    MAX_PLAYERS,
    MIN_PLAYERS,
)
from app.game.enums import GameStatus, Role
from app.game.exceptions import CREATOR_LEFT, LobbyError
from app.game.settings import GameSettings
from app.services.session import GameSession

if TYPE_CHECKING:
    from aiogram import Bot

    from app.services.cleanup import MessageTracker
    from app.services.timer import TimerManager

logger = logging.getLogger(__name__)


@dataclass
class LobbyMeta:
    """Per-lobby live state used to drive the gathering timer + card.

    ``deadline`` is a ``time.monotonic`` instant at which the lobby is
    due to auto-start or dissolve. ``refresh_task`` is the periodic card
    updater; it is cancelled on teardown.
    """

    bot: "Bot" = None  # type: ignore[assignment]
    creator_id: int = 0
    creator_name: str = ""
    card_message_id: Optional[int] = None
    deadline: float = 0.0
    refresh_task: Optional[asyncio.Task[None]] = None
    # Translator for group messages is resolved lazily on each refresh.


class LobbyService:
    """Manages lobby creation, joining, leaving and starting games."""

    def __init__(self) -> None:
        # chat_id -> live GameSession (only running games live here)
        self._sessions: dict[int, GameSession] = {}
        # chat_id -> creator_id for active lobbies (status LOBBY)
        self._lobby_creators: dict[int, int] = {}
        # chat_id -> live gathering metadata (timer + card)
        self._lobby_meta: dict[int, LobbyMeta] = {}
        # chat_id -> creator-tuned GameSettings (timings + optional roles)
        self._settings: dict[int, GameSettings] = {}
        # Bound after construction in main.py (avoids import cycles).
        self._timers: "Optional[TimerManager]" = None
        self._tracker: "Optional[MessageTracker]" = None

    # --- wiring ----------------------------------------------------------

    def bind(
        self, timers: "TimerManager", tracker: "MessageTracker"
    ) -> None:
        """Receive the shared timer + message tracker (called from main)."""
        self._timers = timers
        self._tracker = tracker

    # --- registry access -------------------------------------------------

    def get(self, chat_id: int) -> Optional[GameSession]:
        return self._sessions.get(chat_id)

    def has(self, chat_id: int) -> bool:
        return chat_id in self._sessions

    def remove(self, chat_id: int) -> Optional[GameSession]:
        self._lobby_creators.pop(chat_id, None)
        self.teardown_lobby(chat_id)
        return self._sessions.pop(chat_id, None)

    def get_settings(self, chat_id: int):
        """Return the lobby settings (default if none)."""
        return self._settings.setdefault(chat_id, GameSettings())

    # --- lobby lifecycle -------------------------------------------------

    async def create_lobby(
        self,
        db: AsyncSession,
        chat_id: int,
        creator_id: int,
        creator_name: str,
        bot: "Bot",
        card_message_id: int,
        username: str | None = None,
    ) -> Game:
        """Create a new lobby and start its gathering countdown.

        Raises if another game is active in the chat. ``card_message_id``
        is the already-posted lobby card to refresh in place.
        """
        if self.has(chat_id):
            raise LobbyError("errors.game_already_active")
        # Clear out a stale "zombie" lobby row from a previous process.
        await self._clear_zombie_lobby(db, chat_id)
        active = await GameRepo(db).get_active(chat_id)
        if active is not None and active.status != GameStatus.LOBBY:
            raise LobbyError("errors.game_already_active")

        game = await GameRepo(db).create(chat_id=chat_id, creator_id=creator_id)
        await PlayerRepo(db).add(
            game_id=game.id, user_id=creator_id, full_name=creator_name
        )
        await StatsRepo(db).upsert_touch(creator_id, creator_name, username)
        await db.commit()

        self._lobby_creators[chat_id] = creator_id
        meta = LobbyMeta(
            bot=bot,
            creator_id=creator_id,
            creator_name=creator_name,
            card_message_id=card_message_id,
            deadline=monotonic() + LOBBY_TIMEOUT,
        )
        self._lobby_meta[chat_id] = meta
        self._start_gathering_timer(chat_id, LOBBY_TIMEOUT)
        self._start_card_refresh(chat_id)
        logger.info("Lobby created: chat=%s creator=%s game_id=%s",
                    chat_id, creator_id, game.id)
        return game

    async def _clear_zombie_lobby(
        self, db: AsyncSession, chat_id: int
    ) -> None:
        """Finish a stale LOBBY row with no live in-memory counterpart.

        After a restart, an old lobby row would otherwise block a new
        game with ``game_already_active``. We only dissolve rows that
        are still in the LOBBY status (a true RUNNING game is rare to
        survive a restart since sessions are in-memory).
        """
        stale = await GameRepo(db).get_active(chat_id)
        if stale is not None and stale.status == GameStatus.LOBBY:
            await GameRepo(db).set_status(stale, GameStatus.FINISHED)
            stale.winner = "none"
            logger.info("Dissolved zombie lobby: chat=%s game_id=%s",
                        chat_id, stale.id)

    async def join(
        self,
        db: AsyncSession,
        chat_id: int,
        user_id: int,
        full_name: str,
        username: str | None = None,
    ) -> Game:
        """Add a player to the active lobby for ``chat_id``."""
        game = await self._require_lobby(db, chat_id)
        players = await PlayerRepo(db).list_by_game(game.id)

        if any(p.user_id == user_id for p in players):
            raise LobbyError("errors.already_in_lobby")
        if len(players) >= MAX_PLAYERS:
            raise LobbyError("errors.lobby_full", max=MAX_PLAYERS)

        await PlayerRepo(db).add(
            game_id=game.id, user_id=user_id, full_name=full_name
        )
        await StatsRepo(db).upsert_touch(user_id, full_name, username)
        await db.commit()
        # Reflect the new roster on the live card immediately.
        await self._refresh_card(chat_id, db)
        logger.info("Player joined: chat=%s user=%s", chat_id, user_id)
        return game

    async def leave(
        self,
        db: AsyncSession,
        chat_id: int,
        user_id: int,
    ) -> Game:
        """Remove a player from the active lobby."""
        game = await self._require_lobby(db, chat_id)
        players = await PlayerRepo(db).list_by_game(game.id)
        player = next((p for p in players if p.user_id == user_id), None)
        if player is None:
            raise LobbyError("errors.not_in_lobby")

        # Creator leaving dissolves the lobby.
        if game.creator_id == user_id:
            await self._cancel_lobby(db, chat_id, game)
            raise LobbyError(CREATOR_LEFT)

        await PlayerRepo(db).remove(player)
        await db.commit()
        await self._refresh_card(chat_id, db)
        logger.info("Player left: chat=%s user=%s", chat_id, user_id)
        return game

    async def cancel(
        self, db: AsyncSession, chat_id: int, by_user_id: int
    ) -> None:
        """Cancel the lobby (creator-only)."""
        game = await self._require_lobby(db, chat_id)
        if game.creator_id != by_user_id:
            raise LobbyError("errors.only_creator_cancel")
        await self._cancel_lobby(db, chat_id, game)

    async def start(
        self,
        db: AsyncSession,
        chat_id: int,
        rng: Optional[random.Random] = None,
    ) -> GameSession:
        """Promote the lobby to a running game and return its session."""
        from app.services.session import PlayerState

        game = await self._require_lobby(db, chat_id)
        players = await PlayerRepo(db).list_by_game(game.id)

        if game.creator_id not in {p.user_id for p in players}:
            # Edge case: creator left but lobby still tracked. Reset.
            await self._cancel_lobby(db, chat_id, game)
            raise LobbyError("lobby.creator_left_new_game")

        count = len(players)
        if not (MIN_PLAYERS <= count <= MAX_PLAYERS):
            raise LobbyError(
                "errors.need_players_range", min=MIN_PLAYERS, max=MAX_PLAYERS, count=count
            )

        # Assign roles using the creator-chosen settings so optional
        # roles (Don / Whore) toggled in the lobby actually take effect.
        settings = self._settings.get(chat_id, GameSettings())
        user_ids = [p.user_id for p in players]

        # Role cards: honour activated claims, refund the ones that could
        # not be satisfied (role disabled in this lobby or already taken).
        claim_repo = RoleClaimRepo(db)
        claims = await claim_repo.active_for(user_ids)
        forced: dict[int, Role] = {}
        for user_id, claim in claims.items():
            try:
                forced[user_id] = Role(claim.role)
            except ValueError:
                await claim_repo.cancel(claim)

        assignments = assign_roles(
            user_ids, settings=settings, rng=rng, forced=forced
        )

        honoured: dict[int, bool] = {}
        for user_id, claim in claims.items():
            if user_id not in forced:
                continue
            if assignments.get(user_id) is forced[user_id]:
                await claim_repo.consume(claim)
                honoured[user_id] = True
            else:
                # Somebody else got the role first - give the card back.
                await claim_repo.cancel(claim)
                if claim.item_id:
                    await InventoryRepo(db).add(user_id, claim.item_id, 1)
                honoured[user_id] = False
        player_repo = PlayerRepo(db)
        name_by_id = {p.user_id: p.full_name for p in players}
        states: dict[int, PlayerState] = {}
        for user_id, role in assignments.items():
            # Persist role on the matching Player row.
            row = next(p for p in players if p.user_id == user_id)
            await player_repo.assign_role(row, Role(role))
            states[user_id] = PlayerState(
                user_id=user_id,
                full_name=name_by_id[user_id],
                role=Role(role),
            )

        await GameRepo(db).set_status(game, GameStatus.RUNNING)
        await db.commit()

        session = GameSession(
            game_id=game.id,
            chat_id=chat_id,
            creator_id=game.creator_id,
            players=states,
        )
        # Carry the creator's chosen settings into the live session.
        session.settings = settings
        # user_id -> True when their role card worked, False when refunded.
        session.card_results = honoured
        self._sessions[chat_id] = session
        self._lobby_creators.pop(chat_id, None)
        self.teardown_lobby(chat_id)
        logger.info(
            "Game started: chat=%s game_id=%s players=%s",
            chat_id, game.id, count,
        )
        return session

    # --- gathering timer & card refresh ---------------------------------

    def _start_gathering_timer(self, chat_id: int, delay: float) -> None:
        """(Re)schedule the auto-start/dissolve callback for the lobby."""
        if self._timers is None:
            return

        async def _on_expire() -> None:
            await self._on_gathering_expire(chat_id)

        self._timers.reschedule_lobby(chat_id, delay, _on_expire)

    def _start_card_refresh(self, chat_id: int) -> None:
        """Spawn the periodic lobby-card editor (every REFRESH_INTERVAL)."""
        meta = self._lobby_meta.get(chat_id)
        if meta is None or meta.refresh_task is not None:
            return

        async def _loop() -> None:
            try:
                while chat_id in self._lobby_meta:
                    await asyncio.sleep(LOBBY_REFRESH_INTERVAL)
                    meta2 = self._lobby_meta.get(chat_id)
                    if meta2 is None:
                        return
                    await self._refresh_card(chat_id, db=None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Card refresh loop failed for chat %s.", chat_id)

        meta.refresh_task = asyncio.create_task(
            _loop(), name=f"mafia-card-refresh-{chat_id}"
        )

    async def _on_gathering_expire(self, chat_id: int) -> None:
        """Auto-start if enough players, otherwise dissolve the lobby."""
        from app.db.session import get_session_factory
        from app.i18n import get_i18n
        from app.services.orchestrator import start_night

        if chat_id not in self._lobby_meta:
            return
        meta = self._lobby_meta[chat_id]
        bot = meta.bot
        factory = get_session_factory()
        async with factory() as db:
            game = await GameRepo(db).get_active(chat_id)
            if game is None or game.status != GameStatus.LOBBY:
                # Lobby row already gone (e.g. creator left concurrently).
                self.teardown_lobby(chat_id)
                return
            players = await PlayerRepo(db).list_by_game(game.id)
            count = len(players)
            t_group = get_i18n().translator_for(
                await StatsRepo(db).get_language(meta.creator_id)
            )

            if MIN_PLAYERS <= count <= MAX_PLAYERS:
                # Enough players — promote to a running game.
                try:
                    game_session = await self.start(db=db, chat_id=chat_id)
                except LobbyError:
                    self.teardown_lobby(chat_id)
                    return
                await db.commit()
                # Announce auto-start by editing the lobby card in place.
                await self._safe_edit(
                    bot, chat_id, meta.card_message_id,
                    t_group("lobby.autostarted", count=count),
                )
                await _notify_group_started(bot, chat_id, game_session, db)
                await _send_roles_via_bot(bot, db, game_session)
                async with factory() as night_db:
                    try:
                        await start_night(
                            bot, self, self._timers, night_db, game_session,
                            tracker=self._tracker,
                        )
                        await night_db.commit()
                    except Exception:
                        await night_db.rollback()
                        raise
            else:
                # Not enough players — dissolve the lobby.
                await self._cancel_lobby(db, chat_id, game)
                await self._safe_edit(
                    bot, chat_id, meta.card_message_id,
                    t_group("lobby.timeout_dismissed", count=count, min=MIN_PLAYERS),
                )

    async def _refresh_card(
        self, chat_id: int, db: Optional[AsyncSession]
    ) -> None:
        """Re-render the lobby card with the current roster + countdown."""
        meta = self._lobby_meta.get(chat_id)
        if meta is None or meta.card_message_id is None:
            return
        owns_session = db is None
        if owns_session:
            from app.db.session import get_session_factory
            db = get_session_factory()()
        try:
            from app.i18n import get_i18n
            from app.texts import lobby_opened_countdown

            game = await GameRepo(db).get_active(chat_id)  # type: ignore[union-attr]
            if game is None:
                return
            players = await PlayerRepo(db).list_by_game(game.id)  # type: ignore[union-attr]
            names = [p.full_name for p in players]
            t_group = get_i18n().translator_for(
                await StatsRepo(db).get_language(meta.creator_id)  # type: ignore[union-attr]
            )
            remaining = max(0, int(meta.deadline - monotonic()))
            from app.keyboards.inline import lobby_kb
            text = lobby_opened_countdown(
                t_group, meta.creator_name, names, remaining
            )
            await self._safe_edit(
                meta.bot, chat_id, meta.card_message_id, text,
                reply_markup=lobby_kb(game.id, t_group),
            )
        finally:
            if owns_session:
                await db.close()  # type: ignore[union-attr]

    @staticmethod
    async def _safe_edit(
        bot: "Bot", chat_id: int, message_id: Optional[int], text: str,
        reply_markup=None,
    ) -> None:
        """Edit a message in place, best-effort (card may be gone).

        ``reply_markup`` must be re-supplied on every edit: Telegram
        drops the inline keyboard when ``editMessageText`` omits it, so
        without this the lobby Join/Leave/Start buttons would disappear
        on the first countdown refresh.
        """
        from aiogram.exceptions import TelegramAPIError
        if message_id is None:
            return
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramAPIError:
            pass

    def teardown_lobby(self, chat_id: int) -> None:
        """Stop the gathering timer + card refresh; drop lobby metadata."""
        meta = self._lobby_meta.pop(chat_id, None)
        if meta is not None and meta.refresh_task is not None:
            meta.refresh_task.cancel()
        if self._timers is not None:
            self._timers.cancel_lobby(chat_id)
        self._settings.pop(chat_id, None)

    # --- extend / shorten the gathering wait ----------------------------

    def adjust_deadline(
        self, chat_id: int, delta_seconds: int
    ) -> Optional[int]:
        """Move the gathering deadline by ``delta_seconds`` (signed).

        Returns the new remaining seconds (clamped to the legal range),
        or ``None`` if there is no live lobby for ``chat_id``.
        """
        meta = self._lobby_meta.get(chat_id)
        if meta is None:
            return None
        now = monotonic()
        current_remaining = max(0.0, meta.deadline - now)
        # New remaining time, clamped so it never goes below the floor or
        # above the ceiling (measured from "time already elapsed").
        new_remaining = current_remaining + delta_seconds
        # Total elapsed budget cap: LOBBY_MAX_TIMEOUT from the original start.
        # We approximate by clamping absolute remaining into [MIN, MAX].
        new_remaining = max(
            float(LOBBY_MIN_TIMEOUT),
            min(float(LOBBY_MAX_TIMEOUT), new_remaining),
        )
        meta.deadline = now + new_remaining
        # Re-arm the gathering timer with the new remaining.
        self._start_gathering_timer(chat_id, new_remaining)
        return int(new_remaining)

    # --- private helpers -------------------------------------------------

    async def _require_lobby(self, db: AsyncSession, chat_id: int) -> Game:
        game = await GameRepo(db).get_active(chat_id)
        if game is None or game.status != GameStatus.LOBBY:
            raise LobbyError("errors.lobby_not_found")
        return game

    async def _cancel_lobby(
        self, db: AsyncSession, chat_id: int, game: Game
    ) -> None:
        await GameRepo(db).set_status(game, GameStatus.FINISHED)
        game.winner = "none"
        await db.commit()
        # Stop the gathering timer + card refresh and clear lobby metadata.
        # ``_sessions`` has no entry while the game is still in LOBBY status,
        # so there is nothing else to remove here.
        self.teardown_lobby(chat_id)
        logger.info("Lobby cancelled: chat=%s game_id=%s", chat_id, game.id)


# Module-level helpers kept here to avoid circular imports with handlers.

async def _notify_group_started(
    bot: "Bot", chat_id: int, game_session, db: AsyncSession
) -> None:
    """Post the "game started" line to the group (best-effort)."""
    from aiogram.exceptions import TelegramAPIError
    from app.i18n import get_i18n

    t_group = get_i18n().translator_for(
        await StatsRepo(db).get_language(game_session.creator_id)
    )
    try:
        await bot.send_message(
            chat_id,
            t_group("lobby.game_started", count=len(game_session.players)),
        )
    except TelegramAPIError:
        pass


async def _send_roles_via_bot(bot: "Bot", db: AsyncSession, game_session) -> None:
    """DM each player their role in their own language (best-effort)."""
    import logging as _logging
    from aiogram.exceptions import TelegramAPIError

    from app.i18n import get_i18n
    from app.texts import mafia_extra_for, your_role

    log = _logging.getLogger(__name__)
    i18n = get_i18n()
    stats_repo = StatsRepo(db)
    for user_id, player in game_session.players.items():
        lang = await stats_repo.get_language(user_id)
        t = i18n.translator_for(lang)
        extra = mafia_extra_for(t, game_session, user_id)
        try:
            await bot.send_message(user_id, your_role(t, player.role, extra))
        except TelegramAPIError:
            log.warning("Could not DM user %s their role (bot blocked?).", user_id)
