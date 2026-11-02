from __future__ import annotations

import pytest

from consent_ledger.keywords import classify
from consent_ledger.phone import (
    area_code,
    is_nanp,
    match_key,
    normalize_e164,
    phones_match,
    redact,
    spellings,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(415) 555-0134", "+14155550134"),
        ("415.555.0134", "+14155550134"),
        ("1-415-555-0134", "+14155550134"),
        ("+1 415 555 0134", "+14155550134"),
        ("+44 20 7946 0134", "+442079460134"),
        # Digits in any script are the same number (finding 4).
        ("".join(chr(0xFF10 + int(d)) for d in "4155550134"), "+14155550134"),
        ("٤١٥٥٥٥٠١٣٤", "+14155550134"),
        ("\uff0b1 415 555 0134", "+14155550134"),  # a full-width plus
        ("555-0134", None),
        ("+1234", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize(raw: str | None, expected: str | None) -> None:
    assert normalize_e164(raw) == expected


def test_spellings_cover_every_form_of_one_number() -> None:
    assert spellings("(415) 555-0134") == [
        "(415) 555-0134",
        "+14155550134",
        "4155550134",
        "14155550134",
    ]
    assert spellings("+14155550134")[0] == "+14155550134"


def test_a_foreign_number_never_gets_us_spellings() -> None:
    # Its last ten digits also spell a US number in Maine: a different person.
    forms = spellings("+44 20 7946 0134")
    assert forms == ["+44 20 7946 0134", "+442079460134"]
    assert "+12079460134" not in forms


@pytest.mark.parametrize("junk", ["", "   ", "+", "+12", "555-0134", "call me"])
def test_junk_yields_nothing_so_the_caller_refuses(junk: str) -> None:
    assert spellings(junk) == []


def test_helpers() -> None:
    assert phones_match("(415) 555-0134", "+14155550134")
    assert not phones_match("junk", "junk")
    assert match_key("+1 (415) 555-0134") == "4155550134"
    assert is_nanp("4155550134")
    assert not is_nanp("+442079460134")
    assert area_code("+14155550134") == "415"
    assert area_code("+442079460134") == ""
    assert redact("+14155550134") == "***-**0134"
    assert redact("12") == "***-****"


@pytest.mark.parametrize(
    ("body", "keyword"),
    [
        ("STOP", "stop"),
        (" stop ", "stop"),
        ("Opt Out", "stop"),
        ("opt-out", "stop"),
        ("unsubscribe", "stop"),
        ("Revoke", "stop"),
        ("quit", "stop"),
        # Finding 1: punctuation, other scripts and a few words around it.
        ("Stop.", "stop"),
        ("STOP!", "stop"),
        ("Stop please", "stop"),
        ("please stop", "stop"),
        ("Unsubscribe me", "stop"),
        ("STOP STOP", "stop"),
        ("".join(chr(ord(c) + 0xFEE0) for c in "STOP"), "stop"),  # full width
        ("Opt\u2013out", "stop"),
        ("stop\u200b", "stop"),
        ("ALTO", "stop"),
        ("Parar", "stop"),
        ("stop texting me about the leak", "stop"),
        ("Hi, please stop sending me these, I never signed up", "stop"),
        ("Remove me from this list", "stop"),
        ("Don\u2019t text me again", "stop"),
        # Longer messages that only use the word are a person's to read.
        ("Can I cancel my order before it ships?", None),
        ("I'll stop by the office tomorrow", None),
        ("START", "start"),
        ("Start.", "start"),
        ("unstop", "start"),
        # Finding 2: "yes" answers ordinary questions; it is not START.
        ("yes", None),
        ("start the order please", None),
        ("help", "help"),
        ("Help!", "help"),
        ("", None),
        ("?!", None),
        (None, None),
    ],
)
def test_keywords(body: str | None, keyword: str | None) -> None:
    assert classify(body) == keyword
