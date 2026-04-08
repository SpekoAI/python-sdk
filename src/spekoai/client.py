"""SpekoAI client — sync and async."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from spekoai.errors import SpekoApiError, SpekoAuthError, SpekoRateLimitError
from spekoai.models import (
    PipelineConfig,
    Session,
    SessionDetail,
    UsageSummary,
)

DEFAULT_BASE_URL = "https://api.speko.ai"
DEFAULT_TIMEOUT = 30.0


def _handle_error(response: httpx.Response) -> None:
    """Raise typed errors from API responses."""
    try:
        data = response.json()
        message = data.get("error", response.text)
        code = data.get("code", "UNKNOWN")
    except Exception:
        message = response.text or response.reason_phrase
        code = "UNKNOWN"

    if response.status_code == 401:
        raise SpekoAuthError(message)
    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        raise SpekoRateLimitError(message, int(retry) if retry else None)
    raise SpekoApiError(message, response.status_code, code)


class _SessionsResource:
    """Sync sessions resource."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def create(
        self,
        pipeline: PipelineConfig,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Session:
        """Create a new voice session.

        Example::

            session = speko.sessions.create(
                pipeline=PipelineConfig(
                    stt=SttConfig(),
                    llm=LlmConfig(model="gpt-4o"),
                    tts=TtsConfig(provider="elevenlabs", voice="rachel"),
                ),
            )
            print(session.token)
        """
        body: dict[str, Any] = {"pipeline": pipeline.model_dump(exclude_none=True)}
        if metadata:
            body["metadata"] = metadata
        resp = self._client.post("/v1/sessions", json=body)
        if resp.status_code >= 400:
            _handle_error(resp)
        return Session.model_validate(resp.json())

    def get(self, session_id: str) -> SessionDetail:
        """Get a session by ID."""
        resp = self._client.get(f"/v1/sessions/{session_id}")
        if resp.status_code >= 400:
            _handle_error(resp)
        return SessionDetail.model_validate(resp.json())

    def end(self, session_id: str) -> dict[str, str]:
        """End an active session."""
        resp = self._client.delete(f"/v1/sessions/{session_id}")
        if resp.status_code >= 400:
            _handle_error(resp)
        return resp.json()


class _AsyncSessionsResource:
    """Async sessions resource."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def create(
        self,
        pipeline: PipelineConfig,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Session:
        """Create a new voice session."""
        body: dict[str, Any] = {"pipeline": pipeline.model_dump(exclude_none=True)}
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post("/v1/sessions", json=body)
        if resp.status_code >= 400:
            _handle_error(resp)
        return Session.model_validate(resp.json())

    async def get(self, session_id: str) -> SessionDetail:
        """Get a session by ID."""
        resp = await self._client.get(f"/v1/sessions/{session_id}")
        if resp.status_code >= 400:
            _handle_error(resp)
        return SessionDetail.model_validate(resp.json())

    async def end(self, session_id: str) -> dict[str, str]:
        """End an active session."""
        resp = await self._client.delete(f"/v1/sessions/{session_id}")
        if resp.status_code >= 400:
            _handle_error(resp)
        return resp.json()


class _UsageResource:
    """Sync usage resource."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Get usage summary for the current billing period."""
        params: dict[str, str] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        resp = self._client.get("/v1/usage", params=params)
        if resp.status_code >= 400:
            _handle_error(resp)
        return UsageSummary.model_validate(resp.json())


class _AsyncUsageResource:
    """Async usage resource."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Get usage summary for the current billing period."""
        params: dict[str, str] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        resp = await self._client.get("/v1/usage", params=params)
        if resp.status_code >= 400:
            _handle_error(resp)
        return UsageSummary.model_validate(resp.json())


class SpekoAI:
    """Synchronous SpekoAI client.

    Example::

        from spekoai import SpekoAI

        speko = SpekoAI(api_key="sk_live_...")
        session = speko.sessions.create(...)
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "spekoai-python/0.0.1",
            },
        )
        self.sessions = _SessionsResource(self._client)
        self.usage = _UsageResource(self._client)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SpekoAI:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncSpekoAI:
    """Async SpekoAI client.

    Example::

        from spekoai import AsyncSpekoAI

        async with AsyncSpekoAI(api_key="sk_live_...") as speko:
            session = await speko.sessions.create(...)
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "spekoai-python/0.0.1",
            },
        )
        self.sessions = _AsyncSessionsResource(self._client)
        self.usage = _AsyncUsageResource(self._client)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncSpekoAI:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
