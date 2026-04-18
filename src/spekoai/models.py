"""Pydantic models and literal types for the Speko Python SDK.

All models serialize/validate using camelCase aliases to match the wire
protocol, while exposing snake_case attributes on the Python side.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Vertical = Literal["general", "healthcare", "finance", "legal"]
OptimizeFor = Literal["balanced", "accuracy", "latency", "cost"]
ProviderModality = Literal["stt", "llm", "tts"]
ChatRole = Literal["system", "user", "assistant"]


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
    - ``vertical``: domain bucket used by the benchmark tables.
    - ``optimize_for``: preset that biases the weighted score.
    """

    language: str
    vertical: Vertical
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
    quantity: float
    cost: float


class UsageSummary(_SpekoModel):
    total_sessions: int
    total_minutes: float
    total_cost: float
    breakdown: list[UsageByProvider]
