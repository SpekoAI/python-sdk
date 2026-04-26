# Changelog

All notable changes to `spekoai` (Python SDK) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-26

### Removed

- **BREAKING**: `vertical` field removed from `RoutingIntent` and from the
  `transcribe()` / `synthesize()` keyword arguments. The router now ranks
  on `(language, optimize_for)` only. `Vertical` literal is no longer
  exported from `spekoai`. Callers passing `vertical=...` will hit a
  `TypeError` at the call site.

## [0.0.1] - 2026-04-18

### Added

- Initial release. Surface mirrors `@spekoai/sdk`:
  `speko.transcribe(audio, ...)`, `speko.synthesize(text, ...)`,
  `speko.complete(messages=..., intent=...)`, `speko.usage.get(...)`.
- Sync `Speko` and async `AsyncSpeko` clients.
- Pydantic v2 models with camelCase wire aliases and snake_case Python
  fields: `RoutingIntent`, `PipelineConstraints`, `AllowedProviders`,
  `ChatMessage`, `TranscribeResult`, `SynthesizeResult`, `CompleteResult`,
  `UsageSummary`, `UsageByProvider`, plus `OptimizeFor` literals.
- Typed errors: `SpekoApiError`, `SpekoAuthError`, `SpekoRateLimitError`.
