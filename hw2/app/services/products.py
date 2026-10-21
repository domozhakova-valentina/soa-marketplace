"""Product business rules; each public operation owns one transaction."""
from uuid import UUID

from sqlalchemy.engine import Engine

from app.errors import AppError
from app.repositories.products import ProductsRepository
from marketplace_api.models.product_create import ProductCreate
from marketplace_api.models.product_page import ProductPage
from marketplace_api.models.product_response import ProductResponse
from marketplace_api.models.product_status import ProductStatus
from marketplace_api.models.product_update import ProductUpdate
from marketplace_api.models.user_response import UserResponse
from marketplace_api.models.user_role import UserRole


class ProductsService:
    def __init__(self, engine: Engine, repository: ProductsRepository | None = None):
        self.engine = engine
        self.repository = repository if repository is not None else ProductsRepository()

    def _current_user(self, connection, subject: UUID) -> UserResponse:
        row = self.repository.get_user(connection, subject)
        if row is None:
            raise AppError("TOKEN_INVALID", "Пользователь токена не найден", 401)
        return UserResponse.model_validate(row)

    @staticmethod
    def _allow_write(user: UserResponse, seller_id: UUID | None = None) -> None:
        if user.role == UserRole.ADMIN:
            return
        if user.role == UserRole.SELLER and (seller_id is None or seller_id == user.id):
            return
        raise AppError("ACCESS_DENIED", "Недостаточно прав", 403)

    def _get_product(self, connection, product_id: UUID, *, for_update=False):
        row = self.repository.get(connection, product_id, for_update=for_update)
        if row is None:
            raise AppError("PRODUCT_NOT_FOUND", "Товар не найден", 404)
        return row

    def create_product(self, subject: UUID, payload: ProductCreate) -> ProductResponse:
        with self.engine.begin() as connection:
            user = self._current_user(connection, subject)
            seller_id = payload.seller_id if payload.seller_id is not None else user.id
            self._allow_write(user, seller_id)
            seller = self.repository.get_user(connection, seller_id)
            if seller is None or seller["role"] not in ("SELLER", "ADMIN"):
                raise AppError("VALIDATION_ERROR", "Некорректные входные данные", 400, {
                    "fields": [{"field": "seller_id", "message": "Продавец не найден или имеет неподходящую роль"}]
                })
            values = payload.model_dump()
            values.update(status=payload.status.value, seller_id=seller_id)
            row = self.repository.create(connection, values)
            # Validate before commit: an invalid server response must roll back.
            return ProductResponse.model_validate(row)

    def get_product(self, subject: UUID, product_id: UUID) -> ProductResponse:
        with self.engine.begin() as connection:
            self._current_user(connection, subject)
            return ProductResponse.model_validate(self._get_product(connection, product_id))

    def update_product(self, subject: UUID, product_id: UUID, payload: ProductUpdate) -> ProductResponse:
        with self.engine.begin() as connection:
            user = self._current_user(connection, subject)
            current = self._get_product(connection, product_id, for_update=True)
            self._allow_write(user, current["seller_id"])
            values = payload.model_dump()
            values["status"] = payload.status.value
            row = self.repository.update(connection, product_id, values)
            return ProductResponse.model_validate(row)

    def list_products(self, subject: UUID, page: int, size: int,
                      status: ProductStatus | None, category: str | None) -> ProductPage:
        with self.engine.begin() as connection:
            self._current_user(connection, subject)
            rows, total = self.repository.list(connection, page, size,
                                               status.value if status is not None else None, category)
            return ProductPage(items=[ProductResponse.model_validate(row) for row in rows],
                               total_elements=total, page=page, size=size)

    def archive_product(self, subject: UUID, product_id: UUID) -> None:
        with self.engine.begin() as connection:
            user = self._current_user(connection, subject)
            current = self._get_product(connection, product_id, for_update=True)
            self._allow_write(user, current["seller_id"])
            if current["status"] != "ARCHIVED":
                self.repository.archive(connection, product_id)
