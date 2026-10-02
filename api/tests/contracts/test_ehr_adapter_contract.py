"""The behavior every EHR adapter must show, whatever system it talks to.

Each adapter joins the suite by adding a harness to ``HARNESS_FACTORIES``. Phase 1
runs the in-memory fake; the FHIR R4 and Healthie adapters join with recorded fixtures.
"""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.ehr.ports import (
    ApprovedSummaryDocument,
    EhrAdapter,
    OperationNotSupportedError,
    PermanentSourceError,
    RateLimitedError,
    RecordNotFoundError,
    SignatureInvalidError,
    SourceRecord,
)
from app.timeline.canonical import content_sha256
from tests.fixtures import SENTINEL_FAMILY_NAME, populated_fake_adapter

MAX_PAGES = 100


@dataclass
class AdapterHarness:
    adapter: EhrAdapter
    patient_external_id: str
    throttle_next_call: Callable[[], None]
    sign_notification: Callable[[bytes], Mapping[str, str]]


def _fake_harness() -> AdapterHarness:
    adapter = populated_fake_adapter()
    return AdapterHarness(
        adapter=adapter,
        patient_external_id="patient-1",
        throttle_next_call=adapter.throttle_next_call,
        sign_notification=adapter.sign_notification,
    )


HARNESS_FACTORIES: dict[str, Callable[[], AdapterHarness]] = {"fake": _fake_harness}


@pytest.fixture(params=sorted(HARNESS_FACTORIES))
def harness(request: pytest.FixtureRequest) -> AdapterHarness:
    return HARNESS_FACTORIES[request.param]()


async def _all_changes(harness: AdapterHarness, since: datetime | None) -> list[SourceRecord]:
    capabilities = await harness.adapter.capabilities()
    records: list[SourceRecord] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await harness.adapter.fetch_changes(
            harness.patient_external_id, capabilities.record_kinds, since, cursor
        )
        records.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return records
    pytest.fail(f"fetch_changes did not finish within {MAX_PAGES} pages")


def _approved_document(harness: AdapterHarness) -> ApprovedSummaryDocument:
    return ApprovedSummaryDocument(
        summary_id=uuid4(),
        revision_sha256=bytes(32),
        patient_external_id=harness.patient_external_id,
        approved_by=uuid4(),
        approved_at=datetime(2026, 9, 30, 15, 0, tzinfo=UTC),
        body="Synthetic approved summary.",
    )


async def test_patient_pages_terminate_without_duplicates(harness: AdapterHarness) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await harness.adapter.list_patients(cursor, page_size=2)
        seen.extend(patient.external_id for patient in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        pytest.fail(f"list_patients did not finish within {MAX_PAGES} pages")

    assert seen
    assert len(seen) == len(set(seen))


async def test_an_unissued_cursor_is_a_typed_error(harness: AdapterHarness) -> None:
    with pytest.raises(PermanentSourceError):
        await harness.adapter.list_patients("not-a-cursor-this-source-issued", page_size=2)


async def test_every_record_is_named_and_its_hash_matches_its_payload(
    harness: AdapterHarness,
) -> None:
    records = await _all_changes(harness, since=None)

    assert records
    for record in records:
        assert record.system == harness.adapter.source
        assert record.resource_type
        assert record.resource_id
        assert record.content_sha256 == content_sha256(record.payload, record.system.kind)


async def test_a_listed_record_re_resolves_to_the_same_content(harness: AdapterHarness) -> None:
    for listed in await _all_changes(harness, since=None):
        exact_version = await harness.adapter.get_record(
            listed.resource_type, listed.resource_id, listed.version_id
        )
        latest = await harness.adapter.get_record(listed.resource_type, listed.resource_id)

        assert exact_version.content_sha256 == listed.content_sha256
        assert latest.content_sha256 == listed.content_sha256


async def test_changes_since_a_time_are_strictly_later_and_repeatable(
    harness: AdapterHarness,
) -> None:
    everything = await _all_changes(harness, since=None)
    update_times = sorted(
        record.source_updated_at for record in everything if record.source_updated_at is not None
    )
    cutoff = update_times[len(update_times) // 2]

    first_pass = await _all_changes(harness, since=cutoff)
    second_pass = await _all_changes(harness, since=cutoff)

    assert first_pass
    assert all(
        record.source_updated_at is not None and record.source_updated_at > cutoff
        for record in first_pass
    )
    assert [record.content_sha256 for record in first_pass] == [
        record.content_sha256 for record in second_pass
    ]


async def test_no_naive_datetime_crosses_the_boundary(harness: AdapterHarness) -> None:
    for record in await _all_changes(harness, since=None):
        if record.source_updated_at is not None:
            assert record.source_updated_at.utcoffset() is not None


async def test_an_unknown_record_is_not_found(harness: AdapterHarness) -> None:
    with pytest.raises(RecordNotFoundError):
        await harness.adapter.get_record("Observation", f"missing-{uuid4()}")


async def test_throttling_is_a_typed_error_with_a_retry_hint(harness: AdapterHarness) -> None:
    harness.throttle_next_call()

    with pytest.raises(RateLimitedError) as raised:
        await harness.adapter.list_patients(None, page_size=2)
    assert raised.value.retry_after_seconds is None or raised.value.retry_after_seconds >= 0


async def test_write_back_is_idempotent_or_declared_unsupported(harness: AdapterHarness) -> None:
    document = _approved_document(harness)
    if not (await harness.adapter.capabilities()).supports_write_back:
        with pytest.raises(OperationNotSupportedError):
            await harness.adapter.write_back(document, "key-1")
        return

    first = await harness.adapter.write_back(document, "key-1")
    repeated = await harness.adapter.write_back(document, "key-1")
    other = await harness.adapter.write_back(document, "key-2")

    assert repeated == first
    assert other.resource_id != first.resource_id
    written = await harness.adapter.get_record(first.resource_type, first.resource_id)
    assert written.resource_id == first.resource_id


async def test_notifications_need_a_valid_signature(harness: AdapterHarness) -> None:
    body = json.dumps(
        {
            "events": [
                {"resource_type": "Observation", "resource_id": "obs-1-1", "event_type": "updated"},
                {
                    "resource_type": "Observation",
                    "resource_id": "obs-1-2",
                    "event_type": "teleported",
                },
            ]
        }
    ).encode()
    if not (await harness.adapter.capabilities()).supports_notifications:
        with pytest.raises(OperationNotSupportedError):
            harness.adapter.parse_notification({}, body)
        return

    with pytest.raises(SignatureInvalidError):
        harness.adapter.parse_notification({"x-fake-signature": "0" * 64}, body)

    changes = harness.adapter.parse_notification(harness.sign_notification(body), body)
    assert [(change.resource_id, change.event_type) for change in changes] == [
        ("obs-1-1", "updated")
    ]


async def test_payload_content_never_reaches_logs_errors_or_reprs(
    harness: AdapterHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    surfaced_text: list[str] = []

    records = await _all_changes(harness, since=None)
    surfaced_text.extend(repr(record) for record in records)
    surfaced_text.append(repr(_approved_document(harness)))
    for failing_call in (
        harness.adapter.get_record("Patient", f"missing-{uuid4()}"),
        harness.adapter.list_patients("garbage-cursor", page_size=2),
    ):
        with pytest.raises(Exception) as raised:  # noqa: PT011  # any adapter error type
            await failing_call
        surfaced_text.append(str(raised.value))
    surfaced_text.extend(record.getMessage() for record in caplog.records)

    assert any(SENTINEL_FAMILY_NAME.encode() in record.payload for record in records)
    assert not [text for text in surfaced_text if SENTINEL_FAMILY_NAME in text]


async def test_a_page_size_below_one_is_refused(harness: AdapterHarness) -> None:
    with pytest.raises(ValueError, match="page_size"):
        await harness.adapter.list_patients(None, page_size=0)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"events": "abc"}', id="events not a list"),
        pytest.param(b'{"events": [1]}', id="event not an object"),
        pytest.param(
            b'{"events": [{"event_type": "updated", "resource_id": "x"}]}',
            id="missing resource type",
        ),
        pytest.param(b"not json", id="not json"),
    ],
)
async def test_a_signed_but_malformed_notification_is_a_typed_error(
    harness: AdapterHarness, body: bytes
) -> None:
    if not (await harness.adapter.capabilities()).supports_notifications:
        pytest.skip("adapter declares no notifications")

    with pytest.raises(PermanentSourceError):
        harness.adapter.parse_notification(harness.sign_notification(body), body)


async def test_a_non_ascii_signature_is_rejected_not_crashed(harness: AdapterHarness) -> None:
    if not (await harness.adapter.capabilities()).supports_notifications:
        pytest.skip("adapter declares no notifications")
    body = b'{"events": []}'

    with pytest.raises(SignatureInvalidError):
        harness.adapter.parse_notification({"x-fake-signature": "\u00e9" * 64}, body)
