from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from kalpi_engine import __version__
from kalpi_engine.api import brokers as brokers_api
from kalpi_engine.brokers.registry import Registry, get_registry
from kalpi_engine.config import Settings, get_settings


def create_app(settings: Settings | None = None, registry: Registry | None = None) -> FastAPI:
    settings = settings or get_settings()
    registry = registry or get_registry()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await registry.aclose()

    app = FastAPI(title="Kalpi Execution Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.include_router(brokers_api.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, str]:
        """Readiness: dependency checks (DB) are added with the storage bead."""
        return {"status": "ready"}

    return app


app = create_app()
