import httpx
import pytest
import respx

from spekoai import (
    SessionStreamCallEvent,
    SessionStreamEnd,
    SessionStreamStatus,
    SessionStreamTranscript,
    SpekoApiError,
)
from tests.conftest import BASE, sse


def _turn(index: int, text: str) -> dict:
    return {
        "id": f"t_{index}",
        "index": index,
        "source": "agent",
        "text": text,
        "startedAt": "2026-07-24T10:00:00.000Z",
        "endedAt": None,
        "provider": None,
        "model": None,
        "eouMs": None,
        "llmTtftMs": None,
        "ttsTtfbMs": None,
        "latencyStatus": None,
        "conversationalLatencyMs": None,
        "toolCalls": [],
    }


def _event(event_id: str, created_at: str) -> dict:
    return {
        "id": event_id,
        "session_id": "sess_1",
        "organization_id": "org_1",
        "provider": "livekit",
        "event_type": "participant_joined",
        "status": None,
        "failure_cause": None,
        "sip_status_code": None,
        "sip_status": None,
        "occurred_at": created_at,
        "payload": {},
        "created_at": created_at,
    }


FIRST_CONNECTION = sse(
    ("status", {"status": "active", "endedAt": None}),
    ("transcript", _turn(0, "Hello!")),
    ("event", _event("ev_1", "2026-07-24T10:00:01.000Z")),
    ("end", {"reason": "timeout"}),  # server stream rotation → reconnect
)

SECOND_CONNECTION = sse(
    ("event", _event("ev_1", "2026-07-24T10:00:01.000Z")),  # overlap replay
    ("event", _event("ev_2", "2026-07-24T10:00:02.000Z")),
    ("end", {"reason": "session_ended"}),
)


@respx.mock
def test_stream_reconnects_dedupes_and_advances_cursor(speko):
    route = respx.get(f"{BASE}/v1/sessions/sess_1/stream")
    route.side_effect = [
        httpx.Response(
            200, text=FIRST_CONNECTION, headers={"content-type": "text/event-stream"}
        ),
        httpx.Response(
            200, text=SECOND_CONNECTION, headers={"content-type": "text/event-stream"}
        ),
    ]

    events = list(speko.sessions.stream("sess_1"))

    types = [type(e) for e in events]
    assert types == [
        SessionStreamStatus,
        SessionStreamTranscript,
        SessionStreamCallEvent,
        SessionStreamCallEvent,
        SessionStreamEnd,
    ]
    # ev_1 must be yielded exactly once despite the overlap replay.
    ids = [e.event.id for e in events if isinstance(e, SessionStreamCallEvent)]
    assert ids == ["ev_1", "ev_2"]

    first_cursor = dict(route.calls[0].request.url.params)["cursor"]
    second_cursor = dict(route.calls[1].request.url.params)["cursor"]
    assert first_cursor == "-1:0"
    # Cursor advanced to turn 0 and ev_1's created_at (ms since epoch).
    assert second_cursor.startswith("0:")
    assert second_cursor != "0:0"


@respx.mock
def test_stream_raises_on_4xx(speko):
    respx.get(f"{BASE}/v1/sessions/nope/stream").respond(
        status_code=404, json={"error": "not found", "code": "SESSION_NOT_FOUND"}
    )
    with pytest.raises(SpekoApiError) as exc:
        list(speko.sessions.stream("nope"))
    assert exc.value.status == 404


@respx.mock
async def test_async_stream_mirror(aspeko):
    route = respx.get(f"{BASE}/v1/sessions/sess_1/stream")
    route.side_effect = [
        httpx.Response(
            200, text=FIRST_CONNECTION, headers={"content-type": "text/event-stream"}
        ),
        httpx.Response(
            200, text=SECOND_CONNECTION, headers={"content-type": "text/event-stream"}
        ),
    ]
    events = []
    async for event in aspeko.sessions.stream("sess_1"):
        events.append(event)
    assert isinstance(events[-1], SessionStreamEnd)
    ids = [e.event.id for e in events if isinstance(e, SessionStreamCallEvent)]
    assert ids == ["ev_1", "ev_2"]


@respx.mock
def test_stream_resumes_from_supplied_cursor(speko):
    route = respx.get(f"{BASE}/v1/sessions/sess_1/stream").respond(
        text=sse(("end", {"reason": "session_ended"})),
        headers={"content-type": "text/event-stream"},
    )
    list(speko.sessions.stream("sess_1", cursor="5:1753351201000"))
    assert dict(route.calls[0].request.url.params)["cursor"] == "5:1753351201000"
