"""Shared identity, role and order ownership checks."""
from collections.abc import Sequence
from uuid import UUID

from app.errors import AppError
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.user_role import UserRole


def token_subject(token: TokenModel | None) -> UUID:
    if not isinstance(token, TokenModel):
        raise AppError("TOKEN_INVALID", "Невалидный токен доступа", 401)
    try:
        return UUID(token.sub)
    except (ValueError, TypeError, AttributeError) as error:
        raise AppError("TOKEN_INVALID", "Невалидный токен доступа", 401) from error


def require_roles(current_user: TokenModel, allowed_roles: Sequence[str]) -> None:
    if not isinstance(current_user, TokenModel):
        raise AppError("TOKEN_INVALID", "Невалидный токен доступа", 401)
    if current_user.role.value not in allowed_roles:
        raise AppError("ACCESS_DENIED", "Недостаточно прав", 403)


def require_order_owner(current_user: TokenModel, owner_id: UUID) -> None:
    require_roles(current_user, ("USER", "ADMIN"))
    if current_user.role != UserRole.ADMIN and UUID(current_user.sub) != owner_id:
        raise AppError("ORDER_OWNERSHIP_VIOLATION", "Заказ принадлежит другому пользователю", 403)
