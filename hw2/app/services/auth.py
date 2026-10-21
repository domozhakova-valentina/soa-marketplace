"""Authentication returns only DTOs generated from the OpenAPI contract."""
from uuid import UUID

from sqlalchemy.engine import Engine

from app.errors import AppError
from app.repositories.users import UsersRepository
from app.security.passwords import dummy_password_hash, hash_password, verify_password
from app.security.tokens import TokenService
from marketplace_api.models.access_token_response import AccessTokenResponse
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.login_request import LoginRequest
from marketplace_api.models.refresh_request import RefreshRequest
from marketplace_api.models.register_request import RegisterRequest
from marketplace_api.models.token_pair_response import TokenPairResponse
from marketplace_api.models.user_response import UserResponse


def public_user(row) -> UserResponse:
    # Explicit projection keeps password_hash out of every API response.
    return UserResponse(id=row["id"], username=row["username"], role=row["role"])


class AuthService:
    def __init__(self, engine: Engine, tokens: TokenService, repository: UsersRepository | None = None):
        self.engine = engine
        self.tokens = tokens
        self.repository = repository if repository is not None else UsersRepository()

    def register_user(self, payload: RegisterRequest) -> UserResponse:
        password_hash = hash_password(payload.password)
        with self.engine.begin() as connection:
            row = self.repository.create(connection, payload.username, password_hash, "USER")
            if row is None:
                raise AppError("USER_ALREADY_EXISTS", "Имя пользователя уже занято", 409)
            return public_user(row)

    def login_user(self, payload: LoginRequest) -> TokenPairResponse:
        with self.engine.begin() as connection:
            row = self.repository.get_by_username(connection, payload.username)
            password_hash = row["password_hash"] if row is not None else dummy_password_hash()
            if not verify_password(payload.password, password_hash) or row is None:
                raise AppError("INVALID_CREDENTIALS", "Неверное имя пользователя или пароль", 401)
            return self.tokens.issue_pair(public_user(row))

    def refresh_access_token(self, payload: RefreshRequest) -> AccessTokenResponse:
        claims = self.tokens.decode_refresh(payload.refresh_token)
        with self.engine.begin() as connection:
            row = self.repository.get_by_id(connection, UUID(claims.sub))
            if row is None:
                raise AppError("REFRESH_TOKEN_INVALID", "Пользователь токена не найден", 401)
            # Refresh picks up the current DB role instead of retaining an old role.
            return self.tokens.issue_access(public_user(row))

    def authenticate_claims(self, claims: TokenModel) -> TokenModel:
        with self.engine.begin() as connection:
            row = self.repository.get_by_id(connection, UUID(claims.sub))
            if row is None or row["role"] != claims.role.value:
                raise AppError("TOKEN_INVALID", "Пользователь или роль токена недействительны", 401)
            user = public_user(row)
            return TokenModel(sub=str(user.id), role=user.role)
