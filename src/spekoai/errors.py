"""Error types for the Speko Python SDK."""

from __future__ import annotations

from typing import Any, Optional


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


_TRANSLATION_ERRORS_PAGE = "https://docs.speko.dev/translation/errors"


class SpekoTranslationError(SpekoApiError):
    """An error from the Translation API. Branch on ``type``; ``code`` names the
    specific error, and new codes can appear within an API version. ``doc_url`` is the
    server's link for this code, or the errors page when the error started in the SDK."""

    def __init__(
        self,
        message: str,
        status: int,
        code: str = "UNKNOWN",
        *,
        type: str = "api_error",
        param: Optional[str] = None,
        doc_url: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        # Named, not super(): two subclasses also inherit SpekoAuthError or
        # SpekoRateLimitError, whose constructors take other arguments.
        SpekoApiError.__init__(self, message, status, code)
        self.type = type
        self.param = param
        self.doc_url = doc_url or _TRANSLATION_ERRORS_PAGE
        self.request_id = request_id


class SpekoTranslationInvalidRequestError(SpekoTranslationError):
    """``invalid_request_error``: the request is wrong. Fix the client."""


class SpekoTranslationAuthError(SpekoTranslationError, SpekoAuthError):
    """``authentication_error``: the credential is missing, wrong or expired."""


class SpekoTranslationPermissionError(SpekoTranslationError):
    """``permission_error``: the credential is valid but may not do this."""


class SpekoTranslationRateLimitError(SpekoTranslationError, SpekoRateLimitError):
    """``rate_limit_error``: too many realtime sessions at once. Retry when one ends."""

    retry_after: Optional[int] = None


class SpekoTranslationBalanceError(SpekoTranslationError):
    """``balance_error``: the organization's balance ran out."""


class SpekoTranslationServerError(SpekoTranslationError):
    """``api_error``: Speko, or translation, failed. Retry, or create a new session."""


class SpekoTranslationConnectionError(SpekoTranslationError):
    """The network or the stream failed. Raised by the SDK, not sent by the server, so
    ``type`` is ``connection_error`` and ``status`` is 0. ``code`` is ``network_error``,
    ``connect_failed``, ``resume_failed``, ``connection_lost``, ``protocol_error``,
    ``stream_closed``, ``end_timeout`` or ``client_too_slow``."""

    def __init__(self, message: str, code: str, *, close_code: Optional[int] = None) -> None:
        super().__init__(message, 0, code, type="connection_error")
        self.close_code = close_code


_TRANSLATION_ERROR_CLASSES: dict[str, type[SpekoTranslationError]] = {
    "invalid_request_error": SpekoTranslationInvalidRequestError,
    "authentication_error": SpekoTranslationAuthError,
    "permission_error": SpekoTranslationPermissionError,
    "rate_limit_error": SpekoTranslationRateLimitError,
    "balance_error": SpekoTranslationBalanceError,
    "api_error": SpekoTranslationServerError,
}


def translation_error_from_body(
    body: Any, status: int, *, fallback_message: str, request_id: Optional[str] = None
) -> SpekoTranslationError:
    """Build the typed error for a response body. A body that isn't the contract's
    error shape, such as one from a proxy, still gives an error with its status."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return SpekoTranslationError(fallback_message, status, request_id=request_id)
    error_type = str(error.get("type", "api_error"))
    error_class = _TRANSLATION_ERROR_CLASSES.get(error_type, SpekoTranslationError)
    return error_class(
        str(error.get("message", fallback_message)),
        status,
        str(error.get("code", "UNKNOWN")),
        type=error_type,
        param=error.get("param"),
        doc_url=error.get("doc_url"),
        request_id=error.get("request_id", request_id),
    )
