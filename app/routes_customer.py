"""The customer's side: create a ticket, read it, and reopen it from the return link."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import ACCESS_DAYS, CODE_TTL_MINUTES, EMAIL_MAX, MESSAGE_MAX, NAME_MAX, QUERY_MAX
from .core import Support, customer_ticket_view, message_view
from .http import CUSTOMER_COOKIE, ApiError, browser_post_guard, client_ip, customer_cookie_path, set_session_cookie
from .mail import MailError, code_mail, link_mail
from .store import OPEN, DuplicateRequest
from .tokens import digest, new_code, new_token, normalize_reference
from .turnstile import CaptchaError, CaptchaMissing, CaptchaUnavailable
from .validation import InvalidInput, clean_text, mask_email, normalize_email, normalize_phone

log = logging.getLogger("support.customer")

# A replayed submit (same idempotency key) gets its ticket back only this soon after creating it.
REPLAY_WINDOW = timedelta(hours=1)


class TicketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turnstile_token: str = Field(default="", max_length=4096)
    idempotency_key: uuid.UUID
    name: str = Field(default="", max_length=1000)
    email: str | None = Field(default=None, max_length=1000)
    phone: str | None = Field(default=None, max_length=100)
    phone_country: str | None = Field(default=None, max_length=2)
    message: str = Field(default="", max_length=20000)


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    code: str | None = Field(default=None, max_length=20)


def router(support_of, settings) -> APIRouter:
    r = APIRouter(prefix="/api/support")
    post_guard = Depends(browser_post_guard(settings))

    def limit(sup: Support, key: str, n: int, window: int) -> None:
        ok, retry = sup.limiter.hit(key, n, window)
        if not ok:
            raise ApiError(429, "rate_limited", f"Too many attempts. Please try again in {retry} seconds.", headers={"Retry-After": str(retry)})

    async def customer_session(sup: Support, request: Request, ref: str, *, require_open: bool = False) -> tuple[dict, dict]:
        ref = normalize_reference(ref)
        if not ref:
            raise ApiError(404, "not_found", "No such ticket.")
        token = request.cookies.get(CUSTOMER_COOKIE, "")
        s = await sup.db(sup.store.session, digest(token), "customer") if token else None
        if not s or s["reference"] != ref:
            raise ApiError(401, "no_session", "This conversation is not open in this browser.")
        t = await sup.db(sup.store.ticket, s["ticket_id"])
        if require_open and t["effective_status"] not in OPEN:
            raise ApiError(410, "ticket_closed", "This conversation has ended.", status=t["effective_status"])
        return s, t

    @r.get("/config")
    async def config(request: Request):
        sup: Support = support_of(request)
        return {
            "turnstile_site_key": sup.settings.turnstile_site_key or None,
            "captcha": sup.turnstile.enabled,
            "access_days": ACCESS_DAYS,
            "support_email": sup.settings.support_email,
            "limits": {"name": NAME_MAX, "email": EMAIL_MAX, "query": QUERY_MAX, "message": MESSAGE_MAX},
        }

    @r.post("/tickets", status_code=201, dependencies=[post_guard])
    async def create_ticket(body: TicketIn, request: Request, response: Response):
        sup: Support = support_of(request)
        ip = client_ip(request)
        # A loose cap on every attempt; the strict one (below) counts only submits that would create a ticket,
        # so a visitor fixing typos is never locked out.
        limit(sup, f"ticket-any:{ip}", 30, 15 * 60)

        fields: dict[str, str] = {}
        name = email = phone = message = None
        try:
            name = clean_text(body.name, max_len=NAME_MAX, multiline=False)
        except InvalidInput as e:
            fields["name"] = str(e) if body.name.strip() else "Tell us your name."
        if body.email and body.email.strip():
            try:
                email = normalize_email(body.email)
            except InvalidInput as e:
                fields["email"] = str(e)
        if body.phone and body.phone.strip():
            try:
                phone = normalize_phone(body.phone, body.phone_country)
            except InvalidInput as e:
                fields["phone"] = str(e)
        if not (body.email or "").strip() and not (body.phone or "").strip():
            fields["contact"] = "Give us an email address or a phone number so we can reach you."
        try:
            message = clean_text(body.message, max_len=QUERY_MAX)
        except InvalidInput as e:
            fields["message"] = str(e) if body.message.strip() else "Tell us how we can help."
        if fields:
            raise ApiError(422, "invalid_request", "Please fix the highlighted fields.", fields=fields)

        idem_hash = digest(f"idempotency:{body.idempotency_key}")
        fingerprint = digest(json.dumps([name, email, phone, message], ensure_ascii=False))

        async def replay(existing: dict) -> dict:
            """The same submit again (a retry after a lost response): its ticket, never a second one."""
            if existing.get("request_fingerprint") != fingerprint or datetime.now(timezone.utc) - existing["created_at"] > REPLAY_WINDOW:
                raise ApiError(409, "idempotency_conflict", "This request was already used. Please reload and try again.")
            if existing["effective_status"] in OPEN:
                session = new_token()
                await sup.db(sup.store.add_session, kind="customer", ticket_id=existing["id"], token_hash=digest(session), expires_at=existing["access_expires_at"])
                set_session_cookie(response, sup.settings, name=CUSTOMER_COOKIE, token=session, path=customer_cookie_path(existing["reference"]), expires_at=existing["access_expires_at"])
            response.status_code = 200
            return {"ticket": customer_ticket_view(existing), "return_url": None, "link_emailed": False, "replayed": True}

        existing = await sup.db(sup.store.find_idempotent, idem_hash)
        if existing:
            return await replay(existing)

        limit(sup, f"ticket:{ip}", 5, 15 * 60)
        limit(sup, f"ticket-day:{ip}", 20, 24 * 3600)
        try:
            await sup.turnstile.verify(body.turnstile_token, ip)
        except CaptchaError as e:
            if isinstance(e, CaptchaUnavailable):
                log.error("captcha check unavailable: %s", e)
                raise ApiError(503, e.code, "The security check is unavailable right now. Please try again in a minute.") from None
            log.warning("captcha rejected ip=%s: %s", ip, e)
            msg = "Please complete the security check." if isinstance(e, CaptchaMissing) else "The security check failed or expired. Please try it again."
            raise ApiError(400, e.code, msg) from None

        link_token, session = new_token(), new_token()
        try:
            t = await sup.db(
                sup.store.create_ticket, name=name, email=email, phone=phone, query=message,
                token_hash=digest(link_token), idem_hash=idem_hash, fingerprint=fingerprint, session_hash=digest(session),
            )
        except DuplicateRequest:
            # Two copies of one submit raced; the other one created the ticket.
            existing = await sup.db(sup.store.find_idempotent, idem_hash)
            return await replay(existing)

        log.info("ticket %s created (email=%s phone=%s)", t["reference"], bool(email), bool(phone))
        set_session_cookie(response, sup.settings, name=CUSTOMER_COOKIE, token=session, path=customer_cookie_path(t["reference"]), expires_at=t["access_expires_at"])
        sup.outbox_wake.set()
        return_url = sup.return_url(t["reference"], link_token)
        emailed = bool(email and sup.mailer.enabled)
        if emailed:
            sup.background(_send_link(sup, email, t["reference"], return_url))
        return {"ticket": customer_ticket_view(t), "return_url": return_url, "link_emailed": emailed, "replayed": False}

    @r.get("/tickets/{ref}")
    async def get_ticket(ref: str, request: Request):
        sup: Support = support_of(request)
        limit(sup, f"read:{client_ip(request)}", 120, 60)
        _, t = await customer_session(sup, request, ref)
        return {"ticket": customer_ticket_view(t)}

    @r.get("/tickets/{ref}/messages")
    async def get_messages(ref: str, request: Request, after: int = Query(0, ge=0), limit_: int = Query(100, ge=1, le=200, alias="limit")):
        sup: Support = support_of(request)
        limit(sup, f"read:{client_ip(request)}", 120, 60)
        _, t = await customer_session(sup, request, ref, require_open=True)
        rows, more = await sup.db(sup.store.messages, t["id"], after=after, limit=limit_)
        return {"messages": [message_view(m, "customer") for m in rows], "has_more": more}

    @r.post("/tickets/{ref}/session", dependencies=[post_guard])
    async def open_session(ref: str, body: SessionIn, request: Request, response: Response):
        """The return link. In the browser that already holds this ticket's session: straight in.
        Anywhere else: a one-time code to the customer's email first, never the link alone."""
        sup: Support = support_of(request)
        ip = client_ip(request)
        limit(sup, f"link:{ip}", 10, 15 * 60)
        ref_n = normalize_reference(ref)
        t = await sup.db(sup.store.ticket_by_token, digest(body.token)) if ref_n else None
        if not t or t["reference"] != ref_n:
            raise ApiError(404, "invalid_link", "This link is not valid. Check that you copied all of it.")
        if t["effective_status"] not in OPEN:
            raise ApiError(410, "ticket_closed", "This conversation has ended.", status=t["effective_status"])

        cookie = request.cookies.get(CUSTOMER_COOKIE, "")
        s = await sup.db(sup.store.session, digest(cookie), "customer") if cookie else None
        if s and s["ticket_id"] == t["id"]:
            return {"ticket": customer_ticket_view(t)}

        if body.code is None:
            if not t["customer_email"] or not sup.mailer.enabled:
                raise ApiError(
                    403, "verification_unavailable",
                    "For your security this conversation opens only in the browser where it was started, because we cannot "
                    f"send you a code here. Open the link in that browser, or email {sup.settings.support_email} with your ticket number {t['reference']}.",
                    reference=t["reference"],
                )
            if await sup.db(sup.store.codes_sent_since, t["id"], 15 * 60) >= 3:
                raise ApiError(429, "rate_limited", "We have sent several codes already. Please check your inbox, or try again in 15 minutes.")
            code = new_code()
            await sup.db(sup.store.create_code, t["id"], code)
            try:
                await sup.db(sup.mailer.send, t["customer_email"], *code_mail(t["reference"], code, CODE_TTL_MINUTES))
            except MailError as e:
                log.error("code mail for %s not sent: %s", t["reference"], e)
                raise ApiError(503, "mail_unavailable", "We could not send the code just now. Please try again in a minute.") from None
            return JSONResponse({"verification": "email_code", "email_hint": mask_email(t["customer_email"]), "minutes": CODE_TTL_MINUTES}, status_code=202)

        code = body.code.strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ApiError(400, "invalid_code", "Enter the 6-digit code from the email.")
        session = new_token()
        outcome, left = await sup.db(sup.store.redeem_code, t["id"], code, session_hash=digest(session), session_expires=t["access_expires_at"])
        if outcome == "ok":
            log.info("ticket %s opened in a new browser after the email code", t["reference"])
            set_session_cookie(response, sup.settings, name=CUSTOMER_COOKIE, token=session, path=customer_cookie_path(t["reference"]), expires_at=t["access_expires_at"])
            return {"ticket": customer_ticket_view(t)}
        if outcome == "invalid":
            raise ApiError(400, "invalid_code", "That code is not right. Check the email and try again.", attempts_left=left)
        if outcome == "locked":
            raise ApiError(429, "code_locked", "Too many wrong codes. Ask for a new code.")
        raise ApiError(400, "code_expired", "That code has expired. Ask for a new one.")

    return r


async def _send_link(sup: Support, email: str, ref: str, return_url: str) -> None:
    try:
        await sup.db(sup.mailer.send, email, *link_mail(ref, return_url, sup.settings.support_email))
    except MailError as e:
        log.warning("return-link mail for %s not sent: %s", ref, e)
