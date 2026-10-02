"""A fixture Healthie GraphQL endpoint over ``healthie/world.json``.

Queries run through graphql-core against the schema excerpt, so a query that is not valid
against the excerpt is answered with a GraphQL error and the test that sent it fails. The
records are hand-built and synthetic (see ``healthie/README.md``); this is not a recording.
"""

import json
from pathlib import Path
from typing import Any

import httpx
from graphql import ExecutionResult, GraphQLResolveInfo, build_schema, execute, parse, validate

from app.ehr.healthie import HEALTHIE_API_VERSION

HERE = Path(__file__).parent / "healthie"
SCHEMA = build_schema((HERE / "schema" / f"healthie-{HEALTHIE_API_VERSION}.graphql").read_text())
ENDPOINT = "https://healthie.fixture.invalid/graphql"
API_KEY = "synthetic-fixture-key"


def load_world() -> dict[str, Any]:
    world: dict[str, Any] = json.loads((HERE / "world.json").read_text())
    return world


class FixtureHealthie(httpx.AsyncBaseTransport):
    """Answers the adapter's requests; ``throttle_next_call`` gives the next one a 429."""

    def __init__(self, world: dict[str, Any] | None = None) -> None:
        self.world = world if world is not None else load_world()
        self.requests: list[httpx.Request] = []
        self.operations: list[str] = []
        self._throttle_next = False

    def throttle_next_call(self) -> None:
        self._throttle_next = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._throttle_next:
            self._throttle_next = False
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        headers = request.headers
        if (
            headers.get("Authorization") != f"Basic {API_KEY}"
            or headers.get("AuthorizationSource") != "API"
            or headers.get("Healthie-GraphQL-API-Version") != HEALTHIE_API_VERSION
        ):
            return httpx.Response(401, json={"errors": [{"message": "unauthorized"}]})
        body = json.loads(request.content)
        self.operations.append(body["query"])
        document = parse(body["query"])
        problems = validate(SCHEMA, document)
        if problems:
            return httpx.Response(200, json={"errors": [{"message": p.message} for p in problems]})
        result = execute(
            SCHEMA, document, _Root(self.world), variable_values=body.get("variables") or {}
        )
        assert isinstance(result, ExecutionResult)  # nothing here is async
        payload: dict[str, Any] = {"data": result.data}
        if result.errors:
            payload["errors"] = [{"message": e.message} for e in result.errors]
        return httpx.Response(200, json=payload)


class _Root:
    def __init__(self, world: dict[str, Any]) -> None:
        self._world = world

    def users(
        self, _: GraphQLResolveInfo, first: int | None = None, after: str | None = None
    ) -> Any:
        return _connection(self._world["users"], first, after)

    def user(self, _: GraphQLResolveInfo, id: str | None = None) -> Any:  # noqa: A002
        return _by_id(self._world["users"], id)

    def medications(self, _: GraphQLResolveInfo, patient_id: str | None = None) -> Any:
        return [m for m in self._world["medications"] if m["user_id"] == patient_id]

    def medication(self, _: GraphQLResolveInfo, id: str | None = None) -> Any:  # noqa: A002
        return _by_id(self._world["medications"], id)

    def carePlans(  # noqa: N802
        self,
        _: GraphQLResolveInfo,
        patient_id: str | None = None,
        first: int | None = None,
        after: str | None = None,
    ) -> Any:
        plans = [_plan(p) for p in self._world["care_plans"] if p["patient_id"] == patient_id]
        return _connection(plans, first, after)

    def carePlan(self, _: GraphQLResolveInfo, id: str | None = None) -> Any:  # noqa: A002, N802
        found = _by_id(self._world["care_plans"], id)
        return None if found is None else _plan(found)

    def documents(
        self,
        _: GraphQLResolveInfo,
        private_user_id: str | None = None,
        first: int | None = None,
        after: str | None = None,
    ) -> Any:
        mine = [d for d in self._world["documents"] if d["rel_user_id"] == private_user_id]
        return _connection(mine, first, after)

    def document(self, _: GraphQLResolveInfo, id: str | None = None) -> Any:  # noqa: A002
        return _by_id(self._world["documents"], id)

    def createDocument(self, _: GraphQLResolveInfo, input: dict[str, Any]) -> Any:  # noqa: A002, N802
        documents = self._world["documents"]
        created = {
            "id": str(6000 + len(documents)),
            "display_name": input.get("display_name"),
            "description": input.get("description"),
            "rel_user_id": input.get("rel_user_id"),
            "include_in_charting": bool(input.get("include_in_charting")),
            "metadata": input.get("metadata"),
            "created_at": "2026-09-30T15:00:00-04:00",
        }
        documents.append(created)
        return {"document": created, "messages": None}


def _plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {**plan, "patient": {"id": plan["patient_id"]}}


def _by_id(items: list[dict[str, Any]], wanted: str | None) -> dict[str, Any] | None:
    return next((item for item in items if item["id"] == wanted), None)


def _connection(items: list[dict[str, Any]], first: int | None, after: str | None) -> Any:
    start = 0 if after is None else int(after.removeprefix("c"))
    size = len(items) if first is None else first
    end = min(start + size, len(items))
    return {
        "nodes": items[start:end],
        "page_info": {"has_next_page": end < len(items), "end_cursor": f"c{end}"},
    }
