"""AngelOne client identity headers come from config, never placeholders (C4 fix)."""

import pytest
import respx

from kalpi_engine.brokers.angelone import AngelOneBroker
from kalpi_engine.domain.errors import BrokerRejected

LOGIN = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
PARAMS = {"client_code": "C1", "pin": "1", "totp": "123456"}


async def test_create_session_names_unset_ip_mac_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("ANGELONE_CLIENT_LOCAL_IP", "ANGELONE_CLIENT_PUBLIC_IP", "ANGELONE_MAC"):
        monkeypatch.delenv(v, raising=False)
    a = AngelOneBroker(api_key="k", client_ids={"X-ClientPublicIP": "1.2.3.4"})
    async with respx.mock(assert_all_called=False) as r:
        route = r.post(LOGIN)
        with pytest.raises(BrokerRejected) as ei:
            await a.create_session(PARAMS)
        assert not route.called  # nothing sent without real identity headers
    msg = str(ei.value)
    assert "ANGELONE_CLIENT_LOCAL_IP" in msg and "ANGELONE_MAC" in msg
    assert "ANGELONE_CLIENT_PUBLIC_IP" not in msg
    await a.aclose()


async def test_headers_use_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANGELONE_CLIENT_LOCAL_IP", "10.1.1.1")
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.9")
    monkeypatch.setenv("ANGELONE_MAC", "aa:bb:cc:00:11:22")
    a = AngelOneBroker(api_key="k")
    async with respx.mock() as r:
        route = r.post(LOGIN).respond(
            200, json={"status": True, "data": {"jwtToken": "t", "refreshToken": "r"}}
        )
        await a.create_session(PARAMS)
        h = route.calls.last.request.headers
    assert (h["X-ClientLocalIP"], h["X-ClientPublicIP"], h["X-MACAddress"]) == (
        "10.1.1.1", "203.0.113.9", "aa:bb:cc:00:11:22",
    )  # fmt: skip
    await a.aclose()
