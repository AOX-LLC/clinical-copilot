"""The committed dataset served as a FHIR R4 server would serve it, over an httpx transport.

The real ``FhirR4Adapter`` runs on top of this, so ingest tests exercise its search, paging
and hashing paths over the same resources the seed service loads. Like fhir-candle, the
server stamps every resource with ``meta`` (version 1, a last-updated time), and a different
``stamped_at`` plays a server that was wiped and reloaded: new timestamps, identical content.
"""

import json
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from functools import cache
from typing import Any

import httpx

from app.fhir_seed.load import PATIENT_DIRECTORY, SHARED_FILE, read_bundle
from app.fhir_seed.transform import IdentifierIndex, to_put_bundle
from tests.dataset import DATASET_DIRECTORY

Mutation = Callable[[dict[str, Any]], dict[str, Any]]


@cache
def _stored_resources() -> tuple[tuple[dict[str, Any], ...], ...]:
    """Each patient's resources, rewritten as the loader stores them (``Type/<id>`` references)."""
    shared = read_bundle(DATASET_DIRECTORY / SHARED_FILE)
    index = IdentifierIndex.from_bundles([shared])
    patients = []
    for path in sorted((DATASET_DIRECTORY / PATIENT_DIRECTORY).glob("*.json.gz")):
        bundle = to_put_bundle(read_bundle(path), index)
        patients.append(tuple(entry["resource"] for entry in bundle["entry"]))
    return tuple(patients)


class DatasetFhirTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        stamped_at: datetime = datetime(2026, 10, 2, 14, 0, tzinfo=UTC),
        patient_limit: int | None = None,
        mutations: Mapping[tuple[str, str], Mutation] | None = None,
        omit: Collection[tuple[str, str]] = (),
        failing_types: Collection[str] = (),
    ) -> None:
        # ``omit`` leaves records out, as a source does after they are deleted there;
        # ``failing_types`` makes the search for a type answer with a server error.
        self._failing_types = frozenset(failing_types)
        self._patients: list[bytes] = []
        self._by_key: dict[tuple[str, str], bytes] = {}
        self._by_patient: dict[tuple[str, str], list[bytes]] = {}
        self.patient_ids: list[str] = []
        mutations = mutations or {}
        meta = {"versionId": "1", "lastUpdated": stamped_at.isoformat()}
        for resources in _stored_resources()[:patient_limit]:
            for stored in resources:
                if (stored["resourceType"], stored["id"]) in omit:
                    continue
                resource = json.loads(json.dumps(stored))
                # A server finds a record by what it was stored under; a mutation changes what
                # it says, so it can serve a record that names a different patient.
                owner = _owner_of(resource)
                mutate = mutations.get((resource["resourceType"], resource["id"]))
                if mutate is not None:
                    resource = mutate(resource)
                resource["meta"] = {**resource.get("meta", {}), **meta}
                self._add(resource, owner)

    def _add(self, resource: dict[str, Any], owner: str) -> None:
        body = json.dumps(resource, separators=(",", ":")).encode()
        key = (resource["resourceType"], resource["id"])
        self._by_key[key] = body
        if key[0] == "Patient":
            self._patients.append(body)
            self.patient_ids.append(key[1])
            return
        self._by_patient.setdefault((key[0], owner), []).append(body)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.removeprefix("/fhir/r4").strip("/").split("/")
        if len(parts) == 2:
            body = self._by_key.get((parts[0], parts[1]))
            return _ok(body) if body is not None else httpx.Response(404, json={})
        if parts[0] == "Patient":
            return _ok(_bundle(self._patients))
        if parts[0] in self._failing_types:
            return httpx.Response(500, json={})
        patient = request.url.params.get("patient", "")
        return _ok(_bundle(self._by_patient.get((parts[0], patient), [])))


def _owner_of(resource: dict[str, Any]) -> str:
    reference = (resource.get("subject") or resource.get("patient") or {}).get("reference", "")
    return str(reference).removeprefix("Patient/")


def _bundle(resources: list[bytes]) -> bytes:
    entries = b",".join(b'{"resource":' + resource + b"}" for resource in resources)
    head = b'{"resourceType":"Bundle","type":"searchset","total":%d' % len(resources)
    return head + (b',"entry":[' + entries + b"]" if resources else b"") + b"}"


def _ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/fhir+json"})
