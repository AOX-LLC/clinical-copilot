"""How the practice's supplements and protocols are told apart from ordinary FHIR resources.

Both are plain FHIR R4 resources carrying a category from a code system of this project's own
(ADR 0016): a ``MedicationStatement`` whose ``category`` is ``supplement`` is a supplement
regimen, and a ``CarePlan`` with a ``practice-protocol`` category is a practice protocol.
Everything else keeps its existing kind.
"""

from typing import Any

PRACTICE_CATEGORY_SYSTEM = "https://clinical-copilot.example/fhir/CodeSystem/practice-category"
PROTOCOL_SYSTEM = "https://clinical-copilot.example/fhir/CodeSystem/practice-protocol"
SUPPLEMENT_SYSTEM = "https://clinical-copilot.example/fhir/CodeSystem/supplement"
SUPPLEMENT_CATEGORY = "supplement"
PROTOCOL_CATEGORY = "practice-protocol"


def has_practice_category(codeable: Any, code: str) -> bool:
    """Whether a CodeableConcept carries the practice category ``code``."""
    if not isinstance(codeable, dict):
        return False
    return any(
        isinstance(coding, dict)
        and coding.get("system") == PRACTICE_CATEGORY_SYSTEM
        and coding.get("code") == code
        for coding in codeable.get("coding") or []
    )
