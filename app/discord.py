"""The private support channel, through an incoming webhook.

A webhook (not a bot) is enough here: claiming happens on our agent page, so the message only
needs a link, and a webhook can edit the messages it posted to show the ticket's progress. It
needs no bot token, no gateway connection and no permissions beyond the one channel.

The webhook URL is a secret (it lets anyone post to the channel): it is never logged, and every
error string that leaves this module has it removed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d+)/([A-Za-z0-9_-]+)$")
# Who an escalation may ping: the whole channel, one role or one person. Nothing else ever pings.
MENTION_RE = re.compile(r"^(?:@here|@everyone|<@&(\d{5,25})>|<@!?(\d{5,25})>)$")

STATUS_COLOR = {"PENDING": 0xB45309, "CLAIMED": 0x0A66C2, "RESOLVED": 0x15803D, "CLOSED": 0x6B7280, "EXPIRED": 0x6B7280}
REPLY_COLOR = 0x0A66C2
ESCALATION_COLOR = 0xB91C1C
QUERY_PREVIEW = 300


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def valid_webhook_url(url: str) -> bool:
    return bool(WEBHOOK_RE.fullmatch(url.strip()))


def valid_mention(mention: str) -> bool:
    return bool(MENTION_RE.fullmatch(mention.strip()))


def allowed_mentions_for(mention: str) -> dict:
    """Exactly the configured mention may ping, nothing in the text around it."""
    m = MENTION_RE.fullmatch(mention.strip()) if mention else None
    if not m:
        return {"parse": []}
    if m.group(1):
        return {"parse": [], "roles": [m.group(1)]}
    if m.group(2):
        return {"parse": [], "users": [m.group(2)]}
    return {"parse": ["everyone"]}  # @here and @everyone


class DiscordWebhook:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self.url = url.strip()
        if self.url and not valid_webhook_url(self.url):
            raise ValueError("SUPPORT_DISCORD_WEBHOOK_URL is not a Discord webhook URL")
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create(self, payload: dict) -> str:
        """Posts and returns the new message's id (wait=true makes Discord confirm and answer)."""
        data = await self._send("POST", f"{self.url}?wait=true", payload)
        mid = str(data.get("id", "")) if isinstance(data, dict) else ""
        if not mid.isdigit():
            raise DeliveryError("discord answered without a message id", retryable=False)
        return mid

    async def edit(self, message_id: str, payload: dict) -> None:
        if not message_id.isdigit():
            raise DeliveryError("bad message id", retryable=False)
        await self._send("PATCH", f"{self.url}/messages/{message_id}", payload)

    async def _send(self, method: str, url: str, payload: dict):
        try:
            resp = await self._client.request(method, url, json=payload)
        except httpx.TimeoutException as e:
            raise DeliveryError(f"timeout ({type(e).__name__})", retryable=True) from None
        except httpx.HTTPError as e:
            raise DeliveryError(f"network error ({type(e).__name__})", retryable=True) from None
        if resp.status_code == 429:
            try:
                after = float(resp.json().get("retry_after", 5))
            except (ValueError, AttributeError):
                after = 5.0
            raise DeliveryError("rate limited by discord", retryable=True, retry_after=after)
        if resp.status_code >= 500:
            raise DeliveryError(f"discord answered {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            # 401/403/404: the webhook was deleted or the URL is wrong; 400: we sent something it refuses.
            raise DeliveryError(f"discord refused the request ({resp.status_code}): {_error_text(resp)}", retryable=False)
        return resp.json() if resp.content else {}


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message", ""))[:200] if isinstance(body, dict) else ""
    except ValueError:
        return ""


def redact(text: str, secret_url: str) -> str:
    """Remove the webhook URL, and any webhook token-looking path, from an error before storing it."""
    out = text.replace(secret_url, "[webhook]") if secret_url else text
    return re.sub(r"/api/webhooks/\d+/[A-Za-z0-9_-]+", "/api/webhooks/[redacted]", out)[:500]


_MD = re.compile(r"([\\*_~`|>#\[\]()<:-])")


def escape_markdown(text: str) -> str:
    """Customer text shown verbatim: no formatting, no masked links, no <@id> mention syntax.
    Pings are also switched off per message (allowed_mentions), so a bare @everyone is just text."""
    return _MD.sub(r"\\\1", text)


def _preview(text: str) -> str:
    t = " ".join(text.split())
    return escape_markdown(t if len(t) <= QUERY_PREVIEW else t[: QUERY_PREVIEW - 1].rstrip() + "…")


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _when(dt: datetime | None) -> str:
    """Discord renders <t:...> in each reader's own time zone; the UTC text is for search and logs."""
    if not dt:
        return "–"
    u = _utc(dt)
    return f"{u:%Y-%m-%d %H:%M} UTC (<t:{int(u.timestamp())}:R>)"


def ticket_payload(ticket: dict, portal_url: str) -> dict:
    """The channel message for a ticket in its current state (the same builder posts and edits)."""
    status = ticket["effective_status"]
    created = _utc(ticket["created_at"])
    fields = [
        {"name": "Ticket ID", "value": ticket["reference"], "inline": True},
        {"name": "Status", "value": status, "inline": True},
        {"name": "Customer", "value": escape_markdown(ticket["customer_name"]), "inline": False},
    ]
    if ticket.get("customer_email"):
        fields.append({"name": "Email", "value": escape_markdown(ticket["customer_email"]), "inline": True})
    if ticket.get("customer_phone"):
        fields.append({"name": "Phone", "value": ticket["customer_phone"], "inline": True})
    if ticket.get("agent_name"):
        fields.append({"name": "Assigned to", "value": escape_markdown(ticket["agent_name"]), "inline": False})
    if ticket.get("escalation_count"):
        fields.append({"name": "Escalated", "value": f"{ticket['escalation_count']}×, last {_when(ticket.get('last_escalated_at'))}", "inline": False})
    fields.append({"name": "Created", "value": f"{created:%Y-%m-%d %H:%M} UTC (<t:{int(created.timestamp())}:R>)", "inline": False})
    action = "Open Agent Portal / Claim Ticket" if status == "PENDING" else "Open Agent Portal"
    fields.append({"name": "Action", "value": f"[{action}]({portal_url})", "inline": False})
    title = "RACKLOOM SUPPORT — NEW TICKET" if status == "PENDING" else f"RACKLOOM SUPPORT — {status}"
    return {
        "content": f"New support ticket **{ticket['reference']}**",
        # Customer text must never ping anyone, whatever it contains.
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title,
                "url": portal_url,
                "color": STATUS_COLOR.get(status, 0x6B7280),
                "description": f"**Query**\n{_preview(ticket.get('query') or '')}",
                "fields": fields,
                "timestamp": created.isoformat(),
                "footer": {"text": "Rackloom support"},
            }
        ],
    }


def _ticket_fields(ticket: dict) -> list[dict]:
    fields = [
        {"name": "Ticket ID", "value": ticket["reference"], "inline": True},
        {"name": "Status", "value": ticket["effective_status"], "inline": True},
        {"name": "Handled by", "value": escape_markdown(ticket.get("agent_name") or "Nobody yet"), "inline": True},
        {"name": "Customer", "value": escape_markdown(ticket["customer_name"]), "inline": True},
    ]
    if ticket.get("claimed_at"):
        fields.append({"name": "Claimed", "value": _when(ticket["claimed_at"]), "inline": False})
    fields.append({"name": "Chat open until", "value": _when(ticket["access_expires_at"]), "inline": False})
    return fields


def reply_payload(ticket: dict, unread: dict, portal_url: str) -> dict:
    """A new post (never an edit): the assigned agent's customer wrote while the agent was away."""
    agent = escape_markdown(ticket.get("agent_name") or "Agent")
    n = int(unread.get("unread") or 0)
    plural = "message" if n == 1 else "messages"
    fields = _ticket_fields(ticket)
    fields.insert(4, {"name": "Unread", "value": f"{n} {plural}", "inline": True})
    if unread.get("latest_at"):
        fields.insert(5, {"name": "Last message", "value": _when(unread["latest_at"]), "inline": False})
    fields.append({"name": "Action", "value": f"[Resume the conversation]({portal_url})", "inline": False})
    return {
        "content": f"**{agent}**, your customer on ticket **{ticket['reference']}** has replied and is waiting for you.",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "RACKLOOM SUPPORT — CUSTOMER REPLIED",
                "url": portal_url,
                "color": REPLY_COLOR,
                "description": (
                    f"**{agent}**, the customer on ticket **{ticket['reference']}**, which you are handling, sent {n} new {plural} "
                    f"you have not read yet. Click the link below to resume the chat.\n\n**Latest message**\n{_preview(unread.get('latest') or '')}"
                ),
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Rackloom support"},
            }
        ],
    }


def escalation_payload(ticket: dict, detail: dict, portal_url: str, admin_url: str, mention: str = "") -> dict:
    """A new post asking the team to take up a ticket the customer says is not resolved."""
    agent = escape_markdown(ticket.get("agent_name") or "an agent")
    note = (detail.get("note") or "").strip()
    count = int(detail.get("count") or ticket.get("escalation_count") or 1)
    description = (
        f"The customer on ticket **{ticket['reference']}**, handled by **{agent}**, says their issue has not been resolved "
        f"and has escalated the ticket. Please look into it and resolve it as soon as possible, on the highest priority."
    )
    if note:
        description += f"\n\n**Customer's note**\n{_preview(note)}"
    query = ticket.get("query") or ""
    if query:
        description += f"\n\n**Original query**\n{_preview(query)}"
    fields = _ticket_fields(ticket)
    fields.insert(4, {"name": "Escalation", "value": f"#{count}" + (" (escalated before)" if count > 1 else ""), "inline": True})
    fields.append({"name": "Action", "value": f"[Open in the agent portal]({portal_url}) · [Admin panel]({admin_url})", "inline": False})
    lead = f"{mention.strip()} " if mention and valid_mention(mention) else ""
    return {
        # Only our own fixed text and the configured mention are in `content`; customer text stays in the embed.
        "content": f"{lead}🚨 **Escalated ticket {ticket['reference']}**: highest priority.",
        "allowed_mentions": allowed_mentions_for(mention),
        "embeds": [
            {
                "title": "RACKLOOM SUPPORT — ESCALATED (HIGHEST PRIORITY)",
                "url": portal_url,
                "color": ESCALATION_COLOR,
                "description": description,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Rackloom support · escalation"},
            }
        ],
    }
