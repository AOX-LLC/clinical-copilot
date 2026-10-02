from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app

UNREACHABLE_DATABASE = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"


@asynccontextmanager
async def _client(database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=SecretStr(database_url)))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        # ASGITransport does not run the lifespan, so release the pool here.
        await app.state.engine.dispose()


async def test_liveness_needs_no_database() -> None:
    async with _client(UNREACHABLE_DATABASE) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_fails_without_revealing_why() -> None:
    async with _client(UNREACHABLE_DATABASE) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def test_api_docs_are_not_exposed() -> None:
    async with _client(UNREACHABLE_DATABASE) as client:
        responses = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

    assert [response.status_code for response in responses] == [404, 404, 404]


@pytest.mark.db
async def test_readiness_passes_when_the_schema_is_at_head(migrated_database_url: str) -> None:
    async with _client(migrated_database_url) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.db
async def test_readiness_fails_on_an_unmigrated_database(empty_database_url: str) -> None:
    async with _client(empty_database_url) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503


def test_settings_reject_an_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        Settings(database_url=SecretStr(UNREACHABLE_DATABASE), clinic_timezone="Mars/Olympus")


def test_a_blank_clinic_today_means_real_time() -> None:
    settings = Settings(database_url=SecretStr(UNREACHABLE_DATABASE), clinic_today="")

    assert settings.clinic_today is None
