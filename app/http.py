"""HTTP plumbing: one error shape, the client's address, the browser-only guard on state-changing
requests, session cookies, a request-size limit and the response headers."""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import BODY_LIMIT_BYTES, Settings

log = logging.getLogger("support.http")

CUSTOMER_COOKIE = "rl_support"
AGENT_COOKIE = "rl_support_agent"


def customer_cookie_path(ref: str) -> str:
    return f"/api/support/tickets/{ref}"


def agent_cookie_path(ref: str) -> str:
    return f"/api/support/agent/tickets/{ref}"


class ApiError(Exception):
    """An answer for the client: HTTP status, a stable code, a sentence for people, and extra fields
    (a ticket's `status`, form `fields`) merged into the JSON body."""

    def __init__(self, http_status: int, code: str, message: str, *, headers: dict | None = None, **extra):
        super().__init__(message)
        self.http_status, self.code, self.message, self.headers, self.extra = http_status, code, message, headers, extra


def _error(http_status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=http_status, headers=headers)


_FRIENDLY = {
    "missing": "This field is required.",
    "string_too_long": "This is too long.",
    "string_too_short": "This is too short.",
    "uuid_parsing": "This value is not valid.",
    "uuid_type": "This value is not valid.",
    "int_parsing": "This value is not valid.",
    "extra_forbidden": "Unexpected field.",
    "json_invalid": "The request body is not valid JSON.",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_req: Request, e: ApiError):
        return _error(e.http_status, e.code, e.message, e.headers, **e.extra)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_req: Request, e: RequestValidationError):
        # The submitted values are never echoed back (they may hold a token or a phone number).
        fields: dict[str, str] = {}
        for err in e.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            key = loc[-1] if loc else "body"
            fields.setdefault(key, _FRIENDLY.get(err.get("type", ""), "This value is not valid."))
        return _error(422, "invalid_request", "Please check the highlighted fields.", fields=fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, e: StarletteHTTPException):
        code, message = {
            404: ("not_found", "Not found."),
            413: ("too_large", "This request is too large."),
        }.get(e.status_code, ("error", "This request is not allowed."))
        return _error(e.status_code, code, message)

    @app.exception_handler(Exception)
    async def _crash(req: Request, e: Exception):
        log.error("unhandled error on %s %s: %s", req.method, req.url.path, type(e).__name__, exc_info=e)
        return _error(500, "internal", "Something went wrong on our side. Please try again.")


def client_ip(conn: HTTPConnection) -> str:
    """The visitor's address. X-Forwarded-For counts only when the request came from a proxy on our
    own network (Caddy, or the Next.js dev server); its last entry is the one that proxy saw."""
    peer = conn.client.host if conn.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "unknown"
    xff = conn.headers.get("x-forwarded-for", "")
    if xff and (peer_ip.is_private or peer_ip.is_loopback):
        for part in reversed([p.strip() for p in xff.split(",")]):
            try:
                return str(ipaddress.ip_address(part))
            except ValueError:
                continue
    return str(peer_ip)


def browser_post_guard(settings: Settings):
    """State-changing requests ride on cookies, so they must come from our own pages: an allowed
    Origin (a cross-site form cannot forge it) and a JSON body (a cross-site form cannot send one
    without a CORS preflight, which this API never grants). SameSite=Lax cookies are the third wall."""

    async def guard(request: Request) -> None:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in settings.origins:
            raise ApiError(403, "forbidden_origin", "This request must come from the Rackloom website.")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type", "Send JSON.")

    return guard


def set_session_cookie(response: Response, settings: Settings, *, name: str, token: str, path: str, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(name, token, max_age=max_age, path=path, httponly=True, secure=settings.secure_cookies, samesite="lax")


class BodyLimit:
    """Refuses request bodies over the limit, declared or streamed, before any handler reads them."""

    def __init__(self, app: ASGIApp, limit: int = BODY_LIMIT_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.limit:
                return await _error(413, "too_large", "This request is too large.")(scope, receive, send)
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    # FastAPI re-raises its own HTTPException from a body read; anything else becomes a 400.
                    raise HTTPException(413)
            return msg

        await self.app(scope, limited, send)


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


class SecurityHeaders:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                names = {k.lower() for k, _ in msg.get("headers", [])}
                msg["headers"] = list(msg.get("headers", [])) + [h for h in SECURITY_HEADERS if h[0] not in names]
            await send(msg)

        await self.app(scope, receive, with_headers)
