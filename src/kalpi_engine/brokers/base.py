"""Broker port (SPEC §4): the whole contract a broker adapter implements."""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from kalpi_engine.domain.enums import Exchange
from kalpi_engine.domain.errors import (
    AmbiguousSubmit,
    AuthExpired,
    BrokerRejected,
    KalpiError,
    RateLimited,
    TransientError,
)
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent


class AuthMode(StrEnum):
    OAUTH_REDIRECT = "OAUTH_REDIRECT"
    CREDENTIALS_TOTP = "CREDENTIALS_TOTP"
    API_KEY_SECRET = "API_KEY_SECRET"
    NONE = "NONE"


class Outcome(StrEnum):
    """Transport/response classification (D23)."""

    OK = "OK"
    RETRY_SAFE = "RETRY_SAFE"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"
    AUTH_EXPIRED = "AUTH_EXPIRED"


class RateLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    orders_per_sec: float = Field(gt=0)
    reads_per_sec: float = Field(gt=0)
    orders_per_day: int | None = None


class BrokerMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    auth_mode: AuthMode
    credential_fields: tuple[str, ...] = ()
    rate_limits: RateLimits
    requires_static_ip: bool = False
    daily_2fa: bool = False
    tag_max_len: int = Field(default=20, ge=8)
    experimental: bool = True
    live_tested: bool = False
    market_order_verified: bool = False


class BrokerSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker_id: str
    access_token: SecretStr
    expires_at: datetime | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class InstrumentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    symbol: str
    token: str
    lot_size: int = 1
    tick_size: float = 0.05


class BrokerAdapter(ABC):
    meta: ClassVar[BrokerMeta]

    async def login_url(self, state: str) -> str:
        raise NotImplementedError(f"{self.meta.id} does not use OAuth redirect")

    @abstractmethod
    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession: ...

    @abstractmethod
    async def get_holdings(self, s: BrokerSession) -> list[Holding]: ...

    @abstractmethod
    async def get_funds(self, s: BrokerSession) -> Funds | None: ...

    @abstractmethod
    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef: ...

    @abstractmethod
    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        """Return ONE broker order id. Must send params that prevent order slicing."""

    @abstractmethod
    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState: ...

    @abstractmethod
    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None: ...

    async def cancel_order(self, s: BrokerSession, broker_order_id: str) -> None:
        raise NotImplementedError(f"{self.meta.id} does not support cancel")

    async def aclose(self) -> None:
        return None


# Signals that prove the request never left this process.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_MAYBE_SENT = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError)


class HttpBrokerAdapter(BrokerAdapter, ABC):
    """Shared httpx client + classify(). Subclasses override request building/parsing only."""

    base_url: ClassVar[str] = ""
    auth_error_codes: ClassVar[frozenset[str]] = frozenset()
    timeout: ClassVar[httpx.Timeout] = httpx.Timeout(10.0, connect=5.0, pool=5.0)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # --- response parsing hooks -------------------------------------------------
    def error_code(self, body: Any) -> str | None:
        """Broker error code in a JSON body, or None if the body reports success."""
        if not isinstance(body, dict):
            return None
        for key in ("error_type", "errorcode", "errorCode", "error_code"):
            if body.get(key):
                return str(body[key])
        if body.get("status") in (False, "error", "failure", "FAILURE"):
            return str(body.get("code") or body.get("message") or "STATUS_FALSE")
        return None

    def order_id_from(self, body: Any) -> str | None:
        """Extract the broker order id from a place_order response body."""
        return None

    # --- classification (SPEC §4 table) ------------------------------------------
    def classify(self, result: httpx.Response | BaseException, *, placing: bool = False) -> Outcome:
        if isinstance(result, BaseException):
            if isinstance(result, _NEVER_SENT):
                return Outcome.RETRY_SAFE
            if isinstance(result, _MAYBE_SENT):
                return Outcome.AMBIGUOUS
            return Outcome.AMBIGUOUS if placing else Outcome.REJECTED
        status = result.status_code
        if status == 429:
            return Outcome.RETRY_SAFE
        if status >= 500:
            return Outcome.AMBIGUOUS
        body = _json_or_none(result)
        code = self.error_code(body)
        if status == 401 or (code is not None and code in self.auth_error_codes):
            return Outcome.AUTH_EXPIRED
        if status >= 400 or code is not None:
            return Outcome.REJECTED
        if placing and (body is None or self.order_id_from(body) is None):
            return Outcome.AMBIGUOUS
        return Outcome.OK

    def raise_for(self, outcome: Outcome, result: httpx.Response | BaseException) -> None:
        """Map a non-OK outcome to the domain error taxonomy."""
        if outcome is Outcome.OK:
            return
        msg = _describe(result)
        err: KalpiError
        if outcome is Outcome.RETRY_SAFE:
            if isinstance(result, httpx.Response):
                err = RateLimited(msg, retry_after=_retry_after(result))
            else:
                err = TransientError(msg)
        elif outcome is Outcome.AMBIGUOUS:
            err = AmbiguousSubmit(msg)
        elif outcome is Outcome.AUTH_EXPIRED:
            err = AuthExpired(msg)
        else:
            err = BrokerRejected(msg)
        if isinstance(result, BaseException):
            raise err from result
        raise err

    async def request(
        self, method: str, url: str, *, placing: bool = False, **kwargs: Any
    ) -> Any:
        """Send, classify, raise on non-OK; return the parsed JSON body."""
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            self.raise_for(self.classify(exc, placing=placing), exc)
            raise  # unreachable: raise_for always raises for exceptions
        self.raise_for(self.classify(resp, placing=placing), resp)
        return _json_or_none(resp)


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def _describe(result: httpx.Response | BaseException) -> str:
    if isinstance(result, httpx.Response):
        # Body is broker error text, never credentials; truncate to keep logs small.
        return f"HTTP {result.status_code}: {result.text[:200]}"
    return f"{type(result).__name__}: {result}"
