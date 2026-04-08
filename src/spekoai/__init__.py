"""SpekoAI Python SDK — Voice AI gateway client."""

from spekoai.client import SpekoAI, AsyncSpekoAI
from spekoai.models import (
    PipelineConfig,
    SttConfig,
    LlmConfig,
    TtsConfig,
    Session,
    SessionDetail,
    UsageSummary,
    UsageByProvider,
)
from spekoai.errors import SpekoApiError, SpekoAuthError, SpekoRateLimitError

__all__ = [
    "SpekoAI",
    "AsyncSpekoAI",
    "PipelineConfig",
    "SttConfig",
    "LlmConfig",
    "TtsConfig",
    "Session",
    "SessionDetail",
    "UsageSummary",
    "UsageByProvider",
    "SpekoApiError",
    "SpekoAuthError",
    "SpekoRateLimitError",
]

__version__ = "0.0.1"
