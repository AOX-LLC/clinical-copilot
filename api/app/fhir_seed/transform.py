"""Pure transforms from Synthea transaction bundles to bundles fhir-candle can load.

Synthea writes each patient as a transaction of ``POST`` entries that reference one
another by ``urn:uuid`` and reach practitioners, organizations and locations through
conditional references (``Practitioner?identifier=...``). The local FHIR server wants
client-assigned ids, so every entry becomes ``PUT Type/<synthea-uuid>`` and every
reference becomes ``Type/<id>``. The ids therefore match Synthea's and stay stable
across reloads.

``trim_bundle`` runs once, when the dataset is generated, and drops the resource types
the product never reads (claims, imaging, device records and similar). ``to_put_bundle``
runs on every load. Nothing here logs or echoes resource content.
"""

from collections.abc import Iterable, Mapping
from typing import Any

type Json = dict[str, Any]

# What the adapter reads plus what those resources point at.
KEPT_TYPES = frozenset(
    {
        "Patient",
        "Encounter",
        "Condition",
        "Observation",
        "MedicationRequest",
        "Medication",
        "Procedure",
        "Immunization",
        "AllergyIntolerance",
        "CarePlan",
        "Practitioner",
        "Organization",
        "Location",
    }
)

URN_PREFIX = "urn:uuid:"
_REMOVED = object()


class SeedError(Exception):
    """A bundle could not be transformed or loaded. Messages name types and ids, never content."""


def trim_bundle(bundle: Mapping[str, Any]) -> Json:
    """Drop resource types the product does not read, and any reference to a dropped resource."""
    entries = bundle.get("entry", [])
    dropped_urls = {entry["fullUrl"] for entry in entries if _type_of(entry) not in KEPT_TYPES}
    kept = []
    for entry in entries:
        if _type_of(entry) not in KEPT_TYPES:
            continue
        cleaned = _strip_references_to(entry["resource"], dropped_urls)
        kept.append({**entry, "resource": cleaned})
    return {**bundle, "entry": kept}


def _strip_references_to(node: Any, dropped_urls: set[str]) -> Any:
    """Remove every object whose ``reference`` names a dropped resource, from its parent."""
    if isinstance(node, dict):
        if node.get("reference") in dropped_urls:
            return _REMOVED
        cleaned = {}
        for key, value in node.items():
            stripped = _strip_references_to(value, dropped_urls)
            emptied_by_stripping = isinstance(value, list) and bool(value) and not stripped
            if stripped is not _REMOVED and not emptied_by_stripping:
                cleaned[key] = stripped
        return cleaned
    if isinstance(node, list):
        stripped_items = (_strip_references_to(item, dropped_urls) for item in node)
        return [item for item in stripped_items if item is not _REMOVED]
    return node


class IdentifierIndex:
    """Maps a conditional reference such as ``Practitioner?identifier=sys|value`` to ``Type/id``."""

    def __init__(self) -> None:
        self._by_query: dict[str, str] = {}
        self._types_by_url: dict[str, tuple[str, str]] = {}

    @classmethod
    def from_bundles(cls, bundles: Iterable[Mapping[str, Any]]) -> "IdentifierIndex":
        index = cls()
        for bundle in bundles:
            for entry in bundle.get("entry", []):
                index._add(entry)
        return index

    def _add(self, entry: Mapping[str, Any]) -> None:
        resource = entry["resource"]
        resource_type, resource_id = resource["resourceType"], resource["id"]
        self._types_by_url[entry["fullUrl"]] = (resource_type, resource_id)
        for identifier in resource.get("identifier", []):
            query = f"{resource_type}?identifier={identifier['system']}|{identifier['value']}"
            self._by_query[query] = f"{resource_type}/{resource_id}"

    def find_conditional(self, query: str) -> str | None:
        return self._by_query.get(query)

    def resolve_urn(self, url: str) -> tuple[str, str] | None:
        return self._types_by_url.get(url)


def to_put_bundle(bundle: Mapping[str, Any], shared: IdentifierIndex) -> Json:
    """Rewrite one Synthea transaction as ``PUT Type/<id>`` entries and ``Type/<id>`` references."""
    entries = bundle.get("entry", [])
    local = IdentifierIndex.from_bundles([bundle])
    rewritten = []
    for entry in entries:
        resource = _rewrite_references(entry["resource"], local, shared)
        resource_type, resource_id = resource["resourceType"], resource["id"]
        if entry["fullUrl"] != f"{URN_PREFIX}{resource_id}":
            raise SeedError(f"{resource_type}/{resource_id} does not carry its own id in fullUrl")
        url = f"{resource_type}/{resource_id}"
        rewritten.append(
            {"fullUrl": url, "resource": resource, "request": {"method": "PUT", "url": url}}
        )
    return {"resourceType": "Bundle", "type": "transaction", "entry": rewritten}


def _rewrite_references(node: Any, local: IdentifierIndex, shared: IdentifierIndex) -> Any:
    if isinstance(node, dict):
        rewritten = {}
        for key, value in node.items():
            if key == "reference" and isinstance(value, str):
                rewritten[key] = _rewrite_reference(value, local, shared)
            else:
                rewritten[key] = _rewrite_references(value, local, shared)
        return rewritten
    if isinstance(node, list):
        return [_rewrite_references(item, local, shared) for item in node]
    return node


def _rewrite_reference(reference: str, local: IdentifierIndex, shared: IdentifierIndex) -> str:
    if reference.startswith(URN_PREFIX):
        target = local.resolve_urn(reference) or shared.resolve_urn(reference)
        if target is None:
            raise SeedError("a urn:uuid reference points outside the bundle and the shared data")
        return f"{target[0]}/{target[1]}"
    if "?" in reference:
        resolved = local.find_conditional(reference) or shared.find_conditional(reference)
        if resolved is None:
            resource_type = reference.split("?")[0]
            raise SeedError(f"no resource matches a conditional {resource_type} reference")
        return resolved
    return reference


def _type_of(entry: Mapping[str, Any]) -> str:
    resource_type = entry["resource"]["resourceType"]
    return str(resource_type)
