import json

import pytest

from app.timeline.canonical import CanonicalizationError, canonical_json, content_sha256
from app.timeline.vocabulary import SourceKind

OBSERVATION = {
    "resourceType": "Observation",
    "id": "obs-1",
    "status": "final",
    "valueQuantity": {"value": 5.4, "unit": "%"},
}


def _payload(document: object, **dump_options: object) -> bytes:
    return json.dumps(document, **dump_options).encode("utf-8")  # type: ignore[arg-type]


def test_key_order_and_whitespace_do_not_change_the_hash() -> None:
    compact = _payload(OBSERVATION, separators=(",", ":"))
    pretty_reversed = _payload(dict(reversed(list(OBSERVATION.items()))), indent=4)

    assert content_sha256(compact, SourceKind.FHIR_R4) == content_sha256(
        pretty_reversed, SourceKind.FHIR_R4
    )


def test_canonical_form_is_sorted_and_compact() -> None:
    canonical = canonical_json(b'{ "b": 1, "a": [true, null, "x"] }', SourceKind.LAB_FEED)

    assert canonical == b'{"a":[true,null,"x"],"b":1}'


def test_fhir_server_assigned_meta_is_ignored() -> None:
    first_load = {**OBSERVATION, "meta": {"versionId": "3", "lastUpdated": "2026-01-02T10:00:00Z"}}
    after_reset = {
        **OBSERVATION,
        "meta": {"versionId": "1", "lastUpdated": "2026-09-30T08:00:00Z", "source": "#reload"},
    }

    assert content_sha256(_payload(first_load), SourceKind.FHIR_R4) == content_sha256(
        _payload(after_reset), SourceKind.FHIR_R4
    )


def test_emptied_meta_is_dropped_but_other_meta_is_kept() -> None:
    only_server_meta = {**OBSERVATION, "meta": {"versionId": "1"}}
    with_profile = {**OBSERVATION, "meta": {"versionId": "1", "profile": ["http://example.org/p"]}}

    assert canonical_json(_payload(only_server_meta), SourceKind.FHIR_R4) == canonical_json(
        _payload(OBSERVATION), SourceKind.FHIR_R4
    )
    assert b'"meta":{"profile":["http://example.org/p"]}' in canonical_json(
        _payload(with_profile), SourceKind.FHIR_R4
    )


def test_only_top_level_meta_is_stripped() -> None:
    contained_meta = {
        **OBSERVATION,
        "contained": [{"resourceType": "X", "meta": {"versionId": "9"}}],
    }

    assert b'"versionId":"9"' in canonical_json(_payload(contained_meta), SourceKind.FHIR_R4)


def test_decimal_precision_is_significant() -> None:
    one_point_zero = b'{"resourceType": "Observation", "valueQuantity": {"value": 1.0}}'
    one = b'{"resourceType": "Observation", "valueQuantity": {"value": 1}}'

    assert content_sha256(one_point_zero, SourceKind.FHIR_R4) != content_sha256(
        one, SourceKind.FHIR_R4
    )
    assert b'"value":1.0' in canonical_json(one_point_zero, SourceKind.FHIR_R4)


def test_healthie_updated_at_is_ignored_and_fhir_rules_do_not_apply() -> None:
    first = {"id": "77", "name": "Magnesium", "updated_at": "2026-01-01 10:00:00 -0500"}
    second = {**first, "updated_at": "2026-02-01 09:00:00 -0500"}
    with_meta = {**first, "meta": {"versionId": "1"}}

    assert content_sha256(_payload(first), SourceKind.HEALTHIE) == content_sha256(
        _payload(second), SourceKind.HEALTHIE
    )
    assert b'"meta"' in canonical_json(_payload(with_meta), SourceKind.HEALTHIE)


def test_lab_feed_payloads_are_hashed_whole() -> None:
    first = {"event_id": "e1", "updated_at": "2026-01-01T00:00:00Z"}
    second = {**first, "updated_at": "2026-01-02T00:00:00Z"}

    assert content_sha256(_payload(first), SourceKind.LAB_FEED) != content_sha256(
        _payload(second), SourceKind.LAB_FEED
    )


def test_non_ascii_text_is_kept_as_utf8() -> None:
    canonical = canonical_json(_payload({"note": "café"}), SourceKind.LAB_FEED)

    assert canonical == '{"note":"café"}'.encode()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b'{"a": 1, "a": 2}', id="duplicate key"),
        pytest.param(b"[1, 2]", id="not an object"),
        pytest.param(b'{"a": NaN}', id="non-standard constant"),
        pytest.param(b'{"a": ', id="truncated"),
        pytest.param(b"\xff\xfe", id="not utf-8"),
        pytest.param('{"a": 1}'.encode("utf-16"), id="utf-16"),
        pytest.param(b'\xef\xbb\xbf{"a": 1}', id="utf-8 byte order mark"),
        pytest.param(b'{"a": "\\ud800"}', id="unpaired surrogate"),
        pytest.param(b'{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}", id="nested too deep"),
    ],
)
def test_ambiguous_or_invalid_payloads_are_rejected(payload: bytes) -> None:
    with pytest.raises(CanonicalizationError):
        content_sha256(payload, SourceKind.FHIR_R4)
