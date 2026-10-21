"""Generated authentication API adapter."""
from starlette.concurrency import run_in_threadpool

from app.db.connection import get_engine
from app.security.tokens import get_token_service
from app.services.auth import AuthService
from marketplace_api.apis.auth_api_base import BaseAuthApi
from marketplace_api.models.access_token_response import AccessTokenResponse
from marketplace_api.models.login_request import LoginRequest
from marketplace_api.models.refresh_request import RefreshRequest
from marketplace_api.models.register_request import RegisterRequest
from marketplace_api.models.token_pair_response import TokenPairResponse
from marketplace_api.models.user_response import UserResponse


def get_auth_service() -> AuthService:
    return AuthService(get_engine(), get_token_service())


class AuthApi(BaseAuthApi):
    async def _call(self, method: str, payload):
        def work():
            return getattr(get_auth_service(), method)(payload)
        return await run_in_threadpool(work)

    async def register_user(self, register_request: RegisterRequest) -> UserResponse:
        return await self._call("register_user", register_request)

    async def login_user(self, login_request: LoginRequest) -> TokenPairResponse:
        return await self._call("login_user", login_request)

    async def refresh_access_token(self, refresh_request: RefreshRequest) -> AccessTokenResponse:
        return await self._call("refresh_access_token", refresh_request)
