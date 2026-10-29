"""Outbound phone calls via Speko's managed telephony gateway."""

from __future__ import annotations

from typing import Any, Union

import httpx

from spekoai._http import dump_params, raise_for_status
from spekoai.models import VoiceDialParams, VoiceDialResult

VoiceDialInput = Union[VoiceDialParams, dict[str, Any]]


class VoiceResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def dial(self, params: VoiceDialInput) -> VoiceDialResult:
        """Place an outbound call. The destination's phone rings; once they
        pick up, audio bridges to a Speko worker running the configured
        pipeline (STT→LLM→TTS) on the media transport.

        Example::

            result = speko.voice.dial({
                "to": "+12015551234",
                "intent": {"language": "en", "optimize_for": "latency"},
                "system_prompt": "You are a helpful Speko assistant.",
            })
            print("dialing:", result.session_id, result.status)
        """
        body = dump_params(params, VoiceDialParams)
        resp = self._client.post("/v1/sessions/phone", json=body)
        raise_for_status(resp)
        return VoiceDialResult.model_validate(resp.json())


class AsyncVoiceResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def dial(self, params: VoiceDialInput) -> VoiceDialResult:
        """Place an outbound call (async)."""
        body = dump_params(params, VoiceDialParams)
        resp = await self._client.post("/v1/sessions/phone", json=body)
        raise_for_status(resp)
        return VoiceDialResult.model_validate(resp.json())
