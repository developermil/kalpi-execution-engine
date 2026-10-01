"""Adapter contract harness (C0). Shared; a new broker never edits this file.

A broker supplies ONE fixture module `tests/contract/fixtures/<broker_id>.py` that *arranges*
conditions (respx routes for HTTP brokers, fault knobs for Paper). The cases here perform the
adapter call and assert the domain outcome, so every broker is held to the same contract.

Fixture module API (all hooks are `async def hook(env, ...)`):
  BROKER_ID: str                 must equal the adapter's meta.id
  SIGNS_REQUESTS: bool           True -> must define checksum_vectors()
  make_adapter() -> BrokerAdapter
  setup(env)                     optional: mock the instrument master etc.
  login(env) -> params           mock the token exchange; params for create_session
  holdings(env, s) -> {symbol: sellable_qty}
  place_ok(env, s, intent) -> PlaceCheck   mock success; expose the outgoing payload
  rate_limited / auth_expired / reject / error_200 / timeout (env, s, intent) -> None
  tag_hit(env, s, tag) -> broker_order_id ; tag_miss(env, s, tag) -> None
  resolve(env) -> (symbol, token)  a symbol the master knows, and its broker token
  checksum_vectors() -> [(computed, expected)]   (only when SIGNS_REQUESTS)
"""

import importlib.util
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import respx

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.enums import Exchange, OrderType, Phase, Side
from kalpi_engine.domain.errors import (
    AmbiguousSubmit,
    AuthExpired,
    BrokerRejected,
    InsufficientFunds,
    InvalidOrder,
    RateLimited,
    UnknownSymbol,
)
from kalpi_engine.domain.models import OrderIntent

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REJECTS = (BrokerRejected, InvalidOrder, InsufficientFunds)
REQUIRED_HOOKS = (
    "make_adapter", "login", "holdings", "place_ok", "rate_limited", "auth_expired",
    "reject", "error_200", "timeout", "tag_hit", "tag_miss", "resolve",
)  # fmt: skip


@dataclass
class Env:
    adapter: BrokerAdapter
    router: respx.MockRouter


@dataclass
class PlaceCheck:
    order_id: str  # the id the mocked broker returns
    sent: Callable[[], Mapping[str, Any]]  # the payload actually sent (read after the call)
    expect: Mapping[str, Any]  # broker-specific fields that MUST be in it (D24)


def discover_fixtures(*dirs: Path) -> dict[str, ModuleType]:
    """Load every `<id>.py` in the fixture dirs by filename; no registry file to edit."""
    found: dict[str, ModuleType] = {}
    for d in dirs or (FIXTURES_DIR,):
        for path in sorted(d.glob("[!_]*.py")):
            spec = importlib.util.spec_from_file_location(f"contract_fixture_{path.stem}", path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            found[mod.BROKER_ID] = mod
    return found


def missing_hooks(fx: ModuleType) -> list[str]:
    names = [*REQUIRED_HOOKS, *(["checksum_vectors"] if getattr(fx, "SIGNS_REQUESTS", 0) else [])]
    return [n for n in names if not callable(getattr(fx, n, None))]


def hook(fx: ModuleType, name: str) -> Callable[..., Any]:
    fn = getattr(fx, name, None)
    if not callable(fn):
        pytest.fail(f"contract fixture {fx.__name__} is missing required hook {name!r}")
    return fn


def intent(tag: str = "KCONTRACT0000001", side: Side = Side.BUY, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        leg_id="leg-1", phase=Phase(side.value), symbol="RELIANCE", exchange=Exchange.NSE,
        side=side, quantity=qty, order_type=OrderType.MARKET, tag=tag,
    )  # fmt: skip


Body = Callable[[Env, BrokerSession], Awaitable[None]]


async def _with_session(fx: ModuleType, body: Body) -> None:
    async with respx.mock(assert_all_called=False) as router:
        env = Env(adapter=hook(fx, "make_adapter")(), router=router)
        try:
            if callable(getattr(fx, "setup", None)):
                await fx.setup(env)
            params = await hook(fx, "login")(env)
            s = await env.adapter.create_session(params)
            await body(env, s)
        finally:
            await env.adapter.aclose()


# ---------- cases: each takes a fixture module and raises/fails on contract breach ----------


async def case_meta(fx: ModuleType) -> None:
    assert not missing_hooks(fx), f"missing hooks: {missing_hooks(fx)}"
    assert hook(fx, "make_adapter")().meta.id == fx.BROKER_ID


async def case_login(fx: ModuleType) -> None:
    async def body(env: Env, s: BrokerSession) -> None:
        assert s.broker_id == fx.BROKER_ID
        assert s.access_token.get_secret_value()

    await _with_session(fx, body)


async def case_checksum(fx: ModuleType) -> None:
    if not fx.SIGNS_REQUESTS:
        return
    vectors = hook(fx, "checksum_vectors")()
    assert vectors, "a signing broker needs at least one checksum test vector"
    for computed, expected in vectors:
        assert computed == expected


async def case_holdings(fx: ModuleType) -> None:
    async def body(env: Env, s: BrokerSession) -> None:
        expected = await hook(fx, "holdings")(env, s)
        assert expected, "holdings case must expect at least one holding"
        got = {h.symbol: h.sellable_qty for h in await env.adapter.get_holdings(s)}
        assert got == expected

    await _with_session(fx, body)


async def case_place_ok(fx: ModuleType) -> None:
    async def body(env: Env, s: BrokerSession) -> None:
        it = intent()
        check: PlaceCheck = await hook(fx, "place_ok")(env, s, it)
        assert check.expect, "place_ok must assert at least one outgoing payload field"
        assert await env.adapter.place_order(s, it) == check.order_id
        sent = check.sent()
        wrong = {k: sent.get(k) for k, v in check.expect.items() if sent.get(k) != v}
        assert not wrong, f"outgoing payload differs from expected on {wrong}"

    await _with_session(fx, body)


def _raises(name: str, exc: type[BaseException] | tuple[type[BaseException], ...]) -> Any:
    async def case(fx: ModuleType) -> None:
        async def body(env: Env, s: BrokerSession) -> None:
            it = intent()
            await hook(fx, name)(env, s, it)
            with pytest.raises(exc):
                if name == "auth_expired":
                    await env.adapter.get_holdings(s)
                else:
                    await env.adapter.place_order(s, it)

        await _with_session(fx, body)

    return case


async def case_tag_lookup(fx: ModuleType) -> None:
    async def body(env: Env, s: BrokerSession) -> None:
        oid = await hook(fx, "tag_hit")(env, s, "KTAGHIT000000001")
        hit = await env.adapter.find_order_by_tag(s, "KTAGHIT000000001")
        assert hit is not None and hit.broker_order_id == oid
        await hook(fx, "tag_miss")(env, s, "KTAGMISS00000001")
        assert await env.adapter.find_order_by_tag(s, "KTAGMISS00000001") is None

    await _with_session(fx, body)


async def case_resolve(fx: ModuleType) -> None:
    async def body(env: Env, s: BrokerSession) -> None:
        symbol, token = await hook(fx, "resolve")(env)
        assert (await env.adapter.resolve_instrument(Exchange.NSE, symbol)).token == token
        with pytest.raises(UnknownSymbol):
            await env.adapter.resolve_instrument(Exchange.NSE, "NOSUCHSYMBOLX")

    await _with_session(fx, body)


CASES: dict[str, Callable[[ModuleType], Awaitable[None]]] = {
    "meta": case_meta,
    "login": case_login,
    "checksum": case_checksum,
    "holdings": case_holdings,
    "place_ok": case_place_ok,
    "rate_limited": _raises("rate_limited", RateLimited),
    "auth_expired": _raises("auth_expired", AuthExpired),
    "reject": _raises("reject", REJECTS),
    "error_200": _raises("error_200", (*REJECTS, AuthExpired)),
    "timeout": _raises("timeout", AmbiguousSubmit),
    "tag_lookup": case_tag_lookup,
    "resolve": case_resolve,
}
