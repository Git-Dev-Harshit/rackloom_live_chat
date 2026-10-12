"""Cloudflare Turnstile, checked here and never in the browser. The same rules as the auth
service's verifier (auth/internal/captcha): single-use tokens, our own hostname only, fail closed.
Adds the widget's `action` check and Cloudflare's idempotency_key, so a siteverify call that
times out can be retried without the token counting as spent.
"""
from __future__ import annotations

import logging
import uuid

import httpx

log = logging.getLogger("support.captcha")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ACTION = "support"
MAX_TOKEN_LEN = 2048  # Cloudflare's own limit; anything longer is not a token

# Cloudflare's published test secrets: they answer the same for any token from any host.
_TEST_SECRETS = {
    "1x0000000000000000000000000000000AA",
    "2x0000000000000000000000000000000AA",
    "3x0000000000000000000000000000000AA",
}


def is_test_secret(secret: str) -> bool:
    return secret.strip() in _TEST_SECRETS


class CaptchaError(Exception):
    code = "captcha_failed"


class CaptchaMissing(CaptchaError):
    code = "captcha_required"


class CaptchaRejected(CaptchaError):
    """Invalid, expired, already used, made on another site, or for another form."""
    code = "captcha_failed"


class CaptchaUnavailable(CaptchaError):
    """Cloudflare could not judge the token. Refused all the same: the check fails closed."""
    code = "captcha_unavailable"


def _bare(host: str) -> str:
    h = (host or "").strip().lower()
    return h[4:] if h.startswith("www.") else h


class Turnstile:
    def __init__(self, secret: str, host: str = "", *, endpoint: str = SITEVERIFY_URL, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0, attempts: int = 2):
        self.secret = secret.strip()
        self.host = _bare(host)
        self.endpoint = endpoint
        self.attempts = max(1, attempts)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify(self, token: str, remote_ip: str | None = None) -> None:
        """Raises a CaptchaError unless Cloudflare vouches for this token, on our host, for this form."""
        if not self.enabled:
            return
        token = (token or "").strip()
        if not token:
            raise CaptchaMissing()
        if len(token) > MAX_TOKEN_LEN:
            raise CaptchaRejected("token too long")
        form = {"secret": self.secret, "response": token, "idempotency_key": str(uuid.uuid4())}
        if remote_ip:
            form["remoteip"] = remote_ip
        out = None
        for attempt in range(self.attempts):
            try:
                resp = await self._client.post(self.endpoint, data=form)
            except httpx.HTTPError as e:
                log.warning("captcha check unavailable (attempt %d): %s", attempt + 1, type(e).__name__)
                continue
            if resp.status_code >= 500:
                log.warning("captcha check unavailable (attempt %d): siteverify answered %d", attempt + 1, resp.status_code)
                continue
            if resp.status_code != 200:
                raise CaptchaUnavailable(f"siteverify answered {resp.status_code}")
            try:
                out = resp.json()
            except ValueError:
                raise CaptchaUnavailable("unreadable siteverify answer") from None
            codes = out.get("error-codes") or []
            # Cloudflare's own hiccup: the same idempotency key makes a second ask safe.
            if not out.get("success") and "internal-error" in codes and attempt + 1 < self.attempts:
                continue
            break
        if out is None:
            raise CaptchaUnavailable("siteverify unreachable")
        codes = [str(c) for c in (out.get("error-codes") or [])]
        if not out.get("success"):
            # A secret Cloudflare does not accept, or its own failure: nothing the visitor did.
            if any(c in ("internal-error", "missing-input-secret", "invalid-input-secret") for c in codes):
                raise CaptchaUnavailable(",".join(codes))
            raise CaptchaRejected(",".join(codes) or "rejected")
        # Test secrets report example.com and no action for every token: nothing to compare.
        if is_test_secret(self.secret):
            return
        if self.host and _bare(str(out.get("hostname", ""))) != self.host:
            raise CaptchaRejected(f"token made on {out.get('hostname')!r}")
        if out.get("action") != ACTION:
            raise CaptchaRejected(f"token for action {out.get('action')!r}")
