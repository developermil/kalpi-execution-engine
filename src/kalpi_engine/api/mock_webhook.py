"""Demo webhook consumer: verifies the HMAC signature, logs, keeps the last N payloads."""

import json
import logging
from collections import deque
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from kalpi_engine.config import Settings
from kalpi_engine.notify.sign import SIGNATURE_HEADER, verify

log = logging.getLogger(__name__)
router = APIRouter(prefix="/mock/webhook", tags=["mock"])
KEEP_LAST = 50


def _store(request: Request) -> deque[dict[str, Any]]:
    st = request.app.state
    if not hasattr(st, "mock_webhooks"):
        st.mock_webhooks = deque(maxlen=KEEP_LAST)
    store: deque[dict[str, Any]] = st.mock_webhooks
    return store


@router.post("", response_model=None)
async def receive(request: Request) -> dict[str, str] | JSONResponse:
    settings: Settings = request.app.state.settings
    body = await request.body()
    if not verify(
        settings.webhook_secret.get_secret_value(), body, request.headers.get(SIGNATURE_HEADER)
    ):
        return JSONResponse(
            {"error": {"code": "BAD_SIGNATURE", "message": "signature mismatch", "details": None}},
            status_code=401,
        )
    payload = json.loads(body)
    _store(request).append(payload)
    log.info(
        "mock webhook got %s run=%s seq=%s",
        payload.get("event"),
        payload.get("run_id"),
        payload.get("seq"),
    )
    return {"status": "ok"}


@router.get("")
async def recent(request: Request) -> list[dict[str, Any]]:
    return list(_store(request))
