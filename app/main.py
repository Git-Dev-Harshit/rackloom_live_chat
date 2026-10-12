"""Rackloom support service: tickets from the site's support widget, a Discord ping for the team,
the agent's phone check and claim, and the private chat between them.

Run: uvicorn app.main:create_app --factory --host 0.0.0.0 --port 9500 --workers 1
One worker on purpose: open sockets and rate limits live in this process (app/hub.py).

Public (through Caddy): /api/support/*  ·  Private network only: /internal/*, /healthz
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from starlette.requests import HTTPConnection

from . import routes_agent, routes_customer, routes_internal, sockets, workers
from .config import Settings
from .core import Support
from .db import open_pool
from .discord import DiscordWebhook
from .http import BodyLimit, SecurityHeaders, install_error_handlers
from .mail import Mailer
from .store import Store
from .turnstile import Turnstile

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("support")


def create_app(settings: Settings | None = None, *, turnstile: Turnstile | None = None, discord: DiscordWebhook | None = None,
               mailer: Mailer | None = None, run_workers: bool = True) -> FastAPI:
    settings = settings or Settings()
    problems = settings.startup_problems()
    if problems:
        # Refuse to start rather than serve a support form anyone can flood.
        raise RuntimeError("support service refuses to start: " + "; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await anyio.to_thread.run_sync(open_pool, settings.database_url)
        sup = Support(
            settings=settings,
            store=Store(pool),
            turnstile=turnstile or Turnstile(settings.turnstile_secret, settings.public_host if settings.production else ""),
            discord=discord or DiscordWebhook(settings.discord_webhook_url),
            mailer=mailer or Mailer(settings),
        )
        app.state.support = sup
        log.info(
            "support service up: captcha %s, discord %s, mail %s, public url %s",
            "on" if sup.turnstile.enabled else "off", "on" if sup.discord.enabled else "off (tickets queue)", sup.mailer.kind, settings.public_url,
        )
        tasks = [asyncio.create_task(workers.outbox_loop(sup)), asyncio.create_task(workers.sweep_loop(sup))] if run_workers else []
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for conn in sup.hub.conns():
                await conn.close(1012, "restarting")
            await sup.turnstile.aclose()
            await sup.discord.aclose()
            await anyio.to_thread.run_sync(pool.close)

    app = FastAPI(title="rackloom-support", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(BodyLimit)
    app.add_middleware(SecurityHeaders)

    def support_of(conn: HTTPConnection) -> Support:
        return conn.app.state.support

    app.include_router(routes_customer.router(support_of, settings))
    app.include_router(routes_agent.router(support_of, settings))
    app.include_router(routes_internal.router(support_of, settings))
    app.include_router(sockets.router(support_of))

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        sup = support_of(request)
        try:
            counts = await sup.db(sup.store.counts)
            ok = True
        except Exception as e:  # noqa: BLE001 - names only
            counts, ok = {"error": type(e).__name__}, False
        return {
            "ok": ok,
            "captcha": "on" if sup.turnstile.enabled else "off",
            "discord": "on" if sup.discord.enabled else "off",
            "mail": sup.mailer.kind,
            "sockets": len(sup.hub.conns()),
            "counts": counts,
        }

    return app
