"""Scheduled callbacks — agent-requested follow-up calls."""

from __future__ import annotations

from typing import Any, Optional, Union

import httpx

from spekoai._http import dump_params, path_id, query_string, raise_for_status
from spekoai.models import (
    CancelScheduledCallbackParams,
    ScheduledCallback,
    ScheduledCallbackStatus,
)

CancelInput = Union[CancelScheduledCallbackParams, dict[str, Any], None]


def _callbacks_from(payload: Any) -> list[ScheduledCallback]:
    return [ScheduledCallback.model_validate(row) for row in payload.get("callbacks", [])]


class CallbacksResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(
        self,
        *,
        status: Optional[ScheduledCallbackStatus] = None,
        source_session_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ScheduledCallback]:
        resp = self._client.get(
            "/v1/callbacks",
            params=query_string(
                {
                    "status": status,
                    "source_session_id": source_session_id,
                    "limit": limit,
                }
            ),
        )
        raise_for_status(resp)
        return _callbacks_from(resp.json())

    def get(self, callback_id: str) -> ScheduledCallback:
        resp = self._client.get(f"/v1/callbacks/{path_id(callback_id)}")
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())

    def cancel(self, callback_id: str, params: CancelInput = None) -> ScheduledCallback:
        body = dump_params(params, CancelScheduledCallbackParams) if params else {}
        resp = self._client.post(f"/v1/callbacks/{path_id(callback_id)}/cancel", json=body)
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())

    def dispatch(self, callback_id: str) -> ScheduledCallback:
        """Dispatch a scheduled callback immediately instead of waiting for
        its scheduled time."""
        resp = self._client.post(f"/v1/callbacks/{path_id(callback_id)}/dispatch", json={})
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())


class AsyncCallbacksResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(
        self,
        *,
        status: Optional[ScheduledCallbackStatus] = None,
        source_session_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ScheduledCallback]:
        resp = await self._client.get(
            "/v1/callbacks",
            params=query_string(
                {
                    "status": status,
                    "source_session_id": source_session_id,
                    "limit": limit,
                }
            ),
        )
        raise_for_status(resp)
        return _callbacks_from(resp.json())

    async def get(self, callback_id: str) -> ScheduledCallback:
        resp = await self._client.get(f"/v1/callbacks/{path_id(callback_id)}")
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())

    async def cancel(self, callback_id: str, params: CancelInput = None) -> ScheduledCallback:
        body = dump_params(params, CancelScheduledCallbackParams) if params else {}
        resp = await self._client.post(f"/v1/callbacks/{path_id(callback_id)}/cancel", json=body)
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())

    async def dispatch(self, callback_id: str) -> ScheduledCallback:
        resp = await self._client.post(f"/v1/callbacks/{path_id(callback_id)}/dispatch", json={})
        raise_for_status(resp)
        return ScheduledCallback.model_validate(resp.json())
