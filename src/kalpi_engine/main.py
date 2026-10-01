import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from kalpi_engine import __version__
from kalpi_engine.api import brokers as brokers_api
from kalpi_engine.api import mock_webhook
from kalpi_engine.brokers.registry import Registry, get_registry
from kalpi_engine.config import Settings, get_settings
from kalpi_engine.notify.worker import Notifier
from kalpi_engine.storage.db import create_all, make_engine, make_sessionmaker


def create_app(settings: Settings | None = None, registry: Registry | None = None) -> FastAPI:
    settings = settings or get_settings()
    registry = registry or get_registry()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await create_all(engine)  # Alembic is stretch (S3)
        stop = asyncio.Event()
        async with httpx.AsyncClient() as client:
            notifier = Notifier(
                app.state.sessionmaker, client, secret=settings.webhook_secret.get_secret_value()
            )
            task = asyncio.create_task(notifier.run_forever(stop))
            yield
            stop.set()
            await task
        await registry.aclose()
        await engine.dispose()

    app = FastAPI(title="Kalpi Execution Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.include_router(brokers_api.router)
    app.include_router(mock_webhook.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, str]:
        """Readiness: the DB answers."""
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app


app = create_app()
