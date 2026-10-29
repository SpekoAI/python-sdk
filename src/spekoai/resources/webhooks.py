"""Organization-owned lifecycle webhook endpoints and their delivery
history."""

from __future__ import annotations

from typing import Any, Optional, Union

import httpx

from spekoai._http import dump_params, path_id, query_string, raise_for_status
from spekoai.models import (
    WebhookDeliveryDetail,
    WebhookDeliveryPage,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    WebhookEndpointInput,
    WebhookEndpointUpdate,
    WebhookEventType,
    WebhookRedeliverResult,
)

EndpointInput = Union[WebhookEndpointInput, dict[str, Any]]
EndpointUpdateInput = Union[WebhookEndpointUpdate, dict[str, Any]]


class WebhooksResource:
    deliveries: WebhookDeliveriesResource

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self.deliveries = WebhookDeliveriesResource(client)

    def list(self) -> list[WebhookEndpoint]:
        resp = self._client.get("/v1/webhooks")
        raise_for_status(resp)
        return [WebhookEndpoint.model_validate(row) for row in resp.json().get("data", [])]

    def create(self, params: EndpointInput) -> WebhookEndpoint:
        resp = self._client.post("/v1/webhooks", json=dump_params(params, WebhookEndpointInput))
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    def get(self, endpoint_id: str) -> WebhookEndpoint:
        resp = self._client.get(f"/v1/webhooks/{path_id(endpoint_id)}")
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    def update(self, endpoint_id: str, params: EndpointUpdateInput) -> WebhookEndpoint:
        resp = self._client.patch(
            f"/v1/webhooks/{path_id(endpoint_id)}",
            json=dump_params(params, WebhookEndpointUpdate),
        )
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    def delete(self, endpoint_id: str) -> None:
        resp = self._client.delete(f"/v1/webhooks/{path_id(endpoint_id)}")
        raise_for_status(resp)


class WebhookDeliveriesResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(
        self,
        *,
        endpoint_id: Optional[str] = None,
        event: Optional[WebhookEventType] = None,
        agent_id: Optional[str] = None,
        status: Optional[WebhookDeliveryStatus] = None,
        session_id: Optional[str] = None,
        event_id: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> WebhookDeliveryPage:
        resp = self._client.get(
            "/v1/webhook-deliveries",
            params=_delivery_query(
                endpoint_id,
                event,
                agent_id,
                status,
                session_id,
                event_id,
                from_date,
                to_date,
                cursor,
                limit,
            ),
        )
        raise_for_status(resp)
        return WebhookDeliveryPage.model_validate(resp.json())

    def get(self, delivery_id: str) -> WebhookDeliveryDetail:
        resp = self._client.get(f"/v1/webhook-deliveries/{path_id(delivery_id)}")
        raise_for_status(resp)
        return WebhookDeliveryDetail.model_validate(resp.json())

    def redeliver(self, delivery_id: str) -> WebhookRedeliverResult:
        resp = self._client.post(
            f"/v1/webhook-deliveries/{path_id(delivery_id)}/redeliver", json={}
        )
        raise_for_status(resp)
        return WebhookRedeliverResult.model_validate(resp.json())


class AsyncWebhooksResource:
    deliveries: AsyncWebhookDeliveriesResource

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.deliveries = AsyncWebhookDeliveriesResource(client)

    async def list(self) -> list[WebhookEndpoint]:
        resp = await self._client.get("/v1/webhooks")
        raise_for_status(resp)
        return [WebhookEndpoint.model_validate(row) for row in resp.json().get("data", [])]

    async def create(self, params: EndpointInput) -> WebhookEndpoint:
        resp = await self._client.post(
            "/v1/webhooks", json=dump_params(params, WebhookEndpointInput)
        )
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    async def get(self, endpoint_id: str) -> WebhookEndpoint:
        resp = await self._client.get(f"/v1/webhooks/{path_id(endpoint_id)}")
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    async def update(self, endpoint_id: str, params: EndpointUpdateInput) -> WebhookEndpoint:
        resp = await self._client.patch(
            f"/v1/webhooks/{path_id(endpoint_id)}",
            json=dump_params(params, WebhookEndpointUpdate),
        )
        raise_for_status(resp)
        return WebhookEndpoint.model_validate(resp.json())

    async def delete(self, endpoint_id: str) -> None:
        resp = await self._client.delete(f"/v1/webhooks/{path_id(endpoint_id)}")
        raise_for_status(resp)


class AsyncWebhookDeliveriesResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(
        self,
        *,
        endpoint_id: Optional[str] = None,
        event: Optional[WebhookEventType] = None,
        agent_id: Optional[str] = None,
        status: Optional[WebhookDeliveryStatus] = None,
        session_id: Optional[str] = None,
        event_id: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> WebhookDeliveryPage:
        resp = await self._client.get(
            "/v1/webhook-deliveries",
            params=_delivery_query(
                endpoint_id,
                event,
                agent_id,
                status,
                session_id,
                event_id,
                from_date,
                to_date,
                cursor,
                limit,
            ),
        )
        raise_for_status(resp)
        return WebhookDeliveryPage.model_validate(resp.json())

    async def get(self, delivery_id: str) -> WebhookDeliveryDetail:
        resp = await self._client.get(f"/v1/webhook-deliveries/{path_id(delivery_id)}")
        raise_for_status(resp)
        return WebhookDeliveryDetail.model_validate(resp.json())

    async def redeliver(self, delivery_id: str) -> WebhookRedeliverResult:
        resp = await self._client.post(
            f"/v1/webhook-deliveries/{path_id(delivery_id)}/redeliver", json={}
        )
        raise_for_status(resp)
        return WebhookRedeliverResult.model_validate(resp.json())


def _delivery_query(
    endpoint_id: Optional[str],
    event: Optional[str],
    agent_id: Optional[str],
    status: Optional[str],
    session_id: Optional[str],
    event_id: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    cursor: Optional[str],
    limit: Optional[int],
) -> dict[str, str]:
    return query_string(
        {
            "endpointId": endpoint_id,
            "event": event,
            "agentId": agent_id,
            "status": status,
            "sessionId": session_id,
            "eventId": event_id,
            "from": from_date,
            "to": to_date,
            "cursor": cursor,
            "limit": limit,
        }
    )
