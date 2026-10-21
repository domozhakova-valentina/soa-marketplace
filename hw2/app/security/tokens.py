"""Signed JWTs with fixed algorithms, required claims and separate purposes."""
from datetime import datetime, timezone
from functools import lru_cache
import os
from uuid import UUID, uuid4

import jwt

from app.errors import AppError
from marketplace_api.models.access_token_response import AccessTokenResponse
from marketplace_api.models.extra_models import TokenModel
from marketplace_api.models.token_pair_response import TokenPairResponse
from marketplace_api.models.user_response import UserResponse
from marketplace_api.models.user_role import UserRole


ACCESS_SECONDS = 30 * 60
REFRESH_SECONDS = 14 * 24 * 60 * 60
ALGORITHM = "HS256"
ISSUER = "marketplace-api"
AUDIENCE = "marketplace-clients"
REQUIRED_CLAIMS = ["sub", "role", "token_type", "exp", "iat", "jti", "iss", "aud"]


class TokenService:
    def __init__(self, secret: str):
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        self._secret = secret

    def _encode(self, user: UserResponse, purpose: str, lifetime: int) -> str:
        now = int(datetime.now(timezone.utc).timestamp())
        return jwt.encode({
            "sub": str(user.id), "role": user.role.value, "token_type": purpose,
            "iat": now, "exp": now + lifetime, "jti": str(uuid4()),
            "iss": ISSUER, "aud": AUDIENCE,
        }, self._secret, algorithm=ALGORITHM)

    def issue_pair(self, user: UserResponse) -> TokenPairResponse:
        return TokenPairResponse(
            access_token=self._encode(user, "access", ACCESS_SECONDS),
            refresh_token=self._encode(user, "refresh", REFRESH_SECONDS),
            token_type="bearer", expires_in=ACCESS_SECONDS, refresh_expires_in=REFRESH_SECONDS,
        )

    def issue_access(self, user: UserResponse) -> AccessTokenResponse:
        return AccessTokenResponse(access_token=self._encode(user, "access", ACCESS_SECONDS),
                                   token_type="bearer", expires_in=ACCESS_SECONDS)

    def _decode(self, encoded: str, purpose: str) -> TokenModel:
        invalid_code = "TOKEN_INVALID" if purpose == "access" else "REFRESH_TOKEN_INVALID"
        invalid_message = "Невалидный токен доступа" if purpose == "access" else "Невалидный refresh-токен"
        expired = False
        options = {"require": REQUIRED_CLAIMS, "strict_aud": True}
        try:
            try:
                payload = jwt.decode(encoded, self._secret, algorithms=[ALGORITHM],
                                     issuer=ISSUER, audience=AUDIENCE, options=options)
            except jwt.ExpiredSignatureError:
                # Still verify signature, purpose and remaining claims before
                # describing the token as expired (e.g. refresh is not access).
                payload = jwt.decode(encoded, self._secret, algorithms=[ALGORITHM],
                                     issuer=ISSUER, audience=AUDIENCE,
                                     options={**options, "verify_exp": False})
                expired = True
            if payload["token_type"] != purpose:
                raise ValueError("Wrong token purpose")
            if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
                raise ValueError("JWT dates must be integer NumericDates")
            if payload["exp"] <= payload["iat"]:
                raise ValueError("Invalid token lifetime")
            subject = str(UUID(payload["sub"]))
            UUID(payload["jti"])
            role = UserRole(payload["role"])
        except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError, OverflowError) as error:
            raise AppError(invalid_code, invalid_message, 401) from error
        if expired:
            if purpose == "access":
                raise AppError("TOKEN_EXPIRED", "Срок действия токена истёк", 401)
            raise AppError("REFRESH_TOKEN_INVALID", "Refresh-токен истёк", 401)
        return TokenModel(sub=subject, role=role)

    def decode_access(self, encoded: str) -> TokenModel:
        return self._decode(encoded, "access")

    def decode_refresh(self, encoded: str) -> TokenModel:
        return self._decode(encoded, "refresh")


@lru_cache
def get_token_service() -> TokenService:
    return TokenService(os.environ["JWT_SECRET"])
