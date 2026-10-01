"""Zerodha specifics beyond the shared contract suite (C1)."""

import pytest
import respx
from pydantic import SecretStr

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.zerodha import ZerodhaBroker, _state
from kalpi_engine.domain.enums import OrderStatus

B = "https://api.kite.trade"


def test_meta_is_honest() -> None:
    m = ZerodhaBroker.meta
    assert m.experimental and not m.live_tested and m.requires_static_ip


@pytest.mark.parametrize(
    ("row", "status", "filled"),
    [
        ({"status": "COMPLETE", "filled_quantity": 5}, OrderStatus.FILLED, 5),
        ({"status": "OPEN", "filled_quantity": 2, "pending_quantity": 3}, OrderStatus.PARTIAL, 2),
        ({"status": "OPEN PENDING", "filled_quantity": 0}, OrderStatus.OPEN, 0),
        ({"status": "WEIRD", "filled_quantity": 0}, OrderStatus.OPEN, 0),
        ({"status": "REJECTED"}, OrderStatus.REJECTED, 0),
        ({"status": "CANCELLED"}, OrderStatus.CANCELLED, 0),
    ],
)
def test_status_mapping(row: dict[str, object], status: OrderStatus, filled: int) -> None:
    s = _state({"order_id": "1", **row})
    assert (s.status, s.filled_qty) == (status, filled)


async def test_login_url_and_funds() -> None:
    a = ZerodhaBroker(api_key="k", api_secret="s")
    url = await a.login_url("st8")
    assert url.startswith("https://kite.zerodha.com/connect/login?v=3&api_key=k")
    assert "st8" in url
    async with respx.mock() as r:
        r.get(f"{B}/user/margins/equity").respond(
            200, json={"status": "success", "data": {"available": {"cash": 1234.5}}}
        )
        s = BrokerSession(broker_id="zerodha", access_token=SecretStr("t"))
        funds = await a.get_funds(s)
        assert funds and funds.available_cash == 1234.5
        assert r.calls.last.request.headers["Authorization"] == "token k:t"
    await a.aclose()
