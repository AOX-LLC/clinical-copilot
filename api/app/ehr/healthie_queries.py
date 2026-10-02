"""Every GraphQL operation the Healthie adapter sends.

They were written from Healthie's public API reference for version 2025-11-30 and are
checked against an excerpt of it in ``tests/test_healthie_queries.py``. The reference pages
each one was written from are listed in ADR 0018. Nothing here has run against a live
Healthie account.

Connection fields cost their page size toward Healthie's query complexity limit of 2000
(rate-limits guide), so pages stay at 50 and nothing nests a connection inside another.
"""

PAGE_SIZE_LIMIT = 50

_USER = "id first_name last_name dob email gender active created_at updated_at"
_MEDICATION = (
    "id user_id name code ndc rxcui route dosage frequency directions comment active "
    "start_date end_date metadata created_at updated_at"
)
_CARE_PLAN = (
    "id name description is_active is_group is_hidden is_template patient { id } "
    "created_at updated_at"
)
_DOCUMENT = "id display_name description rel_user_id include_in_charting metadata created_at"
_PAGE_INFO = "page_info { has_next_page end_cursor }"

USERS_PAGE = (
    "query UsersPage($first: Int, $after: String) "
    f"{{ users(first: $first, after: $after) {{ nodes {{ {_USER} }} {_PAGE_INFO} }} }}"
)
USER_BY_ID = f"query UserById($id: ID) {{ user(id: $id) {{ {_USER} }} }}"

MEDICATIONS_OF_PATIENT = (
    "query MedicationsOfPatient($patient_id: ID) "
    f"{{ medications(patient_id: $patient_id) {{ {_MEDICATION} }} }}"
)
MEDICATION_BY_ID = f"query MedicationById($id: ID) {{ medication(id: $id) {{ {_MEDICATION} }} }}"

CARE_PLANS_PAGE = (
    "query CarePlansPage($patient_id: ID, $first: Int, $after: String) "
    "{ carePlans(patient_id: $patient_id, first: $first, after: $after) "
    f"{{ nodes {{ {_CARE_PLAN} }} {_PAGE_INFO} }} }}"
)
CARE_PLAN_BY_ID = f"query CarePlanById($id: ID) {{ carePlan(id: $id) {{ {_CARE_PLAN} }} }}"

DOCUMENTS_PAGE = (
    "query DocumentsPage($private_user_id: String, $first: Int, $after: String) "
    "{ documents(private_user_id: $private_user_id, first: $first, after: $after) "
    f"{{ nodes {{ {_DOCUMENT} }} {_PAGE_INFO} }} }}"
)
DOCUMENT_BY_ID = f"query DocumentById($id: ID) {{ document(id: $id) {{ {_DOCUMENT} }} }}"

CREATE_DOCUMENT = (
    "mutation CreateDocument($input: createDocumentInput) "
    "{ createDocument(input: $input) "
    f"{{ document {{ {_DOCUMENT} }} messages {{ field message }} }} }}"
)

ALL_OPERATIONS = (
    USERS_PAGE,
    USER_BY_ID,
    MEDICATIONS_OF_PATIENT,
    MEDICATION_BY_ID,
    CARE_PLANS_PAGE,
    CARE_PLAN_BY_ID,
    DOCUMENTS_PAGE,
    DOCUMENT_BY_ID,
    CREATE_DOCUMENT,
)
