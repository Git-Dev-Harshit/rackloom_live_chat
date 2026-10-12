"""Creating a ticket: the captcha, the form rules, duplicates, limits and the Discord outbox."""
from __future__ import annotations

import uuid


from app.discord import DeliveryError

from .conftest import ORIGIN, drain, needs_db, owner_exec, ticket_body

pytestmark = needs_db

# Names unique to this run: the test database is shared and keeps earlier runs' rows.
RUN = uuid.uuid4().hex[:8]
N_CAPTCHA_OK = f"Captcha Ok {RUN}"
N_CAPTCHA_BAD = f"Captcha Bad {RUN}"
N_CAPTCHA_REUSE = f"Captcha Reuse {RUN}"
N_CAPTCHA_DOWN = f"Captcha Down {RUN}"
N_EMAIL_ONLY = f"Email Only {RUN}"
N_PHONE_ONLY = f"Phone Only {RUN}"
N_BOTH = f"Both {RUN}"
N_RETRY_SAME = f"Retry Same {RUN}"
N_FLOOD = f"Flood {RUN}"
N_CROSS_SITE = f"Cross Site {RUN}"
N_DISCORD_OK = f"Discord Ok {RUN}"
N_DISCORD_DOWN = f"Discord Down {RUN}"
N_DISCORD_GONE = f"Discord Gone {RUN}"


def tickets_named(name: str) -> int:
    return owner_exec("select count(*) from support_tickets where customer_name = %s", (name,))[0][0]


def test_valid_captcha_creates_one_ticket_with_its_first_message_and_a_session(client, fakes):
    res = client.post("/api/support/tickets", json=ticket_body(name=N_CAPTCHA_OK))
    assert res.status_code == 201, res.text
    body = res.json()
    t = body["ticket"]
    assert t["status"] == "PENDING" and t["reference"].startswith("RCK-") and t["can_send"] is False
    assert body["return_url"].startswith(f"{ORIGIN}/support/ticket/{t['reference']}#k=")
    # The session cookie is HTTP-only, scoped to this ticket's API path, and the token is not in the body.
    cookie = res.headers["set-cookie"]
    assert "rl_support=" in cookie and "HttpOnly" in cookie and f"Path=/api/support/tickets/{t['reference']}" in cookie and "SameSite=lax" in cookie
    assert "token" not in body["ticket"]
    rows = owner_exec(
        "select m.sender_type, m.message_content, t.status, t.access_expires_at - t.created_at from support_messages m "
        "join support_tickets t on t.id = m.ticket_id where t.public_ticket_reference = %s order by m.seq",
        (t["reference"],),
    )
    assert [(r[0], r[1]) for r in rows] == [("CUSTOMER", "My H100 instance will not start. Can someone help?")]
    assert str(rows[0][3]) == "7 days, 0:00:00"
    assert owner_exec("select n.kind, n.status from support_notifications n join support_tickets t on t.id = n.ticket_id where t.public_ticket_reference = %s", (t["reference"],)) == [("discord_created", "pending")]
    # The same browser reads it back with the cookie.
    again = client.get(f"/api/support/tickets/{t['reference']}")
    assert again.status_code == 200 and again.json()["ticket"]["reference"] == t["reference"]
    msgs = client.get(f"/api/support/tickets/{t['reference']}/messages").json()
    assert [m["sender"] for m in msgs["messages"]] == ["customer"]


def test_invalid_missing_and_reused_captcha_create_nothing(client):
    for token, code in [("forged", "captcha_failed"), ("", "captcha_required")]:
        res = client.post("/api/support/tickets", json=ticket_body(name=N_CAPTCHA_BAD, turnstile_token=token))
        assert res.status_code == 400 and res.json()["error"] == code
    # A token already spent on one ticket cannot make another.
    tok = f"ok-{uuid.uuid4()}"
    assert client.post("/api/support/tickets", json=ticket_body(name=N_CAPTCHA_REUSE, turnstile_token=tok)).status_code == 201
    res = client.post("/api/support/tickets", json=ticket_body(name=N_CAPTCHA_REUSE, turnstile_token=tok))
    assert res.status_code == 400 and res.json()["error"] == "captcha_failed"
    assert tickets_named(N_CAPTCHA_BAD) == 0 and tickets_named(N_CAPTCHA_REUSE) == 1


def test_captcha_provider_outage_fails_closed(client):
    res = client.post("/api/support/tickets", json=ticket_body(name=N_CAPTCHA_DOWN, turnstile_token="down"))
    assert res.status_code == 503 and res.json()["error"] == "captcha_unavailable"
    assert tickets_named(N_CAPTCHA_DOWN) == 0


def test_form_rules_are_enforced_on_the_server(client, fakes):
    cases = [
        (dict(name="  "), "name"),
        (dict(message=" \n "), "message"),
        (dict(email=None, phone=None), "contact"),
        (dict(email="", phone="   "), "contact"),
        (dict(email="not-an-email"), "email"),
        (dict(email=None, phone="98765 43210", phone_country=None), "phone"),  # no country, no +code
        (dict(name="x" * 121), "name"),
        (dict(message="x" * 4001), "message"),
    ]
    for over, field in cases:
        res = client.post("/api/support/tickets", json=ticket_body(**over))
        assert res.status_code == 422, (over, res.text)
        assert field in res.json()["fields"], (over, res.json())
    # Validation runs before the captcha, so a typo does not burn the visitor's token.
    assert fakes["turnstile"].calls == []


def test_email_only_phone_only_and_both_are_accepted_and_normalised(client):
    e = client.post("/api/support/tickets", json=ticket_body(name=N_EMAIL_ONLY, email=" Asha@Example.COM ")).json()["ticket"]
    p = client.post("/api/support/tickets", json=ticket_body(name=N_PHONE_ONLY, email=None, phone="(415) 555-2671", phone_country="US")).json()["ticket"]
    b = client.post("/api/support/tickets", json=ticket_body(name=N_BOTH, phone="+91 98765 43210")).json()["ticket"]
    got = dict(owner_exec(
        "select public_ticket_reference, row(customer_email, customer_phone)::text from support_tickets where public_ticket_reference = any(%s)",
        ([e["reference"], p["reference"], b["reference"]],),
    ))
    assert got[e["reference"]] == "(asha@example.com,)"
    assert got[p["reference"]] == "(,+14155552671)"
    assert got[b["reference"]] == "(asha@example.com,+919876543210)"


def test_a_retried_submit_returns_the_same_ticket_and_never_a_second(client, browser, fakes):
    body = ticket_body(name=N_RETRY_SAME)
    first = client.post("/api/support/tickets", json=body)
    # The response was "lost": the browser sends the very same request again (same captcha token too).
    second = client.post("/api/support/tickets", json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["replayed"] is True and second.json()["ticket"]["reference"] == first.json()["ticket"]["reference"]
    assert tickets_named(N_RETRY_SAME) == 1
    assert len([c for c in fakes["turnstile"].calls if c == body["turnstile_token"]]) == 1  # the replay did not re-verify
    # The same key with different content is refused rather than guessed at.
    changed = client.post("/api/support/tickets", json={**body, "message": "something else"})
    assert changed.status_code == 409 and changed.json()["error"] == "idempotency_conflict"


def test_ticket_creation_is_rate_limited_per_address(client):
    codes = [client.post("/api/support/tickets", json=ticket_body(name=N_FLOOD)).status_code for _ in range(7)]
    assert codes[:5] == [201] * 5 and codes[5:] == [429, 429]
    assert tickets_named(N_FLOOD) == 5


def test_only_our_pages_may_post_json_and_bodies_are_bounded(client, browser):
    stranger = browser()
    stranger.headers["Origin"] = "https://evil.example"
    res = stranger.post("/api/support/tickets", json=ticket_body(name=N_CROSS_SITE))
    assert res.status_code == 403 and res.json()["error"] == "forbidden_origin"
    no_origin = browser()
    del no_origin.headers["Origin"]
    assert no_origin.post("/api/support/tickets", json=ticket_body(name=N_CROSS_SITE)).status_code == 403
    form = client.post("/api/support/tickets", content="name=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 415
    huge = client.post("/api/support/tickets", content=b'{"x":"' + b"a" * 20000 + b'"}', headers={"Content-Type": "application/json"})
    assert huge.status_code == 413
    assert tickets_named(N_CROSS_SITE) == 0
    # Responses carry the hardening headers and never a stack trace.
    r = client.get("/api/support/config")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.json()["turnstile_site_key"] == "1x00000000000000000000AA" and r.json()["limits"]["message"] == 2000


def test_validation_errors_never_echo_what_was_sent(client):
    res = client.post("/api/support/tickets", json={**ticket_body(), "idempotency_key": "secret-looking-value"})
    assert res.status_code == 422 and "secret-looking-value" not in res.text


def test_discord_gets_the_ticket_once_and_its_updates_edit_that_message(client, sup, fakes, new_ticket):
    t = new_ticket(name=N_DISCORD_OK, phone="+91 98765 43210")["ticket"]
    drain(client)
    post = next(p for p in fakes["discord"].posts if p["content"].endswith(f"**{t['reference']}**"))
    f = {x["name"]: x["value"] for x in post["embeds"][0]["fields"]}
    assert f["Ticket ID"] == t["reference"] and f["Phone"] == "+919876543210" and f["Status"] == "PENDING"
    assert post["embeds"][0]["url"].endswith(f"/support/agent/ticket/{t['reference']}")
    row = owner_exec("select t.discord_message_id, n.status, n.attempts from support_tickets t join support_notifications n on n.ticket_id = t.id where t.public_ticket_reference = %s", (t["reference"],))
    assert row[0][0] and row[0][1:] == ("sent", 1)
    drain(client)
    assert sum(1 for p in fakes["discord"].posts if t["reference"] in p["content"]) == 1  # delivered rows are not sent again


def test_discord_outage_keeps_the_ticket_and_retries_without_duplicates(client, sup, fakes, new_ticket):
    fakes["discord"].fail = DeliveryError("discord answered 502", retryable=True)
    t = new_ticket(name=N_DISCORD_DOWN)["ticket"]
    drain(client)
    row = owner_exec(
        "select n.status, n.attempts, n.last_error, n.next_attempt_at > now() from support_notifications n join support_tickets t on t.id = n.ticket_id where t.public_ticket_reference = %s",
        (t["reference"],),
    )[0]
    assert row[0] == "pending" and row[1] == 1 and "502" in row[2] and row[3] is True
    assert client.get(f"/api/support/tickets/{t['reference']}").status_code == 200  # the ticket is fine
    # Discord is back; the retry falls due.
    fakes["discord"].fail = None
    owner_exec("update support_notifications n set next_attempt_at = now() from support_tickets t where t.id = n.ticket_id and t.public_ticket_reference = %s", (t["reference"],))
    drain(client)
    drain(client)
    assert sum(1 for p in fakes["discord"].posts if t["reference"] in p["content"]) == 1
    assert tickets_named(N_DISCORD_DOWN) == 1


def test_a_permanent_discord_refusal_is_recorded_redacted_and_retryable_by_an_admin(client, sup, fakes, new_ticket):
    fakes["discord"].fail = DeliveryError(f"discord refused the request (404) at {fakes['discord'].url}", retryable=False)
    t = new_ticket(name=N_DISCORD_GONE)["ticket"]
    drain(client)
    status, err = owner_exec("select n.status, n.last_error from support_notifications n join support_tickets t on t.id = n.ticket_id where t.public_ticket_reference = %s", (t["reference"],))[0]
    assert status == "failed" and "secret-webhook-token" not in err
    overview = client.get("/internal/overview", headers={"X-Internal-Token": "internal-test-token"}).json()
    row = next(x for x in overview["tickets"] if x["reference"] == t["reference"])
    assert row["discord"]["status"] == "failed"
    fakes["discord"].fail = None
    assert client.post(f"/internal/tickets/{t['reference']}/notifications/retry", headers={"X-Internal-Token": "internal-test-token"}).json() == {"retried": 1}
    drain(client)
    assert sum(1 for p in fakes["discord"].posts if t["reference"] in p["content"]) == 1
