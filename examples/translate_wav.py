"""Translate a WAV file into a WAV file and a transcript.

    export SPEKO_API_KEY=...
    python examples/translate_wav.py speech.wav translated.wav transcript.txt --target ru

Audio streams in real time, so a one-minute file takes about a minute. A session is
charged to your organization's balance unless you pass --test. With --test, Speko
sends your own audio back with scripted captions, and it's free.

To try this without a Speko account, use the mock server from this repository:

    python tests/translation_mock.py --port 8787
    SPEKO_API_KEY=mock python examples/translate_wav.py speech.wav translated.wav \
        transcript.txt --test --base-url http://127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import array
import asyncio
import os
import sys
import wave

from spekoai import AsyncSpeko, SpekoTranslationError
from spekoai.resources.translation import DEFAULT_TRANSLATION_BASE_URL

OUTPUT_SAMPLE_RATE = 24000


def read_wav(path: str) -> tuple[bytes, int]:
    """Return the file's audio as 16-bit mono PCM, and its sample rate."""
    with wave.open(path, "rb") as source:
        if source.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected 16-bit PCM, got {source.getsampwidth() * 8}-bit")
        channels = source.getnchannels()
        rate = source.getframerate()
        pcm = source.readframes(source.getnframes())
    if channels == 1:
        return pcm, rate
    samples = array.array("h", pcm)
    mono = array.array(
        "h",
        (sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)),
    )
    return mono.tobytes(), rate


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(OUTPUT_SAMPLE_RATE)
        output.writeframes(pcm)


def write_transcript(path: str, items: dict[str, dict[str, str]]) -> None:
    """One block per phrase: what was said, if known, then its translation."""
    blocks = []
    for sides in items.values():
        lines = [sides[side] for side in ("source", "translated") if side in sides]
        blocks.append("\n".join(lines))
    with open(path, "w", encoding="utf-8") as output:
        output.write("\n\n".join(blocks) + "\n")


async def translate(args: argparse.Namespace) -> int:
    pcm, rate = read_wav(args.input)
    translated = bytearray()
    # item_id: {"source": text, "translated": text}. Each event replaces the earlier text.
    items: dict[str, dict[str, str]] = {}
    reason = None

    try:
        async with AsyncSpeko(
            api_key=os.environ["SPEKO_API_KEY"], translation_base_url=args.base_url
        ) as client:
            session = await client.translation.sessions.create(
                target_language=args.target, test=args.test
            )
            async with client.translation.connect(
                session.client_secret, sample_rate=rate
            ) as stream:
                await stream.send(pcm)
                await stream.finish()
                async for event in stream:
                    if event.type == "audio":
                        translated += event.pcm
                    elif event.type == "transcript.source":
                        items.setdefault(event.item_id, {})["source"] = event.text
                    elif event.type == "transcript.translated":
                        items.setdefault(event.item_id, {})["translated"] = event.text
                    elif event.type == "discontinuity":
                        print(
                            f"Frames {event.first_sequence}-{event.last_sequence} "
                            "may be missing from the translation.",
                            file=sys.stderr,
                        )
                    elif event.type == "error":
                        error = event.error
                        print(f"{error.code}: {error.message} {error.doc_url}", file=sys.stderr)
                    elif event.type == "ended":
                        reason = event.reason
    except SpekoTranslationError as error:
        print(f"{error.code}: {error.message} {error.doc_url or ''}".rstrip(), file=sys.stderr)
        return 1
    finally:
        # Whatever came back before a failure is still worth keeping.
        write_wav(args.output, bytes(translated))
        write_transcript(args.transcript, items)

    seconds = len(translated) / 2 / OUTPUT_SAMPLE_RATE
    print(f"Ended: {reason}. Wrote {seconds:.1f} s of audio and {len(items)} phrases.")
    return 0 if reason == "finished" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate a WAV file with Speko.")
    parser.add_argument("input", help="a 16-bit PCM WAV file, at any common sample rate")
    parser.add_argument("output", help="where to write the translated audio, as a WAV file")
    parser.add_argument("transcript", help="where to write the transcript, as text")
    parser.add_argument("--target", default="ru", help="the language to translate into")
    parser.add_argument("--test", action="store_true", help="use a free test session")
    parser.add_argument("--base-url", default=DEFAULT_TRANSLATION_BASE_URL)
    return asyncio.run(translate(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
