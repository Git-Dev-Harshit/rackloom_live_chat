"""Open chat sockets, by ticket. In memory, so it serves one process: the service runs as a single
uvicorn worker. A second worker or host would not see the first one's sockets; fan-out would then
have to go through Redis pub/sub or Postgres LISTEN/NOTIFY (docs/SUPPORT.md, "Scaling out").
PostgreSQL stays the record either way: a client that misses a push reads it back on reconnect.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

from starlette.websockets import WebSocket, WebSocketState

log = logging.getLogger("support.hub")


@dataclass(eq=False)
class Conn:
    ws: WebSocket
    role: str  # "customer" | "agent"
    ticket_id: uuid.UUID
    session_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    # The newest message this socket has been sent; pushes resume after it.
    last_seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, event: dict) -> bool:
        if self.ws.application_state != WebSocketState.CONNECTED:
            return False
        try:
            async with self.lock:
                await self.ws.send_json(event)
            return True
        except Exception:  # noqa: BLE001 - a dead socket is removed by its own receive loop
            return False

    async def close(self, code: int, reason: str = "") -> None:
        if self.ws.application_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001
                pass


Event = dict | Callable[[Conn], dict | None]


class Hub:
    def __init__(self, max_per_ticket: int = 12):
        self._rooms: dict[uuid.UUID, set[Conn]] = {}
        self.max_per_ticket = max_per_ticket

    def join(self, conn: Conn) -> bool:
        room = self._rooms.setdefault(conn.ticket_id, set())
        if len(room) >= self.max_per_ticket:
            return False
        room.add(conn)
        return True

    def leave(self, conn: Conn) -> None:
        room = self._rooms.get(conn.ticket_id)
        if room is not None:
            room.discard(conn)
            if not room:
                self._rooms.pop(conn.ticket_id, None)

    def conns(self, ticket_id: uuid.UUID | None = None) -> list[Conn]:
        if ticket_id is not None:
            return list(self._rooms.get(ticket_id, ()))
        return [c for room in self._rooms.values() for c in room]

    async def publish(self, ticket_id: uuid.UUID, event: Event, *, exclude: Conn | None = None) -> None:
        """Send to every socket on this ticket only. `event` may be a function of the connection,
        so customers and agents each get their own view of the same change."""
        for conn in self.conns(ticket_id):
            if conn is exclude:
                continue
            payload = event(conn) if callable(event) else event
            if payload is not None:
                await conn.send(payload)

    async def close_ticket(self, ticket_id: uuid.UUID, code: int, reason: str = "") -> None:
        for conn in self.conns(ticket_id):
            await conn.close(code, reason)
