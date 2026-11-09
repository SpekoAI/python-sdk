# spekoai (Python SDK)

Official Python SDK for [Speko](https://speko.ai) — one API, every voice provider.

Speko is a voice AI gateway that benchmarks every STT, LLM, and TTS provider
across languages, then routes each request to the best provider in real
time. Failover is handled. You write one integration; Speko picks the
right provider for every call.

## Installation

```bash
pip install spekoai
# or
uv add spekoai
```

## Quickstart

```python
import os
from pathlib import Path

from spekoai import Speko

speko = Speko(api_key=os.environ["SPEKO_API_KEY"])

# Transcribe — best STT provider auto-routed for your language
audio = Path("call.wav").read_bytes()
result = speko.transcribe(
    audio,
    language="es-MX",
    region="us-east4",  # optional — rank streaming providers in this region
)
print(result.text, result.provider, result.confidence)

# Synthesize — best TTS provider auto-routed
speech = speko.synthesize(
    "Hello world",
    language="en",
)
ext = "mp3" if "mpeg" in speech.content_type else "pcm"
Path(f"out.{ext}").write_bytes(speech.audio)

# Complete — best LLM provider auto-routed
completion = speko.complete(
    messages=[{"role": "user", "content": "Hi!"}],
    intent={"language": "en"},
)
print(completion.text)
```

### Async

```python
import asyncio
from spekoai import AsyncSpeko

async def main():
    async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
        completion = await speko.complete(
            messages=[{"role": "user", "content": "Hi!"}],
            intent={"language": "en"},
        )
        print(completion.text)

asyncio.run(main())
```

### Streaming

Every primitive has a streaming variant on both clients:

```python
# LLM deltas as they arrive
for event in speko.complete_stream(
    messages=[{"role": "user", "content": "Hi!"}],
    intent={"language": "en"},
):
    if event.type == "delta":
        print(event.text, end="", flush=True)

# TTS audio chunks as the provider renders them
with speko.synthesize_stream("Hello world", language="en") as stream:
    print(stream.provider, stream.content_type)
    for chunk in stream:
        play(chunk)

# STT interim transcripts (transcribe_stream) work the same way.
```

## Agents, phone calls, and the rest of the platform

The full REST surface is exposed as resource namespaces (same layout as the
TypeScript SDK):

```python
# Persisted agents + tools + knowledge bases
agent = speko.agents.create({
    "name": "Support Bot",
    "system_prompt": "You are a helpful support agent for Acme.",
    "intent": {"language": "en", "optimize_for": "latency"},
})
kb = speko.knowledge_bases.list(agent_id=agent.id)[0]  # auto-provisioned
doc = speko.knowledge_bases.upload_document(kb.id, {
    "filename": "faq.md",
    "content_type": "text/markdown",
    "data": Path("faq.md").read_bytes(),
})
speko.knowledge_bases.poll_document_ready(kb.id, doc.id)

# Outbound phone call
call = speko.voice.dial({"to": "+12015551234", "agent_id": agent.id})

# Observe the call live (SSE with automatic reconnects)
for ev in speko.sessions.stream(call.session_id):
    if ev.type == "transcript":
        print(ev.turn.source, ev.turn.text)
    elif ev.type == "end":
        break

# Post-call artifacts
report = speko.calls.report(call.session_id)
recording = speko.calls.recording(call.session_id)
```

Also available: `speko.phone_numbers` (search / buy / SIP import / KYB),
`speko.callbacks` (scheduled callbacks), `speko.webhooks` (workspace webhook
endpoints + delivery logs), `speko.voices` (TTS voice catalog),
`speko.usage`, and `speko.credits`. Every namespace has an identical async
mirror on `AsyncSpeko`; speech-to-speech realtime sessions are async-only via
`await speko.connect_realtime(...)`.

Close realtime sessions before leaving `async with AsyncSpeko(...)`. Client
shutdown gives pending provider cleanup and terminal reports up to five seconds
to finish. Explicit shutdown can use `await speko.close(realtime_timeout=5.0)`.
Session close never waits for telemetry. Report delivery is best effort and is
not guaranteed after the deadline or event-loop termination.

## Live translation

Translate a live audio stream, with captions. Your server creates a session, and the stream helper handles the frame format, resampling to 24 kHz, continuous silence, and moving to a new connection when Speko asks. The helper is async-only; `sessions` also works on the sync client.

```python
import asyncio
import os

from spekoai import AsyncSpeko

pcm_bytes = bytes(32000)  # One second of silence. Use your own 16-bit mono PCM at 16 kHz.


def play(pcm):
    pass  # 20 ms of 24 kHz mono PCM. Send it to a speaker or a file.


def show(item_id, text):
    print(item_id, text)


async def main():
    async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as client:
        session = await client.translation.sessions.create(target_language="ru", test=True)

        async with client.translation.connect(session.client_secret, sample_rate=16000) as stream:
            await stream.send(pcm_bytes)  # Any chunk size. It goes out in real time.
            await stream.finish()
            async for event in stream:
                if event.type == "audio":
                    play(event.pcm)
                elif event.type == "transcript.translated":
                    show(event.item_id, event.text)
                elif event.type == "error":
                    print(event.error.code, event.error.doc_url)


asyncio.run(main())
```

`test=True` makes this a free session that sends your audio back with scripted captions. Remove it to translate for real. [`examples/translate_wav.py`](examples/translate_wav.py) translates a WAV file to a WAV file and a transcript.

## Documentation

Full API reference and guides: <https://docs.speko.dev/sdk-python/overview>

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
