"""The service's shared state and the views every route and socket uses."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial

import anyio.to_thread

from .config import Settings
from .discord import DiscordWebhook
from .hub import Conn, Hub
from .mail import Mailer
from .ratelimit import RateLimiter
from .store import Store, first_name
from .turnstile import Turnstile

log = logging.getLogger("support")

# WebSocket close codes the client acts on (it stops reconnecting for 44xx).
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403
WS_ENDED = 4410
WS_RATE_LIMITED = 4429


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def customer_ticket_view(t: dict) -> dict:
    status = t["effective_status"]
    return {
        "reference": t["reference"],
        "status": status,
        "customer_name": t["customer_name"],
        # The customer sees their agent's first name, nothing else about them.
        "agent": {"name": first_name(t["agent_name"])} if t.get("agent_name") and status in ("CLAIMED", "RESOLVED") else None,
        "created_at": iso(t["created_at"]),
        "access_expires_at": iso(t["access_expires_at"]),
        "can_send": status == "CLAIMED",
        "last_seq": t.get("last_seq", 0),
    }


def agent_ticket_view(t: dict, agent_id: uuid.UUID) -> dict:
    """The agent's view. Customer details only while this agent holds the open ticket."""
    status = t["effective_status"]
    mine = t["assigned_agent_id"] == agent_id
    view = {
        "reference": t["reference"],
        "status": status,
        "assigned_to_you": mine,
        "claimable": status == "PENDING",
        "created_at": iso(t["created_at"]),
        "access_expires_at": iso(t["access_expires_at"]),
    }
    if mine and status == "CLAIMED":
        view.update(
            customer={"name": t["customer_name"], "email": t["customer_email"], "phone": t["customer_phone"]},
            agent_name=t["agent_name"],
            claimed_at=iso(t["claimed_at"]),
            can_send=bool(t.get("agent_active")),
            last_seq=t.get("last_seq", 0),
        )
    return view


def message_view(m: dict, role: str) -> dict:
    sender = m["sender_type"].lower()
    author = None
    if sender == "agent":
        author = first_name(m.get("agent_name")) if role == "customer" else m.get("agent_name")
    return {"id": str(m["id"]), "seq": m["seq"], "sender": sender, "author": author, "body": m["message_content"], "created_at": iso(m["created_at"])}


@dataclass
class Support:
    settings: Settings
    store: Store
    turnstile: Turnstile
    discord: DiscordWebhook
    mailer: Mailer
    hub: Hub = field(default_factory=Hub)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    outbox_wake: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: set = field(default_factory=set)

    async def db(self, fn, *args, **kwargs):
        """Run a blocking store call on a worker thread."""
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

    def background(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def portal_url(self, ref: str) -> str:
        return f"{self.settings.public_url}/support/agent/ticket/{ref}"

    def return_url(self, ref: str, token: str) -> str:
        # The token rides in the fragment: browsers never send it to a server, a log or a Referer.
        return f"{self.settings.public_url}/support/ticket/{ref}#k={token}"

    async def ticket_changed(self, ticket_id: uuid.UUID) -> None:
        """Push the ticket's new state to its sockets, then close the ones that may no longer stay."""
        t = await self.db(self.store.ticket, ticket_id)
        if not t:
            return

        def view(conn: Conn) -> dict:
            v = customer_ticket_view(t) if conn.role == "customer" else agent_ticket_view(t, conn.agent_id)
            return {"type": "ticket", "ticket": v}

        await self.hub.publish(ticket_id, view)
        # Messages written with the change (the "agent joined" line, the closing line).
        await self.push_new_messages(ticket_id)
        if t["effective_status"] not in ("PENDING", "CLAIMED"):
            await self.hub.close_ticket(ticket_id, WS_ENDED, "ticket ended")
        else:
            for conn in self.hub.conns(ticket_id):
                if conn.role == "agent" and t["assigned_agent_id"] != conn.agent_id:
                    await conn.close(WS_FORBIDDEN, "not your ticket")
        self.outbox_wake.set()

    async def push_new_messages(self, ticket_id: uuid.UUID) -> None:
        """Send each socket what it has not seen yet, from Postgres. Clients drop duplicates by seq."""
        conns = self.hub.conns(ticket_id)
        if not conns:
            return
        since = min(c.last_seq for c in conns)
        rows, _ = await self.db(self.store.messages, ticket_id, after=since, limit=200)
        for conn in conns:
            for m in rows:
                if m["seq"] > conn.last_seq:
                    await conn.send({"type": "message", "message": message_view(m, conn.role)})
                    conn.last_seq = m["seq"]
