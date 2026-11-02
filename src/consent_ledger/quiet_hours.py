"""When a text may be sent, on the recipient's own clock.

Not legal advice. The rules below are a starting point that errs strict;
check them against current law for your use case, and pass your own
:class:`QuietHoursRules` when they differ.

* **The federal window** is 08:00 to 21:00 recipient-local (47 CFR
  64.1200(c)(1)). It is a floor, not the answer.
* **Some states are stricter**, and state law follows where the recipient
  is: Florida, Oklahoma, Washington and Maryland end at 20:00; Connecticut
  runs 09:00 to 20:00; Texas starts at 09:00, and at noon on Sunday. Other
  states set telemarketing hours too, and some apply them to texts; this list
  is not complete.
* **An unknown state gets the intersection of every rule it knows** (09:00 to
  20:00, noon on Sunday), not the federal default. A guard that cannot see
  takes the strictest reading it has, because the alternative is an illegal
  early-morning text.
* **Where the number puts its owner.** An area code in a single-zone state or
  territory names its zone. One in a state that spans zones (Florida's 850,
  Indiana, Michigan's 906...) is checked on the clocks at both ends of the
  contiguous US, Eastern and Pacific. An area code the table does not know
  (Canada, the Caribbean, a new overlay) is checked on the clocks at both
  ends of every zone a +1 number can be in, Newfoundland and Hawaii (the
  Pacific territories are in the table): only a few hours a day pass. Add the
  area codes you text (``QuietHoursRules.area_codes``) to give them a zone.
* **A recorded time zone adds a check; it never removes one.** The person's
  own zone (from their consent record) must be inside the window too, and
  since a zone does not name a state, it is read with the unknown-state rule.
  A wrong zone (a server's UTC default) can then only block a text, never
  send one at 3 AM.
* **A non-+1 number fails closed** unless its zone is recorded: the US table
  says nothing about somebody in London.
* **The weekday is read on the recipient's clock.** Saturday 23:30 in
  California is already Sunday in UTC.
* **No time zone database, no texts.** Without one every zone fails to
  resolve and every check refuses; on Windows install ``tzdata``.

The area-code table lists codes whose zone is certain; a wrong one is an
illegal text, a missing one costs only hours. Arizona and Hawaii do not
observe daylight saving time (except the Navajo Nation, so Arizona's 928 is
in the multi-zone table), which is why the tables hold IANA zone names
rather than UTC offsets.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from consent_ledger.phone import area_code, is_nanp

logger = logging.getLogger(__name__)

Window = tuple[dtime, dtime]
#: Where to check: (IANA zone, state or None when the state is unknown).
Check = tuple[str, str | None]

FEDERAL: Window = (dtime(8, 0), dtime(21, 0))
_8_TO_8: Window = (dtime(8, 0), dtime(20, 0))


@dataclass(frozen=True)
class StateRule:
    weekday: Window
    sunday: Window | None = None

    def window(self, is_sunday: bool) -> Window:
        return self.sunday if (is_sunday and self.sunday is not None) else self.weekday


#: The state rules this package knows, each with the law it comes from. NOT
#: VERIFIED by counsel; erring strict. Override or extend them with
#: ``QuietHoursRules(states=...)``, which merges over these.
DEFAULT_STATE_RULES: Mapping[str, StateRule] = {
    "FL": StateRule(_8_TO_8),  # Florida Telephone Solicitation Act (Fla. Stat. 501.059)
    "OK": StateRule(_8_TO_8),  # Oklahoma Telephone Solicitation Act
    "WA": StateRule(_8_TO_8),  # telephone solicitation (RCW 80.36.390)
    "MD": StateRule(_8_TO_8),  # Maryland Stop the Spam Calls Act (2023), texts included
    "CT": StateRule((dtime(9, 0), dtime(20, 0))),  # Conn. Gen. Stat. 42-288a, texts included
    "TX": StateRule(  # Tex. Bus. & Com. Code 301.051
        (dtime(9, 0), dtime(21, 0)), (dtime(12, 0), dtime(21, 0))
    ),
}

_EASTERN = "America/New_York"
_CENTRAL = "America/Chicago"
_MOUNTAIN = "America/Denver"
_ARIZONA = "America/Phoenix"
_PACIFIC = "America/Los_Angeles"
_HAWAII = "Pacific/Honolulu"


def _intersection(default: Window, rules: Iterable[StateRule]) -> StateRule:
    """The window inside every rule: the latest start and the earliest end."""
    rules = list(rules)

    def meet(windows: list[Window]) -> Window:
        return (max(w[0] for w in windows), min(w[1] for w in windows))

    return StateRule(
        meet([default, *(r.window(False) for r in rules)]),
        meet([default, *(r.window(True) for r in rules)]),
    )


@dataclass(frozen=True)
class QuietHoursRules:
    """The windows in force. Replace any part to match your counsel's reading."""

    default: Window = FEDERAL
    #: Merged over :data:`DEFAULT_STATE_RULES`, so adding a state never drops
    #: Florida's or Texas's. To relax a default, override it (``FEDERAL``).
    states: Mapping[str, StateRule] = field(default_factory=dict)
    #: When the state is unknown. ``None``: the intersection of every rule,
    #: worked out when asked (:attr:`unknown_rule`), so it follows ``states``.
    unknown_state: StateRule | None = None
    #: The ends of the contiguous US: a number in a multi-zone state must be
    #: inside the window on both clocks.
    contiguous_zones: tuple[str, ...] = (_EASTERN, _PACIFIC)
    #: The ends of every zone a +1 number can be in, the Pacific territories
    #: aside (they are in the table): Newfoundland and Hawaii.
    everywhere_zones: tuple[str, ...] = ("America/St_Johns", _HAWAII)
    #: Your own area codes: code -> (state, IANA zone). They beat the table.
    area_codes: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", {**DEFAULT_STATE_RULES, **self.states})

    @property
    def unknown_rule(self) -> StateRule:
        """The rule for an unknown state: ``unknown_state`` if you set one,
        else the window inside the default and every state rule."""
        return self.unknown_state or _intersection(self.default, self.states.values())

    def window_for(self, state: str | None, is_sunday: bool) -> Window:
        if state is None:
            return self.unknown_rule.window(is_sunday)
        rule = self.states.get(state)
        return rule.window(is_sunday) if rule is not None else self.default

    def place(self, phone: str | None) -> list[Check] | None:
        """Where the number puts its owner: the clocks to check, each with
        its state. ``[]`` for a non-+1 number (only a recorded zone can place
        it); ``None`` when it cannot be placed at all (no zones configured)."""
        if not is_nanp(phone):
            return []
        code = area_code(phone)
        zones: tuple[str, ...]
        if code in self.area_codes:
            state, zone = self.area_codes[code]
            return [(zone, state)]
        if code in AREA_CODE_ZONES:
            return [(AREA_CODE_ZONES[code], AREA_CODE_STATES[code])]
        if code in MULTI_ZONE_STATES:
            state = MULTI_ZONE_STATES[code]
            zones = AREA_CODES_MULTI_ZONE[state][0] or self.contiguous_zones
            checks: list[Check] = [(zone, state) for zone in zones]
        else:
            checks = [(zone, None) for zone in self.everywhere_zones]
        return checks or None

    def checks(self, phone: str | None, zone: str | None = None) -> list[Check]:
        """Every (zone, state) a send must be inside. Empty means refuse."""
        placed = self.place(phone)
        if placed is None:
            return []
        if zone is None:
            return placed
        if [z for z, _ in placed] == [zone]:
            return placed  # the recorded zone agrees with the number's
        # A zone does not name a state: read it with the unknown-state rule,
        # and keep every check the number makes.
        return [(zone, None), *placed]


DEFAULT_RULES = QuietHoursRules()

#: State or territory -> (zone, area codes). Single-zone places only, grouped
#: so each line can be checked against something a reader already knows. The
#: area codes are a string so formatters leave the table reviewable.
AREA_CODES_BY_STATE: dict[str, tuple[str, str]] = {
    # Eastern
    "CT": (_EASTERN, "203 475 860 959"),
    "DC": (_EASTERN, "202"),
    "DE": (_EASTERN, "302"),
    "GA": (_EASTERN, "229 404 470 478 678 706 762 770 912 943"),
    "MA": (_EASTERN, "339 351 413 508 617 774 781 857 978"),
    "MD": (_EASTERN, "240 301 410 443 667"),
    "ME": (_EASTERN, "207"),
    "NC": (_EASTERN, "252 336 704 743 828 910 919 980 984"),
    "NH": (_EASTERN, "603"),
    "NJ": (_EASTERN, "201 551 609 640 732 848 856 862 908 973"),
    "NY": (_EASTERN, "212 315 332 347 516 518 585 607 631 646 680 716 718 838 845 914 917 929 934"),
    "OH": (_EASTERN, "216 220 234 326 330 380 419 440 513 567 614 740 937"),
    "PA": (_EASTERN, "215 223 267 272 412 445 484 570 582 610 717 724 814 878"),
    "RI": (_EASTERN, "401"),
    "SC": (_EASTERN, "803 839 843 854 864"),
    "VA": (_EASTERN, "276 434 540 571 703 757 804 826 948"),
    "VT": (_EASTERN, "802"),
    "WV": (_EASTERN, "304 681"),
    # Florida without 850 and 448 (the panhandle straddles Eastern and Central).
    "FL": (_EASTERN, "239 305 321 352 386 407 561 656 689 727 754 772 786 813 863 904 941 954"),
    # Central
    "AL": (_CENTRAL, "205 251 256 334 659 938"),
    "AR": (_CENTRAL, "479 501 870"),
    "IA": (_CENTRAL, "319 515 563 641 712"),
    "IL": (_CENTRAL, "217 224 309 312 331 447 464 618 630 708 730 773 779 815 847 872"),
    "LA": (_CENTRAL, "225 318 337 504 985"),
    "MN": (_CENTRAL, "218 320 507 612 651 763 952"),
    "MO": (_CENTRAL, "314 417 557 573 636 660 816"),
    "MS": (_CENTRAL, "228 601 662 769"),
    "OK": (_CENTRAL, "405 539 572 580 918"),
    "WI": (_CENTRAL, "262 274 414 534 608 715 920"),
    # Texas without 915 (El Paso is Mountain).
    "TX": (
        _CENTRAL,
        "210 214 254 281 325 346 361 409 430 432 469 512 682 713 726 737 806 "
        "817 830 832 903 936 940 945 956 972 979",
    ),
    # Mountain
    "CO": (_MOUNTAIN, "303 719 720 970 983"),
    "MT": (_MOUNTAIN, "406"),
    "NM": (_MOUNTAIN, "505 575"),
    "UT": (_MOUNTAIN, "385 435 801"),
    "WY": (_MOUNTAIN, "307"),
    # Arizona keeps standard time all year: a different zone, not an offset.
    # Without 928, which covers the Navajo Nation (it keeps daylight time).
    "AZ": (_ARIZONA, "480 520 602 623"),
    # Pacific
    "CA": (
        _PACIFIC,
        "209 213 279 310 323 341 350 408 415 424 442 510 530 559 562 619 626 "
        "628 650 657 661 669 707 714 747 760 805 818 820 831 840 858 909 916 "
        "925 949 951",
    ),
    "WA": (_PACIFIC, "206 253 360 425 509 564"),
    # Hawaii
    "HI": (_HAWAII, "808"),
    # Territories, each one zone, none of them near the mainland's clocks.
    "PR": ("America/Puerto_Rico", "787 939"),
    "VI": ("America/St_Thomas", "340"),
    "GU": ("Pacific/Guam", "671"),
    "MP": ("Pacific/Saipan", "670"),
    "AS": ("Pacific/Pago_Pago", "684"),
}

#: States in more than one zone -> (the zones at both ends of the state, or
#: ``None`` for "somewhere in the contiguous US", area codes). Every code here
#: is certainly in that state; which zone inside it is not.
AREA_CODES_MULTI_ZONE: dict[str, tuple[tuple[str, ...] | None, str]] = {
    "AK": (("America/Anchorage", "America/Adak"), "907"),
    "AZ": (None, "928"),
    "FL": (None, "448 850"),
    "ID": (None, "208 986"),
    "IN": (None, "219 260 317 463 574 765 812 930"),
    "KS": (None, "316 620 785 913"),
    "KY": (None, "270 364 502 606 859"),
    "MI": (None, "231 248 269 313 517 586 616 679 734 810 906 947 989"),
    "ND": (None, "701"),
    "NE": (None, "308 402 531"),
    "NV": (None, "702 725 775"),
    "OR": (None, "458 503 541 971"),
    "SD": (None, "605"),
    "TN": (None, "423 615 629 731 865 901 931"),
    "TX": (None, "915"),
}

#: Flattened from the tables above, so a number cannot get one state's zone
#: and another state's rule. The tests check no area code appears twice.
AREA_CODE_ZONES: dict[str, str] = {
    npa: zone for zone, npas in AREA_CODES_BY_STATE.values() for npa in npas.split()
}
AREA_CODE_STATES: dict[str, str] = {
    npa: state for state, (_zone, npas) in AREA_CODES_BY_STATE.items() for npa in npas.split()
}
MULTI_ZONE_STATES: dict[str, str] = {
    npa: state for state, (_zones, npas) in AREA_CODES_MULTI_ZONE.items() for npa in npas.split()
}


def state_for(phone: str | None) -> str | None:
    code = area_code(phone)
    return AREA_CODE_STATES.get(code) or MULTI_ZONE_STATES.get(code)


def zone_for(phone: str | None) -> str | None:
    """The recipient's IANA zone when the area code names exactly one, or
    ``None``: "check every zone it could be", never "assume Eastern"."""
    return AREA_CODE_ZONES.get(area_code(phone))


def validate_zone(zone: str) -> str:
    """A recorded zone must name a place (``America/Chicago``) that this
    machine's time zone database knows. ``UTC``, ``EST`` and ``Etc/GMT+5`` name
    no place, so they are refused rather than stored."""
    name = (zone or "").strip()
    if "/" not in name or name.lower().startswith("etc/"):
        raise ValueError(f"{zone!r} is not a place's time zone; use a name like America/Chicago")
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown time zone {zone!r}") from None
    return name


def _local(now_utc: datetime, zone: str) -> datetime | None:
    try:
        return now_utc.astimezone(ZoneInfo(zone))
    except Exception:  # an unknown zone name, or no time zone database
        logger.warning("unresolvable time zone %r; failing closed", zone)
        return None


def _inside(now_utc: datetime, zone: str, state: str | None, rules: QuietHoursRules) -> bool:
    local = _local(now_utc, zone)
    if local is None:
        return False
    start, end = rules.window_for(state, local.weekday() == 6)
    return start <= local.time() < end


def may_send_now(
    phone: str,
    now_utc: datetime,
    *,
    zone: str | None = None,
    rules: QuietHoursRules = DEFAULT_RULES,
) -> bool:
    """May this number be texted at ``now_utc``?

    It must be inside the window on every clock the number puts its owner on,
    and on ``zone``'s too when one is recorded (read with the unknown-state
    rule: a zone does not name a state). Nothing to check means no.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    checks = rules.checks(phone, zone)
    if not checks:
        logger.warning("no clock to check this number against; failing closed")
        return False
    return all(_inside(now_utc, z, state, rules) for z, state in checks)


def describe_window(
    phone: str,
    now_utc: datetime,
    *,
    zone: str | None = None,
    rules: QuietHoursRules = DEFAULT_RULES,
) -> str:
    """The window that applies to this recipient now, as text ("9:00 AM-9:00 PM",
    or "8:00 AM-8:00 PM in America/New_York and America/Los_Angeles"), derived
    the same way as :func:`may_send_now` so a refusal quotes the rule actually
    applied, Sunday's included."""

    def clock(t: dtime) -> str:
        return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"

    checks = rules.checks(phone, zone)
    parts: list[tuple[str, str]] = []
    for z, state in checks:
        local = _local(now_utc, z)
        is_sunday = local is not None and local.weekday() == 6
        start, end = rules.window_for(state, is_sunday)
        parts.append((f"{clock(start)}-{clock(end)}", z))
    if not parts:
        return "none: there is no clock to check this number against"
    if len(parts) == 1:
        return parts[0][0]
    windows = {window for window, _ in parts}
    if len(windows) == 1:
        return f"{parts[0][0]} in " + " and ".join(z for _, z in parts)
    return "; ".join(f"{window} in {z}" for window, z in parts)


__all__ = [
    "AREA_CODES_BY_STATE",
    "AREA_CODES_MULTI_ZONE",
    "AREA_CODE_STATES",
    "AREA_CODE_ZONES",
    "DEFAULT_RULES",
    "DEFAULT_STATE_RULES",
    "FEDERAL",
    "MULTI_ZONE_STATES",
    "QuietHoursRules",
    "StateRule",
    "describe_window",
    "may_send_now",
    "state_for",
    "validate_zone",
    "zone_for",
]
