"""Human-readable rendering of the admin audit trail.

The raw rows store machine codes (``game.force_end``, ``user.ban``...),
which are great for grepping and terrible for reading. This module turns
them into a card a human can scan: entries grouped by day, an emoji per
category, a translated action name and a compact "who -> whom" line.

The journal shows the last :data:`AUDIT_PAGE_TOTAL` actions, split into
pages of :data:`AUDIT_PAGE_SIZE` so every message stays far below
Telegram's 4096-character limit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

# How many entries the journal keeps in view, and how many fit on a page.
AUDIT_PAGE_TOTAL = 50
AUDIT_PAGE_SIZE = 10

# Emoji per action, falling back to the category prefix and finally to a
# neutral bullet. Keeping this table here (instead of in the locales)
# means a new action code degrades gracefully in every language.
ACTION_EMOJI: dict[str, str] = {
    "game.force_end": "\U0001F6D1",
    "game.skip_phase": "\u23ED\uFE0F",
    "game.extend": "\u23F1\uFE0F",
    "game.kick_player": "\U0001F45F",
    "game.reveal_roles": "\U0001F441\uFE0F",
    "user.ban": "\U0001F6AB",
    "user.unban": "\u267B\uFE0F",
    "user.autoban": "\U0001F916",
    "user.warn": "\u26A0\uFE0F",
    "user.clear_warnings": "\U0001F9F9",
    "user.mute": "\U0001F507",
    "chat.ban": "\U0001F4F5",
    "chat.unban": "\U0001F4F2",
    "economy.multiplier": "\u2716\uFE0F",
    "economy.give": "\U0001F4B0",
    "economy.take": "\U0001F4B8",
    "shop.grant": "\U0001F381",
    "role.grant": "\U0001F3AD",
    "role.revoke": "\U0001F5D1\uFE0F",
    "promo.create": "\U0001F39F\uFE0F",
    "promo.delete": "\u2702\uFE0F",
    "admin.grant": "\U0001F451",
    "admin.revoke": "\U0001F53B",
    "system.flag": "\U0001F39B\uFE0F",
    "system.maintenance": "\U0001F6A7",
    "system.reload_locales": "\U0001F504",
    "system.broadcast": "\U0001F4E2",
    "system.error": "\U0001F4A5",
    "season.close": "\U0001F3C1",
}

CATEGORY_EMOJI: dict[str, str] = {
    "game": "\U0001F3B2",
    "user": "\U0001F464",
    "chat": "\U0001F4AC",
    "economy": "\U0001FA99",
    "shop": "\U0001F6D2",
    "role": "\U0001F3AD",
    "promo": "\U0001F39F\uFE0F",
    "admin": "\U0001F6E1\uFE0F",
    "system": "\u2699\uFE0F",
    "season": "\U0001F3C6",
}

FALLBACK_EMOJI = "\u2022"


def action_emoji(action: str) -> str:
    """Pick the icon for an action code."""
    if action in ACTION_EMOJI:
        return ACTION_EMOJI[action]
    category = action.split(".", 1)[0]
    return CATEGORY_EMOJI.get(category, FALLBACK_EMOJI)


def action_title(action: str, t) -> str:
    """Translated action name, falling back to the raw code.

    Unknown codes must never render as an empty line, so a missing
    ``admin.action.<code>`` key degrades to the code itself.
    """
    key = f"admin.action.{action}"
    label = t(key)
    if not label or label == key:
        return action
    return label


def _as_local(moment: Optional[datetime]) -> Optional[datetime]:
    """Treat naive timestamps as UTC so grouping never crashes."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _day_label(moment: Optional[datetime], t) -> str:
    if moment is None:
        return t("admin.audit_day_unknown")
    today = datetime.now(timezone.utc).date()
    day = moment.date()
    delta = (today - day).days
    if delta == 0:
        return t("admin.audit_day_today")
    if delta == 1:
        return t("admin.audit_day_yesterday")
    return moment.strftime("%d.%m.%Y")


def _shorten(value: str, limit: int = 60) -> str:
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "\u2026"


def page_count(total: int, page_size: int = AUDIT_PAGE_SIZE) -> int:
    """Number of pages for ``total`` entries (at least one)."""
    if total <= 0:
        return 1
    return (total + page_size - 1) // page_size


def render_page(
    rows: Sequence,
    t,
    *,
    page: int,
    total: int,
    names: Optional[dict[int, str]] = None,
    page_size: int = AUDIT_PAGE_SIZE,
) -> str:
    """Render one page of the journal.

    Args:
        rows: the ``AdminAudit`` rows for this page, newest first.
        t: translator.
        page: zero-based page index.
        total: how many entries the journal holds in total.
        names: optional ``admin_id -> display name`` map, so the trail
            reads "Anna" instead of a bare numeric id.
    """
    if not rows:
        return t("admin.audit_empty")

    names = names or {}
    first = page * page_size + 1
    last = page * page_size + len(rows)
    lines = [
        t(
            "admin.audit_title",
            first=first,
            last=last,
            total=total,
            page=page + 1,
            pages=page_count(total, page_size),
        )
    ]

    current_day: Optional[str] = None
    for index, row in enumerate(rows, start=first):
        moment = _as_local(getattr(row, "created_at", None))
        day = _day_label(moment, t)
        if day != current_day:
            lines.append("")
            lines.append(t("admin.audit_day", day=day))
            current_day = day

        action = str(getattr(row, "action", "") or "")
        admin_id = int(getattr(row, "admin_id", 0) or 0)
        admin_name = names.get(admin_id) or str(admin_id)
        lines.append(
            t(
                "admin.audit_entry",
                index=index,
                emoji=action_emoji(action),
                action=action_title(action, t),
                time=moment.strftime("%H:%M") if moment else "--:--",
                admin=_shorten(admin_name, 32),
                admin_id=admin_id,
            )
        )

        target = str(getattr(row, "target", "") or "").strip()
        details = str(getattr(row, "details", "") or "").strip()
        if target:
            lines.append(t("admin.audit_target", target=_shorten(target)))
        if details:
            lines.append(t("admin.audit_details", details=_shorten(details, 90)))

    return "\n".join(lines)


def admin_ids_in(rows: Iterable) -> list[int]:
    """Distinct admin ids referenced by these rows (for name lookup)."""
    seen: list[int] = []
    for row in rows:
        admin_id = int(getattr(row, "admin_id", 0) or 0)
        if admin_id and admin_id not in seen:
            seen.append(admin_id)
    return seen


__all__ = [
    "AUDIT_PAGE_SIZE",
    "AUDIT_PAGE_TOTAL",
    "action_emoji",
    "action_title",
    "admin_ids_in",
    "page_count",
    "render_page",
]
