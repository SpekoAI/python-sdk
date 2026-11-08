"""Runs examples/translate_wav.py as a program, against the mock server."""

import asyncio
import math
import os
import runpy
import struct
import sys
import wave
from pathlib import Path

from .translation_mock import MockTranslationServer

EXAMPLE = Path(__file__).parent.parent / "examples" / "translate_wav.py"


def write_tone(path: Path, *, rate: int, channels: int, seconds: float) -> None:
    samples = []
    for index in range(int(rate * seconds)):
        value = int(10000 * math.sin(2 * math.pi * 440 * index / rate))
        samples.extend([value] * channels)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


async def run_example(server: MockTranslationServer, source: Path, *extra: str):
    env = {**os.environ, "SPEKO_API_KEY": "mock"}
    output = source.parent / "out.wav"
    transcript = source.parent / "out.txt"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXAMPLE),
        str(source),
        str(output),
        str(transcript),
        "--test",
        "--base-url",
        server.base_url,
        *extra,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    return process.returncode, stdout.decode(), stderr.decode(), output, transcript


async def test_a_stereo_16_khz_file_becomes_a_24_khz_mono_file_and_a_transcript(tmp_path):
    source = tmp_path / "in.wav"
    write_tone(source, rate=16000, channels=2, seconds=2.5)

    # Short delays, and a move in the middle, as a test session does every 60 seconds.
    async with MockTranslationServer(
        ready_delay=0.01, echo_delay=0.05, reconnect_every=None, reconnect_after_frames=60
    ) as server:
        code, stdout, stderr, output, transcript = await run_example(server, source)

        assert len(server.session().connections) >= 2

    assert code == 0, stderr
    assert "Ended: finished" in stdout
    with wave.open(str(output), "rb") as result:
        assert result.getframerate() == 24000
        assert result.getnchannels() == 1
        assert result.getsampwidth() == 2
        # 2.5 s is 125 frames, and the resampler's edges can add one.
        assert 125 * 480 <= result.getnframes() <= 126 * 480
        pcm = result.readframes(result.getnframes())
    # The tone survived: the middle of the file isn't silent.
    middle = struct.unpack(f"<{24000}h", pcm[48000:96000])
    assert max(middle) > 8000
    assert transcript.read_text(encoding="utf-8") == (
        "Good morning, everyone.\nДоброе утро всем.\n\n"
        "Let's start the meeting.\nДавайте начнём встречу.\n"
    )


def test_a_phrase_without_source_text_writes_only_its_translation(tmp_path):
    write_transcript = runpy.run_path(str(EXAMPLE))["write_transcript"]
    path = tmp_path / "out.txt"

    write_transcript(
        str(path), {"a": {"source": "Hi.", "translated": "Привет."}, "b": {"translated": "Да."}}
    )

    assert path.read_text(encoding="utf-8") == "Hi.\nПривет.\n\nДа.\n"


async def test_a_session_that_fails_exits_non_zero_and_keeps_what_arrived(tmp_path):
    source = tmp_path / "in.wav"
    write_tone(source, rate=24000, channels=1, seconds=1.0)

    async with MockTranslationServer(
        ready_delay=0.01, echo_delay=0.05, reconnect_every=None, trigger_delay=0.5
    ) as server:
        code, stdout, stderr, output, _ = await run_example(
            server, source, "--target", "x-error-internal"
        )

    assert code == 1
    assert "Ended: internal" in stdout
    assert "internal: " in stderr
    assert "https://docs.speko.dev/translation/errors#internal" in stderr
    with wave.open(str(output), "rb") as result:
        assert result.getnframes() > 0


async def test_a_refused_create_prints_the_code_and_doc_url(tmp_path):
    source = tmp_path / "in.wav"
    write_tone(source, rate=24000, channels=1, seconds=0.1)

    async with MockTranslationServer() as server:
        code, _, stderr, _, _ = await run_example(server, source, "--target", "xx")

    assert code == 1
    assert "unsupported_language: " in stderr
    assert "https://docs.speko.dev/translation/errors#unsupported_language" in stderr
