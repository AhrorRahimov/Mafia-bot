"""Shop catalogue and the in-game currency rules.

The currency ("coins") is earned by playing: every finished game pays
``COINS_PER_GAME``, the winning side gets ``COINS_PER_WIN`` on top and
survivors get ``COINS_SURVIVOR_BONUS``. Coins are spent here.

Two kinds of goods are sold:

* **Cosmetics** - badges, titles and frames shown next to the player's
  name. Bought once and owned forever (``user_purchases``).
* **Role cards** - consumable tickets that reserve a role for the next
  game. They stack in the player's inventory (``user_inventory``), are
  activated from ``/inventory`` and are burned when the role is dealt.
  A card only works if the lobby actually contains that role and nobody
  activated the same card first; otherwise it is returned untouched.

Item names and descriptions are translated: each item ``x`` resolves
``shop.item.x.name`` and ``shop.item.x.desc`` from the locale files.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Item kinds. ``COSMETIC`` items are owned once; ``ROLE_CARD`` items are
# consumable and stack in the inventory.
KIND_COSMETIC = "cosmetic"
KIND_ROLE_CARD = "role_card"


@dataclass(frozen=True, slots=True)
class ShopItem:
    """A single purchasable item."""

    item_id: str
    price: int
    emoji: str
    kind: str = KIND_COSMETIC
    # For role cards: the role this ticket reserves.
    role: Optional[str] = None

    @property
    def is_role_card(self) -> bool:
        return self.kind == KIND_ROLE_CARD


# Ordered catalogue. The index inside this tuple is what travels in the
# inline-button callback payload, so NEVER reorder it without a migration:
# append new items at the end instead.
SHOP_ITEMS: tuple[ShopItem, ...] = (
    ShopItem("badge_rookie", 100, "\U0001F0CF"),
    ShopItem("badge_detective", 250, "\U0001F575\uFE0F"),
    ShopItem("badge_don", 400, "\U0001F3A9"),
    ShopItem("badge_maniac", 600, "\U0001F52A"),
    ShopItem("title_veteran", 900, "\U0001F3C5"),
    ShopItem("frame_gold", 1500, "\U0001F451"),
    # --- new cosmetics -------------------------------------------------
    ShopItem("badge_doctor", 250, "\U0001F489"),
    ShopItem("badge_sheriff", 350, "\U0001F693"),
    ShopItem("title_legend", 2000, "\U0001F31F"),
    ShopItem("frame_neon", 1200, "\U0001F4A0"),
    ShopItem("pet_crow", 800, "\U0001F426"),
    # --- role cards (consumable) ---------------------------------------
    ShopItem("card_citizen", 150, "\U0001F464", KIND_ROLE_CARD, "citizen"),
    ShopItem("card_mafia", 400, "\U0001F52B", KIND_ROLE_CARD, "mafia"),
    ShopItem("card_detective", 500, "\U0001F575\uFE0F", KIND_ROLE_CARD, "detective"),
    ShopItem("card_doctor", 450, "\U0001F48A", KIND_ROLE_CARD, "doctor"),
    ShopItem("card_don", 700, "\U0001F3A9", KIND_ROLE_CARD, "don"),
    ShopItem("card_whore", 550, "\U0001F484", KIND_ROLE_CARD, "whore"),
    ShopItem("card_sergeant", 500, "\U0001F396\uFE0F", KIND_ROLE_CARD, "sergeant"),
    ShopItem("card_lawyer", 650, "\U0001F4BC", KIND_ROLE_CARD, "lawyer"),
    ShopItem("card_maniac", 900, "\U0001F52A", KIND_ROLE_CARD, "maniac"),
)

# Convenience views over the catalogue.
COSMETIC_ITEMS: tuple[ShopItem, ...] = tuple(
    i for i in SHOP_ITEMS if i.kind == KIND_COSMETIC
)
ROLE_CARDS: tuple[ShopItem, ...] = tuple(
    i for i in SHOP_ITEMS if i.kind == KIND_ROLE_CARD
)
CARD_BY_ROLE: dict[str, ShopItem] = {i.role: i for i in ROLE_CARDS if i.role}


def get_item(item_id: str) -> Optional[ShopItem]:
    """Look an item up by its id, or ``None`` if it does not exist."""
    return next((i for i in SHOP_ITEMS if i.item_id == item_id), None)


def get_item_by_index(index: int) -> Optional[ShopItem]:
    """Resolve the item referenced by an inline-button payload.

    Returns ``None`` for out-of-range indexes so a stale keyboard from an
    older bot version can never crash the handler.
    """
    if 0 <= index < len(SHOP_ITEMS):
        return SHOP_ITEMS[index]
    return None


def role_of_item(item_id: str) -> Optional[str]:
    """Role reserved by a card item, or ``None`` for cosmetics."""
    item = get_item(item_id)
    if item is None or not item.is_role_card:
        return None
    return item.role


def card_for_role(role: str) -> Optional[ShopItem]:
    """The card item that reserves ``role`` (used by /giverole)."""
    return CARD_BY_ROLE.get(str(role))


def is_role_card(item_id: str) -> bool:
    return role_of_item(item_id) is not None
