"""A small Translation API v1 server for tests, written from the spec (2026-11-08).

It behaves like a test session in the spec's "Test mode" section and docs/test-mode.md:
it echoes each frame back after a delay, sends the scripted transcripts, and asks for a
reconnect on a schedule. It checks what clients send as strictly as the spec says Speko
does.

Run it on its own to try the example without a Speko account:

    python tests/translation_mock.py --port 8787

The HTTP API listens on one port and the stream on another. That is allowed, because
clients use ``stream_url`` as given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

API_VERSION = "2026-11-08"
FRAME_BYTES = 976
SAMPLES_PER_FRAME = 480
FRAME_MS = 20
# docs/test-mode.md: "The fake holds at most 1.7 s of audio that has not come back yet."
TEST_MODE_HELD_FRAMES = 85
# docs/test-mode.md: "Every fifth item has no transcript.source."
SOURCELESS_EVERY = 5
DOCS = "https://docs.speko.dev/translation/errors#"

# code: (type, HTTP status). The close code is 4000 + status.
ERRORS = {
    "parameter_invalid": ("invalid_request_error", 400),
    "unsupported_language": ("invalid_request_error", 400),
    "resource_missing": ("invalid_request_error", 404),
    "idempotency_key_reused": ("invalid_request_error", 409),
    "frame_invalid": ("invalid_request_error", 400),
    "message_invalid": ("invalid_request_error", 400),
    "client_too_slow": ("invalid_request_error", 408),
    "audio_timeout": ("invalid_request_error", 408),
    "runtime_timeout": ("invalid_request_error", 408),
    "key_invalid": ("authentication_error", 401),
    "client_secret_invalid": ("authentication_error", 401),
    "resume_token_invalid": ("authentication_error", 401),
    "org_concurrency_limit": ("rate_limit_error", 429),
    "balance_exhausted": ("balance_error", 402),
    "translation_failed": ("api_error", 502),
    "service_unavailable": ("api_error", 503),
    "internal": ("api_error", 500),
}
# Triggers that fire a while after `ready`. The other two fire at connect.
LATE_TRIGGERS = {
    "translation_failed",
    "balance_exhausted",
    "client_too_slow",
    "audio_timeout",
    "internal",
}
CONNECT_TRIGGERS = {"org_concurrency_limit", "resume_token_invalid"}
EN_PHRASES = [
    ("Good morning,", "Good morning, everyone."),
    ("Let's start", "Let's start the meeting."),
    ("This is", "This is a test translation."),
]
RU_PHRASES = [
    ("Доброе утро", "Доброе утро всем."),
    ("Давайте начнём", "Давайте начнём встречу."),
    ("Это тестовый", "Это тестовый перевод."),
]
# Its debug log would print credentials from the handshake.
_WS_LOGGER = logging.getLogger("tests.translation_mock.websocket")
_WS_LOGGER.setLevel(logging.INFO)
STREAM_PATH = re.compile(r"^/v1/translation/sessions/(?P<id>[^/]+)/stream$")
SESSION_PATH = re.compile(r"^/v1/translation/sessions/(?P<id>[^/]+)(?P<end>/end)?$")


def _offered_protocols(headers: Any) -> list[str]:
    joined = ",".join(headers.get_all("Sec-WebSocket-Protocol"))
    return [protocol.strip() for protocol in joined.split(",") if protocol.strip()]


def error_body(code: str, message: str, param: Optional[str] = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": ERRORS[code][0],
        "code": code,
        "message": message,
        "doc_url": DOCS + code,
        "request_id": "req_" + secrets.token_hex(8),
    }
    if param is not None:
        error["param"] = param
    return {"error": error}


@dataclass
class Connection:
    """One socket, and everything the tests want to check about it afterwards."""

    credential: str
    ws: ServerConnection
    offered: list[str]
    # Frames as received: (sequence, sample offset, pcm).
    frames: list[tuple[int, int, bytes]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    frames_out: int = 0
    # Set once the old socket stops output, so this one may start.
    may_output: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    output: deque[tuple[float, int, bytes]] = field(default_factory=deque)
    output_ready: asyncio.Event = field(default_factory=asyncio.Event)
    moving: bool = False
    first_frame_seen: bool = False
    last_frame_at: float = 0.0


@dataclass
class Session:
    id: str
    target_language: str
    source_language: str
    test: bool
    created: int
    expires_at: int
    stream_url: str
    secrets: set[str] = field(default_factory=set)
    resume_tokens: dict[str, int] = field(default_factory=dict)
    status: str = "pending"
    ended_at: Optional[int] = None
    end_reason: Optional[str] = None
    frames_in: int = 0
    frames_out: int = 0
    next_input: int = 0
    finishing: Optional[str] = None
    connections: list[Connection] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # Transcript state: frames in the item in progress, and its number from 1.
    item_frames: int = 0
    item_number: int = 1

    def to_json(self, client_secret: Optional[str] = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "translation_session",
            "status": self.status,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "test": self.test,
            "created": self.created,
            "expires_at": self.expires_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "stream_url": self.stream_url,
            "client_secret": client_secret,
            "usage": {
                "input_audio_ms": self.frames_in * FRAME_MS,
                "output_audio_ms": self.frames_out * FRAME_MS,
            },
        }


class MockTranslationServer:
    """Timings default to the spec's. Tests shorten them, and the options below the
    timings make the server do the things a real one only does under a deploy."""

    def __init__(
        self,
        *,
        port: int = 0,
        ready_delay: float = 0.3,
        echo_delay: float = 1.5,
        reconnect_every: Optional[float] = 60.0,
        audio_gap: float = 5.0,
        trigger_delay: float = 2.0,
        # Ask for a reconnect once a connection has taken this many frames.
        reconnect_after_frames: Optional[int] = None,
        # Report next_sequence this many frames behind what the connection took.
        reconnect_rewind: int = 0,
        # Drop the old socket right after `reconnect` instead of draining it with 1012.
        drop_old_on_reconnect: bool = False,
        # Abort the connection without a word once it has taken this many frames.
        drop_after_frames: Optional[int] = None,
        # Refuse this many WebSocket upgrades with HTTP 503.
        refuse_opens: int = 0,
        # The same, only for upgrades that offer a resume token.
        refuse_resumes: int = 0,
        # Fail a session that holds more frames than this that haven't come back yet.
        max_held_frames: Optional[int] = None,
        # Frames in one transcript item. The spec says 100.
        frames_per_item: int = 100,
    ) -> None:
        self._port = port
        self.ready_delay = ready_delay
        self.echo_delay = echo_delay
        self.reconnect_every = reconnect_every
        self.audio_gap = audio_gap
        self.trigger_delay = trigger_delay
        self.reconnect_after_frames = reconnect_after_frames
        self.reconnect_rewind = reconnect_rewind
        self.drop_old_on_reconnect = drop_old_on_reconnect
        self.drop_after_frames = drop_after_frames
        self.refuse_opens = refuse_opens
        self.refused_opens = 0
        self.refuse_resumes = refuse_resumes
        self.refused_resumes: list[str] = []
        self.max_held_frames = max_held_frames
        self.frames_per_item = frames_per_item
        self.sessions: dict[str, Session] = {}
        self.http_requests: list[tuple[str, str, dict[str, str]]] = []
        self._idempotency: dict[str, tuple[str, bytes, str]] = {}
        self._http_server: Optional[asyncio.AbstractServer] = None
        self._ws_server: Any = None
        self.base_url = ""
        self._ws_base = ""

    async def __aenter__(self) -> MockTranslationServer:
        self._ws_server = await serve(
            self._stream,
            "127.0.0.1",
            0,
            subprotocols=["speko-translation"],
            process_request=self._process_upgrade,
            compression=None,
            logger=_WS_LOGGER,
        )
        ws_port = self._ws_server.sockets[0].getsockname()[1]
        self._ws_base = f"ws://127.0.0.1:{ws_port}"
        self._http_server = await asyncio.start_server(self._http, "127.0.0.1", self._port)
        http_port = self._http_server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{http_port}"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._http_server is not None
        self._http_server.close()
        await self._http_server.wait_closed()
        self._ws_server.close()
        await self._ws_server.wait_closed()

    def session(self) -> Session:
        """The only session, for tests that create one."""
        assert len(self.sessions) == 1
        return next(iter(self.sessions.values()))

    # --- HTTP -------------------------------------------------------------------

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.http_requests.append((method, path, headers))
            status, payload = await self._route(method, path, headers, body)
            data = json.dumps(payload).encode()
            writer.write(
                (
                    f"HTTP/1.1 {status} Mock\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n"
                    f"Request-Id: req_{secrets.token_hex(8)}\r\n"
                    f"Speko-Version: {API_VERSION}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()

    async def _route(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        if not headers.get("authorization", "").startswith("Bearer "):
            return 401, error_body("key_invalid", "Invalid API key.")
        if headers.get("speko-version") != API_VERSION:
            message = "Send Speko-Version: " + API_VERSION
            return 400, error_body("parameter_invalid", message, "Speko-Version")

        key = headers.get("idempotency-key")
        if method == "POST" and key is not None:
            seen = self._idempotency.get(key)
            if seen is not None:
                if seen[:2] != (path, body):
                    message = "This Idempotency-Key was used on a different path or body."
                    return 409, error_body("idempotency_key_reused", message, "Idempotency-Key")
                return self._replay(self.sessions[seen[2]], path)

        if method == "POST" and path == "/v1/translation/sessions":
            status, payload = self._create(body)
            if status == 200 and key is not None:
                self._idempotency[key] = (path, body, payload["id"])
            return status, payload
        match = SESSION_PATH.match(path)
        session = self.sessions.get(match["id"]) if match else None
        if session is None:
            return 404, error_body("resource_missing", "No such translation session.", "id")
        if method == "GET" and not match["end"]:
            return 200, session.to_json()
        if method == "POST" and match["end"]:
            status, payload = await self._end(session)
            if key is not None:
                self._idempotency[key] = (path, body, session.id)
            return status, payload
        return 404, error_body("resource_missing", "No such route.")

    def _replay(self, session: Session, path: str) -> tuple[int, dict[str, Any]]:
        if not path.endswith("/end") and session.status == "pending":
            fresh = self._new_secret(session)
            session.expires_at = int(time.time()) + 60
            return 200, session.to_json(fresh)
        return 200, session.to_json()

    def _create(self, body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            params = json.loads(body)
        except ValueError:
            return 400, error_body("parameter_invalid", "The body is not JSON.")
        unknown = set(params) - {"target_language", "source_language", "test"}
        if unknown:
            field_name = sorted(unknown)[0]
            return 400, error_body("parameter_invalid", f"Unknown field {field_name}.", field_name)
        target = params.get("target_language")
        if not isinstance(target, str):
            return 400, error_body("parameter_invalid", "Required.", "target_language")
        test = params.get("test", False)
        target_ok = target.lower() in ("en", "ru") or (
            test
            and target.lower().startswith("x-error-")
            and target[8:].lower() in LATE_TRIGGERS | CONNECT_TRIGGERS
        )
        if not target_ok:
            message = f"Translation into '{target}' is not supported."
            return 400, error_body("unsupported_language", message, "target_language")
        source = params.get("source_language", "auto")
        if source.lower() not in ("auto", "en", "ru"):
            message = f"Translation from '{source}' is not supported."
            return 400, error_body("unsupported_language", message, "source_language")
        session_id = "trs_" + secrets.token_urlsafe(15).replace("-", "a").replace("_", "b")
        created = int(time.time())
        session = Session(
            id=session_id,
            target_language=target.lower() if not target.lower().startswith("x-error-") else target,
            source_language=source.lower(),
            test=test,
            created=created,
            expires_at=created + 60,
            stream_url=f"{self._ws_base}/v1/translation/sessions/{session_id}/stream",
        )
        self.sessions[session_id] = session
        return 200, session.to_json(self._new_secret(session))

    def _new_secret(self, session: Session) -> str:
        secret = f"{session.id}_secret_{secrets.token_hex(12)}"
        session.secrets.add(secret)
        return secret

    async def _end(self, session: Session) -> tuple[int, dict[str, Any]]:
        if session.status == "pending":
            self._mark_ended(session, "ended_by_api")
        elif session.status == "active":
            await self._begin_finish(session, session.connections[-1], "ended_by_api")
            await asyncio.wait_for(session.done.wait(), 25)
        return 200, session.to_json()

    # --- Stream -----------------------------------------------------------------

    def _process_upgrade(self, connection: ServerConnection, request: Any) -> Optional[Response]:
        if self.refused_opens < self.refuse_opens:
            self.refused_opens += 1
            return Response(503, "Service Unavailable", Headers(), b"")
        offered = _offered_protocols(request.headers)
        credential = offered[1] if len(offered) > 1 else ""
        if "_resume_" in credential and len(self.refused_resumes) < self.refuse_resumes:
            self.refused_resumes.append(credential)
            return Response(503, "Service Unavailable", Headers(), b"")
        return None

    async def _stream(self, ws: ServerConnection) -> None:
        # websockets 13.0 runs this handler even after _process_upgrade refused.
        if ws.response.status_code != 101:
            return
        match = STREAM_PATH.match(ws.request.path)
        session = self.sessions.get(match["id"]) if match else None
        offered = _offered_protocols(ws.request.headers)
        credential = next((p for p in offered if p != "speko-translation"), "")
        conn = Connection(credential=credential, ws=ws, offered=offered)
        if session is None or not self._admit(session, conn):
            code = "resume_token_invalid" if "_resume_" in credential else "client_secret_invalid"
            message = "That credential is not valid."
            await self._send_json(ws, {"type": "error", **error_body(code, message)})
            await ws.close(4401)
            return

        target = session.target_language.lower()
        trigger = target[8:] if target.startswith("x-error-") else None
        if trigger == "org_concurrency_limit":
            await self._fail(session, conn, trigger)
            return
        if trigger == "resume_token_invalid":
            await self._send_json(ws, {"type": "error", **error_body(trigger, "Token lapsed.")})
            self._mark_ended(session, "resume_expired")
            await ws.close(4401)
            return

        tasks = [
            asyncio.create_task(self._writer(session, conn)),
            asyncio.create_task(self._heartbeat(session, conn)),
            asyncio.create_task(self._audio_gap_watch(session, conn)),
        ]
        if trigger in LATE_TRIGGERS:
            tasks.append(asyncio.create_task(self._fire_late_trigger(session, conn, trigger)))
        try:
            async for message in ws:
                if session.status == "ended":
                    continue
                if isinstance(message, str):
                    await self._on_control(session, conn, message)
                else:
                    await self._on_frame(session, conn, message)
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            conn.drained.set()
            self._after_socket_closed(session, conn)

    def _admit(self, session: Session, conn: Connection) -> bool:
        if session.status == "pending" and conn.credential in session.secrets:
            session.secrets.discard(conn.credential)
            session.status = "active"
            conn.may_output.set()
        elif session.status == "active" and conn.credential in session.resume_tokens:
            session.next_input = session.resume_tokens.pop(conn.credential)
        else:
            return False
        if session.connections:
            previous = session.connections[-1]
            asyncio.create_task(self._open_output_after(previous, conn))
        session.connections.append(conn)
        conn.last_frame_at = time.monotonic()
        return True

    async def _open_output_after(self, previous: Connection, conn: Connection) -> None:
        await previous.drained.wait()
        if previous.output:
            # "The old socket drops during a reconnect and its output is lost. This one
            # arrives on the new socket."
            lost = [item[1] for item in previous.output]
            previous.output.clear()
            await self._send_json(
                conn.ws,
                {"type": "discontinuity", "first_sequence": lost[0], "last_sequence": lost[-1]},
            )
        conn.may_output.set()

    def _after_socket_closed(self, session: Session, conn: Connection) -> None:
        # "After reconnect, the old socket closing or dropping doesn't end the session."
        if conn.moving or session.status != "active" or conn is not session.connections[-1]:
            return
        self._mark_ended(session, "disconnected")

    def _mark_ended(self, session: Session, reason: str) -> None:
        if session.status == "ended":
            return
        session.status = "ended"
        session.ended_at = int(time.time())
        session.end_reason = reason
        session.done.set()

    @staticmethod
    async def _send_json(ws: ServerConnection, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _fail(self, session: Session, conn: Connection, code: str) -> None:
        """An error that ends the session: `error`, `ended`, close 4000 + status."""
        ws = conn.ws
        await self._send_json(ws, {"type": "error", **error_body(code, f"Mock {code}.")})
        self._mark_ended(session, code)
        await self._send_ended(session, ws)
        await ws.close(4000 + ERRORS[code][1])

    async def _send_ended(self, session: Session, ws: ServerConnection) -> None:
        await self._send_json(
            ws,
            {
                "type": "ended",
                "reason": session.end_reason,
                "usage": session.to_json()["usage"],
            },
        )

    async def _on_frame(self, session: Session, conn: Connection, data: bytes) -> None:
        if conn.moving or session.finishing is not None:
            return  # "frames from next_sequence on are ignored here"
        if len(data) != FRAME_BYTES:
            await self._fail(session, conn, "frame_invalid")
            return
        sequence, offset = struct.unpack_from("<QQ", data)
        resumed = len(session.connections) > 1
        expected = session.next_input
        later_first_frame = resumed and not conn.first_frame_seen and sequence > expected
        in_order = sequence == expected or later_first_frame
        if offset != sequence * SAMPLES_PER_FRAME or not in_order:
            await self._fail(session, conn, "frame_invalid")
            return
        if later_first_frame:
            await self._send_json(
                conn.ws,
                {
                    "type": "discontinuity",
                    "first_sequence": expected,
                    "last_sequence": sequence - 1,
                },
            )
        conn.first_frame_seen = True
        conn.last_frame_at = time.monotonic()
        conn.frames.append((sequence, offset, data[16:]))
        session.next_input = sequence + 1
        session.frames_in += 1
        conn.output.append((time.monotonic() + self.echo_delay, sequence, data[16:]))
        conn.output_ready.set()
        held = sum(len(connection.output) for connection in session.connections)
        if self.max_held_frames is not None and held > self.max_held_frames:
            await self._fail(session, conn, "internal")
            return
        if self.drop_after_frames is not None and len(conn.frames) >= self.drop_after_frames:
            conn.ws.transport.abort()
            return
        if (
            self.reconnect_after_frames is not None
            and len(conn.frames) >= self.reconnect_after_frames
        ):
            await self._send_reconnect(session, conn)

    async def _on_control(self, session: Session, conn: Connection, message: str) -> None:
        if conn.moving:
            return  # "Controls reaching the old socket after reconnect are ignored"
        try:
            control = json.loads(message)
            kind = control["type"]
        except (ValueError, KeyError, TypeError):
            await self._fail(session, conn, "message_invalid")
            return
        conn.controls.append(control)
        ping_id = control.get("id")
        if kind == "finish":
            await self._begin_finish(session, conn, "finished")
        elif kind == "cancel":
            conn.output.clear()
            self._mark_ended(session, "canceled")
            await self._send_ended(session, conn.ws)
            await conn.ws.close(1000)
        elif kind == "ping" and (ping_id is None or len(ping_id) <= 64):
            pong = {"type": "pong"} if ping_id is None else {"type": "pong", "id": ping_id}
            await self._send_json(conn.ws, pong)
        else:
            await self._fail(session, conn, "message_invalid")

    async def _begin_finish(self, session: Session, conn: Connection, reason: str) -> None:
        if session.finishing is None:
            session.finishing = reason
            conn.output_ready.set()

    async def _send_reconnect(self, session: Session, conn: Connection) -> None:
        if conn.moving or session.finishing is not None:
            return
        conn.moving = True
        next_sequence = max(session.next_input - self.reconnect_rewind, 0)
        token = f"{session.id}_resume_{secrets.token_hex(12)}"
        session.resume_tokens[token] = next_sequence
        session.next_input = next_sequence
        # This connection "didn't take" the frames from next_sequence on.
        conn.output = deque(item for item in conn.output if item[1] < next_sequence)
        await self._send_json(
            conn.ws, {"type": "reconnect", "resume_token": token, "next_sequence": next_sequence}
        )
        conn.output_ready.set()
        if self.drop_old_on_reconnect:
            conn.ws.transport.abort()

    async def _writer(self, session: Session, conn: Connection) -> None:
        """The output side: delayed echo, transcripts, then the way the socket ends."""
        ws = conn.ws
        await conn.may_output.wait()
        while True:
            if conn.output:
                due, _, pcm = conn.output[0]
                now = time.monotonic()
                if due > now:
                    await asyncio.sleep(due - now)
                    continue
                conn.output.popleft()
                await self._send_output(session, conn, pcm)
            elif conn.moving:
                await ws.close(1012)
                conn.drained.set()
                return
            elif session.finishing is not None:
                await self._finish_transcript(session, ws)
                self._mark_ended(session, session.finishing)
                await self._send_ended(session, ws)
                await ws.close(1000)
                return
            else:
                conn.output_ready.clear()
                await conn.output_ready.wait()

    async def _send_output(self, session: Session, conn: Connection, pcm: bytes) -> None:
        sequence = conn.frames_out
        await conn.ws.send(struct.pack("<QQ", sequence, sequence * SAMPLES_PER_FRAME) + pcm)
        conn.frames_out += 1
        session.frames_out += 1
        session.item_frames += 1
        if session.item_frames == self.frames_per_item // 2:
            await self._send_transcript(session, conn.ws, partial=True, final=False)
        elif session.item_frames == self.frames_per_item:
            await self._send_transcript(session, conn.ws, partial=False, final=False)
            await self._send_transcript(session, conn.ws, partial=False, final=True)
            session.item_frames = 0
            session.item_number += 1

    async def _finish_transcript(self, session: Session, ws: ServerConnection) -> None:
        # "An item cut short by finish still gets its full text and final: true."
        if session.item_frames:
            await self._send_transcript(session, ws, partial=False, final=True)
            session.item_frames = 0

    async def _send_transcript(
        self, session: Session, ws: ServerConnection, *, partial: bool, final: bool
    ) -> None:
        source, translated = self._phrases(session, partial=partial)
        item_id = f"item_test_{session.item_number}"
        sides = [("transcript.translated", translated)]
        if session.item_number % SOURCELESS_EVERY != 0:
            sides.insert(0, ("transcript.source", source))
        for kind, text in sides:
            await self._send_json(
                ws, {"type": kind, "item_id": item_id, "text": text, "final": final}
            )

    @staticmethod
    def _phrases(session: Session, *, partial: bool) -> tuple[str, str]:
        n = session.item_number
        target = session.target_language
        if target.lower().startswith("x-error-"):
            text = f"Test phrase {n}" if partial else f"Test phrase {n}."
            return text, f"[{target}] {text}"
        index = (n - 1) % 3
        side = 0 if partial else 1
        english, russian = EN_PHRASES[index][side], RU_PHRASES[index][side]
        return (english, russian) if target == "ru" else (russian, english)

    async def _heartbeat(self, session: Session, conn: Connection) -> None:
        """`ready`, then `lag` every second and `reconnect` on the schedule."""
        await asyncio.sleep(self.ready_delay)
        await self._send_json(conn.ws, {"type": "ready"})
        ready_at = time.monotonic()
        while session.status == "active":
            await asyncio.sleep(min(1.0, self.reconnect_every or 1.0))
            if conn.moving:
                return
            if session.status != "active":
                return
            await self._send_json(
                conn.ws, {"type": "lag", "input_buffered_ms": 0, "output_buffered_ms": 0}
            )
            if (
                self.reconnect_every is not None
                and time.monotonic() - ready_at >= self.reconnect_every
            ):
                await self._send_reconnect(session, conn)

    async def _audio_gap_watch(self, session: Session, conn: Connection) -> None:
        while True:
            await asyncio.sleep(0.05)
            if session.finishing is not None or conn.moving or session.status != "active":
                return
            if time.monotonic() - conn.last_frame_at > self.audio_gap:
                await self._fail(session, conn, "audio_timeout")
                return

    async def _fire_late_trigger(self, session: Session, conn: Connection, code: str) -> None:
        await asyncio.sleep(self.ready_delay + self.trigger_delay)
        if session.status == "active":
            conn.output.clear()
            await self._fail(session, conn, code)


async def _serve_forever(args: argparse.Namespace) -> None:
    async with MockTranslationServer(
        port=args.port,
        echo_delay=args.echo_delay,
        reconnect_every=args.reconnect_every,
        max_held_frames=TEST_MODE_HELD_FRAMES,
    ) as server:
        print(f"Mock Translation API on {server.base_url}", flush=True)
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--echo-delay", type=float, default=1.5)
    parser.add_argument("--reconnect-every", type=float, default=60.0)
    try:
        asyncio.run(_serve_forever(parser.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
