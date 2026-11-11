"""Read-only TTS voice catalog.

Use this to browse the curated list of voices per provider before calling
``speko.synthesize`` — handy for picking a voice id without reading each
provider's API docs.

ElevenLabs voices are account-scoped and are NOT returned by this endpoint;
the response's ``providers`` list sets ``voices_fetched_live=True`` on the
elevenlabs entry as a signal to fetch them directly from ElevenLabs at
runtime.
"""

from __future__ import annotations

from typing import Optional

import httpx

from spekoai._http import query_string, raise_for_status
from spekoai.models import VoicesListResult


class VoicesResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(self, *, provider: Optional[str] = None) -> VoicesListResult:
        """List catalog voices, optionally filtered to a single provider.

        ``provider`` accepts either the routing key (``cartesia``, ``xai``,
        ``alibaba``, ``openai``, ``inworld``, ``elevenlabs``) or the catalog
        suffix form (``xai-tts``, ``alibaba-tts``, ``openai-tts``).
        """
        resp = self._client.get(
            "/v1/voices", params=query_string({"provider": provider})
        )
        raise_for_status(resp)
        return VoicesListResult.model_validate(resp.json())


class AsyncVoicesResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(self, *, provider: Optional[str] = None) -> VoicesListResult:
        """List catalog voices (async)."""
        resp = await self._client.get(
            "/v1/voices", params=query_string({"provider": provider})
        )
        raise_for_status(resp)
        return VoicesListResult.model_validate(resp.json())
