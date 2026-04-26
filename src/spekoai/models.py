"""Pydantic models and literal types for the Speko Python SDK.

All models serialize/validate using camelCase aliases to match the wire
protocol, while exposing snake_case attributes on the Python side.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

OptimizeFor = Literal["balanced", "accuracy", "latency", "cost"]
ProviderModality = Literal["stt", "llm", "tts"]
ChatRole = Literal["system", "user", "assistant"]
KeySource = Literal["BYOK", "MANAGED"]
CreditLedgerKind = Literal["grant", "debit", "topup", "refund", "adjustment"]


class _SpekoModel(BaseModel):
    """Base model: camelCase wire aliases, snake_case Python fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class AllowedProviders(_SpekoModel):
    stt: Optional[list[str]] = None
    llm: Optional[list[str]] = None
    tts: Optional[list[str]] = None


class PipelineConstraints(_SpekoModel):
    """Optional allowlists layered on top of RoutingIntent.

    The router still ranks candidates by benchmark score — but if an
    ``allowed_providers`` list is set for a modality, only that subset
    is considered.
    """

    allowed_providers: Optional[AllowedProviders] = None


class RoutingIntent(_SpekoModel):
    """Routing signal for the Speko router.

    - ``language``: BCP-47 tag, e.g. ``"en"`` or ``"es-MX"``.
    - ``optimize_for``: preset that biases the weighted score.
    """

    language: str
    optimize_for: Optional[OptimizeFor] = None


class ChatMessage(_SpekoModel):
    role: ChatRole
    content: str


# --- Transcribe -------------------------------------------------------------


class TranscribeResult(_SpekoModel):
    text: str
    provider: str
    model: str
    confidence: Optional[float] = None
    failover_count: int = 0
    scores_run_id: Optional[str] = None


# --- Synthesize -------------------------------------------------------------


class SynthesizeResult(_SpekoModel):
    """Result of a synthesize call.

    ``audio`` holds the raw bytes. The format depends on the chosen
    provider — check ``content_type`` (ElevenLabs returns
    ``audio/mpeg``; Cartesia returns ``audio/pcm;rate=24000``).
    """

    audio: bytes
    content_type: str
    provider: str
    model: str
    failover_count: int = 0
    scores_run_id: Optional[str] = None


# --- Complete (LLM) ---------------------------------------------------------


class CompleteUsage(_SpekoModel):
    prompt_tokens: int
    completion_tokens: int


class CompleteResult(_SpekoModel):
    text: str
    provider: str
    model: str
    usage: CompleteUsage
    failover_count: int = 0
    scores_run_id: Optional[str] = None


# --- Usage ------------------------------------------------------------------


class UsageByProvider(_SpekoModel):
    provider: str
    type: ProviderModality
    metric: str
    key_source: KeySource
    quantity: float
    cost: float


class UsageSummary(_SpekoModel):
    total_sessions: int
    total_minutes: float
    total_cost: float
    breakdown: list[UsageByProvider]
    # bigint as string over the wire; exposed as Python str for lossless
    # round-trips. Convert with `int(summary.balance_micro_usd)` when math
    # is needed.
    balance_micro_usd: str
    balance_usd: float


# --- Credits ----------------------------------------------------------------


class OrganizationBalance(_SpekoModel):
    balance_micro_usd: str
    balance_usd: float
    updated_at: str


class CreditLedgerEntry(_SpekoModel):
    id: str
    kind: CreditLedgerKind
    # Signed — positive for grants/topups/refunds, negative for debits.
    # Kept as string so >2**53 values survive JSON.
    amount_micro_usd: str
    metric: Optional[str] = None
    provider: Optional[str] = None
    session_id: Optional[str] = None
    created_at: str


class CreditLedgerPage(_SpekoModel):
    entries: list[CreditLedgerEntry]
    next_cursor: Optional[str] = None


# --- Realtime (S2S) ---------------------------------------------------------

RealtimeProvider = Literal["openai", "google", "xai"]


class RealtimeToolSpec(_SpekoModel):
    name: str
    description: str
    parameters: dict[str, object]


class RealtimeConnectParams(_SpekoModel):
    """Parameters for opening an S2S realtime session.

    Unlike cascade sessions, realtime bypasses LiveKit: the server proxies
    the client WebSocket directly to the provider (OpenAI Realtime, Gemini
    Live, xAI Grok Voice) so time-to-first-audio stays under ~300 ms.
    """

    provider: RealtimeProvider
    model: str
    voice: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    input_sample_rate: Optional[Literal[16000, 24000]] = None
    output_sample_rate: Optional[Literal[16000, 24000]] = None
    tools: Optional[list[RealtimeToolSpec]] = None
    metadata: Optional[dict[str, object]] = None
    # Max session duration in seconds. Server-capped at 1800 (30 min).
    ttl_seconds: Optional[int] = None


class RealtimeSessionInfo(_SpekoModel):
    """Raw response from POST /v1/sessions when mode == 's2s'."""

    mode: Literal["s2s"]
    session_id: str
    ws_url: str
    ws_token: str
    expires_at: str
