from fastapi import APIRouter, Request

from kalpi_engine.brokers.base import BrokerMeta
from kalpi_engine.brokers.registry import Registry

router = APIRouter(prefix="/v1/brokers", tags=["brokers"])


@router.get("", response_model=list[BrokerMeta])
async def list_brokers(request: Request) -> list[BrokerMeta]:
    """Adapter metadata; the UI renders connect forms from `auth_mode` + `credential_fields`."""
    registry: Registry = request.app.state.registry
    return registry.metas()
