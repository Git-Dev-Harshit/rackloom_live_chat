"""Mail for customers: the return link after a ticket is created, and the code that opens it in a
new browser. Same settings and route as web and auth (in production, the Google Workspace SMTP
relay). Without SMTP_HOST, development prints mail to the log; production has no mail, and the
new-browser check says so instead of being skipped.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .config import Settings

log = logging.getLogger("support.mail")


class MailError(Exception):
    pass


class Mailer:
    def __init__(self, settings: Settings):
        self.s = settings
        if settings.smtp_host:
            self.kind = "smtp"
        elif not settings.production:
            self.kind = "console"
        else:
            self.kind = "off"
        name, addr = parseaddr(settings.mail_from)
        # Through a mailbox's own login the mail must come from that mailbox (as web/src/lib/mailer.ts).
        if settings.smtp_user and addr.lower() != settings.smtp_user.lower():
            addr = settings.smtp_user
        self.sender = formataddr((name or "Rackloom", addr or "support@rackloom.in"))

    @property
    def enabled(self) -> bool:
        return self.kind != "off"

    def send(self, to: str, subject: str, text: str) -> None:
        """Blocking; call it from a worker thread. Raises MailError on failure."""
        if self.kind == "off":
            raise MailError("mail is not configured")
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Reply-To"] = self.s.support_email
        msg.set_content(text)
        if self.kind == "console":
            log.info("mail:console to=%s subject=%s\n%s", to, subject, text)
            return
        try:
            helo = self.s.public_host or "localhost"
            if self.s.smtp_port == 465:
                conn = smtplib.SMTP_SSL(self.s.smtp_host, 465, local_hostname=helo, timeout=15, context=ssl.create_default_context())
            else:
                conn = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, local_hostname=helo, timeout=15)
            with conn:
                if self.s.smtp_port != 465:
                    conn.starttls(context=ssl.create_default_context())
                if self.s.smtp_user:
                    conn.login(self.s.smtp_user, self.s.smtp_pass)
                conn.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(type(e).__name__) from None


def link_mail(ref: str, return_url: str, support_email: str) -> tuple[str, str]:
    subject = f"Your Rackloom support request {ref}"
    text = (
        f"Thank you for contacting Rackloom Support. We received your request {ref} and are finding an available agent.\n\n"
        f"Return to your conversation here:\n{return_url}\n\n"
        "This link works for 7 days, or until the ticket is resolved or closed. In the browser you used to write to us it "
        "opens straight away; anywhere else we will email a one-time code to this address first.\n\n"
        f"Keep this link to yourself: it leads to your conversation. If you did not contact us, ignore this email or write to {support_email}.\n\n"
        "Rackloom Support"
    )
    return subject, text


def code_mail(ref: str, code: str, minutes: int) -> tuple[str, str]:
    subject = f"Your code to open Rackloom support request {ref}"
    text = (
        f"Your one-time code is: {code}\n\n"
        f"Enter it on the page that asked for it within {minutes} minutes. It opens your conversation {ref} in that browser.\n\n"
        "If you did not ask for this code, someone has your conversation link. Nothing opens without this code; you can ignore this email.\n\n"
        "Rackloom Support"
    )
    return subject, text
