"""Error types for the Speko Python SDK."""

from __future__ import annotations

from typing import Optional


class SpekoApiError(Exception):
    """Base error for all Speko API errors."""

    def __init__(self, message: str, status: int, code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class SpekoAuthError(SpekoApiError):
    """Raised when the API key is invalid or missing."""

    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(message, status=401, code="AUTH_ERROR")


class SpekoRateLimitError(SpekoApiError):
    """Raised when the rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message, status=429, code="RATE_LIMITED")
        self.retry_after = retry_after
