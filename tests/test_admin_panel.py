"""Tests for the admin panel: callback routing and locale coverage.

These tests intentionally avoid importing aiogram or SQLAlchemy so they can
run in a bare environment; they cover the pure logic and the translation
files, which is where regressions are cheapest to catch.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.keyboards.callbacks import CallbackAction, parse_admin_route

LOCALES_DIR = Path(__file__).resolve().parents[1] / "app" / "locales"
LANGUAGES = ("ru", "en", "uz")


def _load(lang: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Callback routing
# --------------------------------------------------------------------------

def test_admin_action_value_is_short():
    # Telegram caps callback_data at 64 bytes, so the prefix must stay tiny.
    assert CallbackAction.ADMIN.value == "adm"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("adm", ("menu", [])),
        ("adm:", ("menu", [])),
        ("adm:games", ("games", [])),
        ("adm:game:-1001234", ("game", ["-1001234"])),
        ("adm:extend:-1001234:30", ("extend", ["-1001234", "30"])),
        ("adm:toggle:feature.shop", ("toggle", ["feature.shop"])),
    ],
)
def test_parse_admin_route(payload, expected):
    assert parse_admin_route(payload) == expected


def test_parse_admin_route_never_raises_on_garbage():
    for payload in ("adm::", "adm:::", "adm:unknown:x:y:z"):
        section, args = parse_admin_route(payload)
        assert isinstance(section, str)
        assert isinstance(args, list)


def test_admin_routes_fit_telegram_callback_limit():
    route = f"{CallbackAction.ADMIN.value}:extend:-1001234567890:600"
    assert len(route.encode("utf-8")) <= 64


# --------------------------------------------------------------------------
# Locale coverage
# --------------------------------------------------------------------------

def test_all_locales_share_the_same_keys():
    base = set(_load("ru"))
    for lang in LANGUAGES[1:]:
        assert set(_load(lang)) == base, f"{lang}.json is out of sync with ru.json"


def test_admin_and_promo_namespaces_are_translated():
    ru = _load("ru")
    admin_keys = [key for key in ru if key.startswith("admin.")]
    assert len(admin_keys) > 100, "admin panel texts are missing"
    for lang in LANGUAGES:
        data = _load(lang)
        for key in admin_keys + [k for k in ru if k.startswith("promo.")]:
            assert data.get(key), f"{lang}.json has no text for {key}"


def test_adminhelp_sections_exist():
    for lang in LANGUAGES:
        data = _load(lang)
        for section in (
            "games",
            "moderation",
            "economy",
            "analytics",
            "system",
            "broadcast",
            "admins",
        ):
            assert data.get(f"admin.help.{section}"), f"{lang}: help.{section} missing"


def test_placeholders_match_across_languages():
    """A missing ``{count}`` in one language would raise KeyError at runtime."""
    ru = _load("ru")
    others = {lang: _load(lang) for lang in LANGUAGES[1:]}
    pattern = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
    for key, text in ru.items():
        expected = set(pattern.findall(text))
        for lang, data in others.items():
            assert set(pattern.findall(data[key])) == expected, (
                f"{lang}.json placeholders differ for {key}"
            )


def test_no_unescaped_html_brackets_in_admin_help():
    """Help texts are sent with parse_mode=HTML; raw <sec> would break them."""
    allowed = re.compile(r"</?(b|i|u|s|code|pre|a)(\s[^<>]*)?>")
    for lang in LANGUAGES:
        data = _load(lang)
        for key, text in data.items():
            if not key.startswith("admin.help."):
                continue
            stripped = allowed.sub("", text)
            assert "<" not in stripped and ">" not in stripped, (
                f"{lang}: {key} contains raw angle brackets"
            )


# --------------------------------------------------------------------------
# Button-driven panel routes (added in 1.9)
# --------------------------------------------------------------------------

PANEL_ROUTES = (
    "moderation", "bans:0", "unban:123456789", "chatbans",
    "unbanchat:-1001234567890", "economy", "mult:1.5", "promos:0",
    "promodel:WELCOME2026", "admins", "revoke:123456789",
)


@pytest.mark.parametrize("route", PANEL_ROUTES)
def test_panel_routes_are_short_and_parse_back(route):
    data = f"{CallbackAction.ADMIN.value}:{route}"
    assert len(data.encode()) <= 64
    section, args = parse_admin_route(data)
    assert section == route.split(":")[0]
    assert args == route.split(":")[1:]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_panel_button_labels_exist(lang):
    data = _load(lang)
    for key in (
        "admin.btn.bans",
        "admin.btn.chatbans",
        "admin.btn.unban",
        "admin.btn.multiplier",
        "admin.btn.active",
        "admin.btn.promos",
        "admin.btn.promo_del",
        "admin.btn.revoke",
        "admin.bans_header",
        "admin.bans_line",
        "admin.bans_empty",
        "admin.chatbans_header",
        "admin.chatbans_line",
        "admin.chatbans_empty",
        "admin.promos_header",
        "admin.promos_line",
        "admin.promos_empty",
        "admin.admins_header",
        "admin.admins_line",
        "admin.admins_empty",
        "admin.forever",
        "admin.unbanned",
        "admin.chat_unbanned",
        "admin.revoked",
        "admin.promo_missing",
    ):
        assert key in data, key


def test_multiplier_presets_are_sane():
    # inline.py imports aiogram, which is absent in a bare environment, so
    # the preset tuple is read straight from the source instead.
    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "keyboards" / "inline.py"
    ).read_text(encoding="utf-8")
    match = re.search(r"MULTIPLIER_PRESETS = \(([^)]*)\)", source)
    assert match, "MULTIPLIER_PRESETS not found"
    values = [float(v.strip().strip('\"')) for v in match.group(1).split(",") if v.strip()]
    assert values == sorted(values)
    assert 1.0 in values
    assert all(0.0 <= v <= 10.0 for v in values)
