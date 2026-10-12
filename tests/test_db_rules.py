"""The database's own guarantees, checked as the role the service runs as."""
from __future__ import annotations

import psycopg
import pytest

from .conftest import APP_URL, OWNER, needs_db, owner_exec

pytestmark = needs_db


@pytest.fixture()
def ticket_ref(new_ticket):
    return new_ticket()["ticket"]["reference"]


def as_app(sql: str, params: tuple = ()) -> None:
    with psycopg.connect(APP_URL, autocommit=True) as c:
        c.execute(sql, params)


def test_the_service_role_cannot_edit_history_delete_rows_or_change_the_schema(ticket_ref):
    for sql in [
        "update support_messages set message_content = 'edited' where ticket_id = (select id from support_tickets where public_ticket_reference = %s)",
        "update support_ticket_events set actor = 'admin' where ticket_id = (select id from support_tickets where public_ticket_reference = %s)",
    ]:
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)):
            as_app(sql, (ticket_ref,))
    for sql in [
        "delete from support_tickets where public_ticket_reference = %s",
        "delete from support_messages where ticket_id = (select id from support_tickets where public_ticket_reference = %s)",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_app(sql, (ticket_ref,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_app("alter table support_tickets add column x int")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_app("create table support_x (id int)")
    # Even the owner cannot rewrite a message.
    with pytest.raises(psycopg.errors.RaiseException):
        owner_exec("update support_messages set message_content = 'edited' where ticket_id = (select id from support_tickets where public_ticket_reference = %s)", (ticket_ref,))


def test_the_schema_refuses_impossible_tickets(ticket_ref):
    with pytest.raises(psycopg.errors.CheckViolation):  # a window longer than seven days
        owner_exec("update support_tickets set access_expires_at = created_at + interval '8 days' where public_ticket_reference = %s", (ticket_ref,))
    with pytest.raises(psycopg.errors.CheckViolation):  # claimed by nobody
        owner_exec("update support_tickets set status = 'CLAIMED' where public_ticket_reference = %s", (ticket_ref,))
    with pytest.raises(psycopg.errors.CheckViolation):  # no way to reach the customer
        owner_exec("update support_tickets set customer_email = null, customer_phone = null where public_ticket_reference = %s", (ticket_ref,))
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_exec("update support_tickets set status = 'LOST' where public_ticket_reference = %s", (ticket_ref,))


def test_migrating_again_changes_nothing_and_keeps_every_row(ticket_ref):
    from app.db import migrate

    before = owner_exec("select (select count(*) from support_tickets), (select count(*) from support_messages), (select count(*) from support_agents)")[0]
    assert migrate(OWNER) == []
    after = owner_exec("select (select count(*) from support_tickets), (select count(*) from support_messages), (select count(*) from support_agents)")[0]
    assert before == after
    assert owner_exec("select count(*) from support_schema_migrations where version = '0001_support'")[0][0] == 1


def test_tokens_are_stored_only_as_hashes(new_ticket):
    created = new_ticket()
    token = created["return_url"].split("#k=")[1]
    row = owner_exec("select customer_token_hash from support_tickets where public_ticket_reference = %s", (created["ticket"]["reference"],))[0][0]
    assert len(bytes(row)) == 32 and token.encode() not in bytes(row)
    dump = str(owner_exec("select t.*, s.* from support_tickets t join support_sessions s on s.ticket_id = t.id where t.public_ticket_reference = %s", (created["ticket"]["reference"],)))
    assert token not in dump
