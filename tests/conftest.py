"""Fixtures. Cloudflare, Discord and mail are fakes: no test needs a credential or the internet.

The database tests run against a real Postgres with the migrations applied, as the real
least-privilege role, so a missing GRANT fails here and not in production. They are skipped
unless both URLs are set:
  SUPPORT_TEST_OWNER_URL     the owner (applies migrations, arranges expired tickets)
  SUPPORT_TEST_DATABASE_URL  support_app (what the service runs as)
"""
from __future__ import annotations

import os
import random
import uuid
import warnings

import psycopg
import pytest

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")

from starlette.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.discord import DeliveryError  # noqa: E402
from app.turnstile import CaptchaMissing, CaptchaRejected, CaptchaUnavailable  # noqa: E402

OWNER = os.environ.get("SUPPORT_TEST_OWNER_URL", "")
APP_URL = os.environ.get("SUPPORT_TEST_DATABASE_URL", "")
ORIGIN = "http://localhost:3000"
needs_db = pytest.mark.skipif(not (OWNER and APP_URL), reason="no test database (SUPPORT_TEST_OWNER_URL, SUPPORT_TEST_DATABASE_URL)")


class FakeTurnstile:
    """Tokens: "ok-<anything>" passes once, "down" is Cloudflare unreachable, anything else is refused."""

    enabled = True

    def __init__(self):
        self.calls: list[str] = []
        self.used: set[str] = set()

    async def verify(self, token: str, remote_ip: str | None = None) -> None:
        self.calls.append(token)
        if not token:
            raise CaptchaMissing()
        if token == "down":
            raise CaptchaUnavailable("timeout")
        if not token.startswith("ok-") or token in self.used:
            raise CaptchaRejected("invalid-input-response" if not token.startswith("ok-") else "timeout-or-duplicate")
        self.used.add(token)

    async def aclose(self) -> None:
        pass


class FakeDiscord:
    url = "https://discord.com/api/webhooks/123/secret-webhook-token"
    enabled = True

    def __init__(self):
        self.posts: list[dict] = []
        self.edits: list[tuple[str, dict]] = []
        self.fail: DeliveryError | None = None

    async def create(self, payload: dict) -> str:
        if self.fail:
            raise self.fail
        self.posts.append(payload)
        return str(900000 + len(self.posts))

    async def edit(self, message_id: str, payload: dict) -> None:
        if self.fail:
            raise self.fail
        self.edits.append((message_id, payload))

    async def aclose(self) -> None:
        pass


class FakeMailer:
    kind = "smtp"
    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))


def random_phone() -> str:
    return "+9198" + "".join(random.choice("0123456789") for _ in range(8))


def owner_exec(sql: str, params: tuple = ()) -> list:
    with psycopg.connect(OWNER, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture(scope="session")
def migrated():
    from app.db import migrate

    password = psycopg.conninfo.conninfo_to_dict(APP_URL).get("password", "")
    migrate(OWNER, str(password or ""))


@pytest.fixture()
def fakes():
    return {"turnstile": FakeTurnstile(), "discord": FakeDiscord(), "mailer": FakeMailer()}


@pytest.fixture()
def settings():
    return Settings(database_url=APP_URL, public_url=ORIGIN, turnstile_site_key="1x00000000000000000000AA", internal_token="internal-test-token")


@pytest.fixture()
def app(migrated, settings, fakes):
    from app.main import create_app

    return create_app(settings, run_workers=False, **fakes)


@pytest.fixture()
def client(app):
    with TestClient(app, headers={"Origin": ORIGIN}) as c:
        c.app.state.support.limiter.reset()
        yield c


@pytest.fixture()
def sup(client):
    return client.app.state.support


@pytest.fixture()
def browser(client):
    """Another browser on the same running app: its own cookie jar, the same event loop and hub."""

    def make() -> TestClient:
        b = TestClient(client.app, headers={"Origin": ORIGIN})
        b.portal = client.portal
        return b

    return make


def ticket_body(**over) -> dict:
    body = {
        "turnstile_token": f"ok-{uuid.uuid4()}",
        "idempotency_key": str(uuid.uuid4()),
        "name": "Asha Verma",
        "email": "asha@example.com",
        "phone": None,
        "phone_country": None,
        "message": "My H100 instance will not start. Can someone help?",
    }
    body.update(over)
    return body


@pytest.fixture()
def new_ticket(client):
    def make(b: TestClient | None = None, **over) -> dict:
        res = (b or client).post("/api/support/tickets", json=ticket_body(**over))
        assert res.status_code == 201, res.text
        return res.json()

    return make


@pytest.fixture()
def new_agent(sup):
    def make(name: str = "Priya Sharma", active: bool = True) -> dict:
        a = sup.store.create_agent(name, random_phone())
        if not active:
            a = sup.store.update_agent(a["id"], active=False)
        return a

    return make


def verify(b: TestClient, ref: str, phone: str, country: str | None = None):
    return b.post("/api/support/agent/verify", json={"reference": ref, "phone": phone, "phone_country": country})


def claim(b: TestClient, ref: str):
    return b.post(f"/api/support/agent/tickets/{ref}/claim", json={})


def drain(client: TestClient) -> None:
    """Deliver every Discord row that is due now (other tests leave theirs in the shared database)."""
    from app import workers

    sup = client.app.state.support
    while client.portal.call(workers.deliver_due, sup):
        pass


def recv_until(ws, kind: str, limit: int = 50) -> tuple[dict, list[dict]]:
    """Read events until one of `kind` arrives; returns it and everything read before it."""
    seen = []
    for _ in range(limit):
        ev = ws.receive_json()
        if ev.get("type") == kind:
            return ev, seen
        seen.append(ev)
    raise AssertionError(f"no {kind!r} event in {limit} frames: {seen}")
