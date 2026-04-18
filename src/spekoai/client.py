"""Speko client — sync and async.

The client mirrors the TypeScript SDK's surface:

- ``Speko.transcribe(audio, language=..., vertical=...)``
- ``Speko.synthesize(text, language=..., vertical=...)``
- ``Speko.complete(messages=..., intent=...)``
- ``Speko.usage.get()``
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Optional, Union

import httpx

from spekoai.errors import SpekoApiError, SpekoAuthError, SpekoRateLimitError
from spekoai.models import (
    ChatMessage,
    CompleteResult,
    CreditLedgerPage,
    OptimizeFor,
    OrganizationBalance,
    PipelineConstraints,
    RoutingIntent,
    SynthesizeResult,
    TranscribeResult,
    UsageSummary,
    Vertical,
)

DEFAULT_BASE_URL = "https://api.speko.ai"
DEFAULT_TIMEOUT = 30.0

try:
    _PKG_VERSION = version("spekoai")
except PackageNotFoundError:  # pragma: no cover — running from source without install
    _PKG_VERSION = "0.0.0+unknown"

USER_AGENT = f"spekoai-python/{_PKG_VERSION}"

IntentInput = Union[RoutingIntent, dict[str, Any]]
ConstraintsInput = Union[PipelineConstraints, dict[str, Any], None]
MessageInput = Union[ChatMessage, dict[str, Any]]


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        data = resp.json()
        message = data.get("error", resp.text)
        code = data.get("code", "UNKNOWN")
    except Exception:
        message = resp.text or resp.reason_phrase
        code = "UNKNOWN"
    if resp.status_code == 401:
        raise SpekoAuthError(message)
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After")
        retry_after = int(retry) if retry is not None and retry.isdigit() else None
        raise SpekoRateLimitError(message, retry_after)
    raise SpekoApiError(message, resp.status_code, code)


def _intent_from_fields(
    language: str,
    vertical: Vertical,
    optimize_for: Optional[OptimizeFor],
) -> dict[str, Any]:
    return RoutingIntent(
        language=language,
        vertical=vertical,
        optimize_for=optimize_for,
    ).model_dump(by_alias=True, exclude_none=True)


def _intent_from_input(intent: IntentInput) -> dict[str, Any]:
    model = (
        intent
        if isinstance(intent, RoutingIntent)
        else RoutingIntent.model_validate(intent)
    )
    return model.model_dump(by_alias=True, exclude_none=True)


def _constraints_payload(constraints: ConstraintsInput) -> Optional[dict[str, Any]]:
    if constraints is None:
        return None
    model = (
        constraints
        if isinstance(constraints, PipelineConstraints)
        else PipelineConstraints.model_validate(constraints)
    )
    return model.model_dump(by_alias=True, exclude_none=True)


def _messages_payload(messages: list[MessageInput]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        model = m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m)
        out.append(model.model_dump(by_alias=True, exclude_none=True))
    return out


def _complete_body(
    *,
    messages: list[MessageInput],
    intent: IntentInput,
    system_prompt: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    constraints: ConstraintsInput,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": _messages_payload(messages),
        "intent": _intent_from_input(intent),
    }
    if system_prompt is not None:
        body["systemPrompt"] = system_prompt
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["maxTokens"] = max_tokens
    cs = _constraints_payload(constraints)
    if cs is not None:
        body["constraints"] = cs
    return body


def _synthesize_body(
    *,
    text: str,
    intent: dict[str, Any],
    voice: Optional[str],
    speed: Optional[float],
    constraints: Optional[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text, "intent": intent}
    if voice is not None:
        body["voice"] = voice
    if speed is not None:
        body["speed"] = speed
    if constraints is not None:
        body["constraints"] = constraints
    return body


def _transcribe_headers(
    *,
    content_type: str,
    intent: dict[str, Any],
    constraints: Optional[dict[str, Any]],
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "X-Speko-Intent": json.dumps(intent, separators=(",", ":")),
    }
    if constraints is not None:
        headers["X-Speko-Constraints"] = json.dumps(
            constraints, separators=(",", ":")
        )
    return headers


def _usage_params(
    from_date: Optional[str], to_date: Optional[str]
) -> dict[str, str]:
    params: dict[str, str] = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return params


def _parse_synth_headers(hdrs: httpx.Headers) -> dict[str, Any]:
    raw_failover = hdrs.get("x-speko-failover-count")
    failover_count = (
        int(raw_failover)
        if raw_failover is not None and raw_failover.isdigit()
        else 0
    )
    return {
        "content_type": hdrs.get("content-type", "application/octet-stream"),
        "provider": hdrs.get("x-speko-provider", "unknown"),
        "model": hdrs.get("x-speko-model", "unknown"),
        "failover_count": failover_count,
        "scores_run_id": hdrs.get("x-speko-scores-run-id") or None,
    }


def _default_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }


def _ledger_params(
    limit: Optional[int], cursor: Optional[str]
) -> dict[str, str]:
    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if cursor:
        params["cursor"] = cursor
    return params


class _UsageResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Usage summary for the current billing period.

        Example::

            usage = speko.usage.get()
            print(usage.total_minutes, usage.total_cost)
        """
        resp = self._client.get(
            "/v1/usage", params=_usage_params(from_date, to_date)
        )
        _raise_for_status(resp)
        return UsageSummary.model_validate(resp.json())


class _AsyncUsageResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Usage summary for the current billing period."""
        resp = await self._client.get(
            "/v1/usage", params=_usage_params(from_date, to_date)
        )
        _raise_for_status(resp)
        return UsageSummary.model_validate(resp.json())


class _CreditsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get_balance(self) -> OrganizationBalance:
        """Current prepaid credit balance for the caller's org.

        Example::

            balance = speko.credits.get_balance()
            if balance.balance_usd < 0.5:
                print("Top up before running long sessions.")
        """
        resp = self._client.get("/v1/credits/balance")
        _raise_for_status(resp)
        return OrganizationBalance.model_validate(resp.json())

    def get_ledger(
        self,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> CreditLedgerPage:
        """Most-recent-first page of credit movements.

        Pass back ``next_cursor`` as ``cursor`` to fetch the next page;
        ``next_cursor is None`` means the history is exhausted.
        """
        resp = self._client.get(
            "/v1/credits/ledger", params=_ledger_params(limit, cursor)
        )
        _raise_for_status(resp)
        return CreditLedgerPage.model_validate(resp.json())


class _AsyncCreditsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_balance(self) -> OrganizationBalance:
        """Current prepaid credit balance (async)."""
        resp = await self._client.get("/v1/credits/balance")
        _raise_for_status(resp)
        return OrganizationBalance.model_validate(resp.json())

    async def get_ledger(
        self,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> CreditLedgerPage:
        """Most-recent-first page of credit movements (async)."""
        resp = await self._client.get(
            "/v1/credits/ledger", params=_ledger_params(limit, cursor)
        )
        _raise_for_status(resp)
        return CreditLedgerPage.model_validate(resp.json())


class Speko:
    """Speko client — one API, every voice provider.

    Example::

        from spekoai import Speko

        speko = Speko(api_key=os.environ["SPEKO_API_KEY"])

        result = speko.transcribe(
            audio_bytes,
            language="es-MX",
            vertical="healthcare",
        )
    """

    usage: _UsageResource
    credits: _CreditsResource

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Speko: api_key is required. Get one at "
                "https://dashboard.speko.ai/api-keys"
            )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=_default_headers(api_key),
        )
        self.usage = _UsageResource(self._client)
        self.credits = _CreditsResource(self._client)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Speko:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str,
        vertical: Vertical,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
    ) -> TranscribeResult:
        """Transcribe audio. Best STT provider auto-routed.

        The router picks the best STT provider for your
        ``(language, vertical, optimize_for)`` and fails over
        automatically.

        Example::

            with open("call.wav", "rb") as f:
                audio = f.read()
            result = speko.transcribe(
                audio,
                language="es-MX",
                vertical="healthcare",
            )
            print(result.text, result.provider, result.confidence)
        """
        intent = _intent_from_fields(language, vertical, optimize_for)
        cs = _constraints_payload(constraints)
        headers = _transcribe_headers(
            content_type=content_type, intent=intent, constraints=cs
        )
        resp = self._client.post(
            "/v1/transcribe", content=bytes(audio), headers=headers
        )
        _raise_for_status(resp)
        return TranscribeResult.model_validate(resp.json())

    def synthesize(
        self,
        text: str,
        *,
        language: str,
        vertical: Vertical,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        constraints: ConstraintsInput = None,
    ) -> SynthesizeResult:
        """Synthesize text to audio. Best TTS provider auto-routed.

        Returned ``audio`` format depends on the chosen provider —
        inspect ``content_type`` (ElevenLabs: ``audio/mpeg``;
        Cartesia: ``audio/pcm;rate=24000``).
        """
        intent = _intent_from_fields(language, vertical, optimize_for)
        cs = _constraints_payload(constraints)
        body = _synthesize_body(
            text=text, intent=intent, voice=voice, speed=speed, constraints=cs
        )
        resp = self._client.post("/v1/synthesize", json=body)
        _raise_for_status(resp)
        return SynthesizeResult(
            audio=resp.content, **_parse_synth_headers(resp.headers)
        )

    def complete(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        constraints: ConstraintsInput = None,
    ) -> CompleteResult:
        """Run an LLM completion. Best LLM provider auto-routed."""
        body = _complete_body(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            constraints=constraints,
        )
        resp = self._client.post("/v1/complete", json=body)
        _raise_for_status(resp)
        return CompleteResult.model_validate(resp.json())


class AsyncSpeko:
    """Async Speko client.

    Example::

        from spekoai import AsyncSpeko

        async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
            result = await speko.transcribe(
                audio_bytes,
                language="es-MX",
                vertical="healthcare",
            )
    """

    usage: _AsyncUsageResource
    credits: _AsyncCreditsResource

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Speko: api_key is required. Get one at "
                "https://dashboard.speko.ai/api-keys"
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=_default_headers(api_key),
        )
        self.usage = _AsyncUsageResource(self._client)
        self.credits = _AsyncCreditsResource(self._client)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncSpeko:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str,
        vertical: Vertical,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
    ) -> TranscribeResult:
        """Transcribe audio (async). Best STT provider auto-routed."""
        intent = _intent_from_fields(language, vertical, optimize_for)
        cs = _constraints_payload(constraints)
        headers = _transcribe_headers(
            content_type=content_type, intent=intent, constraints=cs
        )
        resp = await self._client.post(
            "/v1/transcribe", content=bytes(audio), headers=headers
        )
        _raise_for_status(resp)
        return TranscribeResult.model_validate(resp.json())

    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        vertical: Vertical,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        constraints: ConstraintsInput = None,
    ) -> SynthesizeResult:
        """Synthesize text to audio (async). Best TTS provider auto-routed."""
        intent = _intent_from_fields(language, vertical, optimize_for)
        cs = _constraints_payload(constraints)
        body = _synthesize_body(
            text=text, intent=intent, voice=voice, speed=speed, constraints=cs
        )
        resp = await self._client.post("/v1/synthesize", json=body)
        _raise_for_status(resp)
        return SynthesizeResult(
            audio=resp.content, **_parse_synth_headers(resp.headers)
        )

    async def complete(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        constraints: ConstraintsInput = None,
    ) -> CompleteResult:
        """Run an LLM completion (async). Best LLM provider auto-routed."""
        body = _complete_body(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            constraints=constraints,
        )
        resp = await self._client.post("/v1/complete", json=body)
        _raise_for_status(resp)
        return CompleteResult.model_validate(resp.json())
