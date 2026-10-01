"""E2: drive the real compose stack (app + Postgres) over HTTP with the Paper broker.

Needs `make itest` (which brings the stack up with docker-compose.itest.yml). Values below
must match that override file.
"""

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE = f"http://127.0.0.1:{os.environ.get('APP_PORT', '8000')}"  # APP_PORT: see docker-compose.yml
H = {"X-API-Key": "itest-key"}
TERMINAL = {"COMPLETED", "COMPLETED_WITH_FAILURES", "FAILED"}
COMPOSE = ["docker", "compose", "-p", "kalpi-itest",
           "-f", "docker-compose.yml", "-f", "docker-compose.itest.yml"]  # fmt: skip


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE, timeout=10) as c:
        deadline = time.monotonic() + 60
        while True:
            try:
                if c.get("/readyz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("compose stack not ready on :8000 (run `make itest`)")
            time.sleep(1)
        yield c


def _session(http: httpx.Client, holdings: dict[str, int]) -> str:
    r = http.post(
        "/v1/sessions", json={"broker": "paper", "credentials": {"holdings": holdings}}, headers=H
    )
    assert r.status_code == 200, r.text
    return str(r.json()["session_id"])


def _body(session_id: str, mode: str, instructions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "mode": mode,
        "options": {"poll_timeout_s": 10},
        "instructions": instructions,
    }


def _wait(http: httpx.Client, run_id: str, *, delivered: bool = True) -> dict[str, Any]:
    """Poll until the run is terminal (and its webhook SENT when `delivered`)."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run: dict[str, Any] = http.get(f"/v1/executions/{run_id}", headers=H).json()
        sent = any(n["status"] == "SENT" for n in run.get("notifications", []))
        if run["status"] in TERMINAL and (sent or not delivered):
            # A healthy run finishes in its first executor; resume means it crashed/stalled.
            assert "run.resumed" not in {e["type"] for e in run["events"]}, run["events"]
            return run
        time.sleep(0.5)
    pytest.fail(f"run {run_id} did not finish: {run}")


def _webhooks(http: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = http.get("/mock/webhook").json()
    return [p for p in got if p["run_id"] == run_id]


def test_first_time_buy_completes_and_webhook_received(http: httpx.Client) -> None:
    sid = _session(http, {})
    instr = [
        {"symbol": "RELIANCE", "action": "BUY", "quantity": 2},
        {"symbol": "TCS", "action": "BUY", "quantity": 1},
    ]
    r = http.post(
        "/v1/executions",
        json=_body(sid, "FIRST_TIME", instr),
        headers=H | {"Idempotency-Key": f"ft-{uuid.uuid4()}"},
    )
    assert r.status_code == 202, r.text
    run = _wait(http, r.json()["run_id"])
    assert run["status"] == "COMPLETED"
    assert {lg["status"] for lg in run["legs"]} == {"FILLED"}
    hooks = _webhooks(http, run["run_id"])
    assert hooks and hooks[-1]["event"] == "execution.completed"
    assert hooks[-1]["summary"]["filled"] == 2 and hooks[-1]["summary"]["total"] == 2


def test_rebalance_sells_before_buys(http: httpx.Client) -> None:
    sid = _session(http, {"INFY": 8, "ITC": 40})
    instr = [
        {"symbol": "INFY", "action": "SELL", "quantity": 8},
        {"symbol": "ITC", "action": "REBALANCE", "side": "SELL", "quantity": 10},
        {"symbol": "SBIN", "action": "BUY", "quantity": 5},
    ]
    r = http.post(
        "/v1/executions",
        json=_body(sid, "REBALANCE", instr),
        headers=H | {"Idempotency-Key": f"rb-{uuid.uuid4()}"},
    )
    assert r.status_code == 202, r.text
    run = _wait(http, r.json()["run_id"])
    assert run["status"] == "COMPLETED"
    phases = [lg["side"] for lg in sorted(run["legs"], key=lambda lg: lg["leg_index"])]
    assert phases == ["SELL", "SELL", "BUY"]
    assert {lg["status"] for lg in run["legs"]} == {"FILLED"}
    assert _webhooks(http, run["run_id"])


def test_duplicate_idempotency_key_same_run_and_db_unique(http: httpx.Client) -> None:
    sid = _session(http, {})
    key = f"dup-{uuid.uuid4()}"
    body = _body(sid, "FIRST_TIME", [{"symbol": "WIPRO", "action": "BUY", "quantity": 1}])
    first = http.post("/v1/executions", json=body, headers=H | {"Idempotency-Key": key})
    second = http.post("/v1/executions", json=body, headers=H | {"Idempotency-Key": key})
    assert (first.status_code, second.status_code) == (202, 200)
    run_id = first.json()["run_id"]
    assert second.json()["run_id"] == run_id
    other = body | {"instructions": [{"symbol": "WIPRO", "action": "BUY", "quantity": 2}]}
    reused = http.post("/v1/executions", json=other, headers=H | {"Idempotency-Key": key})
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    run = _wait(http, run_id)
    assert len(run["legs"]) == 1

    # Postgres-level guarantee, independent of the app's pre-check: one row per (user, key),
    # and a raw duplicate insert is refused by the UNIQUE constraint.
    def psql(sql: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*COMPOSE, "exec", "-T", "db", "psql", "-U", "kalpi", "-d", "kalpi",
             "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],  # fmt: skip
            capture_output=True, text=True, timeout=30,
        )

    count = psql(f"SELECT count(*) FROM runs WHERE idempotency_key = '{key}'")
    assert count.returncode == 0, count.stderr
    assert count.stdout.strip() == "1"
    dup = psql(
        "INSERT INTO runs (id, user_id, idempotency_key, request_hash, session_id, mode, "
        "status, options_json, created_at) SELECT md5(random()::text)::uuid::text, user_id, "
        "idempotency_key, request_hash, session_id, mode, status, options_json, created_at "
        f"FROM runs WHERE idempotency_key = '{key}'"
    )
    assert dup.returncode != 0
    assert "duplicate key value violates unique constraint" in dup.stderr
