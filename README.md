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

## Documentation

Full API reference and guides: <https://docs.speko.dev/sdk-python>

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
