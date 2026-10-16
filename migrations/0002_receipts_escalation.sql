-- Read receipts, customer escalation, and the Discord ping when a customer writes to an agent who is
-- not looking. Additive only: new columns with defaults, wider CHECK lists, one index. No row is
-- rewritten and nothing is dropped except the two CHECK constraints that are recreated wider.

-- How far each side has got through the conversation, as a message seq (seq only grows within a
-- ticket, so "everything up to N" is one number). Delivered: their browser has the message. Read: it
-- was on their screen. Messages stay append-only; only these cursors move, and only forwards.
ALTER TABLE support_tickets
  ADD COLUMN customer_delivered_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN customer_read_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN agent_delivered_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN agent_read_seq bigint NOT NULL DEFAULT 0,
  -- The customer may escalate an open ticket at most once in any 24 hours (enforced in the UPDATE).
  ADD COLUMN last_escalated_at timestamptz,
  ADD COLUMN escalation_count integer NOT NULL DEFAULT 0 CHECK (escalation_count >= 0);

ALTER TABLE support_ticket_events DROP CONSTRAINT support_ticket_events_action_check;
ALTER TABLE support_ticket_events ADD CONSTRAINT support_ticket_events_action_check
  CHECK (action IN ('CREATED', 'CLAIMED', 'REASSIGNED', 'RESOLVED', 'CLOSED', 'EXPIRED', 'CUSTOMER_VERIFIED', 'ESCALATED'));

-- discord_customer_reply: a new channel post telling the assigned agent their customer wrote again.
-- discord_escalated: a new channel post asking the team to take up an escalated ticket.
ALTER TABLE support_notifications DROP CONSTRAINT support_notifications_kind_check;
ALTER TABLE support_notifications ADD CONSTRAINT support_notifications_kind_check
  CHECK (kind IN ('discord_created', 'discord_updated', 'discord_customer_reply', 'discord_escalated'));
-- What a post needs beyond the ticket's current state (the escalation's note and number).
ALTER TABLE support_notifications ADD COLUMN detail jsonb NOT NULL DEFAULT '{}'::jsonb;

-- At most one "your customer wrote" ping waiting per ticket, however many messages arrive.
CREATE UNIQUE INDEX support_notifications_reply_pending_uq ON support_notifications (ticket_id)
  WHERE kind = 'discord_customer_reply' AND status = 'pending';
