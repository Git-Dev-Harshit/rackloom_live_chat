"""The conversation: sockets, acknowledgements, reconnects, the return link and when access ends."""
from __future__ import annotations

import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app import workers

from .conftest import claim, needs_db, owner_exec, recv_until, verify

pytestmark = needs_db


@pytest.fixture()
def claimed(client, browser, new_ticket, new_agent):
    """A ticket created by `client` (the customer) and claimed by an agent in their own browser."""
    created = new_ticket()
    ref = created["ticket"]["reference"]
    agent = new_agent("Priya Sharma")
    ab = browser()
    assert verify(ab, ref, agent["normalized_phone_number"]).status_code == 200
    assert claim(ab, ref).status_code == 200
    return {"ref": ref, "agent": agent, "agent_browser": ab, "return_url": created["return_url"]}


def cws(b, ref, after=0):
    return b.websocket_connect(f"/api/support/tickets/{ref}/ws?after={after}")


def aws(b, ref, after=0):
    return b.websocket_connect(f"/api/support/agent/tickets/{ref}/ws?after={after}")


def send(ws, body, cid=None):
    cid = cid or str(uuid.uuid4())
    ws.send_json({"type": "message", "client_id": cid, "body": body})
    return cid


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        for _ in range(20):
            ws.receive_json()
    return e.value.code


def test_customer_and_agent_talk_in_real_time_and_the_customer_sees_the_claim(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    with cws(client, ref) as c:
        ready = c.receive_json()
        assert ready["type"] == "ready" and ready["ticket"]["status"] == "PENDING" and ready["ticket"]["can_send"] is False
        first = c.receive_json()
        assert first["type"] == "message" and first["message"]["sender"] == "customer" and "H100" in first["message"]["body"]
        # Nobody has joined yet: the customer waits, nothing is stored.
        send(c, "hello?")
        err, _ = recv_until(c, "error")
        assert err["code"] == "not_claimed"

        agent = new_agent("Priya Sharma")
        ab = browser()
        verify(ab, ref, agent["normalized_phone_number"])
        assert claim(ab, ref).status_code == 200
        upd, _ = recv_until(c, "ticket")
        assert upd["ticket"]["status"] == "CLAIMED" and upd["ticket"]["agent"] == {"name": "Priya"} and upd["ticket"]["can_send"]
        joined, _ = recv_until(c, "message")
        assert joined["message"]["sender"] == "system" and "Priya" in joined["message"]["body"]

        with aws(ab, ref) as a:
            aready = a.receive_json()
            assert aready["type"] == "ready" and aready["ticket"]["customer"]["name"] == "Asha Verma"
            backlog = [a.receive_json() for _ in range(2)]
            assert [m["message"]["sender"] for m in backlog] == ["customer", "system"]

            cid = send(c, "My pod is stuck in pending <b>since</b> 10:00")
            ack, _ = recv_until(c, "ack")
            assert ack["client_id"] == cid and ack["message"]["sender"] == "customer"
            # Acknowledged only once it is in Postgres.
            assert owner_exec("select message_content from support_messages where client_message_id = %s", (cid,)) == [("My pod is stuck in pending <b>since</b> 10:00",)]
            got, _ = recv_until(a, "message")
            assert got["message"]["body"] == "My pod is stuck in pending <b>since</b> 10:00" and got["message"]["seq"] == ack["message"]["seq"]

            send(a, "Looking at it now.")
            aack, _ = recv_until(a, "ack")
            assert aack["message"]["author"] == "Priya Sharma"
            reply, _ = recv_until(c, "message")
            while reply["message"]["sender"] != "agent":
                reply, _ = recv_until(c, "message")
            assert reply["message"]["body"] == "Looking at it now." and reply["message"]["author"] == "Priya"  # first name only


def test_a_resent_message_is_stored_once(client, claimed):
    with cws(client, claimed["ref"]) as c:
        recv_until(c, "ready")
        cid = str(uuid.uuid4())
        send(c, "only once please", cid)
        first, _ = recv_until(c, "ack")
        send(c, "only once please", cid)  # the connection dropped before the ack; the client resends
        second, _ = recv_until(c, "ack")
        assert first["message"]["id"] == second["message"]["id"]
    assert owner_exec("select count(*) from support_messages where client_message_id = %s", (cid,))[0][0] == 1


def test_a_reconnecting_client_gets_what_it_missed(client, claimed):
    with cws(client, claimed["ref"]) as c:
        recv_until(c, "ready")
        last = 0
        for _ in range(2):  # the backlog: the query and the "joined" line
            last = c.receive_json()["message"]["seq"]
    with aws(claimed["agent_browser"], claimed["ref"], after=last) as a:
        recv_until(a, "ready")
        send(a, "First while you were away")
        recv_until(a, "ack")
        send(a, "Second while you were away")
        recv_until(a, "ack")
    with cws(client, claimed["ref"], after=last) as c:
        recv_until(c, "ready")
        missed = [c.receive_json()["message"]["body"] for _ in range(2)]
        assert missed == ["First while you were away", "Second while you were away"]
    page = client.get(f"/api/support/tickets/{claimed['ref']}/messages", params={"after": last}).json()
    assert [m["body"] for m in page["messages"]] == missed and page["has_more"] is False


def test_sockets_follow_the_same_rules_as_http(client, browser, claimed, new_ticket, new_agent):
    ref = claimed["ref"]
    # No cookie.
    with browser().websocket_connect(f"/api/support/tickets/{ref}/ws") as ws:
        assert closed_with(ws) == 4401
    # A page on another site.
    stranger = browser()
    stranger.headers["Origin"] = "https://evil.example"
    with pytest.raises(WebSocketDisconnect):
        with stranger.websocket_connect(f"/api/support/tickets/{ref}/ws") as ws:
            ws.receive_json()
    # Another customer's cookie, sent for this ticket's path by hand.
    other = browser()
    new_ticket(other)
    token = next(c.value for c in other.cookies.jar if c.name == "rl_support")
    with browser().websocket_connect(f"/api/support/tickets/{ref}/ws", headers={"cookie": f"rl_support={token}"}) as ws:
        assert closed_with(ws) == 4401
    assert browser().get(f"/api/support/tickets/{ref}/messages", headers={"cookie": f"rl_support={token}"}).status_code == 401
    # A customer's cookie is not an agent's.
    cust = next(c.value for c in client.cookies.jar if c.name == "rl_support" and ref in c.path)
    with browser().websocket_connect(f"/api/support/agent/tickets/{ref}/ws", headers={"cookie": f"rl_support_agent={cust}"}) as ws:
        assert closed_with(ws) == 4401
    # An agent who verified but lost the claim cannot open the chat or write into it.
    loser = new_agent("Late Agent")
    t2 = new_ticket()["ticket"]["reference"]
    lb = browser()
    verify(lb, t2, loser["normalized_phone_number"])
    winner = browser()
    verify(winner, t2, new_agent("Quick Agent")["normalized_phone_number"])
    claim(winner, t2)
    with aws(lb, t2) as ws:
        assert closed_with(ws) == 4403


def test_unauthorised_visitors_cannot_read_another_ticket(client, browser, claimed):
    nosy = browser()
    assert nosy.get(f"/api/support/tickets/{claimed['ref']}").status_code == 401
    assert nosy.get(f"/api/support/tickets/{claimed['ref']}/messages").status_code == 401
    assert nosy.get(f"/api/support/agent/tickets/{claimed['ref']}").status_code == 401
    assert nosy.get("/api/support/tickets/RCK-0000000Z/messages", headers={"cookie": "rl_support=made-up"}).status_code == 401


def test_a_copied_link_in_another_browser_needs_the_email_code(client, browser, claimed, fakes):
    token = claimed["return_url"].split("#k=")[1]
    ref = claimed["ref"]
    # The browser that created it: straight in.
    assert client.post(f"/api/support/tickets/{ref}/session", json={"token": token}).status_code == 200
    # Anyone else with the link: a code goes to the customer's email, and nothing opens without it.
    other = browser()
    res = other.post(f"/api/support/tickets/{ref}/session", json={"token": token})
    assert res.status_code == 202 and res.json() == {"verification": "email_code", "email_hint": "a***@example.com", "minutes": 10}
    assert other.get(f"/api/support/tickets/{ref}/messages").status_code == 401
    to, _, text = fakes["mailer"].sent[-1]
    code = next(w for w in text.split() if w.isdigit() and len(w) == 6)
    assert to == "asha@example.com" and token not in text
    wrong = other.post(f"/api/support/tickets/{ref}/session", json={"token": token, "code": "000000" if code != "000000" else "111111"})
    assert wrong.status_code == 400 and wrong.json()["attempts_left"] == 4
    ok = other.post(f"/api/support/tickets/{ref}/session", json={"token": token, "code": code})
    assert ok.status_code == 200
    assert other.get(f"/api/support/tickets/{ref}/messages").status_code == 200
    # The code is spent.
    assert browser().post(f"/api/support/tickets/{ref}/session", json={"token": token, "code": code}).json()["error"] == "code_expired"
    # A made-up token, or the right token for the wrong ticket, opens nothing.
    assert browser().post(f"/api/support/tickets/{ref}/session", json={"token": "x" * 43}).status_code == 404
    assert browser().post("/api/support/tickets/RCK-0000000Z/session", json={"token": token}).status_code == 404
    assert owner_exec("select count(*) from support_ticket_events e join support_tickets t on t.id = e.ticket_id where t.public_ticket_reference = %s and e.action = 'CUSTOMER_VERIFIED'", (ref,))[0][0] == 1


def test_wrong_codes_lock_and_phone_only_customers_get_instructions(client, browser, new_ticket, fakes):
    created = new_ticket()
    ref, token = created["ticket"]["reference"], created["return_url"].split("#k=")[1]
    other = browser()
    assert other.post(f"/api/support/tickets/{ref}/session", json={"token": token}).status_code == 202
    statuses = [other.post(f"/api/support/tickets/{ref}/session", json={"token": token, "code": "999999"}).status_code for _ in range(5)]
    assert statuses[-1] == 429
    phone_only = new_ticket(email=None, phone="+44 20 7946 0958")
    ref2, token2 = phone_only["ticket"]["reference"], phone_only["return_url"].split("#k=")[1]
    res = browser().post(f"/api/support/tickets/{ref2}/session", json={"token": token2})
    assert res.status_code == 403 and res.json()["error"] == "verification_unavailable" and ref2 in res.json()["message"]
    assert phone_only["link_emailed"] is False


def test_resolving_ends_the_conversation_for_both_sides_at_once(client, claimed):
    ref, ab = claimed["ref"], claimed["agent_browser"]
    with cws(client, ref) as c, aws(ab, ref) as a:
        recv_until(c, "ready")
        recv_until(a, "ready")
        assert ab.post(f"/api/support/agent/tickets/{ref}/resolve", json={}).status_code == 200
        upd, _ = recv_until(c, "ticket")
        assert upd["ticket"]["status"] == "RESOLVED" and upd["ticket"]["can_send"] is False
        assert closed_with(c) == 4410
        assert closed_with(a) == 4410
    with cws(client, ref) as c:  # a fresh socket is refused too, after saying why
        ev = c.receive_json()
        assert ev["type"] == "ticket" and ev["ticket"]["status"] == "RESOLVED"
        assert closed_with(c) == 4410


def test_an_expired_ticket_takes_no_more_messages_even_on_an_open_socket(client, sup, claimed):
    ref = claimed["ref"]
    window = owner_exec("select access_expires_at from support_tickets where public_ticket_reference = %s", (ref,))[0][0]
    with cws(client, ref) as c:
        recv_until(c, "ready")
        send(c, "still here")
        recv_until(c, "ack")
        assert owner_exec("select access_expires_at from support_tickets where public_ticket_reference = %s", (ref,))[0][0] == window  # never extended
        owner_exec("update support_tickets set created_at = now() - interval '8 days', access_expires_at = now() - interval '1 day' where public_ticket_reference = %s", (ref,))
        cid = send(c, "am I too late?")
        upd, _ = recv_until(c, "ticket")
        assert upd["ticket"]["status"] == "EXPIRED"
        err, _ = recv_until(c, "error")
        assert err["code"] == "not_allowed"
        assert closed_with(c) == 4410
    assert owner_exec("select count(*) from support_messages where client_message_id = %s", (cid,))[0][0] == 0
    assert client.get(f"/api/support/tickets/{ref}/messages").status_code == 410
    # The sweep writes the expiry down, with its event and a Discord update.
    client.portal.call(workers.sweep_once, sup)
    row = owner_exec(
        "select t.status, (select count(*) from support_ticket_events e where e.ticket_id = t.id and e.action = 'EXPIRED'), "
        "(select count(*) from support_notifications n where n.ticket_id = t.id and n.kind = 'discord_updated') from support_tickets t where t.public_ticket_reference = %s",
        (ref,),
    )[0]
    assert row[0] == "EXPIRED" and row[1] == 1 and row[2] >= 2


def test_a_closed_ticket_takes_no_messages_and_the_sweep_closes_stale_sockets(client, sup, claimed):
    ref = claimed["ref"]
    with cws(client, ref) as c:
        recv_until(c, "ready")
        assert sup.store.admin_close(ref, "test") == "done"  # closed by an admin from the CLI, outside this process
        client.portal.call(workers.sweep_once, sup)
        upd, _ = recv_until(c, "ticket")
        assert upd["ticket"]["status"] == "CLOSED"
        assert closed_with(c) == 4410


def test_message_size_and_frame_rules(client, claimed):
    with cws(client, claimed["ref"]) as c:
        recv_until(c, "ready")
        send(c, "x" * 2001)
        err, _ = recv_until(c, "error")
        assert err["code"] == "invalid_message"
        c.send_text("not json")
        err, _ = recv_until(c, "error")
        assert err["code"] == "bad_frame"
        c.send_json({"type": "ping"})
        recv_until(c, "pong")
