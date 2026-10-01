"""B7 fault-injection suite: randomised 100-leg runs, invariants I1-I4, I7 (PLAN §Invariants).

Faults are assigned per leg tag from a seeded RNG, so each seed is reproducible regardless of
the order concurrent legs reach the broker.
"""

import random
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers import paper
from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import PaperBroker, _Order
from kalpi_engine.domain.enums import LegStatus, Side
from kalpi_engine.domain.errors import AmbiguousSubmit, BrokerRejected, RateLimited
from kalpi_engine.domain.models import BrokerOrderState, OrderIntent
from kalpi_engine.domain.status import run_status
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import create_all, make_engine, make_sessionmaker
from tests.engine.fakes import Harness

SM = async_sessionmaker[AsyncSession]
N_LEGS = 100
SEEDS = range(20)
SYMBOLS = [f"CHAOS{i:03d}" for i in range(N_LEGS)]
UNRESOLVED = {LegStatus.PLANNED, LegStatus.SUBMITTING, LegStatus.SUBMITTED}
# Cumulative fault mix from the bead: 20% 429, 5% ambiguous, 10% reject, 5% partial, 3% late.
MIX = [(0.20, "429"), (0.25, "timeout"), (0.35, "reject"), (0.40, "partial"), (0.43, "late")]
LATE_MISSES = 4  # 3 executor tag lookups + 1 finalise recheck, then visible to the sweep


class ChaosPaper(PaperBroker):
    """Paper broker with per-tag faults and broker-side invariant probes."""

    def __init__(self, sm: SM) -> None:
        super().__init__()
        self.sm = sm
        self.run_id = ""
        self.kind: dict[str, str] = {}
        self.rate_limits: dict[str, int] = {}
        self.hidden: dict[str, int] = {}
        self.calls: Counter[str] = Counter()
        self.violations: list[str] = []

    def assign(self, rng: random.Random, legs: list[Any]) -> None:
        for lg in sorted(legs, key=lambda x: x.idx):
            r = rng.random()
            kind = next((k for p, k in MIX if r < p), "ok")
            if kind == "timeout":
                kind = rng.choice(["timeout_after", "timeout_before"])
            self.kind[lg.tag] = kind
            if kind == "429":
                self.rate_limits[lg.tag] = rng.randint(1, 4)  # 4 exhausts max_attempts
            elif kind == "partial":
                self.faults.partial_fill[lg.symbol] = lg.qty // 2
            elif kind == "late":
                self.hidden[lg.tag] = LATE_MISSES

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        if intent.side is Side.BUY:
            await self._probe_barrier(intent.tag)
        tag = intent.tag
        self.calls[tag] += 1
        kind = self.kind.get(tag, "ok")
        if kind == "429" and self.calls[tag] <= self.rate_limits[tag]:
            self.place_calls += 1
            raise RateLimited("chaos: 429", retry_after=0.0)
        if kind == "reject":
            self.place_calls += 1
            raise BrokerRejected("chaos: reject")
        if kind == "timeout_before":
            self.place_calls += 1
            raise AmbiguousSubmit("chaos: timeout before accept")
        order_id = await super().place_order(s, intent)
        if kind in ("timeout_after", "late"):
            raise AmbiguousSubmit("chaos: timeout after accept")
        return order_id

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        if self.hidden.get(tag, 0) > 0:
            self.hidden[tag] -= 1
            return None
        for order in self._orders.values():  # a late order fills once it becomes visible
            if order.intent.tag == tag:
                self._try_fill(order)
        return await super().find_order_by_tag(s, tag)

    def _try_fill(self, order: _Order) -> None:
        if self.hidden.get(order.intent.tag, 0) > 0:
            return  # late fill: stays OPEN while the engine cannot see it
        super()._try_fill(order)

    async def _probe_barrier(self, tag: str) -> None:
        """I3: a buy reaching the broker while any sell is unresolved is a violation."""
        async with self.sm() as s:
            legs = await repo.get_legs(s, self.run_id)
        open_sells = [lg.tag for lg in legs if lg.side is Side.SELL and lg.status in UNRESOLVED]
        if open_sells:
            self.violations.append(f"buy {tag} placed while sells unresolved: {open_sells}")

    def orders_per_tag(self) -> Counter[str]:
        return Counter(o.intent.tag for o in self._orders.values())


@pytest.fixture
def chaos_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paper, "PRICES", {sym: 100.0 for sym in SYMBOLS})


async def _sm(tmp_path: Path, seed: int) -> tuple[Any, SM]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / f'chaos{seed}.db'}")
    await create_all(eng)
    return eng, make_sessionmaker(eng)


def _instructions(rng: random.Random) -> tuple[list[dict[str, Any]], dict[str, int]]:
    n_sells = rng.randint(30, 50)
    ins: list[dict[str, Any]] = []
    holdings: dict[str, int] = {}
    for i, sym in enumerate(SYMBOLS):
        qty = rng.randint(2, 10)
        if i < n_sells:
            holdings[sym] = qty + rng.randint(0, 5)
            ins.append({"symbol": sym, "action": "SELL", "quantity": qty})
        else:
            ins.append({"symbol": sym, "action": "BUY", "quantity": qty})
    rng.shuffle(ins)
    return ins, holdings


def _check_notification(payload: Mapping[str, Any], legs: list[Any]) -> None:
    """I4: every non-filled leg listed (PARTIAL in executed and failed); counts sum to total."""
    summary = payload["summary"]
    assert summary["total"] == len(legs) == sum(v for k, v in summary.items() if k != "total")
    not_filled = sorted(lg.symbol for lg in legs if lg.status is not LegStatus.FILLED)
    assert sorted(f["symbol"] for f in payload["failed"]) == not_filled
    executed = {e["symbol"] for e in payload["executed"]}
    for lg in legs:
        if lg.status is LegStatus.PARTIAL:
            assert lg.symbol in executed
        assert summary[lg.status.value.lower()] >= 1
    assert payload["status"] == run_status(lg.status for lg in legs).value


async def _assert_invariants(h: Harness, broker: ChaosPaper, run_id: str) -> None:
    run, legs, _, outbox = await h.state(run_id)
    per_tag = broker.orders_per_tag()
    assert max(per_tag.values(), default=0) <= 1, "I1 duplicate order for a tag"  # I1
    assert not [lg.tag for lg in legs if lg.status in UNRESOLVED], "I2 leg left unresolved"
    assert broker.violations == [], broker.violations  # I3
    assert run.status is run_status(lg.status for lg in legs)  # I7
    assert outbox, "no notification written"
    _check_notification(outbox[-1].payload_json, legs)  # I4
    for lg in legs:  # an ambiguous submit is never resent
        if broker.kind[lg.tag] in ("timeout_after", "timeout_before", "late"):
            assert broker.calls[lg.tag] <= 1, f"{lg.tag} resubmitted"


@pytest.mark.parametrize("seed", SEEDS)
async def test_randomised_run_keeps_money_invariants(
    tmp_path: Path, chaos_prices: None, seed: int
) -> None:
    rng = random.Random(seed)
    eng, sm = await _sm(tmp_path, seed)
    try:
        h = Harness(sm)
        broker = h.broker = ChaosPaper(sm)
        ins, holdings = _instructions(rng)
        halt = rng.random() < 0.25
        run_id = await h.start("REBALANCE", ins, holdings=holdings, halt_on_sell_failure=halt)
        broker.run_id = run_id
        broker.assign(rng, (await h.state(run_id))[1])

        await h.executor().run(run_id)
        await _assert_invariants(h, broker, run_id)

        late = [t for t, k in broker.kind.items() if k == "late"]
        _, legs, _, _ = await h.state(run_id)
        placed_late = [lg for lg in legs if lg.tag in late and broker.calls[lg.tag]]
        assert all(lg.status is LegStatus.UNKNOWN for lg in placed_late)

        h.clock.advance(61)  # recheck sweep due (T+60)
        await h.reconciler().sweep(run_id)
        await _assert_invariants(h, broker, run_id)

        _, legs, _, outbox = await h.state(run_id)
        for lg in placed_late:  # late fill adopted by reconcile, never resubmitted
            leg = next(x for x in legs if x.id == lg.id)
            assert leg.status in (LegStatus.FILLED, LegStatus.PARTIAL), leg.status
            assert broker.calls[lg.tag] == 1 and broker.orders_per_tag()[lg.tag] == 1
        if placed_late:
            assert outbox[-1].payload_json["event"] == "execution.updated"
    finally:
        await eng.dispose()
