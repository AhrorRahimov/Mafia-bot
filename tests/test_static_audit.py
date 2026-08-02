"""Static audit of the whole ``app`` package.

These tests need no aiogram, no database and no network: they parse the
source with :mod:`ast`. They exist because three silent breakages shipped
in a row and none of them could be caught by the behavioural tests:

* a helper (``_on_off``) was lost during an edit, so the "System" screen
  raised ``NameError`` the moment an admin opened it;
* a call site kept the old signature of ``_catalogue_text`` after the
  shop grew tabs, crashing right after a purchase;
* a couple of ``t(...)`` calls stopped passing a placeholder the locale
  string still contained, printing raw ``{user}`` to admins.
"""
from __future__ import annotations

import ast
import builtins
import json
import re
import string
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
LOCALES = APP / "locales"
BUILTINS = set(dir(builtins))
FORMATTER = string.Formatter()


def _sources():
    for path in sorted(APP.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), str(path))


def _rel(path: Path) -> str:
    return str(path.relative_to(APP.parent))


# ---------------------------------------------------------------------------
# 1. Every name a module uses must actually exist in it
# ---------------------------------------------------------------------------

class _Scope:
    def __init__(self, parent=None):
        self.names: set[str] = set()
        self.parent = parent

    def has(self, name: str) -> bool:
        scope = self
        while scope is not None:
            if name in scope.names:
                return True
            scope = scope.parent
        return False


def _bind(node, scope: _Scope) -> None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            scope.names.add(sub.id)


def _bind_args(args: ast.arguments, scope: _Scope) -> None:
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        scope.names.add(arg.arg)
    if args.vararg:
        scope.names.add(args.vararg.arg)
    if args.kwarg:
        scope.names.add(args.kwarg.arg)


def _walk(node, scope: _Scope, found: list) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope.names.add(node.name)
        inner = _Scope(scope)
        _bind_args(node.args, inner)
        for statement in node.body:
            _walk(statement, inner, found)
        return
    if isinstance(node, ast.ClassDef):
        scope.names.add(node.name)
        inner = _Scope(scope)
        for statement in node.body:
            _walk(statement, inner, found)
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            scope.names.add((alias.asname or alias.name).split(".")[0])
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        for target in targets:
            _bind(target, scope)
        if getattr(node, "value", None) is not None:
            _walk(node.value, scope, found)
        return
    if isinstance(node, (ast.For, ast.AsyncFor)):
        _bind(node.target, scope)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                _bind(item.optional_vars, scope)
    if isinstance(node, ast.ExceptHandler) and node.name:
        scope.names.add(node.name)
    if isinstance(
        node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    ):
        inner = _Scope(scope)
        for generator in node.generators:
            _bind(generator.target, inner)
            _walk(generator.iter, inner, found)
            for condition in generator.ifs:
                _walk(condition, inner, found)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.comprehension):
                _walk(child, inner, found)
        return
    if isinstance(node, ast.Lambda):
        inner = _Scope(scope)
        _bind_args(node.args, inner)
        _walk(node.body, inner, found)
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id not in BUILTINS and not scope.has(node.id):
            found.append((node.lineno, node.id))
        return
    for child in ast.iter_child_nodes(node):
        _walk(child, scope, found)


def test_no_undefined_names():
    broken = []
    for path, tree in _sources():
        module = _Scope()
        module.names.update({"__name__", "__file__", "__doc__"})
        for statement in tree.body:
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                module.names.add(statement.name)
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    module.names.add(
                        (alias.asname or alias.name).split(".")[0]
                    )
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    _bind(target, module)
            elif isinstance(statement, ast.AnnAssign):
                _bind(statement.target, module)
            elif isinstance(statement, ast.Try):
                for sub in ast.walk(statement):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        for alias in sub.names:
                            module.names.add(
                                (alias.asname or alias.name).split(".")[0]
                            )
        found: list = []
        for statement in tree.body:
            _walk(statement, module, found)
        broken += [f"{_rel(path)}:{line} -> {name}" for line, name in found]
    assert not broken, "undefined names: " + "; ".join(sorted(set(broken)))


# ---------------------------------------------------------------------------
# 2. Local call sites must match local signatures
# ---------------------------------------------------------------------------

def test_local_calls_match_signatures():
    signatures: dict[str, list] = {}
    for path, tree in _sources():
        for statement in tree.body:
            if not isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            args = statement.args
            positional = [
                a.arg for a in [*args.posonlyargs, *args.args]
            ]
            defaults = len(args.defaults)
            required = (
                positional[: len(positional) - defaults]
                if defaults else positional
            )
            keyword_only = [a.arg for a in args.kwonlyargs]
            required_kw = [
                name
                for name, default in zip(keyword_only, args.kw_defaults)
                if default is None
            ]
            signatures.setdefault(statement.name, []).append(
                (positional, required, keyword_only, required_kw,
                 args.vararg is not None, args.kwarg is not None)
            )

    broken = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            candidates = signatures.get(node.func.id)
            if not candidates or len(candidates) != 1:
                continue
            positional, required, kwonly, required_kw, star, kwargs = (
                candidates[0]
            )
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            given_kw = [k.arg for k in node.keywords]
            where = f"{_rel(path)}:{node.lineno} {node.func.id}()"
            if len(node.args) > len(positional) and not star:
                broken.append(f"{where}: too many positional arguments")
                continue
            covered = set(positional[: len(node.args)]) | set(given_kw)
            missing = [n for n in required if n not in covered]
            missing += [n for n in required_kw if n not in covered]
            unknown = [
                n for n in given_kw
                if n not in positional and n not in kwonly and not kwargs
            ]
            if missing:
                broken.append(f"{where}: missing {missing}")
            if unknown:
                broken.append(f"{where}: unexpected {unknown}")
    assert not broken, "bad calls: " + "; ".join(broken)


# ---------------------------------------------------------------------------
# 3. Every t(...) call must feed the placeholders its string needs
# ---------------------------------------------------------------------------

def _placeholders(raw: str) -> set[str]:
    names = set()
    try:
        for _, field, _, _ in FORMATTER.parse(raw):
            if field:
                names.add(field.split(".")[0].split("[")[0])
    except ValueError:
        return set()
    return names - {""}


def test_translation_calls_pass_every_placeholder():
    locale = json.loads((LOCALES / "ru.json").read_text(encoding="utf-8"))
    broken = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name) and node.func.id == "t"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            key = node.args[0].value
            if not isinstance(key, str) or key not in locale:
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            given = {k.arg for k in node.keywords if k.arg}
            missing = _placeholders(locale[key]) - given
            if missing:
                broken.append(
                    f"{_rel(path)}:{node.lineno} {key} -> {sorted(missing)}"
                )
    assert not broken, "unfilled placeholders: " + "; ".join(broken)


# ---------------------------------------------------------------------------
# 4. Feature switches must be nameable in every language
# ---------------------------------------------------------------------------

FLAG_KEYS = (
    "maintenance",
    "feature.shop",
    "feature.dead_chat",
    "feature.detective_shoot",
    "feature.role_cards",
)


def test_every_feature_flag_has_a_label():
    source = (APP / "game" / "flags.py").read_text(encoding="utf-8")
    for key in FLAG_KEYS:
        assert f'"{key}"' in source, f"{key} disappeared from flags.py"
    for lang in ("ru", "en", "uz"):
        data = json.loads(
            (LOCALES / f"{lang}.json").read_text(encoding="utf-8")
        )
        for key in FLAG_KEYS:
            assert f"admin.flag.{key}" in data, f"{lang}: admin.flag.{key}"


def test_admin_command_menu_lists_real_commands():
    """Everything advertised in the "/" menu must have a handler."""
    main = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main)
    advertised: set[str] = set()
    for node in ast.walk(tree):
        # The lists are annotated assignments, so both node types matter.
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        names = {
            target.id for target in targets
            if isinstance(target, ast.Name)
        }
        if not names & {
            "GROUP_COMMANDS", "PRIVATE_COMMANDS", "ADMIN_COMMANDS"
        }:
            continue
        for element in ast.walk(node.value):
            if isinstance(element, ast.Tuple) and element.elts:
                first = element.elts[0]
                if isinstance(first, ast.Constant) and isinstance(
                    first.value, str
                ):
                    advertised.add(first.value)

    handled: set[str] = set()
    for _, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Command":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(
                        arg.value, str
                    ):
                        handled.add(arg.value)

    assert advertised, "no command menu found in main.py"
    missing = sorted(advertised - handled)
    assert not missing, f"advertised but not handled: {missing}"


def test_admin_commands_are_advertised():
    """Admin-only commands worth a menu entry must be published."""
    main = (APP / "main.py").read_text(encoding="utf-8")
    block = main.split("ADMIN_COMMANDS", 1)[1]
    for command in ("admin", "flags", "health", "maintenance", "audit"):
        assert f'("{command}"' in block, f"/{command} is not advertised"


# ---------------------------------------------------------------------------
# 5. Command routing: nothing may be silently shadowed
# ---------------------------------------------------------------------------

# Routers are consulted in this order, so an earlier file wins a tie.
ROUTER_ORDER = (
    "admin.py", "admin_panel.py", "basic.py", "lobby.py",
    "night.py", "day.py", "shop.py", "private_chat.py",
)

# /extend is deliberately claimed twice: the admin handler runs first and
# hands the update to the lobby one whenever it is not an admin call.
DELEGATED_COMMANDS = {"extend": "lobby_extend"}


def _command_registrations():
    """``command -> [(file, handler)]`` for every message handler."""
    found: dict[str, list[tuple[str, str]]] = {}
    handlers = APP / "handlers"
    for path in sorted(handlers.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in tree.body:
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            for decorator in node.decorator_list:
                dump = ast.dump(decorator)
                if "router" not in dump or "message" not in dump:
                    continue
                for sub in ast.walk(decorator):
                    if not isinstance(sub, ast.Call):
                        continue
                    if not (
                        isinstance(sub.func, ast.Name)
                        and sub.func.id == "Command"
                    ):
                        continue
                    for arg in sub.args:
                        if isinstance(arg, ast.Constant) and isinstance(
                            arg.value, str
                        ):
                            found.setdefault(arg.value, []).append(
                                (path.name, node.name)
                            )
    return found


def test_no_command_is_silently_shadowed():
    """Two handlers for one command means the later one never runs."""
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (APP / "handlers").glob("*.py")
    }
    for command, owners in _command_registrations().items():
        if len(owners) == 1:
            continue
        marker = DELEGATED_COMMANDS.get(command)
        assert marker, (
            f"/{command} is registered {len(owners)} times: {owners}"
        )
        first_file = min(
            owners, key=lambda item: ROUTER_ORDER.index(item[0])
        )[0]
        assert marker in sources[first_file], (
            f"/{command} is claimed twice but {first_file} never "
            f"delegates to the other handler"
        )


def test_every_handled_command_is_reachable():
    """Commands the help text advertises must exist as handlers."""
    registered = set(_command_registrations())
    locale = json.loads((LOCALES / "ru.json").read_text(encoding="utf-8"))
    advertised = set()
    for key, raw in locale.items():
        if not (key.startswith("help") or key.startswith("admin.help")):
            continue
        for match in re.finditer(r"(?<!<)/([a-z]{2,20})\b", raw):
            advertised.add(match.group(1))
    missing = sorted(advertised - registered)
    assert not missing, f"documented but not handled: {missing}"


# ---------------------------------------------------------------------------
# 6. Feature-switch names admins actually type
# ---------------------------------------------------------------------------

def test_flag_aliases_resolve():
    from app.game.flags import (
        ALL_FLAG_KEYS,
        FEATURE_FLAGS,
        KEY_MAINTENANCE,
        flag_default,
        resolve_flag_key,
    )

    # Full keys always resolve to themselves.
    for key in ALL_FLAG_KEYS:
        assert resolve_flag_key(key) == key

    # Short forms, casing, dashes and a stray slash are all accepted.
    assert resolve_flag_key("shop") == "feature.shop"
    assert resolve_flag_key("SHOP") == "feature.shop"
    assert resolve_flag_key("/dead-chat") == "feature.dead_chat"
    assert resolve_flag_key("cards") == "feature.role_cards"
    assert resolve_flag_key("shoot") == "feature.detective_shoot"

    # Garbage is rejected rather than toggling something at random.
    for junk in ("", "   ", "nonsense", "feature.", "coin_multiplier"):
        assert resolve_flag_key(junk) is None, junk

    # Defaults: maintenance off, every feature on.
    assert flag_default(KEY_MAINTENANCE) is False
    for key in FEATURE_FLAGS:
        assert flag_default(key) is True


def test_flags_command_uses_the_resolver_and_buttons():
    source = (APP / "handlers" / "admin.py").read_text(encoding="utf-8")
    body = source.split("async def cmd_flags", 1)[1].split("\n@router", 1)[0]
    assert "resolve_flag_key(" in body, "/flags must accept short names"
    assert "admin_flags_kb(" in body, "/flags must offer buttons"
    assert "is_maintenance(" in body, "/flags must list maintenance too"


def test_unhandled_errors_are_reported():
    source = (APP / "main.py").read_text(encoding="utf-8")
    assert "dp.errors.register" in source, "no global error handler"
    for lang in ("ru", "en", "uz"):
        data = json.loads(
            (LOCALES / f"{lang}.json").read_text(encoding="utf-8")
        )
        assert "errors.unexpected" in data, lang


# ---------------------------------------------------------------------------
# 7. The "/" menus Telegram shows
# ---------------------------------------------------------------------------

def _menu_entries() -> dict[str, list[tuple[str, str]]]:
    """``menu name -> [(command, description)]`` parsed from main.py."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    menus: dict[str, list[tuple[str, str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if not target.id.endswith("_COMMANDS"):
                continue
            entries: list[tuple[str, str]] = []
            for element in ast.walk(node.value):
                if not (isinstance(element, ast.Tuple) and len(element.elts) == 2):
                    continue
                name, description = element.elts
                if isinstance(name, ast.Constant) and isinstance(
                    description, ast.Constant
                ):
                    entries.append((name.value, description.value))
            menus[target.id] = entries
    return menus


def test_menus_obey_telegram_limits():
    """Telegram silently rejects a whole menu if one entry is invalid."""
    for menu, entries in _menu_entries().items():
        assert entries, f"{menu} is empty"
        assert len(entries) <= 100, f"{menu} exceeds 100 commands"
        seen: set[str] = set()
        for name, description in entries:
            assert re.fullmatch(r"[a-z0-9_]{1,32}", name), (
                f"{menu}: /{name} is not a valid command name"
            )
            assert 1 <= len(description) <= 256, f"{menu}: /{name} description"
            assert name not in seen, f"{menu} lists /{name} twice"
            seen.add(name)


def test_every_menu_entry_has_a_handler():
    """An advertised command that does nothing looks like a broken bot.

    Aliases count too: Telegram matches menu entries literally, which is
    why /newgame has to be listed even though /game reaches the same
    handler.
    """
    registered = set(_command_registrations())
    for menu, entries in _menu_entries().items():
        missing = sorted(
            name for name, _ in entries if name not in registered
        )
        assert not missing, f"{menu} advertises unhandled: {missing}"


def test_group_menu_holds_no_private_only_commands():
    """Commands that refuse to work in a group must not be offered there.

    /role answers "private chat only", so listing it at the table would
    just teach players that the menu lies.
    """
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (APP / "handlers").glob("*.py")
    }
    private_only = set()
    for file_name, source in sources.items():
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            # Merely reading chat.type is not a refusal: /top checks it
            # to pick which leaderboards to offer and still works in a
            # group. Only a "private_only" reply means the command is
            # unusable at the table.
            if 'chat.type != "private"' not in body:
                continue
            if "private_only" not in body:
                continue
            for decorator in node.decorator_list:
                for sub in ast.walk(decorator):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "Command"
                    ):
                        for arg in sub.args:
                            if isinstance(arg, ast.Constant):
                                private_only.add(arg.value)
    group = {name for name, _ in _menu_entries().get("GROUP_COMMANDS", [])}
    clash = sorted(group & private_only)
    assert not clash, f"group menu offers private-only commands: {clash}"


def test_lobby_aliases_are_all_advertised():
    """Every spelling of the "open a lobby" command belongs in the menu."""
    group = {name for name, _ in _menu_entries().get("GROUP_COMMANDS", [])}
    for alias in ("newgame", "game"):
        assert alias in group, f"/{alias} is missing from the group menu"


# ---------------------------------------------------------------------------
# 8. await mismatches
# ---------------------------------------------------------------------------

# Names that also exist on third-party objects (SQLAlchemy sessions, aiogram
# messages...) where we cannot tell from the source whether they are
# coroutines, so judging them would only produce false alarms.
_AMBIGUOUS = {
    "get", "set", "all", "log", "top", "close", "start", "answer", "add",
}


def _async_and_sync_names() -> tuple[dict[str, set], dict[str, set]]:
    async_names: dict[str, set] = {}
    sync_names: dict[str, set] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_names.setdefault(node.name, set()).add(path.name)
            elif isinstance(node, ast.FunctionDef):
                sync_names.setdefault(node.name, set()).add(path.name)
    return async_names, sync_names


def _called_name(call: ast.Call):
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def test_nothing_awaits_a_plain_function():
    """``await`` on a normal function raises at runtime, not at import.

    ``/top`` died with "'bool' object can't be awaited" because the season
    freshness check is an ordinary method that was being awaited. Static
    name checks cannot see this; comparing definitions to call sites can.
    """
    async_names, sync_names = _async_and_sync_names()
    only_sync = set(sync_names) - set(async_names)
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
            ):
                continue
            name = _called_name(node.value)
            if name and name in only_sync and name not in _AMBIGUOUS:
                offenders.append(f"{path.name}:{node.lineno} await {name}()")
    assert not offenders, f"awaiting plain functions: {offenders}"


def test_no_coroutine_is_called_without_await():
    """A coroutine called as a statement never runs and never complains."""
    async_names, sync_names = _async_and_sync_names()
    only_async = set(async_names) - set(sync_names)
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
            ):
                continue
            name = _called_name(node.value)
            if name and name in only_async and name not in _AMBIGUOUS:
                offenders.append(f"{path.name}:{node.lineno} {name}()")
    assert not offenders, f"coroutines never awaited: {offenders}"

# ---------------------------------------------------------------------------
# 9. per-chat leaderboard
# ---------------------------------------------------------------------------


def _all_locales():
    return {
        lang: json.loads(
            (LOCALES / f"{lang}.json").read_text(encoding="utf-8")
        )
        for lang in ("ru", "en", "uz")
    }


def test_chat_board_is_translated_everywhere():
    """The chat board must not fall back to Russian for other languages."""
    required = (
        "top.board.chat",
        "top.header.chat",
        "top.line.chat",
        "top.chat_empty",
    )
    for lang, locale in _all_locales().items():
        for key in required:
            assert key in locale, f"{lang} is missing {key}"
            assert locale[key].strip(), f"{lang}:{key} is empty"


def test_chat_board_placeholders_match_the_code():
    """Every language must accept exactly the arguments texts.py passes."""
    for lang, locale in _all_locales().items():
        assert "{chat}" in locale["top.header.chat"], lang
        line = locale["top.line.chat"]
        for field in ("{medal}", "{name}", "{wins}", "{played}", "{winrate}"):
            assert field in line, f"{lang} line is missing {field}"


def test_error_message_hides_internal_wording():
    """Players should not be told about journals or tracebacks."""
    leaks = ("traceback", "exception", "typeerror")
    for lang, locale in _all_locales().items():
        text = locale["errors.unexpected"]
        assert text.strip(), lang
        lowered = text.lower()
        for leak in leaks:
            assert leak not in lowered, f"{lang} leaks {leak!r}"


def test_chat_board_is_group_only():
    """The chat board belongs to the group menu, never to the DM menu."""
    source = (APP / "texts.py").read_text(encoding="utf-8")
    assert 'CHAT_BOARD = "chat"' in source
    assert "GROUP_TOP_BOARDS = (CHAT_BOARD,) + TOP_BOARDS" in source
    assert '"chat"' not in source.split("TOP_BOARDS = (")[1].split(")")[0]


def test_top_handler_scopes_the_board_to_the_chat():
    """/top and its buttons must pass the chat id through."""
    source = (APP / "handlers" / "basic.py").read_text(encoding="utf-8")
    assert "ChatLeaderboardRepo" in source
    assert "GROUP_TOP_BOARDS" in source
    # Both the command and the callback decide the scope themselves.
    assert source.count("chat_id=") >= 2
    assert source.count('!= "private"') >= 2
