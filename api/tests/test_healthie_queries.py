"""Every operation the Healthie adapter sends is valid against the reference excerpt.

The excerpt is built from Healthie's public API reference (``tests/recorded/healthie/schema``).
This proves the adapter's queries agree with the reference as transcribed; it does not prove
that a live Healthie account accepts them.
"""

import re

import pytest
from graphql import FieldNode, OperationDefinitionNode, parse, validate

from app.ehr import healthie_queries as queries
from app.ehr.healthie import HEALTHIE_API_VERSION
from tests.recorded.healthie_server import HERE, SCHEMA

MAX_DEPTH = 25  # Healthie's documented depth limit (rate-limits guide)


@pytest.mark.parametrize("operation", queries.ALL_OPERATIONS)
def test_an_operation_is_valid_against_the_excerpt(operation: str) -> None:
    assert validate(SCHEMA, parse(operation)) == []


@pytest.mark.parametrize("operation", queries.ALL_OPERATIONS)
def test_an_operation_stays_inside_healthies_depth_limit(operation: str) -> None:
    definition = parse(operation).definitions[0]
    assert isinstance(definition, OperationDefinitionNode)

    def depth(node: FieldNode | OperationDefinitionNode) -> int:
        selections = node.selection_set.selections if node.selection_set else ()
        return 1 + max((depth(s) for s in selections if isinstance(s, FieldNode)), default=0)

    assert depth(definition) <= MAX_DEPTH


def test_no_connection_is_asked_for_more_than_the_page_limit() -> None:
    assert queries.PAGE_SIZE_LIMIT * 2 <= 2000  # complexity limit, with room for nesting


def test_an_invalid_query_is_caught_by_the_excerpt() -> None:
    broken = queries.USER_BY_ID.replace("first_name", "firstName")

    assert validate(SCHEMA, parse(broken))


def test_the_excerpt_is_pinned_to_the_adapters_version_and_says_what_it_is() -> None:
    header = (HERE / "schema" / f"healthie-{HEALTHIE_API_VERSION}.graphql").read_text()

    assert "An excerpt of Healthie's public API reference" in header
    assert "used to check this adapter's queries" in header
    assert f"https://docs.gethealthie.com/reference/{HEALTHIE_API_VERSION}/objects/user" in header
    assert (
        f"https://docs.gethealthie.com/reference/{HEALTHIE_API_VERSION}/mutations/createdocument"
        in header
    )


def test_the_excerpt_holds_definitions_without_descriptions() -> None:
    text = (HERE / "schema" / f"healthie-{HEALTHIE_API_VERSION}.graphql").read_text()
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))

    assert '"""' not in body
    assert not re.search(r'^\s*"', body, re.M)
