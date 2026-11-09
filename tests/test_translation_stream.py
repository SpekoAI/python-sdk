"""The stream helper against the spec's mock server, over real sockets."""

import array
import asyncio
import json
import logging
import random
import struct
import time
from contextlib import asynccontextmanager

import pytest
from websockets.exceptions import ConnectionClosed, InvalidURI

import spekoai.translation_stream as stream_module
from spekoai import (
    AsyncSpeko,
    AsyncTranslationStream,
    SpekoTranslationConnectionError,
    TranslationAudio,
    TranslationReconnect,
)
from spekoai.resources import translation as translation_module

from .translation_mock import TEST_MODE_HELD_FRAMES, MockTranslationServer

FRAME_PCM = 960
SILENCE = bytes(FRAME_PCM)
LOOSE_TIMEOUT = 20.0


def tagged_frame(index: int) -> bytes:
    """A frame of PCM that says which frame it is. Never silent."""
    return struct.pack("<h", (index % 30000) + 1) * 480


def tagged_audio(count: int, start: int = 0) -> bytes:
    return b"".join(tagged_frame(index) for index in range(start, start + count))


def wire_sequences(connection) -> list[int]:
    return [sequence for sequence, _, _ in connection.frames]


def wire_pcm(connection) -> list[bytes]:
    return [pcm for _, _, pcm in connection.frames]


def audio_pcm(events) -> list[bytes]:
    return [event.pcm for event in events if event.type == "audio"]


def of_type(events, kind: str) -> list:
    return [event for event in events if event.type == kind]


async def close_code(connection) -> int:
    await asyncio.wait_for(connection.ws.wait_closed(), LOOSE_TIMEOUT)
    return connection.ws.protocol.close_code


async def collect(stream) -> list:
    async def run():
        return [event async for event in stream]

    return await asyncio.wait_for(run(), LOOSE_TIMEOUT)


@asynccontextmanager
async def running(**options):
    settings = {"ready_delay": 0.01, "echo_delay": 0.05, "reconnect_every": None, **options}
    async with MockTranslationServer(**settings) as server:
        client = AsyncSpeko(api_key="sk-test", translation_base_url=server.base_url)
        try:
            yield server, client
        finally:
            await client.close()


async def new_session(client, target_language="ru"):
    return await client.translation.sessions.create(target_language=target_language, test=True)


# --- Framing and pacing on the wire ------------------------------------------------


async def test_it_offers_both_subprotocols_and_numbers_frames_from_zero():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            await stream.finish()
            await collect(stream)

        connection = server.session().connections[0]
        assert connection.offered == ["speko-translation", session.client_secret]
        assert connection.ws.subprotocol == "speko-translation"
        assert connection.frames == [
            (index, index * 480, tagged_frame(index)) for index in range(5)
        ]
        assert connection.controls == [{"type": "finish"}]


async def test_chunks_of_any_size_reach_the_wire_as_whole_contiguous_frames():
    rng = random.Random(7)
    audio = tagged_audio(30)
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            position = 0
            while position < len(audio):
                size = rng.randrange(1, 2500)
                await stream.send(audio[position : position + size])
                position += size
            await stream.finish()
            await collect(stream)

        connection = server.session().connections[0]
        assert wire_sequences(connection) == list(range(30))
        assert b"".join(wire_pcm(connection)) == audio


async def test_empty_and_array_chunks_are_fine():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(b"")
            await stream.send(array.array("h", [5] * 480))
            await stream.finish()
            await collect(stream)

        assert wire_pcm(server.session().connections[0]) == [struct.pack("<h", 5) * 480]


async def test_finish_pads_the_last_partial_frame_with_silence():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(b"\x07\x00" * 500)
            await stream.finish()
            await collect(stream)

        pcm = wire_pcm(server.session().connections[0])
        assert pcm == [b"\x07\x00" * 480, b"\x07\x00" * 20 + bytes(920)]


async def test_a_common_rate_is_resampled_to_24_khz_on_the_wire():
    rate = 16000
    audio = struct.pack("<h", 9000) * (rate // 2)
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret, sample_rate=rate) as stream:
            await stream.send(audio)
            await stream.finish()
            await collect(stream)

        connection = server.session().connections[0]
        # Half a second is 25 frames, and the resampler's edges can add one.
        assert 25 <= len(connection.frames) <= 26
        assert wire_sequences(connection) == list(range(len(connection.frames)))
        middle = struct.unpack("<480h", connection.frames[12][2])
        assert all(abs(sample - 9000) < 200 for sample in middle)


async def test_a_bad_sample_rate_is_refused_before_any_connection():
    async with running() as (server, client):
        session = await new_session(client)
        with pytest.raises(ValueError):
            client.translation.connect(session.client_secret, sample_rate=0)
        assert server.session().connections == []


async def test_audio_queued_at_once_goes_out_in_real_time():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            started = time.monotonic()
            await stream.send(tagged_audio(100))
            await asyncio.sleep(0.3)
            elapsed = time.monotonic() - started
            taken = len(server.session().connections[0].frames)

            assert 0 < taken <= elapsed / 0.02 + 2
            await stream.cancel()
            await collect(stream)


async def test_a_file_sent_at_once_stays_within_what_test_mode_holds():
    # The spec's 1.5 s echo and 1.7 s hold, with a move halfway through whose lost time
    # the helper catches up on. Sending ahead of real time would fail the session.
    async with running(
        echo_delay=1.5, max_held_frames=TEST_MODE_HELD_FRAMES, reconnect_after_frames=100
    ) as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(150))
            await stream.finish()
            events = await collect(stream)

        assert events[-1].reason == "finished"
        assert audio_pcm(events) == [tagged_frame(index) for index in range(150)]


async def test_silence_keeps_a_quiet_session_alive():
    async with running(audio_gap=0.3) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await asyncio.sleep(0.8)

            mock_session = server.session()
            connection = mock_session.connections[0]
            assert mock_session.status == "active"
            taken = len(connection.frames)
            assert taken >= 20
            assert wire_sequences(connection) == list(range(taken))
            assert set(wire_pcm(connection)) == {SILENCE}

            await stream.finish()
            events = await collect(stream)

        assert events[-1].type == "ended"
        assert events[-1].reason == "finished"


async def test_silence_stops_at_finish():
    async with running(echo_delay=0.3) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            await asyncio.sleep(0.2)
            taken = len(server.session().connections[0].frames)
            await asyncio.sleep(0.2)
            assert len(server.session().connections[0].frames) == taken
            await collect(stream)


async def test_silence_between_sends_keeps_the_numbering_and_the_audio():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            await asyncio.sleep(0.4)
            await stream.send(tagged_audio(5, start=5))
            await stream.finish()
            await collect(stream)

        connection = server.session().connections[0]
        assert wire_sequences(connection) == list(range(len(connection.frames)))
        pcm = wire_pcm(connection)
        assert SILENCE in pcm
        assert [frame for frame in pcm if frame != SILENCE] == [
            tagged_frame(index) for index in range(10)
        ]


async def test_a_late_chunk_inside_the_grace_period_gets_no_silence():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            # 100 ms of audio every 100 ms on average, with every other chunk 70 ms late.
            for chunk in range(8):
                await stream.send(tagged_audio(5, start=chunk * 5))
                await asyncio.sleep(0.17 if chunk % 2 == 0 else 0.03)
            await stream.finish()
            await collect(stream)

        pcm = wire_pcm(server.session().connections[0])
        # A stall on a busy machine can still earn a silent frame or two. Without the
        # grace period the first late chunk costs four, and they stay as added latency.
        assert pcm.count(SILENCE) <= 2
        assert [frame for frame in pcm if frame != SILENCE] == [
            tagged_frame(index) for index in range(40)
        ]


async def test_a_large_send_lets_the_event_loop_run():
    async with running() as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            turns = 0

            async def count_turns():
                nonlocal turns
                while True:
                    turns += 1
                    await asyncio.sleep(0)

            counter = asyncio.create_task(count_turns())
            await stream.send(bytes(48000))
            counter.cancel()

            assert turns >= 5
            await stream.cancel()


# --- Events -------------------------------------------------------------------------


async def test_it_yields_audio_captions_lag_and_the_rest_in_order():
    async with running(reconnect_every=None) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(120))
            await stream.finish()
            events = await collect(stream)

        audio = of_type(events, "audio")
        assert [event.sequence for event in audio] == list(range(120))
        assert [event.sample_offset for event in audio] == [i * 480 for i in range(120)]
        assert audio_pcm(events) == [tagged_frame(index) for index in range(120)]
        assert isinstance(audio[0], TranslationAudio)

        assert events[0].type in {"ready", "audio"}
        assert len(of_type(events, "ready")) == 1
        assert of_type(events, "lag")
        assert all(event.input_buffered_ms == 0 for event in of_type(events, "lag"))

        translated = of_type(events, "transcript.translated")
        source = of_type(events, "transcript.source")
        assert [(e.item_id, e.text, e.final) for e in translated] == [
            ("item_test_1", "Доброе утро", False),
            ("item_test_1", "Доброе утро всем.", False),
            ("item_test_1", "Доброе утро всем.", True),
            ("item_test_2", "Давайте начнём встречу.", True),
        ]
        assert source[-1].text == "Let's start the meeting."

        ended = events[-1]
        assert ended.type == "ended"
        assert ended.reason == "finished"
        assert ended.usage.input_audio_ms == 2400
        assert ended.usage.output_audio_ms == 2400
        assert await close_code(server.session().connections[0]) == 1000


async def test_an_item_without_source_text_comes_through():
    async with running(frames_per_item=4) as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(20))
            await stream.finish()
            events = await collect(stream)

        translated = {event.item_id for event in of_type(events, "transcript.translated")}
        source = {event.item_id for event in of_type(events, "transcript.source")}
        assert translated == {f"item_test_{number}" for number in range(1, 6)}
        assert source == {f"item_test_{number}" for number in range(1, 5)}


async def test_error_targets_are_case_insensitive_and_keep_the_case_sent():
    async with running(trigger_delay=0.2) as (_, client):
        session = await new_session(client, target_language="X-Error-Internal")
        async with client.translation.connect(session.client_secret) as stream:
            events = await collect(stream)

        assert session.target_language == "X-Error-Internal"
        assert events[-1].reason == "internal"


async def test_ping_gets_a_pong():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.ping("ping_1")
            await stream.ping()
            await stream.finish()
            events = await collect(stream)

        assert [event.id for event in of_type(events, "pong")] == ["ping_1", None]
        assert server.session().connections[0].controls[:2] == [
            {"type": "ping", "id": "ping_1"},
            {"type": "ping"},
        ]


async def test_a_ping_id_the_server_would_end_the_session_for_is_refused_here():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            with pytest.raises(ValueError):
                await stream.ping("x" * 65)
            await stream.ping("x" * 64)
            await stream.finish()
            events = await collect(stream)

        assert events[-1].reason == "finished"
        assert server.session().end_reason == "finished"


async def test_unknown_event_types_and_fields_are_ignored():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            ws = server.session().connections[0].ws
            await ws.send('{"type": "from.the.future", "value": 1}')
            await ws.send('{"type": "pong", "id": "a", "from_the_future": true}')
            await stream.finish()
            events = await collect(stream)

        assert "from.the.future" not in {event.type for event in events}
        assert of_type(events, "pong")[0].id == "a"
        assert events[-1].reason == "finished"


async def test_cancel_drops_the_queue_and_ends_now():
    async with running(echo_delay=5.0) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(500))
            await stream.cancel()
            events = await collect(stream)

        assert events[-1].type == "ended"
        assert events[-1].reason == "canceled"
        assert not of_type(events, "audio")
        connection = server.session().connections[0]
        assert connection.controls == [{"type": "cancel"}]
        assert len(connection.frames) < 500
        assert await close_code(connection) == 1000


async def test_ending_from_the_server_reaches_the_stream():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            ended = await client.translation.sessions.end(session.id)
            events = await collect(stream)

        assert ended.status == "ended"
        assert ended.end_reason == "ended_by_api"
        assert events[-1].reason == "ended_by_api"
        assert await close_code(server.session().connections[0]) == 1000


# --- Reconnect ----------------------------------------------------------------------


async def test_it_moves_to_a_new_connection_and_loses_nothing():
    async with running(reconnect_after_frames=25) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(60))
            await stream.finish()
            events = await collect(stream)

        connections = server.session().connections
        assert len(connections) == 3
        reconnects = of_type(events, "reconnect")
        assert len(reconnects) == 2
        assert connections[0].credential == session.client_secret
        assert connections[1].credential == reconnects[0].resume_token
        assert connections[2].credential == reconnects[1].resume_token
        assert connections[1].offered == ["speko-translation", reconnects[0].resume_token]

        assert await close_code(connections[0]) == 1012
        assert await close_code(connections[1]) == 1012
        assert await close_code(connections[2]) == 1000

        # Numbering continues from next_sequence on each connection, with no gap.
        assert wire_sequences(connections[0])[0] == 0
        assert wire_sequences(connections[1])[0] == reconnects[0].next_sequence
        assert wire_sequences(connections[2])[0] == reconnects[1].next_sequence
        assert wire_sequences(connections[0]) + wire_sequences(connections[1]) + wire_sequences(
            connections[2]
        ) == list(range(60))

        # All the audio comes back once, in order, and each socket numbers its own from 0.
        assert audio_pcm(events) == [tagged_frame(index) for index in range(60)]
        sequences = [event.sequence for event in of_type(events, "audio")]
        assert sequences == [*range(25), *range(25), *range(10)]
        assert len(of_type(events, "ready")) == 3
        assert events[-1].reason == "finished"


async def test_a_send_that_fails_as_a_move_begins_does_not_stop_the_stream():
    # With the rewind, the frame whose send gets cut off is the first one to resend.
    async with running(
        reconnect_after_frames=10, reconnect_rewind=1, audio_gap=1.0, echo_delay=0.2
    ) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            old_socket = stream._ws
            real_send = old_socket.send
            sends = 0

            async def send_until_the_move(message):
                nonlocal sends
                sends += 1
                await real_send(message)
                if sends == 10:
                    # Frame 9 makes the mock ask for the move. This send stays suspended
                    # while the reader takes `reconnect`, and then the old socket fails it.
                    while not stream._moving:
                        await asyncio.sleep(0.001)
                    raise ConnectionClosed(None, None)

            old_socket.send = send_until_the_move
            await stream.send(tagged_audio(20))
            await stream.finish()
            events = await collect(stream)

        assert events[-1].reason == "finished"
        connections = server.session().connections
        assert of_type(events, "reconnect")[0].next_sequence == 9
        assert wire_sequences(connections[1])[0] == 9
        assert audio_pcm(events) == [tagged_frame(index) for index in range(20)]


async def test_a_drop_after_a_move_is_still_a_lost_connection():
    async with running(reconnect_after_frames=5, drop_after_frames=8) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(30))
            with pytest.raises(SpekoTranslationConnectionError) as caught:

                async def read_past_the_move():
                    async for event in stream:
                        if event.type == "reconnect":
                            server.reconnect_after_frames = None

                await asyncio.wait_for(read_past_the_move(), LOOSE_TIMEOUT)

        assert caught.value.code == "connection_lost"
        assert len(server.session().connections) == 2


async def test_a_second_move_keeps_the_frames_the_first_had_not_resent_yet():
    async with running() as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            stream._sent.clear()
            stream._sent.extend([(1, b"one"), (2, b"two")])
            stream._replay.extend([(3, b"three")])

            stream._begin_move(TranslationReconnect(resume_token="unused", next_sequence=2))

            assert list(stream._replay) == [(2, b"two"), (3, b"three")]
            assert not stream._sent


async def test_silence_does_not_burst_after_a_slow_move():
    async with running(reconnect_after_frames=5, refuse_resumes=2) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            async for event in stream:
                if event.type == "reconnect":
                    server.reconnect_after_frames = None
                    break
            mock_session = server.session()
            while len(mock_session.connections) < 2:
                await asyncio.sleep(0.005)
            opened = time.monotonic()
            await asyncio.sleep(0.2)
            elapsed = time.monotonic() - opened
            taken = len(mock_session.connections[1].frames)

            # The move took about 0.75 s, which is about 37 frames of silence if owed.
            assert 0 < taken <= elapsed / 0.02 + 6
            await stream.cancel()


async def test_a_second_move_does_not_resend_a_frame_twice():
    # Each connection takes 30 frames and then gives 25 of them back, so frames that
    # were replayed on one connection get replayed again on the next.
    async with running(reconnect_after_frames=30, reconnect_rewind=25, echo_delay=2.0) as (
        server,
        client,
    ):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(50))
            await stream.finish()
            events = await collect(stream)

        for connection in server.session().connections:
            sequences = wire_sequences(connection)
            assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
        assert not of_type(events, "discontinuity")
        assert audio_pcm(events) == [tagged_frame(index) for index in range(50)]
        assert events[-1].reason == "finished"


async def test_a_reconnect_with_nothing_queued_carries_on_with_silence():
    async with running(reconnect_after_frames=10) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            events = []
            async for event in stream:
                events.append(event)
                if len(of_type(events, "ready")) == 2:
                    await asyncio.sleep(0.1)
                    await stream.finish()

        connections = server.session().connections
        sequences = [
            sequence for connection in connections for sequence in wire_sequences(connection)
        ]
        assert sequences == list(range(len(sequences)))
        assert events[-1].reason == "finished"


async def test_frames_already_sent_are_resent_from_next_sequence():
    async with running(reconnect_after_frames=25, reconnect_rewind=10, echo_delay=0.5) as (
        server,
        client,
    ):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(40))
            await stream.finish()
            events = await collect(stream)

        connections = server.session().connections
        first_move = of_type(events, "reconnect")[0]
        assert first_move.next_sequence == 15
        assert wire_sequences(connections[1])[0] == 15
        # The old connection "didn't take" frames 15-24, so they come out once only.
        assert audio_pcm(events) == [tagged_frame(index) for index in range(40)]
        assert not of_type(events, "discontinuity")
        assert events[-1].reason == "finished"


async def test_a_first_frame_later_than_next_sequence_gets_a_discontinuity():
    # next_sequence is 0, but only the last second of sent frames is still held.
    async with running(reconnect_after_frames=70, reconnect_rewind=70, echo_delay=0.05) as (
        server,
        client,
    ):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(80))
            await stream.finish()
            events = await collect(stream)

        connections = server.session().connections
        move = of_type(events, "reconnect")[0]
        assert move.next_sequence == 0
        assert wire_sequences(connections[1]) == list(range(20, 80))
        assert wire_pcm(connections[1])[0] == tagged_frame(20)
        gap = of_type(events, "discontinuity")
        assert [(event.first_sequence, event.last_sequence) for event in gap] == [(0, 19)]
        assert events[-1].reason == "finished"


async def test_a_refused_open_retries_with_the_same_resume_token():
    async with running(reconnect_after_frames=10, refuse_resumes=2) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(20))
            await stream.finish()
            events = await collect(stream)

        connections = server.session().connections
        token = of_type(events, "reconnect")[0].resume_token
        assert server.refused_resumes == [token, token]
        assert connections[1].credential == token
        assert audio_pcm(events) == [tagged_frame(index) for index in range(20)]


async def test_giving_up_on_a_resume_raises_a_typed_error(monkeypatch):
    monkeypatch.setattr(stream_module, "_RESUME_SECONDS", 0.5)
    async with running(reconnect_after_frames=10, refuse_resumes=1000) as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(20))
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code == "resume_failed"


async def test_a_finish_that_crossed_the_reconnect_is_sent_again():
    async with running(reconnect_after_frames=5, echo_delay=0.05) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            await stream.finish()
            events = await collect(stream)

        connections = server.session().connections
        # The mock asked to move on frame 4, so it ignored the finish on that socket.
        assert connections[0].controls == []
        assert connections[1].controls == [{"type": "finish"}]
        assert await close_code(connections[1]) == 1000
        assert events[-1].reason == "finished"
        assert audio_pcm(events) == [tagged_frame(index) for index in range(5)]


async def test_a_cancel_during_a_move_is_sent_on_the_new_socket():
    async with running(reconnect_after_frames=5) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(200))
            events = []
            async for event in stream:
                events.append(event)
                if event.type == "reconnect":
                    await stream.cancel()

        connections = server.session().connections
        assert len(connections) == 2
        assert connections[0].controls == []
        assert connections[1].controls == [{"type": "cancel"}]
        assert connections[1].frames == []
        assert events[-1].reason == "canceled"


async def test_a_ping_during_a_move_waits_for_the_new_socket():
    async with running(reconnect_after_frames=5) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            events = []
            async for event in stream:
                events.append(event)
                if event.type == "reconnect":
                    await stream.ping("during_move")
                if event.type == "pong":
                    await stream.finish()

        assert of_type(events, "pong")[0].id == "during_move"
        assert server.session().connections[1].controls[0] == {
            "type": "ping",
            "id": "during_move",
        }
        assert events[-1].reason == "finished"


async def test_the_old_socket_dropping_during_a_move_does_not_end_the_stream():
    async with running(reconnect_after_frames=10, drop_old_on_reconnect=True, echo_delay=0.5) as (
        server,
        client,
    ):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(15))
            await stream.finish()
            events = await collect(stream)

        # The socket dropped with frames 0-9 still waiting to go out, and the new
        # socket reports them.
        assert (of_type(events, "discontinuity")[0].first_sequence) == 0
        assert (of_type(events, "discontinuity")[0].last_sequence) == 9
        assert audio_pcm(events) == [tagged_frame(index) for index in range(10, 15)]
        assert events[-1].reason == "finished"
        assert server.session().end_reason == "finished"


# --- Opening ------------------------------------------------------------------------


async def test_a_refused_open_retries_with_the_same_secret():
    async with running(refuse_opens=2) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            events = await collect(stream)

        assert server.refused_opens == 2
        assert server.session().connections[0].credential == session.client_secret
        assert events[-1].reason == "finished"


async def test_giving_up_on_opening_raises_a_typed_error():
    async with running(refuse_opens=1000) as (_, client):
        session = await new_session(client)
        with pytest.raises(SpekoTranslationConnectionError) as caught:
            async with client.translation.connect(session.client_secret, connect_timeout=0.5):
                pass

        assert caught.value.code == "connect_failed"
        assert caught.value.status == 0
        assert caught.value.type == "connection_error"
        assert session.client_secret not in str(caught.value)


async def test_a_stream_url_that_can_never_work_fails_at_once():
    stream = AsyncTranslationStream("secret", stream_url="wss://")
    started = time.monotonic()
    with pytest.raises(InvalidURI):
        async with stream:
            pass
    assert time.monotonic() - started < 1


async def test_connect_finds_the_stream_url_of_a_session_this_client_created():
    async with running() as (server, client):
        session = await new_session(client)
        stream = client.translation.connect(session.client_secret)
        async with stream:
            await stream.finish()
            await collect(stream)

        assert server.session().connections


async def test_a_secret_from_elsewhere_needs_its_stream_url():
    async with running() as (server, creating_client):
        session = await new_session(creating_client)
        other = AsyncSpeko(api_key="sk-test", translation_base_url=server.base_url)
        try:
            with pytest.raises(ValueError, match="stream_url"):
                other.translation.connect(session.client_secret)

            stream = other.translation.connect(session.client_secret, stream_url=session.stream_url)
            async with stream:
                await stream.finish()
                events = await collect(stream)
        finally:
            await other.close()

        assert events[-1].reason == "finished"


async def test_an_expired_secret_is_forgotten_at_the_next_create(monkeypatch):
    async with running() as (_, client):
        old = await new_session(client)
        client.translation.connect(old.client_secret)

        later = time.time() + 120
        monkeypatch.setattr(translation_module.time, "time", lambda: later)
        new = await new_session(client)

        client.translation.connect(new.client_secret)
        with pytest.raises(ValueError, match="stream_url"):
            client.translation.connect(old.client_secret)


async def test_an_app_without_an_api_key_uses_the_stream_directly():
    async with running() as (_, client):
        session = await new_session(client)
        stream = AsyncTranslationStream(session.client_secret, stream_url=session.stream_url)
        async with stream:
            await stream.finish()
            events = await collect(stream)

        assert events[-1].reason == "finished"


async def test_a_replay_of_create_gives_a_fresh_secret_that_connects():
    async with running() as (server, client):
        first = await client.translation.sessions.create(
            target_language="ru", test=True, idempotency_key="same"
        )
        replay = await client.translation.sessions.create(
            target_language="ru", test=True, idempotency_key="same"
        )
        assert replay.id == first.id
        assert replay.client_secret != first.client_secret

        async with client.translation.connect(replay.client_secret) as stream:
            await stream.finish()
            await collect(stream)

        after = await client.translation.sessions.create(
            target_language="ru", test=True, idempotency_key="same"
        )
        assert after.id == first.id
        assert after.client_secret is None
        assert server.session().connections[0].credential == replay.client_secret


# --- Failures -----------------------------------------------------------------------


async def test_a_refused_credential_is_an_error_event_and_the_session_is_unchanged():
    async with running() as (server, client):
        session = await new_session(client)
        stream = client.translation.connect("not_the_secret", stream_url=session.stream_url)
        async with stream:
            events = await collect(stream)

        assert [event.type for event in events] == ["error"]
        assert events[0].error.code == "client_secret_invalid"
        assert events[0].error.type == "authentication_error"
        assert events[0].error.doc_url.endswith("#client_secret_invalid")
        assert server.session().status == "pending"


@pytest.mark.parametrize(
    "code", ["translation_failed", "balance_exhausted", "audio_timeout", "internal"]
)
async def test_an_error_after_ready_is_an_error_event_then_ended(code):
    async with running(trigger_delay=0.1) as (server, client):
        session = await new_session(client, target_language=f"x-error-{code}")
        async with client.translation.connect(session.client_secret) as stream:
            events = await collect(stream)

        assert events[0].type == "ready"
        assert [event.type for event in events[-2:]] == ["error", "ended"]
        assert events[-2].error.code == code
        assert events[-1].reason == code
        assert server.session().end_reason == code
        assert await close_code(server.session().connections[0]) >= 4400


async def test_a_limit_at_connect_is_an_error_event_then_ended():
    async with running() as (server, client):
        session = await new_session(client, target_language="x-error-org_concurrency_limit")
        async with client.translation.connect(session.client_secret) as stream:
            events = await collect(stream)

        assert [event.type for event in events] == ["error", "ended"]
        assert events[0].error.type == "rate_limit_error"
        assert await close_code(server.session().connections[0]) == 4429


async def test_a_lapsed_token_is_an_error_event_with_no_ended():
    async with running() as (_, client):
        session = await new_session(client, target_language="x-error-resume_token_invalid")
        async with client.translation.connect(session.client_secret) as stream:
            events = await collect(stream)

        assert [event.type for event in events] == ["error"]
        assert events[0].error.code == "resume_token_invalid"


async def test_a_lost_connection_raises_once_and_then_the_stream_is_over():
    async with running(drop_after_frames=5) as (_, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(20))
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

            assert caught.value.code == "connection_lost"
            assert caught.value.close_code == 1006
            assert await collect(stream) == []
            assert await collect(stream) == []
            with pytest.raises(SpekoTranslationConnectionError) as closed:
                await stream.send(tagged_audio(1))
            assert closed.value.code == "stream_closed"


@pytest.mark.parametrize(
    "message",
    [
        b"too short",
        "not json",
        '["not", "an", "object"]',
        '{"type": "lag"}',
        '{"type": "reconnect", "resume_token": "trs_1_resume_SECRETVALUE"}',
        '{"type": []}',
        '{"type": {"a": 1}}',
        "[" * 200000,
        '{"type":' * 100000,
    ],
    ids=[
        "short-frame",
        "bad-json",
        "not-object",
        "missing-field",
        "reconnect-missing-field",
        "type-is-a-list",
        "type-is-an-object",
        "deep-array",
        "deep-object",
    ],
)
async def test_a_malformed_message_from_the_server_raises_without_echoing_it(message):
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await server.session().connections[0].ws.send(message)
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code == "protocol_error"
        assert "SECRETVALUE" not in str(caught.value)
        assert caught.value.__cause__ is None


async def test_a_failure_here_stops_the_silence_and_cancels_the_session_at_once():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            connection = server.session().connections[0]
            await connection.ws.send('{"type": []}')
            with pytest.raises(SpekoTranslationConnectionError):
                await collect(stream)

            await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
            assert server.session().end_reason == "canceled"
            sent = len(connection.frames)
            await asyncio.sleep(0.15)
            assert len(connection.frames) == sent


@pytest.mark.parametrize(
    "token",
    ["", "a, SECRETVALUE", "abc\r\nX: SECRETVALUE", "токен-SECRETVALUE"],
    ids=["empty", "comma", "crlf", "not-ascii"],
)
async def test_a_resume_token_that_cannot_be_offered_raises_without_echoing_it(token):
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            reconnect = {"type": "reconnect", "resume_token": token, "next_sequence": 0}
            await server.session().connections[0].ws.send(json.dumps(reconnect))
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code in ("protocol_error", "resume_failed")
        assert "SECRETVALUE" not in str(caught.value)


async def test_a_redirect_answering_a_resume_gives_up_at_the_budget(monkeypatch):
    monkeypatch.setattr(stream_module, "_RESUME_SECONDS", 0.5)
    options = {"reconnect_after_frames": 3, "redirect_resumes_to": "https://edge.example/x"}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code == "resume_failed"
        assert len(server.session().connections) == 1


async def test_a_credential_does_not_follow_a_redirect_to_another_host():
    async with MockTranslationServer() as other:
        target = f"{other._ws_base}/v1/translation/sessions/trs_x/stream"
        async with running(redirect_opens_to=target) as (server, client):
            session = await new_session(client)
            stream = client.translation.connect(session.client_secret, connect_timeout=0.1)
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                async with stream:
                    pass

        assert caught.value.code == "connect_failed"
        assert server.upgrades[0] == session.client_secret
        assert other.upgrades == []


# --- Plain transports ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.com/stream",
        "ws://127.0.0.1.example.com/stream",
        "ws://localhost.example.com/stream",
        "ws://127.0.0.1@example.com/stream",
        "ws://10.0.0.1/stream",
        "ws:///stream",
        "http://127.0.0.1/stream",
        "https://example.com/stream",
        "not a websocket url",
    ],
)
def test_a_stream_url_that_would_expose_the_credential_is_refused(url):
    with pytest.raises(ValueError):
        AsyncTranslationStream("secret", stream_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "wss://example.com/stream",
        "ws://127.0.0.1:9/stream",
        "ws://127.8.8.8/stream",
        "ws://[::1]:9/stream",
        "ws://localhost:9/stream",
        "ws://LOCALHOST:9/stream",
    ],
)
def test_wss_and_a_loopback_ws_are_accepted(url):
    AsyncTranslationStream("secret", stream_url=url)


async def test_a_plain_stream_url_in_a_created_session_is_refused_at_connect():
    async with running() as (server, client):
        session = await new_session(client)
        known = client.translation.sessions._unexpired_secrets
        known[session.client_secret] = ("ws://example.com/stream", session.expires_at)
        with pytest.raises(ValueError):
            client.translation.connect(session.client_secret)


# --- Unread events ------------------------------------------------------------------


AUDIO_EVENT_COST = 976 + stream_module._EVENT_OVERHEAD_BYTES


def quiet_server():
    """A session that sends nothing unless a test tells it to."""
    return running(ready_delay=60, echo_delay=60)


async def test_events_left_unread_end_the_session_with_a_typed_error(monkeypatch):
    monkeypatch.setattr(stream_module, "_MAX_BUFFERED_BYTES", 10 * AUDIO_EVENT_COST)
    async with quiet_server() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            connection = server.session().connections[0]
            for index in range(11):
                await connection.ws.send(struct.pack("<QQ", index, index * 480) + SILENCE)

            # Nothing has been read yet, and the session has already stopped.
            await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
            assert server.session().end_reason == "canceled"

            events = []
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                async for event in stream:
                    events.append(event)

        assert [event.sequence for event in events] == list(range(10))
        assert caught.value.code == "client_too_slow"


async def test_reading_events_makes_room_for_more(monkeypatch):
    monkeypatch.setattr(stream_module, "_MAX_BUFFERED_BYTES", 2 * AUDIO_EVENT_COST)
    async with quiet_server() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            connection = server.session().connections[0]
            for index in range(10):
                await connection.ws.send(struct.pack("<QQ", index, index * 480) + SILENCE)
                event = await asyncio.wait_for(stream.__anext__(), LOOSE_TIMEOUT)
                assert event.sequence == index

            await stream.cancel()
            await collect(stream)

        assert server.session().end_reason == "canceled"


async def test_the_limit_on_unread_events_also_counts_small_ones(monkeypatch):
    monkeypatch.setattr(stream_module, "_MAX_BUFFERED_BYTES", 10 * AUDIO_EVENT_COST)
    async with quiet_server() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            connection = server.session().connections[0]
            pong = '{"type": "pong"}'
            for _ in range(int(10 * AUDIO_EVENT_COST / stream_module._EVENT_OVERHEAD_BYTES) + 1):
                await connection.ws.send(pong)

            await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code == "client_too_slow"


# --- Finishing ----------------------------------------------------------------------


async def test_a_server_that_never_ends_after_finish_raises_a_typed_error(monkeypatch):
    monkeypatch.setattr(stream_module, "_END_TIMEOUT_SECONDS", 0.2)
    async with running(never_end=True) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

            assert caught.value.code == "end_timeout"
            await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
            assert server.session().end_reason == "canceled"


async def test_the_deadline_after_finish_starts_when_finish_goes_out(monkeypatch):
    monkeypatch.setattr(stream_module, "_END_TIMEOUT_SECONDS", 0.2)
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(30))  # 0.6 s of queue, three times the deadline.
            await stream.finish()
            events = await collect(stream)

        assert events[-1].type == "ended"
        assert events[-1].reason == "finished"


async def test_the_deadline_after_finish_starts_again_on_a_new_connection(monkeypatch):
    monkeypatch.setattr(stream_module, "_END_TIMEOUT_SECONDS", 0.3)
    options = {"reconnect_after_frames": 3, "refuse_resumes": 2, "echo_delay": 0.2}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(3))
            await stream.finish()
            events = await collect(stream)

        assert events[-1].type == "ended"
        assert server.session().connections[1].controls == [{"type": "finish"}]


async def test_a_prompt_ended_after_finish_raises_nothing(monkeypatch):
    monkeypatch.setattr(stream_module, "_END_TIMEOUT_SECONDS", 1.0)
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            events = await collect(stream)
            await asyncio.sleep(1.1)  # Past the deadline: nothing may fire any more.

        assert events[-1].type == "ended"
        assert server.session().end_reason == "finished"


# --- Limits on moves ----------------------------------------------------------------


async def test_reconnects_during_a_move_are_ignored():
    options = {"reconnect_after_frames": 3, "refuse_resumes": 2}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            events = []
            async for event in stream:
                events.append(event)
                if event.type == "reconnect":
                    break
            old = server.session().connections[0]
            for index in range(5):
                extra = {
                    "type": "reconnect",
                    "resume_token": f"extra_resume_{index}",
                    "next_sequence": 0,
                }
                await old.ws.send(json.dumps(extra))
            await stream.finish()
            events += await collect(stream)

        assert len(of_type(events, "reconnect")) == 1
        assert not [token for token in server.upgrades if token.startswith("extra")]
        assert len(server.session().connections) == 2


async def test_a_reconnect_from_a_socket_already_left_is_ignored():
    options = {"reconnect_after_frames": 3, "keep_old_open": True}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(3))
            events = []
            async for event in stream:
                events.append(event)
                if event.type == "reconnect":
                    break
            while len(server.session().connections) < 2:
                await asyncio.sleep(0.01)
            server.reconnect_after_frames = None
            await asyncio.sleep(0.05)
            old = server.session().connections[0]
            extra = {"type": "reconnect", "resume_token": "extra_resume", "next_sequence": 0}
            await old.ws.send(json.dumps(extra))
            await asyncio.sleep(0.1)
            await stream.cancel()
            events += await collect(stream)

        assert "extra_resume" not in server.upgrades
        assert len(of_type(events, "reconnect")) == 1


async def test_too_many_open_sockets_end_the_stream_with_a_typed_error():
    options = {"reconnect_after_frames": 1, "keep_old_open": True}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await collect(stream)

        assert caught.value.code == "protocol_error"
        assert len(server.session().connections) == stream_module._MAX_OPEN_SOCKETS


# --- Lifecycle ----------------------------------------------------------------------


async def test_leaving_the_block_during_a_move_cancels_on_the_new_socket():
    options = {"reconnect_after_frames": 3, "refuse_resumes": 2}
    async with running(**options) as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            async for event in stream:
                if event.type == "reconnect":
                    break

        await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
        assert server.session().end_reason == "canceled"
        old, new = server.session().connections
        assert old.controls == []
        assert new.controls == [{"type": "cancel"}]
        assert new.frames == []


async def test_leaving_the_block_during_a_move_that_never_opens_stops_at_the_budget(monkeypatch):
    monkeypatch.setattr(stream_module, "_RESUME_SECONDS", 0.5)
    options = {"reconnect_after_frames": 3, "refuse_resumes": 1000}
    async with running(**options) as (server, client):
        session = await new_session(client)
        started = None
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))
            async for event in stream:
                if event.type == "reconnect":
                    started = time.monotonic()
                    break

        # It waited for the move to give up, and no longer than that.
        assert 0.1 < time.monotonic() - started < 2.0
        assert server.session().connections[0].controls == []


async def test_a_cancelled_close_still_releases_the_sockets():
    options = {"reconnect_after_frames": 3, "refuse_resumes": 1000}
    async with running(**options) as (server, client):
        session = await new_session(client)
        stream = client.translation.connect(session.client_secret)
        await stream.__aenter__()
        await stream.send(tagged_audio(5))
        async for event in stream:
            if event.type == "reconnect":
                break
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stream.close(), 0.05)

        await asyncio.wait_for(server.session().connections[0].ws.wait_closed(), LOOSE_TIMEOUT)
        assert stream._tasks == set()



async def test_leaving_the_block_early_cancels_the_session():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.send(tagged_audio(5))

        await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
        assert server.session().end_reason == "canceled"


async def test_an_exception_in_the_block_cancels_the_session_and_propagates():
    async with running() as (server, client):
        session = await new_session(client)
        with pytest.raises(KeyError):
            async with client.translation.connect(session.client_secret):
                raise KeyError("from the app")

        await asyncio.wait_for(server.session().done.wait(), LOOSE_TIMEOUT)
        assert server.session().end_reason == "canceled"


async def test_leaving_after_the_session_ended_sends_nothing_more():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            await collect(stream)

        assert server.session().end_reason == "finished"
        assert server.session().connections[0].controls == [{"type": "finish"}]


async def test_send_after_finish_is_refused_and_a_second_finish_does_nothing():
    async with running() as (server, client):
        session = await new_session(client)
        async with client.translation.connect(session.client_secret) as stream:
            await stream.finish()
            await stream.finish()
            with pytest.raises(SpekoTranslationConnectionError) as caught:
                await stream.send(tagged_audio(1))
            assert caught.value.code == "stream_closed"
            await collect(stream)

        assert server.session().connections[0].controls == [{"type": "finish"}]


async def test_using_the_stream_outside_its_block_is_a_clear_error():
    stream = AsyncTranslationStream("secret", stream_url="ws://127.0.0.1:9/stream")
    with pytest.raises(RuntimeError, match="async with"):
        await stream.send(bytes(960))


# --- Credentials --------------------------------------------------------------------


async def test_credentials_stay_out_of_logs_and_reprs(caplog):
    caplog.set_level(logging.DEBUG)
    async with running(reconnect_after_frames=5) as (_, client):
        session = await new_session(client)
        stream = client.translation.connect(session.client_secret)
        async with stream:
            await stream.send(tagged_audio(10))
            await stream.finish()
            events = await collect(stream)

    token = of_type(events, "reconnect")[0].resume_token
    assert session.client_secret not in caplog.text
    assert token not in caplog.text
    assert token not in repr(events)
    assert session.client_secret not in repr(stream)
