from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.timeline.clinical_time import (
    ClinicalTimeError,
    clinic_calendar_date,
    parse_fhir_datetime,
    sort_instant,
)
from app.timeline.vocabulary import TimePrecision

NEW_YORK = ZoneInfo("America/New_York")
ZONES_ACROSS_THE_DATE_LINE = [
    ZoneInfo(name)
    for name in (
        "Pacific/Pago_Pago",
        "America/Los_Angeles",
        "America/New_York",
        "UTC",
        "Asia/Kolkata",
        "Pacific/Kiritimati",
    )
]


@pytest.mark.parametrize(
    ("raw", "precision", "calendar_date"),
    [
        ("2026", TimePrecision.YEAR, date(2026, 1, 1)),
        ("2026-03", TimePrecision.MONTH, date(2026, 3, 1)),
        ("2026-03-08", TimePrecision.DAY, date(2026, 3, 8)),
    ],
)
def test_partial_dates_keep_their_precision(
    raw: str, precision: TimePrecision, calendar_date: date
) -> None:
    parsed = parse_fhir_datetime(raw)

    assert parsed.precision is precision
    assert parsed.calendar_date == calendar_date
    assert parsed.instant is None
    assert parsed.raw == raw


def test_instants_are_normalized_to_utc_and_keep_the_original_text() -> None:
    parsed = parse_fhir_datetime("2026-03-08T09:15:00.123-05:00")

    assert parsed.precision is TimePrecision.INSTANT
    assert parsed.instant == datetime(2026, 3, 8, 14, 15, 0, 123000, tzinfo=UTC)
    assert parsed.raw == "2026-03-08T09:15:00.123-05:00"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("2026-03-08T09:15:00", id="time without offset"),
        pytest.param("2026-02-30", id="impossible date"),
        pytest.param("2026-13", id="impossible month"),
        pytest.param("26-03-08", id="two-digit year"),
        pytest.param("2026-03-08T25:00:00Z", id="impossible hour"),
        pytest.param("", id="empty"),
    ],
)
def test_malformed_or_ambiguous_times_are_rejected(raw: str) -> None:
    with pytest.raises(ClinicalTimeError):
        parse_fhir_datetime(raw)


def test_date_only_events_sort_at_local_midnight_across_dst_changes() -> None:
    spring_forward = parse_fhir_datetime("2026-03-08")
    fall_back = parse_fhir_datetime("2026-11-01")

    assert sort_instant(spring_forward, NEW_YORK) == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert sort_instant(fall_back, NEW_YORK) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


def test_instants_either_side_of_the_dst_gap_order_by_real_time() -> None:
    before_gap = parse_fhir_datetime("2026-03-08T01:30:00-05:00")
    after_gap = parse_fhir_datetime("2026-03-08T03:30:00-04:00")

    assert sort_instant(after_gap, NEW_YORK) - sort_instant(before_gap, NEW_YORK) == timedelta(
        hours=1
    )


def test_an_evening_instant_falls_on_the_practice_calendar_date() -> None:
    evening_lab = parse_fhir_datetime("2026-03-08T03:30:00Z")

    assert clinic_calendar_date(evening_lab, NEW_YORK) == date(2026, 3, 7)


@given(
    calendar_date=st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)),
    zone=st.sampled_from(ZONES_ACROSS_THE_DATE_LINE),
)
def test_a_date_only_event_never_moves_to_another_day(calendar_date: date, zone: ZoneInfo) -> None:
    parsed = parse_fhir_datetime(calendar_date.isoformat())

    assert clinic_calendar_date(parsed, zone) == calendar_date
    assert sort_instant(parsed, zone).astimezone(zone).date() == calendar_date


@given(
    moment=st.datetimes(
        min_value=datetime(1900, 1, 1),
        max_value=datetime(2100, 12, 31),
        timezones=st.sampled_from(
            [timezone(timedelta(hours=hours)) for hours in (-12, -5, 0, 5, 14)]
        ),
    )
)
def test_any_offset_instant_round_trips_to_the_same_moment(moment: datetime) -> None:
    parsed = parse_fhir_datetime(moment.isoformat())

    assert parsed.instant == moment
    assert parsed.instant is not None
    assert parsed.instant.tzinfo is UTC
