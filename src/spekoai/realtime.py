"""Realtime (S2S) session handle for the async Speko client.

Only available on ``AsyncSpeko`` — a synchronous WebSocket loop blocks the
event loop on every audio chunk, which defeats the purpose of a low-latency
S2S pipeline.

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
import json
from collections.abc import AsyncIterator
from typing import Any, Optional

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from spekoai.models import RealtimeSessionInfo

RealtimeFrame = dict[str, Any]


class AsyncRealtimeSession:
    """Active WebSocket to the Speko S2S proxy.

    Iterate to receive frames; call ``send_audio`` / ``commit`` /
    ``send_tool_result`` / ``interrupt`` to push state to the provider.
    Use as an async context manager so the socket closes deterministically
    on exceptions.
    """

    def __init__(self, info: RealtimeSessionInfo, ws: ClientConnection) -> None:
        self._info = info
        self._ws = ws
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._info.session_id

    @property
    def expires_at(self) -> str:
        return self._info.expires_at

    async def send_audio(self, pcm: bytes) -> None:
        """Ship a PCM16 audio chunk to the server (binary frame).

        Users holding ``bytearray``/``memoryview`` should wrap with
        ``bytes(...)`` at the call site — the signature is narrowed to
        ``bytes`` to keep the type surface minimal.
        """
        if self._closed:
            return
        await self._ws.send(pcm)

    async def commit(self) -> None:
        """Signal end-of-user-turn; server flushes buffered audio upstream."""
        await self._send_json({"t": "control", "action": "commit"})

    async def interrupt(self) -> None:
        """Cancel the assistant's current response mid-generation."""
        await self._send_json({"t": "interrupt"})

    async def send_tool_result(self, call_id: str, output: str) -> None:
        """Return the result of a previously-issued tool call."""
        await self._send_json(
            {"t": "tool_result", "callId": call_id, "output": output}
        )

    async def close(self, code: int = 1000, reason: str = "client_closed") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass

    async def __aenter__(self) -> AsyncRealtimeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[RealtimeFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[RealtimeFrame]:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray, memoryview)):
                    yield {
                        "type": "audio",
                        "pcm": bytes(raw),
                        "sample_rate": 24000,
                    }
                    continue
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                frame = _translate_frame(parsed)
                if frame is not None:
                    yield frame
        finally:
            self._closed = True

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        await self._ws.send(json.dumps(payload, separators=(",", ":")))


def _translate_frame(parsed: Any) -> Optional[RealtimeFrame]:
    if not isinstance(parsed, dict):
        return None
    t = parsed.get("t")
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


async def open_realtime_session(
    info: RealtimeSessionInfo,
    *,
    timeout: float = 10.0,
) -> AsyncRealtimeSession:
    """Open the WebSocket for a session previously created via POST /v1/sessions.

    Passes the short-lived ``ws_token`` as the first WebSocket subprotocol —
    browsers can't set headers on ``new WebSocket()`` and the Python SDK
    matches that pattern for symmetry with the JS SDK.
    """
    ws = await asyncio.wait_for(
        ws_connect(info.ws_url, subprotocols=[info.ws_token]),
        timeout=timeout,
    )
    return AsyncRealtimeSession(info, ws)
