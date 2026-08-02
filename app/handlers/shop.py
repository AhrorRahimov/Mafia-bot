"""Shop, wallet and inventory: ``/shop``, ``/balance``, ``/inventory``.

Coins are earned automatically at the end of every game (see
``app.services.orchestrator.end_game``) and spent here.

The catalogue holds two kinds of goods:

* cosmetics - bought once, kept forever (``user_purchases``);
* role cards - consumable tickets that reserve a role for the next game.
  They stack in ``user_inventory`` and are activated from ``/inventory``.

Both the buy and the use callbacks carry the item's INDEX in
``SHOP_ITEMS`` rather than its id, which keeps the payload well inside
Telegram's 64-byte limit. A stale keyboard resolves to ``None`` and is
answered with a friendly error instead of crashing.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.inventory_repo import InventoryRepo, RoleClaimRepo
from app.db.repo import ShopRepo, StatsRepo
from app.game.shop import SHOP_ITEMS, get_item, get_item_by_index
from app.i18n import Translator
from app.keyboards.callbacks import (
    CallbackAction,
    parse_callback,
    parse_shop_page,
)
from app.keyboards.inline import inventory_kb, shop_kb
from app.services.admin import (
    KEY_FEATURE_ROLE_CARDS,
    KEY_FEATURE_SHOP,
    runtime_config,
)

logger = logging.getLogger(__name__)
router = Router(name="shop")


SHOP_PAGE_SIZE = 5
CATEGORY_COSMETICS = "cos"
CATEGORY_CARDS = "card"


def _category_items(category: str):
    """``(index, item)`` pairs of one category, keeping catalogue order.

    The index is the position in ``SHOP_ITEMS`` because that is what the
    buy callback carries.
    """
    want_cards = category == CATEGORY_CARDS
    return [
        (index, item)
        for index, item in enumerate(SHOP_ITEMS)
        if bool(getattr(item, "is_role_card", False)) is want_cards
    ]


def _item_page(category: str, item_id: str) -> int:
    """Page of ``item_id`` inside its own tab (0 when not found).

    Used to re-render the very page the buyer was looking at instead of
    throwing them back to the first one.
    """
    for position, (_, item) in enumerate(_category_items(category)):
        if item.item_id == item_id:
            return position // SHOP_PAGE_SIZE
    return 0


def _page_count(total: int, size: int = SHOP_PAGE_SIZE) -> int:
    """Number of pages, never less than one (an empty tab still renders)."""
    if total <= 0:
        return 1
    return (total + size - 1) // size


def _catalogue_text(
    t: Translator,
    balance: int,
    owned: set[str],
    inventory: dict[str, int],
    entries,
    *,
    category: str,
    page: int,
    pages: int,
) -> str:
    """Render one page of one category: header, goods, footer hint."""
    lines = [t("shop.header", balance=balance), ""]
    lines.append(
        t("shop.cards_header") if category == CATEGORY_CARDS
        else t("shop.cosmetics_header")
    )
    lines.append("")
    if not entries:
        lines.append(t("shop.category_empty"))
    for _, item in entries:
        name = t(f"shop.item.{item.item_id}.name")
        desc = t(f"shop.item.{item.item_id}.desc")
        if item.is_role_card:
            lines.append(
                t(
                    "shop.card_line",
                    emoji=item.emoji,
                    name=name,
                    desc=desc,
                    price=item.price,
                    qty=inventory.get(item.item_id, 0),
                )
            )
        elif item.item_id in owned:
            lines.append(
                t("shop.line_owned", emoji=item.emoji, name=name, desc=desc)
            )
        else:
            lines.append(
                t(
                    "shop.line",
                    emoji=item.emoji,
                    name=name,
                    desc=desc,
                    price=item.price,
                )
            )
    lines.append("")
    if pages > 1:
        lines.append(t("shop.page_footer", page=page + 1, pages=pages))
    lines.append(t("shop.hint"))
    return "\n".join(lines)


async def _shop_state(session: AsyncSession, user_id: int):
    """Balance, cosmetics and card quantities in one place."""
    balance = await StatsRepo(session).get_coins(user_id)
    owned = await ShopRepo(session).owned_items(user_id)
    inventory = await InventoryRepo(session).items(user_id)
    return balance, owned, inventory


async def _render_shop(
    session: AsyncSession, t: Translator, user_id: int, category: str, page: int
):
    """Build the (text, keyboard) pair for one shop page.

    Returns ``None`` when role cards are disabled and the cards tab was
    requested, so callers can fall back to the cosmetics tab instead of
    showing an empty screen.
    """
    cards_on = await runtime_config.feature_enabled(
        session, KEY_FEATURE_ROLE_CARDS
    )
    if category == CATEGORY_CARDS and not cards_on:
        category = CATEGORY_COSMETICS
    balance, owned, inventory = await _shop_state(session, user_id)
    entries = _category_items(category)
    pages = _page_count(len(entries))
    page = min(max(page, 0), pages - 1)
    window = entries[page * SHOP_PAGE_SIZE:(page + 1) * SHOP_PAGE_SIZE]
    text = _catalogue_text(
        t, balance, owned, inventory, window,
        category=category, page=page, pages=pages,
    )
    markup = shop_kb(
        window, owned, t,
        cards_enabled=cards_on,
        category=category,
        page=page,
        pages=pages,
    )
    return text, markup


@router.message(Command("shop"))
async def cmd_shop(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Show the catalogue with a buy button per item."""
    if not await runtime_config.feature_enabled(session, KEY_FEATURE_SHOP):
        await message.answer(t("shop.disabled"))
        return
    text, markup = await _render_shop(
        session, t, message.from_user.id, CATEGORY_COSMETICS, 0
    )
    await message.answer(text, reply_markup=markup)


@router.callback_query(
    lambda c: c.data and c.data.startswith(f"{CallbackAction.SHOP_PAGE}:")
)
async def cb_shop_page(
    query: CallbackQuery,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Switch the catalogue tab or turn a page, editing the same message."""
    if not await runtime_config.feature_enabled(session, KEY_FEATURE_SHOP):
        await query.answer(t("shop.disabled"), show_alert=True)
        return
    category, page = parse_shop_page(query.data)
    text, markup = await _render_shop(
        session, t, query.from_user.id, category, page
    )
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        # Same page tapped twice - Telegram rejects a no-op edit.
        pass
    await query.answer()


@router.message(Command("balance"))
async def cmd_balance(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Show how many coins the user has."""
    balance = await StatsRepo(session).get_coins(message.from_user.id)
    await message.answer(t("shop.balance", balance=balance))


def _inventory_text(
    t: Translator,
    owned: set[str],
    inventory: dict[str, int],
    claim_role: str | None,
) -> str:
    """Cosmetics + role cards + the currently activated card."""
    lines = [t("inv.header")]

    cosmetics = [i for i in SHOP_ITEMS if not i.is_role_card and i.item_id in owned]
    lines.append("")
    lines.append(t("inv.section_cosmetics"))
    if cosmetics:
        for item in cosmetics:
            lines.append(
                t(
                    "inv.line_cosmetic",
                    emoji=item.emoji,
                    name=t(f"shop.item.{item.item_id}.name"),
                )
            )
    else:
        lines.append(t("inv.empty_cosmetics"))

    cards = [
        (item, inventory.get(item.item_id, 0))
        for item in SHOP_ITEMS
        if item.is_role_card and inventory.get(item.item_id, 0) > 0
    ]
    lines.append("")
    lines.append(t("inv.section_cards"))
    if cards:
        for item, quantity in cards:
            lines.append(
                t(
                    "inv.line_card",
                    emoji=item.emoji,
                    name=t(f"shop.item.{item.item_id}.name"),
                    qty=quantity,
                )
            )
    else:
        lines.append(t("inv.empty_cards"))

    lines.append("")
    if claim_role:
        lines.append(t("inv.active_claim", role=t(f"role.{claim_role}.title")))
    else:
        lines.append(t("inv.no_claim"))
    lines.append(t("inv.hint"))
    return "\n".join(lines)


def _card_entries(inventory: dict[str, int]):
    """``(index, item, quantity)`` for every role card the player owns."""
    entries = []
    for index, item in enumerate(SHOP_ITEMS):
        if not item.is_role_card:
            continue
        quantity = inventory.get(item.item_id, 0)
        if quantity > 0:
            entries.append((index, item, quantity))
    return entries


@router.message(Command("inventory", "inv"))
async def cmd_inventory(
    message: Message,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Everything the user owns, with buttons to activate role cards."""
    user_id = message.from_user.id
    owned = await ShopRepo(session).owned_items(user_id)
    inventory = await InventoryRepo(session).items(user_id)
    claim = await RoleClaimRepo(session).active(user_id)
    if not owned and not inventory and claim is None:
        await message.answer(t("shop.inventory_empty"))
        return
    await message.answer(
        _inventory_text(t, owned, inventory, claim.role if claim else None),
        reply_markup=inventory_kb(
            _card_entries(inventory), t, has_claim=claim is not None
        ),
    )


@router.callback_query(
    lambda c: c.data and c.data.startswith(f"{CallbackAction.SHOP_BUY}:")
)
async def cb_shop_buy(
    query: CallbackQuery,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Buy an item: validate, debit the coins, then deliver it.

    The order matters - we only deliver after ``spend_coins`` returned
    True, so a user who cannot afford the item never ends up owning it.
    Cosmetics are one-per-account; role cards stack.
    """
    try:
        _, index = parse_callback(query.data)
    except ValueError:
        await query.answer(t("shop.item_unknown"), show_alert=True)
        return

    item = get_item_by_index(index)
    if item is None:
        await query.answer(t("shop.item_unknown"), show_alert=True)
        return

    user_id = query.from_user.id
    shop = ShopRepo(session)
    stats = StatsRepo(session)

    if item.is_role_card and not await runtime_config.feature_enabled(
        session, KEY_FEATURE_ROLE_CARDS
    ):
        await query.answer(t("inv.cards_disabled"), show_alert=True)
        return

    if not item.is_role_card and await shop.has_item(user_id, item.item_id):
        await query.answer(t("shop.already_owned"), show_alert=True)
        return

    if not await stats.spend_coins(user_id, item.price):
        balance = await stats.get_coins(user_id)
        await query.answer(
            t("shop.not_enough", price=item.price, balance=balance),
            show_alert=True,
        )
        return

    if item.is_role_card:
        await InventoryRepo(session).add(user_id, item.item_id, 1)
    else:
        await shop.grant(user_id, item.item_id, item.price)
    await session.commit()

    name = t(f"shop.item.{item.item_id}.name")
    await query.answer(t("shop.bought", name=name), show_alert=True)

    # Re-render the exact tab and page the buyer was on.
    category = CATEGORY_CARDS if item.is_role_card else CATEGORY_COSMETICS
    text, markup = await _render_shop(
        session, t, user_id, category, _item_page(category, item.item_id)
    )
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        logger.debug("Could not refresh the shop card for %s.", user_id)


async def _refresh_inventory(
    query: CallbackQuery, session: AsyncSession, t: Translator
) -> None:
    """Re-render the inventory card after a change."""
    user_id = query.from_user.id
    owned = await ShopRepo(session).owned_items(user_id)
    inventory = await InventoryRepo(session).items(user_id)
    claim = await RoleClaimRepo(session).active(user_id)
    try:
        await query.message.edit_text(
            _inventory_text(t, owned, inventory, claim.role if claim else None),
            reply_markup=inventory_kb(
                _card_entries(inventory), t, has_claim=claim is not None
            ),
        )
    except TelegramAPIError:
        logger.debug("Could not refresh the inventory card for %s.", user_id)


@router.callback_query(
    lambda c: c.data and c.data.startswith(f"{CallbackAction.INV_USE}:")
)
async def cb_inventory_use(
    query: CallbackQuery,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Activate a role card: it leaves the inventory and becomes a claim.

    Only one claim can be active at a time - the previous one is put back
    into the inventory so nothing is ever lost.
    """
    try:
        _, index = parse_callback(query.data)
    except ValueError:
        await query.answer(t("shop.item_unknown"), show_alert=True)
        return

    item = get_item_by_index(index)
    if item is None or not item.is_role_card or not item.role:
        await query.answer(t("shop.item_unknown"), show_alert=True)
        return

    if not await runtime_config.feature_enabled(session, KEY_FEATURE_ROLE_CARDS):
        await query.answer(t("inv.cards_disabled"), show_alert=True)
        return

    user_id = query.from_user.id
    inventory = InventoryRepo(session)
    claims = RoleClaimRepo(session)

    if not await inventory.take(user_id, item.item_id, 1):
        await query.answer(t("inv.card_missing"), show_alert=True)
        return

    previous = await claims.active(user_id)
    if previous is not None:
        await claims.cancel(previous)
        if previous.item_id:
            await inventory.add(user_id, previous.item_id, 1)

    await claims.create(user_id, item.role, item.item_id)
    await session.commit()

    await query.answer(
        t("inv.activated", role=t(f"role.{item.role}.title")), show_alert=True
    )
    await _refresh_inventory(query, session, t)


@router.callback_query(
    lambda c: c.data and c.data.startswith(f"{CallbackAction.INV_CANCEL}:")
)
async def cb_inventory_cancel(
    query: CallbackQuery,
    session: AsyncSession,
    t: Translator,
) -> None:
    """Cancel the active claim and return the card to the inventory."""
    user_id = query.from_user.id
    claims = RoleClaimRepo(session)
    claim = await claims.active(user_id)
    if claim is None:
        await query.answer(t("inv.no_claim_short"), show_alert=True)
        return
    await claims.cancel(claim)
    if claim.item_id and get_item(claim.item_id) is not None:
        await InventoryRepo(session).add(user_id, claim.item_id, 1)
    await session.commit()
    await query.answer(t("inv.cancelled"), show_alert=True)
    await _refresh_inventory(query, session, t)
