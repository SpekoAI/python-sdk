"""Speko Python SDK — one API, every voice provider."""

from spekoai.client import AsyncSpeko, Speko
from spekoai.errors import SpekoApiError, SpekoAuthError, SpekoRateLimitError
from spekoai.models import (
    AllowedProviders,
    ChatMessage,
    ChatRole,
    CompleteResult,
    CompleteUsage,
    CreditLedgerEntry,
    CreditLedgerKind,
    CreditLedgerPage,
    KeySource,
    OptimizeFor,
    OrganizationBalance,
    PipelineConstraints,
    ProviderModality,
    RealtimeConnectParams,
    RealtimeProvider,
    RealtimeSessionInfo,
    RealtimeToolSpec,
    RoutingIntent,
    SynthesizeResult,
    TranscribeResult,
    UsageByProvider,
    UsageSummary,
    Vertical,
)
from spekoai.realtime import AsyncRealtimeSession

__all__ = [
    "Speko",
    "AsyncSpeko",
    "AsyncRealtimeSession",
    "SpekoApiError",
    "SpekoAuthError",
    "SpekoRateLimitError",
    "AllowedProviders",
    "ChatMessage",
    "ChatRole",
    "CompleteResult",
    "CompleteUsage",
    "CreditLedgerEntry",
    "CreditLedgerKind",
    "CreditLedgerPage",
    "KeySource",
    "OptimizeFor",
    "OrganizationBalance",
    "PipelineConstraints",
    "ProviderModality",
    "RealtimeConnectParams",
    "RealtimeProvider",
    "RealtimeSessionInfo",
    "RealtimeToolSpec",
    "RoutingIntent",
    "SynthesizeResult",
    "TranscribeResult",
    "UsageByProvider",
    "UsageSummary",
    "Vertical",
]

__version__ = "0.0.1"
