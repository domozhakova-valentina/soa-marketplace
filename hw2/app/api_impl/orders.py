"""Adapters for generated order routes; transactions run in a worker thread."""
from datetime import timedelta
import os
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from app.db.connection import get_engine
from app.security.authorization import token_subject
from app.services.orders import OrdersService
from marketplace_api.apis.orders_api_base import BaseOrdersApi
from marketplace_api.models.order_create import OrderCreate
from marketplace_api.models.order_response import OrderResponse
from marketplace_api.models.order_update import OrderUpdate


def get_orders_service() -> OrdersService:
    interval = timedelta(minutes=float(os.environ.get("ORDER_CREATE_INTERVAL_MINUTES", "5")))
    update_interval = timedelta(minutes=float(os.environ.get("ORDER_UPDATE_INTERVAL_MINUTES", "5")))
    return OrdersService(get_engine(), create_interval=interval, update_interval=update_interval)


class OrdersApi(BaseOrdersApi):
    async def _call(self, method: str, *args):
        subject = token_subject(self.token_BearerAuth)

        def work():
            return getattr(get_orders_service(), method)(subject, *args)

        return await run_in_threadpool(work)

    async def create_order(self, order_create: OrderCreate) -> OrderResponse:
        return await self._call("create_order", order_create)

    async def get_order(self, id: UUID) -> OrderResponse:
        return await self._call("get_order", id)

    async def update_order(self, id: UUID, order_update: OrderUpdate) -> OrderResponse:
        return await self._call("update_order", id, order_update)

    async def cancel_order(self, id: UUID) -> OrderResponse:
        return await self._call("cancel_order", id)
