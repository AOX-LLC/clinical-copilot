"""Healthie adapter behavior beyond the contract suite, over hand-built synthetic fixtures."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.ehr import healthie_queries
from app.ehr.healthie import (
    HEALTHIE_API_VERSION,
    PRODUCTION_ENDPOINT,
    SANDBOX_ENDPOINT,
    HealthieAdapter,
    HealthieConfig,
    sign_webhook,
)
from app.ehr.ports import (
    ApprovedSummaryDocument,
    ChangeNotification,
    OperationNotSupportedError,
    PermanentSourceError,
    RateLimitedError,
    RecordKind,
    RecordNotFoundError,
    RetryableSourceError,
    SignatureInvalidError,
    SourceRecord,
    SourceSystemRef,
)
from app.timeline.vocabulary import SourceKind
from tests.recorded.healthie_server import API_KEY, ENDPOINT, FixtureHealthie

SOURCE = SourceSystemRef(code="healthie-fixture", kind=SourceKind.HEALTHIE)
SECRET = "whsec_synthetic-test-secret"
PATH = "/webhooks/healthie"


def make(
    transport: httpx.AsyncBaseTransport | None = None, **overrides: object
) -> tuple[HealthieAdapter, FixtureHealthie]:
    fixture = transport if isinstance(transport, FixtureHealthie) else FixtureHealthie()
    config = HealthieConfig(
        endpoint=ENDPOINT,
        api_key=API_KEY,
        webhook_secret=SECRET,
        webhook_path=PATH,
        allow_custom_endpoint=True,
        **overrides,  # type: ignore[arg-type]
    )
    client = httpx.AsyncClient(transport=transport or fixture)
    return HealthieAdapter(SOURCE, client, config), fixture


def signed(
    adapter_config_path: str, body: bytes, *, secret: str = SECRET, query: str = ""
) -> dict[str, str]:
    return sign_webhook(
        HealthieConfig(
            ENDPOINT,
            API_KEY,
            webhook_secret=secret,
            webhook_path=adapter_config_path,
            allow_custom_endpoint=True,
            webhook_query=query,
        ),
        body,
    )


def document(patient_id: str = "9001") -> ApprovedSummaryDocument:
    return ApprovedSummaryDocument(
        summary_id=uuid4(),
        revision_sha256=bytes(32),
        patient_external_id=patient_id,
        approved_by=uuid4(),
        approved_at=datetime(2026, 9, 30, 15, 0, tzinfo=UTC),
        body="Synthetic approved summary.",
    )


def test_the_endpoints_and_version_are_the_documented_ones() -> None:
    assert PRODUCTION_ENDPOINT == "https://api.gethealthie.com/graphql"
    assert SANDBOX_ENDPOINT == "https://staging-api.gethealthie.com/graphql"
    assert HEALTHIE_API_VERSION == "2025-11-30"


def test_the_config_repr_hides_its_secrets() -> None:
    text = repr(
        HealthieConfig(
            ENDPOINT, API_KEY, webhook_secret=SECRET, webhook_path=PATH, allow_custom_endpoint=True
        )
    )

    assert API_KEY not in text
    assert SECRET not in text


async def test_every_request_carries_the_documented_headers() -> None:
    adapter, fixture = make()

    await adapter.list_patients(None, 2)

    sent = fixture.requests[0]
    assert sent.method == "POST"
    assert sent.headers["Authorization"] == f"Basic {API_KEY}"
    assert sent.headers["AuthorizationSource"] == "API"
    assert sent.headers["Healthie-GraphQL-API-Version"] == "2025-11-30"
    assert sent.headers["Content-Type"] == "application/json"


async def test_a_refused_key_is_a_permanent_error() -> None:
    fixture = FixtureHealthie()
    client = httpx.AsyncClient(transport=fixture)
    adapter = HealthieAdapter(
        SOURCE, client, HealthieConfig(ENDPOINT, "wrong-key", allow_custom_endpoint=True)
    )

    with pytest.raises(PermanentSourceError):
        await adapter.list_patients(None, 2)


async def test_capabilities_say_what_healthie_lacks() -> None:
    adapter, _ = make()

    capabilities = await adapter.capabilities()

    assert capabilities.record_kinds == {
        RecordKind.PATIENT,
        RecordKind.MEDICATION,
        RecordKind.CARE_PLAN,
    }
    assert not capabilities.supports_versions
    assert not capabilities.supports_since
    assert capabilities.supports_notifications
    assert not capabilities.supports_write_back


async def test_a_throttle_error_in_the_body_is_a_rate_limit() -> None:
    body = {
        "errors": [
            {
                "message": "Too many requests. Please try again later.",
                "extensions": {"code": "TOO_MANY_REQUESTS"},
            }
        ]
    }
    adapter, _ = make(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, json=body, headers={"Retry-After": "7"})
        )
    )

    with pytest.raises(RateLimitedError) as raised:
        await adapter.list_patients(None, 2)
    assert raised.value.retry_after_seconds == 7.0


async def test_other_graphql_errors_are_permanent_and_never_echo_their_message() -> None:
    body = {
        "errors": [{"message": "Quillfeather-Healthie leaked", "extensions": {"code": "SOME_CODE"}}]
    }
    adapter, _ = make(httpx.MockTransport(lambda _request: httpx.Response(200, json=body)))

    with pytest.raises(PermanentSourceError) as raised:
        await adapter.list_patients(None, 2)
    assert "Quillfeather" not in str(raised.value)
    assert "SOME_CODE" in str(raised.value)


@pytest.mark.parametrize("status", [500, 503])
async def test_a_server_error_is_retryable(status: int) -> None:
    adapter, _ = make(httpx.MockTransport(lambda _request: httpx.Response(status)))

    with pytest.raises(RetryableSourceError):
        await adapter.list_patients(None, 2)


async def test_a_dropped_connection_is_retryable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter, _ = make(httpx.MockTransport(refuse))

    with pytest.raises(RetryableSourceError):
        await adapter.list_patients(None, 2)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"data": null}', b'{"data": {"users": 3}}'])
async def test_an_unexpected_response_is_permanent(body: bytes) -> None:
    adapter, _ = make(httpx.MockTransport(lambda _request: httpx.Response(200, content=body)))

    with pytest.raises(PermanentSourceError):
        await adapter.list_patients(None, 2)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01T10:15:00-05:00", datetime.fromisoformat("2026-03-01T10:15:00-05:00")),
        ("2026-02-20 08:00:00 -0500", datetime.fromisoformat("2026-02-20T08:00:00-05:00")),
    ],
)
async def test_both_timestamp_shapes_become_aware_datetimes(value: str, expected: datetime) -> None:
    world = FixtureHealthie()
    world.world["users"][0]["updated_at"] = value
    adapter, _ = make(world)

    record = await adapter.get_record("User", "9001")

    assert record.source_updated_at == expected


@pytest.mark.parametrize("value", ["2026-03-01T10:15:00", "yesterday", "2026-03-01"])
async def test_a_timestamp_without_a_zone_is_refused(value: str) -> None:
    world = FixtureHealthie()
    world.world["users"][0]["updated_at"] = value
    adapter, _ = make(world)

    with pytest.raises(PermanentSourceError):
        await adapter.get_record("User", "9001")


async def test_a_record_without_updated_at_has_no_source_time() -> None:
    world = FixtureHealthie()
    world.world["users"][0]["updated_at"] = None
    adapter, _ = make(world)

    assert (await adapter.get_record("User", "9001")).source_updated_at is None


async def test_the_version_is_not_a_thing_healthie_offers() -> None:
    adapter, _ = make()

    with pytest.raises(OperationNotSupportedError):
        await adapter.get_record("User", "9001", "2")


@pytest.mark.parametrize("resource_id", ["", "9001; drop", "../9001", '9001"', "x" * 65, "9 001"])
async def test_an_id_that_is_not_an_id_never_reaches_the_source(resource_id: str) -> None:
    adapter, fixture = make()

    with pytest.raises(RecordNotFoundError):
        await adapter.get_record("User", resource_id)
    with pytest.raises(RecordNotFoundError):
        await adapter.fetch_changes(resource_id, frozenset(RecordKind), None, None)
    assert fixture.requests == []


async def test_a_type_the_adapter_does_not_read_is_not_found() -> None:
    adapter, fixture = make()

    with pytest.raises(RecordNotFoundError):
        await adapter.get_record("Appointment", "1")
    assert fixture.requests == []


@pytest.mark.parametrize(
    "cursor", ["!!!", "e30=", "eyJrIjogMSwgImEiOiBudWxsfQ==", "eyJrIjogbnVsbCwgImEiOiAiIn0="]
)
async def test_a_cursor_that_is_not_ours_is_refused_before_any_request(cursor: str) -> None:
    adapter, fixture = make()

    with pytest.raises(PermanentSourceError):
        await adapter.list_patients(cursor, 2)
    assert fixture.requests == []


async def test_a_cursor_for_a_kind_that_was_not_asked_for_is_refused() -> None:
    adapter, _ = make()
    first = await adapter.fetch_changes("9001", frozenset(RecordKind), None, None)
    assert first.next_cursor is not None

    with pytest.raises(PermanentSourceError):
        await adapter.fetch_changes(
            "9001", frozenset({RecordKind.PATIENT}), None, first.next_cursor
        )


async def test_care_plans_page_through_a_real_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthie_queries, "PAGE_SIZE_LIMIT", 1)
    adapter, _ = make()
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(10):
        page = await adapter.fetch_changes("9001", frozenset({RecordKind.CARE_PLAN}), None, cursor)
        seen.extend(record.resource_id for record in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == ["8201", "8202"]


async def test_a_source_that_pages_without_moving_forward_is_an_error() -> None:
    stuck = {
        "data": {"users": {"nodes": [], "page_info": {"has_next_page": True, "end_cursor": None}}}
    }
    adapter, _ = make(httpx.MockTransport(lambda _request: httpx.Response(200, json=stuck)))

    with pytest.raises(PermanentSourceError):
        await adapter.list_patients(None, 2)


async def test_other_kinds_than_the_three_it_reads_return_nothing() -> None:
    adapter, fixture = make()

    page = await adapter.fetch_changes("9001", frozenset({RecordKind.ENCOUNTER}), None, None)

    assert page.items == ()
    assert page.next_cursor is None
    assert fixture.requests == []


async def test_a_patient_without_records_gives_empty_pages_not_errors() -> None:
    adapter, _ = make()
    cursor: str | None = None
    records: list[SourceRecord] = []

    for _ in range(10):
        page = await adapter.fetch_changes("9003", frozenset(RecordKind), None, cursor)
        records.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert [r.resource_type for r in records] == ["User"]


async def test_the_content_hash_ignores_updated_at_but_not_content() -> None:
    world = FixtureHealthie()
    adapter, _ = make(world)
    before = await adapter.get_record("Medication", "7101")

    world.world["medications"][0]["updated_at"] = "2026-09-01T00:00:00-04:00"
    touched = await adapter.get_record("Medication", "7101")
    world.world["medications"][0]["dosage"] = "10000 IU"
    changed = await adapter.get_record("Medication", "7101")

    assert touched.content_sha256 == before.content_sha256
    assert touched.source_updated_at != before.source_updated_at
    assert changed.content_sha256 != before.content_sha256


# -- webhooks ---------------------------------------------------------------------------------


def event(event_type: str, resource_id: object = "7101") -> bytes:
    return json.dumps(
        {
            "resource_id": resource_id,
            "resource_id_type": "User" if event_type.startswith("patient.") else "Whatever",
            "event_type": event_type,
        }
    ).encode()


def test_the_signature_follows_the_documented_construction() -> None:
    body = event("medication.updated")
    digest = hashlib.sha256(body).hexdigest()
    # "post <path> <query> <digest> <content-type> <length>", with an empty query between two spaces
    string = f"post {PATH}  {digest} application/json {len(body)}"
    expected = hmac.new(SECRET.encode(), string.encode(), hashlib.sha256).hexdigest()

    headers = signed(PATH, body)

    assert headers == {"Content-Digest": f"SHA-256={digest}", "Signature": f"sig1={expected}"}


@pytest.mark.parametrize(
    ("event_type", "resource_type"),
    [
        ("patient.updated", "User"),
        ("patient.created", "User"),
        ("medication.created", "Medication"),
        ("medication.deleted", "Medication"),
        ("care_plan.activated", "CarePlan"),
    ],
)
async def test_an_event_names_the_record_to_read_again(event_type: str, resource_type: str) -> None:
    adapter, _ = make()
    body = event(event_type, "123")

    changes = adapter.parse_notification(signed(PATH, body), body)

    assert changes == [ChangeNotification(resource_type, "123", event_type)]


@pytest.mark.parametrize(
    "event_type", ["appointment.created", "entry.created", "lab_result.updated", "x"]
)
async def test_an_event_for_something_else_is_ignored(event_type: str) -> None:
    adapter, _ = make()
    body = event(event_type)

    assert adapter.parse_notification(signed(PATH, body), body) == []


async def test_a_numeric_resource_id_is_accepted() -> None:
    adapter, _ = make()
    body = event("patient.updated", 4242)

    [change] = adapter.parse_notification(signed(PATH, body), body)

    assert change.resource_id == "4242"


@pytest.mark.parametrize(
    "tamper",
    [
        "body",
        "path",
        "query",
        "secret",
        "digest only",
        "signature only",
        "no headers",
        "short signature",
        "uppercase hex",
    ],
)
async def test_any_change_to_what_was_signed_is_refused(tamper: str) -> None:
    adapter, _ = make()
    body = event("medication.updated")
    headers = signed(PATH, body)
    match tamper:
        case "body":
            body = event("medication.updated", "8")
        case "path":
            headers = signed("/webhooks/other", body)
        case "query":
            headers = signed(PATH, body, query="x=1")
        case "secret":
            headers = signed(PATH, body, secret="whsec_other-synthetic-secret")
        case "digest only":
            headers = {"Content-Digest": headers["Content-Digest"]}
        case "signature only":
            headers = {"Signature": headers["Signature"]}
        case "no headers":
            headers = {}
        case "short signature":
            headers = {**headers, "Signature": headers["Signature"][:-2]}
        case "uppercase hex":
            headers = {**headers, "Signature": "sig1=" + headers["Signature"][5:].upper()}

    with pytest.raises(SignatureInvalidError):
        adapter.parse_notification(headers, body)


async def test_header_names_are_not_case_sensitive() -> None:
    adapter, _ = make()
    body = event("patient.updated")
    headers = {name.lower(): value for name, value in signed(PATH, body).items()}

    assert adapter.parse_notification(headers, body)


async def test_without_a_secret_notifications_are_unsupported() -> None:
    adapter = HealthieAdapter(
        SOURCE,
        httpx.AsyncClient(transport=FixtureHealthie()),
        HealthieConfig(ENDPOINT, API_KEY, allow_custom_endpoint=True),
    )

    assert not (await adapter.capabilities()).supports_notifications
    with pytest.raises(OperationNotSupportedError):
        adapter.parse_notification({}, b"{}")


async def test_a_replayed_notification_does_the_same_thing_twice() -> None:
    adapter, fixture = make()
    body = event("medication.updated", "7101")
    headers = signed(PATH, body)

    first = await adapter.refetch(adapter.parse_notification(headers, body))
    second = await adapter.refetch(adapter.parse_notification(headers, body))

    assert [r.record.content_sha256 for r in first if r.record] == [
        r.record.content_sha256 for r in second if r.record
    ]
    assert fixture.world["documents"] == []  # reading changed nothing at the source


async def test_refetch_reads_each_named_record_once_and_reports_what_is_gone() -> None:
    adapter, fixture = make()
    changes = [
        ChangeNotification("Medication", "7101", "medication.updated"),
        ChangeNotification("Medication", "7101", "medication.updated"),
        ChangeNotification("Medication", "7999", "medication.deleted"),
    ]

    result = await adapter.refetch(changes)

    assert [(r.change.resource_id, r.record is not None) for r in result] == [
        ("7101", True),
        ("7999", False),
    ]
    assert len(fixture.requests) == 2


# -- write-back -------------------------------------------------------------------------------


async def test_write_back_is_off_unless_it_is_switched_on() -> None:
    adapter, fixture = make()

    with pytest.raises(OperationNotSupportedError):
        await adapter.write_back(document(), "key-1")
    assert fixture.requests == []


async def test_write_back_creates_one_chart_document_per_key() -> None:
    adapter, fixture = make(write_back_enabled=True)
    summary = document()

    first = await adapter.write_back(summary, "key-1")
    again = await adapter.write_back(summary, "key-1")
    other = await adapter.write_back(summary, "key-2")

    assert again == first
    assert other.resource_id != first.resource_id
    created = fixture.world["documents"]
    assert len(created) == 2
    assert created[0]["include_in_charting"] is True
    assert created[0]["rel_user_id"] == "9001"
    assert json.loads(created[0]["metadata"])["idempotency_key"] == "key-1"
    assert json.loads(created[0]["metadata"])["revision_sha256"] == bytes(32).hex()


async def test_write_back_looks_only_at_the_patients_own_documents() -> None:
    adapter, fixture = make(write_back_enabled=True)
    summary = document()
    await adapter.write_back(summary, "key-1")

    elsewhere = await adapter.write_back(document("9002"), "key-1")

    assert len(fixture.world["documents"]) == 2
    assert elsewhere.resource_id != "6000"


@pytest.mark.parametrize("key", ["", "k" * 201])
async def test_write_back_refuses_an_unusable_key(key: str) -> None:
    adapter, _ = make(write_back_enabled=True)

    with pytest.raises(ValueError, match="idempotency key"):
        await adapter.write_back(document(), key)


async def test_write_back_refuses_a_patient_id_that_is_not_an_id() -> None:
    adapter, fixture = make(write_back_enabled=True)

    with pytest.raises(RecordNotFoundError):
        await adapter.write_back(document("9001; x"), "key-1")
    assert fixture.requests == []


async def test_a_rejected_document_is_permanent_and_does_not_echo_the_validation_text() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        data: dict[str, Any]
        if "createDocument" in query:
            data = {
                "createDocument": {
                    "document": None,
                    "messages": [{"field": "file_string", "message": "Quillfeather"}],
                }
            }
        else:
            data = {
                "documents": {
                    "nodes": [],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                }
            }
        return httpx.Response(200, json={"data": data})

    adapter, _ = make(httpx.MockTransport(respond), write_back_enabled=True)

    with pytest.raises(PermanentSourceError) as raised:
        await adapter.write_back(document(), "key-1")
    assert "Quillfeather" not in str(raised.value)


async def test_write_back_will_not_guess_when_it_cannot_rule_out_an_earlier_write() -> None:
    always_more = {
        "data": {
            "documents": {"nodes": [], "page_info": {"has_next_page": True, "end_cursor": "c9"}}
        }
    }
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(json.dumps(always_more))
        body["data"]["documents"]["page_info"]["end_cursor"] = f"c{calls}"
        return httpx.Response(200, json=body)

    adapter, _ = make(httpx.MockTransport(respond), write_back_enabled=True)

    with pytest.raises(PermanentSourceError, match="earlier write"):
        await adapter.write_back(document(), "key-1")


@pytest.mark.parametrize("secret", ["", "short", "x" * 15])
def test_a_webhook_secret_too_short_to_sign_anything_is_refused(secret: str) -> None:
    with pytest.raises(ValueError, match="webhook secret"):
        HealthieConfig(
            ENDPOINT, API_KEY, webhook_secret=secret, webhook_path=PATH, allow_custom_endpoint=True
        )


@pytest.mark.parametrize("path", ["", "webhooks/healthie"])
def test_a_webhook_secret_needs_the_path_that_was_signed(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        HealthieConfig(
            ENDPOINT, API_KEY, webhook_secret=SECRET, webhook_path=path, allow_custom_endpoint=True
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.gethealthie.com/graphql",
        "https://healthie.fixture.invalid/graphql",  # not Healthie's, and not allowed
        "http://169.254.169.254/latest",
        "",
    ],
)
def test_the_api_key_is_only_sent_to_healthies_own_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        HealthieConfig(endpoint, API_KEY)


def test_a_custom_endpoint_still_has_to_be_https() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        HealthieConfig("http://localhost/graphql", API_KEY, allow_custom_endpoint=True)


def test_healthies_two_endpoints_are_accepted() -> None:
    HealthieConfig(PRODUCTION_ENDPOINT, API_KEY)
    HealthieConfig(SANDBOX_ENDPOINT, API_KEY)


def test_an_empty_api_key_is_refused() -> None:
    with pytest.raises(ValueError, match="API key"):
        HealthieConfig(PRODUCTION_ENDPOINT, "")


async def test_an_event_we_ignore_is_ignored_whatever_its_id() -> None:
    adapter, _ = make()
    body = event("appointment.created", "not an id!")

    assert adapter.parse_notification(signed(PATH, body), body) == []


async def test_a_patient_event_that_names_another_record_type_is_refused() -> None:
    adapter, _ = make()
    body = json.dumps(
        {"resource_id": "1", "resource_id_type": "Appointment", "event_type": "patient.updated"}
    ).encode()

    with pytest.raises(PermanentSourceError, match="does not match"):
        adapter.parse_notification(signed(PATH, body), body)


async def test_a_hostile_cursor_is_a_typed_error_not_a_crash() -> None:
    adapter, fixture = make()
    deep = base64.urlsafe_b64encode(b"[" * 200_000).decode()
    long = base64.urlsafe_b64encode(b"x" * 100_000).decode()

    for cursor in (deep, long):
        with pytest.raises(PermanentSourceError):
            await adapter.list_patients(cursor, 2)
    assert fixture.requests == []


async def test_a_response_that_cannot_be_decoded_is_a_typed_error() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"this is not gzip data"
        )

    adapter, _ = make(httpx.MockTransport(respond))

    with pytest.raises(PermanentSourceError, match="could not be read"):
        await adapter.list_patients(None, 2)
