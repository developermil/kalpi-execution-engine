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


def _serve(name: str, media_type: str) -> Response:
    for d in _CANDIDATES:
        if (d / name).is_file():
            return FileResponse(d / name, media_type=media_type)
    return HTMLResponse(f"UI file not found (frontend/{name})", status_code=404)


@router.get("/ui")
async def ui() -> Response:
    return _serve("index.html", "text/html")


@router.get("/ui/portfolio.js")
async def portfolio_js() -> Response:
    """File-upload parser (.json / .csv); a separate file so node can unit-test it."""
    return _serve("portfolio.js", "text/javascript")
