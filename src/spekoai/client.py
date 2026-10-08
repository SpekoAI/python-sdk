"""Speko client — sync and async.

The client mirrors the TypeScript SDK's surface:

- ``Speko.transcribe(audio, language=...)`` / ``transcribe_stream``
- ``Speko.synthesize(text, language=...)`` / ``synthesize_stream``
- ``Speko.complete(messages=..., intent=...)`` / ``complete_stream``
- Resource namespaces: ``usage``, ``credits``, ``voice``, ``voices``,
  ``sessions``, ``phone_numbers``, ``agents``, ``knowledge_bases``,
  ``calls``, ``callbacks``, ``webhooks``, ``sms``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Optional, Union
from uuid import uuid4

import httpx

from spekoai._http import (
    aiter_sse,
    araise_for_status_streamed,
    iter_sse,
    raise_for_status,
    raise_for_status_streamed,
)
from spekoai._user_agent import USER_AGENT
from spekoai.errors import SpekoApiError
from spekoai.models import (
    ChatMessage,
    ChatTool,
    ChatToolChoice,
    CompleteResult,
    CompleteStreamDelta,
    CompleteStreamDone,
    CompleteStreamEvent,
    CompleteStreamMeta,
    CompleteStreamServerToolCall,
    CompleteStreamToolCall,
    OptimizeFor,
    PipelineConstraints,
    RealtimeConnectParams,
    RealtimeSessionInfo,
    ReasoningEffort,
    RoutingIntent,
    StreamError,
    SynthesizeResult,
    TranscribeResult,
    TranscribeStreamDone,
    TranscribeStreamEvent,
    TranscribeStreamMeta,
    TranscribeStreamTranscript,
)
from spekoai.realtime import AsyncRealtimeSession, _drain_background_tasks, open_realtime_session
from spekoai.resources import (
    AgentsResource,
    AsyncAgentsResource,
    AsyncCallbacksResource,
    AsyncCallsResource,
    AsyncCreditsResource,
    AsyncKnowledgeBasesResource,
    AsyncPhoneNumbersResource,
    AsyncSessionsResource,
    AsyncSmsResource,
    AsyncUsageResource,
    AsyncVoiceResource,
    AsyncVoicesResource,
    AsyncWebhooksResource,
    CallbacksResource,
    CallsResource,
    CreditsResource,
    KnowledgeBasesResource,
    PhoneNumbersResource,
    SessionsResource,
    SmsResource,
    UsageResource,
    VoiceResource,
    VoicesResource,
    WebhooksResource,
)

DEFAULT_BASE_URL = "https://api.speko.dev"
DEFAULT_TIMEOUT = 30.0

IntentInput = Union[RoutingIntent, dict[str, Any]]
ConstraintsInput = Union[PipelineConstraints, dict[str, Any], None]
MessageInput = Union[ChatMessage, dict[str, Any]]
ToolInput = Union[ChatTool, dict[str, Any]]
RealtimeInput = Union[RealtimeConnectParams, dict[str, Any]]


def _intent_from_fields(
    language: str,
    region: Optional[str],
    optimize_for: Optional[OptimizeFor],
) -> dict[str, Any]:
    return RoutingIntent(
        language=language,
        region=region,
        optimize_for=optimize_for,
    ).model_dump(by_alias=True, exclude_none=True)


def _intent_from_input(intent: IntentInput) -> dict[str, Any]:
    model = intent if isinstance(intent, RoutingIntent) else RoutingIntent.model_validate(intent)
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


def _tools_payload(tools: Optional[list[ToolInput]]) -> Optional[list[dict[str, Any]]]:
    if tools is None:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        model = t if isinstance(t, ChatTool) else ChatTool.model_validate(t)
        out.append(model.model_dump(by_alias=True, exclude_none=True))
    return out


def _session_id_header(session_id: Optional[str]) -> dict[str, str]:
    trimmed = session_id.strip() if session_id else None
    return {"x-session-id": trimmed} if trimmed else {}


def _complete_body(
    *,
    messages: list[MessageInput],
    intent: IntentInput,
    system_prompt: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    reasoning_effort: Optional[ReasoningEffort],
    constraints: ConstraintsInput,
    tools: Optional[list[ToolInput]],
    tool_choice: Optional[ChatToolChoice],
    parallel_tool_calls: Optional[bool],
    max_tool_hops: Optional[int],
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
    if reasoning_effort is not None:
        body["reasoningEffort"] = reasoning_effort
    cs = _constraints_payload(constraints)
    if cs is not None:
        body["constraints"] = cs
    tools_payload = _tools_payload(tools)
    if tools_payload is not None:
        body["tools"] = tools_payload
    if tool_choice is not None:
        body["toolChoice"] = tool_choice
    if parallel_tool_calls is not None:
        body["parallelToolCalls"] = parallel_tool_calls
    if max_tool_hops is not None:
        body["maxToolHops"] = max_tool_hops
    return body


def _synthesize_body(
    *,
    text: str,
    intent: dict[str, Any],
    voice: Optional[str],
    model: Optional[str],
    speed: Optional[float],
    instructions: Optional[str],
    spoken_form: Optional[bool],
    constraints: Optional[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text, "intent": intent}
    if voice is not None:
        body["voice"] = voice
    if model is not None:
        body["model"] = model
    if speed is not None:
        body["speed"] = speed
    if instructions is not None:
        body["instructions"] = instructions
    if spoken_form is not None:
        body["spokenForm"] = spoken_form
    if constraints is not None:
        body["constraints"] = constraints
    return body


def _transcribe_headers(
    *,
    content_type: str,
    intent: dict[str, Any],
    constraints: Optional[dict[str, Any]],
    session_id: Optional[str],
    keywords: Optional[list[str]],
    stt_language: Optional[str],
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "X-Speko-Intent": json.dumps(intent, separators=(",", ":")),
    }
    if constraints is not None:
        headers["X-Speko-Constraints"] = json.dumps(constraints, separators=(",", ":"))
    headers.update(_session_id_header(session_id))
    stt_options: dict[str, Any] = {}
    if keywords:
        stt_options["keywords"] = list(keywords)
    if stt_language is not None:
        stt_options["language"] = stt_language
    if stt_options:
        headers["X-Speko-Stt-Options"] = json.dumps(stt_options, separators=(",", ":"))
    return headers


def _parse_synth_headers(hdrs: httpx.Headers) -> dict[str, Any]:
    raw_failover = hdrs.get("x-speko-failover-count")
    failover_count = int(raw_failover) if raw_failover is not None and raw_failover.isdigit() else 0
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


def _stream_error(data: Any) -> StreamError:
    payload = data if isinstance(data, dict) else {}
    return StreamError(
        error=str(payload.get("error", data)),
        code=str(payload.get("code", "STREAM_ERROR")),
    )


def _transcribe_event(event: str, data: Any) -> Optional[TranscribeStreamEvent]:
    if event == "error":
        return _stream_error(data)
    if not isinstance(data, dict):
        return None
    if event == "meta":
        return TranscribeStreamMeta.model_validate(data)
    if event == "transcript":
        return TranscribeStreamTranscript.model_validate(data)
    if event == "done":
        return TranscribeStreamDone.model_validate(data)
    return None


def _complete_event(event: str, data: Any) -> Optional[CompleteStreamEvent]:
    if event == "error":
        return _stream_error(data)
    if not isinstance(data, dict):
        return None
    if event == "meta":
        return CompleteStreamMeta.model_validate(data)
    if event == "delta":
        return CompleteStreamDelta.model_validate(data)
    if event == "tool_call":
        return CompleteStreamToolCall.model_validate(data)
    if event == "server_tool_call":
        return CompleteStreamServerToolCall.model_validate(data)
    if event == "done":
        return CompleteStreamDone.model_validate(data)
    return None


def _realtime_session_body(params: RealtimeInput) -> dict[str, Any]:
    model = (
        params
        if isinstance(params, RealtimeConnectParams)
        else RealtimeConnectParams.model_validate(params)
    )
    body = model.model_dump(by_alias=True, exclude_none=True)
    # Request-only retry control; it is carried as the standard HTTP header,
    # never as provider session configuration.
    body.pop("idempotencyKey", None)
    # The create-session schema expects the realtime config under `s2s` and a
    # top-level `mode` discriminator; agentId/webhookTags/metadata/ttlSeconds
    # stay top-level.
    top_level = ("agentId", "webhookTags", "metadata", "ttlSeconds")
    wrapped: dict[str, Any] = {
        "mode": "s2s",
        "s2s": {k: v for k, v in body.items() if k not in top_level},
    }
    for key in top_level:
        if key in body:
            wrapped[key] = body[key]
    return wrapped


class SynthesizeStream:
    """Streamed synthesize response: metadata now, audio chunks as they
    arrive. Iterate for chunks; the underlying connection closes when the
    iterator is exhausted (or via ``close()`` / the context manager)."""

    def __init__(self, resp: httpx.Response, meta: dict[str, Any]) -> None:
        self._resp = resp
        self.content_type: str = meta["content_type"]
        self.provider: str = meta["provider"]
        self.model: str = meta["model"]
        self.failover_count: int = meta["failover_count"]
        self.scores_run_id: Optional[str] = meta["scores_run_id"]

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._resp.iter_bytes():
                if chunk:
                    yield chunk
        finally:
            self.close()

    def read(self) -> bytes:
        """Drain the stream and return the concatenated audio bytes."""
        return b"".join(self)

    def close(self) -> None:
        self._resp.close()

    def __enter__(self) -> SynthesizeStream:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncSynthesizeStream:
    """Async streamed synthesize response — see ``SynthesizeStream``."""

    def __init__(self, resp: httpx.Response, meta: dict[str, Any]) -> None:
        self._resp = resp
        self.content_type: str = meta["content_type"]
        self.provider: str = meta["provider"]
        self.model: str = meta["model"]
        self.failover_count: int = meta["failover_count"]
        self.scores_run_id: Optional[str] = meta["scores_run_id"]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await self.aclose()

    async def read(self) -> bytes:
        """Drain the stream and return the concatenated audio bytes."""
        chunks = [chunk async for chunk in self]
        return b"".join(chunks)

    async def aclose(self) -> None:
        await self._resp.aclose()

    async def __aenter__(self) -> AsyncSynthesizeStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()


class Speko:
    """Speko client — one API, every voice provider.

    Example::

        from spekoai import Speko

        speko = Speko(api_key=os.environ["SPEKO_API_KEY"])

        result = speko.transcribe(
            audio_bytes,
            language="es-MX",
        )
    """

    usage: UsageResource
    credits: CreditsResource
    voice: VoiceResource
    voices: VoicesResource
    sessions: SessionsResource
    phone_numbers: PhoneNumbersResource
    agents: AgentsResource
    knowledge_bases: KnowledgeBasesResource
    calls: CallsResource
    callbacks: CallbacksResource
    webhooks: WebhooksResource
    sms: SmsResource

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Speko: api_key is required. Get one at https://platform.speko.dev/agents/keys"
            )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=_default_headers(api_key),
        )
        self.usage = UsageResource(self._client)
        self.credits = CreditsResource(self._client)
        self.voice = VoiceResource(self._client)
        self.voices = VoicesResource(self._client)
        self.sessions = SessionsResource(self._client)
        self.phone_numbers = PhoneNumbersResource(self._client)
        self.agents = AgentsResource(self._client)
        self.knowledge_bases = KnowledgeBasesResource(self._client)
        self.calls = CallsResource(self._client)
        self.callbacks = CallbacksResource(self._client)
        self.webhooks = WebhooksResource(self._client)
        self.sms = SmsResource(self._client)

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
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        stt_language: Optional[str] = None,
    ) -> TranscribeResult:
        """Transcribe audio. Best STT provider auto-routed.

        The router picks the best STT provider for your
        ``(language, region, optimize_for)`` and fails over automatically.

        - ``session_id``: forwarded as ``x-session-id`` for usage attribution.
        - ``keywords``: domain keywords to bias the STT toward, forwarded to
          whichever provider the router picks. Casing matters for proper
          nouns.
        - ``stt_language``: provider-facing STT stream-language override.
          Routing continues to use ``language``.

        Example::

            with open("call.wav", "rb") as f:
                audio = f.read()
            result = speko.transcribe(
                audio,
                language="es-MX",
                region="us-east4",
            )
            print(result.text, result.provider, result.confidence)
        """
        done: Optional[TranscribeStreamDone] = None
        for event in self.transcribe_stream(
            audio,
            language=language,
            region=region,
            optimize_for=optimize_for,
            content_type=content_type,
            constraints=constraints,
            session_id=session_id,
            keywords=keywords,
            stt_language=stt_language,
        ):
            if isinstance(event, StreamError):
                raise SpekoApiError(event.error, 200, event.code)
            if isinstance(event, TranscribeStreamDone):
                done = event
        if done is None:
            raise SpekoApiError("Transcribe stream ended without a done event", 200, "STREAM_ENDED")
        return TranscribeResult.model_validate(done.model_dump(by_alias=True))

    def transcribe_stream(
        self,
        audio: bytes,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        stt_language: Optional[str] = None,
    ) -> Iterator[TranscribeStreamEvent]:
        """Transcribe audio, yielding typed stream events as they arrive:
        ``meta`` → interim ``transcript`` frames → ``done`` (or ``error``)."""
        intent = _intent_from_fields(language, region, optimize_for)
        headers = _transcribe_headers(
            content_type=content_type,
            intent=intent,
            constraints=_constraints_payload(constraints),
            session_id=session_id,
            keywords=keywords,
            stt_language=stt_language,
        )
        with self._client.stream(
            "POST", "/v1/transcribe", content=bytes(audio), headers=headers
        ) as resp:
            raise_for_status_streamed(resp)
            for event, data in iter_sse(resp.iter_text()):
                item = _transcribe_event(event, data)
                if item is not None:
                    yield item

    def synthesize(
        self,
        text: str,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        instructions: Optional[str] = None,
        spoken_form: Optional[bool] = None,
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
    ) -> SynthesizeResult:
        """Synthesize text to audio. Best TTS provider auto-routed.

        Returned ``audio`` format depends on the chosen provider —
        inspect ``content_type`` (ElevenLabs: ``audio/mpeg``;
        Cartesia: ``audio/pcm;rate=24000``).

        - ``model``: upstream model override for the primary candidate only.
        - ``instructions``: free-text speaking-style instruction; only
          instruction-capable models honor it (safe to always pass).
        - ``spoken_form``: normalize text into spoken form before TTS.
        """
        intent = _intent_from_fields(language, region, optimize_for)
        body = _synthesize_body(
            text=text,
            intent=intent,
            voice=voice,
            model=model,
            speed=speed,
            instructions=instructions,
            spoken_form=spoken_form,
            constraints=_constraints_payload(constraints),
        )
        resp = self._client.post(
            "/v1/synthesize", json=body, headers=_session_id_header(session_id)
        )
        raise_for_status(resp)
        return SynthesizeResult(audio=resp.content, **_parse_synth_headers(resp.headers))

    def synthesize_stream(
        self,
        text: str,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        instructions: Optional[str] = None,
        spoken_form: Optional[bool] = None,
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
    ) -> SynthesizeStream:
        """Synthesize text to audio, streaming chunks as the provider
        renders them. Returns a handle exposing provider metadata plus an
        iterator of raw audio byte chunks::

            with speko.synthesize_stream("Hello", language="en") as stream:
                print(stream.provider, stream.content_type)
                for chunk in stream:
                    play(chunk)
        """
        intent = _intent_from_fields(language, region, optimize_for)
        body = _synthesize_body(
            text=text,
            intent=intent,
            voice=voice,
            model=model,
            speed=speed,
            instructions=instructions,
            spoken_form=spoken_form,
            constraints=_constraints_payload(constraints),
        )
        request = self._client.build_request(
            "POST", "/v1/synthesize", json=body, headers=_session_id_header(session_id)
        )
        resp = self._client.send(request, stream=True)
        if resp.status_code >= 400:
            try:
                raise_for_status_streamed(resp)
            finally:
                resp.close()
        return SynthesizeStream(resp, _parse_synth_headers(resp.headers))

    def complete(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        constraints: ConstraintsInput = None,
        tools: Optional[list[ToolInput]] = None,
        tool_choice: Optional[ChatToolChoice] = None,
        parallel_tool_calls: Optional[bool] = None,
        max_tool_hops: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> CompleteResult:
        """Run an LLM completion. Best LLM provider auto-routed.

        Pass ``tools`` (``ChatTool`` models or dicts) to expose tools to the
        model; inline tool invocations come back on ``result.tool_calls``.
        ``max_tool_hops`` caps provider hops when server-executed
        (webhook/builtin/integration) tools are present.
        """
        done: Optional[CompleteStreamDone] = None
        for event in self.complete_stream(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            constraints=constraints,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            max_tool_hops=max_tool_hops,
            session_id=session_id,
        ):
            if isinstance(event, StreamError):
                raise SpekoApiError(event.error, 200, event.code)
            if isinstance(event, CompleteStreamDone):
                done = event
        if done is None:
            raise SpekoApiError("Complete stream ended without a done event", 200, "STREAM_ENDED")
        return CompleteResult.model_validate(done.model_dump(by_alias=True))

    def complete_stream(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        constraints: ConstraintsInput = None,
        tools: Optional[list[ToolInput]] = None,
        tool_choice: Optional[ChatToolChoice] = None,
        parallel_tool_calls: Optional[bool] = None,
        max_tool_hops: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> Iterator[CompleteStreamEvent]:
        """Run an LLM completion, yielding typed stream events: ``meta`` →
        ``delta`` text fragments / ``tool_call`` / ``server_tool_call`` →
        ``done`` (or ``error``)."""
        body = _complete_body(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            constraints=constraints,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            max_tool_hops=max_tool_hops,
        )
        with self._client.stream(
            "POST", "/v1/complete", json=body, headers=_session_id_header(session_id)
        ) as resp:
            raise_for_status_streamed(resp)
            for event, data in iter_sse(resp.iter_text()):
                item = _complete_event(event, data)
                if item is not None:
                    yield item


class AsyncSpeko:
    """Async Speko client.

    Example::

        from spekoai import AsyncSpeko

        async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
            result = await speko.transcribe(
                audio_bytes,
                language="es-MX",
            )
    """

    usage: AsyncUsageResource
    credits: AsyncCreditsResource
    voice: AsyncVoiceResource
    voices: AsyncVoicesResource
    sessions: AsyncSessionsResource
    phone_numbers: AsyncPhoneNumbersResource
    agents: AsyncAgentsResource
    knowledge_bases: AsyncKnowledgeBasesResource
    calls: AsyncCallsResource
    callbacks: AsyncCallbacksResource
    webhooks: AsyncWebhooksResource
    sms: AsyncSmsResource

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Speko: api_key is required. Get one at https://platform.speko.dev/agents/keys"
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=_default_headers(api_key),
        )
        self._realtime_tasks: set[asyncio.Task[None]] = set()
        self.usage = AsyncUsageResource(self._client)
        self.credits = AsyncCreditsResource(self._client)
        self.voice = AsyncVoiceResource(self._client)
        self.voices = AsyncVoicesResource(self._client)
        self.sessions = AsyncSessionsResource(self._client)
        self.phone_numbers = AsyncPhoneNumbersResource(self._client)
        self.agents = AsyncAgentsResource(self._client)
        self.knowledge_bases = AsyncKnowledgeBasesResource(self._client)
        self.calls = AsyncCallsResource(self._client)
        self.callbacks = AsyncCallbacksResource(self._client)
        self.webhooks = AsyncWebhooksResource(self._client)
        self.sms = AsyncSmsResource(self._client)

    async def close(self, *, realtime_timeout: float = 5.0) -> None:
        """Close HTTP resources after a bounded realtime cleanup/report drain.

        Use the client context manager, or await this method before stopping
        the event loop. The timeout covers all pending realtime work together.
        Delivery is best effort and is not guaranteed after the deadline or
        event-loop termination. A timeout does not cancel provider cleanup.
        """
        try:
            await _drain_background_tasks(self._realtime_tasks, realtime_timeout)
        finally:
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
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        stt_language: Optional[str] = None,
    ) -> TranscribeResult:
        """Transcribe audio (async). Best STT provider auto-routed."""
        done: Optional[TranscribeStreamDone] = None
        async for event in self.transcribe_stream(
            audio,
            language=language,
            region=region,
            optimize_for=optimize_for,
            content_type=content_type,
            constraints=constraints,
            session_id=session_id,
            keywords=keywords,
            stt_language=stt_language,
        ):
            if isinstance(event, StreamError):
                raise SpekoApiError(event.error, 200, event.code)
            if isinstance(event, TranscribeStreamDone):
                done = event
        if done is None:
            raise SpekoApiError("Transcribe stream ended without a done event", 200, "STREAM_ENDED")
        return TranscribeResult.model_validate(done.model_dump(by_alias=True))

    async def transcribe_stream(
        self,
        audio: bytes,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        content_type: str = "audio/wav",
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        stt_language: Optional[str] = None,
    ) -> AsyncIterator[TranscribeStreamEvent]:
        """Transcribe audio, yielding typed stream events (async)."""
        intent = _intent_from_fields(language, region, optimize_for)
        headers = _transcribe_headers(
            content_type=content_type,
            intent=intent,
            constraints=_constraints_payload(constraints),
            session_id=session_id,
            keywords=keywords,
            stt_language=stt_language,
        )
        async with self._client.stream(
            "POST", "/v1/transcribe", content=bytes(audio), headers=headers
        ) as resp:
            await araise_for_status_streamed(resp)
            async for event, data in aiter_sse(resp.aiter_text()):
                item = _transcribe_event(event, data)
                if item is not None:
                    yield item

    async def synthesize(
        self,
        text: str,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        instructions: Optional[str] = None,
        spoken_form: Optional[bool] = None,
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
    ) -> SynthesizeResult:
        """Synthesize text to audio (async). Best TTS provider auto-routed."""
        intent = _intent_from_fields(language, region, optimize_for)
        body = _synthesize_body(
            text=text,
            intent=intent,
            voice=voice,
            model=model,
            speed=speed,
            instructions=instructions,
            spoken_form=spoken_form,
            constraints=_constraints_payload(constraints),
        )
        resp = await self._client.post(
            "/v1/synthesize", json=body, headers=_session_id_header(session_id)
        )
        raise_for_status(resp)
        return SynthesizeResult(audio=resp.content, **_parse_synth_headers(resp.headers))

    async def synthesize_stream(
        self,
        text: str,
        *,
        language: str,
        region: Optional[str] = None,
        optimize_for: Optional[OptimizeFor] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        instructions: Optional[str] = None,
        spoken_form: Optional[bool] = None,
        constraints: ConstraintsInput = None,
        session_id: Optional[str] = None,
    ) -> AsyncSynthesizeStream:
        """Synthesize text to audio, streaming chunks (async)::

        stream = await speko.synthesize_stream("Hello", language="en")
        async with stream:
            async for chunk in stream:
                play(chunk)
        """
        intent = _intent_from_fields(language, region, optimize_for)
        body = _synthesize_body(
            text=text,
            intent=intent,
            voice=voice,
            model=model,
            speed=speed,
            instructions=instructions,
            spoken_form=spoken_form,
            constraints=_constraints_payload(constraints),
        )
        request = self._client.build_request(
            "POST", "/v1/synthesize", json=body, headers=_session_id_header(session_id)
        )
        resp = await self._client.send(request, stream=True)
        if resp.status_code >= 400:
            try:
                await araise_for_status_streamed(resp)
            finally:
                await resp.aclose()
        return AsyncSynthesizeStream(resp, _parse_synth_headers(resp.headers))

    async def complete(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        constraints: ConstraintsInput = None,
        tools: Optional[list[ToolInput]] = None,
        tool_choice: Optional[ChatToolChoice] = None,
        parallel_tool_calls: Optional[bool] = None,
        max_tool_hops: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> CompleteResult:
        """Run an LLM completion (async). Best LLM provider auto-routed."""
        done: Optional[CompleteStreamDone] = None
        async for event in self.complete_stream(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            constraints=constraints,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            max_tool_hops=max_tool_hops,
            session_id=session_id,
        ):
            if isinstance(event, StreamError):
                raise SpekoApiError(event.error, 200, event.code)
            if isinstance(event, CompleteStreamDone):
                done = event
        if done is None:
            raise SpekoApiError("Complete stream ended without a done event", 200, "STREAM_ENDED")
        return CompleteResult.model_validate(done.model_dump(by_alias=True))

    async def complete_stream(
        self,
        *,
        messages: list[MessageInput],
        intent: IntentInput,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        constraints: ConstraintsInput = None,
        tools: Optional[list[ToolInput]] = None,
        tool_choice: Optional[ChatToolChoice] = None,
        parallel_tool_calls: Optional[bool] = None,
        max_tool_hops: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[CompleteStreamEvent]:
        """Run an LLM completion, yielding typed stream events (async)."""
        body = _complete_body(
            messages=messages,
            intent=intent,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            constraints=constraints,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            max_tool_hops=max_tool_hops,
        )
        async with self._client.stream(
            "POST", "/v1/complete", json=body, headers=_session_id_header(session_id)
        ) as resp:
            await araise_for_status_streamed(resp)
            async for event, data in aiter_sse(resp.aiter_text()):
                item = _complete_event(event, data)
                if item is not None:
                    yield item

    async def connect_realtime(self, params: RealtimeInput) -> AsyncRealtimeSession:
        """Open a speech-to-speech (S2S) session.

        Posts ``/v1/sessions`` with ``mode='s2s'`` to mint a short-lived
        provider credential, then opens the provider transport directly.
        Speko remains on the setup and billing paths only; audio does not
        traverse a Speko proxy.

        Example::

            session = await speko.connect_realtime(
                RealtimeConnectParams(provider="openai", model="gpt-realtime"),
            )
            async with session:
                await session.send_audio(pcm_chunk)
                async for frame in session:
                    ...
        """
        model = (
            params
            if isinstance(params, RealtimeConnectParams)
            else RealtimeConnectParams.model_validate(params)
        )
        resp = await self._client.post(
            "/v1/sessions",
            json=_realtime_session_body(model),
            headers={"Idempotency-Key": model.idempotency_key or str(uuid4())},
        )
        raise_for_status(resp)
        info = RealtimeSessionInfo.model_validate(resp.json())
        return await open_realtime_session(info, _pending_tasks=self._realtime_tasks)
