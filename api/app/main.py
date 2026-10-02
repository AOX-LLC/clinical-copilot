"""FastAPI application factory and health endpoints."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db import create_engine, expected_schema_revision

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    engine = create_engine(settings.database_url.get_secret_value())
    schema_revision = expected_schema_revision()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Clinical Copilot API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.state.schema_revision = schema_revision

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        if await _schema_is_current(request.app.state.engine, request.app.state.schema_revision):
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    return app


async def _schema_is_current(engine: AsyncEngine, expected_revision: str) -> bool:
    try:
        async with engine.connect() as connection:
            applied = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except (SQLAlchemyError, OSError) as error:
        logger.warning(
            "readiness check could not read the schema revision: %s", type(error).__name__
        )
        return False
    if applied != expected_revision:
        logger.warning("schema revision %s does not match expected %s", applied, expected_revision)
        return False
    return True


def create_app_from_environment() -> FastAPI:
    """Entry point for ``uvicorn --factory app.main:create_app_from_environment``."""
    return create_app(Settings())  # values come from the environment
