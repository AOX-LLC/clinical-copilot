"""The Healthie adapter.

It does transport and identity only: it reads patients, medications and care plans from
Healthie's GraphQL API and says which record each one is. Normalizers (not built yet) turn
them into timeline rows.

It was written from Healthie's public API reference and has never run against a live
Healthie account (ADR 0018). What Healthie does not offer is declared, not faked:

- There are no version ids, so ``supports_versions`` is false and a record's version is its
  ``updated_at``; identity is the content hash. Reading an exact version raises.
- There is no "changed after" filter on these queries, so ``supports_since`` is false:
  ``fetch_changes`` returns every record of the requested kinds and ingest skips content it
  has seen by hash.
- Healthie has no idempotency key for ``createDocument``. Write-back looks for an earlier
  document carrying the key in its metadata before it creates one. The look-up and the create
  are not atomic across processes.

Webhooks carry ids only. ``parse_notification`` verifies the signature as Healthie documents
it and names the changed records; ``refetch`` reads them again, which is safe to repeat.

Nothing here puts payload content into an exception message, a log line or a ``repr``.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.ehr import healthie_queries as queries
from app.ehr._wire import dump_compact, parse_json_object, read_capped, retry_after
from app.ehr.ports import (
    AdapterCapabilities,
    ApprovedSummaryDocument,
    ChangeNotification,
    OperationNotSupportedError,
    Page,
    PermanentSourceError,
    RateLimitedError,
    RecordKind,
    RecordNotFoundError,
    RetryableSourceError,
    SignatureInvalidError,
    SourcePatient,
    SourceRecord,
    SourceSystemRef,
    WriteBackReceipt,
)

HEALTHIE_API_VERSION = "2025-11-30"
PRODUCTION_ENDPOINT = "https://api.gethealthie.com/graphql"
SANDBOX_ENDPOINT = "https://staging-api.gethealthie.com/graphql"

USER = "User"
MEDICATION = "Medication"
CARE_PLAN = "CarePlan"
DOCUMENT = "Document"

# The order a patient's records are read in; a cursor names the kind it is inside.
_KIND_ORDER = (RecordKind.PATIENT, RecordKind.MEDICATION, RecordKind.CARE_PLAN)
_BY_ID = {
    USER: (queries.USER_BY_ID, "user"),
    MEDICATION: (queries.MEDICATION_BY_ID, "medication"),
    CARE_PLAN: (queries.CARE_PLAN_BY_ID, "carePlan"),
    DOCUMENT: (queries.DOCUMENT_BY_ID, "document"),
}
# Webhook event names start with the resource they are about (event reference page).
_TYPE_OF_EVENT_PREFIX = {"patient": USER, "medication": MEDICATION, "care_plan": CARE_PLAN}

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ERROR_CODE = re.compile(r"[A-Z][A-Z_]{0,39}")
_SIGNATURE_HEADER = re.compile(r"sig1=([0-9a-f]{64})")
_DIGEST_HEADER = re.compile(r"SHA-256=([0-9a-f]{64})")
MIN_WEBHOOK_SECRET_BYTES = 16
_MAX_CURSOR_CHARS = 512
_MAX_LOOKUP_PAGES = 20
_MAX_METADATA_KEY_CHARS = 200
_WEBHOOK_CONTENT_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class HealthieConfig:
    endpoint: str
    api_key: str = field(repr=False)
    api_version: str = HEALTHIE_API_VERSION
    webhook_secret: str | None = field(default=None, repr=False)
    # The path (and query string, usually empty) Healthie posts events to: the signature
    # covers them, so the adapter checks against what it was told to expect.
    webhook_path: str = ""
    webhook_query: str = ""
    write_back_enabled: bool = False
    # Only Healthie's own two endpoints are accepted unless this is set, because the API key goes
    # to whatever the endpoint names. Tests point at a fixture host and set it.
    allow_custom_endpoint: bool = False

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("the Healthie API key is empty")
        if self.endpoint not in (PRODUCTION_ENDPOINT, SANDBOX_ENDPOINT) and not (
            self.allow_custom_endpoint and self.endpoint.startswith("https://")
        ):
            raise ValueError("the endpoint must be one of Healthie's own https endpoints")
        if self.webhook_secret is not None:
            if len(self.webhook_secret.encode()) < MIN_WEBHOOK_SECRET_BYTES:
                raise ValueError("the webhook secret is too short to sign anything")
            if not self.webhook_path.startswith("/"):
                raise ValueError("a webhook secret needs the path Healthie posts to")


@dataclass(frozen=True, slots=True)
class RefetchedRecord:
    change: ChangeNotification
    record: SourceRecord | None  # None: Healthie no longer has it


class HealthieAdapter:
    def __init__(
        self, source: SourceSystemRef, client: httpx.AsyncClient, config: HealthieConfig
    ) -> None:
        self._source = source
        self._client = client
        self._config = config
        self._write_lock = asyncio.Lock()

    @property
    def source(self) -> SourceSystemRef:
        return self._source

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            record_kinds=frozenset(_KIND_ORDER),
            supports_versions=False,
            supports_write_back=self._config.write_back_enabled,
            supports_notifications=self._config.webhook_secret is not None,
            supports_since=False,
        )

    async def list_patients(self, cursor: str | None, page_size: int) -> Page[SourcePatient]:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        after = _decode_cursor(cursor)[1]
        connection = await self._connection(
            queries.USERS_PAGE,
            {"first": min(page_size, queries.PAGE_SIZE_LIMIT), "after": after},
            "users",
            "the patient list",
        )
        patients = [
            SourcePatient(node_id, self._record(USER, node))
            for node in connection.nodes
            for node_id in [_node_id(node)]
        ]
        return Page(tuple(patients), _encode_cursor(None, connection.next_after))

    async def fetch_changes(
        self,
        patient_external_id: str,
        kinds: frozenset[RecordKind],
        since: datetime | None,
        cursor: str | None,
    ) -> Page[SourceRecord]:
        """Every record of the requested kinds; ``since`` is ignored (``supports_since``)."""
        _require_id(USER, patient_external_id)
        order = [kind for kind in _KIND_ORDER if kind in kinds]
        if not order:
            return Page((), None)
        kind_name, after = _decode_cursor(cursor)
        kind = order[0] if kind_name is None else _kind_named(kind_name, order)
        records, next_after = await self._read_kind(kind, patient_external_id, after)
        following = order[order.index(kind) + 1 :]
        if next_after is not None:
            next_cursor: str | None = _encode_cursor(kind.value, next_after)
        elif following:
            next_cursor = _encode_cursor(following[0].value, None)
        else:
            next_cursor = None
        return Page(tuple(records), next_cursor)

    async def get_record(
        self, resource_type: str, resource_id: str, version_id: str | None = None
    ) -> SourceRecord:
        if resource_type not in _BY_ID:
            raise RecordNotFoundError("the adapter does not read that resource type")
        _require_id(resource_type, resource_id)
        if version_id is not None:
            raise OperationNotSupportedError("Healthie has no exact-version reads")
        query, root = _BY_ID[resource_type]
        data = await self._execute(query, {"id": resource_id}, f"{resource_type}/{resource_id}")
        node = data.get(root)
        if node is None:
            raise RecordNotFoundError(f"{resource_type}/{resource_id} not found")
        return self._record(resource_type, _as_node(node))

    async def write_back(
        self, document: ApprovedSummaryDocument, idempotency_key: str
    ) -> WriteBackReceipt:
        """Write an approved summary into the patient's chart as a document (createDocument)."""
        if not self._config.write_back_enabled:
            raise OperationNotSupportedError("write-back is not enabled for this adapter")
        patient_id = document.patient_external_id
        _require_id(USER, patient_id)
        if not idempotency_key or len(idempotency_key) > _MAX_METADATA_KEY_CHARS:
            raise ValueError("the idempotency key must be 1 to 200 characters")
        async with self._write_lock:
            existing = await self._find_written(patient_id, idempotency_key)
            if existing is not None:
                return WriteBackReceipt(DOCUMENT, existing, None)
            created = await self._create_document(document, patient_id, idempotency_key)
            return WriteBackReceipt(DOCUMENT, created, None)

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ChangeNotification]:
        secret = self._config.webhook_secret
        if secret is None:
            raise OperationNotSupportedError("webhook signature checking is not configured")
        self._verify_signature(secret, headers, body)
        event = parse_json_object(body)
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            raise PermanentSourceError("the notification names no event")
        resource_type = _TYPE_OF_EVENT_PREFIX.get(event_type.partition(".")[0])
        if resource_type is None:
            return []  # an event for a resource this adapter does not read, whatever its id
        resource_id = event.get("resource_id")
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id):
            raise PermanentSourceError("the notification names no usable record id")
        # The event reference types patient events as User. A signed event that says otherwise
        # would make this re-read an unrelated record under that id.
        id_type = event.get("resource_id_type")
        if resource_type == USER and id_type is not None and id_type != "User":
            raise PermanentSourceError("the notification's record type does not match its event")
        return [ChangeNotification(resource_type, resource_id, event_type)]

    async def refetch(self, changes: list[ChangeNotification]) -> list[RefetchedRecord]:
        """Read each named record again. Repeating a notification repeats the read, nothing else."""
        refetched: list[RefetchedRecord] = []
        for change in dict.fromkeys(
            (c.resource_type, c.resource_id, c.event_type) for c in changes
        ):
            notification = ChangeNotification(*change)
            try:
                record = await self.get_record(notification.resource_type, notification.resource_id)
            except RecordNotFoundError:
                record = None
            refetched.append(RefetchedRecord(notification, record))
        return refetched

    # -- reading ---------------------------------------------------------------------------

    async def _read_kind(
        self, kind: RecordKind, patient_id: str, after: str | None
    ) -> tuple[list[SourceRecord], str | None]:
        if kind is RecordKind.PATIENT:
            data = await self._execute(queries.USER_BY_ID, {"id": patient_id}, "the patient")
            node = data.get("user")
            if node is None:
                raise RecordNotFoundError(f"{USER}/{patient_id} not found")
            return [self._record(USER, _as_node(node))], None
        if kind is RecordKind.MEDICATION:
            data = await self._execute(
                queries.MEDICATIONS_OF_PATIENT, {"patient_id": patient_id}, "the medications"
            )
            nodes = data.get("medications") or []
            if not isinstance(nodes, list):
                raise PermanentSourceError("the medications came back in an unexpected shape")
            return [self._record(MEDICATION, _as_node(node)) for node in nodes], None
        connection = await self._connection(
            queries.CARE_PLANS_PAGE,
            {"patient_id": patient_id, "first": queries.PAGE_SIZE_LIMIT, "after": after},
            "carePlans",
            "the care plans",
        )
        return [self._record(CARE_PLAN, node) for node in connection.nodes], connection.next_after

    async def _connection(
        self, query: str, variables: Mapping[str, Any], root: str, what: str
    ) -> "_Connection":
        data = await self._execute(query, variables, what)
        connection = data.get(root)
        if not isinstance(connection, dict):
            raise PermanentSourceError(f"{what} came back in an unexpected shape")
        nodes = connection.get("nodes")
        info = connection.get("page_info")
        if not isinstance(nodes, list) or not isinstance(info, dict):
            raise PermanentSourceError(f"{what} came back in an unexpected shape")
        next_after: str | None = None
        if info.get("has_next_page") is True:
            end = info.get("end_cursor")
            if not isinstance(end, str) or not end or end == variables.get("after"):
                # Following a cursor that does not move would loop forever.
                raise PermanentSourceError(f"{what}: the source paged without moving forward")
            next_after = end
        return _Connection([_as_node(node) for node in nodes], next_after)

    async def _execute(self, query: str, variables: Mapping[str, Any], what: str) -> dict[str, Any]:
        try:
            async with self._client.stream(
                "POST",
                self._config.endpoint,
                headers={
                    "Authorization": f"Basic {self._config.api_key}",
                    "AuthorizationSource": "API",
                    "Healthie-GraphQL-API-Version": self._config.api_version,
                    "Content-Type": "application/json",
                },
                content=json.dumps({"query": query, "variables": variables}).encode(),
            ) as response:
                _check_status(response, what)
                body = await read_capped(response, what)
                hint = retry_after(response)
        except httpx.TimeoutException:
            raise RetryableSourceError(f"{what}: the source timed out") from None
        except httpx.TransportError:
            raise RetryableSourceError(f"{what}: the source could not be reached") from None
        except httpx.HTTPError:
            # What is left is a response that could not be read (a corrupt compressed body).
            raise PermanentSourceError(f"{what}: the response could not be read") from None
        document = parse_json_object(body)
        errors = document.get("errors")
        if errors:
            _raise_graphql_errors(errors, what, hint)
        data = document.get("data")
        if not isinstance(data, dict):
            raise PermanentSourceError(f"{what}: the source returned no data")
        return data

    def _record(self, resource_type: str, node: dict[str, Any]) -> SourceRecord:
        return SourceRecord.from_payload(
            self._source,
            resource_type,
            _node_id(node),
            None,
            _timestamp(node.get("updated_at")),
            dump_compact(node).encode("utf-8"),
        )

    # -- write-back ------------------------------------------------------------------------

    async def _find_written(self, patient_id: str, key: str) -> str | None:
        after: str | None = None
        for _ in range(_MAX_LOOKUP_PAGES):
            connection = await self._connection(
                queries.DOCUMENTS_PAGE,
                {"private_user_id": patient_id, "first": queries.PAGE_SIZE_LIMIT, "after": after},
                "documents",
                "the earlier documents",
            )
            for node in connection.nodes:
                if _metadata_key(node.get("metadata")) == key:
                    return _node_id(node)
            if connection.next_after is None:
                return None
            after = connection.next_after
        raise PermanentSourceError("could not rule out an earlier write: too many documents")

    async def _create_document(
        self, document: ApprovedSummaryDocument, patient_id: str, key: str
    ) -> str:
        metadata = {
            "idempotency_key": key,
            "summary_id": str(document.summary_id),
            "revision_sha256": document.revision_sha256.hex(),
        }
        text = base64.b64encode(document.body.encode("utf-8")).decode("ascii")
        data = await self._execute(
            queries.CREATE_DOCUMENT,
            {
                "input": {
                    "rel_user_id": patient_id,
                    "display_name": f"Approved summary {document.summary_id}",
                    "description": "Approved pre-visit summary",
                    "file_string": f"data:text/plain;base64,{text}",
                    "include_in_charting": True,
                    "metadata": json.dumps(metadata, sort_keys=True),
                }
            },
            "the document write",
        )
        result = data.get("createDocument")
        if not isinstance(result, dict):
            raise PermanentSourceError("the document write came back in an unexpected shape")
        messages = result.get("messages")
        if messages:
            count = len(messages) if isinstance(messages, list) else 1
            raise PermanentSourceError(f"the source rejected the document ({count} message(s))")
        created = result.get("document")
        if not isinstance(created, dict):
            raise PermanentSourceError("the source did not return the document it created")
        return _node_id(created)

    # -- webhooks --------------------------------------------------------------------------

    def _verify_signature(self, secret: str, headers: Mapping[str, str], body: bytes) -> None:
        """Check ``Signature`` as Healthie documents it (webhooks guide).

        The signed string is ``post <path> <query> <digest> application/json <length>``, the
        digest is the hex SHA-256 from ``Content-Digest`` (``SHA-256=<hex>``), and the
        signature is the hex HMAC-SHA256 of that string under the webhook secret, sent as
        ``sig1=<hex>``. The guide's sample measures the length on a re-serialized body; this
        uses the bytes received, and also requires the digest to match them.
        """
        lowered = {name.lower(): value for name, value in headers.items()}
        signature = _SIGNATURE_HEADER.fullmatch(lowered.get("signature", ""))
        digest = _DIGEST_HEADER.fullmatch(lowered.get("content-digest", ""))
        if signature is None or digest is None:
            raise SignatureInvalidError("the notification is not signed as expected")
        actual_digest = hashlib.sha256(body).hexdigest()
        signed = " ".join(
            (
                "post",
                self._config.webhook_path,
                self._config.webhook_query,
                digest.group(1),
                _WEBHOOK_CONTENT_TYPE,
                str(len(body)),
            )
        )
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        digest_ok = hmac.compare_digest(digest.group(1), actual_digest)
        signature_ok = hmac.compare_digest(signature.group(1), expected)
        if not (digest_ok and signature_ok):
            raise SignatureInvalidError("the notification signature does not match")


@dataclass(frozen=True, slots=True)
class _Connection:
    nodes: list[dict[str, Any]]
    next_after: str | None


def sign_webhook(config: HealthieConfig, body: bytes) -> dict[str, str]:
    """Headers Healthie would send for ``body``. For tests and the fixture harness."""
    if config.webhook_secret is None:
        raise ValueError("no webhook secret is configured")
    digest = hashlib.sha256(body).hexdigest()
    signed = " ".join(
        (
            "post",
            config.webhook_path,
            config.webhook_query,
            digest,
            _WEBHOOK_CONTENT_TYPE,
            str(len(body)),
        )
    )
    signature = hmac.new(config.webhook_secret.encode(), signed.encode(), hashlib.sha256)
    return {"Content-Digest": f"SHA-256={digest}", "Signature": f"sig1={signature.hexdigest()}"}


def _check_status(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if response.is_success:
        return
    if status == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(
            f"{what}: the source asked the client to slow down", retry_after(response)
        )
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise RetryableSourceError(f"{what}: source error ({status})")
    raise PermanentSourceError(f"{what}: the source refused the request ({status})")


def _raise_graphql_errors(errors: object, what: str, hint: float | None) -> None:
    """Map Healthie's error list to a typed error, naming codes but never messages."""
    entries = errors if isinstance(errors, list) else [errors]
    codes = []
    for entry in entries:
        extensions = entry.get("extensions") if isinstance(entry, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
            codes.append(code)
    if "TOO_MANY_REQUESTS" in codes:
        raise RateLimitedError(f"{what}: the source asked the client to slow down", hint)
    named = f" ({', '.join(sorted(set(codes)))})" if codes else ""
    raise PermanentSourceError(f"{what}: the source reported {len(entries)} error(s){named}")


def _as_node(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermanentSourceError("the source returned a record in an unexpected shape")
    return value


def _node_id(node: Mapping[str, Any]) -> str:
    value = node.get("id")  # JSON numbers arrive as ``Number``, a str, so an int id is a str here
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PermanentSourceError("the source returned a record without a usable id")
    return value


def _require_id(resource_type: str, resource_id: str) -> None:
    """Refuse values that are not ids; no such record can exist, and ids go into queries."""
    if not _ID.fullmatch(resource_id):
        raise RecordNotFoundError(f"{resource_type}: not a Healthie id")


def _timestamp(value: object) -> datetime | None:
    """A Healthie ``updated_at`` as an aware datetime.

    The reference types it ``ISO8601DateTime``; its examples elsewhere show a space and a
    ``+hhmm`` offset, so both are accepted. A value with no offset is refused rather than
    guessed at.
    """
    if value is None:
        return None
    text = str(value)
    for pattern in (None, "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = (
                datetime.fromisoformat(text)
                if pattern is None
                else datetime.strptime(text, pattern)
            )
        except ValueError:
            continue
        if parsed.utcoffset() is None:
            break
        return parsed
    raise PermanentSourceError("the source sent an updated_at that is not a timestamp with a zone")


def _metadata_key(metadata: object) -> str | None:
    if not isinstance(metadata, str):
        return None
    try:
        parsed = json.loads(metadata)
    except (ValueError, RecursionError):
        return None
    key = parsed.get("idempotency_key") if isinstance(parsed, dict) else None
    return key if isinstance(key, str) else None


def _encode_cursor(kind: str | None, after: str | None) -> str | None:
    if kind is None and after is None:
        return None
    return base64.urlsafe_b64encode(json.dumps({"k": kind, "a": after}).encode()).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if cursor is None:
        return None, None
    if len(cursor) > 4 * _MAX_CURSOR_CHARS:
        raise PermanentSourceError("cursor is not one this source issued")
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        kind, after = decoded["k"], decoded["a"]
    except (binascii.Error, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        raise PermanentSourceError("cursor is not one this source issued") from None
    valid_kind = kind is None or (isinstance(kind, str) and len(kind) < 32)
    valid_after = after is None or (isinstance(after, str) and 0 < len(after) <= _MAX_CURSOR_CHARS)
    if not (valid_kind and valid_after):
        raise PermanentSourceError("cursor is not one this source issued")
    return kind, after


def _kind_named(name: str, order: list[RecordKind]) -> RecordKind:
    for kind in order:
        if kind.value == name:
            return kind
    raise PermanentSourceError("cursor is not one this source issued")
