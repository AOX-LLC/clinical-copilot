"""What the FHIR R4 adapter does beyond the shared contract. Synthetic data only."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.ehr import fhir_r4
from app.ehr.fhir_r4 import FhirR4Adapter
from app.ehr.ports import (
    ApprovedSummaryDocument,
    OperationNotSupportedError,
    PermanentSourceError,
    RateLimitedError,
    RecordKind,
    RecordNotFoundError,
    RetryableSourceError,
    SourceRecord,
    SourceSystemRef,
)
from app.timeline.canonical import content_sha256
from app.timeline.vocabulary import SourceKind
from tests.recorded.replay import BASE_URL, ReplayTransport

SOURCE = SourceSystemRef(code="fhir-local", kind=SourceKind.FHIR_R4)
PATIENT_ID = "11111111-0000-4000-8000-000000000001"
ALL_KINDS = frozenset(RecordKind) - {RecordKind.DOCUMENT, RecordKind.APPOINTMENT}

# Number tokens a re-serialized float would change: trailing zeros and exponents.
OBSERVATION_BYTES = (
    b'{"resourceType":"Observation","id":"obs-1","meta":{"versionId":"3",'
    b'"lastUpdated":"2026-10-02T13:59:17.5281016+00:00"},"status":"final",'
    b'"valueQuantity":{"value":1.50,"unit":"mg/dL"},"referenceRange":[{"low":{"value":1.0},'
    b'"high":{"value":1E2}}]}'
)


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> FhirR4Adapter:
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return FhirR4Adapter(SOURCE, client)


def _bundle_response(*resources: bytes) -> httpx.Response:
    entries = b",".join(b'{"resource":' + resource + b"}" for resource in resources)
    body = b'{"resourceType":"Bundle","type":"searchset","entry":[' + entries + b"]}"
    return httpx.Response(200, content=body)


def _replay_adapter(requests: list[httpx.Request]) -> FhirR4Adapter:
    replay = ReplayTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return await replay.handle_async_request(request)

    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return FhirR4Adapter(SOURCE, client)


async def test_it_declares_what_the_server_cannot_do() -> None:
    capabilities = await _adapter(lambda _request: httpx.Response(200)).capabilities()

    assert not capabilities.supports_versions
    assert not capabilities.supports_since
    assert not capabilities.supports_write_back
    assert not capabilities.supports_notifications
    assert RecordKind.DOCUMENT not in capabilities.record_kinds
    assert RecordKind.MEDICATION in capabilities.record_kinds


async def test_a_search_result_hashes_the_same_as_a_direct_read_and_keeps_number_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Observation/obs-1"):
            return httpx.Response(200, content=OBSERVATION_BYTES)
        return _bundle_response(OBSERVATION_BYTES)

    adapter = _adapter(handler)

    page = await adapter.fetch_changes(PATIENT_ID, frozenset({RecordKind.OBSERVATION}), None, None)
    direct = await adapter.get_record("Observation", "obs-1")

    listed = page.items[0]
    assert listed.content_sha256 == direct.content_sha256
    for token in (b'"value":1.50', b'"value":1.0', b'"value":1E2'):
        assert token in listed.payload
    assert direct.payload == OBSERVATION_BYTES
    assert listed.content_sha256 == content_sha256(OBSERVATION_BYTES, SourceKind.FHIR_R4)


async def test_a_record_carries_its_version_and_a_timezone_aware_update_time() -> None:
    adapter = _adapter(lambda _request: httpx.Response(200, content=OBSERVATION_BYTES))

    record = await adapter.get_record("Observation", "obs-1")

    assert record.version_id == "3"
    assert record.source_updated_at is not None
    assert record.source_updated_at.utcoffset() == UTC.utcoffset(None)
    assert record.source_updated_at.year == 2026


async def test_a_resource_without_meta_has_no_version_or_update_time() -> None:
    body = b'{"resourceType":"Observation","id":"obs-2","status":"final"}'
    adapter = _adapter(lambda _request: httpx.Response(200, content=body))

    record = await adapter.get_record("Observation", "obs-2")

    assert (record.version_id, record.source_updated_at) == (None, None)


@pytest.mark.parametrize(
    "last_updated", [pytest.param("2026-10-02T13:59:17", id="no timezone"), pytest.param("soon")]
)
async def test_an_unusable_update_time_is_a_typed_error(last_updated: str) -> None:
    body = json.dumps(
        {"resourceType": "Observation", "id": "obs-3", "meta": {"lastUpdated": last_updated}}
    ).encode()
    adapter = _adapter(lambda _request: httpx.Response(200, content=body))

    with pytest.raises(PermanentSourceError):
        await adapter.get_record("Observation", "obs-3")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(httpx.Response(404), RecordNotFoundError, id="404"),
        pytest.param(httpx.Response(500), RetryableSourceError, id="500"),
        pytest.param(httpx.Response(503), RetryableSourceError, id="503"),
        pytest.param(httpx.Response(400), PermanentSourceError, id="400"),
        pytest.param(httpx.Response(401), PermanentSourceError, id="401"),
        pytest.param(httpx.Response(200, content=b"<html>"), PermanentSourceError, id="not JSON"),
        pytest.param(httpx.Response(200, content=b"[1]"), PermanentSourceError, id="not an object"),
    ],
)
async def test_failures_map_to_typed_errors(
    response: httpx.Response, expected: type[Exception]
) -> None:
    adapter = _adapter(lambda _request: response)

    with pytest.raises(expected):
        await adapter.get_record("Observation", "obs-1")


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [
        pytest.param({"Retry-After": "7"}, 7.0, id="seconds"),
        pytest.param({}, None, id="absent"),
        pytest.param({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None, id="a date"),
    ],
)
async def test_throttling_carries_the_retry_hint_when_there_is_one(
    headers: dict[str, str], retry_after: float | None
) -> None:
    adapter = _adapter(lambda _request: httpx.Response(429, headers=headers))

    with pytest.raises(RateLimitedError) as raised:
        await adapter.get_record("Observation", "obs-1")

    assert raised.value.retry_after_seconds == retry_after


async def test_a_dropped_connection_is_retryable_and_the_message_names_no_address() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RetryableSourceError) as raised:
        await _adapter(refuse).list_patients(None, page_size=5)

    assert "ConnectError" in str(raised.value)
    assert "fhir.recorded" not in str(raised.value)


async def test_a_malformed_bundle_is_a_typed_error() -> None:
    body = b'{"resourceType":"Bundle","entry":[{"resource":{"resourceType":"Observation"}}]}'
    adapter = _adapter(lambda _request: httpx.Response(200, content=body))

    with pytest.raises(PermanentSourceError, match="malformed"):
        await adapter.list_patients(None, page_size=5)


@pytest.mark.parametrize("resource_id", ["../metadata", "a/b", "a b", "", "x" * 65, "id?x=1"])
async def test_an_id_that_could_change_the_request_path_never_reaches_the_server(
    resource_id: str,
) -> None:
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)

    with pytest.raises(RecordNotFoundError):
        await adapter.get_record("Observation", resource_id)
    with pytest.raises(RecordNotFoundError):
        await adapter.fetch_changes(resource_id, frozenset({RecordKind.OBSERVATION}), None, None)

    assert requests == []


async def test_a_resource_type_that_could_change_the_request_path_never_reaches_the_server() -> (
    None
):
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)

    with pytest.raises(RecordNotFoundError):
        await adapter.get_record("Observation/../metadata", "obs-1")

    assert requests == []


async def test_an_exact_version_other_than_the_current_one_is_not_supported() -> None:
    adapter = _adapter(lambda _request: httpx.Response(200, content=OBSERVATION_BYTES))

    current = await adapter.get_record("Observation", "obs-1", version_id="3")
    with pytest.raises(OperationNotSupportedError, match="version 2"):
        await adapter.get_record("Observation", "obs-1", version_id="2")

    assert current.version_id == "3"


async def test_changes_ignore_since_and_never_filter_by_time_or_ask_the_server_to() -> None:
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)
    patient_id = next(iter(ReplayTransport().patient_ids))
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    far_future = datetime(2999, 1, 1, tzinfo=UTC)

    everything = await _all(adapter, patient_id, since=None)
    after_long_ago = await _all(adapter, patient_id, since=long_ago)
    after_far_future = await _all(adapter, patient_id, since=far_future)

    assert everything
    assert [r.content_sha256 for r in after_long_ago] == [r.content_sha256 for r in everything]
    assert [r.content_sha256 for r in after_far_future] == [r.content_sha256 for r in everything]
    sent = " ".join(str(request.url) for request in requests)
    assert "_lastUpdated" not in sent
    assert "_count" not in sent


async def test_changes_page_in_a_stable_order_without_gaps_or_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 7)
    adapter = _replay_adapter([])
    patient_id = next(iter(ReplayTransport().patient_ids))

    whole = await _all(adapter, patient_id, since=None)
    pages: list[list[SourceRecord]] = []
    cursor: str | None = None
    while True:
        page = await adapter.fetch_changes(patient_id, ALL_KINDS, None, cursor)
        pages.append(list(page.items))
        cursor = page.next_cursor
        if cursor is None:
            break

    flat = [record for page in pages for record in page]
    assert len(pages) > 1
    assert all(len(page) <= 7 for page in pages)
    assert [(r.resource_type, r.resource_id) for r in flat] == sorted(
        (r.resource_type, r.resource_id) for r in flat
    )
    assert len({(r.resource_type, r.resource_id) for r in flat}) == len(flat) == len(whole)


async def test_only_the_requested_kinds_are_fetched() -> None:
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)
    patient_id = next(iter(ReplayTransport().patient_ids))

    page = await adapter.fetch_changes(patient_id, frozenset({RecordKind.IMMUNIZATION}), None, None)

    assert {record.resource_type for record in page.items} == {"Immunization"}
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == ["Immunization"]


async def test_every_recorded_resource_hashes_the_same_listed_and_read_directly() -> None:
    adapter = _replay_adapter([])
    checked = 0

    for patient_id in ReplayTransport().patient_ids:
        for listed in await _all(adapter, patient_id, since=None):
            direct = await adapter.get_record(listed.resource_type, listed.resource_id)
            assert direct.content_sha256 == listed.content_sha256
            checked += 1

    assert checked > 100


async def test_listing_patients_reads_every_patient_once_in_id_order() -> None:
    adapter = _replay_adapter([])
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await adapter.list_patients(cursor, page_size=2)
        seen.extend(patient.external_id for patient in page.items)
        assert all(patient.record.resource_type == "Patient" for patient in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == sorted(ReplayTransport().patient_ids)


async def test_write_back_and_notifications_are_declared_unsupported() -> None:
    adapter = _adapter(lambda _request: httpx.Response(200))
    document = ApprovedSummaryDocument(
        summary_id=uuid4(),
        revision_sha256=bytes(32),
        patient_external_id=PATIENT_ID,
        approved_by=uuid4(),
        approved_at=datetime(2026, 9, 30, tzinfo=UTC),
        body="Synthetic.",
    )

    with pytest.raises(OperationNotSupportedError):
        await adapter.write_back(document, "key")
    with pytest.raises(OperationNotSupportedError):
        adapter.parse_notification({}, b"{}")


async def test_later_pages_come_from_the_snapshot_without_touching_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 5)
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)
    patient_id = next(iter(ReplayTransport().patient_ids))

    first = await adapter.fetch_changes(patient_id, ALL_KINDS, None, None)
    requests_for_first_page = len(requests)
    assert first.next_cursor is not None
    cursor: str | None = first.next_cursor
    while cursor is not None:
        cursor = (await adapter.fetch_changes(patient_id, ALL_KINDS, None, cursor)).next_cursor

    assert len(requests) == requests_for_first_page


async def test_pages_stay_consistent_when_the_source_changes_mid_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 2)
    resources = [
        b'{"resourceType":"Observation","id":"obs-%d","status":"final"}' % number
        for number in (2, 3, 4, 5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _bundle_response(*resources)

    adapter = _adapter(handler)
    kinds = frozenset({RecordKind.OBSERVATION})

    first = await adapter.fetch_changes(PATIENT_ID, kinds, None, None)
    # A record that sorts before everything already served appears at the source.
    resources.insert(0, b'{"resourceType":"Observation","id":"obs-1","status":"final"}')
    second = await adapter.fetch_changes(PATIENT_ID, kinds, None, first.next_cursor)

    served = [record.resource_id for record in (*first.items, *second.items)]
    assert served == ["obs-2", "obs-3", "obs-4", "obs-5"]
    assert second.next_cursor is None


async def test_a_new_listing_sees_what_changed_at_the_source() -> None:
    resources = [b'{"resourceType":"Observation","id":"obs-2","status":"final"}']
    adapter = _adapter(lambda _request: _bundle_response(*resources))
    kinds = frozenset({RecordKind.OBSERVATION})

    before = await adapter.fetch_changes(PATIENT_ID, kinds, None, None)
    resources.append(b'{"resourceType":"Observation","id":"obs-3","status":"final"}')
    after = await adapter.fetch_changes(PATIENT_ID, kinds, None, None)

    assert [r.resource_id for r in before.items] == ["obs-2"]
    assert [r.resource_id for r in after.items] == ["obs-2", "obs-3"]


async def test_a_cursor_whose_snapshot_expired_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 3)
    clock = [1000.0]
    monkeypatch.setattr(fhir_r4, "_now", lambda: clock[0])
    adapter = _replay_adapter([])
    patient_id = next(iter(ReplayTransport().patient_ids))
    first = await adapter.fetch_changes(patient_id, ALL_KINDS, None, None)
    assert first.next_cursor is not None

    clock[0] += fhir_r4.SNAPSHOT_SECONDS + 1

    with pytest.raises(RetryableSourceError, match="start the listing again"):
        await adapter.fetch_changes(patient_id, ALL_KINDS, None, first.next_cursor)


async def test_a_cursor_cannot_be_used_again_after_its_listing_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 40)
    adapter = _replay_adapter([])
    patient_id = next(iter(ReplayTransport().patient_ids))
    first = await adapter.fetch_changes(patient_id, ALL_KINDS, None, None)
    assert first.next_cursor is not None
    last = await adapter.fetch_changes(patient_id, ALL_KINDS, None, first.next_cursor)
    cursors = [first.next_cursor]
    while last.next_cursor is not None:
        cursors.append(last.next_cursor)
        last = await adapter.fetch_changes(patient_id, ALL_KINDS, None, last.next_cursor)

    with pytest.raises(RetryableSourceError):
        await adapter.fetch_changes(patient_id, ALL_KINDS, None, cursors[0])


async def test_a_cursor_from_another_listing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 3)
    adapter = _replay_adapter([])
    first_patient, second_patient = ReplayTransport().patient_ids[:2]
    page = await adapter.fetch_changes(first_patient, ALL_KINDS, None, None)
    assert page.next_cursor is not None

    with pytest.raises(PermanentSourceError, match="different listing"):
        await adapter.fetch_changes(second_patient, ALL_KINDS, None, page.next_cursor)
    with pytest.raises(PermanentSourceError, match="different listing"):
        await adapter.list_patients(page.next_cursor, page_size=2)


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param(
            base64.urlsafe_b64encode(b"snapshot:" + b"a" * 32 + b":-1").decode(), id="negative"
        ),
        pytest.param(base64.urlsafe_b64encode(b"snapshot:short:1").decode(), id="short id"),
        pytest.param(base64.urlsafe_b64encode(b"snapshot:" + b"a" * 32).decode(), id="no offset"),
        pytest.param(base64.urlsafe_b64encode(b"offset:3").decode(), id="another adapter's cursor"),
        pytest.param("%%%", id="not base64"),
    ],
)
async def test_a_cursor_this_adapter_did_not_issue_is_refused_before_any_request(
    cursor: str,
) -> None:
    requests: list[httpx.Request] = []
    adapter = _replay_adapter(requests)

    with pytest.raises(PermanentSourceError):
        await adapter.list_patients(cursor, page_size=2)

    assert requests == []


async def test_only_a_few_snapshots_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fhir_r4, "CHANGES_PAGE_SIZE", 3)
    adapter = _replay_adapter([])
    patient_id = next(iter(ReplayTransport().patient_ids))
    first = await adapter.fetch_changes(patient_id, ALL_KINDS, None, None)
    assert first.next_cursor is not None

    for _ in range(fhir_r4.MAX_SNAPSHOTS):
        await adapter.fetch_changes(patient_id, ALL_KINDS, None, None)

    with pytest.raises(RetryableSourceError):
        await adapter.fetch_changes(patient_id, ALL_KINDS, None, first.next_cursor)


async def _all(
    adapter: FhirR4Adapter, patient_id: str, since: datetime | None
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    cursor: str | None = None
    while True:
        page = await adapter.fetch_changes(patient_id, ALL_KINDS, since, cursor)
        records.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return records
