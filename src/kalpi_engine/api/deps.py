import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from kalpi_engine.api.errors import ApiError
from kalpi_engine.config import Settings
from kalpi_engine.service import Runtime


def api_key_map(settings: Settings) -> dict[str, str]:
    """API_KEYS="user1:key1,user2:key2" -> {key: user_id}."""
    out: dict[str, str] = {}
    for pair in settings.api_keys.get_secret_value().split(","):
        user, sep, key = pair.strip().partition(":")
        if sep and user and key:
            out[key] = user
    return out


async def current_user(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
    if x_api_key:
        for key, user in api_key_map(request.app.state.settings).items():
            if hmac.compare_digest(key.encode(), x_api_key.encode()):
                return user
    raise ApiError(401, "UNAUTHORIZED", "missing or invalid X-API-Key")


def get_runtime(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    return rt


User = Annotated[str, Depends(current_user)]
Rt = Annotated[Runtime, Depends(get_runtime)]
