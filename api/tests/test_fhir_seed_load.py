"""Preparing the dataset and loading it into a FHIR server. Synthetic data only."""

import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.fhir_seed import load
from app.fhir_seed.__main__ import MANIFEST_FILE, prepare_dataset
from app.fhir_seed.load import PATIENT_DIRECTORY, SHARED_FILE, load_dataset, patient_files
from app.fhir_seed.transform import SeedError

PRACTITIONER_ID = "22222222-0000-4000-8000-000000000001"
SENTINEL_FAMILY_NAME = "Quillfeather-Sentinel"


def _entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "fullUrl": f"urn:uuid:{resource['id']}",
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"]},
    }


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [_entry(resource) for resource in resources],
    }


def _patient_bundle(number: int) -> dict[str, Any]:
    patient_id = f"11111111-0000-4000-8000-00000000000{number}"
    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"family": SENTINEL_FAMILY_NAME}],
    }
    encounter = {
        "resourceType": "Encounter",
        "id": f"33333333-0000-4000-8000-00000000000{number}",
        "subject": {"reference": f"urn:uuid:{patient_id}"},
        "participant": [
            {"individual": {"reference": "Practitioner?identifier=http://example.test/npi|1"}}
        ],
    }
    claim = {"resourceType": "Claim", "id": f"44444444-0000-4000-8000-00000000000{number}"}
    return _bundle(patient, encounter, claim)


@pytest.fixture
def raw_directory(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    practitioner = {
        "resourceType": "Practitioner",
        "id": PRACTITIONER_ID,
        "identifier": [{"system": "http://example.test/npi", "value": "1"}],
    }
    (raw / "practitionerInformation111.json").write_text(json.dumps(_bundle(practitioner)))
    for number in (1, 2):
        name = f"Test{number}_{SENTINEL_FAMILY_NAME}_id.json"
        (raw / name).write_text(json.dumps(_patient_bundle(number)))
    return raw


@pytest.fixture
def dataset(raw_directory: Path, tmp_path: Path) -> Path:
    prepared = tmp_path / "dataset"
    prepare_dataset(raw_directory, prepared)
    return prepared


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(load, "READY_POLL_SECONDS", 0.0)


class FakeServer:
    """A stand-in FHIR server that records every transaction it receives."""

    def __init__(self, post_status: Callable[[int], int] = lambda _call: 200) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.patients = 0
        self._post_status = post_status
        self.post_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        if request.method == "GET":
            return httpx.Response(200, json={"resourceType": "Bundle", "total": self.patients})
        self.post_calls += 1
        status = self._post_status(self.post_calls)
        if status != 200:
            return httpx.Response(status, json={"resourceType": "OperationOutcome"})
        bundle = json.loads(request.content)
        self.transactions.append(bundle)
        self.patients += sum(e["resource"]["resourceType"] == "Patient" for e in bundle["entry"])
        entries = [{"response": {"status": "201 Created"}} for _ in bundle["entry"]]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": entries})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://fhir.test/fhir/r4", transport=httpx.MockTransport(self.handler)
        )


def _read(path: Path) -> bytes:
    with gzip.open(path, "rb") as stream:
        return stream.read()


def test_prepare_writes_trimmed_gzip_files_named_by_patient_id(dataset: Path) -> None:
    names = [path.name for path in patient_files(dataset)]
    assert names == [
        "11111111-0000-4000-8000-000000000001.json.gz",
        "11111111-0000-4000-8000-000000000002.json.gz",
    ]
    types = [
        e["resource"]["resourceType"] for e in json.loads(_read(patient_files(dataset)[0]))["entry"]
    ]
    assert types == ["Patient", "Encounter"]
    shared = json.loads(_read(dataset / SHARED_FILE))
    assert [e["resource"]["resourceType"] for e in shared["entry"]] == ["Practitioner"]


def test_prepare_is_reproducible_and_the_manifest_hashes_decompressed_content(
    raw_directory: Path, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"

    prepare_dataset(raw_directory, first)
    prepare_dataset(raw_directory, second)

    assert (first / MANIFEST_FILE).read_text() == (second / MANIFEST_FILE).read_text()
    for path in (first / PATIENT_DIRECTORY).glob("*.json.gz"):
        assert path.read_bytes() == (second / PATIENT_DIRECTORY / path.name).read_bytes()
    assert len((first / MANIFEST_FILE).read_text().splitlines()) == 3


def test_prepare_refuses_a_directory_without_shared_and_patient_bundles(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SeedError, match="shared and patient bundles"):
        prepare_dataset(empty, tmp_path / "out")


async def test_the_shared_bundle_loads_first_then_each_patient_as_one_put_transaction(
    dataset: Path,
) -> None:
    server = FakeServer()

    async with server.client() as client:
        summary = await load_dataset(client, dataset)

    assert (summary.patients, summary.resources) == (2, 5)
    first_urls = [e["request"]["url"] for e in server.transactions[0]["entry"]]
    assert first_urls == [f"Practitioner/{PRACTITIONER_ID}"]
    for transaction in server.transactions:
        assert transaction["type"] == "transaction"
        assert {e["request"]["method"] for e in transaction["entry"]} == {"PUT"}
    encounter = server.transactions[1]["entry"][1]["resource"]
    assert encounter["participant"][0]["individual"] == {
        "reference": f"Practitioner/{PRACTITIONER_ID}"
    }


async def test_a_server_error_is_retried_and_the_load_still_succeeds(dataset: Path) -> None:
    server = FakeServer(post_status=lambda call: 503 if call == 2 else 200)

    async with server.client() as client:
        summary = await load_dataset(client, dataset)

    assert summary.patients == 2
    assert server.post_calls == 4


async def test_a_refused_transaction_is_not_retried_and_names_status_and_file(
    dataset: Path,
) -> None:
    server = FakeServer(post_status=lambda call: 422 if call == 2 else 200)

    async with server.client() as client:
        with pytest.raises(SeedError) as raised:
            await load_dataset(client, dataset)

    assert server.post_calls == 2
    assert "422" in str(raised.value)
    assert "11111111-0000-4000-8000-000000000001" in str(raised.value)
    assert SENTINEL_FAMILY_NAME not in str(raised.value)


async def test_the_load_gives_up_after_the_attempt_limit(dataset: Path) -> None:
    server = FakeServer(post_status=lambda _call: 503)

    async with server.client() as client:
        with pytest.raises(SeedError, match="gave up after"):
            await load_dataset(client, dataset)

    assert server.post_calls == load.MAX_ATTEMPTS


async def test_entries_the_server_did_not_store_fail_the_load(dataset: Path) -> None:
    def partial(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"total": 99})
        entries = [{"response": {"status": "400 Bad Request"}}]
        return httpx.Response(200, json={"entry": entries})

    async with httpx.AsyncClient(
        base_url="http://fhir.test/fhir/r4", transport=httpx.MockTransport(partial)
    ) as client:
        with pytest.raises(SeedError, match="were not stored"):
            await load_dataset(client, dataset)


async def test_a_server_that_holds_fewer_patients_than_loaded_fails_the_load(
    dataset: Path,
) -> None:
    server = FakeServer()
    original = server.handler

    def lose_patients(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "GET" and not request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"total": 1})
        return response

    async with httpx.AsyncClient(
        base_url="http://fhir.test/fhir/r4", transport=httpx.MockTransport(lose_patients)
    ) as client:
        with pytest.raises(SeedError, match="expected at least 2 patients"):
            await load_dataset(client, dataset)


async def test_an_empty_dataset_directory_is_refused(tmp_path: Path) -> None:
    raw = tmp_path / "dataset"
    (raw / PATIENT_DIRECTORY).mkdir(parents=True)
    with gzip.open(raw / SHARED_FILE, "wb") as stream:
        stream.write(json.dumps(_bundle()).encode())

    async with FakeServer().client() as client:
        with pytest.raises(SeedError, match="no patient bundles"):
            await load_dataset(client, raw)
