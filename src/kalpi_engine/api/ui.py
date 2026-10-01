"""Minimal static UI (D14, D37): one HTML file served at /ui; it calls the same /v1 API."""

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

router = APIRouter(tags=["ui"], include_in_schema=False)
_CANDIDATES = (
    Path(os.environ.get("FRONTEND_DIR", "frontend")),  # container: /app/frontend (cwd)
    Path(__file__).resolve().parents[3] / "frontend",  # source checkout
)


@router.get("/ui")
async def ui() -> Response:
    for d in _CANDIDATES:
        if (d / "index.html").is_file():
            return FileResponse(d / "index.html", media_type="text/html")
    return HTMLResponse("UI not found (frontend/index.html)", status_code=404)
