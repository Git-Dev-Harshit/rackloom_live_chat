"""Administration for the web app's /admin/support page, over the private Docker network only.

Never routed by Caddy (it forwards /api/support/* and nothing else), and every call needs
X-Internal-Token = SUPPORT_INTERNAL_TOKEN. With no token configured, every call is refused.
"""
from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from .core import Support, iso
from .http import ApiError
from .store import AgentExists
from .tokens import normalize_reference
from .validation import InvalidInput, clean_name, normalize_phone
from .config import NAME_MAX


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=500)
    phone: str = Field(max_length=40)
    phone_country: str | None = Field(default=None, max_length=2)


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=40)
    phone_country: str | None = Field(default=None, max_length=2)
    is_active: bool | None = None


def agent_out(a: dict) -> dict:
    return {
        "id": str(a["id"]), "name": a["agent_name"], "phone": a["normalized_phone_number"], "is_active": a["is_active"],
        "open_tickets": a.get("open_tickets"), "created_at": iso(a["created_at"]), "updated_at": iso(a["updated_at"]),
    }


def ticket_out(t: dict) -> dict:
    return {
        "reference": t["reference"], "status": t["effective_status"], "agent_name": t["agent_name"],
        "created_at": iso(t["created_at"]), "claimed_at": iso(t["claimed_at"]), "resolved_at": iso(t["resolved_at"]),
        "closed_at": iso(t["closed_at"]), "last_message_at": iso(t["last_message_at"]), "access_expires_at": iso(t["access_expires_at"]),
        "message_count": t["message_count"],
        "escalation_count": t.get("escalation_count") or 0, "last_escalated_at": iso(t.get("last_escalated_at")),
        "discord": {"status": t["discord_status"], "attempts": t["discord_attempts"], "error": t["discord_error"]},
    }


def router(support_of, settings) -> APIRouter:
    def internal_auth(x_internal_token: str | None = Header(default=None)) -> None:
        if not settings.internal_token or not x_internal_token or not hmac.compare_digest(x_internal_token, settings.internal_token):
            raise ApiError(401, "unauthorized", "unauthorized")

    r = APIRouter(prefix="/internal", dependencies=[Depends(internal_auth)])

    def fields_from(name: str | None, phone: str | None, country: str | None) -> tuple[str | None, str | None]:
        errors: dict[str, str] = {}
        name_ok = e164 = None
        if name is not None:
            try:
                name_ok = clean_name(name, max_len=NAME_MAX)
            except InvalidInput as e:
                errors["name"] = str(e)
        if phone is not None:
            try:
                e164 = normalize_phone(phone, country)
            except InvalidInput as e:
                errors["phone"] = str(e)
        if errors:
            raise ApiError(422, "invalid_request", "Please check the highlighted fields.", fields=errors)
        return name_ok, e164

    @r.get("/overview")
    async def overview(request: Request, status: str | None = None, limit: int = 50):
        sup: Support = support_of(request)
        if status and status not in ("PENDING", "CLAIMED", "RESOLVED", "CLOSED", "EXPIRED"):
            raise ApiError(422, "invalid_request", "Unknown status.")
        counts = await sup.db(sup.store.counts)
        agents = await sup.db(sup.store.list_agents)
        tickets = await sup.db(sup.store.list_tickets, limit=max(1, min(limit, 200)), status=status)
        return {
            "counts": counts,
            "integrations": {"captcha": sup.turnstile.enabled, "discord": sup.discord.enabled, "mail": sup.mailer.kind},
            "agents": [agent_out(a) for a in agents],
            "tickets": [ticket_out(t) for t in tickets],
        }

    @r.post("/agents", status_code=201)
    async def create_agent(body: AgentIn, request: Request):
        sup: Support = support_of(request)
        name, e164 = fields_from(body.name, body.phone, body.phone_country)
        try:
            return {"agent": agent_out(await sup.db(sup.store.create_agent, name, e164))}
        except AgentExists:
            raise ApiError(409, "agent_exists", "An agent with this phone number already exists.", fields={"phone": "Already registered."}) from None

    @r.patch("/agents/{agent_id}")
    async def update_agent(agent_id: uuid.UUID, body: AgentPatch, request: Request):
        sup: Support = support_of(request)
        name, e164 = fields_from(body.name, body.phone, body.phone_country)
        try:
            row = await sup.db(sup.store.update_agent, agent_id, name=name, e164=e164, active=body.is_active)
        except AgentExists:
            raise ApiError(409, "agent_exists", "An agent with this phone number already exists.", fields={"phone": "Already registered."}) from None
        if not row:
            raise ApiError(404, "not_found", "No such agent.")
        if body.is_active is False or e164 is not None:
            # Their open sockets go now, not at the next sweep.
            for conn in sup.hub.conns():
                if conn.agent_id == agent_id:
                    await conn.close(4403, "access changed")
        return {"agent": agent_out(row)}

    @r.post("/tickets/{ref}/notifications/retry")
    async def retry_notifications(ref: str, request: Request):
        sup: Support = support_of(request)
        ref_n = normalize_reference(ref)
        if not ref_n:
            raise ApiError(404, "not_found", "No such ticket.")
        n = await sup.db(sup.store.retry_failed_notifications, ref_n)
        sup.outbox_wake.set()
        return {"retried": n}

    return r
