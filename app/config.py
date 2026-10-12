"""Settings from the environment. No well-known fallback for anything sensitive."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


# Policy. Fixed in code on purpose: the seven-day window is a promise made to customers, not a knob.
ACCESS_DAYS = 7
AGENT_SESSION_HOURS = 12
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
NAME_MAX = 120
EMAIL_MAX = 254
QUERY_MAX = 4000
MESSAGE_MAX = 2000
BODY_LIMIT_BYTES = 16 * 1024


@dataclass(frozen=True)
class Settings:
    # Runtime role (support_app). The owner URL is used only by `python -m app.cli migrate`.
    database_url: str = field(default_factory=lambda: _env("SUPPORT_DATABASE_URL"))
    migrate_database_url: str = field(default_factory=lambda: _env("SUPPORT_MIGRATE_DATABASE_URL"))
    app_db_password: str = field(default_factory=lambda: _env("SUPPORT_DB_APP_PASSWORD"))
    # The public site (links in Discord and email, the expected Turnstile hostname, cookie Secure flag).
    public_url: str = field(default_factory=lambda: _env("SUPPORT_PUBLIC_URL", "http://localhost:3000").rstrip("/"))
    # Browser origins allowed to call the API and open sockets. Empty: the public URL and its www twin.
    allowed_origins_raw: str = field(default_factory=lambda: _env("SUPPORT_ALLOWED_ORIGINS"))
    # Shared with web for the /internal admin API. Empty: that API refuses every request.
    internal_token: str = field(default_factory=lambda: _env("SUPPORT_INTERNAL_TOKEN"))
    # The same Turnstile widget as the account forms (site key public, secret private).
    turnstile_site_key: str = field(default_factory=lambda: _env("TURNSTILE_SITE_KEY"))
    turnstile_secret: str = field(default_factory=lambda: _env("TURNSTILE_SECRET"))
    turnstile_allow_test_keys: bool = field(default_factory=lambda: _env("TURNSTILE_ALLOW_TEST_KEYS") == "1")
    # Incoming webhook of the private support channel. Empty: tickets queue until it is set.
    discord_webhook_url: str = field(default_factory=lambda: _env("SUPPORT_DISCORD_WEBHOOK_URL"))
    # Mail for the return link and the new-browser code. Same variables as web and auth.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587") or "587"))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_pass: str = field(default_factory=lambda: os.environ.get("SMTP_PASS", ""))
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM", "Rackloom <support@rackloom.in>"))
    support_email: str = field(default_factory=lambda: _env("SUPPORT_CONTACT_EMAIL", "support@rackloom.in"))

    @property
    def public_host(self) -> str:
        return (urlsplit(self.public_url).hostname or "").lower()

    @property
    def production(self) -> bool:
        """Served over https from a real host. The captcha is mandatory there."""
        return self.public_url.startswith("https://") and self.public_host not in ("localhost", "127.0.0.1", "::1")

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origins(self) -> frozenset[str]:
        if self.allowed_origins_raw:
            return frozenset(o.rstrip("/") for o in _list(self.allowed_origins_raw))
        u = urlsplit(self.public_url)
        base = f"{u.scheme}://{u.netloc}"
        host = u.hostname or ""
        if host and not host.startswith("www.") and host not in ("localhost", "127.0.0.1"):
            return frozenset({base, base.replace(f"://{host}", f"://www.{host}", 1)})
        return frozenset({base})

    def startup_problems(self) -> list[str]:
        """Reasons to refuse to start. Names only, never values."""
        from .turnstile import is_test_secret

        out = []
        if not self.database_url:
            out.append("SUPPORT_DATABASE_URL is not set")
        if self.production:
            if not self.turnstile_secret:
                out.append(f"TURNSTILE_SECRET is not set: the support form would have no captcha ({self.public_url} is production)")
            elif is_test_secret(self.turnstile_secret) and not self.turnstile_allow_test_keys:
                out.append("TURNSTILE_SECRET is a Cloudflare test secret, which passes any token: set the site's real secret")
            if not self.turnstile_site_key:
                out.append("TURNSTILE_SITE_KEY is not set: the widget could not show the captcha")
        return out
