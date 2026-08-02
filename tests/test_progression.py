"""Tests for the 1.9 progression layer: MMR, achievements, cards, audit.

Everything here is stdlib-only on purpose (no aiogram / SQLAlchemy), so the
suite still runs in a bare environment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.game.achievements import (
    ACHIEVEMENTS,
    ACHIEVEMENTS_BY_CODE,
    SPECIAL_CODES,
    PlayerOutcome,
    evaluate,
)
from app.game.enums import Role, Winner
from app.game.shop import (
    CARD_BY_ROLE,
    COSMETIC_ITEMS,
    ROLE_CARDS,
    SHOP_ITEMS,
    card_for_role,
    get_item,
    get_item_by_index,
    is_role_card,
    role_of_item,
)
from app.keyboards.callbacks import CallbackAction, parse_shop_page
from app.services.audit_view import (
    AUDIT_PAGE_SIZE,
    AUDIT_PAGE_TOTAL,
    action_emoji,
    action_title,
    page_count,
)

LOCALES_DIR = Path(__file__).resolve().parents[1] / "app" / "locales"
LANGUAGES = ("ru", "en", "uz")


def _load(lang: str) -> dict:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _outcome(**kwargs) -> PlayerOutcome:
    base = dict(
        role=Role.CITIZEN,
        winner=Winner.CITY,
        role_games=1,
        role_wins=0,
        won=False,
        survived=False,
        games_played=1,
        wins=0,
        win_streak=0,
        coins=0,
        rounds=1,
        players_total=6,
        kills=0,
        heals_landed=0,
        blocked_actions=0,
        correct_checks=0,
        first_check_found_mafia=False,
        was_lynched=False,
        last_alive_town=False,
    )
    base.update(kwargs)
    return PlayerOutcome(**base)


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------

def test_achievement_codes_are_unique():
    codes = [a.code for a in ACHIEVEMENTS]
    assert len(codes) == len(set(codes))
    assert len(ACHIEVEMENTS_BY_CODE) == len(ACHIEVEMENTS)


def test_every_achievement_has_a_positive_reward():
    assert all(a.reward > 0 for a in ACHIEVEMENTS)


def test_first_game_unlocks_once():
    fresh = {a.code for a in evaluate(_outcome(), unlocked=set())}
    assert "first_game" in fresh
    # Already owned achievements are never handed out twice.
    again = evaluate(_outcome(), unlocked=fresh)
    assert all(a.code not in fresh for a in again)


def test_role_win_achievement():
    fresh = {
        a.code
        for a in evaluate(
            _outcome(role=Role.MANIAC, won=True, wins=1), unlocked=set()
        )
    }
    assert "win_maniac" in fresh
    assert "win_citizen" not in fresh


def test_streak_achievements_are_cumulative():
    fresh = {
        a.code
        for a in evaluate(
            _outcome(won=True, wins=5, win_streak=5, games_played=12),
            unlocked=set(),
        )
    }
    assert {"streak_3", "streak_5", "veteran_10"} <= fresh
    assert "streak_10" not in fresh


def test_special_codes_are_never_auto_granted():
    fresh = {a.code for a in evaluate(_outcome(games_played=500), unlocked=set())}
    assert not (fresh & SPECIAL_CODES)


def test_scapegoat_only_for_town():
    town = {a.code for a in evaluate(_outcome(was_lynched=True), unlocked=set())}
    mafia = {
        a.code
        for a in evaluate(_outcome(role=Role.MAFIA, was_lynched=True), unlocked=set())
    }
    assert "scapegoat" in town
    assert "scapegoat" not in mafia


# ---------------------------------------------------------------------------
# Shop / role cards
# ---------------------------------------------------------------------------

def test_shop_item_ids_are_unique():
    ids = [item.item_id for item in SHOP_ITEMS]
    assert len(ids) == len(set(ids))


def test_catalogue_split_between_cosmetics_and_cards():
    assert len(COSMETIC_ITEMS) + len(ROLE_CARDS) == len(SHOP_ITEMS)
    assert all(item.is_role_card for item in ROLE_CARDS)
    assert not any(item.is_role_card for item in COSMETIC_ITEMS)


def test_every_playable_role_has_a_card():
    for role in Role:
        item = card_for_role(role.value)
        assert item is not None, role
        assert item.role == role.value
        assert CARD_BY_ROLE[role.value] is item


def test_card_lookup_helpers_agree():
    for index, item in enumerate(SHOP_ITEMS):
        assert get_item_by_index(index) is item
        assert get_item(item.item_id) is item
        assert is_role_card(item.item_id) == item.is_role_card
        assert role_of_item(item.item_id) == item.role


def test_prices_are_positive():
    assert all(item.price > 0 for item in SHOP_ITEMS)


# ---------------------------------------------------------------------------
# Audit journal rendering
# ---------------------------------------------------------------------------

def test_audit_page_geometry():
    assert AUDIT_PAGE_TOTAL == 50
    assert AUDIT_PAGE_SIZE > 0
    assert page_count(0, AUDIT_PAGE_SIZE) == 1
    assert page_count(10, 10) == 1
    assert page_count(11, 10) == 2
    assert page_count(50, 10) == 5


def test_action_emoji_has_a_fallback():
    assert action_emoji("user.ban")
    assert action_emoji("totally.unknown")


def test_action_title_falls_back_to_the_raw_code():
    ru = _load("ru")

    def translate(key, **kwargs):
        value = ru.get(key)
        return value.format(**kwargs) if value else key

    assert action_title("user.ban", translate) != "user.ban"
    assert action_title("nope.nope", translate) == "nope.nope"


# ---------------------------------------------------------------------------
# Locale coverage for the new features
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang", LANGUAGES)
def test_achievement_strings_exist(lang):
    data = _load(lang)
    for achievement in ACHIEVEMENTS:
        assert f"achv.{achievement.code}.name" in data
        assert f"achv.{achievement.code}.desc" in data


@pytest.mark.parametrize("lang", LANGUAGES)
def test_shop_strings_exist(lang):
    data = _load(lang)
    for item in SHOP_ITEMS:
        assert f"shop.item.{item.item_id}.name" in data
        assert f"shop.item.{item.item_id}.desc" in data


@pytest.mark.parametrize("lang", LANGUAGES)
def test_leaderboard_and_profile_strings_exist(lang):
    data = _load(lang)
    for board in ("season", "wins", "coins", "winrate", "streak"):
        assert f"top.board.{board}" in data
        assert f"top.header.{board}" in data
        assert f"top.line.{board}" in data
    for key in (
        "profile.header",
        "profile.stats_header",
        "profile.played",
        "profile.wins",
        "profile.losses",
        "profile.winrate",
        "profile.rating_header",
        "profile.mmr",
        "profile.season",
        "profile.rank",
        "profile.streak_header",
        "profile.streak_current",
        "profile.streak_best",
        "profile.coins",
        "profile.roles_header",
        "profile.achievements",
        "inv.header",
        "inv.section_cards",
        "inv.activated",
        "season.reward",
        "rating.changed",
        "achv.unlocked",
        "dead.chat_disabled",
        "admin.list_footer",
        "admin.audit_title",
        "admin.giverole_usage",
    ):
        assert key in data, key


def test_locales_stay_in_sync():
    sizes = {lang: set(_load(lang)) for lang in LANGUAGES}
    reference = sizes["ru"]
    for lang, keys in sizes.items():
        assert keys == reference, sorted(reference ^ keys)[:5]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_help_lists_the_player_commands(lang):
    text = _load(lang)["help.text"]
    for command in ("/me", "/top", "/shop", "/inventory", "/balance", "/promo",
                    "/settings", "/admin"):
        assert command in text, command


# ---------------------------------------------------------------------------
# Shop tabs and pagination
# ---------------------------------------------------------------------------

def test_shop_page_payload_roundtrip():
    for category in ("cos", "card"):
        for page in (0, 1, 7):
            data = f"{CallbackAction.SHOP_PAGE.value}:{category}:{page}"
            assert len(data.encode()) <= 64
            assert parse_shop_page(data) == (category, page)


def test_shop_page_payload_is_forgiving():
    assert parse_shop_page("shop_pg:junk:0") == ("cos", 0)
    assert parse_shop_page("shop_pg:card:oops") == ("card", 0)
    assert parse_shop_page("garbage") == ("cos", 0)
    assert parse_shop_page("shop_pg:card:-3") == ("card", 0)


def test_every_catalogue_item_lands_in_exactly_one_tab():
    cosmetics = [i for i in SHOP_ITEMS if not i.is_role_card]
    cards = [i for i in SHOP_ITEMS if i.is_role_card]
    assert cosmetics and cards
    assert len(cosmetics) + len(cards) == len(SHOP_ITEMS)


def test_tab_pages_cover_the_whole_catalogue():
    size = 5
    for group in ([i for i in SHOP_ITEMS if not i.is_role_card],
                  [i for i in SHOP_ITEMS if i.is_role_card]):
        pages = max(1, (len(group) + size - 1) // size)
        seen = []
        for page in range(pages):
            seen.extend(group[page * size:(page + 1) * size])
        assert seen == group


@pytest.mark.parametrize("lang", LANGUAGES)
def test_shop_navigation_strings_exist(lang):
    data = _load(lang)
    for key in (
        "shop.tab_cosmetics",
        "shop.tab_cards",
        "shop.tab_active",
        "shop.btn_prev",
        "shop.btn_next",
        "shop.btn_page",
        "shop.page_footer",
        "shop.category_empty",
        "shop.cosmetics_header",
        "card.honoured",
        "card.refunded",
    ):
        assert key in data, key
