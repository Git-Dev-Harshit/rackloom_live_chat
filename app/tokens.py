"""Ticket references, secret tokens and their hashes."""
from __future__ import annotations

import hashlib
import re
import secrets

# Crockford base32: no I, L, O or U, so a reference read aloud or copied by hand survives.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REFERENCE_RE = re.compile(r"^RCK-[0-9A-HJKMNP-TV-Z]{8}$")


def new_reference() -> str:
    """RCK- and 8 random characters (40 bits). Public: it names a ticket and grants nothing."""
    return "RCK-" + "".join(secrets.choice(ALPHABET) for _ in range(8))


def normalize_reference(raw: str) -> str | None:
    ref = (raw or "").strip().upper()
    return ref if REFERENCE_RE.fullmatch(ref) else None


def new_token() -> str:
    """256 bits for a return link or a session cookie. Only its hash is stored."""
    return secrets.token_urlsafe(32)


def digest(value: str) -> bytes:
    """SHA-256 of a high-entropy token. No pepper needed: 256 random bits cannot be guessed from a hash."""
    return hashlib.sha256(value.encode()).digest()


def new_code() -> str:
    """Six digits for the new-browser email check (short-lived and attempt-limited)."""
    return f"{secrets.randbelow(1_000_000):06d}"
