"""Generated promo route adapter; run the whole SQL transaction in a worker."""
from starlette.concurrency import run_in_threadpool

from app.db.connection import get_engine
from app.security.authorization import token_subject
from app.services.promo_codes import PromoCodesService
from marketplace_api.apis.promo_codes_api_base import BasePromoCodesApi
from marketplace_api.models.promo_code_create import PromoCodeCreate
from marketplace_api.models.promo_code_response import PromoCodeResponse


def get_promo_codes_service() -> PromoCodesService:
    return PromoCodesService(get_engine())


class PromoCodesApi(BasePromoCodesApi):
    async def create_promo_code(self, promo_code_create: PromoCodeCreate) -> PromoCodeResponse:
        subject = token_subject(self.token_BearerAuth)

        def work():
            return get_promo_codes_service().create_promo_code(subject, promo_code_create)

        return await run_in_threadpool(work)
