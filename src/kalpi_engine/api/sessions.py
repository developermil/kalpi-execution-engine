"""Broker login (SPEC §2): OAuth redirect flow and credential/API-key sessions.

Credentials, TOTPs and tokens are never logged; only the encrypted token is stored.
"""

import logging
import secrets
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from kalpi_engine.api.deps import Rt, User
from kalpi_engine.api.errors import ApiError
from kalpi_engine.brokers.base import AuthMode, BrokerAdapter, BrokerSession
from kalpi_engine.domain.errors import KalpiError
from kalpi_engine.service import Runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
STATE_TTL_S = 600


class LoginUrl(BaseModel):
    login_url: str
    state: str


class SessionCreate(BaseModel):
    broker: str
    credentials: dict[str, Any] = Field(
        default_factory=dict, examples=[{"holdings": {"RELIANCE": 10}}]
    )


class SessionCreated(BaseModel):
    session_id: str
    expires_at: datetime


def _adapter(rt: Runtime, broker: str) -> BrokerAdapter:
    try:
        return rt.registry.get(broker)
    except KeyError:
        raise ApiError(422, "UNKNOWN_BROKER", f"unknown broker {broker!r}") from None


def _states(request: Request) -> dict[str, tuple[str, str, float]]:
    """state -> (user_id, broker, issued_at). Single process; a restart forces a new login."""
    st = request.app.state
    if not hasattr(st, "oauth_states"):
        st.oauth_states = {}
    states: dict[str, tuple[str, str, float]] = st.oauth_states
    return states


async def _store(rt: Runtime, user: str, bs: BrokerSession) -> tuple[str, datetime]:
    try:
        return await rt.save_session(user, bs)
    except ValueError as exc:  # FERNET_KEY missing: refuse rather than store plaintext
        raise ApiError(503, "NOT_CONFIGURED", str(exc)) from None


async def _login(adapter: BrokerAdapter, params: dict[str, Any]) -> BrokerSession:
    try:
        return await adapter.create_session(params)
    except KalpiError as exc:
        log.info("broker login failed broker=%s code=%s", adapter.meta.id, exc.code)
        raise ApiError(401, "BROKER_LOGIN_FAILED", exc.message) from None


@router.get("/login-url", response_model=LoginUrl)
async def login_url(broker: str, request: Request, rt: Rt, user: User) -> LoginUrl:
    adapter = _adapter(rt, broker)
    if adapter.meta.auth_mode is not AuthMode.OAUTH_REDIRECT:
        raise ApiError(422, "NOT_OAUTH_BROKER", f"{broker} logs in via POST /v1/sessions")
    state = secrets.token_urlsafe(24)
    _states(request)[state] = (user, broker, time.monotonic())
    return LoginUrl(login_url=await adapter.login_url(state), state=state)


@router.get("/callback", response_class=RedirectResponse, status_code=303)
async def callback(request: Request, rt: Rt, broker: str, state: str | None = None) -> Any:
    """Browser redirect from the broker: no API key; the one-time `state` identifies the user."""
    entry = _states(request).pop(state or "", None)
    if entry is None or entry[1] != broker or time.monotonic() - entry[2] > STATE_TTL_S:
        raise ApiError(400, "INVALID_STATE", "unknown, expired or mismatched OAuth state")
    user = entry[0]
    params = {k: v for k, v in request.query_params.items() if k not in ("broker", "state")}
    bs = await _login(_adapter(rt, broker), params)
    sid, _ = await _store(rt, user, bs)
    return RedirectResponse(f"/ui?session_id={sid}", status_code=303)


@router.post("", response_model=SessionCreated)
async def create_session(body: SessionCreate, rt: Rt, user: User) -> SessionCreated:
    adapter = _adapter(rt, body.broker)
    if adapter.meta.auth_mode is AuthMode.OAUTH_REDIRECT:
        raise ApiError(422, "OAUTH_BROKER", f"{body.broker} logs in via /v1/sessions/login-url")
    missing = [f for f in adapter.meta.credential_fields if not body.credentials.get(f)]
    if missing:
        raise ApiError(422, "MISSING_CREDENTIALS", f"missing credential fields: {missing}")
    bs = await _login(adapter, body.credentials)
    sid, expires_at = await _store(rt, user, bs)
    return SessionCreated(session_id=sid, expires_at=expires_at)
