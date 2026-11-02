"""Phone numbers: one canonical form for storage, every spelling for lookups.

Carriers and Twilio speak E.164 (``+14155550134``). People and CRMs type
``(415) 555-0134``. An exact string compare between the two never matches,
which in this domain is not a cosmetic bug: a STOP recorded from the webhook
in E.164 and looked up with a hand-typed number finds nothing, and the person
who asked you to stop gets texted. So:

* :func:`normalize_e164` is the form you store and send to;
* :func:`spellings` is every form the ledger might hold for one number, for an
  ``= ANY(...)`` lookup;
* anything that cannot be a phone number yields nothing, and the ledger treats
  "cannot check" as a refusal, never as permission.

US-first: a bare 10-digit number is taken as ``+1``. Numbers that already
carry another ``+`` country code are kept as they are and never given US
spellings (``+44 20 7946 0134`` must not be matched against the US number
``+1 207 946 0134``, a stranger in Maine).

Digits are read in any script. Full-width digits from a phone keyboard, or
Arabic-Indic ones, spell the same number as ``4155550134`` and normalize to
the same ASCII E.164. Otherwise a STOP arriving from Twilio in ASCII would
never match consent recorded under the other spelling.
"""

from __future__ import annotations

import unicodedata

_NANP_LEN = 10
#: Shortest routable E.164 including the country code. Below this there is no
#: number to look up, and a lookup that finds nothing would read as permission.
_MIN_E164_DIGITS = 8


def _digits(raw: str | None) -> str:
    """Every decimal digit in ``raw``, in any script, as ASCII."""
    return "".join(str(unicodedata.decimal(ch)) for ch in raw or "" if ch.isdecimal())


def _text(raw: str | None) -> str:
    # NFKC turns a full-width plus sign into "+"; digits are handled by _digits.
    return unicodedata.normalize("NFKC", raw or "").strip()


def normalize_e164(raw: str | None) -> str | None:
    """Canonical E.164 in ASCII, or ``None`` when ``raw`` cannot be a phone number.

    ``(415) 555-0134`` -> ``+14155550134``; ``+44 20 7946 0134`` ->
    ``+442079460134``; ``555-0134`` -> ``None``.
    """
    text = _text(raw)
    digits = _digits(text)
    if not digits:
        return None
    if text.startswith("+"):
        return f"+{digits}" if len(digits) >= _MIN_E164_DIGITS else None
    if len(digits) == _NANP_LEN:
        return f"+1{digits}"
    if len(digits) == _NANP_LEN + 1 and digits.startswith("1"):
        return f"+{digits}"
    return None


def match_key(raw: str | None) -> str | None:
    """The national number of a NANP phone (last ten digits), or ``None``."""
    digits = _digits(raw)
    if len(digits) == _NANP_LEN + 1 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != _NANP_LEN:
        return None
    return digits


def phones_match(a: str | None, b: str | None) -> bool:
    """Same number? Two unparseable inputs never match each other."""
    ca, cb = normalize_e164(a), normalize_e164(b)
    return ca is not None and ca == cb


def is_nanp(phone: str | None) -> bool:
    """A North American (+1) number: the only kind a US A2P campaign can text."""
    canonical = normalize_e164(phone)
    return canonical is not None and canonical.startswith("+1") and len(canonical) == 12


def area_code(phone: str | None) -> str:
    """The NPA of a NANP number, or ``""``."""
    canonical = normalize_e164(phone)
    if canonical is None or not is_nanp(canonical):
        return ""
    return canonical[2:5]


def spellings(raw: str | None) -> list[str]:
    """Every form the ledger could hold for this number, for an ``= ANY``.

    Each entry denotes the SAME number, so matching any of them can only find
    this person under another spelling, never somebody else. Empty when the
    input cannot be a number: the caller must then refuse, because "we could
    not check" must never mean "so we sent it".
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    digits = _digits(text)
    if _text(text).startswith("+") and not digits.startswith("1"):
        # Another country: its last ten digits mean nothing as a US number.
        if len(digits) < _MIN_E164_DIGITS:
            return []
        found = [text]
        canonical = normalize_e164(text)
        if canonical and canonical not in found:
            found.append(canonical)
        return found
    key = match_key(text)
    if key is None:
        return []
    found = [text]
    for candidate in (f"+1{key}", key, f"1{key}"):
        if candidate not in found:
            found.append(candidate)
    return found


def redact(phone: str | None) -> str:
    """Last four digits, for logs: ``+14155550134`` -> ``***-**0134``."""
    digits = _digits(phone)
    return f"***-**{digits[-4:]}" if len(digits) >= 4 else "***-****"


__all__ = [
    "area_code",
    "is_nanp",
    "match_key",
    "normalize_e164",
    "phones_match",
    "redact",
    "spellings",
]
