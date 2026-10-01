"""Notification payload (SPEC §6), HMAC signing, and the in-app mock receiver."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from kalpi_engine.config import Settings
from kalpi_engine.domain.enums import (
    Exchange,
    LegStatus,
    Mode,
    OrderType,
    Phase,
    RunStatus,
    Side,
)
from kalpi_engine.main import create_app
from kalpi_engine.notify.payload import build_payload
from kalpi_engine.notify.sign import SIGNATURE_HEADER, sign, verify
from kalpi_engine.storage.db import Leg, Run

T0 = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 30, 6, 1, tzinfo=UTC)


def leg(
    i: int, sym: str, side: Side, qty: int, status: LegStatus, filled: int = 0, **kw: object
) -> Leg:
    return Leg(
        id=f"l{i}",
        run_id="r1",
        idx=i,
        phase=Phase(side.value),
        symbol=sym,
        exchange=Exchange.NSE,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        tag=f"KTAG{i:08d}",
        status=status,
        filled_qty=filled,
        **kw,
    )


def run(status: RunStatus = RunStatus.COMPLETED_WITH_FAILURES) -> Run:
    return Run(id="r1", mode=Mode.REBALANCE, status=status, created_at=T0, finished_at=T1)


LEGS = [
    leg(0, "TCS", Side.SELL, 5, LegStatus.FILLED, 5, avg_price=3890.5, broker_order_id="B1"),
    leg(1, "ITC", Side.SELL, 10, LegStatus.PARTIAL, 4, avg_price=450.0, broker_order_id="B2"),
    leg(2, "INFY", Side.BUY, 3, LegStatus.REJECTED, reason="RMS: insufficient funds"),
    leg(3, "SBIN", Side.BUY, 2, LegStatus.UNKNOWN, reason="AMBIGUOUS_SUBMIT"),
    leg(4, "WIPRO", Side.BUY, 1, LegStatus.SKIPPED, reason="SELL_FAILURE_HALT"),
    leg(5, "LT", Side.BUY, 1, LegStatus.OPEN, broker_order_id="B3"),
    leg(6, "AXISBANK", Side.BUY, 1, LegStatus.SUBMITTING),  # non-terminal: in doubt (D36)
]


def test_payload_matches_spec_shape() -> None:
    p = build_payload(run(), LEGS, broker="paper", event="execution.completed", seq=42)
    assert set(p) == {
        "event", "run_id", "mode", "broker", "status", "summary",
        "executed", "failed", "seq", "started_at", "finished_at",
    }  # fmt: skip
    assert (p["event"], p["run_id"], p["mode"], p["broker"], p["seq"]) == (
        "execution.completed", "r1", "REBALANCE", "paper", 42,
    )  # fmt: skip
    assert p["status"] == "COMPLETED_WITH_FAILURES"
    assert p["started_at"] == T0.isoformat() and p["finished_at"] == T1.isoformat()
    json.dumps(p)  # serialisable


def test_summary_counts_sum_to_total() -> None:
    s = build_payload(run(), LEGS, broker="paper", event="execution.completed", seq=1)["summary"]
    assert set(s) == {"total"} | {
        "filled", "partial", "open", "rejected", "cancelled", "failed", "unknown", "skipped",
    }  # fmt: skip
    assert s["total"] == len(LEGS) == sum(v for k, v in s.items() if k != "total")
    assert (s["filled"], s["partial"], s["open"], s["rejected"], s["unknown"], s["skipped"]) == (
        1, 1, 1, 1, 2, 1,
    )  # fmt: skip


def test_partial_leg_in_executed_and_failed_with_remainder() -> None:
    p = build_payload(run(), LEGS, broker="paper", event="execution.completed", seq=1)
    ex = {e["symbol"]: e for e in p["executed"]}
    fl = {f["symbol"]: f for f in p["failed"]}
    assert set(ex) == {"TCS", "ITC"}
    assert ex["ITC"] == {
        "symbol": "ITC", "side": "SELL", "qty": 10, "filled_qty": 4,
        "avg_price": 450.0, "broker_order_id": "B2",
    }  # fmt: skip
    assert fl["ITC"] == {
        "symbol": "ITC",
        "side": "SELL",
        "qty": 6,
        "status": "PARTIAL",
        "reason": None,
    }
    assert fl["INFY"] == {
        "symbol": "INFY", "side": "BUY", "qty": 3, "status": "REJECTED",
        "reason": "RMS: insufficient funds",
    }  # fmt: skip
    assert "TCS" not in fl and set(fl) == {"ITC", "INFY", "SBIN", "WIPRO", "LT", "AXISBANK"}


def test_all_filled_has_no_failed() -> None:
    legs = [leg(0, "TCS", Side.BUY, 1, LegStatus.FILLED, 1, avg_price=1.0)]
    p = build_payload(
        run(RunStatus.COMPLETED), legs, broker="paper", event="execution.completed", seq=3
    )
    assert p["failed"] == [] and p["summary"]["total"] == 1 == p["summary"]["filled"]


# ---------- signing ----------


def test_signature_verifies_and_rejects_tampering() -> None:
    body = b'{"a":1}'
    sig = sign("s3cret", body)
    assert sig.startswith("sha256=") and len(sig) == 7 + 64
    assert verify("s3cret", body, sig)
    assert not verify("s3cret", b'{"a":2}', sig)
    assert not verify("other", body, sig)
    assert not verify("s3cret", body, None)
    assert not verify("s3cret", body, "md5=abc")


# ---------- mock receiver ----------


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
        webhook_secret=SecretStr("hooksecret"),
    )
    return TestClient(create_app(settings))


def test_mock_webhook_accepts_valid_signature_and_keeps_last_n(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        for i in range(55):
            body = json.dumps({"run_id": f"r{i}", "seq": i}).encode()
            r = c.post(
                "/mock/webhook",
                content=body,
                headers={
                    SIGNATURE_HEADER: sign("hooksecret", body),
                    "content-type": "application/json",
                },
            )
            assert r.status_code == 200
        got = c.get("/mock/webhook").json()
    assert len(got) == 50 and got[-1]["run_id"] == "r54" and got[0]["run_id"] == "r5"


def test_mock_webhook_rejects_bad_signature(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        body = b'{"run_id":"x"}'
        r = c.post("/mock/webhook", content=body, headers={SIGNATURE_HEADER: sign("wrong", body)})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "BAD_SIGNATURE"
        assert c.get("/mock/webhook").json() == []
