from fastapi import FastAPI

from kalpi_engine import __version__
from kalpi_engine.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Kalpi Execution Engine", version=__version__)
    app.state.settings = settings

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
