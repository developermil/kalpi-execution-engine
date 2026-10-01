# ruff: noqa: F811  (pytest fixtures imported from test_api and used by name)
"""E1: JSON logs carry run_id/leg_id and never a secret; /readyz reports DB + registry."""

import io
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from kalpi_engine.logging import JsonFormatter, log_context, redact, redact_text
from tests.unit.test_api import (  # noqa: F401
    H,
    Settings,
    client,
    paper_session,
    reg,
    registry,
    settings,
)

SECRETS = ("PIN-9876", "TOTP-424242", "jwt-SECRET", "feed-SECRET", "sk-live-API-SECRET")


@pytest.fixture
def logbuf() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old)


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]


def test_full_run_logs_carry_run_id_and_no_secret(
    client: TestClient,
    reg,
    logbuf: io.StringIO,  # noqa: ANN001
) -> None:
    creds = {"client_code": "C1", "pin": SECRETS[0], "totp": SECRETS[1]}
    with respx.mock(base_url="https://creds.example") as mock:
        mock.post("/login").respond(json={"data": {"jwtToken": SECRETS[2]}})
        assert (
            client.post(
                "/v1/sessions", json={"broker": "fakecreds", "credentials": creds}, headers=H
            ).status_code
            == 200
        )
    sid = paper_session(client, {"TCS": 5})
    body = {
        "session_id": sid, "mode": "REBALANCE",
        "instructions": [
            {"symbol": "TCS", "action": "SELL", "quantity": 2},
            {"symbol": "INFY", "action": "BUY", "quantity": 3},
        ],
    }  # fmt: skip
    r = client.post("/v1/executions", json=body, headers={**H, "Idempotency-Key": "obs-1"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    for _ in range(100):
        if client.get(f"/v1/executions/{run_id}", headers=H).json()["status"] == "COMPLETED":
            break
        time.sleep(0.1)
    recs = lines(logbuf)
    with_run = [x for x in recs if x.get("run_id") == run_id]
    assert any(x["msg"] == "run started" for x in with_run)
    assert any(x["msg"] == "run finished" and x["status"] == "COMPLETED" for x in with_run)
    placed = [x for x in with_run if x["msg"] == "placing order"]
    assert len(placed) == 2 and all(x.get("leg_id") for x in placed)
    assert any(x.get("request_id") for x in recs)  # request logs carry the request id
    paper_tokens = [k for k in reg.get("paper")._accounts]  # noqa: SLF001
    blob = logbuf.getvalue()
    for secret in (*SECRETS, *paper_tokens):
        assert secret not in blob


def test_redaction_formats() -> None:
    cases = [
        "login pin=PIN-9876 totp: TOTP-424242 for C1",
        '{"jwtToken": "jwt-SECRET", "refreshToken": "r-SECRET"}',
        "{'access_token': 'sk-live-API-SECRET', 'ok': 1}",
        "Authorization: token key:sk-live-API-SECRET",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadpart.sigpart",
        "HTTP 400: password=hunter2&client=C1",
        "x-api-key: sk-live-API-SECRET",
    ]
    for c in cases:
        out = redact_text(c)
        for s in (
            "PIN-9876",
            "TOTP-424242",
            "jwt-SECRET",
            "r-SECRET",
            "sk-live-API-SECRET",
            "hunter2",
            "eyJhbGci",
            "payloadpart",
        ):
            assert s not in out, (c, out)
    assert "C1" in redact_text("password=hunter2&client=C1")  # only the secret goes
    data = {"pin": "1", "nested": {"api_key": "k", "symbol": "TCS"}, "l": ["token=abc"]}
    assert redact(data) == {
        "pin": "[REDACTED]", "nested": {"api_key": "[REDACTED]", "symbol": "TCS"},
        "l": ["token=[REDACTED]"],
    }  # fmt: skip


def test_json_line_has_context_and_redacts_extras(logbuf: io.StringIO) -> None:
    log = logging.getLogger("kalpi.test")
    with log_context(run_id="r-1", leg_id="l-1"):
        log.info("hello totp=999999", extra={"password": "x", "symbol": "TCS"})
    (rec,) = [x for x in lines(logbuf) if x["logger"] == "kalpi.test"]
    assert rec["run_id"] == "r-1" and rec["leg_id"] == "l-1" and rec["symbol"] == "TCS"
    assert rec["password"] == "[REDACTED]" and "999999" not in rec["msg"]


def test_request_id_header_roundtrip(client: TestClient) -> None:
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"]
        == "abc-123"
    )
    assert len(client.get("/healthz").headers["X-Request-ID"]) == 16


def test_readyz_ok_then_503_when_db_down(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,  # noqa: F811
) -> None:
    ok = client.get("/readyz")
    assert ok.status_code == 200 and ok.json()["checks"] == {"db": "ok", "registry": "ok"}

    def boom(*_a: object, **_k: object) -> None:  # engine.connect() is sync, returns a ctx mgr
        raise sqlite3.OperationalError("db is down")

    monkeypatch.setattr(AsyncEngine, "connect", boom)
    down = client.get("/readyz")
    assert down.status_code == 503
    assert down.json()["status"] == "not_ready" and down.json()["checks"]["db"].startswith(
        "unavailable"
    )


def test_readyz_503_when_no_brokers(tmp_path: Path) -> None:
    from kalpi_engine.brokers.registry import Registry
    from kalpi_engine.config import Settings as Cfg
    from kalpi_engine.main import create_app

    cfg = Cfg(database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
    with TestClient(create_app(cfg, Registry({}))) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["registry"] != "ok"
