import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from kalpi_engine import __version__
from kalpi_engine.api import brokers as brokers_api
from kalpi_engine.api import errors, executions, mock_webhook, sessions, ui
from kalpi_engine.brokers.registry import Registry, get_registry
from kalpi_engine.config import Settings, get_settings
from kalpi_engine.logging import log_context, setup_logging
from kalpi_engine.notify.worker import Notifier
from kalpi_engine.service import Runtime
from kalpi_engine.storage.db import create_all, make_engine, make_sessionmaker


def create_app(settings: Settings | None = None, registry: Registry | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
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
            # Resume sweep runs at startup and every LEASE_TTL_S: stranded runs restart here.
            tasks = [
                asyncio.create_task(notifier.run_forever(stop)),
                asyncio.create_task(runtime.resume_sweeper().run_forever(stop)),
                asyncio.create_task(runtime.recheck_forever(stop)),
                asyncio.create_task(registry.startup()),  # instrument masters, once (D35)
            ]
            app.state.background = tasks
            yield
            stop.set()
            await asyncio.gather(*tasks)
            await runtime.shutdown()
        await registry.aclose()
        await engine.dispose()

    app = FastAPI(title="Kalpi Execution Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    runtime = Runtime(settings, registry, app.state.sessionmaker)
    app.state.runtime = runtime
    errors.install(app)

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid4().hex[:16]
        with log_context(request_id=rid):
            response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    app.include_router(brokers_api.router)
    app.include_router(sessions.router)
    app.include_router(executions.router)
    app.include_router(mock_webhook.router)
    app.include_router(ui.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        """Readiness: the DB answers and at least one broker adapter is registered."""
        checks = {"db": "ok", "registry": "ok" if registry.ids() else "no brokers registered"}
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            checks["db"] = f"unavailable: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        body = {"status": "ready" if ok else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


app = create_app()
