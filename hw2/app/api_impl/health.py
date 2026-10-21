from marketplace_api.apis.health_api_base import BaseHealthApi
from marketplace_api.models.health_response import HealthResponse


class HealthApi(BaseHealthApi):
    async def get_health(self) -> HealthResponse:
        return HealthResponse(status="ok")