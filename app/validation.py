"""Normalising what people type: names, emails, phone numbers and message text."""
from __future__ import annotations

import re
import unicodedata

import phonenumbers

from .config import EMAIL_MAX

# Control characters, and the bidirectional overrides that can make text read differently from
# what it is (a name that renders as someone else's in Discord). Newline and tab stay.
_STRIP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‎‏‪-‮⁦-⁩]")
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_PHONE_CHARS = re.compile(r"^[0-9+()\-.\s]{4,32}$")


class InvalidInput(ValueError):
    pass


def clean_text(raw: str, *, max_len: int, multiline: bool = True) -> str:
    """NFC, no control or bidi-override characters, \\n line endings, trimmed. Raises on empty or too long."""
    s = unicodedata.normalize("NFC", raw or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _STRIP.sub("", s)
    if not multiline:
        s = " ".join(s.split())
    else:
        s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if not s:
        raise InvalidInput("This field is required.")
    if len(s) > max_len:
        raise InvalidInput(f"Keep this under {max_len} characters.")
    return s


def normalize_email(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) > EMAIL_MAX or not _EMAIL.fullmatch(s) or ".." in s:
        raise InvalidInput("Enter a valid email address.")
    return s.lower()


def normalize_phone(raw: str, region: str | None = None) -> str:
    """E.164 for a number typed with a +country code, or in the national format of `region`.

    No country is assumed: without a + prefix the caller must say which country the number is in.
    """
    s = (raw or "").strip()
    if not _PHONE_CHARS.fullmatch(s):
        raise InvalidInput("Enter a valid phone number.")
    reg = (region or "").strip().upper() or None
    if reg is not None and reg not in phonenumbers.SUPPORTED_REGIONS:
        raise InvalidInput("Choose a valid country.")
    if not s.startswith("+") and reg is None:
        raise InvalidInput("Include the country code, for example +91.")
    try:
        n = phonenumbers.parse(s, reg)
    except phonenumbers.NumberParseException:
        raise InvalidInput("Enter a valid phone number.") from None
    if not phonenumbers.is_valid_number(n):
        raise InvalidInput("Enter a valid phone number.")
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(e164: str) -> str:
    """For logs: the country code and the last two digits, nothing that identifies a person."""
    if not e164:
        return ""
    try:
        cc = str(phonenumbers.parse(e164).country_code)
    except phonenumbers.NumberParseException:
        cc = ""
    return f"+{cc}{'*' * max(0, len(e164) - len(cc) - 3)}{e164[-2:]}"


def mask_email(email: str) -> str:
    """j***@example.com: enough for a customer to recognise their own address."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"
