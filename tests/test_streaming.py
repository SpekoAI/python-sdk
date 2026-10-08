import json

import pytest
import respx

from spekoai import (
    CompleteStreamDelta,
    CompleteStreamDone,
    CompleteStreamMeta,
    CompleteStreamToolCall,
    SpekoApiError,
    TranscribeStreamDone,
    TranscribeStreamMeta,
    TranscribeStreamTranscript,
)
from tests.conftest import BASE, sse

TRANSCRIBE_SSE = sse(
    ("meta", {"provider": "deepgram", "model": "nova-3", "failoverCount": 0, "scoresRunId": None}),
    ("transcript", {"text": "hola", "isFinal": False, "confidence": 0.7}),
    (
        "done",
        {
            "text": "hola mundo",
            "provider": "deepgram",
            "model": "nova-3",
            "confidence": 0.93,
            "failoverCount": 0,
            "scoresRunId": "run_1",
        },
    ),
)

COMPLETE_SSE = sse(
    (
        "meta",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "failoverCount": 0,
            "totalFailoverCount": 0,
            "scoresRunId": None,
            "hop": 0,
        },
    ),
    ("delta", {"text": "Hel"}),
    ("delta", {"text": "lo"}),
    ("tool_call", {"id": "call_1", "name": "lookup", "args": "{\"q\":1}"}),
    (
        "done",
        {
            "text": "Hello",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"promptTokens": 10, "completionTokens": 5},
            "failoverCount": 0,
            "scoresRunId": None,
            "toolCalls": [{"id": "call_1", "name": "lookup", "args": "{\"q\":1}"}],
        },
    ),
)


@respx.mock
def test_transcribe_stream_events(speko):
    route = respx.post(f"{BASE}/v1/transcribe").respond(
        text=TRANSCRIBE_SSE, headers={"content-type": "text/event-stream"}
    )
    events = list(
        speko.transcribe_stream(
            b"\x00\x01",
            language="es-MX",
            session_id=" sess_1 ",
            keywords=["Speko"],
            stt_language="multi",
        )
    )
    assert [type(e) for e in events] == [
        TranscribeStreamMeta,
        TranscribeStreamTranscript,
        TranscribeStreamDone,
    ]
    assert events[2].text == "hola mundo"

    headers = route.calls.last.request.headers
    assert json.loads(headers["X-Speko-Intent"]) == {"language": "es-MX"}
    assert headers["x-session-id"] == "sess_1"
    assert json.loads(headers["X-Speko-Stt-Options"]) == {
        "keywords": ["Speko"],
        "language": "multi",
    }


@respx.mock
def test_transcribe_returns_done_payload(speko):
    respx.post(f"{BASE}/v1/transcribe").respond(
        text=TRANSCRIBE_SSE, headers={"content-type": "text/event-stream"}
    )
    result = speko.transcribe(b"\x00", language="es-MX")
    assert result.text == "hola mundo"
    assert result.scores_run_id == "run_1"


@respx.mock
def test_transcribe_stream_error_event_raises_in_call(speko):
    respx.post(f"{BASE}/v1/transcribe").respond(
        text=sse(("error", {"error": "no provider", "code": "NO_PROVIDER"})),
        headers={"content-type": "text/event-stream"},
    )
    with pytest.raises(SpekoApiError) as exc:
        speko.transcribe(b"\x00", language="xx")
    assert exc.value.code == "NO_PROVIDER"


@respx.mock
def test_streamed_http_error_maps_to_speko_error(speko):
    respx.post(f"{BASE}/v1/transcribe").respond(
        status_code=422, json={"error": "bad intent", "code": "INVALID_INTENT"}
    )
    with pytest.raises(SpekoApiError) as exc:
        list(speko.transcribe_stream(b"\x00", language="xx"))
    assert exc.value.status == 422
    assert exc.value.code == "INVALID_INTENT"


@respx.mock
def test_complete_stream_and_done(speko):
    route = respx.post(f"{BASE}/v1/complete").respond(
        text=COMPLETE_SSE, headers={"content-type": "text/event-stream"}
    )
    events = list(
        speko.complete_stream(
            messages=[{"role": "user", "content": "Hi"}],
            intent={"language": "en"},
            tools=[
                {
                    "name": "lookup",
                    "description": "Look up",
                    "parameters": {"type": "object"},
                }
            ],
            tool_choice="auto",
            parallel_tool_calls=False,
            max_tool_hops=4,
            reasoning_effort="low",
            session_id="sess_1",
        )
    )
    assert isinstance(events[0], CompleteStreamMeta)
    assert isinstance(events[1], CompleteStreamDelta)
    assert isinstance(events[3], CompleteStreamToolCall)
    assert isinstance(events[4], CompleteStreamDone)
    assert events[4].tool_calls[0].name == "lookup"

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["tools"] == [
        {"name": "lookup", "description": "Look up", "parameters": {"type": "object"}}
    ]
    assert body["toolChoice"] == "auto"
    assert body["parallelToolCalls"] is False
    assert body["maxToolHops"] == 4
    assert body["reasoningEffort"] == "low"
    assert request.headers["x-session-id"] == "sess_1"


@respx.mock
def test_complete_result_includes_tool_calls(speko):
    respx.post(f"{BASE}/v1/complete").respond(
        text=COMPLETE_SSE, headers={"content-type": "text/event-stream"}
    )
    result = speko.complete(
        messages=[{"role": "user", "content": "Hi"}], intent={"language": "en"}
    )
    assert result.text == "Hello"
    assert result.tool_calls[0].id == "call_1"
    assert result.usage.prompt_tokens == 10


@respx.mock
def test_complete_tool_role_message_wire_shape(speko):
    route = respx.post(f"{BASE}/v1/complete").respond(
        text=COMPLETE_SSE, headers={"content-type": "text/event-stream"}
    )
    speko.complete(
        messages=[
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "lookup", "args": "{}"}],
            },
            {"role": "tool", "content": "not found", "tool_call_id": "call_1", "is_error": True},
        ],
        intent={"language": "en"},
    )
    body = json.loads(route.calls.last.request.content)
    assert body["messages"][1]["toolCalls"] == [{"id": "call_1", "name": "lookup", "args": "{}"}]
    assert body["messages"][2] == {
        "role": "tool",
        "content": "not found",
        "toolCallId": "call_1",
        "isError": True,
    }


@respx.mock
def test_synthesize_stream_metadata_and_chunks(speko):
    route = respx.post(f"{BASE}/v1/synthesize").respond(
        content=b"pcm-bytes-here",
        headers={
            "content-type": "audio/pcm;rate=24000",
            "x-speko-provider": "cartesia",
            "x-speko-model": "sonic-3.5",
            "x-speko-failover-count": "1",
            "x-speko-scores-run-id": "run_9",
        },
    )
    with speko.synthesize_stream(
        "Hello", language="en", model="sonic-3.5", instructions="calm", spoken_form=True
    ) as stream:
        assert stream.provider == "cartesia"
        assert stream.failover_count == 1
        assert stream.scores_run_id == "run_9"
        audio = b"".join(stream)
    assert audio == b"pcm-bytes-here"

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "sonic-3.5"
    assert body["instructions"] == "calm"
    assert body["spokenForm"] is True


@respx.mock
def test_synthesize_stream_http_error(speko):
    respx.post(f"{BASE}/v1/synthesize").respond(
        status_code=400, json={"error": "bad voice", "code": "INVALID_VOICE"}
    )
    with pytest.raises(SpekoApiError) as exc:
        speko.synthesize_stream("Hello", language="en", voice="nope")
    assert exc.value.code == "INVALID_VOICE"


@respx.mock
async def test_async_streaming_mirror(aspeko):
    respx.post(f"{BASE}/v1/transcribe").respond(
        text=TRANSCRIBE_SSE, headers={"content-type": "text/event-stream"}
    )
    result = await aspeko.transcribe(b"\x00", language="es-MX")
    assert result.text == "hola mundo"

    respx.post(f"{BASE}/v1/complete").respond(
        text=COMPLETE_SSE, headers={"content-type": "text/event-stream"}
    )
    events = []
    async for event in aspeko.complete_stream(
        messages=[{"role": "user", "content": "Hi"}], intent={"language": "en"}
    ):
        events.append(event)
    assert isinstance(events[-1], CompleteStreamDone)

    respx.post(f"{BASE}/v1/synthesize").respond(
        content=b"audio",
        headers={"content-type": "audio/mpeg", "x-speko-provider": "elevenlabs"},
    )
    stream = await aspeko.synthesize_stream("Hi", language="en")
    async with stream:
        chunks = [chunk async for chunk in stream]
    assert b"".join(chunks) == b"audio"
    assert stream.content_type == "audio/mpeg"
