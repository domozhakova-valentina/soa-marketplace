"""Verify JWTs before loading a user or executing a protected operation."""
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.errors import AppError
from app.security.tokens import get_token_service
from marketplace_api.models.extra_models import TokenModel


bearer_auth = HTTPBearer(auto_error=False)


async def get_current_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth),
) -> TokenModel:
    if credentials is None:
        raise AppError("TOKEN_INVALID", "Токен доступа отсутствует", 401)

    def work():
        # Invalid signatures/claims are rejected before any DB access.
        claims = get_token_service().decode_access(credentials.credentials)
        from app.api_impl.auth import get_auth_service
        return get_auth_service().authenticate_claims(claims)

    token = await run_in_threadpool(work)
    # Never attribute a request using unverified claims or a caller-supplied id.
    request.state.user_id = token.sub
    return token
