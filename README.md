# spekoai (Python SDK)

Official Python SDK for the [SpekoAI](https://speko.ai) voice AI gateway.

SpekoAI is a voice AI gateway that runs the STT → LLM → TTS pipeline so you
don't need to juggle separate API keys, credentials, and SDKs for each
provider. This SDK is the Python client for creating voice sessions, issuing
LiveKit tokens, and managing pipeline configuration.

## Installation

```bash
pip install spekoai
# or
uv add spekoai
```

## Quickstart

```python
import os
from spekoai import SpekoAI

client = SpekoAI(api_key=os.environ["SPEKOAI_API_KEY"])

session = client.sessions.create(
    pipeline={
        "stt": {"provider": "deepgram", "model": "nova-3"},
        "llm": {"provider": "anthropic", "model": "claude-opus-4-6"},
        "tts": {"provider": "elevenlabs", "voice_id": "rachel"},
    }
)

print("LiveKit URL:", session.livekit_url)
print("Token:", session.token)
```

## Documentation

Full API reference and guides: <https://docs.speko.dev/python-sdk>

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
