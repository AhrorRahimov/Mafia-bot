"""Inline button panel for admins in a private chat.

Every button carries an ``adm:<route>[:<arg>]`` payload; the router
below maps a route to a screen. Read-only screens are re-rendered in
place with ``edit_text`` so the panel stays a single message instead of
spamming the chat, while destructive routes go through a confirmation
screen first.

The heavy lifting lives in ``app.handlers.admin`` and
``app.services.orchestrator``: this module only renders and dispatches.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.admin_repo import (
    AdminRepo,
    AnalyticsRepo,
    AuditRepo,
    BanRepo,
    PromoRepo,
)
from app.db.repo import StatsRepo
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction, parse_admin_route
from app.keyboards.inline import (
    admin_admins_kb,
    admin_back_kb,
    admin_bans_kb,
    admin_chatbans_kb,
    admin_economy_kb,
    admin_moderation_kb,
    admin_pager_kb,
    admin_promos_kb,
    admin_confirm_kb,
    admin_flags_kb,
    admin_game_kb,
    admin_games_kb,
    admin_menu_kb,
    admin_section_kb,
)
from app.services.admin import (
    FEATURE_FLAGS,
    KEY_COIN_MULTIPLIER,
    KEY_MAINTENANCE,
    PENDING_BROADCASTS,
    broadcast,
    is_admin,
    is_owner,
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
    admin_skip_phase,
)
from app.services.timer import TimerManager
from app.i18n import get_i18n

logger = logging.getLogger(__name__)
router = Router(name="admin_panel")

# One tap on "+30s" adds a comfortable extra half-minute.
PANEL_EXTEND_SECONDS = 30
# Rows per page in the button-driven ban / promo lists.
PANEL_LIST_SIZE = 8


def _pages(total: int, size: int = PANEL_LIST_SIZE) -> int:
    """Page count, never below one so an empty list still renders."""
    return max(1, (total + size - 1) // size)


def _page_arg(args: list[str]) -> int:
    """First route argument as a zero-based page index, tolerating junk."""
    if args and args[0].lstrip("-").isdigit():
        return max(0, int(args[0]))
    return 0


async def _render(
    callback: CallbackQuery, text: str, markup=None
) -> None:
    """Replace the panel message, tolerating an unchanged payload.

    Telegram rejects an edit that would not change anything (for example
    tapping "refresh" twice in a row); that is not an error worth
    surfacing to the admin.
    """
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _admin_names(
    session: AsyncSession, admin_ids: list[int]
) -> dict[int, str]:
    """Map admin ids to display names for the journal, best effort."""
    names: dict[int, str] = {}
    stats = StatsRepo(session)
    for admin_id in admin_ids:
        try:
            row = await stats.get(admin_id)
        except Exception:  # pragma: no cover - never break the journal
            row = None
        if row is not None and getattr(row, "full_name", ""):
            names[admin_id] = row.full_name
    return names


async def _menu(
    callback: CallbackQuery, session: AsyncSession, games: LobbyService,
    t: Translator,
) -> None:
    maintenance = await runtime_config.is_maintenance(session)
    await _render(
        callback,
        t(
            "admin.panel_header",
            games=len(games._sessions),
            mode=t("admin.mode_maintenance") if maintenance
            else t("admin.mode_normal"),
        ),
        admin_menu_kb(t, is_owner=is_owner(callback.from_user.id)),
    )


@router.callback_query(
    lambda query: (query.data or "").startswith(
        f"{CallbackAction.ADMIN.value}:"
    )
    or query.data == CallbackAction.ADMIN.value
)
async def cb_admin(
    callback: CallbackQuery,
    session: AsyncSession,
    games: LobbyService,
    timers: TimerManager,
    bot: Bot,
    t: Translator,
    tracker,
) -> None:
    """Single entry point for every admin button."""
    user_id = callback.from_user.id
    if not await is_admin(session, user_id):
        # Rights can be revoked while an old panel is still on screen.
        await callback.answer(t("admin.denied"), show_alert=True)
        return

    route, args = parse_admin_route(callback.data or "")
    audit = AuditRepo(session)

    # --- navigation ---------------------------------------------------
    if route == "menu":
        await _menu(callback, session, games, t)

    elif route == "games":
        sessions = list(games._sessions.values())
        text = (
            t("admin.games_header", count=len(sessions))
            if sessions else t("admin.games_empty")
        )
        await _render(callback, text, admin_games_kb(sessions, t))

    elif route == "game":
        game = games.get(int(args[0])) if args else None
        if game is None:
            await callback.answer(t("admin.game_not_found"), show_alert=True)
            await _menu(callback, session, games, t)
            return
        await _render(
            callback,
            t("admin.game_card", chat=game.chat_id, phase=game.phase.value,
              round=game.round_number, alive=len(game.alive_players),
              total=len(game.players)),
            admin_game_kb(game.chat_id, t),
        )

    # --- game actions --------------------------------------------------
    elif route in {"extend", "skip", "roles", "endgame", "confirmend"}:
        game = games.get(int(args[0])) if args else None
        if game is None:
            await callback.answer(t("admin.game_not_found"), show_alert=True)
            return

        if route == "extend":
            ok = await admin_extend_phase(
                bot, games, timers, session, game, PANEL_EXTEND_SECONDS,
                tracker=tracker,
            )
            if not ok:
                await callback.answer(
                    t("admin.phase_not_skippable", phase=game.phase.value),
                    show_alert=True,
                )
                return
            await audit.log(user_id, "game.extend", target=str(game.chat_id),
                            details=str(PANEL_EXTEND_SECONDS))
            await callback.answer(
                t("admin.phase_extended", seconds=PANEL_EXTEND_SECONDS)
            )

        elif route == "skip":
            phase = game.phase.value
            ok = await admin_skip_phase(
                bot, games, timers, session, game, tracker=tracker
            )
            if not ok:
                await callback.answer(
                    t("admin.phase_not_skippable", phase=phase),
                    show_alert=True,
                )
                return
            await audit.log(user_id, "game.skip_phase",
                            target=str(game.chat_id), details=phase)
            await callback.answer(t("admin.phase_skipped", phase=phase))

        elif route == "roles":
            if not is_owner(user_id):
                await callback.answer(t("admin.owner_only"), show_alert=True)
                return
            lines = [t("admin.roles_header", chat=game.chat_id)]
            for player in game.players.values():
                lines.append(
                    t("admin.roles_line", name=player.full_name,
                      user=player.user_id, role=player.role,
                      state=t("admin.alive") if player.is_alive
                      else t("admin.dead"))
                )
            await audit.log(user_id, "game.reveal_roles",
                            target=str(game.chat_id))
            await _render(
                callback, "\n".join(lines),
                admin_back_kb(t, f"game:{game.chat_id}"),
            )
            return

        elif route == "endgame":
            # Destructive: ask first.
            await _render(
                callback,
                t("admin.confirm_end", chat=game.chat_id),
                admin_confirm_kb(
                    t, f"confirmend:{game.chat_id}", f"game:{game.chat_id}"
                ),
            )
            return

        else:  # confirmend
            await admin_force_end(
                bot, games, timers, session, game, tracker=tracker
            )
            await audit.log(user_id, "game.force_end",
                            target=str(game.chat_id))
            await callback.answer(t("admin.game_ended", chat=game.chat_id))
            await _menu(callback, session, games, t)
            return

        # Refresh the game card after extend/skip so the phase is current.
        refreshed = games.get(game.chat_id)
        if refreshed is None:
            await _menu(callback, session, games, t)
            return
        await _render(
            callback,
            t("admin.game_card", chat=refreshed.chat_id,
              phase=refreshed.phase.value, round=refreshed.round_number,
              alive=len(refreshed.alive_players),
              total=len(refreshed.players)),
            admin_game_kb(refreshed.chat_id, t),
        )

    # --- read-only sections --------------------------------------------
    elif route == "moderation":
        bans = BanRepo(session)
        ban_count = await bans.count_bans()
        chat_rows = await bans.list_chat_bans()
        lines = [
            t("admin.moderation_header"),
            t("admin.moderation_bans", count=ban_count),
            t("admin.moderation_chats", count=len(chat_rows)),
            "",
            t("admin.help.moderation"),
        ]
        await _render(
            callback,
            "\n".join(lines),
            admin_moderation_kb(t, bans=ban_count, chats=len(chat_rows)),
        )

    elif route == "bans":
        # Every banned player gets their own unban button, so moderation
        # never requires remembering a numeric id.
        bans = BanRepo(session)
        total = await bans.count_bans()
        pages = _pages(total)
        page = min(_page_arg(args), pages - 1)
        rows = await bans.list_bans(
            limit=PANEL_LIST_SIZE, offset=page * PANEL_LIST_SIZE
        )
        if not rows:
            await _render(
                callback, t("admin.bans_empty"), admin_back_kb(t, "moderation")
            )
            return
        stats = StatsRepo(session)
        lines = [t("admin.bans_header", total=total)]
        buttons: list[tuple[int, str]] = []
        for row in rows:
            profile = await stats.get(row.user_id)
            name = getattr(profile, "full_name", "") or str(row.user_id)
            lines.append(
                t(
                    "admin.bans_line",
                    name=name,
                    user=row.user_id,
                    reason=row.reason or t("admin.no_reason"),
                    until=row.until.strftime("%d.%m.%Y") if row.until
                    else t("admin.forever"),
                )
            )
            buttons.append((row.user_id, name))
        await _render(
            callback,
            "\n".join(lines),
            admin_bans_kb(buttons, t, page=page, pages=pages),
        )

    elif route == "unban":
        target = int(args[0]) if args and args[0].lstrip("-").isdigit() else 0
        if not await BanRepo(session).unban_user(target):
            await callback.answer(t("admin.not_banned", user=target),
                                  show_alert=True)
            return
        await audit.log(user_id, "user.unban", target=str(target))
        await callback.answer(t("admin.unbanned", user=target))
        callback.data = f"{CallbackAction.ADMIN.value}:bans:0"
        await cb_admin(
            callback, session, games, timers, bot, t, tracker
        )
        return

    elif route == "chatbans":
        rows = await BanRepo(session).list_chat_bans()
        if not rows:
            await _render(
                callback, t("admin.chatbans_empty"),
                admin_back_kb(t, "moderation"),
            )
            return
        lines = [t("admin.chatbans_header", count=len(rows))]
        buttons: list[tuple[int, str]] = []
        for row in rows:
            title = row.title or str(row.chat_id)
            lines.append(
                t(
                    "admin.chatbans_line",
                    title=title,
                    chat=row.chat_id,
                    reason=row.reason or t("admin.no_reason"),
                )
            )
            buttons.append((row.chat_id, title))
        await _render(callback, "\n".join(lines), admin_chatbans_kb(buttons, t))

    elif route == "unbanchat":
        target = int(args[0]) if args and args[0].lstrip("-").isdigit() else 0
        if not await BanRepo(session).unban_chat(target):
            await callback.answer(t("admin.chat_not_banned", chat=target),
                                  show_alert=True)
            return
        await audit.log(user_id, "chat.unban", target=str(target))
        await callback.answer(t("admin.chat_unbanned", chat=target))
        callback.data = f"{CallbackAction.ADMIN.value}:chatbans"
        await cb_admin(callback, session, games, timers, bot, t, tracker)
        return

    elif route == "economy":
        analytics = AnalyticsRepo(session)
        held, spent, purchases = await analytics.economy_summary()
        promos = await PromoRepo(session).list_codes()
        lines = [
            t("admin.economy_header"),
            t("admin.economy_held", value=held),
            t("admin.economy_spent", value=spent),
            t("admin.economy_purchases", value=purchases),
            t("admin.economy_multiplier",
              value=await runtime_config.coin_multiplier(session)),
            t("admin.economy_promos", count=len(promos)),
            "",
            t("admin.help.economy"),
        ]
        await _render(callback, "\n".join(lines), admin_back_kb(t))

    elif route == "analytics":
        analytics = AnalyticsRepo(session)
        started = await analytics.games_since(30)
        finished = await analytics.finished_since(30)
        lines = [
            t("admin.stats_header", days=30),
            t("admin.stats_games", started=started, finished=finished),
            t("admin.stats_live", value=len(games._sessions)),
            t("admin.stats_players",
              value=round(await analytics.avg_players_per_game(30), 1)),
            t("admin.stats_rounds",
              value=round(await analytics.avg_rounds(30), 1)),
            t("admin.stats_active", value=await analytics.active_users(30)),
            t("admin.stats_total", value=await analytics.total_users()),
        ]
        rows = await analytics.role_winrates(30)
        if rows:
            lines.append("")
            lines.append(t("admin.rolestats_header", days=30))
            for role, played, wins in rows:
                lines.append(
                    t("admin.rolestats_line", role=role, played=played,
                      wins=wins, rate=wins * 100 // max(played, 1))
                )
        await _render(callback, "\n".join(lines), admin_back_kb(t))

    elif route == "audit":
        # The journal keeps the last AUDIT_PAGE_TOTAL actions in view and
        # pages through them, so no message ever hits Telegram's limit.
        total = await audit.count(cap=AUDIT_PAGE_TOTAL)
        pages = page_count(total)
        page = int(args[0]) if args and args[0].lstrip("-").isdigit() else 0
        page = max(0, min(page, pages - 1))
        rows = await audit.page(
            limit=AUDIT_PAGE_SIZE, offset=page * AUDIT_PAGE_SIZE
        )
        rows = rows[: max(0, total - page * AUDIT_PAGE_SIZE)]
        if not rows:
            await _render(callback, t("admin.audit_empty"), admin_back_kb(t))
            return
        names = await _admin_names(session, admin_ids_in(rows))
        await _render(
            callback,
            render_page(rows, t, page=page, total=total, names=names),
            admin_pager_kb(t, route="audit", page=page, pages=pages),
        )

    elif route == "admins":
        if not is_owner(user_id):
            await callback.answer(t("admin.owner_only"), show_alert=True)
            return
        await _render(
            callback, t("admin.help.admins"), admin_back_kb(t)
        )

    elif route == "help":
        await _render(
            callback,
            "\n\n".join(
                t(key) for key in (
                    "admin.help.games",
                    "admin.help.system",
                )
            ),
            admin_back_kb(t),
        )

    # --- system ---------------------------------------------------------
    elif route == "system":
        flags = {
            KEY_MAINTENANCE: await runtime_config.is_maintenance(session)
        }
        for key, default in FEATURE_FLAGS.items():
            flags[key] = await runtime_config.get_bool(session, key, default)
        await _render(
            callback, t("admin.system_header"), admin_flags_kb(flags, t)
        )

    elif route == "toggle":
        key = args[0] if args else ""
        if key != KEY_MAINTENANCE and key not in FEATURE_FLAGS:
            await callback.answer(
                t("admin.flag_unknown",
                  keys=", ".join([KEY_MAINTENANCE, *FEATURE_FLAGS])),
                show_alert=True,
            )
            return
        default = False if key == KEY_MAINTENANCE else FEATURE_FLAGS[key]
        value = await runtime_config.toggle(
            session, key, default=default, admin_id=user_id
        )
        state = "on" if value else "off"
        if key == KEY_MAINTENANCE:
            await audit.log(user_id, "system.maintenance", details=state)
            # Tell the tables what is going on instead of just refusing
            # new lobbies silently.
            notice = (
                "admin.maintenance_notice_on" if value
                else "admin.maintenance_notice_off"
            )
            for live in list(games._sessions.values()):
                try:
                    await bot.send_message(live.chat_id, t(notice))
                except TelegramAPIError:
                    logger.debug(
                        "Could not announce maintenance in %s.", live.chat_id
                    )
        else:
            await audit.log(user_id, "system.flag", target=key, details=state)
        await callback.answer(
            t("admin.flag_set",
              key=t(f"admin.flag.{key}") if key != KEY_MAINTENANCE
              else t("admin.flag.maintenance"),
              state=t("admin.on") if value else t("admin.off"))
        )
        flags = {
            KEY_MAINTENANCE: await runtime_config.is_maintenance(session)
        }
        for flag_key, flag_default in FEATURE_FLAGS.items():
            flags[flag_key] = await runtime_config.get_bool(
                session, flag_key, flag_default
            )
        await _render(
            callback, t("admin.system_header"), admin_flags_kb(flags, t)
        )

    elif route == "reload":
        get_i18n.cache_clear()
        manager = get_i18n()
        await audit.log(user_id, "system.reload_locales")
        await callback.answer(
            t("admin.locales_reloaded",
              langs=", ".join(sorted(manager.available_languages))),
            show_alert=True,
        )

    # --- broadcast -------------------------------------------------------
    elif route == "broadcast":
        if args and args[0] == "go":
            text = PENDING_BROADCASTS.pop(user_id, "")
            if not text:
                await callback.answer(t("admin.broadcast_expired"),
                                      show_alert=True)
                return
            audience = await AnalyticsRepo(session).broadcast_audience()
            await _render(
                callback, t("admin.broadcast_running", count=len(audience))
            )
            stats = StatsRepo(session)
            result = await broadcast(
                bot, audience, text, on_blocked=stats.clear_dm
            )
            await audit.log(user_id, "system.broadcast",
                            details=f"sent={result.sent}/{result.total}")
            await _render(
                callback,
                t("admin.broadcast_done", sent=result.sent,
                  total=result.total, blocked=result.blocked,
                  failed=result.failed),
                admin_back_kb(t),
            )
            return
        await _render(
            callback, t("admin.help.broadcast"), admin_back_kb(t)
        )

    else:
        # Unknown or stale route: fall back to the menu instead of dying.
        await _menu(callback, session, games, t)

    # Some routes above already acknowledged the tap. Answering a query
    # twice makes Telegram raise, the exception escapes the handler and
    # the DB session gets rolled back - which used to silently discard
    # settings changes such as the maintenance switch. Swallow it.
    try:
        await callback.answer()
    except TelegramBadRequest:
        logger.debug("Callback %s was already answered.", callback.id)
