"""Checks the SDK against a real server in test mode. Skipped unless a URL is set:

    SPEKO_TRANSLATION_TEST_URL=https://... SPEKO_API_KEY=... \
        pytest tests/test_translation_integration.py -v

These sessions are `test: true`, so they are never charged. The whole file takes about
90 seconds, because one test stays connected past the 60-second reconnect. The tests
expect the timings and scripted text of the spec's "Test mode" section.
"""

import asyncio
import os
import struct

import pytest

from spekoai import (
    AsyncSpeko,
    SpekoTranslationInvalidRequestError,
)

SERVER_URL = os.environ.get("SPEKO_TRANSLATION_TEST_URL")

pytestmark = pytest.mark.skipif(
    not SERVER_URL, reason="SPEKO_TRANSLATION_TEST_URL is not set; no test-mode server to check"
)

TIMEOUT = 120.0


def tagged_frame(index: int) -> bytes:
    return struct.pack("<h", (index % 30000) + 1) * 480


def tagged_audio(count: int) -> bytes:
    return b"".join(tagged_frame(index) for index in range(count))


def of_type(events, kind: str) -> list:
    return [event for event in events if event.type == kind]


async def collect(stream) -> list:
    async def run():
        return [event async for event in stream]

    return await asyncio.wait_for(run(), TIMEOUT)


@pytest.fixture
async def client():
    speko = AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"], translation_base_url=SERVER_URL)
    yield speko
    await speko.close()


async def test_a_test_session_round_trip(client):
    session = await client.translation.sessions.create(target_language="ru", test=True)
    assert session.status == "pending"
    assert session.test is True
    assert session.client_secret
    assert session.source_language == "auto"

    retrieved = await client.translation.sessions.retrieve(session.id)
    assert retrieved.client_secret is None

    async with client.translation.connect(session.client_secret) as stream:
        await stream.send(tagged_audio(150))
        await stream.ping("integration")
        await stream.finish()
        events = await collect(stream)

    assert events[-1].type == "ended"
    assert events[-1].reason == "finished"
    assert of_type(events, "ready")
    assert of_type(events, "lag")
    assert of_type(events, "pong")[0].id == "integration"
    assert [event.pcm for event in of_type(events, "audio")] == [
        tagged_frame(index) for index in range(150)
    ]
    finals = [event for event in of_type(events, "transcript.translated") if event.final]
    assert [event.text for event in finals] == ["Доброе утро всем.", "Давайте начнём встречу."]

    ended = await client.translation.sessions.retrieve(session.id)
    assert ended.status == "ended"
    assert ended.end_reason == "finished"
    assert ended.usage.input_audio_ms == 3000


async def test_it_survives_the_reconnect_every_60_seconds(client):
    session = await client.translation.sessions.create(target_language="en", test=True)

    async with client.translation.connect(session.client_secret) as stream:
        # 65 seconds of audio, queued at once and sent in real time.
        frames = 65 * 50
        await stream.send(tagged_audio(frames))
        await stream.finish()
        events = await collect(stream)

    assert events[-1].reason == "finished"
    assert len(of_type(events, "reconnect")) == 1
    assert len(of_type(events, "ready")) == 2
    assert not of_type(events, "discontinuity")
    assert [event.pcm for event in of_type(events, "audio")] == [
        tagged_frame(index) for index in range(frames)
    ]
    # docs/test-mode.md: every fifth item has no source text.
    sourceless = "item_test_5"
    assert any(event.item_id == sourceless for event in of_type(events, "transcript.translated"))
    assert not any(event.item_id == sourceless for event in of_type(events, "transcript.source"))


async def test_an_unsupported_language_is_a_typed_error(client):
    with pytest.raises(SpekoTranslationInvalidRequestError) as caught:
        await client.translation.sessions.create(target_language="xx")

    assert caught.value.status == 400
    assert caught.value.code == "unsupported_language"
    assert caught.value.param == "target_language"
    assert caught.value.doc_url
    assert caught.value.request_id


async def test_a_replay_of_create_returns_the_same_session(client):
    key = os.urandom(16).hex()
    first = await client.translation.sessions.create(
        target_language="ru", test=True, idempotency_key=key
    )
    replay = await client.translation.sessions.create(
        target_language="ru", test=True, idempotency_key=key
    )

    assert replay.id == first.id
    assert replay.client_secret
    assert replay.client_secret != first.client_secret


async def test_an_error_trigger_ends_the_session_with_its_code(client):
    session = await client.translation.sessions.create(
        target_language="x-error-internal", test=True
    )
    async with client.translation.connect(session.client_secret) as stream:
        events = await collect(stream)

    assert [event.type for event in events[-2:]] == ["error", "ended"]
    assert events[-2].error.code == "internal"
    assert events[-2].error.doc_url
    assert events[-1].reason == "internal"
    ended = await client.translation.sessions.retrieve(session.id)
    assert ended.end_reason == "internal"


async def test_a_wrong_secret_leaves_the_session_unchanged(client):
    session = await client.translation.sessions.create(target_language="ru", test=True)
    stream = client.translation.connect("not_the_secret", stream_url=session.stream_url)
    async with stream:
        events = await collect(stream)

    assert [event.type for event in events] == ["error"]
    assert events[0].error.code == "client_secret_invalid"
    retrieved = await client.translation.sessions.retrieve(session.id)
    assert retrieved.status == "pending"


async def test_ending_from_the_server_reaches_the_stream(client):
    session = await client.translation.sessions.create(target_language="ru", test=True)
    async with client.translation.connect(session.client_secret) as stream:
        await stream.send(tagged_audio(10))
        ended = await client.translation.sessions.end(session.id)
        events = await collect(stream)

    assert ended.end_reason == "ended_by_api"
    assert events[-1].reason == "ended_by_api"
