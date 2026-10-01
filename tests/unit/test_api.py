"""B6: API layer over httpx ASGI (TestClient) with Paper + test-only OAuth/credential adapters."""

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import update

from kalpi_engine.brokers.base import (
    AuthMode,
    BrokerAdapter,
    BrokerMeta,
    BrokerSession,
    HttpBrokerAdapter,
    InstrumentRef,
    RateLimits,
)
from kalpi_engine.brokers.paper import PaperBroker
from kalpi_engine.brokers.registry import Registry
from kalpi_engine.config import Settings
from kalpi_engine.domain.enums import Exchange, LegStatus, RunStatus
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.main import create_app
from kalpi_engine.planner import plan
from kalpi_engine.service import Runtime
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg, Run, make_engine, make_sessionmaker

H = {"X-API-Key": "k1"}
OAUTH_EXPIRY = datetime(2030, 1, 1, tzinfo=UTC)


class _Stub(BrokerAdapter):
    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        return []

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        return None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return InstrumentRef(exchange=exchange, symbol=symbol, token="1")

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        raise AssertionError("place_order must not be called")

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        raise AssertionError

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        raise AssertionError


class FakeOAuth(_Stub):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="fakeoauth",
        name="Fake OAuth",
        auth_mode=AuthMode.OAUTH_REDIRECT,
        rate_limits=RateLimits(orders_per_sec=5, reads_per_sec=5),
    )

    async def login_url(self, state: str) -> str:
        return f"https://broker.example/login?state={state}"

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        assert params["request_token"] == "rt-1"
        return BrokerSession(
            broker_id="fakeoauth",
            access_token=SecretStr("oauth-tok-SECRET"),
            expires_at=OAUTH_EXPIRY,
        )


class FakeCreds(HttpBrokerAdapter, _Stub):
    base_url: ClassVar[str] = "https://creds.example"
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="fakecreds",
        name="Fake creds",
        auth_mode=AuthMode.CREDENTIALS_TOTP,
        credential_fields=("client_code", "pin", "totp"),
        rate_limits=RateLimits(orders_per_sec=5, reads_per_sec=5),
        market_order_verified=False,
    )

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        body = await self.request("POST", "/login", json=dict(params))
        return BrokerSession(
            broker_id="fakecreds",
            access_token=SecretStr(body["data"]["jwtToken"]),
            extra={"feed_token": "feed-SECRET"},
        )


def registry() -> Registry:
    return Registry({"paper": PaperBroker, "fakeoauth": FakeOAuth, "fakecreds": FakeCreds})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}",
        fernet_key=Fernet.generate_key().decode(),  # type: ignore[arg-type]
        api_keys="u1:k1,u2:k2",  # type: ignore[arg-type]
        webhook_secret="whsec",  # type: ignore[arg-type]
        default_webhook_url="",
        poll_interval_s=0.05,
        recheck_interval_s=0.1,
        lease_ttl_s=5,
    )


@pytest.fixture
def reg() -> Registry:
    return registry()


@pytest.fixture
def client(settings: Settings, reg: Registry) -> Iterator[TestClient]:
    with TestClient(create_app(settings, reg)) as c:
        yield c


def db_path(settings: Settings) -> str:
    return settings.database_url.split("///", 1)[1]


def count(settings: Settings, table: str) -> int:
    with sqlite3.connect(db_path(settings)) as con:
        n: int = con.execute(f"select count(*) from {table}").fetchone()[0]
        return n


def paper_session(c: TestClient, holdings: dict[str, int] | None = None) -> str:
    r = c.post(
        "/v1/sessions",
        json={"broker": "paper", "credentials": {"holdings": holdings or {}}},
        headers=H,
    )
    assert r.status_code == 200, r.text
    sid: str = r.json()["session_id"]
    return sid


def body(sid: str, mode: str = "FIRST_TIME", **extra: Any) -> dict[str, Any]:
    ins = extra.pop(
        "instructions",
        [
            {"symbol": "RELIANCE", "action": "BUY", "quantity": 2},
            {"symbol": "TCS", "action": "BUY", "quantity": 1},
        ],
    )
    return {"session_id": sid, "mode": mode, "instructions": ins, **extra}


def wait_for(fn: Callable[[], bool], timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def terminal(c: TestClient, run_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def done() -> bool:
        out.update(c.get(f"/v1/executions/{run_id}", headers=H).json())
        return out["status"] not in ("CREATED", "RUNNING")

    wait_for(done)
    return out


def assert_envelope(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    err: dict[str, Any] = r.json()["error"]
    assert set(err) == {"code", "message", "details"} and err["code"] == code
    return err


# ---------- sessions ----------


def test_oauth_login_url_and_callback(client: TestClient, settings: Settings) -> None:
    r = client.get("/v1/sessions/login-url", params={"broker": "fakeoauth"}, headers=H)
    data = r.json()
    assert r.status_code == 200 and data["state"] in data["login_url"]
    cb = "/v1/sessions/callback"
    bad = client.get(
        cb,
        params={"broker": "fakeoauth", "state": "nope", "request_token": "rt-1"},
        follow_redirects=False,
    )
    assert_envelope(bad, 400, "INVALID_STATE")
    missing = client.get(
        cb, params={"broker": "fakeoauth", "request_token": "rt-1"}, follow_redirects=False
    )
    assert_envelope(missing, 400, "INVALID_STATE")
    assert count(settings, "broker_sessions") == 0
    ok = client.get(
        cb,
        params={"broker": "fakeoauth", "state": data["state"], "request_token": "rt-1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"].startswith("/ui?session_id=")
    with sqlite3.connect(db_path(settings)) as con:
        user, enc, exp = con.execute(
            "select user_id, token_enc, expires_at from broker_sessions"
        ).fetchone()
    assert user == "u1" and "oauth-tok-SECRET" not in enc and exp.startswith("2030-01-01")
    assert (
        "oauth-tok-SECRET"
        in Fernet(settings.fernet_key.get_secret_value()).decrypt(enc.encode()).decode()
    )
    replay = client.get(
        cb,
        params={"broker": "fakeoauth", "state": data["state"], "request_token": "rt-1"},
        follow_redirects=False,
    )
    assert_envelope(replay, 400, "INVALID_STATE")  # state is single-use


def test_credentials_session_persists_no_secret(client: TestClient, settings: Settings) -> None:
    creds = {"client_code": "C1", "pin": "PIN-9876", "totp": "TOTP-424242"}
    with respx.mock(base_url="https://creds.example") as mock:
        route = mock.post("/login").respond(json={"data": {"jwtToken": "jwt-SECRET"}})
        r = client.post(
            "/v1/sessions", json={"broker": "fakecreds", "credentials": creds}, headers=H
        )
    assert r.status_code == 200 and route.called
    assert set(r.json()) == {"session_id", "expires_at"} and r.json()["expires_at"]
    with sqlite3.connect(db_path(settings)) as con:
        dump = " ".join(str(v) for row in con.execute("select * from broker_sessions") for v in row)
    for secret in ("PIN-9876", "TOTP-424242", "jwt-SECRET", "feed-SECRET"):
        assert secret not in dump
    p = client.post("/v1/sessions", json={"broker": "paper", "credentials": {}}, headers=H)
    assert p.status_code == 200 and set(p.json()) == {"session_id", "expires_at"}
    assert_envelope(
        client.post(
            "/v1/sessions", json={"broker": "fakecreds", "credentials": {"pin": "1"}}, headers=H
        ),
        422,
        "MISSING_CREDENTIALS",
    )


# ---------- preview / execute ----------


def test_preview_plans_without_placing(client: TestClient, reg: Registry) -> None:
    sid = paper_session(client, {"TCS": 5})
    r = client.post(
        "/v1/executions/preview",
        headers=H,
        json=body(
            sid,
            "REBALANCE",
            instructions=[
                {"symbol": "RELIANCE", "action": "BUY", "quantity": 2},
                {"symbol": "TCS", "action": "SELL", "quantity": 5},
            ],
        ),
    )
    assert r.status_code == 200, r.text
    legs = r.json()["legs"]
    assert [(lg["symbol"], lg["phase"]) for lg in legs] == [("TCS", "SELL"), ("RELIANCE", "BUY")]
    paper = reg.get("paper")
    assert isinstance(paper, PaperBroker) and paper.place_calls == 0


def test_preview_warns_market_unverified(client: TestClient) -> None:
    creds = {"client_code": "C1", "pin": "1", "totp": "2"}
    with respx.mock(base_url="https://creds.example") as mock:
        mock.post("/login").respond(json={"data": {"jwtToken": "t"}})
        sid = client.post(
            "/v1/sessions", json={"broker": "fakecreds", "credentials": creds}, headers=H
        ).json()["session_id"]
    r = client.post("/v1/executions/preview", headers=H, json=body(sid))
    assert r.status_code == 200, r.text  # place_order would raise AssertionError -> 500
    codes = [w["code"] for w in r.json()["warnings"]]
    assert codes.count("MARKET_ORDER_UNVERIFIED") == 2


def test_validation_failure_creates_no_run(client: TestClient, settings: Settings) -> None:
    sid = paper_session(client)
    bad = body(
        sid,
        instructions=[
            {"symbol": "NOPE", "action": "BUY", "quantity": 1},
            {"symbol": "TCS", "action": "BUY", "quantity": 10**9},
        ],
    )
    err = assert_envelope(
        client.post("/v1/executions", json=bad, headers=H | {"Idempotency-Key": "a"}),
        422,
        "VALIDATION_FAILED",
    )
    assert {d["code"] for d in err["details"]} == {"UNKNOWN_SYMBOL", "QUANTITY_TOO_LARGE"}
    schema = assert_envelope(
        client.post("/v1/executions", json={"mode": "X"}, headers=H | {"Idempotency-Key": "b"}),
        422,
        "VALIDATION_ERROR",
    )
    assert schema["details"]
    assert count(settings, "runs") == 0 and count(settings, "legs") == 0


def test_error_shapes_401_409(client: TestClient, settings: Settings) -> None:
    assert_envelope(client.get("/v1/executions"), 401, "UNAUTHORIZED")
    assert_envelope(client.get("/v1/executions", headers={"X-API-Key": "bad"}), 401, "UNAUTHORIZED")
    held = paper_session(client, {"RELIANCE": 1})
    err = assert_envelope(
        client.post("/v1/executions", json=body(held), headers=H | {"Idempotency-Key": "h"}),
        409,
        "HOLDINGS_EXIST",
    )
    assert err["details"][0]["code"] == "HOLDINGS_EXIST"
    assert_envelope(
        client.post(
            "/v1/executions", json=body(str(uuid4())), headers=H | {"Idempotency-Key": "x"}
        ),
        401,
        "SESSION_EXPIRED",
    )
    other = {"X-API-Key": "k2", "Idempotency-Key": "o"}  # u2 cannot use u1's session
    assert_envelope(
        client.post("/v1/executions", json=body(held), headers=other), 401, "SESSION_EXPIRED"
    )
    assert count(settings, "runs") == 0


def test_execute_202_terminal_and_idempotency(client: TestClient, reg: Registry) -> None:
    sid = paper_session(client)
    hk = H | {"Idempotency-Key": "key-1"}
    r = client.post("/v1/executions", json=body(sid), headers=hk)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    run = terminal(client, run_id)
    assert run["status"] == "COMPLETED"
    assert [lg["status"] for lg in run["legs"]] == ["FILLED", "FILLED"]
    assert run["events"][0]["type"] == "run.created" and run["notifications"]
    again = client.post("/v1/executions", json=body(sid), headers=hk)
    assert again.status_code == 200 and again.json()["run_id"] == run_id
    changed = body(sid, instructions=[{"symbol": "INFY", "action": "BUY", "quantity": 1}])
    assert_envelope(
        client.post("/v1/executions", json=changed, headers=hk), 422, "IDEMPOTENCY_KEY_REUSED"
    )
    assert_envelope(
        client.post("/v1/executions", json=body(sid), headers=H), 422, "IDEMPOTENCY_KEY_REQUIRED"
    )
    paper = reg.get("paper")
    assert isinstance(paper, PaperBroker) and paper.place_calls == 2
    listed = client.get("/v1/executions", headers=H).json()
    assert [x["run_id"] for x in listed] == [run_id]
    assert client.get(f"/v1/executions/{run_id}", headers={"X-API-Key": "k2"}).status_code == 404


def test_brokers_metadata(client: TestClient) -> None:
    rows = {b["id"]: b for b in client.get("/v1/brokers").json()}
    for key in ("experimental", "live_tested", "market_order_verified"):
        assert key in rows["paper"]
    assert rows["fakecreds"]["market_order_verified"] is False


# ---------- lifespan sweeps ----------


async def _stranded_run(settings: Settings, reg: Registry, sid: str, *, unknown: bool) -> str:
    """A run a crashed worker left behind: leg 0 was accepted by the broker, never recorded."""
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    rt = Runtime(settings, reg, sm)
    loaded = await rt.load_session(sid)
    assert loaded is not None
    req = ExecuteRequest.model_validate(body(sid))
    run_id = str(uuid4())
    intents = plan(req, run_id)
    past = datetime.now(UTC) - timedelta(seconds=5)
    await repo.create_run(
        sm,
        run_id=run_id,
        user_id="u1",
        idempotency_key=run_id,
        request_hash="h",
        session_id=sid,
        mode=req.mode,
        options=req.options.model_dump(mode="json"),
        legs=intents,
        now=past,
    )
    await loaded.adapter.place_order(loaded.session, intents[0])
    async with sm.begin() as s:
        if unknown:  # finished run with an UNKNOWN leg due for recheck
            await s.execute(
                update(Leg)
                .where(Leg.run_id == run_id)
                .values(status=LegStatus.UNKNOWN, recheck_at=past)
            )
            await s.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=RunStatus.FAILED, finished_at=past)
            )
        else:  # crashed between SUBMITTING and recording the broker order id
            await s.execute(
                update(Leg)
                .where(Leg.run_id == run_id, Leg.idx == 0)
                .values(status=LegStatus.SUBMITTING)
            )
            await s.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=RunStatus.RUNNING, lease_owner="dead-worker", lease_until=past)
            )
    await engine.dispose()
    return run_id


def _orders_by_tag(reg: Registry) -> dict[str, int]:
    paper = reg.get("paper")
    assert isinstance(paper, PaperBroker)
    out: dict[str, int] = {}
    for o in paper._orders.values():
        out[o.intent.tag] = out.get(o.intent.tag, 0) + 1
    return out


def test_stranded_submitting_run_resumed_after_restart(settings: Settings, reg: Registry) -> None:
    with TestClient(create_app(settings, reg)) as c:
        sid = paper_session(c)
    run_id = asyncio.run(_stranded_run(settings, reg, sid, unknown=False))  # app is down
    with TestClient(create_app(settings, reg)) as c:  # restart: lifespan resume sweep
        run = terminal(c, run_id)
    assert run["status"] == "COMPLETED"
    assert [lg["status"] for lg in run["legs"]] == ["FILLED", "FILLED"]
    assert "run.resumed" in [e["type"] for e in run["events"]]
    assert sorted(_orders_by_tag(reg).values()) == [1, 1]  # adopted by tag, never resent


def test_recheck_sweep_runs_in_lifespan(settings: Settings, reg: Registry) -> None:
    with TestClient(create_app(settings, reg)) as c:
        sid = paper_session(c)
    run_id = asyncio.run(_stranded_run(settings, reg, sid, unknown=True))
    with TestClient(create_app(settings, reg)) as c:

        def leg0_filled() -> bool:
            run = c.get(f"/v1/executions/{run_id}", headers=H).json()
            return bool(run["legs"][0]["status"] == "FILLED")

        wait_for(leg0_filled)
    assert _orders_by_tag(reg) and sum(_orders_by_tag(reg).values()) == 1
