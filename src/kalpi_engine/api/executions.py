"""Preview / execute / status (SPEC §2). Validation runs before any insert: a 4xx creates no run."""

import hashlib
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Header, Query, Response
from pydantic import BaseModel

from kalpi_engine.api.deps import Rt, User
from kalpi_engine.api.errors import ApiError
from kalpi_engine.domain.enums import Mode, RunStatus
from kalpi_engine.domain.errors import AuthExpired, KalpiError, UnknownSymbol
from kalpi_engine.domain.schemas import (
    ErrorEnvelope,
    ExecuteAccepted,
    ExecuteRequest,
    Issue,
    LegView,
    PlannedLeg,
    PreviewResponse,
)
from kalpi_engine.domain.tags import tag_length
from kalpi_engine.planner import ValidationContext, http_status, plan, validate
from kalpi_engine.service import LoadedSession, Runtime
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Run

router = APIRouter(prefix="/v1/executions", tags=["executions"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    s: {"model": ErrorEnvelope} for s in (401, 409, 422, 502)
}
_EXAMPLES = {
    "rebalance": {
        "summary": "Rebalance: sells run before buys",
        "value": {
            "session_id": "00000000-0000-0000-0000-000000000000",
            "mode": "REBALANCE",
            "options": {"order_type": "MARKET", "halt_on_sell_failure": False},
            "instructions": [
                {"symbol": "TCS", "action": "SELL", "quantity": 5},
                {"symbol": "INFY", "action": "REBALANCE", "side": "BUY", "quantity": 3},
                {
                    "symbol": "HDFCBANK",
                    "action": "BUY",
                    "quantity": 4,
                    "order_type": "LIMIT",
                    "limit_price": 1650.0,
                },
            ],
        },
    },
    "first_time": {
        "summary": "First-time portfolio (all BUY, empty holdings)",
        "value": {
            "session_id": "00000000-0000-0000-0000-000000000000",
            "mode": "FIRST_TIME",
            "instructions": [{"symbol": "RELIANCE", "action": "BUY", "quantity": 10}],
        },
    },
}
Payload = Annotated[ExecuteRequest, Body(openapi_examples=_EXAMPLES)]


class EventView(BaseModel):
    seq: int
    ts: datetime
    type: str
    payload: dict[str, Any]


class NotificationView(BaseModel):
    url: str | None
    status: str
    attempts: int


class RunView(BaseModel):
    run_id: str
    status: RunStatus
    mode: Mode
    session_id: str
    created_at: datetime
    finished_at: datetime | None
    options: dict[str, Any]
    legs: list[LegView] = []
    events: list[EventView] = []
    notifications: list[NotificationView] = []


def request_hash(body: ExecuteRequest) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


def _session_expired(msg: str) -> ApiError:
    issue = Issue(code="SESSION_EXPIRED", message=msg)
    return ApiError(401, "SESSION_EXPIRED", msg, [issue])


async def _validated(
    rt: Runtime, body: ExecuteRequest, user: str
) -> tuple[LoadedSession, list[Issue]]:
    """V1-V11 against live holdings; raises ApiError listing ALL violations."""
    loaded = await rt.load_session(str(body.session_id), user)
    if loaded is None:
        raise _session_expired("session not found; log in again")
    adapter, bs = loaded.adapter, loaded.session
    try:
        holdings = await adapter.get_holdings(bs)
        known: dict[str, bool] = {}
        for ins in body.instructions:
            try:
                await adapter.resolve_instrument(body.options.exchange, ins.symbol)
                known[ins.symbol] = True
            except UnknownSymbol:
                known[ins.symbol] = False
    except AuthExpired:
        raise _session_expired("broker session expired; log in again") from None
    except KalpiError as exc:
        raise ApiError(502, "BROKER_ERROR", exc.message) from None
    ctx = ValidationContext(
        holdings=holdings,
        is_known=lambda _ex, sym: known.get(sym, False),
        market_order_verified=adapter.meta.market_order_verified,
        now=rt.wall(),
        session_expires_at=loaded.row.expires_at,
        max_qty_per_order=rt.settings.max_qty_per_order,
    )
    res = validate(body, ctx)
    if not res.ok:
        status = http_status(res.errors)
        codes = {e.code for e in res.errors}
        if status != 422:  # 401/409 name the error that decided the status
            code = next(e.code for e in res.errors if http_status([e]) == status)
        else:
            code = codes.pop() if len(codes) == 1 else "VALIDATION_FAILED"
        raise ApiError(status, code, f"{len(res.errors)} validation error(s)", res.errors)
    return loaded, res.warnings


def _planned(body: ExecuteRequest, loaded: LoadedSession, run_id: str) -> list[PlannedLeg]:
    intents = plan(body, run_id, tag_length(loaded.adapter.meta.tag_max_len))
    return [
        PlannedLeg(leg_index=int(i.leg_id), **i.model_dump(exclude={"leg_id", "product", "tag"}))
        for i in intents
    ]


@router.post("/preview", response_model=PreviewResponse, responses=_ERRORS)
async def preview(body: Payload, rt: Rt, user: User) -> PreviewResponse:
    """Validate and plan against live holdings. Never places an order."""
    loaded, warnings = await _validated(rt, body, user)
    return PreviewResponse(legs=_planned(body, loaded, "preview"), warnings=warnings)


def _accepted(run: Run) -> ExecuteAccepted:
    return ExecuteAccepted(run_id=UUID(run.id), status=run.status)


@router.post("", status_code=202, response_model=ExecuteAccepted, responses=_ERRORS)
async def execute(
    body: Payload,
    response: Response,
    rt: Rt,
    user: User,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> ExecuteAccepted:
    """202 + run_id; the run executes in the background. Same key + same body -> same run (200)."""
    if not idempotency_key:
        raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
    rhash = request_hash(body)
    async with rt.sm() as s:
        found = await repo.get_run_by_key(s, user, idempotency_key)
    if found is None:
        loaded, _ = await _validated(rt, body, user)
        run_id = str(uuid4())
        intents = plan(body, run_id, tag_length(loaded.adapter.meta.tag_max_len))
        try:
            created = await repo.create_run(
                rt.sm,
                run_id=run_id,
                user_id=user,
                idempotency_key=idempotency_key,
                request_hash=rhash,
                session_id=str(body.session_id),
                mode=body.mode,
                options=body.options.model_dump(mode="json"),
                legs=intents,
                now=rt.wall(),
            )
        except repo.IdempotencyKeyReused as exc:
            raise ApiError(422, exc.code, str(exc)) from None
        if created.created:
            await rt.launch(created.run)
            return _accepted(created.run)
        found = created.run
    if found.request_hash != rhash:
        raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "Idempotency-Key reused with another body")
    response.status_code = 200
    return _accepted(found)


def _view(run: Run) -> RunView:
    return RunView(
        run_id=run.id,
        status=run.status,
        mode=run.mode,
        session_id=run.session_id,
        created_at=run.created_at,
        finished_at=run.finished_at,
        options=run.options_json,
    )


@router.get("/{run_id}", response_model=RunView, responses=_ERRORS)
async def get_execution(run_id: str, rt: Rt, user: User) -> RunView:
    async with rt.sm() as s:
        run = await repo.get_run(s, run_id)
        if run is None or run.user_id != user:
            raise ApiError(404, "RUN_NOT_FOUND", f"run {run_id} not found")
        legs, events, outbox = (
            await repo.get_legs(s, run_id),
            await repo.get_events(s, run_id),
            await repo.get_outbox(s, run_id),
        )
    view = _view(run)
    view.legs = [
        LegView(
            leg_index=lg.idx,
            phase=lg.phase,
            symbol=lg.symbol,
            exchange=lg.exchange,
            side=lg.side,
            quantity=lg.qty,
            order_type=lg.order_type,
            limit_price=lg.limit_price,
            status=lg.status,
            tag=lg.tag,
            broker_order_id=lg.broker_order_id,
            filled_qty=lg.filled_qty,
            avg_price=lg.avg_price,
            message=lg.reason,
        )
        for lg in legs
    ]
    view.events = [
        EventView(seq=e.seq, ts=e.ts, type=e.type, payload=e.payload_json) for e in events
    ]
    view.notifications = [
        NotificationView(url=o.url, status=o.status, attempts=o.attempts) for o in outbox
    ]
    return view


@router.get("", response_model=list[RunView])
async def list_executions(
    rt: Rt, user: User, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunView]:
    async with rt.sm() as s:
        runs = await repo.list_runs(s, user, limit)
    return [_view(r) for r in runs]
