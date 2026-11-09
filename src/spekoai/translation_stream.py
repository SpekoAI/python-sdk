"""Async helper for the Translation API stream.

``AsyncTranslationStream`` does what the stream's contract asks of a client:

- cuts your PCM into numbered 976-byte frames, resampling to 24 kHz if needed;
- sends in real time, and sends silence while you have nothing to send;
- moves to a new connection when Speko asks, resending from ``next_sequence``.

One task writes to the socket and one reads each open socket, so reconnect and
silence keep working however slowly your code reads events.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import logging
from collections import deque
from collections.abc import Coroutine
from typing import Any, Optional
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from spekoai._translation_audio import (
    SILENCE,
    STREAM_SAMPLE_RATE,
    PcmFramer,
    decode_frame,
    encode_frame,
)
from spekoai._user_agent import USER_AGENT
from spekoai.errors import SpekoTranslationConnectionError
from spekoai.models import (
    TranslationAudio,
    TranslationDiscontinuity,
    TranslationEnded,
    TranslationErrorEvent,
    TranslationLag,
    TranslationPong,
    TranslationReady,
    TranslationReconnect,
    TranslationStreamEvent,
    TranslationTranscriptSource,
    TranslationTranscriptTranslated,
)

_SUBPROTOCOL = "speko-translation"
_FRAME_SECONDS = 0.02
# A late chunk can catch up within this time. After it, silence fills the gap.
_SILENCE_GRACE_SECONDS = 0.1
# "Keep the last second of sent frames" for a reconnect.
_RESEND_FRAMES = 50
_RESUME_SECONDS = 30.0
_OPEN_TIMEOUT_SECONDS = 10.0
_CLOSE_TIMEOUT_SECONDS = 2.0
_CANCEL_TIMEOUT_SECONDS = 1.0
_RETRY_FIRST_DELAY_SECONDS = 0.25
_RETRY_MAX_DELAY_SECONDS = 2.0
# send() yields to the event loop this often, so that a whole file doesn't stall the
# sending of frames while it is being cut up.
_SEND_SLICE_BYTES = 9600
_MAX_PING_ID_LENGTH = 64
# How much unread event data may wait in memory: about 5 minutes of audio. Each event
# counts as its size on the wire plus this overhead, so small ones can't pile up either.
_MAX_BUFFERED_BYTES = 32 * 1024 * 1024
_EVENT_OVERHEAD_BYTES = 1024
# Speko caps finishing at 25 seconds. The rest is room for the network.
_END_TIMEOUT_SECONDS = 30.0
# The contract allows one socket plus the old one while it drains, for up to 10 seconds.
# Moves in quick succession can hold a few more, but not this many.
_MAX_OPEN_SOCKETS = 8

# The websockets debug log prints handshake headers, and the credential is one of
# them. This logger never enables debug, whatever the application's root level is.
_WS_LOGGER = logging.getLogger("spekoai.translation.websocket")
_WS_LOGGER.setLevel(logging.INFO)

_END_OF_STREAM = object()

_EVENT_MODELS: dict[str, Any] = {
    "ready": TranslationReady,
    "transcript.source": TranslationTranscriptSource,
    "transcript.translated": TranslationTranscriptTranslated,
    "lag": TranslationLag,
    "discontinuity": TranslationDiscontinuity,
    "reconnect": TranslationReconnect,
    "pong": TranslationPong,
    "error": TranslationErrorEvent,
    "ended": TranslationEnded,
}


class _NoRedirectConnect(ws_connect):
    """The contract has no redirects, and following one would offer the credential to
    whatever host is in ``Location``."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def is_loopback(host: Optional[str]) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _check_stream_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "wss" and not (parts.scheme == "ws" and is_loopback(parts.hostname)):
        raise ValueError("stream_url must use wss://. ws:// is allowed only for a loopback host.")


def _parse_event(message: str) -> Optional[TranslationStreamEvent]:
    """The event in a text message, or ``None`` for a type this SDK doesn't know."""
    data = json.loads(message)
    if not isinstance(data, dict):
        raise ValueError("A stream event is a JSON object")
    model = _EVENT_MODELS.get(data.get("type"))
    return None if model is None else model.model_validate(data)


def _control(control_type: str) -> str:
    return json.dumps({"type": control_type})


class AsyncTranslationStream:
    """One translation session's stream. Use it as a context manager::

        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(pcm_bytes)
            await stream.finish()
            async for event in stream:
                if event.type == "audio":
                    play(event.pcm)
                elif event.type == "transcript.translated":
                    show(event.item_id, event.text)

    Iterate until the stream stops. The last event is ``ended``, or ``error`` when
    Speko refused the credential. A lost connection raises
    ``SpekoTranslationConnectionError``: the session has ended, so create a new one.
    So does no ``ended`` within 30 seconds of ``finish`` going out (code
    ``end_timeout``), and the session is canceled.

    Events wait in memory until you read them, and this helper always reads the socket,
    so Speko's own ``client_too_slow`` limit never fires for an app that stops iterating.
    About 5 minutes of audio (32 MiB) can wait. Past that the session is canceled, and
    after the events already waiting you get ``SpekoTranslationConnectionError`` with
    code ``client_too_slow``.

    Leaving the block ends a session that is still running, with ``cancel``. To get
    the rest of the translation, call ``finish()`` and iterate until ``ended``. During
    a move this waits for the new connection, up to 30 seconds, because Speko ignores
    ``cancel`` on the old one.

    An app with no API key can create this directly, with the ``client_secret`` and
    ``stream_url`` your server passes it.
    """

    def __init__(
        self,
        client_secret: str,
        *,
        stream_url: str,
        sample_rate: int = STREAM_SAMPLE_RATE,
        connect_timeout: float = 60.0,
    ) -> None:
        _check_stream_url(stream_url)
        self._client_secret = client_secret
        self._stream_url = stream_url
        self._connect_timeout = connect_timeout
        self._framer = PcmFramer(sample_rate)
        self._ws: Optional[ClientConnection] = None
        self._sockets: set[ClientConnection] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._entered = False
        self._closed = False
        # True once the stream is over: no more events will be queued after the end.
        self._done = False
        self._moving = False
        self._move_task: Optional[asyncio.Task[None]] = None
        self._error_seen = False
        # True once Speko has ended the session or its socket is gone: nothing to cancel.
        self._session_over = False
        self._stopping: Optional[asyncio.Task[None]] = None
        self._buffered_bytes = 0
        # Audio waiting for its turn, as 960-byte PCM. It gets numbered when sent.
        self._pending: deque[bytes] = deque()
        # Frames to send first on a new connection, as (sequence, frame).
        self._replay: deque[tuple[int, bytes]] = deque()
        self._sent: deque[tuple[int, bytes]] = deque(maxlen=_RESEND_FRAMES)
        self._controls: deque[str] = deque()
        self._sequence = 0
        # "finish" or "cancel" once asked for, and whether this socket has been told.
        self._closing: Optional[str] = None
        self._closing_sent = False
        self._end_deadline = 0.0

    async def __aenter__(self) -> AsyncTranslationStream:
        # Created here so they belong to the running loop on every Python version.
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._connected = asyncio.Event()
        self._ws = await self._open(self._client_secret, self._connect_timeout, "connect_failed")
        self._entered = True
        self._spawn(self._read(self._ws))
        self._connected.set()
        self._spawn(self._pump())
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncTranslationStream:
        return self

    async def __anext__(self) -> TranslationStreamEvent:
        self._check_entered()
        size, item = await self._events.get()
        self._buffered_bytes -= size
        if isinstance(item, SpekoTranslationConnectionError):
            raise item
        if item is _END_OF_STREAM:
            # Stay at the end for anyone who iterates again.
            self._events.put_nowait((0, _END_OF_STREAM))
            raise StopAsyncIteration
        return item

    async def send(self, pcm: bytes) -> None:
        """Queue 16-bit little-endian mono PCM at this stream's sample rate.

        Chunks can be any size. This returns once the audio is queued, not once it
        has gone out: frames leave at 50 per second, so a whole file can be passed
        in one call. Raises ``SpekoTranslationConnectionError`` with code
        ``stream_closed`` once the stream takes no more audio.
        """
        self._check_entered()
        if self._done or self._closing is not None:
            raise SpekoTranslationConnectionError(
                "This stream takes no more audio.", "stream_closed"
            )
        view = memoryview(pcm).cast("B")
        for start in range(0, len(view), _SEND_SLICE_BYTES):
            self._pending.extend(self._framer.push(view[start : start + _SEND_SLICE_BYTES]))
            self._wake.set()
            await asyncio.sleep(0)

    async def finish(self) -> None:
        """Stop input and finish, once the audio already queued has gone out. Speko
        sends the rest of the translation and then ``ended``. Keep iterating."""
        self._check_entered()
        if self._done or self._closing is not None:
            return
        self._pending.extend(self._framer.flush())
        self._closing = "finish"
        self._wake.set()

    async def cancel(self) -> None:
        """End now. Queued audio and output not yet sent are dropped."""
        self._check_entered()
        if self._done or self._closing == "cancel":
            return
        self._closing = "cancel"
        self._closing_sent = False
        self._pending.clear()
        self._replay.clear()
        self._wake.set()

    async def ping(self, id: Optional[str] = None) -> None:
        """Ask for a ``pong`` event, which echoes ``id`` (up to 64 characters)."""
        self._check_entered()
        if id is not None and len(id) > _MAX_PING_ID_LENGTH:
            raise ValueError(f"A ping id is at most {_MAX_PING_ID_LENGTH} characters")
        if self._done:
            raise SpekoTranslationConnectionError("This stream has ended.", "stream_closed")
        control: dict[str, str] = {"type": "ping"}
        if id is not None:
            control["id"] = id
        self._controls.append(json.dumps(control))
        self._wake.set()

    async def close(self) -> None:
        """Release the sockets, ending the session with ``cancel`` if it is running."""
        if self._closed or not self._entered:
            return
        self._closed = True
        self._end_stream()
        try:
            await asyncio.wait({self._stop_session()})
        finally:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(ws.close() for ws in self._sockets), return_exceptions=True)
            self._sockets.clear()

    def _check_entered(self) -> None:
        if not self._entered:
            raise RuntimeError("Use the stream inside 'async with' before calling this.")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        # Deliberately not chained or quoted: the exception can hold a credential.
        self._fail(
            SpekoTranslationConnectionError(
                "The translation stream failed on a message it could not handle.",
                "protocol_error",
            )
        )

    async def _open(self, credential: str, budget: float, failure_code: str) -> ClientConnection:
        """Open a socket. A credential is spent only when the socket opens, so a failed
        open is retried with the same one until the budget runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        delay = _RETRY_FIRST_DELAY_SECONDS
        while True:
            remaining = deadline - loop.time()
            try:
                ws = await _NoRedirectConnect(
                    self._stream_url,
                    subprotocols=[_SUBPROTOCOL, credential],
                    open_timeout=max(min(remaining, _OPEN_TIMEOUT_SECONDS), 0.05),
                    close_timeout=_CLOSE_TIMEOUT_SECONDS,
                    compression=None,
                    user_agent_header=USER_AGENT,
                    logger=_WS_LOGGER,
                )
            except InvalidURI:
                raise
            except (OSError, asyncio.TimeoutError, WebSocketException) as error:
                if loop.time() + delay >= deadline:
                    raise SpekoTranslationConnectionError(
                        f"Could not open the translation stream: {type(error).__name__}.",
                        failure_code,
                    ) from None
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY_SECONDS)
            else:
                self._sockets.add(ws)
                return ws

    # --- Writing -----------------------------------------------------------------

    async def _pump(self) -> None:
        """The only task that writes while the stream runs. A frame leaves every 20 ms: queued
        audio if there is any, otherwise silence. Sequence numbers are given out here,
        so silence and audio share one numbering."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            await self._connected.wait()
            if self._done:
                return
            ws = self._ws
            assert ws is not None
            try:
                if self._replay:
                    await self._send_frame(ws, self._replay.popleft())
                elif self._controls:
                    await ws.send(self._controls.popleft())
                elif self._closing == "cancel" and not self._closing_sent:
                    await self._send_closing(ws)
                elif self._pending:
                    now = loop.time()
                    if now < next_tick:
                        await asyncio.sleep(next_tick - now)
                    else:
                        next_tick += _FRAME_SECONDS
                        await self._send_new_frame(ws, self._pending.popleft())
                elif self._closing is not None:
                    if not self._closing_sent:
                        await self._send_closing(ws)
                    elif loop.time() < self._end_deadline:
                        await self._idle(self._end_deadline - loop.time())
                    else:
                        self._fail(
                            SpekoTranslationConnectionError(
                                "Speko did not end the stream in time.", "end_timeout"
                            )
                        )
                else:
                    now = loop.time()
                    behind = now - next_tick
                    if behind < _SILENCE_GRACE_SECONDS:
                        await self._idle(_SILENCE_GRACE_SECONDS - behind)
                    else:
                        # Past the grace period the clock moves up, so that silence is
                        # never sent in a burst to catch up.
                        next_tick = now - _SILENCE_GRACE_SECONDS + _FRAME_SECONDS
                        await self._send_new_frame(ws, SILENCE)
            except ConnectionClosed:
                # The reader decides what this means: the end of the stream or a move.
                await self._idle(None)

    async def _idle(self, timeout: Optional[float]) -> None:
        """Wait for something new to send. Nothing can change between the pump's checks
        and the ``clear`` here, because no await separates them."""
        if not self._connected.is_set():
            return  # A move began during the pump's last send. Its wake-up was spent.
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _send_closing(self, ws: ClientConnection) -> None:
        assert self._closing is not None
        self._closing_sent = True
        self._end_deadline = asyncio.get_running_loop().time() + _END_TIMEOUT_SECONDS
        await ws.send(_control(self._closing))

    async def _send_new_frame(self, ws: ClientConnection, pcm: bytes) -> None:
        sequence = self._sequence
        self._sequence += 1
        await self._send_frame(ws, (sequence, encode_frame(sequence, pcm)))

    async def _send_frame(self, ws: ClientConnection, numbered: tuple[int, bytes]) -> None:
        # Remembered before the send: a reconnect read while this send is suspended
        # has to resend this frame too.
        self._sent.append(numbered)
        await ws.send(numbered[1])

    # --- Reading -----------------------------------------------------------------

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for message in ws:
                if self._done:
                    return
                self._on_message(ws, message)
        except ConnectionClosed:
            pass
        self._on_socket_closed(ws)

    def _on_message(self, ws: ClientConnection, message: Any) -> None:
        size = len(message) + _EVENT_OVERHEAD_BYTES
        if self._buffered_bytes + size > _MAX_BUFFERED_BYTES:
            self._fail(
                SpekoTranslationConnectionError(
                    "Events went unread for too long, so the session was canceled. "
                    "Keep iterating, and create a new session to continue.",
                    "client_too_slow",
                )
            )
            return
        if not isinstance(message, str):
            sequence, sample_offset, pcm = decode_frame(message)
            event: Any = TranslationAudio(pcm=pcm, sequence=sequence, sample_offset=sample_offset)
        else:
            event = _parse_event(message)
        if event is None:
            return
        if isinstance(event, TranslationReconnect) and (self._moving or ws is not self._ws):
            return  # One move at a time, and only the socket in use can ask for one.
        self._buffered_bytes += size
        self._events.put_nowait((size, event))
        if isinstance(event, TranslationReconnect):
            self._begin_move(event)
        elif isinstance(event, TranslationErrorEvent):
            self._error_seen = True
        elif isinstance(event, TranslationEnded):
            self._session_over = True
            self._end_stream()

    def _on_socket_closed(self, ws: ClientConnection) -> None:
        self._sockets.discard(ws)
        # After `reconnect`, the old socket closing or dropping doesn't end the session.
        if self._moving or ws is not self._ws:
            return
        self._session_over = True
        if self._error_seen:
            # Close 4401 without `ended`: the error event already said why.
            self._end_stream()
            return
        self._end_stream(
            SpekoTranslationConnectionError(
                "The translation connection was lost, so the session has ended. "
                "Create a new session to continue.",
                "connection_lost",
                close_code=ws.protocol.close_code,
            )
        )

    def _end_stream(self, error: Optional[SpekoTranslationConnectionError] = None) -> None:
        if self._done:
            return
        self._done = True
        if error is not None:
            self._events.put_nowait((0, error))
        self._events.put_nowait((0, _END_OF_STREAM))

    def _fail(self, error: SpekoTranslationConnectionError) -> None:
        """End the stream from this side. The session may still be running on the server,
        so tell it to stop now rather than when the app leaves the block."""
        self._end_stream(error)
        self._stop_session()

    def _stop_session(self) -> asyncio.Task[None]:
        if self._stopping is None:
            self._stopping = self._spawn(self._cancel_on_server())
        return self._stopping

    async def _cancel_on_server(self) -> None:
        # Speko ignores controls on the old socket, so a move has to finish first. Its
        # budget bounds this wait.
        if self._move_task is not None:
            await asyncio.wait({self._move_task})
        if self._session_over or self._moving:
            return
        assert self._ws is not None
        try:
            await asyncio.wait_for(self._ws.send(_control("cancel")), _CANCEL_TIMEOUT_SECONDS)
        except (ConnectionClosed, OSError, asyncio.TimeoutError):
            pass

    # --- Moving to a new connection ----------------------------------------------

    def _begin_move(self, event: TranslationReconnect) -> None:
        if len(self._sockets) >= _MAX_OPEN_SOCKETS:
            self._fail(
                SpekoTranslationConnectionError(
                    "The translation server asked for too many connections.", "protocol_error"
                )
            )
            return
        self._moving = True
        self._connected.clear()
        # Everything from next_sequence on goes out again, including what the old socket
        # ignored. If frame next_sequence is gone, this starts from the oldest one still
        # held, and Speko reports the skipped frames with `discontinuity`.
        held = itertools.chain(self._sent, self._replay)
        self._replay = deque(frame for frame in held if frame[0] >= event.next_sequence)
        self._sent.clear()
        # Speko ignores controls that reach the old socket, so this one goes out again,
        # after the frames above.
        self._closing_sent = False
        self._move_task = self._spawn(self._move(event.resume_token))
        self._wake.set()

    async def _move(self, resume_token: str) -> None:
        try:
            ws = await self._open(resume_token, _RESUME_SECONDS, "resume_failed")
        except SpekoTranslationConnectionError as error:
            self._session_over = True
            self._end_stream(error)
            return
        self._ws = ws
        self._moving = False
        self._spawn(self._read(ws))
        self._connected.set()
