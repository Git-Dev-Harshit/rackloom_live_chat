"""Cloudflare siteverify and the Discord webhook, against fake servers (httpx.MockTransport)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.discord import DeliveryError, DiscordWebhook, escape_markdown, redact, ticket_payload, valid_webhook_url
from app.turnstile import CaptchaMissing, CaptchaRejected, CaptchaUnavailable, Turnstile

SECRET = "0x4AAAAAAA-real-secret"


def run(coro):
    return asyncio.run(coro)


def cloudflare(*answers):
    """A siteverify that gives each answer in turn: a dict (JSON), an int (status) or an exception."""
    seen: list[dict] = []
    queue = list(answers)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        a = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(a, Exception):
            raise a
        if isinstance(a, int):
            return httpx.Response(a)
        return httpx.Response(200, json=a)

    return httpx.MockTransport(handler), seen


GOOD = {"success": True, "hostname": "rackloom.in", "action": "support", "challenge_ts": "2026-10-13T00:00:00Z", "error-codes": []}


def test_a_genuine_token_from_our_site_passes_and_the_secret_stays_server_side():
    transport, seen = cloudflare(GOOD)
    run(Turnstile(SECRET, "rackloom.in", transport=transport).verify("tok-1", "203.0.113.9"))
    assert seen[0]["secret"] == SECRET and seen[0]["response"] == "tok-1" and seen[0]["remoteip"] == "203.0.113.9"
    assert len(seen[0]["idempotency_key"]) == 36


def test_www_twin_passes():
    transport, _ = cloudflare({**GOOD, "hostname": "www.rackloom.in"})
    run(Turnstile(SECRET, "rackloom.in", transport=transport).verify("tok"))


@pytest.mark.parametrize(
    "answer",
    [
        {"success": False, "error-codes": ["invalid-input-response"]},
        {"success": False, "error-codes": ["timeout-or-duplicate"]},  # expired or already used
        {**GOOD, "hostname": "evil.example"},
        {**GOOD, "action": "login"},  # a token made for another form
    ],
)
def test_invalid_expired_reused_foreign_or_other_form_tokens_are_refused(answer):
    transport, _ = cloudflare(answer)
    with pytest.raises(CaptchaRejected):
        run(Turnstile(SECRET, "rackloom.in", transport=transport).verify("tok"))


def test_missing_and_oversized_tokens_are_refused_without_asking_cloudflare():
    transport, seen = cloudflare(GOOD)
    t = Turnstile(SECRET, "rackloom.in", transport=transport)
    with pytest.raises(CaptchaMissing):
        run(t.verify("  "))
    with pytest.raises(CaptchaRejected):
        run(t.verify("x" * 2049))
    assert seen == []


def test_a_timeout_is_retried_with_the_same_idempotency_key_then_passes():
    transport, seen = cloudflare(httpx.ReadTimeout("slow"), GOOD)
    run(Turnstile(SECRET, "rackloom.in", transport=transport).verify("tok"))
    assert len(seen) == 2 and seen[0]["idempotency_key"] == seen[1]["idempotency_key"]


@pytest.mark.parametrize(
    "answers",
    [
        (httpx.ConnectTimeout("down"),),
        (503,),
        ({"success": False, "error-codes": ["internal-error"]},),
        ({"success": False, "error-codes": ["invalid-input-secret"]},),
        (403,),
    ],
)
def test_cloudflare_down_or_refusing_our_secret_fails_closed(answers):
    transport, _ = cloudflare(*answers)
    with pytest.raises(CaptchaUnavailable):
        run(Turnstile(SECRET, "rackloom.in", transport=transport).verify("tok"))


def test_test_secrets_skip_the_host_check_and_no_secret_means_off():
    transport, _ = cloudflare({"success": True, "hostname": "example.com"})
    run(Turnstile("1x0000000000000000000000000000000AA", "rackloom.in", transport=transport).verify("XXXX.DUMMY.TOKEN"))
    off = Turnstile("", "rackloom.in")
    assert not off.enabled
    run(off.verify(""))


# --- Discord ----------------------------------------------------------------------------------

def ticket(**over) -> dict:
    t = {
        "reference": "RCK-7K3M9QX2", "effective_status": "PENDING", "customer_name": "Asha Verma",
        "customer_email": "asha@example.com", "customer_phone": "+919876543210", "agent_name": None,
        "created_at": datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc), "query": "My H100 will not start.",
    }
    t.update(over)
    return t


PORTAL = "https://rackloom.in/support/agent/ticket/RCK-7K3M9QX2"


def fields(payload: dict) -> dict:
    return {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}


def test_a_new_ticket_message_has_every_part_and_links_to_the_agent_portal():
    p = ticket_payload(ticket(), PORTAL)
    e = p["embeds"][0]
    assert e["title"] == "RACKLOOM SUPPORT — NEW TICKET" and e["url"] == PORTAL
    f = fields(p)
    assert f["Ticket ID"] == "RCK-7K3M9QX2" and f["Status"] == "PENDING"
    assert f["Customer"] == "Asha Verma" and f["Email"] == "asha@example.com" and f["Phone"] == "+919876543210"
    assert f["Created"].startswith("2026-10-13 09:30 UTC")
    assert f["Action"] == f"[Open Agent Portal / Claim Ticket]({PORTAL})"
    assert "My H100 will not start." in e["description"]
    assert p["allowed_mentions"] == {"parse": []}  # customer text never pings anyone
    blob = json.dumps(p)
    assert "#k=" not in blob and "/support/ticket/" not in blob  # no customer link or token, ever


def test_missing_contact_details_are_left_out_not_shown_empty():
    assert "Email" not in fields(ticket_payload(ticket(customer_email=None), PORTAL))
    f = fields(ticket_payload(ticket(customer_phone=None), PORTAL))
    assert "Phone" not in f and "Email" in f


def test_customer_text_cannot_format_link_or_mention():
    p = ticket_payload(ticket(customer_name="**Boss** [pay here](http://evil.test) <@123>", query="@everyone " + "x" * 400), PORTAL)
    name = fields(p)["Customer"]
    assert "\\*\\*Boss\\*\\*" in name and "\\[pay here\\]\\(http" in name and "\\<@123\\>" in name
    desc = p["embeds"][0]["description"]
    assert desc.endswith("…") and len(desc) < 340
    assert escape_markdown("a_b|c") == "a\\_b\\|c"


def test_claimed_ticket_message_shows_the_agent():
    p = ticket_payload(ticket(effective_status="CLAIMED", agent_name="Priya Sharma"), PORTAL)
    assert p["embeds"][0]["title"] == "RACKLOOM SUPPORT — CLAIMED"
    assert fields(p)["Assigned to"] == "Priya Sharma" and fields(p)["Action"] == f"[Open Agent Portal]({PORTAL})"


HOOK = "https://discord.com/api/webhooks/123456/abcDEF-token_x"


def discord(*answers):
    seen: list[httpx.Request] = []
    queue = list(answers)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        a = queue.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    return DiscordWebhook(HOOK, transport=httpx.MockTransport(handler)), seen


def test_posting_waits_for_the_message_id_and_editing_patches_that_message():
    hook, seen = discord(httpx.Response(200, json={"id": "112233"}), httpx.Response(200, json={"id": "112233"}))
    assert run(hook.create({"content": "x"})) == "112233"
    run(hook.edit("112233", {"content": "y"}))
    assert seen[0].method == "POST" and str(seen[0].url) == HOOK + "?wait=true"
    assert seen[1].method == "PATCH" and str(seen[1].url) == HOOK + "/messages/112233"


@pytest.mark.parametrize(
    ("answer", "retryable"),
    [
        (httpx.Response(429, json={"retry_after": 2.5}), True),
        (httpx.Response(502), True),
        (httpx.ReadTimeout("slow"), True),
        (httpx.ConnectError("refused"), True),
        (httpx.Response(404, json={"message": "Unknown Webhook"}), False),
        (httpx.Response(400, json={"message": "Invalid Form Body"}), False),
    ],
)
def test_delivery_errors_say_whether_a_retry_can_help(answer, retryable):
    hook, _ = discord(answer)
    with pytest.raises(DeliveryError) as e:
        run(hook.create({"content": "x"}))
    assert e.value.retryable is retryable
    assert "abcDEF-token_x" not in str(e.value)
    if isinstance(answer, httpx.Response) and answer.status_code == 429:
        assert e.value.retry_after == 2.5


def test_the_webhook_url_is_checked_and_never_survives_into_an_error():
    assert valid_webhook_url(HOOK) and valid_webhook_url("https://discordapp.com/api/webhooks/1/x")
    assert not valid_webhook_url("https://evil.test/api/webhooks/1/x") and not valid_webhook_url("http://discord.com/api/webhooks/1/x")
    with pytest.raises(ValueError):
        DiscordWebhook("https://evil.test/hook")
    assert "abcDEF" not in redact(f"failed calling {HOOK}/messages/1", HOOK)
    assert "secret" not in redact("POST https://discord.com/api/webhooks/9/secret failed", "")
