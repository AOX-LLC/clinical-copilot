"""The behavior every EHR adapter must show, whatever system it talks to.

Each adapter joins the suite by adding a harness to ``HARNESS_FACTORIES``. The in-memory
fake and the FHIR R4 adapter over recorded responses run by default. ``fhir-live`` runs the
same suite against the running stack and is skipped unless ``LIVE_FHIR_BASE_URL`` is set;
``healthie-fixture`` runs the Healthie adapter over hand-built synthetic fixtures (not
recordings; there is no live Healthie run).
"""

import base64
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.ehr.fhir_r4 import FhirR4Adapter
from app.ehr.healthie import HealthieAdapter, HealthieConfig, sign_webhook
from app.ehr.ports import (
    ApprovedSummaryDocument,
    EhrAdapter,
    OperationNotSupportedError,
    PermanentSourceError,
    RateLimitedError,
    RecordNotFoundError,
    SignatureInvalidError,
    SourceRecord,
    SourceSystemRef,
)
from app.timeline.canonical import content_sha256
from app.timeline.vocabulary import SourceKind
from tests.fixtures import SENTINEL_FAMILY_NAME, populated_fake_adapter
from tests.recorded.healthie_server import API_KEY, ENDPOINT, FixtureHealthie
from tests.recorded.replay import BASE_URL, ReplayTransport, ThrottlingTransport

MAX_PAGES = 100
LIVE_URL_VARIABLE = "LIVE_FHIR_BASE_URL"
FHIR_SOURCE = SourceSystemRef(code="fhir-local", kind=SourceKind.FHIR_R4)
# A synthetic patient from the committed dataset, recorded for the offline run and read live.
CONTRACT_PATIENT_ID = "3b680a6c-b15d-ef35-a863-b733f0230d9d"


FAKE_NOTIFICATION = json.dumps(
    {
        "events": [
            {"resource_type": "Observation", "resource_id": "obs-1-1", "event_type": "updated"},
            {"resource_type": "Observation", "resource_id": "obs-1-2", "event_type": "teleported"},
        ]
    }
).encode()
FAKE_MALFORMED_NOTIFICATIONS = (
    b'{"events": "abc"}',
    b'{"events": [1]}',
    b'{"events": [{"event_type": "updated", "resource_id": "x"}]}',
    b"not json",
)


@dataclass
class AdapterHarness:
    adapter: EhrAdapter
    patient_external_id: str
    throttle_next_call: Callable[[], None]
    sign_notification: Callable[[bytes], Mapping[str, str]]
    # A distinctive value present in the source payloads that must never surface in logs,
    # error messages or reprs.
    payload_sentinel: str
    # What notification tests send: a signed body that names one known change and one event the
    # adapter must ignore, the change expected from it, headers that must be refused, and
    # bodies that are signed but malformed.
    notification_body: bytes = FAKE_NOTIFICATION
    expected_change: tuple[str, str] = ("obs-1-1", "updated")
    forged_signature_headers: Mapping[str, str] = field(
        default_factory=lambda: {"x-fake-signature": "0" * 64}
    )
    non_ascii_signature_headers: Mapping[str, str] = field(
        default_factory=lambda: {"x-fake-signature": "\u00e9" * 64}
    )
    malformed_notification_bodies: tuple[bytes, ...] = FAKE_MALFORMED_NOTIFICATIONS
    # A type the adapter reads, asked for with an id that does not exist, so the not-found test
    # reaches the source's own answer.
    missing_record_type: str = "Observation"


def _fake_harness() -> AdapterHarness:
    adapter = populated_fake_adapter()
    return AdapterHarness(
        adapter=adapter,
        patient_external_id="patient-1",
        throttle_next_call=adapter.throttle_next_call,
        sign_notification=adapter.sign_notification,
        payload_sentinel=SENTINEL_FAMILY_NAME,
    )


def _fhir_harness(
    transport: ThrottlingTransport, base_url: str, patient_id: str, sentinel: str
) -> AdapterHarness:
    client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=60.0)
    return AdapterHarness(
        adapter=FhirR4Adapter(FHIR_SOURCE, client),
        patient_external_id=patient_id,
        throttle_next_call=transport.throttle_next_call,
        sign_notification=lambda _body: {},
        payload_sentinel=sentinel,
    )


def _fhir_recorded_harness() -> AdapterHarness:
    replay = ReplayTransport()
    sentinel = replay.family_names[CONTRACT_PATIENT_ID]
    transport = ThrottlingTransport(replay)
    return _fhir_harness(transport, BASE_URL, CONTRACT_PATIENT_ID, sentinel)


def _fhir_live_harness() -> AdapterHarness:
    base_url = os.environ[LIVE_URL_VARIABLE]
    patient = httpx.get(f"{base_url}/Patient/{CONTRACT_PATIENT_ID}", timeout=30.0).json()
    transport = ThrottlingTransport(httpx.AsyncHTTPTransport())
    sentinel = patient["name"][0]["family"]
    return _fhir_harness(transport, base_url, CONTRACT_PATIENT_ID, sentinel)


HEALTHIE_SOURCE = SourceSystemRef(code="healthie-fixture", kind=SourceKind.HEALTHIE)
HEALTHIE_WEBHOOK_PATH = "/webhooks/healthie"
HEALTHIE_SENTINEL = "Quillfeather-Healthie"


def _healthie_harness() -> AdapterHarness:
    transport = FixtureHealthie()
    config = HealthieConfig(
        endpoint=ENDPOINT,
        api_key=API_KEY,
        webhook_secret="whsec_synthetic-test-secret",
        webhook_path=HEALTHIE_WEBHOOK_PATH,
        write_back_enabled=True,
        allow_custom_endpoint=True,
    )
    adapter = HealthieAdapter(HEALTHIE_SOURCE, httpx.AsyncClient(transport=transport), config)
    wrong_digest = {"Content-Digest": "SHA-256=" + "0" * 64, "Signature": "sig1=" + "0" * 64}
    return AdapterHarness(
        adapter=adapter,
        patient_external_id="9001",
        throttle_next_call=transport.throttle_next_call,
        sign_notification=lambda body: sign_webhook(config, body),
        payload_sentinel=HEALTHIE_SENTINEL,
        notification_body=json.dumps(
            {
                "resource_id": "7101",
                "resource_id_type": "Medication",
                "event_type": "medication.updated",
            }
        ).encode(),
        expected_change=("7101", "medication.updated"),
        forged_signature_headers=wrong_digest,
        non_ascii_signature_headers={
            "Content-Digest": wrong_digest["Content-Digest"],
            "Signature": "sig1=" + "\u00e9" * 64,
        },
        malformed_notification_bodies=(
            b"[1]",
            b'{"event_type": "patient.updated"}',
            b'{"event_type": "patient.updated", "resource_id": "../x"}',
            b'{"resource_id": "9001"}',
            b"not json",
        ),
        missing_record_type="Medication",
    )


HARNESS_FACTORIES: dict[str, Callable[[], AdapterHarness]] = {
    "fake": _fake_harness,
    "healthie-fixture": _healthie_harness,
    "fhir-recorded": _fhir_recorded_harness,
    "fhir-live": _fhir_live_harness,
}


@pytest.fixture(
    params=[
        pytest.param(name, marks=pytest.mark.live) if name.endswith("-live") else name
        for name in sorted(HARNESS_FACTORIES)
    ]
)
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


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("not-a-cursor-this-source-issued", id="garbage"),
        pytest.param(base64.urlsafe_b64encode(b"offset:-1").decode(), id="negative offset"),
    ],
)
async def test_an_unissued_cursor_is_a_typed_error(harness: AdapterHarness, cursor: str) -> None:
    with pytest.raises(PermanentSourceError):
        await harness.adapter.list_patients(cursor, page_size=2)


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
    if not (await harness.adapter.capabilities()).supports_since:
        # A source that cannot answer "changed after" returns everything, every time.
        unfiltered = await _all_changes(harness, since=cutoff)
        assert sorted(record.content_sha256 for record in unfiltered) == sorted(
            record.content_sha256 for record in everything
        )
        return

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
        await harness.adapter.get_record(harness.missing_record_type, f"missing-{uuid4()}")


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
    body = harness.notification_body
    if not (await harness.adapter.capabilities()).supports_notifications:
        with pytest.raises(OperationNotSupportedError):
            harness.adapter.parse_notification({}, body)
        return

    with pytest.raises(SignatureInvalidError):
        harness.adapter.parse_notification(harness.forged_signature_headers, body)

    changes = harness.adapter.parse_notification(harness.sign_notification(body), body)
    assert [(change.resource_id, change.event_type) for change in changes] == [
        harness.expected_change
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

    assert any(harness.payload_sentinel.encode() in record.payload for record in records)
    assert not [text for text in surfaced_text if harness.payload_sentinel in text]


async def test_a_page_size_below_one_is_refused(harness: AdapterHarness) -> None:
    with pytest.raises(ValueError, match="page_size"):
        await harness.adapter.list_patients(None, page_size=0)


async def test_a_signed_but_malformed_notification_is_a_typed_error(
    harness: AdapterHarness,
) -> None:
    if not (await harness.adapter.capabilities()).supports_notifications:
        pytest.skip("adapter declares no notifications")

    for body in harness.malformed_notification_bodies:
        with pytest.raises(PermanentSourceError):
            harness.adapter.parse_notification(harness.sign_notification(body), body)


async def test_a_non_ascii_signature_is_rejected_not_crashed(harness: AdapterHarness) -> None:
    if not (await harness.adapter.capabilities()).supports_notifications:
        pytest.skip("adapter declares no notifications")

    with pytest.raises(SignatureInvalidError):
        harness.adapter.parse_notification(
            harness.non_ascii_signature_headers, harness.notification_body
        )
