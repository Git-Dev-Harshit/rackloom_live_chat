"""The agent's side: the phone check, claiming, the ticket and its messages, resolving and closing.

Phone-number matching is identification, not authentication: a phone number is not a secret. It is
the requested first version, kept behind rate limits and generic answers. `second_factor` is the one
place a PIN or an SMS/email OTP slots in later without changing the routes or the session model.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import AGENT_SESSION_HOURS
from .core import Support, agent_ticket_view, message_view
from .http import AGENT_COOKIE, ApiError, agent_cookie_path, browser_post_guard, client_ip, set_session_cookie
from .tokens import digest, new_token, normalize_reference
from .validation import InvalidInput, mask_phone, normalize_phone

log = logging.getLogger("support.agent")

NOT_VERIFIED = "We couldn't verify that number for this ticket. Check it and try again."
FAILS_PER_IP = 10  # wrong numbers from one address per 15 minutes
FAILS_PER_TICKET = 20  # wrong numbers against one ticket per hour, from anywhere
TAKEN = "This ticket has already been claimed and is no longer available."


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(max_length=32)
    phone: str = Field(max_length=40)
    phone_country: str | None = Field(default=None, max_length=2)


async def second_factor(agent: dict, request: Request) -> bool:
    """Extension point: check a PIN or a one-time code for `agent` here. None in the first version."""
    return True


def router(support_of, settings) -> APIRouter:
    r = APIRouter(prefix="/api/support/agent")
    post_guard = Depends(browser_post_guard(settings))

    def limit(sup: Support, key: str, n: int, window: int) -> None:
        ok, retry = sup.limiter.hit(key, n, window)
        if not ok:
            raise ApiError(429, "rate_limited", f"Too many attempts. Please try again in {retry} seconds.", headers={"Retry-After": str(retry)})

    async def agent_session(sup: Support, request: Request, ref: str) -> tuple[dict, dict]:
        """The agent this browser proved to be, for this one ticket. Nothing the browser sends names the agent."""
        ref = normalize_reference(ref)
        if not ref:
            raise ApiError(404, "not_found", "No such ticket.")
        token = request.cookies.get(AGENT_COOKIE, "")
        s = await sup.db(sup.store.session, digest(token), "agent") if token else None
        if not s or s["reference"] != ref:
            raise ApiError(401, "no_session", "Enter your registered support phone number to continue.")
        t = await sup.db(sup.store.ticket, s["ticket_id"])
        agent = await sup.db(sup.store.agent, s["agent_id"])
        if not agent or not agent["is_active"]:
            raise ApiError(403, "agent_inactive", "Your support access is switched off. Ask an administrator.")
        s["agent_name"] = agent["agent_name"]
        return s, t

    def held(t: dict, s: dict) -> None:
        """The ticket is this agent's and still open, or the request is refused."""
        if t["assigned_agent_id"] != s["agent_id"]:
            if t["effective_status"] == "PENDING":
                raise ApiError(409, "not_claimed", "Claim this ticket first.")
            raise ApiError(403, "claimed_by_other", TAKEN)
        if t["effective_status"] != "CLAIMED":
            raise ApiError(410, "ticket_closed", "This ticket has ended.", status=t["effective_status"])

    @r.post("/verify", dependencies=[post_guard])
    async def verify(body: VerifyIn, request: Request, response: Response):
        sup: Support = support_of(request)
        ip = client_ip(request)
        # Guessing shows up as failures, so failures are what is limited (per address and per ticket);
        # agents sharing an office address verify freely. A loose cap bounds everything else.
        limit(sup, f"agent-verify:{ip}", 60, 15 * 60)
        ref = normalize_reference(body.reference)
        if not ref:
            raise ApiError(422, "invalid_request", "Please check the highlighted fields.", fields={"reference": "This ticket number is not valid."})
        for key, n in ((f"agent-verify-fail:{ip}", FAILS_PER_IP), (f"agent-verify-fail:{ref}", FAILS_PER_TICKET)):
            stop, retry = sup.limiter.blocked(key, n)
            if stop:
                raise ApiError(429, "rate_limited", f"Too many attempts. Please try again in {retry} seconds.", headers={"Retry-After": str(retry)})
        try:
            e164 = normalize_phone(body.phone, body.phone_country)
        except InvalidInput as e:
            raise ApiError(422, "invalid_request", "Please check the highlighted fields.", fields={"phone": str(e)}) from None

        t = await sup.db(sup.store.ticket_by_ref, ref)
        agent = await sup.db(sup.store.agent_by_phone, e164)
        # One answer for an unknown ticket, an unknown number and a switched-off agent alike.
        if not t or not agent or not agent["is_active"] or not await second_factor(agent, request):
            sup.limiter.hit(f"agent-verify-fail:{ip}", FAILS_PER_IP, 15 * 60)
            sup.limiter.hit(f"agent-verify-fail:{ref}", FAILS_PER_TICKET, 3600)
            log.info("agent verification refused ticket=%s phone=%s ip=%s", ref, mask_phone(e164), ip)
            raise ApiError(401, "not_verified", NOT_VERIFIED)
        status = t["effective_status"]
        if status not in ("PENDING", "CLAIMED"):
            raise ApiError(410, "ticket_closed", "This ticket has ended.", status=status)
        if status == "CLAIMED" and t["assigned_agent_id"] != agent["id"]:
            raise ApiError(409, "already_claimed", TAKEN)

        token = new_token()
        expires = min(datetime.now(timezone.utc) + timedelta(hours=AGENT_SESSION_HOURS), t["access_expires_at"])
        await sup.db(sup.store.add_session, kind="agent", ticket_id=t["id"], token_hash=digest(token), expires_at=expires, agent_id=agent["id"])
        set_session_cookie(response, sup.settings, name=AGENT_COOKIE, token=token, path=agent_cookie_path(ref), expires_at=expires)
        log.info("agent %s verified for ticket %s", agent["id"], ref)
        return {"agent": {"name": agent["agent_name"]}, "ticket": agent_ticket_view(t, agent["id"])}

    @r.post("/tickets/{ref}/claim", dependencies=[post_guard])
    async def claim(ref: str, request: Request):
        sup: Support = support_of(request)
        s, t = await agent_session(sup, request, ref)
        outcome = await sup.db(sup.store.claim, t["id"], s["agent_id"])
        if outcome == "claimed":
            log.info("ticket %s claimed by agent %s", t["reference"], s["agent_id"])
            await sup.ticket_changed(t["id"])
        if outcome in ("claimed", "already_yours"):
            fresh = await sup.db(sup.store.ticket, t["id"])
            return {"ticket": agent_ticket_view(fresh, s["agent_id"]), "claimed_now": outcome == "claimed"}
        if outcome == "taken":
            raise ApiError(409, "already_claimed", TAKEN)
        if outcome in ("expired", "ended"):
            raise ApiError(410, "ticket_closed", "This ticket has ended and can no longer be claimed.", status=t["effective_status"])
        if outcome == "agent_inactive":
            raise ApiError(403, "agent_inactive", "Your support access is switched off. Ask an administrator.")
        raise ApiError(404, "not_found", "No such ticket.")

    @r.get("/tickets/{ref}")
    async def get_ticket(ref: str, request: Request):
        sup: Support = support_of(request)
        s, t = await agent_session(sup, request, ref)
        if t["assigned_agent_id"] and t["assigned_agent_id"] != s["agent_id"]:
            raise ApiError(403, "claimed_by_other", TAKEN)
        return {"agent": {"name": s["agent_name"]}, "ticket": agent_ticket_view(t, s["agent_id"])}

    @r.get("/tickets/{ref}/messages")
    async def get_messages(ref: str, request: Request, after: int = Query(0, ge=0), limit_: int = Query(100, ge=1, le=200, alias="limit")):
        sup: Support = support_of(request)
        s, t = await agent_session(sup, request, ref)
        held(t, s)
        rows, more = await sup.db(sup.store.messages, t["id"], after=after, limit=limit_)
        return {"messages": [message_view(m, "agent") for m in rows], "has_more": more}

    async def transition(request: Request, ref: str, to: str):
        sup: Support = support_of(request)
        s, t = await agent_session(sup, request, ref)
        held(t, s)
        outcome = await sup.db(sup.store.agent_transition, t["id"], s["agent_id"], to)
        if outcome == "done":
            log.info("ticket %s %s by agent %s", t["reference"], to.lower(), s["agent_id"])
            await sup.ticket_changed(t["id"])
            fresh = await sup.db(sup.store.ticket, t["id"])
            return {"ticket": agent_ticket_view(fresh, s["agent_id"])}
        if outcome == "not_yours":
            raise ApiError(403, "claimed_by_other", TAKEN)
        raise ApiError(410, "ticket_closed", "This ticket has ended.")

    @r.post("/tickets/{ref}/resolve", dependencies=[post_guard])
    async def resolve(ref: str, request: Request):
        return await transition(request, ref, "RESOLVED")

    @r.post("/tickets/{ref}/close", dependencies=[post_guard])
    async def close(ref: str, request: Request):
        return await transition(request, ref, "CLOSED")

    return r
