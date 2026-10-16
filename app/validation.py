"""Normalising what people type: names, emails, phone numbers and message text."""
from __future__ import annotations

import re
import unicodedata

import phonenumbers

from .config import EMAIL_MAX

# Control characters, and the bidirectional overrides that can make text read differently from
# what it is (a name that renders as someone else's in Discord). Newline and tab stay. Also the
# invisible formatting characters (zero-width space, word joiner, BOM, deprecated format controls)
# that hide text from a reader; the zero-width joiner stays, emoji sequences need it.
_STRIP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​‎‏‪-‮⁠-⁤⁦-⁩⁪-⁯﻿]")
# Characters no one's name needs, and every markup or template language does.
_NAME_MARKUP = re.compile(r"[<>{}`\\]")
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


def clean_name(raw: str, *, max_len: int) -> str:
    """A person's name: one line of text, and no markup characters (< > { } ` and backslash)."""
    s = clean_text(raw, max_len=max_len, multiline=False)
    if _NAME_MARKUP.search(s):
        raise InvalidInput("Use letters, spaces and everyday punctuation only (no < > { } ` or backslash).")
    return s


def normalize_email(raw: str) -> str:
    s = (raw or "").strip()
    local = s.partition("@")[0]
    if len(s) > EMAIL_MAX or not _EMAIL.fullmatch(s) or ".." in s or local.startswith(".") or local.endswith("."):
        raise InvalidInput("Enter a valid email address, for example name@company.com.")
    return s.lower()


def normalize_phone(raw: str, region: str | None = None) -> str:
    """E.164 for a number typed with a +country code, or in the national format of `region`.

    No country is assumed: without a + prefix the caller must say which country the number is in.
    Every country's numbering plan is checked (Google's libphonenumber metadata), and the answer
    says what is wrong: an unknown country code, too few or too many digits, or no such number.
    """
    s = (raw or "").strip()
    if not _PHONE_CHARS.fullmatch(s) or sum(ch.isdigit() for ch in s) < 4:
        raise InvalidInput("Enter a valid phone number, using digits only.")
    reg = (region or "").strip().upper() or None
    if reg is not None and reg not in phonenumbers.SUPPORTED_REGIONS:
        raise InvalidInput("Choose a valid country.")
    if not s.startswith("+") and reg is None:
        raise InvalidInput("Include the country code, for example +91.")
    try:
        n = phonenumbers.parse(s, reg)
    except phonenumbers.NumberParseException as e:
        if e.error_type == phonenumbers.NumberParseException.INVALID_COUNTRY_CODE:
            raise InvalidInput("That country code does not exist. Check the digits after the +.") from None
        if e.error_type == phonenumbers.NumberParseException.TOO_LONG:
            raise InvalidInput("This phone number has too many digits.") from None
        raise InvalidInput("Enter a valid phone number.") from None
    reason = phonenumbers.is_possible_number_with_reason(n)
    R = phonenumbers.ValidationResult
    if reason == R.INVALID_COUNTRY_CODE:
        raise InvalidInput("That country code does not exist. Check the digits after the +.")
    if reason in (R.TOO_SHORT, R.INVALID_LENGTH):
        raise InvalidInput(f"This number has too few digits for +{n.country_code}." if reason == R.TOO_SHORT else f"This number has the wrong number of digits for +{n.country_code}.")
    if reason == R.TOO_LONG:
        raise InvalidInput(f"This number has too many digits for +{n.country_code}.")
    if not phonenumbers.is_valid_number(n):
        raise InvalidInput(f"This is not a valid +{n.country_code} phone number. Check the digits.")
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
