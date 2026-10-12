"""Background work inside the service process: the Discord outbox and the sweep.

The outbox reads rows committed with the ticket (support_notifications), so nothing is lost if
Discord, the network or this process is down: a row waits, is retried with growing gaps, and is
marked failed (and shown on /admin/support) after the last attempt. Retries never touch tickets.
"""
from __future__ import annotations

import asyncio
import logging

from .core import WS_ENDED, Support
from .discord import DeliveryError, redact, ticket_payload

log = logging.getLogger("support.outbox")

# Seconds to wait after attempt 1, 2, ...; after the last one the row is marked failed.
BACKOFF = [5, 15, 60, 300, 900, 1800, 3600, 3600]
SWEEP_SECONDS = 30


async def deliver_due(sup: Support, *, limit: int = 10) -> int:
    """Deliver what is due now. Returns how many rows were handled."""
    if not sup.discord.enabled:
        return 0
    batch = await sup.db(sup.store.claim_notifications, limit=limit)
    for n in batch:
        await _deliver(sup, n)
    return len(batch)


async def _deliver(sup: Support, n: dict) -> None:
    t = await sup.db(sup.store.notification_ticket, n["ticket_id"])
    if not t:
        await sup.db(sup.store.notification_finish, n["id"], status="skipped", error="ticket not found")
        return
    payload = ticket_payload(t, sup.portal_url(t["reference"]))
    try:
        if n["kind"] == "discord_created":
            mid = await sup.discord.create(payload)
            await sup.db(sup.store.notification_sent, n["id"], message_id=mid, ticket_id=t["id"])
            log.info("discord: ticket %s posted", t["reference"])
            return
        if not t["discord_message_id"]:
            if await sup.db(sup.store.creation_post_pending, t["id"]):
                # The first post is still on its way and may have been drawn before this change:
                # edit it once it exists rather than lose the update.
                await sup.db(sup.store.notification_retry, n["id"], error="waiting for the first post", delay_seconds=BACKOFF[0])
            else:
                await sup.db(sup.store.notification_finish, n["id"], status="skipped", error="no channel message to update")
            return
        await sup.discord.edit(t["discord_message_id"], payload)
        await sup.db(sup.store.notification_sent, n["id"])
        log.info("discord: ticket %s updated (%s)", t["reference"], t["effective_status"])
    except DeliveryError as e:
        err = redact(str(e), sup.discord.url)
        attempt = n["attempts"]
        if e.retryable and attempt <= len(BACKOFF):
            delay = max(BACKOFF[attempt - 1], e.retry_after or 0)
            await sup.db(sup.store.notification_retry, n["id"], error=err, delay_seconds=delay)
            log.warning("discord: ticket %s %s attempt %d failed, retrying in %ss: %s", t["reference"], n["kind"], attempt, delay, err)
        else:
            await sup.db(sup.store.notification_finish, n["id"], status="failed", error=err)
            log.error("discord: ticket %s %s failed for good after %d attempt(s): %s", t["reference"], n["kind"], attempt, err)


async def sweep_once(sup: Support) -> None:
    """Write down expiries, and close any open socket whose session or ticket no longer allows it
    (expired, closed elsewhere, handed over, agent switched off, session revoked)."""
    for ticket_id in await sup.db(sup.store.expire_due):
        await sup.ticket_changed(ticket_id)
    conns = sup.hub.conns()
    if not conns:
        return
    valid = await sup.db(sup.store.valid_sessions, [c.session_id for c in conns])
    stale = [c for c in conns if c.session_id not in valid]
    # A ticket that ended without this process seeing it (closed from the CLI): tell its sockets why.
    for ticket_id in {c.ticket_id for c in stale}:
        t = await sup.db(sup.store.ticket, ticket_id)
        if t and t["effective_status"] not in ("PENDING", "CLAIMED"):
            await sup.ticket_changed(ticket_id)
    for c in stale:
        await c.close(WS_ENDED, "access ended")


async def outbox_loop(sup: Support) -> None:
    while True:
        try:
            if await deliver_due(sup):
                continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a database blip; the rows stay and are retried
            log.warning("outbox tick failed: %s", type(e).__name__)
        try:
            await asyncio.wait_for(sup.outbox_wake.wait(), timeout=10)
        except TimeoutError:
            pass
        sup.outbox_wake.clear()


async def sweep_loop(sup: Support) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_once(sup)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("sweep failed: %s", type(e).__name__)
