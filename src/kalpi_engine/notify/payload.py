"""Notification payload (SPEC §6). Pure: built from the run + legs as finalised."""

from collections.abc import Sequence
from typing import Any

from kalpi_engine.domain.enums import LegStatus
from kalpi_engine.storage.db import Leg, Run

SUMMARY_KEYS = (
    "filled",
    "partial",
    "open",
    "rejected",
    "cancelled",
    "failed",
    "unknown",
    "skipped",
)
# Legs still non-terminal at finalisation may have an order at the broker: in doubt (D36).
_BUCKET = {s: s.value.lower() for s in LegStatus if s.value.lower() in SUMMARY_KEYS} | {
    LegStatus.PLANNED: "unknown",
    LegStatus.SUBMITTING: "unknown",
    LegStatus.SUBMITTED: "unknown",
}


def build_payload(
    run: Run, legs: Sequence[Leg], *, broker: str, event: str, seq: int
) -> dict[str, Any]:
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for lg in sorted(legs, key=lambda x: x.idx):
        summary[_BUCKET[lg.status]] += 1
        if lg.filled_qty > 0:
            executed.append(
                {
                    "symbol": lg.symbol,
                    "side": lg.side.value,
                    "qty": lg.qty,
                    "filled_qty": lg.filled_qty,
                    "avg_price": lg.avg_price,
                    "broker_order_id": lg.broker_order_id,
                }
            )
        if lg.status is not LegStatus.FILLED:
            failed.append(
                {
                    "symbol": lg.symbol,
                    "side": lg.side.value,
                    "qty": lg.qty - lg.filled_qty,  # remainder for PARTIAL
                    "status": lg.status.value,
                    "reason": lg.reason,
                }
            )
    return {
        "event": event,
        "run_id": run.id,
        "mode": run.mode.value,
        "broker": broker,
        "status": run.status.value,
        "summary": {"total": sum(summary.values())} | summary,
        "executed": executed,
        "failed": failed,
        "seq": seq,
        "started_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
