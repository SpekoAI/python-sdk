"""Session-scoped reads. A ``session_id`` is the id returned by
``voice.dial(...)`` / session create — the same id ``calls.*`` accepts."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Any, Optional

import httpx

from spekoai._http import (
    aiter_sse,
    araise_for_status_streamed,
    iter_sse,
    path_id,
    raise_for_status,
    raise_for_status_streamed,
)
from spekoai.errors import SpekoApiError
from spekoai.models import (
    CallEvent,
    SessionStreamCallEvent,
    SessionStreamEnd,
    SessionStreamEvent,
    SessionStreamStatus,
    SessionStreamTranscript,
    SessionTranscript,
    SessionTranscriptEntry,
)

# Consecutive failed connect attempts (no frame received) before giving up.
MAX_RECONNECT_ATTEMPTS = 10
# Per-connection cap, above the server's own stream rotation (10 min +
# `end {reason:'timeout'}`) so the server always rotates first and this
# client-side timeout only nets a server that went quiet.
STREAM_REQUEST_TIMEOUT_SECONDS = 15 * 60.0

# Every reconnect re-reads the server's overlap window (its own dedupe state
# dies with the response), so the iterator remembers delivered event ids to
# keep replays away from the consumer. Retention is by the EVENT's timestamp
# falling behind the cursor window — the same invariant the server uses —
# never by count: a capacity ring can evict ids that a reconnect is still
# allowed to replay, reintroducing duplicates. The horizon just needs to
# comfortably exceed the server's 2 s overlap re-read.
_SEEN_EVENT_WINDOW_MS = 10_000

_CURSOR_RE = re.compile(r"^(-?\d+):(\d+)$")


def _created_at_ms(iso: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class _StreamState:
    """Cursor + dedupe bookkeeping shared by the sync/async stream loops."""

    def __init__(self, cursor: Optional[str]) -> None:
        # Cursor is derived from payloads (max turn index / max event
        # created_at), so resume needs no SSE `id:` plumbing.
        self.turn_index = -1
        self.event_ms = 0
        self.failed_attempts = 0
        self._seen_events: dict[str, int] = {}
        if cursor:
            match = _CURSOR_RE.match(cursor.strip())
            if match:
                self.turn_index = int(match.group(1))
                self.event_ms = int(match.group(2))

    @property
    def cursor(self) -> str:
        return f"{self.turn_index}:{self.event_ms}"

    def _remember_event(self, event_id: str, created_ms: Optional[int]) -> None:
        self._seen_events[event_id] = created_ms if created_ms is not None else self.event_ms
        horizon = self.event_ms - _SEEN_EVENT_WINDOW_MS
        for seen_id, seen_ms in list(self._seen_events.items()):
            if seen_ms < horizon:
                del self._seen_events[seen_id]

    def handle_frame(
        self, event: str, data: Any
    ) -> tuple[Optional[SessionStreamEvent], bool]:
        """Advance the cursor for one SSE frame. Returns
        ``(event_to_yield_or_None, stream_ended)``."""
        if event == "status" and isinstance(data, dict):
            status = SessionStreamStatus(
                status=str(data.get("status", "")), ended_at=data.get("endedAt")
            )
            return status, False
        if event == "transcript" and isinstance(data, dict):
            turn = SessionTranscriptEntry.model_validate(data)
            self.turn_index = max(self.turn_index, turn.index)
            return SessionStreamTranscript(turn=turn), False
        if event == "event" and isinstance(data, dict):
            call_event = CallEvent.model_validate(data)
            created_ms = _created_at_ms(call_event.created_at)
            if created_ms is not None:
                self.event_ms = max(self.event_ms, created_ms)
            if call_event.id in self._seen_events:
                return None, False
            self._remember_event(call_event.id, created_ms)
            return SessionStreamCallEvent(event=call_event), False
        if event == "end" and isinstance(data, dict):
            if data.get("reason") == "session_ended":
                return SessionStreamEnd(), True
            # 'timeout' is the server rotating a long response — reconnect.
            return None, False
        # 'error' or unknown frame: the server closes after these; the loop
        # falls through to the reconnect path.
        return None, False

    def note_disconnect(self, received_frame: bool) -> float:
        """Track a dropped connection; returns the backoff delay in seconds.
        Raises once too many consecutive connects yield nothing."""
        if not received_frame:
            self.failed_attempts += 1
            if self.failed_attempts >= MAX_RECONNECT_ATTEMPTS:
                raise SpekoApiError(
                    f"session stream: {MAX_RECONNECT_ATTEMPTS} consecutive reconnect "
                    "attempts failed",
                    0,
                    "STREAM_DISCONNECTED",
                )
        return min(0.5 * max(self.failed_attempts, 1), 5.0)


class SessionsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def transcript(self, session_id: str) -> SessionTranscript:
        """Transcript of a session, oldest turn first — the point-in-time
        snapshot. For live consumption prefer ``stream()``, which pushes each
        turn as it lands; this endpoint stays the final word on per-turn
        latency numbers (legs keep enriching after a turn is first
        written)."""
        resp = self._client.get(f"/v1/sessions/{path_id(session_id)}/transcript")
        raise_for_status(resp)
        return SessionTranscript.model_validate(resp.json())

    def stream(
        self, session_id: str, *, cursor: Optional[str] = None
    ) -> Iterator[SessionStreamEvent]:
        """Observe a session live: transcript turns, call events, and status
        changes pushed as they happen (SSE under the hood). Iterate until the
        ``end`` event — the SDK reconnects through server stream rotations
        and transient drops with an internal cursor, so consumers never poll
        and never see duplicates.

        Example::

            for ev in speko.sessions.stream(session_id):
                if ev.type == "transcript":
                    print(ev.turn.text)
                elif ev.type == "end":
                    break

        A stream opened on an already-ended session replays the backlog and
        then ends. Stop early by breaking out of the loop (or calling
        ``.close()`` on the generator).

        ``cursor`` (``"<lastTurnIndex>:<lastEventCreatedAtMs>"``) is rarely
        needed — the iterator tracks it internally across reconnects; pass it
        only to resume a NEW iterator after your own process restarted. A
        brand-new resumed iterator starts with no dedupe memory and may
        replay up to ~2 s of events — dedupe by ``event.id`` if that matters.
        """
        state = _StreamState(cursor)
        path = f"/v1/sessions/{path_id(session_id)}/stream"
        while True:
            received_frame = False
            try:
                with self._client.stream(
                    "GET",
                    path,
                    params={"cursor": state.cursor},
                    headers={"Accept": "text/event-stream"},
                    timeout=STREAM_REQUEST_TIMEOUT_SECONDS,
                ) as resp:
                    raise_for_status_streamed(resp)
                    for event, data in iter_sse(resp.iter_text()):
                        received_frame = True
                        state.failed_attempts = 0
                        item, ended = state.handle_frame(event, data)
                        if item is not None:
                            yield item
                        if ended:
                            return
            except SpekoApiError as err:
                # Auth failures and unknown sessions are not transient.
                if 400 <= err.status < 500:
                    raise
            except httpx.HTTPError:
                # Network drop, mid-stream abort → reconnect below.
                pass
            time.sleep(state.note_disconnect(received_frame))


class AsyncSessionsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def transcript(self, session_id: str) -> SessionTranscript:
        """Point-in-time transcript snapshot (async)."""
        resp = await self._client.get(f"/v1/sessions/{path_id(session_id)}/transcript")
        raise_for_status(resp)
        return SessionTranscript.model_validate(resp.json())

    async def stream(
        self, session_id: str, *, cursor: Optional[str] = None
    ) -> AsyncIterator[SessionStreamEvent]:
        """Observe a session live (async). See ``SessionsResource.stream``."""
        state = _StreamState(cursor)
        path = f"/v1/sessions/{path_id(session_id)}/stream"
        while True:
            received_frame = False
            try:
                async with self._client.stream(
                    "GET",
                    path,
                    params={"cursor": state.cursor},
                    headers={"Accept": "text/event-stream"},
                    timeout=STREAM_REQUEST_TIMEOUT_SECONDS,
                ) as resp:
                    await araise_for_status_streamed(resp)
                    async for event, data in aiter_sse(resp.aiter_text()):
                        received_frame = True
                        state.failed_attempts = 0
                        item, ended = state.handle_frame(event, data)
                        if item is not None:
                            yield item
                        if ended:
                            return
            except SpekoApiError as err:
                if 400 <= err.status < 500:
                    raise
            except httpx.HTTPError:
                pass
            await asyncio.sleep(state.note_disconnect(received_frame))
