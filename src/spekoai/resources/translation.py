"""Translation API sessions, on their own host (``https://translate.speko.dev``).

Requests go through the client's HTTP pool with absolute URLs, so they share its
credentials and timeouts. ``AsyncTranslationResource.connect`` opens the stream.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from uuid import uuid4

import httpx

from spekoai._http import path_id
from spekoai.errors import translation_error_from_body
from spekoai.models import TranslationSession

DEFAULT_TRANSLATION_BASE_URL = "https://translate.speko.dev"
TRANSLATION_API_VERSION = "2026-11-08"

# Waits before the second and the third attempt. The Idempotency-Key makes a retried
# POST safe: a replay returns the same session instead of creating another.
_RETRY_DELAYS = (0.25, 1.0)
# POST .../end returns within 25 seconds by contract, whatever the client's timeout.
_END_TIMEOUT_SECONDS = 30.0


def _error_code(resp: httpx.Response) -> Optional[str]:
    try:
        return resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _is_retryable(resp: httpx.Response) -> bool:
    if resp.status_code >= 500:
        return True
    return resp.status_code == 409 and _error_code(resp) == "idempotency_key_in_use"


def _session_from(resp: httpx.Response) -> TranslationSession:
    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        raise translation_error_from_body(
            body,
            resp.status_code,
            fallback_message=resp.text or resp.reason_phrase,
            request_id=resp.headers.get("Request-Id"),
        )
    return TranslationSession.model_validate(resp.json())


def _headers(idempotency_key: Optional[str] = None) -> dict[str, str]:
    headers = {"Speko-Version": TRANSLATION_API_VERSION}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _post_headers(idempotency_key: Optional[str]) -> dict[str, str]:
    return _headers(idempotency_key or str(uuid4()))


def _create_body(
    target_language: str, source_language: Optional[str], test: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {"target_language": target_language}
    if source_language is not None:
        body["source_language"] = source_language
    if test:
        body["test"] = True
    return body


class _SessionUrls:
    def __init__(self, base_url: str) -> None:
        self.collection = f"{base_url.rstrip('/')}/v1/translation/sessions"

    def one(self, session_id: str) -> str:
        return f"{self.collection}/{path_id(session_id)}"

    def end(self, session_id: str) -> str:
        return f"{self.one(session_id)}/end"


class TranslationSessionsResource:
    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._urls = _SessionUrls(base_url)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for delay in _RETRY_DELAYS:
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.TransportError:
                time.sleep(delay)
                continue
            if not _is_retryable(resp):
                return resp
            time.sleep(delay)
        return self._client.request(method, url, **kwargs)

    def create(
        self,
        *,
        target_language: str,
        source_language: Optional[str] = None,
        test: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> TranslationSession:
        """Create a session. The response has the ``client_secret`` and the
        ``stream_url`` an app connects with, within 60 seconds.

        ``test=True`` uses a deterministic fake translation and is never charged.
        """
        resp = self._request(
            "POST",
            self._urls.collection,
            json=_create_body(target_language, source_language, test),
            headers=_post_headers(idempotency_key),
        )
        return _session_from(resp)

    def retrieve(self, session_id: str) -> TranslationSession:
        """Fetch a session. ``client_secret`` is ``None``."""
        resp = self._request("GET", self._urls.one(session_id), headers=_headers())
        return _session_from(resp)

    def end(self, session_id: str, *, idempotency_key: Optional[str] = None) -> TranslationSession:
        """End a session from your server. Speko sends the translation of the audio
        it already has, then ends the stream with reason ``ended_by_api``."""
        resp = self._request(
            "POST",
            self._urls.end(session_id),
            headers=_post_headers(idempotency_key),
            timeout=_END_TIMEOUT_SECONDS,
        )
        return _session_from(resp)


class TranslationResource:
    """``sessions`` for your server. The stream helper is async-only, on
    ``AsyncSpeko.translation.connect``."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self.sessions = TranslationSessionsResource(client, base_url)


class AsyncTranslationSessionsResource:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._urls = _SessionUrls(base_url)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for delay in _RETRY_DELAYS:
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.TransportError:
                await asyncio.sleep(delay)
                continue
            if not _is_retryable(resp):
                return resp
            await asyncio.sleep(delay)
        return await self._client.request(method, url, **kwargs)

    async def create(
        self,
        *,
        target_language: str,
        source_language: Optional[str] = None,
        test: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> TranslationSession:
        """Create a session (async). See ``TranslationSessionsResource.create``."""
        resp = await self._request(
            "POST",
            self._urls.collection,
            json=_create_body(target_language, source_language, test),
            headers=_post_headers(idempotency_key),
        )
        return _session_from(resp)

    async def retrieve(self, session_id: str) -> TranslationSession:
        """Fetch a session (async). ``client_secret`` is ``None``."""
        resp = await self._request("GET", self._urls.one(session_id), headers=_headers())
        return _session_from(resp)

    async def end(
        self, session_id: str, *, idempotency_key: Optional[str] = None
    ) -> TranslationSession:
        """End a session from your server (async). See ``TranslationSessionsResource.end``."""
        resp = await self._request(
            "POST",
            self._urls.end(session_id),
            headers=_post_headers(idempotency_key),
            timeout=_END_TIMEOUT_SECONDS,
        )
        return _session_from(resp)


class AsyncTranslationResource:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.sessions = AsyncTranslationSessionsResource(client, base_url)
