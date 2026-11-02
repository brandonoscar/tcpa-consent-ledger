"""STOP / START / HELP.

Erring broad is the safe direction for STOP: an extra opt-out is a
customer-service problem, a missed one is a TCPA violation. The FCC's
revocation rule (47 CFR 64.1200(a)(10), in force since April 2025) lets a
person revoke consent by any reasonable means, and names stop, quit, end,
revoke, opt out, cancel and unsubscribe as valid replies to a text. So a
message counts as STOP when:

* the whole message is a STOP word, whatever its case, punctuation, spacing
  or width ("Stop.", "STOP!", "opt out" with an en dash, full-width letters);
* its first word is one ("Stop texting me", "Cancel all of this");
* it is short, at most :data:`SHORT_MESSAGE_WORDS` words, and contains one
  ("please stop", "Unsubscribe me");
* it asks to stop in so many words ("stop sending these", "remove me from
  this list", "don't text me").

Longer messages that merely use one of the words ("Can I cancel my order?")
are ordinary messages; a person reads those. Spanish STOP words (ALTO, PARAR,
CANCELAR...) count too. Twilio's own ``OptOutType`` parameter, when present,
is honoured for STOP (see :mod:`consent_ledger.twilio`).

START and HELP are the opposite: they only count as the whole message. START
undoes an opt-out, so it must be unmistakable. YES is not a START word,
because "yes" is the most common answer to an ordinary question.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Keyword = Literal["stop", "start", "help"]

STOP_KEYWORDS = frozenset(
    {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
        "REVOKE",
        "OPTOUT",
        # Spanish
        "ALTO",
        "PARAR",
        "PARE",
        "DETENER",
        "CANCELAR",
    }
)
START_KEYWORDS = frozenset({"START", "UNSTOP"})
HELP_KEYWORDS = frozenset({"HELP", "INFO"})

#: A message this short that contains a STOP word is a STOP.
SHORT_MESSAGE_WORDS = 4

_PHRASES = re.compile(
    r"\bstop\s+(?:texting|txting|messaging|sending|contacting|calling|these|this|it|all)\b"
    r"|\b(?:remove|delete|take)\s+me\b"
    r"|\b(?:do\s+not|don['\u2019]?t|no\s+more)\s+(?:text|txt|message|contact)"
    r"|\bno\s+more\s+(?:texts|messages)\b"
    r"|\bopt\s*-?\s*me\s+out\b",
    re.IGNORECASE,
)


def _plain(body: str) -> str:
    """Upper case, compatibility forms folded (full-width letters), every dash
    a hyphen, invisible format characters (zero-width spaces) removed."""
    text = unicodedata.normalize("NFKC", body)
    out = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        out.append("-" if category == "Pd" else ch)
    return "".join(out).upper()


def _words(text: str) -> list[str]:
    """Letters and digits only. A hyphen or space inside "opt-out" joins the
    pair, so "OPT OUT" and "OPT-OUT" both read as OPTOUT."""
    words = re.findall(r"[^\W_]+", text)
    joined: list[str] = []
    skip = False
    for i, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word == "OPT" and i + 1 < len(words) and words[i + 1] == "OUT":
            joined.append("OPTOUT")
            skip = True
        else:
            joined.append(word)
    return joined


def classify(body: str | None) -> Keyword | None:
    """The keyword a message is, or ``None`` for an ordinary message."""
    text = _plain(body or "")
    words = _words(text)
    if not words:
        return None
    whole = "".join(words)
    if whole in STOP_KEYWORDS:
        return "stop"
    if whole in START_KEYWORDS:
        return "start"
    if whole in HELP_KEYWORDS:
        return "help"
    if words[0] in STOP_KEYWORDS:
        return "stop"
    if len(words) <= SHORT_MESSAGE_WORDS and any(word in STOP_KEYWORDS for word in words):
        return "stop"
    if _PHRASES.search(text):
        return "stop"
    return None


__all__ = [
    "HELP_KEYWORDS",
    "SHORT_MESSAGE_WORDS",
    "START_KEYWORDS",
    "STOP_KEYWORDS",
    "Keyword",
    "classify",
]
