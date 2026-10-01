"""One test per row of the SPEC §4 classification table (D23, D34)."""

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx
import pytest
import respx

from kalpi_engine.brokers.base import (
    AuthMode,
    BrokerMeta,
    BrokerSession,
    HttpBrokerAdapter,
    InstrumentRef,
    Outcome,
    RateLimits,
)
from kalpi_engine.domain.enums import Exchange
from kalpi_engine.domain.errors import AmbiguousSubmit, AuthExpired, RateLimited, TransientError
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent


class FakeHttp(HttpBrokerAdapter):
    """Minimal concrete adapter; auth codes mimic Zerodha/AngelOne bodies."""

    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="fakehttp", name="Fake", auth_mode=AuthMode.NONE,
        rate_limits=RateLimits(orders_per_sec=1, reads_per_sec=1),
    )  # fmt: skip
    base_url: ClassVar[str] = "https://broker.test"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"TokenException", "AG8001"})

    def order_id_from(self, body: Any) -> str | None:
        data = body.get("data") if isinstance(body, dict) else None
        return data.get("order_id") if isinstance(data, dict) else None

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        raise NotImplementedError

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        raise NotImplementedError

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        raise NotImplementedError

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        raise NotImplementedError

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        raise NotImplementedError

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        raise NotImplementedError

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        raise NotImplementedError


A = FakeHttp()


def resp(status: int, body: Any = None, text: str | None = None, **headers: str) -> httpx.Response:
    if text is not None:
        return httpx.Response(status, text=text, headers=headers)
    return httpx.Response(status, json=body if body is not None else {}, headers=headers)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_never_sent_is_retry_safe(exc: type[httpx.HTTPError]) -> None:
    assert A.classify(exc("x"), placing=True) is Outcome.RETRY_SAFE


def test_429_is_retry_safe_with_retry_after() -> None:
    r = resp(429, {"message": "slow down"}, **{"Retry-After": "2"})
    assert A.classify(r, placing=True) is Outcome.RETRY_SAFE
    with pytest.raises(RateLimited) as ei:
        A.raise_for(Outcome.RETRY_SAFE, r)
    assert ei.value.retry_after == 2.0


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError]
)
def test_maybe_sent_is_ambiguous(exc: type[httpx.HTTPError]) -> None:
    assert A.classify(exc("x"), placing=True) is Outcome.AMBIGUOUS


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_ambiguous(status: int) -> None:
    assert A.classify(resp(status), placing=True) is Outcome.AMBIGUOUS


def test_401_is_auth_expired() -> None:
    assert A.classify(resp(401)) is Outcome.AUTH_EXPIRED


def test_403_token_exception_is_auth_expired() -> None:
    body = {"status": "error", "error_type": "TokenException", "message": "Incorrect api_key"}
    assert A.classify(resp(403, body)) is Outcome.AUTH_EXPIRED


def test_ag8001_is_auth_expired() -> None:
    body = {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None}
    assert A.classify(resp(200, body)) is Outcome.AUTH_EXPIRED


@pytest.mark.parametrize(
    ("status", "body"),
    [(400, {"error_type": "InputException"}), (403, {"error_type": "PermissionException"}),
     (404, {}), (422, {"message": "bad qty"})],
)  # fmt: skip
def test_other_4xx_is_rejected(status: int, body: dict[str, str]) -> None:
    assert A.classify(resp(status, body), placing=True) is Outcome.REJECTED


def test_200_with_known_error_code_is_rejected() -> None:
    body = {"status": False, "message": "Invalid qty", "errorcode": "AB1008"}
    assert A.classify(resp(200, body), placing=True) is Outcome.REJECTED


@pytest.mark.parametrize(
    "r",
    [resp(200, text="<html>gateway</html>"), resp(200, {"status": "success", "data": {}})],
    ids=["unparseable", "no-order-id"],
)
def test_200_on_place_without_order_id_is_ambiguous(r: httpx.Response) -> None:
    assert A.classify(r, placing=True) is Outcome.AMBIGUOUS


def test_200_with_order_id_is_ok() -> None:
    assert A.classify(resp(200, {"data": {"order_id": "123"}}), placing=True) is Outcome.OK


@respx.mock
async def test_request_maps_to_domain_errors() -> None:
    adapter = FakeHttp()
    respx.post("https://broker.test/orders").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TransientError):
        await adapter.request("POST", "/orders", placing=True)
    respx.post("https://broker.test/orders").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AmbiguousSubmit):
        await adapter.request("POST", "/orders", placing=True)
    respx.get("https://broker.test/holdings").mock(return_value=resp(401))
    with pytest.raises(AuthExpired):
        await adapter.request("GET", "/holdings")
    respx.post("https://broker.test/ok").mock(return_value=resp(200, {"data": {"order_id": "9"}}))
    assert await adapter.request("POST", "/ok", placing=True) == {"data": {"order_id": "9"}}
    await adapter.aclose()
