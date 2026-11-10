"""Frame format and input conditioning, with no sockets involved."""

import math
import random
import struct

import pytest

from spekoai._translation_audio import (
    FRAME_BYTES,
    PCM_BYTES_PER_FRAME,
    SILENCE,
    PcmFramer,
    decode_frame,
    encode_frame,
)

COMMON_RATES = [8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000]
# libswresample's filter delay moves at most this many samples in or out of a second.
LENGTH_TOLERANCE_SAMPLES = 128


def tone(frequency: float, rate: int, seconds: float = 1.0, amplitude: int = 12000) -> bytes:
    count = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / rate)))
        for i in range(count)
    )


def samples(pcm: bytes) -> list[int]:
    return [value for (value,) in struct.iter_unpack("<h", pcm)]


def rms(values: list[int]) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values))


def zero_crossings(values: list[int]) -> int:
    return sum(1 for a, b in zip(values, values[1:]) if a < 0 <= b or b < 0 <= a)


def frame_all(framer: PcmFramer, pcm: bytes, chunk_size: int) -> list[bytes]:
    frames: list[bytes] = []
    for start in range(0, len(pcm), chunk_size):
        frames.extend(framer.push(pcm[start : start + chunk_size]))
    frames.extend(framer.flush())
    return frames


def test_constants_match_the_spec():
    assert FRAME_BYTES == 976
    assert PCM_BYTES_PER_FRAME == 960
    assert SILENCE == bytes(960)


def test_encode_lays_out_the_header_as_two_little_endian_uint64():
    pcm = bytes(range(256)) * 3 + bytes(192)

    frame = encode_frame(1500, pcm)

    assert len(frame) == 976
    assert frame[:8] == (1500).to_bytes(8, "little")
    assert frame[8:16] == (1500 * 480).to_bytes(8, "little")
    assert frame[16:] == pcm


def test_encode_handles_sequence_zero_and_large_sequences():
    assert encode_frame(0, SILENCE)[:16] == bytes(16)
    # Four hours of frames fits, and the offset is computed, not truncated to 32 bits.
    big = 2**40
    assert encode_frame(big, SILENCE)[8:16] == (big * 480).to_bytes(8, "little")


@pytest.mark.parametrize("size", [0, 959, 961, 976])
def test_encode_refuses_a_payload_of_any_other_size(size):
    with pytest.raises(ValueError):
        encode_frame(0, bytes(size))


def test_decode_reads_what_encode_wrote():
    pcm = bytes(range(240)) * 4

    assert decode_frame(encode_frame(7, pcm)) == (7, 3360, pcm)


def test_decode_reads_the_offset_from_the_wire_rather_than_computing_it():
    frame = (3).to_bytes(8, "little") + (99).to_bytes(8, "little") + SILENCE

    assert decode_frame(frame) == (3, 99, SILENCE)


@pytest.mark.parametrize("size", [0, 16, 975, 977, 1952])
def test_decode_refuses_a_message_of_any_other_size(size):
    with pytest.raises(ValueError):
        decode_frame(bytes(size))


def test_decode_returns_bytes_from_a_bytes_like_message():
    pcm = bytes(960)
    frame = bytearray(encode_frame(1, pcm))

    assert decode_frame(frame)[2] == pcm
    assert isinstance(decode_frame(frame)[2], bytes)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 959, 960, 961, 5000])
def test_at_24_khz_the_input_comes_out_byte_for_byte_whatever_the_chunking(chunk_size):
    rng = random.Random(chunk_size)
    # An even number of bytes, not a multiple of a frame.
    pcm = rng.randbytes(960 * 7 + 400)

    frames = frame_all(PcmFramer(24000), pcm, chunk_size)

    assert all(len(frame) == 960 for frame in frames)
    assert b"".join(frames) == pcm + bytes(960 - 400)


def test_a_partial_frame_waits_for_more_audio():
    framer = PcmFramer(24000)

    assert framer.push(bytes(958)) == []
    frames = framer.push(b"\x01\x02\x03\x04")

    assert frames == [bytes(958) + b"\x01\x02"]
    assert framer.flush() == [b"\x03\x04" + bytes(958)]


def test_an_odd_byte_waits_for_the_rest_of_its_sample():
    framer = PcmFramer(24000)

    assert framer.push(b"\x01") == []
    assert framer.push(b"\x02" + bytes(958)) == [b"\x01\x02" + bytes(958)]
    assert framer.push(b"\x03") == []
    assert framer.push(b"\x04") == []
    assert framer.flush() == [b"\x03\x04" + bytes(958)]


def test_flush_on_a_frame_boundary_adds_no_silent_frame():
    framer = PcmFramer(24000)

    assert len(framer.push(bytes(1920))) == 2
    assert framer.flush() == []


def test_flush_with_nothing_sent_gives_nothing():
    assert PcmFramer(24000).flush() == []
    assert PcmFramer(16000).flush() == []


@pytest.mark.parametrize("bad_rate", [0, -24000, 1, 7999, 192001])
def test_a_rate_outside_what_audio_uses_is_refused(bad_rate):
    with pytest.raises(ValueError):
        PcmFramer(bad_rate)


@pytest.mark.parametrize("rate", [8000, 192000])
def test_the_rates_at_the_edges_of_the_range_are_accepted(rate):
    framer = PcmFramer(rate)
    assert framer.push(bytes(rate // 25)) is not None


@pytest.mark.parametrize("rate", COMMON_RATES)
def test_one_second_at_a_common_rate_becomes_one_second_at_24_khz(rate):
    frames = frame_all(PcmFramer(rate), tone(1000, rate), chunk_size=4096)

    output = samples(b"".join(frames))
    assert all(len(frame) == 960 for frame in frames)
    # Padding of the last frame can add up to 479 samples.
    assert 24000 - LENGTH_TOLERANCE_SAMPLES <= len(output) <= 24000 + 480 + LENGTH_TOLERANCE_SAMPLES


@pytest.mark.parametrize("rate", COMMON_RATES)
def test_resampling_keeps_the_pitch_and_the_level(rate):
    frames = frame_all(PcmFramer(rate), tone(1000, rate), chunk_size=4096)

    # The middle of the signal, clear of the filter's edges and the padding.
    output = samples(b"".join(frames))[2400:21600]
    seconds = len(output) / 24000
    assert zero_crossings(output) == pytest.approx(2000 * seconds, rel=0.01)
    assert rms(output) == pytest.approx(12000 / math.sqrt(2), rel=0.05)


@pytest.mark.parametrize("rate", [32000, 44100, 48000])
def test_downsampling_filters_what_24_khz_cannot_carry(rate):
    # 15 kHz is above the new 12 kHz limit. Without a filter it would alias to 9 kHz.
    frames = frame_all(PcmFramer(rate), tone(15000, rate), chunk_size=4096)

    output = samples(b"".join(frames))[2400:21600]
    assert rms(output) < 0.02 * (12000 / math.sqrt(2))


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000])
def test_resampling_does_not_depend_on_how_the_audio_was_chunked(rate):
    pcm = tone(440, rate)
    whole = samples(b"".join(frame_all(PcmFramer(rate), pcm, chunk_size=len(pcm))))

    rng = random.Random(rate)
    framer = PcmFramer(rate)
    frames: list[bytes] = []
    position = 0
    while position < len(pcm):
        size = rng.randrange(1, 3000)
        frames.extend(framer.push(pcm[position : position + size]))
        position += size
    frames.extend(framer.flush())
    chunked = samples(b"".join(frames))

    # The filter state carries across chunks, so the signal has no glitches at the seams.
    assert abs(len(chunked) - len(whole)) <= 480
    compared = min(len(chunked), len(whole)) - 960
    assert max(abs(a - b) for a, b in zip(chunked[:compared], whole[:compared])) <= 2


def test_flush_returns_the_tail_held_in_the_resampler():
    framer = PcmFramer(48000)

    # 40 ms is exactly two frames at 24 kHz, and the resampler holds some of it back.
    frames = framer.push(struct.pack("<h", 9000) * 1920)
    frames.extend(framer.flush())

    # The end is the audio's own tail, not silence added as padding.
    assert len(frames) == 2
    assert samples(frames[-1])[-1] == pytest.approx(9000, abs=50)
