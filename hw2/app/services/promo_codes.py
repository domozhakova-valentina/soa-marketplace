"""Global promo codes, with role checks and one transaction per creation."""
from datetime import timezone
from uuid import UUID

from sqlalchemy.engine import Engine

from app.errors import AppError
from app.repositories.promo_codes import PromoCodesRepository
from app.repositories.users import UsersRepository
from app.security.authorization import require_roles
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.promo_code_create import PromoCodeCreate
from marketplace_api.models.promo_code_response import PromoCodeResponse


class PromoCodesService:
    def __init__(self, engine: Engine, repository: PromoCodesRepository | None = None,
                 users: UsersRepository | None = None):
        self.engine = engine
        self.repository = repository if repository is not None else PromoCodesRepository()
        self.users = users if users is not None else UsersRepository()

    def create_promo_code(self, subject: UUID, payload: PromoCodeCreate) -> PromoCodeResponse:
        with self.engine.begin() as connection:
            user = self.users.get_by_id(connection, subject)
            if user is None:
                raise AppError("TOKEN_INVALID", "Пользователь токена не найден", 401)
            require_roles(TokenModel(sub=str(user["id"]), role=user["role"]), ("SELLER", "ADMIN"))

            fields = [{"field": name, "message": "Дата должна содержать часовой пояс"}
                      for name in ("valid_from", "valid_until") if getattr(payload, name).utcoffset() is None]
            if not fields and payload.valid_until <= payload.valid_from:
                fields.append({"field": "valid_until", "message": "Дата окончания должна быть позже даты начала"})
            if fields:
                raise AppError("VALIDATION_ERROR", "Некорректные входные данные", 400, {"fields": fields})

            values = payload.model_dump()
            values.update(discount_type=payload.discount_type.value,
                          valid_from=payload.valid_from.astimezone(timezone.utc),
                          valid_until=payload.valid_until.astimezone(timezone.utc))
            row = self.repository.create(connection, values)
            if row is None:
                raise AppError("PROMO_CODE_ALREADY_EXISTS", "Промокод с таким кодом уже существует", 409)
            # Validate the generated response before committing the insert.
            return PromoCodeResponse.model_validate(row)
