"""Generated API adapter; synchronous transactions run entirely in a worker."""
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from app.db.connection import get_engine
from app.security.authorization import token_subject
from app.services.products import ProductsService
from marketplace_api.apis.products_api_base import BaseProductsApi
from marketplace_api.models.product_create import ProductCreate
from marketplace_api.models.product_page import ProductPage
from marketplace_api.models.product_response import ProductResponse
from marketplace_api.models.product_status import ProductStatus
from marketplace_api.models.product_update import ProductUpdate


def get_products_service() -> ProductsService:
    return ProductsService(get_engine())


class ProductsApi(BaseProductsApi):
    async def _call(self, method: str, *args):
        subject = token_subject(self.token_BearerAuth)

        def work():
            return getattr(get_products_service(), method)(subject, *args)

        return await run_in_threadpool(work)

    async def create_product(self, product_create: ProductCreate) -> ProductResponse:
        return await self._call("create_product", product_create)

    async def get_product(self, id: UUID) -> ProductResponse:
        return await self._call("get_product", id)

    async def update_product(self, id: UUID, product_update: ProductUpdate) -> ProductResponse:
        return await self._call("update_product", id, product_update)

    async def list_products(self, page: int, size: int, status: ProductStatus | None,
                           category: str | None) -> ProductPage:
        return await self._call("list_products", page, size, status, category)

    async def archive_product(self, id: UUID) -> None:
        await self._call("archive_product", id)
