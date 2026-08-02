"""Middleware that resolves the user's language and injects a translator.

Runs as an inner middleware on the dispatcher. The ``event`` it
receives is the outer ``Update`` object, so we drill into its
non-empty field to find the actual payload (``Message``,
``CallbackQuery``, …) that carries ``from_user``.

Exposes in ``data``:

* ``i18n`` — the process-wide ``I18nManager``.
* ``user_lang`` — the resolved language code (e.g. ``"ru"``).
* ``t`` — a ``Translator`` callable bound to ``user_lang``; handlers
  call it as ``t("some.key", **kwargs)``.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.db.repo import StatsRepo
from app.i18n import I18nManager, get_i18n
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Update payload fields that may carry a ``from_user`` attribute,
# in the order aiogram populates them.
_PAYLOAD_FIELDS = (
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "shipping_query",
    "pre_checkout_query",
    "poll_answer",
    "chat_member",
    "my_chat_member",
    "chat_join_request",
    "message_reaction",
    "message_reaction_count",
)


def _extract_user(event: TelegramObject):
    """Return the ``User`` behind ``event``, or ``None``.

    When registered on ``dp.update``, ``event`` is an ``Update`` wrapper
    that does not have ``from_user`` itself — we must look inside.
    """
    if isinstance(event, Update):
        for field in _PAYLOAD_FIELDS:
            payload = getattr(event, field, None)
            if payload is not None:
                return getattr(payload, "from_user", None)
        return None
    return getattr(event, "from_user", None)


class I18nMiddleware(BaseMiddleware):
    """Resolve user language and inject translator into ``data``."""

    def __init__(self, i18n: I18nManager | None = None) -> None:
        self._i18n = i18n or get_i18n()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event)
        session: AsyncSession | None = data.get("session")

        lang = self._i18n.default_lang
        if user is not None and session is not None:
            try:
                persisted = await StatsRepo(session).get_language(user.id)
                # If the user already has a language row, trust it.
                # Otherwise guess from Telegram's language_code so the very
                # first greeting (before the picker is shown) is close.
                known = await StatsRepo(session).is_known(user.id)
                if known:
                    lang = persisted
                else:
                    lang = self._guess_from_telegram(user) or persisted
            except Exception:  # noqa: BLE001 — keep bot alive if DB hiccups
                logger.exception(
                    "Failed to read language for user %s; using default.",
                    getattr(user, "id", None),
                )
            finally:
                # If we still have no DB row, fall back to Telegram hint.
                if user is not None and not lang:
                    lang = self._guess_from_telegram(user) or self._i18n.default_lang

        translator = self._i18n.translator_for(lang)
        data["i18n"] = self._i18n
        data["user_lang"] = lang
        data["t"] = translator
        return await handler(event, data)

    def _guess_from_telegram(self, user) -> str | None:
        """Map Telegram's ``language_code`` to a supported language.

        ``language_code`` is a 2-letter code like ``"ru"``, ``"en"``,
        ``"uz"``. Returns ``None`` if we cannot map it.
        """
        code = (getattr(user, "language_code", None) or "").lower()
        if not code:
            return None
        supported = self._i18n.available_languages
        if code in supported:
            return code
        # Some Telegram clients report "uz-latn" etc.; match the prefix.
        prefix = code.split("-")[0]
        if prefix in supported:
            return prefix
        return None
