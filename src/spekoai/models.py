"""Pydantic models for the SpekoAI API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict


class SttConfig(BaseModel):
    provider: Literal["deepgram"] = "deepgram"
    model: Optional[str] = None
    language: Optional[str] = None
    keywords: Optional[list[str]] = None


class LlmConfig(BaseModel):
    provider: Literal["openai"] = "openai"
    model: str
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


class TtsConfig(BaseModel):
    provider: Literal["elevenlabs", "cartesia"]
    voice: str
    model: Optional[str] = None
    speed: Optional[float] = None


class PipelineConfig(BaseModel):
    stt: SttConfig
    llm: LlmConfig
    tts: TtsConfig


class Session(BaseModel):
    id: str
    status: Literal["created", "connecting", "active", "ended", "failed"]
    room_name: str
    token: str
    livekit_url: str
    created_at: str


class SessionDetail(BaseModel):
    id: str
    workspace_id: str
    status: Literal["created", "connecting", "active", "ended", "failed"]
    room_name: str
    pipeline_config: PipelineConfig
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    ended_at: Optional[datetime] = None


class UsageByProvider(BaseModel):
    provider: str
    type: Literal["stt", "llm", "tts"]
    metric: str
    quantity: int
    cost: float


class UsageSummary(BaseModel):
    total_sessions: int
    total_minutes: int
    total_cost: float
    breakdown: list[UsageByProvider]
