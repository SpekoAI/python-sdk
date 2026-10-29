"""Provider-direct realtime (S2S) handle for the async Speko client.

Only available on ``AsyncSpeko`` — synchronous realtime I/O blocks the event
loop on every audio chunk, which defeats the purpose of a low-latency S2S
pipeline.

Example::

    async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
        session = await speko.connect_realtime(
            RealtimeConnectParams(provider="openai", model="gpt-realtime"),
        )
        async with session:
            await session.send_audio(pcm_chunk)
            async for frame in session:
                if frame["type"] == "audio":
                    play(frame["pcm"])
                elif frame["type"] == "transcript":
                    print(frame["text"])
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from collections.abc import AsyncIterator, Coroutine
from fractions import Fraction
from typing import Any, Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from spekoai._user_agent import USER_AGENT
from spekoai.models import RealtimeSessionInfo

RealtimeFrame = dict[str, Any]
_WireMessage = Union[str, bytes]
_CLOSED = object()
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _start_background_task(
    coroutine: Coroutine[Any, Any, None],
    pending_tasks: Optional[set[asyncio.Task[None]]] = None,
) -> asyncio.Task[None]:
    # Setup can be cancelled before a session handle exists. Keep cleanup alive.
    task = asyncio.create_task(coroutine)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_background_task_finished)
    if pending_tasks is not None:
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)
    return task


def _background_task_finished(task: asyncio.Task[None]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


async def _drain_background_tasks(pending_tasks: set[asyncio.Task[None]], timeout: float) -> None:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("realtime_timeout must be a finite non-negative number")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = {task for task in pending_tasks if not task.done()}
        remaining = deadline - loop.time()
        if not pending or remaining <= 0:
            return
        # Cleanup can add a terminal report, so recheck after each batch.
        # A deadline or caller cancellation must not cancel provider cleanup.
        await asyncio.wait(pending, timeout=remaining)


class _OpenAIInputAudioTrack(AudioStreamTrack):
    """Paced 24 kHz mono PCM input for OpenAI's WebRTC media path."""

    _sample_rate = 24000
    _samples_per_frame = 480
    _bytes_per_frame = _samples_per_frame * 2

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._pending = bytearray()
        self._pts = 0
        self._next_frame_at: Optional[float] = None

    async def push(self, pcm: bytes) -> None:
        if self.readyState != "live" or not pcm:
            return
        self._pending.extend(pcm)
        while len(self._pending) >= self._bytes_per_frame:
            frame = bytes(self._pending[: self._bytes_per_frame])
            del self._pending[: self._bytes_per_frame]
            await self._queue.put(frame)

    async def flush(self) -> None:
        if self.readyState != "live" or not self._pending:
            return
        padded = bytes(self._pending).ljust(self._bytes_per_frame, b"\0")
        self._pending.clear()
        await self._queue.put(padded)

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            return await super().recv()
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_frame_at is None:
            self._next_frame_at = now
        if self._next_frame_at > now:
            await asyncio.sleep(self._next_frame_at - now)
        self._next_frame_at = max(self._next_frame_at, loop.time()) + (
            self._samples_per_frame / self._sample_rate
        )
        try:
            pcm = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pcm = bytes(self._bytes_per_frame)
        frame = AudioFrame(format="s16", layout="mono", samples=self._samples_per_frame)
        plane = frame.planes[0]
        plane.update(pcm.ljust(plane.buffer_size, b"\0"))
        frame.pts = self._pts
        frame.sample_rate = self._sample_rate
        frame.time_base = Fraction(1, self._sample_rate)
        self._pts += self._samples_per_frame
        return frame


class _OpenAIWebRTCConnection:
    def __init__(
        self,
        peer: RTCPeerConnection,
        channel: Any,
        input_track: _OpenAIInputAudioTrack,
    ) -> None:
        self._peer = peer
        self._channel = channel
        self._input_track = input_track
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._closed = False
        self._iteration_finished = False
        self._output_tasks: set[asyncio.Task[Any]] = set()

        @channel.on("open")
        def on_open() -> None:
            self._ready.set()

        @channel.on("message")
        def on_message(message: Any) -> None:
            if isinstance(message, str):
                self._incoming.put_nowait(message)

        @channel.on("close")
        def on_channel_close() -> None:
            self._finish_iteration()

        @peer.on("track")
        def on_track(track: Any) -> None:
            if getattr(track, "kind", "") != "audio":
                return
            task = asyncio.create_task(self._receive_audio(track))
            self._output_tasks.add(task)
            task.add_done_callback(self._output_tasks.discard)

        @peer.on("connectionstatechange")
        def on_connection_state_change() -> None:
            if peer.connectionState in ("failed", "closed"):
                self._finish_iteration()

    async def wait_ready(self, timeout: float) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def send(self, payload: str) -> None:
        if self._closed or self._channel.readyState != "open":
            raise RuntimeError("OpenAI WebRTC data channel is not open")
        self._channel.send(payload)

    async def send_audio(self, pcm: bytes) -> None:
        if not self._closed:
            await self._input_track.push(pcm)

    async def commit_audio(self) -> None:
        if not self._closed:
            await self._input_track.flush()

    async def recv(self) -> _WireMessage:
        message = await self._incoming.get()
        if message is _CLOSED:
            raise EOFError("OpenAI WebRTC connection closed")
        if not isinstance(message, (str, bytes)):
            raise TypeError("Invalid OpenAI WebRTC message")
        return message

    def __aiter__(self) -> AsyncIterator[_WireMessage]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[_WireMessage]:
        # AsyncRealtimeSession owns transport cleanup, including cancellation.
        while True:
            message = await self._incoming.get()
            if message is _CLOSED:
                return
            if isinstance(message, (str, bytes)):
                yield message

    async def close(self, code: int = 1000, reason: str = "client_closed") -> None:
        del code, reason
        if self._closed:
            return
        self._closed = True
        self._input_track.stop()
        try:
            self._channel.close()
        except Exception:
            pass
        output_tasks = list(self._output_tasks)
        for task in output_tasks:
            task.cancel()
        if output_tasks:
            await asyncio.gather(*output_tasks, return_exceptions=True)
        await self._peer.close()
        self._finish_iteration()

    async def _receive_audio(self, track: Any) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=24000)
        try:
            while not self._closed:
                source = await track.recv()
                for frame in resampler.resample(source):
                    size = frame.samples * 2
                    await self._incoming.put(bytes(frame.planes[0])[:size])
        except (asyncio.CancelledError, EOFError):
            return
        except Exception:
            self._finish_iteration()

    def _finish_iteration(self) -> None:
        if self._iteration_finished:
            return
        self._iteration_finished = True
        self._incoming.put_nowait(_CLOSED)


async def _close_transport(
    ws: Union[ClientConnection, _OpenAIWebRTCConnection], *, code: int, reason: str
) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


class AsyncRealtimeSession:
    """Active connection from the caller process to the selected provider.

    Iterate to receive frames; call ``send_audio`` / ``commit`` /
    ``send_tool_result`` / ``interrupt`` to push state to the provider.
    Use as an async context manager so the socket closes deterministically
    on exceptions.
    """

    def __init__(
        self,
        info: RealtimeSessionInfo,
        ws: Union[ClientConnection, _OpenAIWebRTCConnection],
        *,
        _pending_tasks: Optional[set[asyncio.Task[None]]] = None,
    ) -> None:
        self._info = info
        self._ws = ws
        self._closed = False
        self._close_frame_emitted = False
        self._pending_tasks = _pending_tasks
        self._opened_at = time.monotonic()
        self._closed_at: Optional[float] = None
        self._telemetry_sequence = 0
        self._google_tool_names: dict[str, str] = {}
        self._telemetry_task = asyncio.create_task(self._telemetry_loop())
        self._close_task: Optional[asyncio.Task[None]] = None
        self._terminal_telemetry_task: Optional[asyncio.Task[None]] = None

    @property
    def session_id(self) -> str:
        return self._info.session_id

    @property
    def expires_at(self) -> str:
        return self._info.expires_at

    @property
    def input_sample_rate(self) -> int:
        return self._info.input_sample_rate or 24000

    @property
    def output_sample_rate(self) -> int:
        return self._info.output_sample_rate or 24000

    async def send_audio(self, pcm: bytes) -> None:
        """Ship a PCM16 audio chunk directly to the provider.

        Users holding ``bytearray``/``memoryview`` should wrap with
        ``bytes(...)`` at the call site — the signature is narrowed to
        ``bytes`` to keep the type surface minimal.
        """
        if self._closed:
            return
        if isinstance(self._ws, _OpenAIWebRTCConnection):
            await self._ws.send_audio(pcm)
            return
        audio = base64.b64encode(pcm).decode("ascii")
        if self._info.provider == "google":
            await self._send_json(
                {
                    "realtimeInput": {
                        "audio": {
                            "mimeType": "audio/pcm;rate=16000",
                            "data": audio,
                        }
                    }
                }
            )
            return
        await self._send_json({"type": "input_audio_buffer.append", "audio": audio})

    async def commit(self) -> None:
        """Signal end-of-user-turn without creating a duplicate VAD response."""
        if isinstance(self._ws, _OpenAIWebRTCConnection):
            # The RTP track continues with silence so OpenAI server VAD closes
            # the turn and creates the response exactly once.
            await self._ws.commit_audio()
            return
        if self._info.provider == "google":
            await self._send_json({"realtimeInput": {"audioStreamEnd": True}})
            return
        await self._send_json({"type": "input_audio_buffer.commit"})

    async def interrupt(self) -> None:
        """Cancel the assistant's current response mid-generation."""
        if self._info.provider == "google":
            await self._send_json({"realtimeInput": {"activityEnd": {}}})
            return
        await self._send_json({"type": "response.cancel"})
        if isinstance(self._ws, _OpenAIWebRTCConnection):
            await self._send_json({"type": "output_audio_buffer.clear"})

    async def send_tool_result(self, call_id: str, output: str) -> None:
        """Return the result of a previously-issued tool call."""
        if self._info.provider == "google":
            name = self._google_tool_names.pop(call_id, None)
            if name is None:
                raise ValueError(f"Unknown Gemini Live tool call id: {call_id}")
            try:
                response = json.loads(output)
            except (ValueError, TypeError):
                response = {"result": output}
            if not isinstance(response, dict):
                response = {"result": response}
            await self._send_json(
                {
                    "toolResponse": {
                        "functionResponses": [{"id": call_id, "name": name, "response": response}]
                    }
                }
            )
            return
        await self._send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        await self._send_json({"type": "response.create"})

    async def close(self, code: int = 1000, reason: str = "client_closed") -> None:
        if self._close_task is None:
            self._closed = True
            self._closed_at = time.monotonic()
            self._telemetry_task.cancel()
            self._close_task = _start_background_task(
                self._finish_close(code=code, reason=reason), self._pending_tasks
            )
        await asyncio.shield(self._close_task)

    async def _finish_close(self, *, code: int, reason: str) -> None:
        await _close_transport(self._ws, code=code, reason=reason)
        # Reporting is optional. It must never delay closing the provider or caller.
        self._terminal_telemetry_task = _start_background_task(
            self._report_telemetry(terminal=True), self._pending_tasks
        )

    async def __aenter__(self) -> AsyncRealtimeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[RealtimeFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[RealtimeFrame]:
        if self._close_frame_emitted:
            return
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray, memoryview)):
                    yield {
                        "type": "audio",
                        "pcm": bytes(raw),
                        "sample_rate": self.output_sample_rate,
                    }
                    continue
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                self._remember_google_tool_names(parsed)
                for frame in _translate_provider_frames(self._info, parsed):
                    yield frame
        finally:
            await self.close(reason="provider_closed")
        if not self._close_frame_emitted:
            self._close_frame_emitted = True
            yield {"type": "close", "reason": ""}

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    def _remember_google_tool_names(self, parsed: Any) -> None:
        if self._info.provider != "google" or not isinstance(parsed, dict):
            return
        tool_call = parsed.get("toolCall")
        calls = tool_call.get("functionCalls") if isinstance(tool_call, dict) else None
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            name = call.get("name")
            if isinstance(call_id, str) and isinstance(name, str) and call_id and name:
                self._google_tool_names[call_id] = name

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._info.telemetry.flush_interval_ms / 1000)
                await self._report_telemetry(terminal=False)
        except asyncio.CancelledError:
            return

    async def _report_telemetry(self, *, terminal: bool) -> None:
        observed_at = (
            self._closed_at if terminal and self._closed_at is not None else time.monotonic()
        )
        elapsed_ms = max(0, int((observed_at - self._opened_at) * 1000))
        authorized_ms = self._info.reservation.authorized_duration_seconds * 1000
        quantity_ms = min(elapsed_ms, authorized_ms)
        self._telemetry_sequence += 1
        created_at_ms = int(time.time() * 1000)
        common = {
            "session_id": self._info.session_id,
            "attempt_id": self._info.attempt_id,
            "created_at_ms": created_at_ms,
        }
        events: list[dict[str, Any]] = [
            {
                **common,
                "type": "usage.reported",
                "event_id": (f"{self._info.attempt_id}:usage.reported:{self._telemetry_sequence}"),
                "data": {
                    "unit": "duration_seconds",
                    "quantity_millis": quantity_ms,
                },
            }
        ]
        if terminal:
            events.append(
                {
                    **common,
                    "type": "session.closed",
                    "event_id": f"{self._info.attempt_id}:session.closed",
                }
            )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    self._info.telemetry.endpoint,
                    headers={
                        "Authorization": f"Bearer {self._info.telemetry.token}",
                        "User-Agent": USER_AGENT,
                    },
                    json={"events": events},
                )
        except Exception:
            # Telemetry is a best-effort hint. Runtime's provider-side
            # reconciliation remains the billing authority.
            pass


def _translate_frame(parsed: Any) -> Optional[RealtimeFrame]:
    """Translate the removed Speko-proxy wire format for source compatibility."""
    if not isinstance(parsed, dict):
        return None
    t = parsed.get("t")
    if t == "ready":
        return {
            "type": "ready",
            "input_sample_rate": int(parsed.get("inputSampleRate") or 24000),
            "output_sample_rate": int(parsed.get("outputSampleRate") or 24000),
        }
    if t == "interruption":
        return {
            "type": "interruption",
            "at": "assistant" if parsed.get("at") == "assistant" else "user",
        }
    if t == "server_tool_call":
        return {
            "type": "server_tool_call",
            "id": parsed.get("id", ""),
            "name": parsed.get("name", ""),
            "status": parsed.get("status", "started"),
        }
    if t == "transcript":
        return {
            "type": "transcript",
            "role": parsed.get("role", "assistant"),
            "text": parsed.get("text", ""),
            "final": bool(parsed.get("final", False)),
        }
    if t == "tool_call":
        return {
            "type": "tool_call",
            "call_id": parsed.get("callId", ""),
            "name": parsed.get("name", ""),
            "arguments": parsed.get("arguments", ""),
        }
    if t == "usage":
        return {
            "type": "usage",
            "input_audio_tokens": int(parsed.get("inputAudioTokens") or 0),
            "output_audio_tokens": int(parsed.get("outputAudioTokens") or 0),
        }
    if t == "error":
        return {
            "type": "error",
            "code": parsed.get("code", "UNKNOWN"),
            "message": parsed.get("message", ""),
        }
    if t == "end":
        return {"type": "close", "reason": parsed.get("reason", "")}
    return None


def _translate_provider_frames(info: RealtimeSessionInfo, parsed: Any) -> list[RealtimeFrame]:
    if not isinstance(parsed, dict):
        return []
    if info.provider == "google":
        return _translate_google_frames(parsed, info.output_sample_rate or 24000)

    event_type = parsed.get("type")
    if event_type in ("response.output_audio.delta", "response.audio.delta"):
        return _audio_frames(parsed.get("delta"), info.output_sample_rate or 24000)
    if event_type in (
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.updated",
    ):
        value = parsed.get("delta") or parsed.get("transcript")
        return [_transcript_frame("user", value, False)] if value else []
    if event_type == "conversation.item.input_audio_transcription.completed":
        value = parsed.get("transcript")
        return [_transcript_frame("user", value, True)] if value else []
    if event_type in (
        "response.output_audio_transcript.delta",
        "response.audio_transcript.delta",
    ):
        value = parsed.get("delta")
        return [_transcript_frame("assistant", value, False)] if value else []
    if event_type in (
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
    ):
        value = parsed.get("transcript")
        return [_transcript_frame("assistant", value, True)] if value else []
    if event_type == "input_audio_buffer.speech_started":
        return [{"type": "interruption", "at": "user"}]
    if event_type == "response.function_call_arguments.done":
        return [
            {
                "type": "tool_call",
                "call_id": str(parsed.get("call_id") or ""),
                "name": str(parsed.get("name") or ""),
                "arguments": str(parsed.get("arguments") or ""),
            }
        ]
    if event_type == "response.done":
        response = parsed.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            input_details = usage.get("input_token_details")
            output_details = usage.get("output_token_details")
            input_details = input_details if isinstance(input_details, dict) else {}
            output_details = output_details if isinstance(output_details, dict) else {}
            return [
                {
                    "type": "usage",
                    "input_audio_tokens": int(input_details.get("audio_tokens") or 0),
                    "output_audio_tokens": int(output_details.get("audio_tokens") or 0),
                }
            ]
        return []
    if event_type == "error":
        provider_error = parsed.get("error")
        provider_error = provider_error if isinstance(provider_error, dict) else {}
        return [
            {
                "type": "error",
                "code": str(provider_error.get("code") or "PROVIDER_ERROR"),
                "message": str(provider_error.get("message") or "Provider realtime error"),
            }
        ]
    return []


def _translate_google_frames(parsed: dict[str, Any], sample_rate: int) -> list[RealtimeFrame]:
    frames: list[RealtimeFrame] = []
    content = parsed.get("serverContent")
    content = content if isinstance(content, dict) else {}
    input_transcript = content.get("inputTranscription")
    if isinstance(input_transcript, dict) and input_transcript.get("text"):
        frames.append(_transcript_frame("user", input_transcript["text"], True))
    output_transcript = content.get("outputTranscription")
    if isinstance(output_transcript, dict) and output_transcript.get("text"):
        frames.append(
            _transcript_frame(
                "assistant", output_transcript["text"], bool(content.get("turnComplete"))
            )
        )
    model_turn = content.get("modelTurn")
    parts = model_turn.get("parts", []) if isinstance(model_turn, dict) else []
    if isinstance(parts, list):
        for part in parts:
            inline = part.get("inlineData") if isinstance(part, dict) else None
            if isinstance(inline, dict):
                frames.extend(_audio_frames(inline.get("data"), sample_rate))
    if content.get("interrupted") is True:
        frames.append({"type": "interruption", "at": "assistant"})
    tool_call = parsed.get("toolCall")
    function_calls = tool_call.get("functionCalls") if isinstance(tool_call, dict) else None
    if isinstance(function_calls, list):
        for call in function_calls:
            if not isinstance(call, dict):
                continue
            arguments = call.get("args")
            frames.append(
                {
                    "type": "tool_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "arguments": (
                        arguments
                        if isinstance(arguments, str)
                        else json.dumps(arguments or {}, separators=(",", ":"))
                    ),
                }
            )
    usage = parsed.get("usageMetadata")
    if isinstance(usage, dict):
        frames.append(
            {
                "type": "usage",
                "input_audio_tokens": int(usage.get("promptTokenCount") or 0),
                "output_audio_tokens": int(usage.get("responseTokenCount") or 0),
            }
        )
    provider_error = parsed.get("error")
    if isinstance(provider_error, dict):
        frames.append(
            {
                "type": "error",
                "code": str(
                    provider_error.get("status") or provider_error.get("code") or "PROVIDER_ERROR"
                ),
                "message": str(provider_error.get("message") or "Gemini Live error"),
            }
        )
    return frames


def _audio_frames(value: Any, sample_rate: int) -> list[RealtimeFrame]:
    if not isinstance(value, str) or not value:
        return []
    try:
        pcm = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return []
    return [{"type": "audio", "pcm": pcm, "sample_rate": sample_rate}]


def _transcript_frame(role: str, text: Any, final: bool) -> RealtimeFrame:
    return {
        "type": "transcript",
        "role": role,
        "text": str(text),
        "final": final,
    }


def _provider_session_update(info: RealtimeSessionInfo) -> dict[str, Any]:
    session = info.session
    if info.provider == "google":
        generation: dict[str, Any] = {"responseModalities": ["AUDIO"]}
        if session and session.voice:
            generation["speechConfig"] = {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": session.voice}}
            }
        if session and session.temperature is not None:
            generation["temperature"] = session.temperature
        setup: dict[str, Any] = {
            "model": f"models/{info.model.removeprefix('models/')}",
            "generationConfig": generation,
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "sessionResumption": {},
        }
        if session and session.instructions is not None:
            setup["systemInstruction"] = {"parts": [{"text": session.instructions}]}
        if session and session.tools:
            setup["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in session.tools
                    ]
                }
            ]
        return {"setup": setup}

    provider_session: dict[str, Any] = {
        "type": "realtime",
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {
                    "model": "grok-transcribe"
                    if info.provider == "xai"
                    else "gpt-4o-mini-transcribe"
                },
                **({} if info.provider == "xai" else {"turn_detection": {"type": "server_vad"}}),
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        },
    }
    if info.provider == "xai":
        provider_session["model"] = info.model
        provider_session["turn_detection"] = {"type": "server_vad"}
    if session and session.voice:
        if info.provider == "xai":
            provider_session["voice"] = session.voice
        else:
            provider_session["audio"]["output"]["voice"] = session.voice
    if session and session.instructions is not None:
        provider_session["instructions"] = session.instructions
    if session and session.tools:
        provider_session["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in session.tools
        ]
        provider_session["tool_choice"] = "auto"
    return {"type": "session.update", "session": provider_session}


def _provider_url(info: RealtimeSessionInfo) -> str:
    if info.provider == "openai":
        raise ValueError("OpenAI realtime requires its negotiated WebRTC transport")
    if info.provider_transport != "websocket":
        raise ValueError(f"{info.provider} realtime requires WebSocket")
    parsed = urlsplit(info.endpoint)
    expected_host = {
        "openai": "api.openai.com",
        "xai": "api.x.ai",
        "google": "generativelanguage.googleapis.com",
    }[info.provider]
    expected_path = {
        "openai": "/v1/realtime/calls",
        "xai": "/v1/realtime",
        "google": (
            "/ws/google.ai.generativelanguage.v1beta."
            "GenerativeService.BidiGenerateContentConstrained"
        ),
    }[info.provider]
    if (
        parsed.scheme != "wss"
        or parsed.hostname != expected_host
        or parsed.port not in (None, 443)
        or parsed.path != expected_path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Invalid {info.provider} realtime endpoint")
    query = dict(parse_qsl(parsed.query))
    query["access_token" if info.provider == "google" else "model"] = (
        info.credential.value if info.provider == "google" else info.model
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _validated_openai_webrtc_endpoint(info: RealtimeSessionInfo) -> str:
    if info.provider_transport != "webrtc":
        raise ValueError("OpenAI realtime requires WebRTC")
    parsed = urlsplit(info.endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.openai.com"
        or parsed.port not in (None, 443)
        or parsed.path != "/v1/realtime/calls"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid openai realtime endpoint")
    return info.endpoint


def _validated_sideband_url(info: RealtimeSessionInfo) -> str:
    if not info.sideband_url:
        raise ValueError("OpenAI billing sideband URL is missing")
    sideband = urlsplit(info.sideband_url)
    telemetry = urlsplit(info.telemetry.endpoint)
    expected_path = f"/v1/sessions/{info.session_id}/sidebands/openai"

    def effective_port(scheme: str, port: Optional[int]) -> Optional[int]:
        if port is not None:
            return port
        return 443 if scheme == "https" else None

    if (
        telemetry.scheme != "https"
        or sideband.scheme != telemetry.scheme
        or sideband.hostname != telemetry.hostname
        or effective_port(sideband.scheme, sideband.port)
        != effective_port(telemetry.scheme, telemetry.port)
        or sideband.path != expected_path
        or sideband.username
        or sideband.password
        or sideband.query
        or sideband.fragment
        or telemetry.username
        or telemetry.password
    ):
        raise ValueError("Invalid OpenAI billing sideband URL")
    return info.sideband_url


def _openai_call_id(location: Optional[str]) -> str:
    if not location:
        raise RuntimeError("OpenAI WebRTC response omitted the call location")
    parsed = urlsplit(location)
    if (
        (parsed.scheme and parsed.scheme != "https")
        or (parsed.hostname and parsed.hostname != "api.openai.com")
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("OpenAI WebRTC response has an invalid call location")
    prefix = "/v1/realtime/calls/"
    if not parsed.path.startswith(prefix):
        raise RuntimeError("OpenAI WebRTC response has an invalid call location")
    call_id = parsed.path[len(prefix) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", call_id):
        raise RuntimeError("OpenAI WebRTC response has an invalid call id")
    return call_id


async def _open_openai_webrtc(
    info: RealtimeSessionInfo,
    *,
    timeout: float,
    _pending_tasks: Optional[set[asyncio.Task[None]]] = None,
) -> _OpenAIWebRTCConnection:
    endpoint = _validated_openai_webrtc_endpoint(info)
    sideband_url = _validated_sideband_url(info)
    peer = RTCPeerConnection()
    input_track = _OpenAIInputAudioTrack()
    peer.addTrack(input_track)
    channel = peer.createDataChannel("oai-events")
    connection = _OpenAIWebRTCConnection(peer, channel, input_track)
    try:
        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        local_description = peer.localDescription
        offer_sdp = local_description.sdp if local_description is not None else ""
        if not offer_sdp:
            raise RuntimeError("OpenAI WebRTC offer has no SDP")
        create_session = dict(_provider_session_update(info)["session"])
        create_session["model"] = info.model
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            answer = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {info.credential.value}"},
                files={
                    "sdp": (None, offer_sdp, "application/sdp"),
                    "session": (
                        None,
                        json.dumps(create_session, separators=(",", ":")),
                        "application/json",
                    ),
                },
            )
            if answer.status_code != 201:
                raise RuntimeError(f"OpenAI WebRTC setup failed with HTTP {answer.status_code}")
            answer_sdp = answer.text
            if not answer_sdp or len(answer_sdp) > 128 << 10:
                raise RuntimeError("OpenAI WebRTC answer is invalid")
            call_id = _openai_call_id(answer.headers.get("Location"))
            bound = await client.post(
                sideband_url,
                headers={
                    "Authorization": f"Bearer {info.telemetry.token}",
                    "User-Agent": USER_AGENT,
                },
                json={"attempt_id": info.attempt_id, "provider_session_id": call_id},
            )
            if bound.status_code < 200 or bound.status_code >= 300:
                raise RuntimeError(f"OpenAI billing sideband failed with HTTP {bound.status_code}")
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
        await connection.wait_ready(timeout)
        return connection
    except BaseException as error:
        cleanup = _start_background_task(
            _close_transport(connection, code=1011, reason="setup_failed"), _pending_tasks
        )
        if not isinstance(error, asyncio.CancelledError):
            await asyncio.shield(cleanup)
        raise


async def open_realtime_session(
    info: RealtimeSessionInfo,
    *,
    timeout: float = 10.0,
    _pending_tasks: Optional[set[asyncio.Task[None]]] = None,
) -> AsyncRealtimeSession:
    """Open a provider connection from a direct session bootstrap response."""
    if info.provider == "openai":
        ws: Union[ClientConnection, _OpenAIWebRTCConnection] = await _open_openai_webrtc(
            info, timeout=timeout, _pending_tasks=_pending_tasks
        )
    elif info.provider == "xai":
        connect = ws_connect(
            _provider_url(info),
            subprotocols=[f"xai-client-secret.{info.credential.value}"],
        )
        ws = await asyncio.wait_for(connect, timeout=timeout)
    else:
        connect = ws_connect(_provider_url(info))
        ws = await asyncio.wait_for(connect, timeout=timeout)
    try:
        await ws.send(json.dumps(_provider_session_update(info), separators=(",", ":")))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Provider realtime session did not become ready")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if not isinstance(raw, str):
                continue
            parsed = json.loads(raw)
            if info.provider == "google" and parsed.get("setupComplete") is not None:
                break
            if info.provider != "google" and parsed.get("type") == "session.updated":
                break
            if parsed.get("type") == "error" or parsed.get("error"):
                raise RuntimeError(f"Provider realtime setup failed: {parsed}")
    except BaseException as error:
        cleanup = _start_background_task(
            _close_transport(ws, code=1011, reason="setup_failed"), _pending_tasks
        )
        if not isinstance(error, asyncio.CancelledError):
            await asyncio.shield(cleanup)
        raise
    return AsyncRealtimeSession(info, ws, _pending_tasks=_pending_tasks)
