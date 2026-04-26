# spekoai (Python SDK) — skill sheet

Dense reference for an LLM writing Python against the Speko server SDK.
The wire format and routing are identical to the TypeScript SDK — only
the naming convention differs (snake_case on the Python side, camelCase
on the wire).

## When to use

Pick `spekoai` when you're running Python on the **server side** — a
FastAPI/Flask backend, a batch transcription worker, a data pipeline, a
Jupyter notebook. Both sync and async clients are provided. Python has
no browser client and no LiveKit-Agents adapter in this repo today; for
those, use TypeScript.

## Install

```bash
pip install spekoai
# or: uv add spekoai
```

Python 3.10+. Depends on `httpx>=0.27` and `pydantic>=2`.

## Environment

- `SPEKO_API_KEY` — from `https://dashboard.speko.ai/api-keys`.
- `SPEKO_BASE_URL` — optional, default `https://api.speko.ai`.

## Minimal snippet (sync)

```python
import os
from pathlib import Path
from spekoai import Speko

speko = Speko(api_key=os.environ["SPEKO_API_KEY"])

audio = Path("call.wav").read_bytes()
stt = speko.transcribe(audio, language="es-MX")
print(stt.text, stt.provider, stt.confidence)

reply = speko.complete(
    messages=[{"role": "user", "content": "Hi!"}],
    intent={"language": "en"},
)
print(reply.text)

speech = speko.synthesize("Hello world", language="en")
ext = "mp3" if "mpeg" in speech.content_type else "pcm"
Path(f"out.{ext}").write_bytes(speech.audio)

# Context-manager form closes the underlying httpx client cleanly:
with Speko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
    ...
```

## Minimal snippet (async)

```python
import asyncio, os
from spekoai import AsyncSpeko

async def main():
    async with AsyncSpeko(api_key=os.environ["SPEKO_API_KEY"]) as speko:
        result = await speko.complete(
            messages=[{"role": "user", "content": "Hi!"}],
            intent={"language": "en"},
        )
        print(result.text)

asyncio.run(main())
```

## Public surface

- `Speko(api_key=..., base_url=..., timeout=30.0)` — sync.
- `AsyncSpeko(api_key=..., base_url=..., timeout=30.0)` — async,
  use as `async with`.
- Methods (both classes):
  - `transcribe(audio: bytes, *, language, optimize_for=None,
     content_type="audio/wav", constraints=None) -> TranscribeResult`
  - `synthesize(text, *, language, optimize_for=None,
     voice=None, speed=None, constraints=None) -> SynthesizeResult`
  - `complete(*, messages, intent, system_prompt=None, temperature=None,
     max_tokens=None, constraints=None) -> CompleteResult`
  - `usage.get(*, from_date=None, to_date=None) -> UsageSummary`
- Errors: `SpekoApiError`, `SpekoAuthError`, `SpekoRateLimitError`.
- Models: `RoutingIntent`, `PipelineConstraints`, `AllowedProviders`,
  `ChatMessage`, `TranscribeResult`, `SynthesizeResult`, `CompleteResult`,
  `CompleteUsage`, `UsageSummary`, `UsageByProvider`. Literals:
  `OptimizeFor`, `ProviderModality`, `ChatRole`.

## Concepts

- **Pydantic models with camelCase aliases.** Python attributes are
  snake_case (`result.content_type`, `usage.total_minutes`); the wire is
  camelCase. You don't need to care unless you serialize manually.
- **Keyword-only args.** Every method uses `*` to force kwargs — this
  prevents callers from accidentally swapping positional fields.
- **Two ways to pass intent to `complete()`**: a dict
  (`intent={"language": "en"}`) or a `RoutingIntent` instance. Dicts
  are simpler; the model is handy when you want static typing end to
  end.

## Common gotchas

- **`transcribe()` requires `bytes`**, not a file-like object. Read the
  file first (`Path(...).read_bytes()`).
- **`synthesize()` audio format varies by provider.** Check
  `result.content_type` — don't assume MP3.
- **Sync client leaks sockets if you don't close it.** Use
  `with Speko(...) as speko:` or call `speko.close()`. Async uses
  `async with`.
- **`RoutingIntent.optimize_for` is keyword-only on the wire**; it's
  already keyword-only at the Python level.
- **Rate-limit `Retry-After`** — `SpekoRateLimitError.retry_after` gives
  you the seconds to wait.
- **No streaming** in v1. `/v1/complete` returns the full completion; if
  you need streaming, watch the SDK roadmap.

## See also

- README: `spekoai://docs/sdk-python-readme`
- TypeScript equivalent: `spekoai://docs/sdk-skills`
- Usage tool (forwards the caller's OAuth token): this MCP's
  `get_usage_summary` tool uses this SDK under the hood.
