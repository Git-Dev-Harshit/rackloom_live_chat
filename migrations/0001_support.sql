-- Rackloom customer support: tickets, the conversation, the agents who answer it, their sessions and
-- the Discord outbox. Applied by `python -m app.cli migrate` as the database owner. The service
-- itself connects as support_app, which reads and writes rows but never changes the schema, never
-- deletes a row, and can never edit a message or an event once written.
--
-- Every timestamp is timestamptz (UTC on the wire). Nothing here deletes or rewrites existing data.

CREATE TABLE support_agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name text NOT NULL CHECK (char_length(agent_name) BETWEEN 1 AND 120),
  -- E.164 ("+919876543210"), the only form kept: what an agent types is normalised before lookup.
  normalized_phone_number text NOT NULL UNIQUE CHECK (normalized_phone_number ~ '^\+[1-9][0-9]{6,14}$'),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE support_tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- RCK- and 8 Crockford base32 characters (no I, L, O, U). An identifier, never a credential.
  public_ticket_reference text NOT NULL UNIQUE CHECK (public_ticket_reference ~ '^RCK-[0-9A-HJKMNP-TV-Z]{8}$'),
  customer_name text NOT NULL CHECK (char_length(customer_name) BETWEEN 1 AND 120),
  customer_email text CHECK (customer_email IS NULL OR char_length(customer_email) BETWEEN 3 AND 254),
  customer_phone text CHECK (customer_phone IS NULL OR customer_phone ~ '^\+[1-9][0-9]{6,14}$'),
  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CLAIMED', 'RESOLVED', 'CLOSED', 'EXPIRED')),
  assigned_agent_id uuid REFERENCES support_agents (id),
  -- SHA-256 of the customer's return-link token. The token itself is never stored.
  customer_token_hash bytea NOT NULL UNIQUE,
  -- SHA-256 of the browser's idempotency key and of the normalised request, so a retried submit
  -- returns the ticket it already created instead of making a second one.
  idempotency_key_hash bytea UNIQUE,
  request_fingerprint bytea,
  discord_message_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  resolved_at timestamptz,
  closed_at timestamptz,
  last_message_at timestamptz,
  access_expires_at timestamptz NOT NULL,
  CONSTRAINT support_tickets_contact CHECK (customer_email IS NOT NULL OR customer_phone IS NOT NULL),
  CONSTRAINT support_tickets_claimed_has_agent CHECK (status <> 'CLAIMED' OR assigned_agent_id IS NOT NULL),
  -- The access window is fixed at creation and can only ever be shorter than seven days, never longer.
  CONSTRAINT support_tickets_window CHECK (access_expires_at > created_at AND access_expires_at <= created_at + interval '7 days')
);
CREATE INDEX support_tickets_status_created_idx ON support_tickets (status, created_at DESC);
CREATE INDEX support_tickets_agent_idx ON support_tickets (assigned_agent_id) WHERE assigned_agent_id IS NOT NULL;
CREATE INDEX support_tickets_open_expiry_idx ON support_tickets (access_expires_at) WHERE status IN ('PENDING', 'CLAIMED');
CREATE INDEX support_tickets_created_idx ON support_tickets (created_at DESC);

CREATE TABLE support_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Delivery order, and the cursor a reconnecting client resumes from.
  seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  ticket_id uuid NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
  sender_type text NOT NULL CHECK (sender_type IN ('CUSTOMER', 'AGENT', 'SYSTEM')),
  agent_id uuid REFERENCES support_agents (id),
  message_content text NOT NULL CHECK (char_length(message_content) BETWEEN 1 AND 4000),
  -- The sender's own id for the message: a resend after a dropped connection is recognised, not stored twice.
  client_message_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT support_messages_agent_sender CHECK ((sender_type = 'AGENT') = (agent_id IS NOT NULL)),
  CONSTRAINT support_messages_client_id_uq UNIQUE (ticket_id, sender_type, client_message_id)
);
CREATE INDEX support_messages_ticket_seq_idx ON support_messages (ticket_id, seq);

-- Browser sessions, one per ticket: a customer's (after creating the ticket or proving the return
-- link) or an agent's (after the phone check). The cookie holds a random token; this holds its hash.
CREATE TABLE support_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash bytea NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('customer', 'agent')),
  ticket_id uuid NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
  agent_id uuid REFERENCES support_agents (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  CONSTRAINT support_sessions_agent CHECK ((kind = 'agent') = (agent_id IS NOT NULL))
);
CREATE INDEX support_sessions_ticket_idx ON support_sessions (ticket_id);
CREATE INDEX support_sessions_agent_idx ON support_sessions (agent_id) WHERE agent_id IS NOT NULL;

-- One-time codes emailed to a customer who opens the return link in a new browser.
CREATE TABLE support_access_codes (
  id uuid PRIMARY KEY,
  ticket_id uuid NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
  code_hash bytea NOT NULL,
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz
);
CREATE INDEX support_access_codes_ticket_idx ON support_access_codes (ticket_id, created_at DESC);

-- What happened to a ticket and who did it: creation, claims, hand-overs, resolution, closure,
-- expiry and customer re-verification. Append-only.
CREATE TABLE support_ticket_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id uuid NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
  agent_id uuid REFERENCES support_agents (id),
  action text NOT NULL CHECK (action IN ('CREATED', 'CLAIMED', 'REASSIGNED', 'RESOLVED', 'CLOSED', 'EXPIRED', 'CUSTOMER_VERIFIED')),
  actor text NOT NULL CHECK (actor IN ('customer', 'agent', 'admin', 'system')),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX support_ticket_events_ticket_idx ON support_ticket_events (ticket_id, created_at);

-- Transactional outbox for Discord. A ticket is committed first; its notification row is committed
-- with it and delivered (and retried) afterwards, so a Discord outage never loses a ticket.
CREATE TABLE support_notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id uuid NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('discord_created', 'discord_updated')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
  attempts integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  lease_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE UNIQUE INDEX support_notifications_created_uq ON support_notifications (ticket_id) WHERE kind = 'discord_created';
CREATE INDEX support_notifications_due_idx ON support_notifications (next_attempt_at) WHERE status = 'pending';
CREATE INDEX support_notifications_ticket_idx ON support_notifications (ticket_id);

-- Messages and events are never edited, by anyone.
CREATE FUNCTION support_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;
CREATE TRIGGER support_messages_no_update BEFORE UPDATE ON support_messages FOR EACH ROW EXECUTE FUNCTION support_append_only();
CREATE TRIGGER support_ticket_events_no_update BEFORE UPDATE ON support_ticket_events FOR EACH ROW EXECUTE FUNCTION support_append_only();

-- The runtime role. Password set by the migrate command from SUPPORT_DB_APP_PASSWORD.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'support_app') THEN
    CREATE ROLE support_app LOGIN;
  END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO support_app;
GRANT SELECT, INSERT, UPDATE ON support_agents, support_tickets, support_sessions, support_access_codes, support_notifications TO support_app;
GRANT SELECT, INSERT ON support_messages, support_ticket_events TO support_app;
