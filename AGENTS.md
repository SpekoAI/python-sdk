# AGENTS.md

Guidance for AI coding agents working with `spekoai` (Python SDK).

## What this is

`spekoai` is the official Python SDK for [Speko](https://speko.ai), an OpenRouter-style gateway for voice AI: one API that routes STT, LLM, and TTS requests across every major voice provider (10+ languages), with benchmark-driven provider selection and automatic failover. Full voice stack: speech-to-text, text-to-speech, and LLM completion behind a single typed client. Sync (`Speko`) and async (`AsyncSpeko`) clients are included; the package ships `py.typed`.

## Install

```bash
pip install spekoai
```

## Quickstart

```python
import os

from spekoai import Speko

speko = Speko(api_key=os.environ["SPEKO_API_KEY"])
result = speko.transcribe(audio_bytes, language="es-MX")
speech = speko.synthesize("Hello world", language="en")
completion = speko.complete(messages=[{"role": "user", "content": "Hi!"}], intent={"language": "en"})
```

Async usage mirrors the sync API via `AsyncSpeko` (see README).

## Auth

Sign up at [platform.speko.dev](https://platform.speko.dev), mint a key at [platform.speko.dev/api-keys](https://platform.speko.dev/api-keys), and set `SPEKO_API_KEY`. New accounts get starter credit with no card required.

## Agent resources

- Machine-readable docs index: <https://docs.speko.dev/llms.txt> (full corpus: <https://docs.speko.dev/llms-full.txt>)
- Hosted MCP server: `https://mcp.speko.ai/mcp` (OAuth or API key). One-command setup for Claude Code, Codex, OpenCode, and Cursor: `npx @spekoai/mcp@latest init`
- Python SDK docs: <https://docs.speko.dev/sdk-python/overview>
- Full documentation: <https://docs.speko.dev>

## Repo notes

This repository is a read-mostly mirror of `packages/sdk-python` in the SpekoAI platform monorepo. Issues and PRs are welcome here; merged changes are synced back upstream automatically.
