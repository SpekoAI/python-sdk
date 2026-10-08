"""Call reads, lifecycle controls (web join / kill switch), and transfers."""

from __future__ import annotations

from typing import Any, Union

import httpx

from spekoai._http import dump_params, path_id, raise_for_status
from spekoai.models import (
    BlindTransferParams,
    CallDetail,
    CallEvent,
    CallRecording,
    CallReport,
    CallTransfer,
    CallTransferResponse,
    CancelWarmTransferParams,
    CompleteWarmTransferParams,
    EndCallResult,
    FinalizeCallReportParams,
    FinalizeCallReportResult,
    WarmTransferParams,
    WebJoinParams,
    WebJoinResult,
)

BlindTransferInput = Union[BlindTransferParams, dict[str, Any]]
WarmTransferInput = Union[WarmTransferParams, dict[str, Any]]
CompleteWarmInput = Union[CompleteWarmTransferParams, dict[str, Any], None]
CancelWarmInput = Union[CancelWarmTransferParams, dict[str, Any], None]
FinalizeInput = Union[FinalizeCallReportParams, dict[str, Any], None]
WebJoinInput = Union[WebJoinParams, dict[str, Any], None]


class CallsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(self, call_id: str) -> CallDetail:
        resp = self._client.get(f"/v1/calls/{path_id(call_id)}")
        raise_for_status(resp)
        return CallDetail.model_validate(resp.json())

    def events(self, call_id: str) -> list[CallEvent]:
        resp = self._client.get(f"/v1/calls/{path_id(call_id)}/events")
        raise_for_status(resp)
        return [CallEvent.model_validate(row) for row in resp.json().get("events", [])]

    def report(self, call_id: str) -> CallReport:
        resp = self._client.get(f"/v1/calls/{path_id(call_id)}/report")
        raise_for_status(resp)
        return CallReport.model_validate(resp.json())

    def finalize_report(
        self, call_id: str, params: FinalizeInput = None
    ) -> FinalizeCallReportResult:
        body = dump_params(params, FinalizeCallReportParams) if params else {}
        resp = self._client.post(f"/v1/calls/{path_id(call_id)}/report/finalize", json=body)
        raise_for_status(resp)
        return FinalizeCallReportResult.model_validate(resp.json())

    def recording(self, call_id: str) -> CallRecording:
        resp = self._client.get(f"/v1/calls/{path_id(call_id)}/recording")
        raise_for_status(resp)
        return CallRecording.model_validate(resp.json())

    def web_join(self, call_id: str, params: WebJoinInput = None) -> WebJoinResult:
        """Browser bridge-in: mint a short-lived token that joins THIS live
        call's room from a web client (pass ``token``/``url`` to
        ``@spekoai/client``). Once the browser publishes audio the platform
        bridges it to the phone leg and mutes the agent; when the browser
        leaves, the agent resumes.

        Mint at click time — the token is short-TTL and a 409 means the call
        is no longer live. Concurrent/repeat joins are allowed.
        """
        body = dump_params(params, WebJoinParams) if params else {}
        resp = self._client.post(f"/v1/calls/{path_id(call_id)}/web-join", json=body)
        raise_for_status(resp)
        return WebJoinResult.model_validate(resp.json())

    def end(self, call_id: str) -> EndCallResult:
        """End a live call now (kill switch): tears the room down, which
        hangs up every leg. Returns ``status='ending'`` once teardown was
        requested, or ``status='already_ended'`` if the call was over."""
        resp = self._client.post(f"/v1/calls/{path_id(call_id)}/end", json={})
        raise_for_status(resp)
        return EndCallResult.model_validate(resp.json())

    def blind_transfer(self, call_id: str, params: BlindTransferInput) -> CallTransfer:
        resp = self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/blind",
            json=dump_params(params, BlindTransferParams),
        )
        raise_for_status(resp)
        return CallTransfer.model_validate(resp.json())

    def warm_transfer(self, call_id: str, params: WarmTransferInput) -> CallTransferResponse:
        resp = self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/warm",
            json=dump_params(params, WarmTransferParams),
        )
        raise_for_status(resp)
        return CallTransferResponse.model_validate(resp.json())

    def complete_warm_transfer(
        self, call_id: str, transfer_id: str, params: CompleteWarmInput = None
    ) -> CallTransfer:
        body = dump_params(params, CompleteWarmTransferParams) if params else {}
        resp = self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/{path_id(transfer_id)}/complete",
            json=body,
        )
        raise_for_status(resp)
        return CallTransfer.model_validate(resp.json())

    def cancel_warm_transfer(
        self, call_id: str, transfer_id: str, params: CancelWarmInput = None
    ) -> CallTransferResponse:
        body = dump_params(params, CancelWarmTransferParams) if params else {}
        resp = self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/{path_id(transfer_id)}/cancel",
            json=body,
        )
        raise_for_status(resp)
        return CallTransferResponse.model_validate(resp.json())


class AsyncCallsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(self, call_id: str) -> CallDetail:
        resp = await self._client.get(f"/v1/calls/{path_id(call_id)}")
        raise_for_status(resp)
        return CallDetail.model_validate(resp.json())

    async def events(self, call_id: str) -> list[CallEvent]:
        resp = await self._client.get(f"/v1/calls/{path_id(call_id)}/events")
        raise_for_status(resp)
        return [CallEvent.model_validate(row) for row in resp.json().get("events", [])]

    async def report(self, call_id: str) -> CallReport:
        resp = await self._client.get(f"/v1/calls/{path_id(call_id)}/report")
        raise_for_status(resp)
        return CallReport.model_validate(resp.json())

    async def finalize_report(
        self, call_id: str, params: FinalizeInput = None
    ) -> FinalizeCallReportResult:
        body = dump_params(params, FinalizeCallReportParams) if params else {}
        resp = await self._client.post(f"/v1/calls/{path_id(call_id)}/report/finalize", json=body)
        raise_for_status(resp)
        return FinalizeCallReportResult.model_validate(resp.json())

    async def recording(self, call_id: str) -> CallRecording:
        resp = await self._client.get(f"/v1/calls/{path_id(call_id)}/recording")
        raise_for_status(resp)
        return CallRecording.model_validate(resp.json())

    async def web_join(self, call_id: str, params: WebJoinInput = None) -> WebJoinResult:
        body = dump_params(params, WebJoinParams) if params else {}
        resp = await self._client.post(f"/v1/calls/{path_id(call_id)}/web-join", json=body)
        raise_for_status(resp)
        return WebJoinResult.model_validate(resp.json())

    async def end(self, call_id: str) -> EndCallResult:
        resp = await self._client.post(f"/v1/calls/{path_id(call_id)}/end", json={})
        raise_for_status(resp)
        return EndCallResult.model_validate(resp.json())

    async def blind_transfer(self, call_id: str, params: BlindTransferInput) -> CallTransfer:
        resp = await self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/blind",
            json=dump_params(params, BlindTransferParams),
        )
        raise_for_status(resp)
        return CallTransfer.model_validate(resp.json())

    async def warm_transfer(
        self, call_id: str, params: WarmTransferInput
    ) -> CallTransferResponse:
        resp = await self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/warm",
            json=dump_params(params, WarmTransferParams),
        )
        raise_for_status(resp)
        return CallTransferResponse.model_validate(resp.json())

    async def complete_warm_transfer(
        self, call_id: str, transfer_id: str, params: CompleteWarmInput = None
    ) -> CallTransfer:
        body = dump_params(params, CompleteWarmTransferParams) if params else {}
        resp = await self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/{path_id(transfer_id)}/complete",
            json=body,
        )
        raise_for_status(resp)
        return CallTransfer.model_validate(resp.json())

    async def cancel_warm_transfer(
        self, call_id: str, transfer_id: str, params: CancelWarmInput = None
    ) -> CallTransferResponse:
        body = dump_params(params, CancelWarmTransferParams) if params else {}
        resp = await self._client.post(
            f"/v1/calls/{path_id(call_id)}/transfers/{path_id(transfer_id)}/cancel",
            json=body,
        )
        raise_for_status(resp)
        return CallTransferResponse.model_validate(resp.json())
