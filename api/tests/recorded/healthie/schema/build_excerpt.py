"""Rebuild the Healthie schema excerpt from Healthie's public API reference.

Run by hand (it needs network access); nothing in the test suite calls it:

    uv run python tests/recorded/healthie/schema/build_excerpt.py          # rewrite the file
    uv run python tests/recorded/healthie/schema/build_excerpt.py --check  # compare, write nothing

The excerpt holds type definitions only (names, fields and types), with Healthie's
descriptions removed, and only the fields the adapter selects. Every field named in
``OBJECTS`` and every argument named in ``ROOTS`` must exist on the reference page, so a
reference that drops or renames one makes this script fail.
"""

import html
import re
import sys
from pathlib import Path

import httpx
from graphql import (
    DocumentNode,
    FieldDefinitionNode,
    InputObjectTypeDefinitionNode,
    InputValueDefinitionNode,
    NameNode,
    ObjectTypeDefinitionNode,
    ScalarTypeDefinitionNode,
    VariableDefinitionNode,
    build_ast_schema,
    parse,
    print_ast,
)
from graphql.language import OperationDefinitionNode, parse_type

API_VERSION = "2025-11-30"
REFERENCE = f"https://docs.gethealthie.com/reference/{API_VERSION}"
TARGET = Path(__file__).with_name(f"healthie-{API_VERSION}.graphql")

SCALARS = ("ISO8601Date", "ISO8601DateTime", "JSON", "Cursor")

# type name -> (reference page, fields kept)
OBJECTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "User": (
        "objects/user",
        (
            "id",
            "first_name",
            "last_name",
            "dob",
            "email",
            "gender",
            "active",
            "created_at",
            "updated_at",
        ),
    ),
    "MedicationType": (
        "objects/medicationtype",
        (
            "id",
            "user_id",
            "name",
            "code",
            "ndc",
            "rxcui",
            "route",
            "dosage",
            "frequency",
            "directions",
            "comment",
            "active",
            "start_date",
            "end_date",
            "metadata",
            "created_at",
            "updated_at",
        ),
    ),
    "CarePlan": (
        "objects/careplan",
        (
            "id",
            "name",
            "description",
            "is_active",
            "is_group",
            "is_hidden",
            "is_template",
            "patient",
            "created_at",
            "updated_at",
        ),
    ),
    "Document": (
        "objects/document",
        (
            "id",
            "display_name",
            "description",
            "rel_user_id",
            "include_in_charting",
            "metadata",
            "created_at",
        ),
    ),
    "PageInfo": ("objects/pageinfo", ("end_cursor", "has_next_page")),
    "CarePlanPaginationConnection": (
        "objects/careplanpaginationconnection",
        ("nodes", "page_info"),
    ),
    "DocumentPaginationConnection": (
        "objects/documentpaginationconnection",
        ("nodes", "page_info"),
    ),
    "FieldError": ("objects/fielderror", ("field", "message")),
    "createDocumentPayload": ("objects/createdocumentpayload", ("document", "messages")),
}

# The users query returns a connection whose type name is read from its page.
USERS_CONNECTION_FIELDS = ("nodes", "page_info")

INPUTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "createDocumentInput": (
        "input-objects/createdocumentinput",
        (
            "rel_user_id",
            "display_name",
            "description",
            "file_string",
            "include_in_charting",
            "metadata",
        ),
    ),
}

# root field -> (reference page, arguments kept)
QUERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "users": ("queries/users", ("after", "first")),
    "user": ("queries/user", ("id",)),
    "medications": ("queries/medications", ("patient_id", "active")),
    "medication": ("queries/medication", ("id",)),
    "carePlans": ("queries/careplans", ("patient_id", "after", "first")),
    "carePlan": ("queries/careplan", ("id",)),
    "documents": ("queries/documents", ("private_user_id", "after", "first")),
    "document": ("queries/document", ("id",)),
}
MUTATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "createDocument": ("mutations/createdocument", ("input",)),
}

HEADER = """\
# An excerpt of Healthie's public API reference, used to check this adapter's queries.
# Source: {reference} (API version {version}).
# Type definitions only, with Healthie's descriptions removed, and only the fields and
# arguments the adapter uses. It is not Healthie's schema and is not complete.
# Rebuild with tests/recorded/healthie/schema/build_excerpt.py.
#
# Source page of each definition:
{sources}
"""


def main() -> int:
    excerpt = build()
    if "--check" in sys.argv:
        if TARGET.read_text() == excerpt:
            print("The committed excerpt matches the reference.")
            return 0
        print("The committed excerpt differs from the reference.", file=sys.stderr)
        return 1
    TARGET.write_text(excerpt)
    print(f"Wrote {TARGET}")
    return 0


def build() -> str:
    client = httpx.Client(timeout=90.0, follow_redirects=False)
    definitions: list[object] = [
        ScalarTypeDefinitionNode(name=NameNode(value=name)) for name in SCALARS
    ]
    sources: list[str] = []

    for name, (page, keep) in OBJECTS.items():
        sources.append(f"#   {name}: {REFERENCE}/{page}")
        definitions.append(_trimmed(_definition(client, page), name, keep))
    for name, (page, keep) in INPUTS.items():
        sources.append(f"#   {name}: {REFERENCE}/{page}")
        definitions.append(_trimmed(_definition(client, page), name, keep))

    query_fields: list[FieldDefinitionNode] = []
    for field, (page, args) in QUERIES.items():
        sources.append(f"#   Query.{field}: {REFERENCE}/{page}")
        text = _page_text(client, page)
        query_fields.append(_root_field(field, text, _definition(client, page), args))
        if field == "users":
            connection = _returns(text).strip("![]")
            sources.append(f"#   {connection}: {REFERENCE}/objects/{connection.lower()}")
            definitions.append(
                _trimmed(
                    _definition(client, f"objects/{connection.lower()}"),
                    connection,
                    USERS_CONNECTION_FIELDS,
                )
            )
    mutation_fields: list[FieldDefinitionNode] = []
    for field, (page, args) in MUTATIONS.items():
        sources.append(f"#   Mutation.{field}: {REFERENCE}/{page}")
        text = _page_text(client, page)
        mutation_fields.append(_root_field(field, text, _definition(client, page), args))

    definitions.append(
        ObjectTypeDefinitionNode(name=NameNode(value="Query"), fields=tuple(query_fields))
    )
    definitions.append(
        ObjectTypeDefinitionNode(name=NameNode(value="Mutation"), fields=tuple(mutation_fields))
    )
    document = DocumentNode(definitions=tuple(definitions))
    body = print_ast(document)
    build_ast_schema(parse(body))  # raises if a referenced type is not in the excerpt
    return HEADER.format(reference=REFERENCE, version=API_VERSION, sources="\n".join(sources)) + (
        "\n" + body + "\n"
    )


def _fetch(client: httpx.Client, page: str) -> str:
    for attempt in range(3):
        try:
            response = client.get(f"{REFERENCE}/{page}")
            response.raise_for_status()
            return response.text
        except httpx.HTTPError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _page_text(client: httpx.Client, page: str) -> str:
    page_html = _fetch(client, page)
    main = re.search(r"<main.*?</main>", page_html, re.S)
    body = re.sub(
        r"<script.*?</script>|<style.*?</style>",
        "",
        main.group(0) if main else page_html,
        flags=re.S,
    )
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


def _definition(client: httpx.Client, page: str) -> str:
    page_html = _fetch(client, page)
    start = page_html.find('id="definition"')
    if start < 0:
        raise SystemExit(f"{page} has no Definition section")
    section = page_html[start:]
    section = section[: section.find("</figure>")]
    lines = re.findall(r'<div class="ec-line">(.*?)</div></div>', section, re.S)
    return "\n".join(html.unescape(re.sub(r"<[^>]+>", "", line)) for line in lines)


def _trimmed(sdl: str, name: str, keep: tuple[str, ...]) -> object:
    node = next(
        d
        for d in parse(sdl).definitions
        if isinstance(d, ObjectTypeDefinitionNode | InputObjectTypeDefinitionNode)
        and d.name.value == name
    )
    by_name = {f.name.value: f for f in node.fields or ()}
    missing = [f for f in keep if f not in by_name]
    if missing:
        raise SystemExit(f"{name} no longer has the fields {missing}")
    fields = tuple(_without_description(by_name[f]) for f in keep)
    if isinstance(node, InputObjectTypeDefinitionNode):
        return InputObjectTypeDefinitionNode(name=node.name, fields=fields)
    return ObjectTypeDefinitionNode(name=node.name, fields=fields)


def _without_description(field: FieldDefinitionNode | InputValueDefinitionNode) -> object:
    if isinstance(field, FieldDefinitionNode):
        return FieldDefinitionNode(name=field.name, type=field.type, arguments=())
    return InputValueDefinitionNode(name=field.name, type=field.type)


def _returns(text: str) -> str:
    match = re.search(r"Returns (\S+) (?:Example|Arguments)", text)
    if match is None:
        raise SystemExit("a reference page no longer says what it returns")
    return match.group(1)


def _root_field(field: str, text: str, example: str, keep: tuple[str, ...]) -> FieldDefinitionNode:
    operation = next(
        d for d in parse(example).definitions if isinstance(d, OperationDefinitionNode)
    )
    declared = {v.variable.name.value: v for v in operation.variable_definitions or ()}
    missing = [a for a in keep if a not in declared]
    if missing:
        raise SystemExit(f"{field} no longer takes the arguments {missing}")
    arguments = tuple(_argument(declared[a]) for a in keep)
    return FieldDefinitionNode(
        name=NameNode(value=field), arguments=arguments, type=parse_type(_returns(text))
    )


def _argument(variable: VariableDefinitionNode) -> InputValueDefinitionNode:
    return InputValueDefinitionNode(name=variable.variable.name, type=variable.type)


if __name__ == "__main__":
    sys.exit(main())
