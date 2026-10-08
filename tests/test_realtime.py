import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

import spekoai.realtime as realtime_module
from spekoai import AsyncSpeko
from spekoai._user_agent import USER_AGENT
from spekoai.client import _realtime_session_body
from spekoai.models import RealtimeConnectParams, RealtimeSessionInfo
from spekoai.realtime import (
    AsyncRealtimeSession,
    _OpenAIInputAudioTrack,
    _OpenAIWebRTCConnection,
    _provider_session_update,
    _provider_url,
    _translate_frame,
    _translate_provider_frames,
    open_realtime_session,
)


class FakeRTCDataChannel:
    def __init__(self) -> None:
        self.readyState = "connecting"
        self.sent: list[str] = []
        self.listeners: dict[str, object] = {}

    def on(self, event: str):
        def register(listener):
            self.listeners[event] = listener
            return listener

        return register

    def open(self) -> None:
        self.readyState = "open"
        listener = self.listeners.get("open")
        if callable(listener):
            listener()

    def send(self, payload: str) -> None:
        self.sent.append(payload)
        if json.loads(payload).get("type") == "session.update":
            listener = self.listeners.get("message")
            if callable(listener):
                listener(json.dumps({"type": "session.updated"}))

    def close(self) -> None:
        self.readyState = "closed"
        listener = self.listeners.get("close")
        if callable(listener):
            listener()


class FakeRTCPeerConnection:
    instances: list["FakeRTCPeerConnection"] = []

    def __init__(self) -> None:
        self.connectionState = "new"
        self.localDescription = None
        self.remote_description = None
        self.channel = FakeRTCDataChannel()
        self.listeners: dict[str, object] = {}
        self.input_track = None
        self.sideband_bound = False
        self.__class__.instances.append(self)

    def on(self, event: str):
        def register(listener):
            self.listeners[event] = listener
            return listener

        return register

    def addTrack(self, track) -> None:  # noqa: N802 - mirrors aiortc
        self.input_track = track

    def createDataChannel(self, label: str) -> FakeRTCDataChannel:  # noqa: N802
        assert label == "oai-events"
        return self.channel

    async def createOffer(self):  # noqa: N802
        return SimpleNamespace(type="offer", sdp="offer-sdp")

    async def setLocalDescription(self, description) -> None:  # noqa: N802
        self.localDescription = description

    async def setRemoteDescription(self, description) -> None:  # noqa: N802
        assert self.sideband_bound, "remote media enabled before billing sideband bound"
        self.remote_description = description
        self.connectionState = "connected"
        self.channel.open()

    async def close(self) -> None:
        self.connectionState = "closed"
        listener = self.listeners.get("connectionstatechange")
        if callable(listener):
            listener()


def _openai_session_info() -> RealtimeSessionInfo:
    return RealtimeSessionInfo.model_validate(
        {
            "mode": "s2s",
            "transport": "provider_direct",
            "sessionId": "11111111-1111-4111-8111-111111111111",
            "planId": "plan_openai",
            "attemptId": "att_openai",
            "provider": "openai",
            "model": "gpt-realtime",
            "adapter": "openai.realtime.v1",
            "providerTransport": "webrtc",
            "endpoint": "https://api.openai.com/v1/realtime/calls",
            "sidebandUrl": (
                "https://gateway.speko.dev/v1/sessions/"
                "11111111-1111-4111-8111-111111111111/sidebands/openai"
            ),
            "credential": {
                "kind": "bearer",
                "value": "ek-short-lived",
                "expiresAt": "2100-01-01T00:05:00Z",
            },
            "telemetry": {
                "endpoint": "https://gateway.speko.dev/v1/runtime-events",
                "token": "telemetry-token",
                "flushIntervalMs": 5000,
            },
            "reservation": {
                "id": "res_openai",
                "authorizedDurationSeconds": 300,
                "leaseExpiresAt": "2100-01-01T00:05:00Z",
                "billing": {
                    "mode": "direct_entitlement",
                    "state": "estimated",
                    "maximumAmountMicros": "30000",
                    "currency": "USD",
                },
            },
            "session": {"voice": "marin", "instructions": "Be concise."},
            "inputSampleRate": 24000,
            "outputSampleRate": 24000,
            "expiresAt": "2100-01-01T00:05:00Z",
        }
    )


def test_realtime_session_body_wraps_s2s_and_top_level_keys():
    body = _realtime_session_body(
        RealtimeConnectParams(
            agent_id="ag_1",
            provider="xai",
            model="grok-voice-fast-1.0",
            voice="sophia",
            webhook_tags={"env": "prod"},
            metadata={"k": "v"},
            ttl_seconds=600,
        )
    )
    assert body == {
        "mode": "s2s",
        "s2s": {
            "provider": "xai",
            "model": "grok-voice-fast-1.0",
            "voice": "sophia",
        },
        "agentId": "ag_1",
        "webhookTags": {"env": "prod"},
        "metadata": {"k": "v"},
        "ttlSeconds": 600,
    }


def test_realtime_session_body_accepts_dict():
    body = _realtime_session_body({"provider": "openai", "model": "gpt-realtime"})
    assert body == {"mode": "s2s", "s2s": {"provider": "openai", "model": "gpt-realtime"}}


def test_translate_frame_new_types():
    assert _translate_frame(
        {"t": "ready", "inputSampleRate": 16000, "outputSampleRate": 24000}
    ) == {"type": "ready", "input_sample_rate": 16000, "output_sample_rate": 24000}
    assert _translate_frame({"t": "interruption", "at": "assistant"}) == {
        "type": "interruption",
        "at": "assistant",
    }
    assert _translate_frame(
        {"t": "server_tool_call", "id": "st_1", "name": "search", "status": "completed"}
    ) == {
        "type": "server_tool_call",
        "id": "st_1",
        "name": "search",
        "status": "completed",
    }
    assert _translate_frame({"t": "transcript", "role": "user", "text": "hi", "final": True}) == {
        "type": "transcript",
        "role": "user",
        "text": "hi",
        "final": True,
    }


def test_provider_direct_response_validates_and_builds_xai_connection():
    info = RealtimeSessionInfo.model_validate(
        {
            "mode": "s2s",
            "transport": "provider_direct",
            "sessionId": "11111111-1111-4111-8111-111111111111",
            "planId": "plan_1",
            "attemptId": "att_1",
            "provider": "xai",
            "model": "grok-voice-fast-1.0",
            "adapter": "xai.realtime.v1",
            "providerTransport": "websocket",
            "endpoint": "wss://api.x.ai/v1/realtime",
            "credential": {
                "kind": "bearer",
                "value": "ek-short-lived",
                "expiresAt": "2100-01-01T00:05:00Z",
            },
            "telemetry": {
                "endpoint": "https://gateway.speko.dev/v1/runtime-events",
                "token": "telemetry-token",
                "flushIntervalMs": 5000,
            },
            "reservation": {
                "id": "res_1",
                "authorizedDurationSeconds": 300,
                "leaseExpiresAt": "2100-01-01T00:05:00Z",
                "billing": {
                    "mode": "direct_entitlement",
                    "state": "estimated",
                    "maximumAmountMicros": "30000",
                    "currency": "USD",
                },
            },
            "session": {
                "voice": "Ara",
                "instructions": "Be concise.",
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Look up an order.",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            "inputSampleRate": 24000,
            "outputSampleRate": 24000,
            "expiresAt": "2100-01-01T00:05:00Z",
        }
    )

    assert _provider_url(info) == "wss://api.x.ai/v1/realtime?model=grok-voice-fast-1.0"
    update = _provider_session_update(info)
    assert update["session"]["voice"] == "Ara"
    assert update["session"]["turn_detection"] == {"type": "server_vad"}
    assert update["session"]["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up an order.",
            "parameters": {"type": "object"},
        }
    ]

    frames = _translate_provider_frames(
        info,
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"order_id":"123"}',
        },
    )
    assert frames == [
        {
            "type": "tool_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"order_id":"123"}',
        }
    ]


def test_realtime_idempotency_key_is_not_sent_as_provider_config():
    body = _realtime_session_body(
        RealtimeConnectParams(
            provider="google",
            model="gemini-3.1-flash-live-preview",
            idempotency_key="retry-me",
        )
    )
    assert "idempotencyKey" not in body
    assert "idempotencyKey" not in body["s2s"]


@respx.mock
async def test_openai_uses_webrtc_and_binds_sideband_before_media(monkeypatch):
    FakeRTCPeerConnection.instances = []
    monkeypatch.setattr(realtime_module, "RTCPeerConnection", FakeRTCPeerConnection)
    provider_route = respx.post("https://api.openai.com/v1/realtime/calls").mock(
        return_value=httpx.Response(
            201,
            text="answer-sdp",
            headers={"Location": "/v1/realtime/calls/call_12345678"},
        )
    )

    def bind_sideband(request: httpx.Request) -> httpx.Response:
        peer = FakeRTCPeerConnection.instances[0]
        peer.sideband_bound = True
        return httpx.Response(201, json={"status": "bound"})

    sideband_route = respx.post(
        "https://gateway.speko.dev/v1/sessions/"
        "11111111-1111-4111-8111-111111111111/sidebands/openai"
    ).mock(side_effect=bind_sideband)
    telemetry_route = respx.post("https://gateway.speko.dev/v1/runtime-events").mock(
        return_value=httpx.Response(202, json={"accepted": 2, "deduplicated": 0})
    )

    session = await open_realtime_session(_openai_session_info())
    peer = FakeRTCPeerConnection.instances[0]
    assert provider_route.call_count == 1
    provider_request = provider_route.calls.last.request
    assert provider_request.headers["Authorization"] == "Bearer ek-short-lived"
    assert provider_request.headers["User-Agent"] == f"python-httpx/{httpx.__version__}"
    assert provider_request.headers["Content-Type"].startswith("multipart/form-data;")
    assert b"offer-sdp" in provider_request.content
    assert b'"model":"gpt-realtime"' in provider_request.content
    assert sideband_route.call_count == 1
    sideband_request = sideband_route.calls.last.request
    assert sideband_request.headers["Authorization"] == "Bearer telemetry-token"
    assert sideband_request.headers["User-Agent"] == USER_AGENT
    assert sideband_request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert json.loads(sideband_request.content) == {
        "attempt_id": "att_openai",
        "provider_session_id": "call_12345678",
    }
    assert peer.remote_description.sdp == "answer-sdp"
    assert json.loads(peer.channel.sent[0])["type"] == "session.update"

    await session.send_audio(bytes(480))
    assert isinstance(peer.input_track, _OpenAIInputAudioTrack)
    await session.commit()
    assert peer.input_track._queue.qsize() == 1
    await session._report_telemetry(terminal=False)
    await session.close()
    await session._terminal_telemetry_task
    assert telemetry_route.call_count == 2
    for index, call in enumerate(telemetry_route.calls, start=1):
        request = call.request
        assert request.headers["Authorization"] == "Bearer telemetry-token"
        assert request.headers["User-Agent"] == USER_AGENT
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        events = json.loads(request.content)["events"]
        assert len(events) == index
        assert events[0]["event_id"] == f"att_openai:usage.reported:{index}"
        assert set(events[0]["data"]) == {"unit", "quantity_millis"}
        assert events[0]["data"]["unit"] == "duration_seconds"
        if index == 2:
            assert events[1]["event_id"] == "att_openai:session.closed"


@respx.mock
@pytest.mark.parametrize("provider", ["xai", "google"])
async def test_runtime_marker_does_not_change_provider_websocket_auth(monkeypatch, provider):
    endpoint = (
        "wss://api.x.ai/v1/realtime"
        if provider == "xai"
        else "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
        "v1beta.GenerativeService.BidiGenerateContentConstrained"
    )
    info = _openai_session_info().model_copy(
        update={
            "provider": provider,
            "model": "grok-voice-fast-1.0" if provider == "xai" else "gemini-live",
            "adapter": "xai.realtime.v1" if provider == "xai" else "google.live.v1",
            "provider_transport": "websocket",
            "endpoint": endpoint,
            "sideband_url": None,
        }
    )
    ws = SimpleNamespace(
        send=AsyncMock(),
        recv=AsyncMock(
            return_value=json.dumps(
                {"type": "session.updated"} if provider == "xai" else {"setupComplete": {}}
            )
        ),
        close=AsyncMock(),
    )
    connect = AsyncMock(return_value=ws)
    monkeypatch.setattr(realtime_module, "ws_connect", connect)
    telemetry_route = respx.post(info.telemetry.endpoint).mock(
        return_value=httpx.Response(202, json={"accepted": 2, "deduplicated": 0})
    )

    session = await open_realtime_session(info)
    if provider == "xai":
        connect.assert_awaited_once_with(
            f"{endpoint}?model=grok-voice-fast-1.0",
            subprotocols=["xai-client-secret.ek-short-lived"],
        )
    else:
        connect.assert_awaited_once_with(f"{endpoint}?access_token=ek-short-lived")
    await session.close()
    await session._terminal_telemetry_task
    assert telemetry_route.call_count == 1
    assert telemetry_route.calls.last.request.headers["User-Agent"] == USER_AGENT


def test_openai_provider_url_refuses_websocket_downgrade():
    with pytest.raises(ValueError, match="requires its negotiated WebRTC"):
        _provider_url(_openai_session_info())


@respx.mock
@pytest.mark.parametrize("telemetry_outcome", ["stalled", "failed", "cancelled"])
async def test_close_finishes_before_optional_telemetry(telemetry_outcome):
    telemetry_started = asyncio.Event()
    telemetry_release = asyncio.Event()
    ws = SimpleNamespace(close=AsyncMock())
    requests = []

    async def report(request):
        ws.close.assert_awaited_once_with(code=1000, reason="client_closed")
        requests.append(request)
        telemetry_started.set()
        if telemetry_outcome == "failed":
            raise httpx.ConnectError("telemetry unavailable", request=request)
        await telemetry_release.wait()
        return httpx.Response(202)

    route = respx.post(_openai_session_info().telemetry.endpoint).mock(side_effect=report)
    session = AsyncRealtimeSession(_openai_session_info(), ws)
    try:
        await asyncio.wait_for(session.close(), timeout=1)
        await asyncio.wait_for(telemetry_started.wait(), timeout=1)
        await asyncio.wait_for(session.close(), timeout=1)
        ws.close.assert_awaited_once()
        if telemetry_outcome == "cancelled":
            session._terminal_telemetry_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await session._terminal_telemetry_task
        else:
            telemetry_release.set()
            await session._terminal_telemetry_task
        assert len(requests) == 1
        if telemetry_outcome != "cancelled":
            assert route.call_count == 1
        await session.close()
        ws.close.assert_awaited_once()
    finally:
        telemetry_release.set()
        await asyncio.gather(session._telemetry_task, return_exceptions=True)
        if session._terminal_telemetry_task is not None:
            await asyncio.gather(session._terminal_telemetry_task, return_exceptions=True)


async def test_cancelled_close_continues_once_with_concurrent_callers(monkeypatch):
    provider_closing = asyncio.Event()
    provider_release = asyncio.Event()

    async def close_provider(**kwargs):
        provider_closing.set()
        await provider_release.wait()

    ws = SimpleNamespace(close=AsyncMock(side_effect=close_provider))
    session = AsyncRealtimeSession(_openai_session_info(), ws)
    report = AsyncMock()
    monkeypatch.setattr(session, "_report_telemetry", report)
    first = asyncio.create_task(session.close(code=1001, reason="user_left"))
    second = None
    try:
        await asyncio.wait_for(provider_closing.wait(), timeout=1)
        second = asyncio.create_task(session.close())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=1)
        assert not provider_release.is_set()
        report.assert_not_awaited()
        provider_release.set()
        await asyncio.wait_for(second, timeout=1)
        await session._terminal_telemetry_task
        await session.close()
        ws.close.assert_awaited_once_with(code=1001, reason="user_left")
        report.assert_awaited_once_with(terminal=True)
    finally:
        provider_release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await session.close()
        await session._terminal_telemetry_task


async def test_repeated_iterator_cancellation_does_not_interrupt_openai_peer_close(monkeypatch):
    reading = asyncio.Event()
    peer_closing = asyncio.Event()
    peer_release = asyncio.Event()
    peer = FakeRTCPeerConnection()
    input_track = _OpenAIInputAudioTrack()
    connection = _OpenAIWebRTCConnection(peer, peer.channel, input_track)
    original_close = peer.close

    async def close_peer():
        peer_closing.set()
        await peer_release.wait()
        await original_close()

    peer.close = AsyncMock(side_effect=close_peer)
    session = AsyncRealtimeSession(_openai_session_info(), connection)
    report = AsyncMock()
    monkeypatch.setattr(session, "_report_telemetry", report)

    async def consume():
        reading.set()
        async for _ in session:
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reading.wait(), timeout=1)
        task.cancel()
        await asyncio.wait_for(peer_closing.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert peer.connectionState != "closed"
        report.assert_not_awaited()
        peer_release.set()
        await asyncio.wait_for(session.close(), timeout=1)
        assert peer.connectionState == "closed"
        assert input_track.readyState == "ended"
        peer.close.assert_awaited_once()
        await session._terminal_telemetry_task
        report.assert_awaited_once_with(terminal=True)
    finally:
        peer_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()
        await session._terminal_telemetry_task


@respx.mock
async def test_terminal_duration_stops_when_close_starts(monkeypatch):
    now = [100.0]
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished_at = []
    monkeypatch.setattr(
        realtime_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1_000)
    )

    async def close_provider(**kwargs):
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_finished_at.append(now[0])

    ws = SimpleNamespace(close=AsyncMock(side_effect=close_provider))
    session = AsyncRealtimeSession(_openai_session_info(), ws)
    original_report = session._report_telemetry

    async def delayed_report(**kwargs):
        now[0] = 200.0
        await original_report(**kwargs)

    monkeypatch.setattr(session, "_report_telemetry", delayed_report)
    route = respx.post(_openai_session_info().telemetry.endpoint).mock(
        return_value=httpx.Response(202)
    )
    now[0] = 105.0
    closing = asyncio.create_task(session.close())
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not closing.done()
        now[0] = 150.0
        cleanup_release.set()
        await asyncio.wait_for(closing, timeout=1)
        await session._terminal_telemetry_task
        await session.close()
        ws.close.assert_awaited_once()
        assert cleanup_finished_at == [150.0]
        assert now[0] == 200.0
        assert route.call_count == 1
        events = json.loads(route.calls.last.request.content)["events"]
        assert events[0]["data"]["quantity_millis"] == 5_000
        assert len(events) == 2
    finally:
        cleanup_release.set()
        await asyncio.gather(closing, return_exceptions=True)
        if session._terminal_telemetry_task is not None:
            await asyncio.gather(session._terminal_telemetry_task, return_exceptions=True)


@respx.mock
async def test_cancelled_iteration_closes_native_websocket_inside_context():
    server_closed = asyncio.Event()
    reading = asyncio.Event()
    frames = []

    async def provider(ws):
        await ws.wait_closed()
        server_closed.set()

    telemetry = respx.post(_openai_session_info().telemetry.endpoint).mock(
        return_value=httpx.Response(202)
    )
    async with serve(provider, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await realtime_module.ws_connect(f"ws://127.0.0.1:{port}")
        session = AsyncRealtimeSession(_openai_session_info(), ws)

        async def consume():
            async with session:
                reading.set()
                async for frame in session:
                    frames.append(frame)

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(reading.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            await asyncio.wait_for(server_closed.wait(), timeout=1)
            assert ws.protocol.close_code == 1000
            assert frames == []
            await session._terminal_telemetry_task
            assert telemetry.call_count == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await ws.close()


@pytest.mark.parametrize("code", [1000, 1011])
async def test_native_close_frame_requires_natural_completion_after_cleanup(monkeypatch, code):
    cleanup_complete = asyncio.Event()

    async def provider(ws):
        await ws.send(json.dumps({"type": "response.audio_transcript.delta", "delta": "Hello"}))
        await ws.close(code=code, reason="private provider reason")

    async with serve(provider, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await realtime_module.ws_connect(f"ws://127.0.0.1:{port}")
        native_close = ws.close

        async def close_provider(**kwargs):
            await native_close(**kwargs)
            cleanup_complete.set()

        close = AsyncMock(side_effect=close_provider)
        monkeypatch.setattr(ws, "close", close)
        session = AsyncRealtimeSession(_openai_session_info(), ws)
        report = AsyncMock()
        monkeypatch.setattr(session, "_report_telemetry", report)
        frames = []

        async def consume():
            async for frame in session:
                frames.append(frame)
                if frame["type"] == "close":
                    assert cleanup_complete.is_set()

        try:
            if code == 1000:
                await asyncio.wait_for(consume(), timeout=1)
                assert frames[-1] == {"type": "close", "reason": ""}
                # A completed session must not emit another terminal frame.
                await asyncio.wait_for(consume(), timeout=1)
                assert len(frames) == 2
            else:
                with pytest.raises(ConnectionClosedError):
                    await asyncio.wait_for(consume(), timeout=1)
                assert len(frames) == 1
            assert frames[0] == {
                "type": "transcript",
                "role": "assistant",
                "text": "Hello",
                "final": False,
            }
            assert cleanup_complete.is_set()
            close.assert_awaited_once()
            await session._terminal_telemetry_task
            report.assert_awaited_once_with(terminal=True)
        finally:
            await native_close()


async def test_completed_openai_iteration_does_not_wait_on_consumed_sentinel(monkeypatch):
    peer = FakeRTCPeerConnection()
    connection = _OpenAIWebRTCConnection(peer, peer.channel, _OpenAIInputAudioTrack())
    connection._finish_iteration()
    session = AsyncRealtimeSession(_openai_session_info(), connection)
    report = AsyncMock()
    monkeypatch.setattr(session, "_report_telemetry", report)

    async def consume():
        return [frame async for frame in session]

    assert await asyncio.wait_for(consume(), timeout=1) == [{"type": "close", "reason": ""}]
    assert peer.connectionState == "closed"
    assert await asyncio.wait_for(consume(), timeout=1) == []
    await session._terminal_telemetry_task
    report.assert_awaited_once_with(terminal=True)


@pytest.mark.parametrize("provider", ["openai", "xai", "google"])
async def test_cancelled_setup_closes_provider_without_delaying_cancellation(monkeypatch, provider):
    setup_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def block_setup(*args, **kwargs):
        setup_started.set()
        await asyncio.Future()

    async def close_provider(**kwargs):
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_done.set()

    info = _openai_session_info()
    if provider == "openai":

        class PausedPeer(FakeRTCPeerConnection):
            async def createOffer(self):  # noqa: N802
                await block_setup()

            async def close(self):
                await close_provider()
                await super().close()

        FakeRTCPeerConnection.instances = []
        monkeypatch.setattr(realtime_module, "RTCPeerConnection", PausedPeer)
    else:
        endpoint = (
            "wss://api.x.ai/v1/realtime"
            if provider == "xai"
            else "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
            "v1beta.GenerativeService.BidiGenerateContentConstrained"
        )
        info = info.model_copy(
            update={"provider": provider, "provider_transport": "websocket", "endpoint": endpoint}
        )
        ws = SimpleNamespace(
            send=AsyncMock(), recv=block_setup, close=AsyncMock(side_effect=close_provider)
        )
        monkeypatch.setattr(realtime_module, "ws_connect", AsyncMock(return_value=ws))

    task = asyncio.create_task(open_realtime_session(info))
    try:
        await asyncio.wait_for(setup_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not cleanup_done.is_set()
        cleanup_release.set()
        await asyncio.wait_for(cleanup_done.wait(), timeout=1)
        if provider == "openai":
            peer = PausedPeer.instances[0]
            assert peer.connectionState == "closed"
            assert peer.input_track.readyState == "ended"
        else:
            ws.close.assert_awaited_once_with(code=1011, reason="setup_failed")
    finally:
        cleanup_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("explicit_close", [False, True])
def test_asyncio_run_client_shutdown_finishes_native_session_report(monkeypatch, explicit_close):
    delivered = []
    info = _openai_session_info().model_copy(
        update={
            "provider": "xai",
            "provider_transport": "websocket",
            "endpoint": "wss://api.x.ai/v1/realtime",
        }
    )
    native_connect = realtime_module.ws_connect

    async def main():
        provider_closed = asyncio.Event()
        report_release = asyncio.Event()

        async def provider(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "session.updated"}))
            await ws.wait_closed()
            provider_closed.set()

        async def report(request):
            await report_release.wait()
            delivered.extend(json.loads(request.content)["events"])
            return httpx.Response(200)

        with respx.mock(assert_all_called=True) as router:
            router.post("https://api.test/v1/sessions").respond(json=info.model_dump(by_alias=True))
            telemetry = router.post(info.telemetry.endpoint).mock(side_effect=report)
            async with serve(provider, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                def connect(url, **kwargs):
                    assert url.startswith("wss://api.x.ai/v1/realtime?")
                    return native_connect(f"ws://127.0.0.1:{port}", **kwargs)

                monkeypatch.setattr(realtime_module, "ws_connect", connect)

                async def use_session(client):
                    session = await client.connect_realtime({"provider": "xai", "model": "test"})
                    async with session:
                        await session.send_audio(b"\x00\x00")
                    await asyncio.wait_for(provider_closed.wait(), timeout=1)
                    assert delivered == [], "session close waited for optional telemetry"

                if explicit_close:
                    client = AsyncSpeko(api_key="sk-test", base_url="https://api.test")
                    await asyncio.wait_for(use_session(client), timeout=1)
                    asyncio.get_running_loop().call_soon(report_release.set)
                    await client.close()
                else:
                    async with AsyncSpeko(api_key="sk-test", base_url="https://api.test") as client:
                        await asyncio.wait_for(use_session(client), timeout=1)
                        asyncio.get_running_loop().call_soon(report_release.set)
                assert [event["type"] for event in delivered] == [
                    "usage.reported",
                    "session.closed",
                ]
                assert telemetry.call_count == 1

    asyncio.run(main())
    assert len(delivered) == 2


@pytest.mark.parametrize("provider", ["openai", "xai", "google"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_asyncio_run_client_shutdown_finishes_interrupted_setup(monkeypatch, provider, cancelled):
    class SetupAbort(BaseException):
        pass

    cleanups = []

    async def main():
        setup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        if not cancelled:
            cleanup_release.set()

        async def abort_setup(*args, **kwargs):
            setup_started.set()
            if cancelled:
                await asyncio.Future()
            raise SetupAbort()

        async def cleanup(**kwargs):
            await cleanup_release.wait()
            cleanups.append(provider)

        info = _openai_session_info()
        if provider == "openai":

            class InterruptedPeer(FakeRTCPeerConnection):
                async def createOffer(self):  # noqa: N802
                    await abort_setup()

                async def close(self):
                    await cleanup()
                    await super().close()

            monkeypatch.setattr(realtime_module, "RTCPeerConnection", InterruptedPeer)
        else:
            endpoint = (
                "wss://api.x.ai/v1/realtime"
                if provider == "xai"
                else "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
                "v1beta.GenerativeService.BidiGenerateContentConstrained"
            )
            info = info.model_copy(
                update={
                    "provider": provider,
                    "provider_transport": "websocket",
                    "endpoint": endpoint,
                }
            )
            ws = SimpleNamespace(
                send=AsyncMock(), recv=abort_setup, close=AsyncMock(side_effect=cleanup)
            )
            monkeypatch.setattr(realtime_module, "ws_connect", AsyncMock(return_value=ws))

        with respx.mock(assert_all_called=True) as router:
            router.post("https://api.test/v1/sessions").respond(json=info.model_dump(by_alias=True))
            async with AsyncSpeko(api_key="sk-test", base_url="https://api.test") as client:
                if cancelled:
                    setup = asyncio.create_task(
                        client.connect_realtime({"provider": provider, "model": "test"})
                    )
                    await setup_started.wait()
                    setup.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await setup
                    assert cleanups == [], "setup cancellation waited for cleanup"
                    asyncio.get_running_loop().call_soon(cleanup_release.set)
                else:
                    with pytest.raises(SetupAbort):
                        await client.connect_realtime({"provider": provider, "model": "test"})
            assert cleanups == [provider]

    asyncio.run(main())
    assert cleanups == [provider]


def test_client_shutdown_drain_is_bounded_and_isolated(monkeypatch):
    async def main():
        release = asyncio.Event()
        delivered = []
        cancelled = []
        info = _openai_session_info().model_copy(
            update={
                "provider": "xai",
                "provider_transport": "websocket",
                "endpoint": "wss://api.x.ai/v1/realtime",
            }
        )
        other_info = info.model_copy(update={"session_id": "22222222-2222-4222-8222-222222222222"})
        sockets = []

        async def connect(*args, **kwargs):
            ws = SimpleNamespace(
                send=AsyncMock(),
                recv=AsyncMock(return_value=json.dumps({"type": "session.updated"})),
                close=AsyncMock(),
            )
            sockets.append(ws)
            return ws

        async def report(request):
            session_id = json.loads(request.content)["events"][0]["session_id"]
            if session_id == info.session_id:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(session_id)
                    raise
            delivered.append(session_id)
            return httpx.Response(200)

        monkeypatch.setattr(realtime_module, "ws_connect", connect)
        with respx.mock(assert_all_called=True) as router:
            router.post("https://api.test/v1/sessions").mock(
                side_effect=[
                    httpx.Response(200, json=entry.model_dump(by_alias=True))
                    for entry in (info, other_info)
                ]
            )
            telemetry = router.post(info.telemetry.endpoint).mock(side_effect=report)
            client = AsyncSpeko(api_key="sk-first", base_url="https://api.test")
            other = AsyncSpeko(api_key="sk-other", base_url="https://api.test")
            try:
                for owner in (client, other):
                    session = await owner.connect_realtime({"provider": "xai", "model": "test"})
                    await session.close()
                await asyncio.wait_for(other.close(), timeout=0.5)
                assert delivered == [other_info.session_id]
                await asyncio.wait_for(client.close(realtime_timeout=0.01), timeout=0.5)
                assert cancelled == []
                assert delivered == [other_info.session_id]
                release.set()
                await client.close()
                assert delivered == [other_info.session_id, info.session_id]
                assert telemetry.call_count == 2
                for ws in sockets:
                    ws.close.assert_awaited_once()
            finally:
                release.set()
                await client.close()
                await other.close()

    asyncio.run(main())


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("-inf"), float("nan")])
def test_client_shutdown_rejects_unbounded_timeout(timeout):
    async def main():
        client = AsyncSpeko(api_key="sk-test")
        with pytest.raises(ValueError, match="finite non-negative"):
            await client.close(realtime_timeout=timeout)
        await client.close(realtime_timeout=0)

    asyncio.run(main())


def test_client_shutdown_drains_report_added_after_cancelled_session_close(monkeypatch):
    async def main():
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        report_started = asyncio.Event()
        report_release = asyncio.Event()
        delivered = []
        info = _openai_session_info().model_copy(
            update={
                "provider": "xai",
                "provider_transport": "websocket",
                "endpoint": "wss://api.x.ai/v1/realtime",
            }
        )

        async def cleanup(**kwargs):
            cleanup_started.set()
            await cleanup_release.wait()

        async def report(request):
            report_started.set()
            await report_release.wait()
            delivered.extend(json.loads(request.content)["events"])
            return httpx.Response(200)

        ws = SimpleNamespace(
            send=AsyncMock(),
            recv=AsyncMock(return_value=json.dumps({"type": "session.updated"})),
            close=AsyncMock(side_effect=cleanup),
        )
        monkeypatch.setattr(realtime_module, "ws_connect", AsyncMock(return_value=ws))
        with respx.mock(assert_all_called=True) as router:
            router.post("https://api.test/v1/sessions").respond(json=info.model_dump(by_alias=True))
            telemetry = router.post(info.telemetry.endpoint).mock(side_effect=report)
            client = AsyncSpeko(api_key="sk-test", base_url="https://api.test")
            try:
                session = await client.connect_realtime({"provider": "xai", "model": "test"})
                closing_session = asyncio.create_task(session.close())
                await cleanup_started.wait()
                closing_session.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing_session
                assert not report_started.is_set()

                closing_client = asyncio.create_task(client.close(realtime_timeout=1))
                await asyncio.sleep(0)
                assert not closing_client.done()
                assert not report_started.is_set()

                cleanup_release.set()
                await asyncio.wait_for(report_started.wait(), timeout=1)
                await asyncio.sleep(0)
                assert not closing_client.done(), "drain ignored the later terminal report"
                report_release.set()
                await asyncio.wait_for(closing_client, timeout=1)

                assert [event["type"] for event in delivered] == [
                    "usage.reported",
                    "session.closed",
                ]
                assert telemetry.call_count == 1
                ws.close.assert_awaited_once()
                assert client._realtime_tasks == set()
                await client.close()
                assert client._realtime_tasks == set()
                assert telemetry.call_count == 1
            finally:
                cleanup_release.set()
                report_release.set()
                await client.close()

    asyncio.run(main())
