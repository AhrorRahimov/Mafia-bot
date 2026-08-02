"""Inline keyboard builders for lobby, voting, night actions, language.

Callback payload format is ``<action>:<arg>`` where ``arg`` is the
``game_id`` for lobby buttons, the target ``user_id`` for night/vote
buttons, or the language code for language selection. Decoding is
centralised in ``app.keyboards.callbacks``.
"""
from __future__ import annotations

from typing import Iterable, Optional

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.game.constants import MAX_INLINE_BUTTON_LABEL, SKIP_VOTE_ID
from app.game.enums import MAFIA_SIDE_ROLES
from app.game.settings import GameSettings
from app.i18n import Translator
from app.keyboards.callbacks import CallbackAction
from app.services.session import GameSession, PlayerState


# --- Lobby -------------------------------------------------------------

def lobby_kb(game_id: int, t: Translator) -> InlineKeyboardMarkup:
    """Join / Leave / Start buttons for an open lobby."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("button.join"),
        callback_data=f"{CallbackAction.JOIN}:{game_id}",
    )
    builder.button(
        text=t("button.leave"),
        callback_data=f"{CallbackAction.LEAVE}:{game_id}",
    )
    builder.button(
        text=t("button.start"),
        callback_data=f"{CallbackAction.START}:{game_id}",
    )
    builder.button(
        text=t("button.settings"),
        callback_data=f"{CallbackAction.SETTINGS}:open",
    )
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def rematch_kb(t: Translator) -> InlineKeyboardMarkup:
    """Single "play again" button shown with the game-over summary."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("button.rematch"),
        callback_data=f"{CallbackAction.REMATCH}:0",
    )
    builder.adjust(1)
    return builder.as_markup()


# --- Lobby settings menu ----------------------------------------------

def settings_kb(settings: GameSettings, t: Translator) -> InlineKeyboardMarkup:
    """Creator-only menu to tune timings, roles and game modes.

    Timing buttons cycle through presets on each tap; role and mode
    buttons toggle. Callback args are string keywords, parsed manually
    in the handler.
    """
    def on_off(enabled: bool) -> str:
        return "\u2705" if enabled else "\u274c"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("settings.night", seconds=settings.night_duration),
        callback_data=f"{CallbackAction.SETTINGS}:night",
    )
    builder.button(
        text=t("settings.discussion", seconds=settings.discussion_duration),
        callback_data=f"{CallbackAction.SETTINGS}:disc",
    )
    builder.button(
        text=t("settings.vote", seconds=settings.vote_duration),
        callback_data=f"{CallbackAction.SETTINGS}:vote",
    )
    # --- Optional roles ---
    builder.button(
        text=t("settings.don", state=on_off(settings.enable_don)),
        callback_data=f"{CallbackAction.SETTINGS}:don",
    )
    builder.button(
        text=t("settings.whore", state=on_off(settings.enable_whore)),
        callback_data=f"{CallbackAction.SETTINGS}:whore",
    )
    builder.button(
        text=t("settings.sergeant", state=on_off(settings.enable_sergeant)),
        callback_data=f"{CallbackAction.SETTINGS}:sergeant",
    )
    builder.button(
        text=t("settings.maniac", state=on_off(settings.enable_maniac)),
        callback_data=f"{CallbackAction.SETTINGS}:maniac",
    )
    builder.button(
        text=t("settings.lawyer", state=on_off(settings.enable_lawyer)),
        callback_data=f"{CallbackAction.SETTINGS}:lawyer",
    )
    builder.button(
        text=t(
            "settings.detective_shoot",
            state=on_off(settings.detective_can_shoot),
        ),
        callback_data=f"{CallbackAction.SETTINGS}:shoot",
    )
    # --- Composition ---
    mafia_label = (
        str(settings.mafia_count) if settings.mafia_count is not None
        else t("settings.mafia_count_auto")
    )
    builder.button(
        text=t("settings.mafia_count", value=mafia_label),
        callback_data=f"{CallbackAction.SETTINGS}:mafia_count",
    )
    # --- Game modes ---
    builder.button(
        text=t("settings.reveal_roles", state=on_off(settings.reveal_roles)),
        callback_data=f"{CallbackAction.SETTINGS}:reveal",
    )
    builder.button(
        text=t("settings.nomination", state=on_off(settings.nomination_mode)),
        callback_data=f"{CallbackAction.SETTINGS}:nomination",
    )
    builder.button(
        text=t("settings.skip_vote", state=on_off(settings.allow_skip_vote)),
        callback_data=f"{CallbackAction.SETTINGS}:skip",
    )
    builder.button(
        text=t("settings.dead_chat", state=on_off(settings.dead_chat)),
        callback_data=f"{CallbackAction.SETTINGS}:dead_chat",
    )
    builder.button(
        text=t("settings.afk", state=on_off(settings.afk_autoskip)),
        callback_data=f"{CallbackAction.SETTINGS}:afk",
    )
    builder.button(
        text=t("settings.close"),
        callback_data=f"{CallbackAction.SETTINGS}:close",
    )
    builder.adjust(1)
    return builder.as_markup()


# --- Language ---------------------------------------------------------

def language_kb() -> InlineKeyboardMarkup:
    """Static language picker. Labels are intentionally multilingual
    so the user can recognise their language regardless of the current
    interface language."""
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001F1F7\U0001F1FA \u0420\u0443\u0441\u0441\u043a\u0438\u0439", callback_data=f"{CallbackAction.SET_LANG}:ru")
    builder.button(text="\U0001F1EC\U0001F1E7 English", callback_data=f"{CallbackAction.SET_LANG}:en")
    builder.button(text="\U0001F1FA\U0001F1FF O'zbekcha", callback_data=f"{CallbackAction.SET_LANG}:uz")
    builder.adjust(1)
    return builder.as_markup()


# --- Night actions -----------------------------------------------------

def _safe_label(player: PlayerState) -> str:
    """Trim long names so the inline button stays under Telegram's limit."""
    name = player.full_name or f"User {player.user_id}"
    return name[:60]


def _targets_kb(
    action: CallbackAction, candidates: Iterable[PlayerState]
) -> InlineKeyboardMarkup:
    """Vertical list of alive targets for a night action."""
    builder = InlineKeyboardBuilder()
    for player in candidates:
        builder.button(
            text=_safe_label(player),
            callback_data=f"{action.value}:{player.user_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def mafia_targets_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """Mafia can target any alive player outside the family.

    ``actor_id`` is accepted for API symmetry with the other night-action
    keyboards but not used: mafia members are all on the same team.
    """
    _ = actor_id  # explicit no-op for clarity
    targets = [
        p for p in session.alive_players if p.role not in MAFIA_SIDE_ROLES
    ]
    return _targets_kb(CallbackAction.MAFIA_KILL, targets)


def don_search_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """The don may search anyone outside the family for the detective."""
    targets = [
        p
        for p in session.alive_players
        if p.user_id != actor_id and p.role not in MAFIA_SIDE_ROLES
    ]
    return _targets_kb(CallbackAction.DON_SEARCH, targets)


def lawyer_targets_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """The lawyer defends any alive player except himself.

    He does not know the family, so mafia members stay on the list.
    """
    targets = [p for p in session.alive_players if p.user_id != actor_id]
    return _targets_kb(CallbackAction.LAWYER_DEFEND, targets)


def maniac_targets_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """The maniac may kill anyone but himself - mafia included."""
    targets = [p for p in session.alive_players if p.user_id != actor_id]
    return _targets_kb(CallbackAction.MANIAC_KILL, targets)


def detective_targets_kb(
    session: GameSession, actor_id: int
) -> InlineKeyboardMarkup:
    """Detective can check any alive player except themselves."""
    targets = [p for p in session.alive_players if p.user_id != actor_id]
    return _targets_kb(CallbackAction.DETECTIVE_CHECK, targets)


def detective_shoot_kb(
    session: GameSession, actor_id: int
) -> InlineKeyboardMarkup:
    """Detective's bullet: any alive player except themselves.

    Shooting and checking are mutually exclusive - whichever button the
    detective presses first consumes his night.
    """
    targets = [p for p in session.alive_players if p.user_id != actor_id]
    return _targets_kb(CallbackAction.DETECTIVE_SHOOT, targets)


def doctor_targets_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """Doctor can heal any alive player except the one healed last night.

    The doctor also drops off the list once the one-time self-heal has
    already been used this game.
    """
    targets = [
        p
        for p in session.alive_players
        if p.user_id != session.last_healed
        and not (p.user_id == actor_id and session.doctor_self_heal_used)
    ]
    return _targets_kb(CallbackAction.DOCTOR_HEAL, targets)


def whore_targets_kb(session: GameSession, actor_id: int) -> InlineKeyboardMarkup:
    """Whore can block any alive player except themselves and the one
    blocked last night."""
    targets = [
        p
        for p in session.alive_players
        if p.user_id != actor_id and p.user_id != session.last_blocked
    ]
    return _targets_kb(CallbackAction.WHORE_BLOCK, targets)


# --- Day nomination + vote ---------------------------------------------

def nominate_kb(session: GameSession, voter_id: int) -> InlineKeyboardMarkup:
    """Nomination targets: every alive player except the nominator."""
    targets = [p for p in session.alive_players if p.user_id != voter_id]
    return _targets_kb(CallbackAction.NOMINATE, targets)


def vote_kb(
    session: GameSession,
    voter_id: int,
    candidates: Optional[Iterable[PlayerState]] = None,
) -> InlineKeyboardMarkup:
    """Vote targets plus an optional "skip" ballot.

    ``candidates`` restricts the list (nomination mode); when omitted every
    alive player except the voter is offered.
    """
    pool = list(candidates) if candidates is not None else list(session.alive_players)
    targets = [p for p in pool if p.user_id != voter_id]

    builder = InlineKeyboardBuilder()
    for player in targets:
        builder.button(
            text=_safe_label(player),
            callback_data=f"{CallbackAction.VOTE.value}:{player.user_id}",
        )
    if session.settings.allow_skip_vote:
        builder.button(
            text="\u27a1\ufe0f \u041f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u0442\u044c / Skip",
            callback_data=f"{CallbackAction.VOTE.value}:{SKIP_VOTE_ID}",
        )
    builder.adjust(1)
    return builder.as_markup()


# --- Shop --------------------------------------------------------------

def inventory_kb(entries, t: Translator, *, has_claim: bool) -> InlineKeyboardMarkup:
    """"Use" button per role card, plus a cancel button for an active claim.

    ``entries`` is a sequence of ``(index, item, quantity)`` tuples, where
    ``index`` is the item's position in ``SHOP_ITEMS`` - the same payload
    the shop uses, so the callback data stays short.
    """
    builder = InlineKeyboardBuilder()
    for index, item, quantity in entries:
        label = t(
            "inv.button_use",
            emoji=item.emoji,
            name=t(f"shop.item.{item.item_id}.name"),
            qty=quantity,
        )
        builder.button(
            text=label[:MAX_INLINE_BUTTON_LABEL],
            callback_data=f"{CallbackAction.INV_USE}:{index}",
        )
    if has_claim:
        builder.button(
            text=t("inv.button_cancel")[:MAX_INLINE_BUTTON_LABEL],
            callback_data=f"{CallbackAction.INV_CANCEL}:0",
        )
    builder.adjust(1)
    return builder.as_markup()


def top_kb(t: Translator, boards, active: str) -> InlineKeyboardMarkup:
    """One button per leaderboard; the active board is marked."""
    builder = InlineKeyboardBuilder()
    for board in boards:
        label = t(f"top.board.{board}")
        if board == active:
            label = t("top.board_active", name=label)
        builder.button(
            text=label[:MAX_INLINE_BUTTON_LABEL],
            callback_data=f"{CallbackAction.TOP.value}:{board}",
        )
    builder.adjust(2)
    return builder.as_markup()


def shop_kb(
    items,
    owned: set[str],
    t: Translator,
    *,
    cards_enabled: bool = True,
    category: str = "cos",
    page: int = 0,
    pages: int = 1,
) -> InlineKeyboardMarkup:
    """Buy buttons for one page of one category, plus the navigation rows.

    ``items`` is a list of ``(index, item)`` pairs where ``index`` is the
    position in ``SHOP_ITEMS`` - that keeps the callback payload short.
    Below the goods we render a tab row (cosmetics / role cards) and, when
    the category spans several pages, a prev/next row. Cosmetics already
    owned are shown as such and rejected server-side too; role cards are
    consumable, so they stay buyable unless an admin disabled the feature.
    """
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    for index, item in items:
        if getattr(item, "is_role_card", False):
            label = t(
                "shop.button_card",
                emoji=item.emoji,
                name=t(f"shop.item.{item.item_id}.name"),
                price=item.price,
            )
        elif item.item_id in owned:
            label = t(
                "shop.button_owned",
                emoji=item.emoji,
                name=t(f"shop.item.{item.item_id}.name"),
            )
        else:
            label = t(
                "shop.button_buy",
                emoji=item.emoji,
                name=t(f"shop.item.{item.item_id}.name"),
                price=item.price,
            )
        builder.button(
            text=label[:MAX_INLINE_BUTTON_LABEL],
            callback_data=f"{CallbackAction.SHOP_BUY.value}:{index}",
        )
        rows.append(1)

    # Pager: only when the current tab actually has more than one page.
    if pages > 1:
        pager = 0
        if page > 0:
            builder.button(
                text=t("shop.btn_prev")[:MAX_INLINE_BUTTON_LABEL],
                callback_data=(
                    f"{CallbackAction.SHOP_PAGE.value}:{category}:{page - 1}"
                ),
            )
            pager += 1
        builder.button(
            text=t("shop.btn_page", page=page + 1, pages=pages)[
                :MAX_INLINE_BUTTON_LABEL
            ],
            callback_data=f"{CallbackAction.SHOP_PAGE.value}:{category}:{page}",
        )
        pager += 1
        if page + 1 < pages:
            builder.button(
                text=t("shop.btn_next")[:MAX_INLINE_BUTTON_LABEL],
                callback_data=(
                    f"{CallbackAction.SHOP_PAGE.value}:{category}:{page + 1}"
                ),
            )
            pager += 1
        rows.append(pager)

    # Tabs. The active one is marked, and the cards tab disappears entirely
    # when the feature flag is off.
    tabs = 0
    cos_label = t("shop.tab_cosmetics")
    builder.button(
        text=(
            t("shop.tab_active", name=cos_label) if category == "cos"
            else cos_label
        )[:MAX_INLINE_BUTTON_LABEL],
        callback_data=f"{CallbackAction.SHOP_PAGE.value}:cos:0",
    )
    tabs += 1
    if cards_enabled:
        card_label = t("shop.tab_cards")
        builder.button(
            text=(
                t("shop.tab_active", name=card_label) if category == "card"
                else card_label
            )[:MAX_INLINE_BUTTON_LABEL],
            callback_data=f"{CallbackAction.SHOP_PAGE.value}:card:0",
        )
        tabs += 1
    rows.append(tabs)

    builder.adjust(*rows)
    return builder.as_markup()


def _on_off(enabled: bool) -> str:
    """Compact state marker used on toggle buttons."""
    return "\U00002705" if enabled else "\U0000274C"


def _adm(route: str) -> str:
    """Build an admin callback payload."""
    return f"{CallbackAction.ADMIN.value}:{route}"


def admin_menu_kb(t: Translator, *, is_owner: bool) -> InlineKeyboardMarkup:
    """Root admin menu. Owner-only sections are hidden from moderators."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn.games"), callback_data=_adm("games"))
    builder.button(text=t("admin.btn.moderation"), callback_data=_adm("moderation"))
    builder.button(text=t("admin.btn.economy"), callback_data=_adm("economy"))
    builder.button(text=t("admin.btn.analytics"), callback_data=_adm("analytics"))
    builder.button(text=t("admin.btn.broadcast"), callback_data=_adm("broadcast"))
    builder.button(text=t("admin.btn.system"), callback_data=_adm("system"))
    if is_owner:
        builder.button(text=t("admin.btn.admins"), callback_data=_adm("admins"))
    builder.button(text=t("admin.btn.audit"), callback_data=_adm("audit"))
    builder.button(text=t("admin.btn.help"), callback_data=_adm("help"))
    builder.adjust(2)
    return builder.as_markup()


def admin_back_kb(t: Translator, route: str = "menu") -> InlineKeyboardMarkup:
    """A single "back" button pointing at ``route``."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn.back"), callback_data=_adm(route))
    return builder.as_markup()


def admin_games_kb(sessions, t: Translator) -> InlineKeyboardMarkup:
    """One row per live game: open its control card."""
    builder = InlineKeyboardBuilder()
    for game in sessions:
        label = t(
            "admin.btn.game_row",
            chat=game.chat_id,
            phase=game.phase.value,
            alive=len(game.alive_players),
        )
        builder.button(
            text=label[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"game:{game.chat_id}"),
        )
    builder.button(text=t("admin.btn.refresh"), callback_data=_adm("games"))
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(1)
    return builder.as_markup()


def admin_game_kb(chat_id: int, t: Translator) -> InlineKeyboardMarkup:
    """Controls for one running game."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("admin.btn.extend"), callback_data=_adm(f"extend:{chat_id}")
    )
    builder.button(
        text=t("admin.btn.skip_phase"), callback_data=_adm(f"skip:{chat_id}")
    )
    builder.button(
        text=t("admin.btn.roles"), callback_data=_adm(f"roles:{chat_id}")
    )
    builder.button(
        text=t("admin.btn.force_end"), callback_data=_adm(f"endgame:{chat_id}")
    )
    builder.button(text=t("admin.btn.back"), callback_data=_adm("games"))
    builder.adjust(2)
    return builder.as_markup()


def admin_flags_kb(flags: dict, t: Translator) -> InlineKeyboardMarkup:
    """Toggle buttons for maintenance mode and feature flags."""
    builder = InlineKeyboardBuilder()
    for key, enabled in flags.items():
        builder.button(
            text=t(
                "admin.btn.flag",
                name=t(f"admin.flag.{key}"),
                state=_on_off(enabled),
            )[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"toggle:{key}"),
        )
    builder.button(
        text=t("admin.btn.reload_locales"), callback_data=_adm("reload")
    )
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(1)
    return builder.as_markup()


def admin_section_kb(
    t: Translator, actions: list[tuple[str, str]]
) -> InlineKeyboardMarkup:
    """Generic section keyboard: ``[(label_key, route), ...]`` + back."""
    builder = InlineKeyboardBuilder()
    for label_key, route in actions:
        builder.button(text=t(label_key), callback_data=_adm(route))
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(1)
    return builder.as_markup()


def admin_pager_kb(
    t: Translator,
    *,
    route: str,
    page: int,
    pages: int,
    back: str = "menu",
) -> InlineKeyboardMarkup:
    """‹ Prev / page counter / Next › plus a back button.

    ``route`` is the panel route that renders the list; the page index is
    appended as its argument (e.g. ``audit:2``).
    """
    builder = InlineKeyboardBuilder()
    if pages > 1:
        previous = (page - 1) % pages
        following = (page + 1) % pages
        builder.button(
            text=t("admin.btn.prev")[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"{route}:{previous}"),
        )
        builder.button(
            text=t("admin.btn.page", page=page + 1, pages=pages)[
                :MAX_INLINE_BUTTON_LABEL
            ],
            callback_data=_adm(f"{route}:{page}"),
        )
        builder.button(
            text=t("admin.btn.next")[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"{route}:{following}"),
        )
    builder.button(
        text=t("admin.btn.back")[:MAX_INLINE_BUTTON_LABEL],
        callback_data=_adm(back),
    )
    builder.adjust(3, 1) if pages > 1 else builder.adjust(1)
    return builder.as_markup()


def admin_moderation_kb(
    t: Translator, *, bans: int, chats: int
) -> InlineKeyboardMarkup:
    """Moderation hub: open the ban lists instead of typing commands."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("admin.btn.bans", count=bans)[:MAX_INLINE_BUTTON_LABEL],
        callback_data=_adm("bans:0"),
    )
    builder.button(
        text=t("admin.btn.chatbans", count=chats)[:MAX_INLINE_BUTTON_LABEL],
        callback_data=_adm("chatbans"),
    )
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(2, 1)
    return builder.as_markup()


def admin_bans_kb(
    rows, t: Translator, *, page: int, pages: int
) -> InlineKeyboardMarkup:
    """One unban button per banned player, plus a pager.

    ``rows`` is a list of ``(user_id, label)`` pairs already trimmed to the
    current page by the caller.
    """
    builder = InlineKeyboardBuilder()
    layout: list[int] = []
    for user_id, label in rows:
        builder.button(
            text=t("admin.btn.unban", name=label)[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"unban:{user_id}"),
        )
        layout.append(1)
    if pages > 1:
        builder.button(
            text=t("admin.btn.prev"),
            callback_data=_adm(f"bans:{(page - 1) % pages}"),
        )
        builder.button(
            text=t("admin.btn.page", page=page + 1, pages=pages),
            callback_data=_adm(f"bans:{page}"),
        )
        builder.button(
            text=t("admin.btn.next"),
            callback_data=_adm(f"bans:{(page + 1) % pages}"),
        )
        layout.append(3)
    builder.button(
        text=t("admin.btn.back"), callback_data=_adm("moderation")
    )
    layout.append(1)
    builder.adjust(*layout)
    return builder.as_markup()


def admin_chatbans_kb(rows, t: Translator) -> InlineKeyboardMarkup:
    """Unbutton per blacklisted chat: ``rows`` is ``(chat_id, label)``."""
    builder = InlineKeyboardBuilder()
    for chat_id, label in rows:
        builder.button(
            text=t("admin.btn.unban", name=label)[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"unbanchat:{chat_id}"),
        )
    builder.button(
        text=t("admin.btn.back"), callback_data=_adm("moderation")
    )
    builder.adjust(1)
    return builder.as_markup()


# Multiplier presets offered as buttons; "1" is the normal payout.
MULTIPLIER_PRESETS = ("0.5", "1", "1.5", "2", "3")


def admin_economy_kb(t: Translator, current: float) -> InlineKeyboardMarkup:
    """Coin multiplier presets + a shortcut into the promo code list."""
    builder = InlineKeyboardBuilder()
    for value in MULTIPLIER_PRESETS:
        active = abs(float(value) - current) < 1e-9
        label = t("admin.btn.multiplier", value=value)
        builder.button(
            text=(t("admin.btn.active", name=label) if active else label)[
                :MAX_INLINE_BUTTON_LABEL
            ],
            callback_data=_adm(f"mult:{value}"),
        )
    builder.button(
        text=t("admin.btn.promos"), callback_data=_adm("promos:0")
    )
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(len(MULTIPLIER_PRESETS), 1, 1)
    return builder.as_markup()


def admin_promos_kb(
    rows, t: Translator, *, page: int, pages: int
) -> InlineKeyboardMarkup:
    """A delete button per promo code, plus a pager. ``rows`` are codes."""
    builder = InlineKeyboardBuilder()
    layout: list[int] = []
    for code in rows:
        builder.button(
            text=t("admin.btn.promo_del", code=code)[
                :MAX_INLINE_BUTTON_LABEL
            ],
            callback_data=_adm(f"promodel:{code}"),
        )
        layout.append(1)
    if pages > 1:
        builder.button(
            text=t("admin.btn.prev"),
            callback_data=_adm(f"promos:{(page - 1) % pages}"),
        )
        builder.button(
            text=t("admin.btn.page", page=page + 1, pages=pages),
            callback_data=_adm(f"promos:{page}"),
        )
        builder.button(
            text=t("admin.btn.next"),
            callback_data=_adm(f"promos:{(page + 1) % pages}"),
        )
        layout.append(3)
    builder.button(text=t("admin.btn.back"), callback_data=_adm("economy"))
    layout.append(1)
    builder.adjust(*layout)
    return builder.as_markup()


def admin_admins_kb(rows, t: Translator) -> InlineKeyboardMarkup:
    """Revoke button per runtime admin: ``rows`` is ``(user_id, name)``."""
    builder = InlineKeyboardBuilder()
    for user_id, name in rows:
        builder.button(
            text=t("admin.btn.revoke", name=name)[:MAX_INLINE_BUTTON_LABEL],
            callback_data=_adm(f"revoke:{user_id}"),
        )
    builder.button(text=t("admin.btn.back"), callback_data=_adm("menu"))
    builder.adjust(1)
    return builder.as_markup()


def admin_confirm_kb(
    t: Translator, confirm_route: str, cancel_route: str = "menu"
) -> InlineKeyboardMarkup:
    """Yes/no confirmation for destructive actions (broadcast, force end)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("admin.btn.confirm"), callback_data=_adm(confirm_route)
    )
    builder.button(
        text=t("admin.btn.cancel"), callback_data=_adm(cancel_route)
    )
    builder.adjust(2)
    return builder.as_markup()
