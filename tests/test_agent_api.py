"""The agent's phone check, claiming, resolving and closing, and the admin API for agents."""
from __future__ import annotations

import threading

from .conftest import claim, drain, needs_db, owner_exec, random_phone, verify

pytestmark = needs_db


def expire(ref: str) -> None:
    """Move a ticket's whole life 8 days back, so its 7-day window is over (the DB check still holds)."""
    owner_exec(
        "update support_tickets set created_at = now() - interval '8 days', access_expires_at = now() - interval '1 day' where public_ticket_reference = %s",
        (ref,),
    )


def test_a_registered_active_agent_is_verified_and_gets_a_ticket_scoped_cookie(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    agent = new_agent("Priya Sharma")
    b = browser()
    res = verify(b, ref, agent["normalized_phone_number"])
    assert res.status_code == 200, res.text
    assert res.json()["agent"]["name"] == "Priya Sharma"
    assert res.json()["ticket"] == {**res.json()["ticket"], "status": "PENDING", "claimable": True, "assigned_to_you": False}
    assert "customer" not in res.json()["ticket"]  # nothing about the customer before the claim
    cookie = res.headers["set-cookie"]
    assert "rl_support_agent=" in cookie and f"Path=/api/support/agent/tickets/{ref}" in cookie and "HttpOnly" in cookie


def test_unknown_and_inactive_numbers_get_one_generic_refusal(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    inactive = new_agent("Off Duty", active=False)
    unknown = verify(browser(), ref, "+91 91234 56789")
    off = verify(browser(), ref, inactive["normalized_phone_number"])
    wrong_ticket = verify(browser(), "RCK-00000000", new_agent()["normalized_phone_number"])
    for res in (unknown, off, wrong_ticket):
        assert res.status_code == 401 and res.json() == {"error": "not_verified", "message": unknown.json()["message"]}
        assert "set-cookie" not in res.headers


def test_the_number_matches_however_the_agent_types_it(client, browser, new_ticket, sup):
    e164 = random_phone()
    agent = sup.store.create_agent("Format Test", e164)
    n = e164[3:]  # the ten national digits
    for raw, country in [(f"{n[:5]} {n[5:]}", "IN"), (f"+91-{n[:5]}-{n[5:]}", None), (f"0{n}", "IN")]:
        ref = new_ticket()["ticket"]["reference"]
        assert verify(browser(), ref, raw, country).status_code == 200, (raw, country)
    assert verify(browser(), new_ticket()["ticket"]["reference"], n).status_code == 422  # no country, no +code
    sup.store.update_agent(agent["id"], active=False)


def test_claiming_assigns_once_records_history_and_tells_the_channel(client, browser, new_ticket, new_agent, sup, fakes):
    ref = new_ticket()["ticket"]["reference"]
    drain(client)  # the creation post exists, so the claim edits it
    agent = new_agent("Rahul Mehta")
    b = browser()
    verify(b, ref, agent["normalized_phone_number"])
    res = claim(b, ref)
    assert res.status_code == 200 and res.json()["claimed_now"] is True
    t = res.json()["ticket"]
    assert t["status"] == "CLAIMED" and t["assigned_to_you"] and t["customer"]["email"] == "asha@example.com"
    row = owner_exec(
        "select t.status, t.assigned_agent_id = %s, t.claimed_at is not null, "
        "(select count(*) from support_ticket_events e where e.ticket_id = t.id and e.action = 'CLAIMED'), "
        "(select message_content from support_messages m where m.ticket_id = t.id and m.sender_type = 'SYSTEM') "
        "from support_tickets t where t.public_ticket_reference = %s",
        (agent["id"], ref),
    )[0]
    assert row == ("CLAIMED", True, True, 1, "Rahul from Rackloom Support has joined the conversation.")
    # Claiming again changes nothing.
    again = claim(b, ref)
    assert again.status_code == 200 and again.json()["claimed_now"] is False
    assert owner_exec("select count(*) from support_ticket_events e join support_tickets t on t.id = e.ticket_id where t.public_ticket_reference = %s and e.action = 'CLAIMED'", (ref,))[0][0] == 1
    drain(client)
    mid, payload = next((m, p) for m, p in reversed(fakes["discord"].edits) if p["embeds"][0]["fields"][0]["value"] == ref)
    assert mid.isdigit() and payload["embeds"][0]["title"] == "RACKLOOM SUPPORT — CLAIMED"
    assert {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}["Assigned to"] == "Rahul Mehta"


def test_a_second_agent_cannot_claim_or_verify_into_a_claimed_ticket(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    a, b = new_agent("First Agent"), new_agent("Second Agent")
    ba, bb = browser(), browser()
    verify(ba, ref, a["normalized_phone_number"])
    verify(bb, ref, b["normalized_phone_number"])  # both verified while it was still pending
    assert claim(ba, ref).status_code == 200
    late = claim(bb, ref)
    assert late.status_code == 409 and late.json()["message"] == "This ticket has already been claimed and is no longer available."
    assert verify(browser(), ref, b["normalized_phone_number"]).status_code == 409
    # The loser's session shows nothing of the ticket and cannot read or act on it.
    assert bb.get(f"/api/support/agent/tickets/{ref}").status_code == 403
    assert bb.get(f"/api/support/agent/tickets/{ref}/messages").status_code == 403
    assert bb.post(f"/api/support/agent/tickets/{ref}/resolve", json={}).status_code == 403
    assert owner_exec("select assigned_agent_id = %s from support_tickets where public_ticket_reference = %s", (a["id"], ref))[0][0]


def test_simultaneous_claims_have_exactly_one_winner(client, browser, new_ticket, new_agent):
    for _ in range(3):
        ref = new_ticket()["ticket"]["reference"]
        agents = [new_agent(f"Racer {i}") for i in range(6)]
        browsers = [browser() for _ in agents]
        for b, a in zip(browsers, agents, strict=True):
            assert verify(b, ref, a["normalized_phone_number"]).status_code == 200
        start, results = threading.Barrier(len(browsers)), []

        def go(b, ref=ref, start=start, results=results):
            start.wait()
            results.append(claim(b, ref).status_code)

        threads = [threading.Thread(target=go, args=(b,)) for b in browsers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [200] + [409] * 5, results
        assert owner_exec("select count(*) from support_ticket_events e join support_tickets t on t.id = e.ticket_id where t.public_ticket_reference = %s and e.action = 'CLAIMED'", (ref,))[0][0] == 1


def test_the_store_claim_is_atomic_under_real_concurrency(sup, new_ticket, new_agent):
    for _ in range(3):
        t = sup.store.ticket_by_ref(new_ticket()["ticket"]["reference"])
        agents = [new_agent(f"Thread {i}") for i in range(8)]
        start, outcomes = threading.Barrier(len(agents)), []

        def go(agent_id, t=t, start=start, outcomes=outcomes):
            start.wait()
            outcomes.append(sup.store.claim(t["id"], agent_id))

        threads = [threading.Thread(target=go, args=(a["id"],)) for a in agents]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sorted(outcomes) == ["claimed"] + ["taken"] * 7


def test_expired_and_closed_tickets_cannot_be_claimed(client, browser, new_ticket, new_agent, sup):
    agent = new_agent()
    old = new_ticket()["ticket"]["reference"]
    b = browser()
    assert verify(b, old, agent["normalized_phone_number"]).status_code == 200
    expire(old)
    res = claim(b, old)
    assert res.status_code == 410 and res.json()["error"] == "ticket_closed"
    assert verify(browser(), old, agent["normalized_phone_number"]).status_code == 410
    closed = new_ticket()["ticket"]["reference"]
    b2 = browser()
    verify(b2, closed, agent["normalized_phone_number"])
    assert sup.store.admin_close(closed, "spam") == "done"
    assert claim(b2, closed).status_code == 410
    assert verify(browser(), closed, agent["normalized_phone_number"]).status_code == 410
    assert owner_exec("select status from support_tickets where public_ticket_reference = %s", (old,))[0][0] == "PENDING"  # not yet swept,
    assert sup.store.ticket_by_ref(old)["effective_status"] == "EXPIRED"  # but expired all the same


def test_a_deactivated_agent_loses_access_at_once(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    agent = new_agent("Leaving Agent")
    b = browser()
    verify(b, ref, agent["normalized_phone_number"])
    assert claim(b, ref).status_code == 200
    h = {"X-Internal-Token": "internal-test-token"}
    assert client.patch(f"/internal/agents/{agent['id']}", json={"is_active": False}, headers=h).json()["agent"]["is_active"] is False
    assert b.get(f"/api/support/agent/tickets/{ref}").status_code == 401  # session revoked
    assert verify(browser(), ref, agent["normalized_phone_number"]).status_code == 401


def test_only_the_assigned_agent_resolves_or_closes_and_access_ends_with_it(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    agent = new_agent("Closer")
    b = browser()
    verify(b, ref, agent["normalized_phone_number"])
    claim(b, ref)
    assert b.get(f"/api/support/agent/tickets/{ref}/messages").status_code == 200
    res = b.post(f"/api/support/agent/tickets/{ref}/resolve", json={})
    assert res.status_code == 200 and res.json()["ticket"]["status"] == "RESOLVED"
    assert "customer" not in res.json()["ticket"]
    assert b.get(f"/api/support/agent/tickets/{ref}/messages").status_code == 410
    assert b.post(f"/api/support/agent/tickets/{ref}/close", json={}).status_code == 410
    assert client.get(f"/api/support/tickets/{ref}/messages").status_code == 410  # the customer's access ended too
    assert client.get(f"/api/support/tickets/{ref}").json()["ticket"]["status"] == "RESOLVED"  # but they can see why
    row = owner_exec("select resolved_at is not null, (select count(*) from support_ticket_events e where e.ticket_id = t.id and e.action = 'RESOLVED') from support_tickets t where public_ticket_reference = %s", (ref,))[0]
    assert row == (True, 1)


def test_the_admin_api_needs_its_token_and_manages_agents(client):
    assert client.get("/internal/overview").status_code == 401
    assert client.get("/internal/overview", headers={"X-Internal-Token": "wrong"}).status_code == 401
    h = {"X-Internal-Token": "internal-test-token"}
    e164 = random_phone()
    created = client.post("/internal/agents", json={"name": "Meera Nair", "phone": f"{e164[3:8]} {e164[8:]}", "phone_country": "IN"}, headers=h)
    assert created.status_code == 201 and created.json()["agent"]["phone"] == e164
    dup = client.post("/internal/agents", json={"name": "Someone Else", "phone": e164}, headers=h)
    assert dup.status_code == 409
    aid = created.json()["agent"]["id"]
    assert client.patch(f"/internal/agents/{aid}", json={"name": "Meera N."}, headers=h).json()["agent"]["name"] == "Meera N."
    assert client.patch(f"/internal/agents/{aid}", json={"is_active": False}, headers=h).json()["agent"]["is_active"] is False
    assert client.patch(f"/internal/agents/{aid}", json={"is_active": True}, headers=h).json()["agent"]["is_active"] is True
    overview = client.get("/internal/overview", headers=h).json()
    assert any(a["id"] == aid for a in overview["agents"]) and {"pending", "notifications_failed"} <= overview["counts"].keys()
    client.patch(f"/internal/agents/{aid}", json={"is_active": False}, headers=h)
    # Nothing public lists agents or tickets.
    assert client.get("/api/support/agents").status_code == 404 and client.get("/api/support/tickets").status_code == 405


def test_wrong_numbers_are_limited_but_right_ones_from_the_same_office_are_not(client, browser, new_ticket, new_agent):
    ref = new_ticket()["ticket"]["reference"]
    for _ in range(10):
        assert verify(browser(), ref, "+91 91234 56789").status_code == 401
    blocked = verify(browser(), ref, new_agent()["normalized_phone_number"])
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    client.app.state.support.limiter.reset()
    # Twelve colleagues behind one address, all with the right numbers: nobody is locked out.
    refs = [new_ticket()["ticket"]["reference"] for _ in range(3)]
    for i in range(12):
        assert verify(browser(), refs[i % 3], new_agent(f"Colleague {i}")["normalized_phone_number"]).status_code == 200
