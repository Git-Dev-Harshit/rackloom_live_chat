"""Every SQL statement the service runs, as role support_app. Each public method is one transaction
(the pool commits at the end of the block and rolls back on error). Methods block; the async layer
calls them from a worker thread.

Authorisation that depends on the ticket's state is written into the statements themselves
(a claim is an UPDATE ... WHERE status = 'PENDING'; a message is an INSERT ... SELECT that only
finds a row while the sender may still write), so a check and the change it guards can never be
separated by a race.
"""
from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from .config import ACCESS_DAYS, CODE_MAX_ATTEMPTS, CODE_TTL_MINUTES, ESCALATION_COOLDOWN_HOURS, REPLY_PING_COOLDOWN_MINUTES, REPLY_PING_DELAY_SECONDS
from .tokens import digest, new_reference

OPEN = ("PENDING", "CLAIMED")

# A ticket past its window is EXPIRED whether or not the sweeper has written that down yet.
EFFECTIVE = "case when t.status in ('PENDING', 'CLAIMED') and now() >= t.access_expires_at then 'EXPIRED' else t.status end"

TICKET_SELECT = f"""
select t.id, t.public_ticket_reference as reference, t.customer_name, t.customer_email, t.customer_phone,
       t.status, {EFFECTIVE} as effective_status, t.assigned_agent_id, a.agent_name, a.is_active as agent_active,
       t.created_at, t.updated_at, t.claimed_at, t.resolved_at, t.closed_at, t.last_message_at,
       t.access_expires_at, t.discord_message_id,
       t.customer_delivered_seq, t.customer_read_seq, t.agent_delivered_seq, t.agent_read_seq,
       t.last_escalated_at, t.escalation_count,
       (select coalesce(max(m.seq), 0) from support_messages m where m.ticket_id = t.id) as last_seq
from support_tickets t left join support_agents a on a.id = t.assigned_agent_id
"""


class DuplicateRequest(Exception):
    """The idempotency key was used already (a retried submit)."""


class AgentExists(Exception):
    pass


# Whose progress a read receipt moves. Fixed names, so they can be put into SQL.
RECEIPT_COLUMNS = {"customer": ("customer_delivered_seq", "customer_read_seq"), "agent": ("agent_delivered_seq", "agent_read_seq")}


def first_name(name: str | None) -> str:
    return (name or "").split()[0] if (name or "").split() else "An agent"


class Store:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    # --- tickets -------------------------------------------------------------------------------

    def ticket(self, ticket_id: uuid.UUID) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(TICKET_SELECT + " where t.id = %s", (ticket_id,)).fetchone()

    def ticket_by_ref(self, ref: str) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(TICKET_SELECT + " where t.public_ticket_reference = %s", (ref,)).fetchone()

    def ticket_by_token(self, token_hash: bytes) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(TICKET_SELECT + " where t.customer_token_hash = %s", (token_hash,)).fetchone()

    def find_idempotent(self, idem_hash: bytes) -> dict | None:
        with self.pool.connection() as c:
            row = c.execute(TICKET_SELECT + " where t.idempotency_key_hash = %s", (idem_hash,)).fetchone()
            if row:
                row["request_fingerprint"] = bytes(c.execute("select request_fingerprint from support_tickets where id = %s", (row["id"],)).fetchone()["request_fingerprint"] or b"")
            return row

    def create_ticket(self, *, name: str, email: str | None, phone: str | None, query: str, token_hash: bytes,
                      idem_hash: bytes, fingerprint: bytes, session_hash: bytes) -> dict:
        """Ticket, its first message (the query), the CREATED event, the Discord outbox row and the
        creator's session: all or nothing."""
        for _ in range(5):
            try:
                with self.pool.connection() as c:
                    t = c.execute(
                        """
                        insert into support_tickets (public_ticket_reference, customer_name, customer_email, customer_phone,
                               customer_token_hash, idempotency_key_hash, request_fingerprint, last_message_at, access_expires_at)
                        values (%s, %s, %s, %s, %s, %s, %s, now(), now() + make_interval(days => %s))
                        returning id, access_expires_at
                        """,
                        (new_reference(), name, email, phone, token_hash, idem_hash, fingerprint, ACCESS_DAYS),
                    ).fetchone()
                    c.execute("insert into support_messages (ticket_id, sender_type, message_content) values (%s, 'CUSTOMER', %s)", (t["id"], query))
                    c.execute("insert into support_ticket_events (ticket_id, action, actor) values (%s, 'CREATED', 'customer')", (t["id"],))
                    c.execute("insert into support_notifications (ticket_id, kind) values (%s, 'discord_created')", (t["id"],))
                    c.execute(
                        "insert into support_sessions (token_hash, kind, ticket_id, expires_at) values (%s, 'customer', %s, %s)",
                        (session_hash, t["id"], t["access_expires_at"]),
                    )
                return self.ticket(t["id"])
            except psycopg.errors.UniqueViolation as e:
                constraint = e.diag.constraint_name or ""
                if constraint == "support_tickets_idempotency_key_hash_key":
                    raise DuplicateRequest() from None
                if constraint == "support_tickets_public_ticket_reference_key":
                    continue  # 40-bit reference collision: draw another
                raise
        raise RuntimeError("could not allocate a ticket reference")

    # --- sessions ------------------------------------------------------------------------------

    def add_session(self, *, kind: str, ticket_id: uuid.UUID, token_hash: bytes, expires_at: datetime, agent_id: uuid.UUID | None = None) -> uuid.UUID:
        with self.pool.connection() as c:
            return c.execute(
                "insert into support_sessions (token_hash, kind, ticket_id, agent_id, expires_at) values (%s, %s, %s, %s, %s) returning id",
                (token_hash, kind, ticket_id, agent_id, expires_at),
            ).fetchone()["id"]

    def session(self, token_hash: bytes, kind: str) -> dict | None:
        """A live session of this kind: not revoked, not past its expiry."""
        with self.pool.connection() as c:
            return c.execute(
                """
                select s.id as session_id, s.ticket_id, s.agent_id, s.expires_at, t.public_ticket_reference as reference
                from support_sessions s join support_tickets t on t.id = s.ticket_id
                where s.token_hash = %s and s.kind = %s and s.revoked_at is null and s.expires_at > now()
                """,
                (token_hash, kind),
            ).fetchone()

    def valid_sessions(self, session_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these open sockets may still be open: the same rule as connecting, checked again."""
        if not session_ids:
            return set()
        with self.pool.connection() as c:
            rows = c.execute(
                """
                select s.id from support_sessions s
                join support_tickets t on t.id = s.ticket_id
                left join support_agents a on a.id = s.agent_id
                where s.id = any(%s) and s.revoked_at is null and s.expires_at > now() and now() < t.access_expires_at
                  and ((s.kind = 'customer' and t.status in ('PENDING', 'CLAIMED'))
                    or (s.kind = 'agent' and t.status = 'CLAIMED' and t.assigned_agent_id = s.agent_id and a.is_active))
                """,
                (list(session_ids),),
            ).fetchall()
        return {r["id"] for r in rows}

    # --- messages ------------------------------------------------------------------------------

    def messages(self, ticket_id: uuid.UUID, *, after: int = 0, limit: int = 100) -> tuple[list[dict], bool]:
        with self.pool.connection() as c:
            rows = c.execute(
                """
                select m.id, m.seq, m.sender_type, m.message_content, m.created_at, a.agent_name
                from support_messages m left join support_agents a on a.id = m.agent_id
                where m.ticket_id = %s and m.seq > %s order by m.seq limit %s
                """,
                (ticket_id, after, limit + 1),
            ).fetchall()
        return rows[:limit], len(rows) > limit

    def insert_message(self, *, ticket_id: uuid.UUID, session_id: uuid.UUID, sender: str, agent_id: uuid.UUID | None,
                       body: str, client_id: uuid.UUID) -> tuple[str, dict | None]:
        """("ok", message) | ("duplicate", the message stored earlier) | ("not_allowed", None).

        The row is written only if, at this instant, the ticket is claimed and inside its window,
        the session is live, and (for an agent) the ticket is still theirs and they are active.
        """
        agent_rule = "t.assigned_agent_id = %(agent)s::uuid and exists (select 1 from support_agents a where a.id = %(agent)s::uuid and a.is_active)" if sender == "AGENT" else "true"
        with self.pool.connection() as c:
            row = c.execute(
                f"""
                insert into support_messages (ticket_id, sender_type, agent_id, message_content, client_message_id)
                select t.id, %(sender)s::text, %(agent)s::uuid, %(body)s::text, %(cid)s::uuid
                from support_tickets t
                where t.id = %(tid)s and t.status = 'CLAIMED' and now() < t.access_expires_at and {agent_rule}
                  and exists (select 1 from support_sessions s where s.id = %(sid)s and s.ticket_id = t.id
                              and s.revoked_at is null and s.expires_at > now())
                on conflict (ticket_id, sender_type, client_message_id) do nothing
                returning id, seq, sender_type, message_content, created_at
                """,
                {"sender": sender, "agent": agent_id, "body": body, "cid": client_id, "tid": ticket_id, "sid": session_id},
            ).fetchone()
            if row:
                c.execute("update support_tickets set last_message_at = now(), updated_at = now() where id = %s", (ticket_id,))
                if sender == "CUSTOMER":
                    self._queue_reply_ping(c, ticket_id)
                return "ok", self._with_author(c, row, agent_id)
            dup = c.execute(
                """
                select m.id, m.seq, m.sender_type, m.message_content, m.created_at, a.agent_name
                from support_messages m left join support_agents a on a.id = m.agent_id
                where m.ticket_id = %s and m.sender_type = %s and m.client_message_id = %s
                """,
                (ticket_id, sender, client_id),
            ).fetchone()
            return ("duplicate", dup) if dup else ("not_allowed", None)

    @staticmethod
    def _queue_reply_ping(c, ticket_id: uuid.UUID) -> None:
        """A "your customer wrote again" post for the assigned agent, due a little later. Whether it is
        sent is decided then: only if the agent has still not read the message. At most one waits per
        ticket (a partial unique index), and none is queued within the cooldown after the last one."""
        c.execute(
            """
            insert into support_notifications (ticket_id, kind, next_attempt_at)
            select %(tid)s, 'discord_customer_reply', now() + make_interval(secs => %(delay)s)
            where not exists (select 1 from support_notifications where ticket_id = %(tid)s and kind = 'discord_customer_reply'
                              and status = 'sent' and sent_at > now() - make_interval(mins => %(cool)s))
            on conflict do nothing
            """,
            {"tid": ticket_id, "delay": REPLY_PING_DELAY_SECONDS, "cool": REPLY_PING_COOLDOWN_MINUTES},
        )

    def record_receipt(self, ticket_id: uuid.UUID, role: str, *, delivered: int, read: int) -> dict | None:
        """Move one side's delivered/read cursors forwards (never back). Returns all four cursors when
        something moved, None when nothing did. The caller bounds the values by what it has sent."""
        d_col, r_col = RECEIPT_COLUMNS[role]
        delivered = max(delivered, read)
        with self.pool.connection() as c:
            return c.execute(
                f"""
                update support_tickets set {d_col} = greatest({d_col}, %(d)s), {r_col} = greatest({r_col}, %(r)s)
                where id = %(id)s and ({d_col} < %(d)s or {r_col} < %(r)s)
                returning customer_delivered_seq, customer_read_seq, agent_delivered_seq, agent_read_seq
                """,
                {"id": ticket_id, "d": delivered, "r": read},
            ).fetchone()

    def reply_ping_context(self, ticket_id: uuid.UUID) -> dict:
        """What the customer wrote that the agent has not read: how many messages, the latest one and when.

        Unread is after the agent's read cursor, after their last reply (they answered what came before)
        and after the claim (claiming shows the conversation so far). The last two also cover tickets
        claimed before read receipts existed, whose cursor is still 0.
        """
        with self.pool.connection() as c:
            return c.execute(
                """
                select count(*) as unread,
                       (array_agg(m.message_content order by m.seq desc))[1] as latest,
                       max(m.created_at) as latest_at
                from support_messages m join support_tickets t on t.id = m.ticket_id
                where m.ticket_id = %(id)s and m.sender_type = 'CUSTOMER' and m.created_at > t.claimed_at
                  and m.seq > greatest(t.agent_read_seq,
                        (select coalesce(max(r.seq), 0) from support_messages r where r.ticket_id = %(id)s and r.sender_type = 'AGENT'))
                """,
                {"id": ticket_id},
            ).fetchone()

    @staticmethod
    def _with_author(c, row: dict, agent_id: uuid.UUID | None) -> dict:
        row["agent_name"] = c.execute("select agent_name from support_agents where id = %s", (agent_id,)).fetchone()["agent_name"] if agent_id else None
        return row

    # --- agents and claiming -------------------------------------------------------------------

    def agent_by_phone(self, e164: str) -> dict | None:
        with self.pool.connection() as c:
            return c.execute("select id, agent_name, normalized_phone_number, is_active from support_agents where normalized_phone_number = %s", (e164,)).fetchone()

    def claim(self, ticket_id: uuid.UUID, agent_id: uuid.UUID) -> str:
        """claimed | already_yours | taken | expired | ended | agent_inactive | not_found.

        One conditional UPDATE: under concurrent claims Postgres re-checks `status = 'PENDING'` on
        the row once the first claim commits, so exactly one agent wins and nobody is overwritten.
        """
        with self.pool.connection() as c:
            won = c.execute(
                """
                update support_tickets t set status = 'CLAIMED', assigned_agent_id = %(agent)s, claimed_at = now(), updated_at = now()
                where t.id = %(id)s and t.status = 'PENDING' and now() < t.access_expires_at
                  and exists (select 1 from support_agents a where a.id = %(agent)s and a.is_active)
                returning t.id
                """,
                {"id": ticket_id, "agent": agent_id},
            ).fetchone()
            if won:
                name = c.execute("select agent_name from support_agents where id = %s", (agent_id,)).fetchone()["agent_name"]
                c.execute("insert into support_ticket_events (ticket_id, agent_id, action, actor) values (%s, %s, 'CLAIMED', 'agent')", (ticket_id, agent_id))
                self._system(c, ticket_id, f"{first_name(name)} from Rackloom Support has joined the conversation.")
                self._notify(c, ticket_id)
                return "claimed"
            t = c.execute(f"select t.status, t.assigned_agent_id, {EFFECTIVE} as effective from support_tickets t where t.id = %s", (ticket_id,)).fetchone()
            if not t:
                return "not_found"
            if t["effective"] == "EXPIRED":
                return "expired"
            if t["status"] in ("RESOLVED", "CLOSED", "EXPIRED"):
                return "ended"
            if t["status"] == "CLAIMED":
                return "already_yours" if t["assigned_agent_id"] == agent_id else "taken"
            return "agent_inactive"

    def agent_transition(self, ticket_id: uuid.UUID, agent_id: uuid.UUID, to: str) -> str:
        """The assigned agent resolves or closes a claimed ticket: done | not_yours | ended | expired."""
        assert to in ("RESOLVED", "CLOSED")
        with self.pool.connection() as c:
            row = c.execute(
                """
                update support_tickets set status = %(to)s, updated_at = now(),
                       resolved_at = case when %(to)s = 'RESOLVED' then now() else resolved_at end,
                       closed_at = case when %(to)s = 'CLOSED' then now() else closed_at end
                where id = %(id)s and status = 'CLAIMED' and assigned_agent_id = %(agent)s and now() < access_expires_at
                returning id
                """,
                {"to": to, "id": ticket_id, "agent": agent_id},
            ).fetchone()
            if row:
                name = c.execute("select agent_name from support_agents where id = %s", (agent_id,)).fetchone()["agent_name"]
                c.execute("insert into support_ticket_events (ticket_id, agent_id, action, actor) values (%s, %s, %s, 'agent')", (ticket_id, agent_id, to))
                text = (f"{first_name(name)} marked this ticket as resolved. This conversation is now closed." if to == "RESOLVED"
                        else "This conversation was closed by Rackloom Support.")
                self._system(c, ticket_id, text)
                self._notify(c, ticket_id)
                return "done"
            t = c.execute(f"select t.status, t.assigned_agent_id, {EFFECTIVE} as effective from support_tickets t where t.id = %s", (ticket_id,)).fetchone()
            if not t:
                return "not_found"
            if t["effective"] == "EXPIRED":
                return "expired"
            if t["status"] in ("RESOLVED", "CLOSED"):
                return "ended"
            return "not_yours"

    def escalate(self, ticket_id: uuid.UUID, note: str | None) -> tuple[str, datetime | None]:
        """The customer escalates their claimed ticket: ("done", None) | ("cooldown", when it is allowed
        again) | ("not_claimed", None) | ("ended", None).

        Once in any ESCALATION_COOLDOWN_HOURS, enforced by the UPDATE itself, so two clicks (or two
        tabs) at the same instant escalate once.
        """
        with self.pool.connection() as c:
            row = c.execute(
                """
                update support_tickets set last_escalated_at = now(), escalation_count = escalation_count + 1, updated_at = now()
                where id = %(id)s and status = 'CLAIMED' and now() < access_expires_at
                  and (last_escalated_at is null or last_escalated_at <= now() - make_interval(hours => %(h)s))
                returning escalation_count, assigned_agent_id
                """,
                {"id": ticket_id, "h": ESCALATION_COOLDOWN_HOURS},
            ).fetchone()
            if row:
                detail = {"count": row["escalation_count"], "note": note or ""}
                c.execute(
                    "insert into support_ticket_events (ticket_id, agent_id, action, actor, metadata) values (%s, %s, 'ESCALATED', 'customer', %s)",
                    (ticket_id, row["assigned_agent_id"], json.dumps(detail)),
                )
                text = "This ticket was escalated to the Rackloom Support team for priority attention."
                self._system(c, ticket_id, f"{text} Reason given: “{note}”" if note else text)
                c.execute("insert into support_notifications (ticket_id, kind, detail) values (%s, 'discord_escalated', %s)", (ticket_id, json.dumps(detail)))
                self._notify(c, ticket_id)  # the ticket's own channel post shows the escalation too
                return "done", None
            t = c.execute(
                f"""select t.status, {EFFECTIVE} as effective, t.last_escalated_at + make_interval(hours => %s) as next_at
                    from support_tickets t where t.id = %s""",
                (ESCALATION_COOLDOWN_HOURS, ticket_id),
            ).fetchone()
            if not t or t["effective"] not in ("PENDING", "CLAIMED"):
                return "ended", None
            if t["effective"] == "PENDING":
                return "not_claimed", None
            return "cooldown", t["next_at"]

    def admin_reassign(self, ref: str, agent_id: uuid.UUID) -> str:
        """An administrator hands a claimed ticket to another active agent: done | not_found | not_claimed | agent_inactive | same."""
        with self.pool.connection() as c:
            t = c.execute(f"select t.id, t.status, t.assigned_agent_id, {EFFECTIVE} as effective from support_tickets t where t.public_ticket_reference = %s for update", (ref,)).fetchone()
            if not t:
                return "not_found"
            if t["effective"] != "CLAIMED":
                return "not_claimed"
            if t["assigned_agent_id"] == agent_id:
                return "same"
            agent = c.execute("select agent_name, is_active from support_agents where id = %s", (agent_id,)).fetchone()
            if not agent or not agent["is_active"]:
                return "agent_inactive"
            c.execute("update support_tickets set assigned_agent_id = %s, updated_at = now() where id = %s", (agent_id, t["id"]))
            c.execute(
                "insert into support_ticket_events (ticket_id, agent_id, action, actor, metadata) values (%s, %s, 'REASSIGNED', 'admin', %s)",
                (t["id"], agent_id, json.dumps({"from_agent_id": str(t["assigned_agent_id"])})),
            )
            # The previous agent's sessions end with the hand-over.
            c.execute("update support_sessions set revoked_at = now() where ticket_id = %s and kind = 'agent' and agent_id <> %s and revoked_at is null", (t["id"], agent_id))
            self._system(c, t["id"], f"This conversation was handed over to {first_name(agent['agent_name'])}.")
            self._notify(c, t["id"])
            return "done"

    def admin_close(self, ref: str, reason: str = "") -> str:
        """An administrator closes a ticket in any open or resolved state: done | not_found | ended."""
        with self.pool.connection() as c:
            row = c.execute(
                "update support_tickets set status = 'CLOSED', closed_at = now(), updated_at = now() "
                "where public_ticket_reference = %s and status in ('PENDING', 'CLAIMED', 'RESOLVED') returning id",
                (ref,),
            ).fetchone()
            if not row:
                return "ended" if c.execute("select 1 from support_tickets where public_ticket_reference = %s", (ref,)).fetchone() else "not_found"
            c.execute(
                "insert into support_ticket_events (ticket_id, action, actor, metadata) values (%s, 'CLOSED', 'admin', %s)",
                (row["id"], json.dumps({"reason": reason[:200]} if reason else {})),
            )
            self._system(c, row["id"], "This conversation was closed by Rackloom Support.")
            self._notify(c, row["id"])
            return "done"

    def expire_due(self) -> list[uuid.UUID]:
        """Write down EXPIRED for open tickets past their window. Enforcement never waits for this."""
        with self.pool.connection() as c:
            rows = c.execute(
                """
                with expired as (
                  update support_tickets set status = 'EXPIRED', updated_at = now()
                  where status in ('PENDING', 'CLAIMED') and access_expires_at <= now() returning id
                ), ev as (
                  insert into support_ticket_events (ticket_id, action, actor) select id, 'EXPIRED', 'system' from expired
                ), msg as (
                  insert into support_messages (ticket_id, sender_type, message_content)
                  select id, 'SYSTEM', 'This conversation has expired.' from expired
                ), nt as (
                  insert into support_notifications (ticket_id, kind) select id, 'discord_updated' from expired
                )
                select id from expired
                """
            ).fetchall()
        return [r["id"] for r in rows]

    @staticmethod
    def _system(c, ticket_id: uuid.UUID, text: str) -> None:
        c.execute("insert into support_messages (ticket_id, sender_type, message_content) values (%s, 'SYSTEM', %s)", (ticket_id, text))

    @staticmethod
    def _notify(c, ticket_id: uuid.UUID) -> None:
        c.execute("insert into support_notifications (ticket_id, kind) values (%s, 'discord_updated')", (ticket_id,))

    # --- the new-browser email check -----------------------------------------------------------

    def codes_sent_since(self, ticket_id: uuid.UUID, seconds: int) -> int:
        with self.pool.connection() as c:
            return c.execute(
                "select count(*) as n from support_access_codes where ticket_id = %s and created_at > now() - make_interval(secs => %s)",
                (ticket_id, seconds),
            ).fetchone()["n"]

    def create_code(self, ticket_id: uuid.UUID, code: str) -> None:
        code_id = uuid.uuid4()
        with self.pool.connection() as c:
            # A new code replaces any earlier one still waiting.
            c.execute("update support_access_codes set used_at = now() where ticket_id = %s and used_at is null", (ticket_id,))
            c.execute(
                "insert into support_access_codes (id, ticket_id, code_hash, expires_at) values (%s, %s, %s, now() + make_interval(mins => %s))",
                (code_id, ticket_id, digest(f"{code_id}:{code}"), CODE_TTL_MINUTES),
            )

    def redeem_code(self, ticket_id: uuid.UUID, code: str, *, session_hash: bytes, session_expires: datetime) -> tuple[str, int]:
        """("ok", 0) and a new customer session | ("invalid", attempts left) | ("expired", 0) | ("locked", 0)."""
        with self.pool.connection() as c:
            row = c.execute(
                "select id, code_hash, attempts, expires_at > now() as live from support_access_codes "
                "where ticket_id = %s and used_at is null order by created_at desc limit 1 for update",
                (ticket_id,),
            ).fetchone()
            if not row or not row["live"]:
                return "expired", 0
            if row["attempts"] >= CODE_MAX_ATTEMPTS:
                return "locked", 0
            c.execute("update support_access_codes set attempts = attempts + 1 where id = %s", (row["id"],))
            if not hmac.compare_digest(bytes(row["code_hash"]), digest(f"{row['id']}:{code}")):
                left = CODE_MAX_ATTEMPTS - row["attempts"] - 1
                return ("invalid", left) if left > 0 else ("locked", 0)
            c.execute("update support_access_codes set used_at = now() where id = %s", (row["id"],))
            c.execute(
                "insert into support_sessions (token_hash, kind, ticket_id, expires_at) values (%s, 'customer', %s, %s)",
                (session_hash, ticket_id, session_expires),
            )
            c.execute("insert into support_ticket_events (ticket_id, action, actor) values (%s, 'CUSTOMER_VERIFIED', 'customer')", (ticket_id,))
            return "ok", 0

    # --- the Discord outbox --------------------------------------------------------------------

    def claim_notifications(self, *, limit: int = 10, lease_seconds: int = 60) -> list[dict]:
        """Lease due rows. A crashed delivery's lease runs out and the row is picked up again."""
        with self.pool.connection() as c:
            return c.execute(
                """
                update support_notifications n set lease_until = now() + make_interval(secs => %s), attempts = attempts + 1, last_attempt_at = now()
                where n.id in (
                  select id from support_notifications
                  where status = 'pending' and next_attempt_at <= now() and (lease_until is null or lease_until < now())
                  order by next_attempt_at limit %s for update skip locked)
                returning n.id, n.ticket_id, n.kind, n.attempts, n.detail
                """,
                (lease_seconds, limit),
            ).fetchall()

    def notification_ticket(self, ticket_id: uuid.UUID) -> dict | None:
        """The ticket as the channel shows it: current state plus the customer's original query."""
        with self.pool.connection() as c:
            t = c.execute(TICKET_SELECT + " where t.id = %s", (ticket_id,)).fetchone()
            if t:
                q = c.execute("select message_content from support_messages where ticket_id = %s and sender_type = 'CUSTOMER' order by seq limit 1", (ticket_id,)).fetchone()
                t["query"] = q["message_content"] if q else ""
            return t

    def creation_post_pending(self, ticket_id: uuid.UUID) -> bool:
        with self.pool.connection() as c:
            return bool(c.execute("select 1 from support_notifications where ticket_id = %s and kind = 'discord_created' and status = 'pending'", (ticket_id,)).fetchone())

    def notification_sent(self, nid: uuid.UUID, *, message_id: str | None = None, ticket_id: uuid.UUID | None = None) -> None:
        with self.pool.connection() as c:
            c.execute("update support_notifications set status = 'sent', sent_at = now(), lease_until = null, last_error = null where id = %s", (nid,))
            if message_id and ticket_id:
                c.execute("update support_tickets set discord_message_id = %s where id = %s", (message_id, ticket_id))

    def notification_finish(self, nid: uuid.UUID, *, status: str, error: str | None = None) -> None:
        assert status in ("failed", "skipped")
        with self.pool.connection() as c:
            c.execute("update support_notifications set status = %s, lease_until = null, last_error = %s where id = %s", (status, error, nid))

    def notification_retry(self, nid: uuid.UUID, *, error: str, delay_seconds: float) -> None:
        with self.pool.connection() as c:
            c.execute(
                "update support_notifications set lease_until = null, last_error = %s, next_attempt_at = now() + make_interval(secs => %s) where id = %s",
                (error, delay_seconds, nid),
            )

    def retry_failed_notifications(self, ref: str) -> int:
        with self.pool.connection() as c:
            return c.execute(
                "update support_notifications n set status = 'pending', attempts = 0, next_attempt_at = now(), lease_until = null "
                "from support_tickets t where t.id = n.ticket_id and t.public_ticket_reference = %s and n.status = 'failed'",
                (ref,),
            ).rowcount

    def counts(self) -> dict:
        with self.pool.connection() as c:
            return c.execute(
                f"""
                select
                  (select count(*) from support_tickets t where {EFFECTIVE} = 'PENDING') as pending,
                  (select count(*) from support_tickets t where {EFFECTIVE} = 'CLAIMED') as claimed,
                  (select count(*) from support_notifications where status = 'pending') as notifications_pending,
                  (select count(*) from support_notifications where status = 'failed') as notifications_failed,
                  (select count(*) from support_agents where is_active) as active_agents
                """
            ).fetchone()

    # --- administration ------------------------------------------------------------------------

    def list_agents(self) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute(
                """
                select a.id, a.agent_name, a.normalized_phone_number, a.is_active, a.created_at, a.updated_at,
                       (select count(*) from support_tickets t where t.assigned_agent_id = a.id and t.status = 'CLAIMED' and now() < t.access_expires_at) as open_tickets
                from support_agents a order by a.is_active desc, a.agent_name
                """
            ).fetchall()

    def agent(self, agent_id: uuid.UUID) -> dict | None:
        with self.pool.connection() as c:
            return c.execute("select id, agent_name, normalized_phone_number, is_active, created_at, updated_at from support_agents where id = %s", (agent_id,)).fetchone()

    def create_agent(self, name: str, e164: str) -> dict:
        try:
            with self.pool.connection() as c:
                return c.execute(
                    "insert into support_agents (agent_name, normalized_phone_number) values (%s, %s) returning id, agent_name, normalized_phone_number, is_active, created_at, updated_at",
                    (name, e164),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise AgentExists() from None

    def update_agent(self, agent_id: uuid.UUID, *, name: str | None = None, e164: str | None = None, active: bool | None = None) -> dict | None:
        sets, args = [], []
        if name is not None:
            sets.append("agent_name = %s")
            args.append(name)
        if e164 is not None:
            sets.append("normalized_phone_number = %s")
            args.append(e164)
        if active is not None:
            sets.append("is_active = %s")
            args.append(active)
        if not sets:
            return self.agent(agent_id)
        try:
            with self.pool.connection() as c:
                row = c.execute(
                    f"update support_agents set {', '.join(sets)}, updated_at = now() where id = %s returning id, agent_name, normalized_phone_number, is_active, created_at, updated_at",
                    (*args, agent_id),
                ).fetchone()
                # A deactivated agent, or one whose number changed, is signed out everywhere at once.
                if row and (active is False or e164 is not None):
                    c.execute("update support_sessions set revoked_at = now() where agent_id = %s and revoked_at is null", (agent_id,))
                return row
        except psycopg.errors.UniqueViolation:
            raise AgentExists() from None

    def list_tickets(self, *, limit: int = 50, status: str | None = None) -> list[dict]:
        where = f"where {EFFECTIVE} = %(status)s" if status else ""
        with self.pool.connection() as c:
            return c.execute(
                f"""
                select t.public_ticket_reference as reference, {EFFECTIVE} as effective_status, a.agent_name,
                       t.created_at, t.claimed_at, t.resolved_at, t.closed_at, t.last_message_at, t.access_expires_at,
                       t.last_escalated_at, t.escalation_count,
                       (select count(*) from support_messages m where m.ticket_id = t.id and m.sender_type <> 'SYSTEM') as message_count,
                       n.status as discord_status, n.attempts as discord_attempts, n.last_error as discord_error
                from support_tickets t
                left join support_agents a on a.id = t.assigned_agent_id
                left join support_notifications n on n.ticket_id = t.id and n.kind = 'discord_created'
                {where}
                order by t.created_at desc limit %(limit)s
                """,
                {"status": status, "limit": limit},
            ).fetchall()
