"""Serve the recorded FHIR resources over httpx, in the shapes the live server uses.

The fixture holds real fhir-candle responses, one raw resource per line. This transport
answers the reads and searches the adapter makes from that store, so the adapter's HTTP and
parsing paths run offline over the server's own bytes. The live harness runs the same
contract suite against the running server, which is what keeps these shapes honest.
"""

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURE = Path(__file__).parent / "fhir_r4" / "resources.ndjson"
BASE_URL = "http://fhir.recorded/fhir/r4"
KNOWN_TYPES = frozenset(
    {
        "Patient",
        "Encounter",
        "Condition",
        "Observation",
        "MedicationRequest",
        "MedicationStatement",
        "Procedure",
        "Immunization",
        "AllergyIntolerance",
        "CarePlan",
    }
)


class ThrottlingTransport(httpx.AsyncBaseTransport):
    """Wraps a transport; the next request after ``throttle_next_call`` gets a 429."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self._throttle_next = False

    def throttle_next_call(self) -> None:
        self._throttle_next = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._throttle_next:
            self._throttle_next = False
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return await self._inner.handle_async_request(request)


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, fixture: Path = FIXTURE) -> None:
        self._by_key: dict[tuple[str, str], bytes] = {}
        self._by_patient: dict[tuple[str, str], list[bytes]] = {}
        self._patients: list[bytes] = []
        self.patient_ids: list[str] = []
        self.family_names: dict[str, str] = {}
        for line in fixture.read_bytes().splitlines():
            self._add(line)

    def _add(self, line: bytes) -> None:
        resource: dict[str, Any] = json.loads(line)
        key = (resource["resourceType"], resource["id"])
        self._by_key[key] = line
        if key[0] == "Patient":
            self._patients.append(line)
            self.patient_ids.append(key[1])
            self.family_names[key[1]] = resource["name"][0]["family"]
            return
        owner = (resource.get("subject") or resource.get("patient") or {}).get("reference", "")
        self._by_patient.setdefault((key[0], owner.removeprefix("Patient/")), []).append(line)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/fhir/r4").strip("/")
        if path == "metadata":
            return _json_response(b'{"resourceType":"CapabilityStatement"}')
        parts = path.split("/")
        if parts[0] not in KNOWN_TYPES:
            return _not_found()
        if len(parts) == 2:
            line = self._by_key.get((parts[0], parts[1]))
            return _json_response(line) if line is not None else _not_found()
        if parts[0] == "Patient":
            return _json_response(_bundle(self._patients))
        patient = request.url.params.get("patient", "")
        return _json_response(_bundle(self._by_patient.get((parts[0], patient), [])))


def _bundle(resources: list[bytes]) -> bytes:
    entries = b",".join(b'{"resource":' + resource + b"}" for resource in resources)
    head = b'{"resourceType":"Bundle","type":"searchset","total":%d' % len(resources)
    return head + (b',"entry":[' + entries + b"]" if resources else b"") + b"}"


def _json_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/fhir+json"})


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"resourceType": "OperationOutcome"})
