from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfoNotFoundError

import pytest

from consent_ledger import quiet_hours
from consent_ledger.quiet_hours import (
    AREA_CODE_STATES,
    AREA_CODE_ZONES,
    AREA_CODES_BY_STATE,
    AREA_CODES_MULTI_ZONE,
    MULTI_ZONE_STATES,
    QuietHoursRules,
    StateRule,
    describe_window,
    may_send_now,
    state_for,
    validate_zone,
    zone_for,
)

MIAMI = "+13055550134"  # Florida, Eastern
SAN_FRANCISCO = "+14155550134"  # California, Pacific
AUSTIN = "+15125550134"  # Texas, Central
PHOENIX = "+16025550134"  # Arizona, no daylight saving time
HONOLULU = "+18085550134"
HARTFORD = "+18605550134"  # Connecticut
BALTIMORE = "+14105550134"  # Maryland
PANHANDLE = "+18505550134"  # Florida's 850: Eastern or Central
FLAGSTAFF = "+19285550134"  # Arizona's 928: includes the Navajo Nation
ANCHORAGE = "+19075550134"  # Alaska: Alaska time, and Hawaii-Aleutian on the islands
GUAM = "+16715550134"
SAIPAN = "+16705550134"
PAGO_PAGO = "+16845550134"
SAN_JUAN = "+17875550134"
TORONTO = "+14165550134"  # not in the table
LONDON = "+442079460134"


def utc(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# July 2026: St. John's UTC-2:30, New York UTC-4, Chicago UTC-5, Phoenix UTC-7,
# Los Angeles UTC-7, Anchorage UTC-8, Adak UTC-9, Honolulu UTC-10, Guam UTC+10,
# Pago Pago UTC-11. July 15 is a Wednesday, July 19 a Sunday.


@pytest.mark.parametrize(
    ("phone", "moment", "allowed"),
    [
        (MIAMI, utc(7, 15, 23, 59), True),  # 19:59 Miami
        (MIAMI, utc(7, 16, 0, 30), False),  # 20:30 Miami: Florida ends at 20:00
        (SAN_FRANCISCO, utc(7, 16, 3, 30), True),  # 20:30 in California: federal 21:00
        (SAN_FRANCISCO, utc(7, 16, 4, 0), False),  # 21:00
        (SAN_FRANCISCO, utc(7, 15, 14, 59), False),  # 07:59
        (AUSTIN, utc(7, 15, 13, 30), False),  # 08:30 Austin: Texas starts at 09:00
        (AUSTIN, utc(7, 15, 14, 0), True),  # 09:00
        (AUSTIN, utc(7, 19, 16, 0), False),  # Sunday 11:00: Texas waits until noon
        (AUSTIN, utc(7, 19, 17, 0), True),  # Sunday 12:00
        (PHOENIX, utc(7, 15, 14, 30), False),  # 07:30 Phoenix (no DST)
        (PHOENIX, utc(7, 15, 15, 0), True),  # 08:00
        (HONOLULU, utc(7, 15, 17, 59), False),  # 07:59 Honolulu
        (HONOLULU, utc(7, 15, 18, 0), True),  # 08:00
        (HARTFORD, utc(7, 15, 12, 30), False),  # 08:30 Hartford: Connecticut starts at 09:00
        (HARTFORD, utc(7, 15, 13, 0), True),  # 09:00
        (HARTFORD, utc(7, 16, 0, 30), False),  # 20:30: Connecticut ends at 20:00
        (BALTIMORE, utc(7, 16, 0, 30), False),  # 20:30: Maryland ends at 20:00
        (BALTIMORE, utc(7, 15, 12, 30), True),  # 08:30
    ],
)
def test_state_windows(phone: str, moment: datetime, allowed: bool) -> None:
    assert may_send_now(phone, moment) is allowed


@pytest.mark.parametrize(
    ("phone", "moment", "allowed"),
    [
        # Finding 3: the territories fell back to Eastern and Pacific, which
        # allowed texts at 02:30 in Guam and 05:30 in American Samoa.
        (GUAM, utc(7, 15, 16, 30), False),  # 02:30 Guam
        (GUAM, utc(7, 15, 0, 0), True),  # 10:00 Guam
        (SAIPAN, utc(7, 15, 16, 30), False),  # 02:30 Saipan
        (PAGO_PAGO, utc(7, 15, 16, 30), False),  # 05:30 Pago Pago
        (PAGO_PAGO, utc(7, 15, 23, 0), True),  # 12:00 Pago Pago
        (SAN_JUAN, utc(7, 15, 11, 30), False),  # 07:30 San Juan
        (SAN_JUAN, utc(7, 15, 12, 0), True),  # 08:00
    ],
)
def test_the_territories_keep_their_own_clocks(phone: str, moment: datetime, allowed: bool) -> None:
    assert may_send_now(phone, moment) is allowed


def test_a_multi_zone_state_is_checked_on_both_contiguous_clocks() -> None:
    assert zone_for(PANHANDLE) is None
    assert state_for(PANHANDLE) == "FL"
    # 13:00 New York, 10:00 Los Angeles: inside Florida's 08:00-20:00 on both.
    assert may_send_now(PANHANDLE, utc(7, 15, 17, 0))
    # 10:00 New York but 07:00 Los Angeles.
    assert not may_send_now(PANHANDLE, utc(7, 15, 14, 0))
    # 20:30 New York: inside the federal window, outside Florida's.
    assert not may_send_now(PANHANDLE, utc(7, 16, 0, 30))


def test_arizona_928_keeps_navajo_daylight_time_in_mind() -> None:
    # Finding 6: 928 includes the Navajo Nation, which keeps daylight time:
    # 20:30 in Phoenix is 21:30 in Window Rock.
    assert zone_for(FLAGSTAFF) is None
    assert not may_send_now(FLAGSTAFF, utc(7, 16, 3, 30))
    assert may_send_now(FLAGSTAFF, utc(7, 15, 18, 0))  # 11:00 Phoenix, 14:00 New York


def test_alaska_is_checked_on_both_of_its_clocks() -> None:
    assert not may_send_now(ANCHORAGE, utc(7, 15, 16, 30))  # 08:30 Anchorage, 07:30 Adak
    assert may_send_now(ANCHORAGE, utc(7, 15, 18, 0))  # 10:00 Anchorage, 09:00 Adak


def test_an_unknown_area_code_gets_only_the_hours_legal_everywhere() -> None:
    assert state_for(TORONTO) is None
    # 12:30 New York but 06:30 in Honolulu: a +1 number could be there.
    assert not may_send_now(TORONTO, utc(7, 15, 16, 30))
    # 17:30 in St. John's, 10:00 in Honolulu.
    assert may_send_now(TORONTO, utc(7, 15, 20, 0))
    # 20:30 in St. John's.
    assert not may_send_now(TORONTO, utc(7, 15, 23, 0))


def test_your_own_area_codes_give_a_number_its_zone() -> None:
    rules = QuietHoursRules(area_codes={"416": ("ON", "America/Toronto")})
    assert may_send_now(TORONTO, utc(7, 15, 16, 30), rules=rules)  # 12:30 Toronto


def test_a_foreign_number_fails_closed_unless_its_zone_is_known() -> None:
    noon_london = utc(7, 15, 11, 0)
    assert not may_send_now(LONDON, noon_london)
    assert may_send_now(LONDON, noon_london, zone="Europe/London")


def test_a_recorded_zone_adds_a_check_and_reads_as_an_unknown_state() -> None:
    # Finding 5: the recorded zone replaced the area code's clock.
    three_am_san_francisco = utc(7, 15, 10, 0)
    assert not may_send_now(SAN_FRANCISCO, three_am_san_francisco, zone="UTC")
    # A Miami number whose owner lives in Los Angeles, at 20:30 there: the
    # zone names no state, so Florida-or-anywhere's 20:00 applies.
    assert not may_send_now(MIAMI, utc(7, 16, 3, 30), zone="America/Los_Angeles")
    # A zone that agrees with the number changes nothing.
    assert may_send_now(SAN_FRANCISCO, utc(7, 16, 3, 30), zone="America/Los_Angeles")


def test_the_weekday_is_read_on_the_recipients_clock() -> None:
    # A rule whose Sunday ends early: Saturday 19:00 in Austin is already
    # Sunday 00:00 in UTC, and must still get Saturday's window.
    rules = QuietHoursRules(
        states={"TX": StateRule((dtime(9, 0), dtime(21, 0)), (dtime(12, 0), dtime(18, 0)))}
    )
    saturday_7pm_austin = utc(7, 19, 0, 0)
    assert may_send_now(AUSTIN, saturday_7pm_austin, rules=rules)


def test_your_own_states_add_to_the_defaults() -> None:
    # Finding 13: passing states={"CT": ...} dropped Florida, Oklahoma,
    # Washington and Texas.
    rules = QuietHoursRules(states={"CT": StateRule((dtime(9, 0), dtime(20, 0)))})
    assert not may_send_now(MIAMI, utc(7, 16, 0, 30), rules=rules)  # 20:30 Miami
    assert set(rules.states) >= {"FL", "OK", "WA", "TX", "MD", "CT"}


def test_the_unknown_state_rule_is_the_intersection_of_every_rule() -> None:
    assert QuietHoursRules().unknown_rule == StateRule(
        (dtime(9, 0), dtime(20, 0)), (dtime(12, 0), dtime(20, 0))
    )
    zz = {"ZZ": StateRule((dtime(10, 0), dtime(19, 0)))}
    assert QuietHoursRules(states=zz).unknown_rule.weekday == (dtime(10, 0), dtime(19, 0))
    # It follows the rules it is built from, a copy made with replace() included.
    copied = dataclasses.replace(QuietHoursRules(), states=zz)
    assert copied.unknown_rule.weekday == (dtime(10, 0), dtime(19, 0))


def test_no_zones_to_check_means_no() -> None:
    # Finding 13: fallback_zones=() allowed any hour.
    any_hour = utc(7, 15, 7, 0)  # 03:00 New York
    assert not may_send_now(PANHANDLE, any_hour, rules=QuietHoursRules(contiguous_zones=()))
    assert not may_send_now(TORONTO, any_hour, rules=QuietHoursRules(everywhere_zones=()))
    noon_new_york = utc(7, 15, 16, 0)
    assert not may_send_now(PANHANDLE, noon_new_york, rules=QuietHoursRules(contiguous_zones=()))


def test_an_unknown_zone_name_fails_closed() -> None:
    assert not may_send_now(SAN_FRANCISCO, utc(7, 15, 20, 0), zone="Mars/Olympus_Mons")


def test_no_time_zone_database_means_no_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 15: on a machine without one (Windows without tzdata).
    def missing(key: str) -> object:
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(quiet_hours, "ZoneInfo", missing)
    assert not may_send_now(SAN_FRANCISCO, utc(7, 15, 19, 0))


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        may_send_now(SAN_FRANCISCO, datetime(2026, 7, 15, 12, 0))


def test_a_recorded_zone_must_name_a_place() -> None:
    assert validate_zone("America/Chicago") == "America/Chicago"
    for zone in ("UTC", "GMT", "EST", "Etc/GMT+5", "Mars/Olympus_Mons", ""):
        with pytest.raises(ValueError, match="time zone"):
            validate_zone(zone)


def test_describe_window_matches_the_rule_applied() -> None:
    assert describe_window(MIAMI, utc(7, 15, 18, 0)) == "8:00 AM-8:00 PM"
    assert describe_window(AUSTIN, utc(7, 19, 16, 0)) == "12:00 PM-9:00 PM"
    assert describe_window(SAN_FRANCISCO, utc(7, 15, 18, 0)) == "8:00 AM-9:00 PM"
    assert (
        describe_window(PANHANDLE, utc(7, 15, 18, 0))
        == "8:00 AM-8:00 PM in America/New_York and America/Los_Angeles"
    )


def test_describe_window_on_a_sunday_quotes_sundays_rule() -> None:
    # The unknown-state window on a Sunday starts at noon, not at 09:00.
    sunday = utc(7, 19, 22, 15)  # 19:45 St. John's, 12:15 Honolulu
    assert may_send_now(TORONTO, sunday)
    assert describe_window(TORONTO, sunday) == (
        "12:00 PM-8:00 PM in America/St_Johns and Pacific/Honolulu"
    )


def test_no_area_code_is_listed_twice() -> None:
    single = [npa for _zone, npas in AREA_CODES_BY_STATE.values() for npa in npas.split()]
    multi = [npa for _zones, npas in AREA_CODES_MULTI_ZONE.values() for npa in npas.split()]
    assert len(single) == len(AREA_CODE_ZONES) == len(AREA_CODE_STATES)
    assert len(multi) == len(MULTI_ZONE_STATES)
    assert not set(single) & set(multi)
    for npa in ("850", "448", "915", "928", "907"):
        assert npa not in AREA_CODE_ZONES
    for npa in ("671", "670", "684", "787", "939", "340"):
        assert npa in AREA_CODE_ZONES
