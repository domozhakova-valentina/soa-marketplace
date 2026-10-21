"""Application exceptions; HTTP response DTOs are generated from OpenAPI."""
from typing import Any


class AppError(Exception):
    """An expected error safe to return to the API client.

    Only put public information into message/details. Unexpected database and
    programming exceptions should propagate to the internal error handler.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details
