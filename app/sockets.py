"""The live conversation over WebSockets. Same cookies, same rules as the HTTP routes, checked at
connect, again in SQL on every message, and again by the periodic sweep for sockets left open.

Client -> server:  {"type": "message", "client_id": "<uuid>", "body": "..."}  |  {"type": "ping"}
                   {"type": "typing", "typing": true|false}
                   {"type": "receipt", "delivered": <seq>, "read": <seq>}   (how far this side has got)
Server -> client:  ready (ticket) | message | ack (client_id, message) | ticket | error | pong
                   typing (sender, typing) | receipts (the other side's delivered/read seq)
Typing and receipts are best-effort extras: never stored as messages, rate-limited, and silently
dropped when over the limit, so a chatty client can slow neither the chat nor the database.
Close codes 44xx mean "do not reconnect": 4401 no session, 4403 not yours, 4410 ended, 4429 too many.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from .config import MESSAGE_MAX
from .core import WS_ENDED, WS_FORBIDDEN, WS_RATE_LIMITED, WS_UNAUTHORIZED, Support, agent_ticket_view, customer_ticket_view, message_view
from .http import AGENT_COOKIE, CUSTOMER_COOKIE, client_ip
from .hub import Conn
from .store import OPEN
from .tokens import digest, normalize_reference
from .validation import InvalidInput, clean_text

log = logging.getLogger("support.ws")

MAX_FRAME = 16 * 1024
BACKLOG_PAGES = 10


def router(support_of) -> APIRouter:
    r = APIRouter()

    @r.websocket("/api/support/tickets/{ref}/ws")
    async def customer_ws(ws: WebSocket, ref: str, after: int = 0):
        await serve(support_of(ws), ws, ref, "customer", after)

    @r.websocket("/api/support/agent/tickets/{ref}/ws")
    async def agent_ws(ws: WebSocket, ref: str, after: int = 0):
        await serve(support_of(ws), ws, ref, "agent", after)

    return r


def _view(t: dict, conn_role: str, agent_id: uuid.UUID | None) -> dict:
    return customer_ticket_view(t) if conn_role == "customer" else agent_ticket_view(t, agent_id)


async def serve(sup: Support, ws: WebSocket, ref: str, role: str, after: int) -> None:
    # A page on another site must not be able to open a socket with our visitors' cookies.
    origin = ws.headers.get("origin", "").rstrip("/")
    if origin not in sup.settings.origins:
        await ws.close(code=WS_FORBIDDEN)
        return
    ok, _ = sup.limiter.hit(f"ws:{client_ip(ws)}", 60, 60)
    if not ok:
        await ws.close(code=WS_RATE_LIMITED)
        return
    await ws.accept()

    ref_n = normalize_reference(ref)
    token = ws.cookies.get(CUSTOMER_COOKIE if role == "customer" else AGENT_COOKIE, "")
    s = await sup.db(sup.store.session, digest(token), role) if (token and ref_n) else None
    if not s or s["reference"] != ref_n:
        await ws.close(code=WS_UNAUTHORIZED, reason="no session")
        return
    t = await sup.db(sup.store.ticket, s["ticket_id"])
    if role == "agent" and (t["assigned_agent_id"] != s["agent_id"] or not t["agent_active"]):
        await ws.close(code=WS_FORBIDDEN, reason="not your ticket")
        return
    if t["effective_status"] not in (OPEN if role == "customer" else ("CLAIMED",)):
        await ws.send_json({"type": "ticket", "ticket": _view(t, role, s["agent_id"])})
        await ws.close(code=WS_ENDED, reason="ticket ended")
        return

    conn = Conn(ws=ws, role=role, ticket_id=t["id"], session_id=s["session_id"], agent_id=s["agent_id"], last_seq=max(0, after))
    if not sup.hub.join(conn):
        await ws.close(code=WS_RATE_LIMITED, reason="too many connections")
        return
    try:
        await conn.send({"type": "ready", "ticket": _view(t, role, s["agent_id"])})
        # What the client missed while away, from Postgres. Live pushes may repeat some; seq dedupes.
        for _ in range(BACKLOG_PAGES):
            rows, more = await sup.db(sup.store.messages, t["id"], after=conn.last_seq, limit=200)
            for m in rows:
                await conn.send({"type": "message", "message": message_view(m, role)})
                conn.last_seq = max(conn.last_seq, m["seq"])
            if not more:
                break
        await _receive(sup, conn)
    except WebSocketDisconnect:
        pass
    finally:
        sup.hub.leave(conn)


async def _receive(sup: Support, conn: Conn) -> None:
    while True:
        msg = await conn.ws.receive()
        if msg["type"] == "websocket.disconnect":
            return
        text = msg.get("text")
        if text is None or len(text) > MAX_FRAME:
            await conn.send({"type": "error", "code": "bad_frame", "message": "Send JSON text frames."})
            continue
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            await conn.send({"type": "error", "code": "bad_frame", "message": "Send JSON objects."})
            continue
        kind = data.get("type")
        if kind == "ping":
            await conn.send({"type": "pong"})
        elif kind == "message":
            if await _message(sup, conn, data) is False:
                return
        elif kind == "typing":
            await _typing(sup, conn, data)
        elif kind == "receipt":
            await _receipt(sup, conn, data)
        else:
            await conn.send({"type": "error", "code": "unknown_type", "message": "Unknown message type."})


async def _typing(sup: Support, conn: Conn, data: dict) -> None:
    """Relay "is typing" to the other side's sockets only. Nothing is stored. A client sends it every
    few seconds while someone types; more than that is dropped."""
    ok, _ = sup.limiter.hit(f"typing:{conn.session_id}", 20, 20)
    if not ok:
        return
    event = {"type": "typing", "sender": conn.role, "typing": data.get("typing") is True}
    await sup.hub.publish(conn.ticket_id, lambda other: event if other.role != conn.role else None)


def _seq(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


async def _receipt(sup: Support, conn: Conn, data: dict) -> None:
    """This side has received (delivered) and seen (read) the conversation up to a seq. A client can
    only vouch for what this socket was actually sent, so both are capped at conn.last_seq."""
    delivered, read = _seq(data.get("delivered")), _seq(data.get("read"))
    if delivered is None and read is None:
        return
    read = min(read or 0, conn.last_seq)
    delivered = max(min(delivered or 0, conn.last_seq), read)
    if delivered <= conn.receipt[0] and read <= conn.receipt[1]:
        return
    ok, _ = sup.limiter.hit(f"receipt:{conn.session_id}", 60, 30)
    if not ok:
        return
    conn.receipt = (max(delivered, conn.receipt[0]), max(read, conn.receipt[1]))
    cursors = await sup.db(sup.store.record_receipt, conn.ticket_id, conn.role, delivered=delivered, read=read)
    if cursors:
        await sup.publish_receipts(conn.ticket_id, cursors, changed_by=conn.role)


async def _message(sup: Support, conn: Conn, data: dict) -> bool | None:
    """Store, then acknowledge, then deliver. Returns False when the socket was closed."""
    try:
        client_id = uuid.UUID(str(data.get("client_id")))
    except ValueError:
        await conn.send({"type": "error", "code": "invalid_message", "message": "Missing message id."})
        return None
    try:
        body = clean_text(str(data.get("body") or ""), max_len=MESSAGE_MAX)
    except InvalidInput as e:
        await conn.send({"type": "error", "code": "invalid_message", "client_id": str(client_id), "message": str(e)})
        return None
    ok, retry = sup.limiter.hit(f"msg:{conn.session_id}", 20, 30)
    if not ok:
        await conn.send({"type": "error", "code": "rate_limited", "client_id": str(client_id), "message": f"You're sending messages too quickly. Try again in {retry} seconds."})
        return None

    sender = "CUSTOMER" if conn.role == "customer" else "AGENT"
    outcome, row = await sup.db(
        sup.store.insert_message, ticket_id=conn.ticket_id, session_id=conn.session_id, sender=sender,
        agent_id=conn.agent_id if sender == "AGENT" else None, body=body, client_id=client_id,
    )
    if outcome in ("ok", "duplicate"):
        # Only now, with the row committed, does the sender hear "sent".
        await conn.send({"type": "ack", "client_id": str(client_id), "message": message_view(row, conn.role)})
        if outcome == "ok":
            await sup.push_new_messages(conn.ticket_id)
        return None

    t = await sup.db(sup.store.ticket, conn.ticket_id)
    status = t["effective_status"]
    await conn.send({"type": "ticket", "ticket": _view(t, conn.role, conn.agent_id)})
    if conn.role == "customer" and status == "PENDING":
        await conn.send({"type": "error", "code": "not_claimed", "client_id": str(client_id), "message": "You can send messages once an agent has joined."})
        return None
    await conn.send({"type": "error", "code": "not_allowed", "client_id": str(client_id), "message": "This conversation is closed to new messages."})
    if status not in OPEN:
        await conn.close(WS_ENDED, "ticket ended")
    elif conn.role == "agent" and t["assigned_agent_id"] != conn.agent_id:
        await conn.close(WS_FORBIDDEN, "not your ticket")
    else:  # the ticket is open, but this session or the agent's access went away
        await conn.close(WS_UNAUTHORIZED, "session ended")
    return False
