"""Normalising input, tokens, the rate limiter and the startup rules. No database."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.ratelimit import RateLimiter
from app.tokens import REFERENCE_RE, digest, new_code, new_reference, new_token, normalize_reference
from app.validation import InvalidInput, clean_text, mask_email, mask_phone, normalize_email, normalize_phone


@pytest.mark.parametrize(
    ("raw", "region", "want"),
    [
        ("+91 98765 43210", None, "+919876543210"),
        ("+91-98765-43210", None, "+919876543210"),
        ("98765 43210", "IN", "+919876543210"),
        ("098765 43210", "in", "+919876543210"),
        ("(415) 555-2671", "US", "+14155552671"),
        ("+44 20 7946 0958", None, "+442079460958"),
        ("+49 30 901820", "IN", "+4930901820"),  # an explicit +code wins over the chosen country
        ("+65 6123 4567", None, "+6561234567"),
    ],
)
def test_phone_numbers_become_e164_for_any_country(raw, region, want):
    assert normalize_phone(raw, region) == want


@pytest.mark.parametrize(
    ("raw", "region"),
    [
        ("98765 43210", None),  # no +code and no country: we never assume India
        ("+91 12345", None),  # too short to be a number
        ("call me", None),
        ("+91 98765 4321O", None),  # a letter O
        ("", "IN"),
        ("9" * 40, "IN"),
        ("98765 43210", "XX"),  # not a country
    ],
)
def test_bad_phone_numbers_are_refused(raw, region):
    with pytest.raises(InvalidInput):
        normalize_phone(raw, region)


def test_emails_are_trimmed_lowercased_and_checked():
    assert normalize_email("  Asha.Verma@Example.COM ") == "asha.verma@example.com"
    assert normalize_email("a+tag@mail.co.in") == "a+tag@mail.co.in"
    for bad in ["no-at-sign", "a@b", "a@-bad.com", "a..b@x.com", "a@x..com", "a b@x.com", "a@x.com\nBcc: y@z.com", f"{'a' * 250}@x.com"]:
        with pytest.raises(InvalidInput):
            normalize_email(bad)


def test_text_is_cleaned_and_bounded():
    assert clean_text("  hello\r\nworld\x00‮  ", max_len=50) == "hello\nworld"
    assert clean_text("a\n\n\n\n\nb", max_len=50) == "a\n\nb"
    assert clean_text("  Asha \n Verma ", max_len=50, multiline=False) == "Asha Verma"
    assert clean_text("<script>alert(1)</script>", max_len=50) == "<script>alert(1)</script>"  # stored as text; escaped on render
    with pytest.raises(InvalidInput):
        clean_text(" \n\t ", max_len=50)
    with pytest.raises(InvalidInput):
        clean_text("x" * 51, max_len=50)


def test_logs_get_masked_contact_details():
    assert mask_phone("+919876543210") == "+91********10"
    assert "98765" not in mask_phone("+919876543210")
    assert mask_email("asha@example.com") == "a***@example.com"


def test_references_tokens_and_codes():
    refs = {new_reference() for _ in range(500)}
    assert len(refs) == 500 and all(REFERENCE_RE.fullmatch(r) for r in refs)
    assert not any(c in r[4:] for r in refs for c in "ILOU")
    assert normalize_reference(" rck-0123abcd ") == "RCK-0123ABCD"
    assert normalize_reference("RCK-0123ABCI") is None  # I is not Crockford
    assert normalize_reference("../etc/passwd") is None
    t = new_token()
    assert len(t) >= 43 and t != new_token()
    assert len(digest(t)) == 32 and digest(t) == digest(t)
    assert all(len(c) == 6 and c.isdigit() for c in (new_code() for _ in range(100)))


def test_rate_limiter_counts_per_key_and_window():
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert [rl.hit("a", 3, 60)[0] for _ in range(4)] == [True, True, True, False]
    assert rl.hit("b", 3, 60)[0]  # another key is untouched
    ok, retry = rl.hit("a", 3, 60)
    assert not ok and retry == 60
    now[0] = 61
    assert rl.hit("a", 3, 60)[0]


def test_production_refuses_to_start_without_a_real_captcha_secret():
    prod = dict(database_url="postgresql://x", public_url="https://rackloom.in", turnstile_site_key="site")
    assert any("TURNSTILE_SECRET is not set" in p for p in Settings(**prod).startup_problems())
    assert any("test secret" in p for p in Settings(**prod, turnstile_secret="1x0000000000000000000000000000000AA").startup_problems())
    assert Settings(**prod, turnstile_secret="1x0000000000000000000000000000000AA", turnstile_allow_test_keys=True).startup_problems() == []
    assert Settings(**prod, turnstile_secret="0x4AAAA-real").startup_problems() == []
    # Development without keys starts, with the captcha off.
    assert Settings(database_url="postgresql://x", public_url="http://localhost:3000").startup_problems() == []
    assert Settings(public_url="http://localhost:3000").startup_problems() == ["SUPPORT_DATABASE_URL is not set"]


def test_allowed_origins_and_cookie_security_follow_the_public_url():
    prod = Settings(public_url="https://rackloom.in")
    assert prod.origins == {"https://rackloom.in", "https://www.rackloom.in"}
    assert prod.secure_cookies and prod.production
    dev = Settings(public_url="http://localhost:3000")
    assert dev.origins == {"http://localhost:3000"} and not dev.secure_cookies and not dev.production
    assert Settings(public_url="https://rackloom.in", allowed_origins_raw="https://a.test, https://b.test/").origins == {"https://a.test", "https://b.test"}
