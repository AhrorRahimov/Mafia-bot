"""Actually *execute* handler bodies instead of only parsing them.

The static audits catch typos and bad signatures, but they cannot catch a
handler that raises the moment it runs. ``/flags`` shipped broken twice
because of exactly that gap: every name existed, yet the very first call
died with ``t() got multiple values for argument 'key'``.

The bot's real dependencies (aiogram, SQLAlchemy) are not installed here,
so a handler body is lifted out of the source with ``ast`` and executed
against small fakes. The i18n manager and the flag registry are pure
stdlib, so those are the genuine implementations.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

from app.game.flags import (
    ALL_FLAG_KEYS,
    FEATURE_FLAGS,
    KEY_MAINTENANCE,
    flag_default,
    resolve_flag_key,
)
from app.i18n.manager import I18nManager

APP = Path(__file__).resolve().parent.parent / "app"
LOCALES = APP / "locales"


def _translator(lang: str = "ru"):
    """The real translator, so placeholder bugs surface here."""
    return I18nManager(LOCALES, default_lang="ru").translator_for(lang)


class FakeConfig:
    """In-memory stand-in for the ``bot_config`` table."""

    def __init__(self) -> None:
        self.values: dict[str, bool] = {}

    async def is_maintenance(self, session) -> bool:
        return self.values.get(KEY_MAINTENANCE, False)

    async def get_bool(self, session, key, default=False) -> bool:
        return self.values.get(key, default)

    async def toggle(self, session, key, *, default, admin_id) -> bool:
        value = not self.values.get(key, default)
        self.values[key] = value
        return value


class FakeMessage:
    def __init__(self) -> None:
        self.from_user = type("U", (), {"id": 1})()
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text, **kwargs):
        assert isinstance(text, str) and text.strip(), "empty reply"
        self.sent.append((text, kwargs))


class FakeCommand:
    def __init__(self, args=None) -> None:
        self.args = args


def load_handler(module: str, name: str, namespace: dict):
    """Compile one handler out of ``app/handlers/<module>.py``."""
    source = (APP / "handlers" / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    fn.decorator_list = []
    scope = dict(namespace)
    # Annotations are evaluated at def time; the real types are irrelevant.
    for hint in (
        "Message", "CommandObject", "AsyncSession", "Translator",
        "CallbackQuery", "Bot",
    ):
        scope.setdefault(hint, object)
    exec(compile(ast.Module([fn], []), f"{module}.{name}", "exec"), scope)
    return scope[name]


def _flags_namespace(config: FakeConfig) -> dict:
    """Everything ``cmd_flags`` closes over, with the real helpers kept."""

    async def _guard(message, session, t):
        return True

    async def _audit(session, message, action, **kwargs):
        recorded.append((action, kwargs))

    def _args(command):
        return (command.args or "").split()

    def admin_flags_kb(flags, t):
        # Mirrors the real keyboard: every label goes through the
        # translator, so a missing flag label fails the test.
        labels = [
            t("admin.btn.flag", name=t(f"admin.flag.{key}"),
              state="on" if enabled else "off")
            for key, enabled in flags.items()
        ]
        assert all(labels), "a flag button had an empty label"
        return labels

    recorded: list[tuple[str, dict]] = []
    return {
        "_guard": _guard,
        "_audit": _audit,
        "_args": _args,
        "admin_flags_kb": admin_flags_kb,
        "runtime_config": config,
        "resolve_flag_key": resolve_flag_key,
        "flag_default": flag_default,
        "ALL_FLAG_KEYS": ALL_FLAG_KEYS,
        "FEATURE_FLAGS": FEATURE_FLAGS,
        "KEY_MAINTENANCE": KEY_MAINTENANCE,
        "_recorded": recorded,
    }


def test_flags_listing_runs():
    """Bare ``/flags`` must answer with a list *and* buttons."""
    config = FakeConfig()
    namespace = _flags_namespace(config)
    handler = load_handler("admin", "cmd_flags", namespace)
    message = FakeMessage()
    asyncio.run(handler(message, FakeCommand(), None, _translator()))

    assert len(message.sent) == 1
    text, kwargs = message.sent[0]
    for key in ALL_FLAG_KEYS:
        assert key in text, f"{key} missing from the listing"
    buttons = kwargs.get("reply_markup")
    assert buttons, "/flags answered without buttons"
    assert len(buttons) == len(ALL_FLAG_KEYS)


def test_flags_toggle_runs_for_every_spelling():
    """Full keys, short names and aliases must all flip a switch."""
    spellings = {
        "shop": "feature.shop",
        "feature.shop": "feature.shop",
        "cards": "feature.role_cards",
        "dead-chat": "feature.dead_chat",
        "maintenance": KEY_MAINTENANCE,
        "TECH": KEY_MAINTENANCE,
    }
    for typed, expected in spellings.items():
        config = FakeConfig()
        namespace = _flags_namespace(config)
        handler = load_handler("admin", "cmd_flags", namespace)
        message = FakeMessage()
        asyncio.run(
            handler(message, FakeCommand(typed), None, _translator())
        )
        assert expected in config.values, f"/flags {typed} changed nothing"
        actions = [action for action, _ in namespace["_recorded"]]
        assert actions, f"/flags {typed} was not written to the journal"
        if expected == KEY_MAINTENANCE:
            assert actions == ["system.maintenance"]
        else:
            assert actions == ["system.flag"]


def test_flags_rejects_nonsense_without_crashing():
    config = FakeConfig()
    namespace = _flags_namespace(config)
    handler = load_handler("admin", "cmd_flags", namespace)
    message = FakeMessage()
    asyncio.run(handler(message, FakeCommand("nonsense"), None, _translator()))
    assert config.values == {}, "a typo must not flip anything"
    assert len(message.sent) == 1


def test_flags_run_in_every_language():
    for lang in ("ru", "en", "uz"):
        config = FakeConfig()
        namespace = _flags_namespace(config)
        handler = load_handler("admin", "cmd_flags", namespace)
        message = FakeMessage()
        asyncio.run(handler(message, FakeCommand(), None, _translator(lang)))
        assert message.sent, lang


# ---------------------------------------------------------------------------
# The translator itself
# ---------------------------------------------------------------------------

def test_translator_accepts_reserved_placeholder_names():
    """A locale string may use ``{key}``/``{lang}`` as a placeholder.

    This is what broke ``/flags``: the translator took ``key`` as a normal
    parameter, so passing ``key=`` as interpolation data collided with it.
    """
    t = _translator()
    rendered = t("admin.flags_line", key="feature.shop", state="on")
    assert "feature.shop" in rendered

    manager = I18nManager(LOCALES, default_lang="ru")
    assert manager.translate("ru", "admin.flags_line", key="x", state="y")


def test_no_locale_placeholder_can_shadow_translator_parameters():
    """Guard the rule for every string in every language."""
    import string

    t = _translator()
    reserved = {"self", "lang"}
    for lang in ("ru", "en", "uz"):
        data = json.loads(
            (LOCALES / f"{lang}.json").read_text(encoding="utf-8")
        )
        for locale_key, raw in data.items():
            fields = {
                name
                for _, name, _, _ in string.Formatter().parse(raw)
                if name
            }
            clash = fields & reserved
            assert not clash, f"{lang}:{locale_key} uses {clash}"
    # ``key`` is allowed precisely because it is positional-only now.
    assert t("admin.flag_set", key="k", state="on")
