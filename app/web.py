"""Minimal HTTP server for Render Web Service compliance.

Render's Web Service expects the process to bind to ``$PORT`` within
~60 seconds of boot, otherwise the deploy is marked failed. This
module provides a tiny ``aiohttp.web`` app exposing ``GET /`` and
``GET /healthz`` returning a JSON health payload.

The server is designed to run as an ``asyncio.Task`` alongside the
aiogram polling loop — both share the same event loop.

Usage::

    from app.web import start_health_server
    runner = await start_health_server(port=8080)
    ...
    await runner.cleanup()
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_HEALTH_BODY = {"status": "ok", "service": "mafia-bot"}


async def _health_handler(_: web.Request) -> web.Response:
    """Return 200 OK for Render health checks and external keep-alive pings."""
    return web.json_response(_HEALTH_BODY)


def _build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", _health_handler),
        web.get("/healthz", _health_handler),
    ])
    return app


async def start_health_server(port: int) -> web.AppRunner:
    """Start the HTTP server on ``port`` and return the runner.

    The caller is responsible for ``await runner.cleanup()`` on shutdown.
    """
    app = _build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health-check server listening on 0.0.0.0:%s", port)
    return runner


# Silence aiohttp's default access log unless explicitly enabled.
_AIOHTTP_ACCESS_LOGGER = "aiohttp.access"
if not logging.getLogger(_AIOHTTP_ACCESS_LOGGER).level:
    logging.getLogger(_AIOHTTP_ACCESS_LOGGER).setLevel(logging.WARNING)


__all__: list[Any] = ["start_health_server"]
