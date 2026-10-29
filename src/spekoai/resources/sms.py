"""Durable 10DLC SMS messages, conversations, consent, and live events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Optional, Union

import httpx

from spekoai._http import (
    aiter_sse_with_id,
    araise_for_status_streamed,
    dump_params,
    iter_sse_with_id,
    path_id,
    query_string,
    raise_for_status,
    raise_for_status_streamed,
)
from spekoai.models import (
    SmsBatch,
    SmsBatchCreateParams,
    SmsConsent,
    SmsConsentInput,
    SmsConversation,
    SmsConversationNote,
    SmsConversationSendParams,
    SmsConversationUpdate,
    SmsMessage,
    SmsSendParams,
    SmsSettings,
    SmsSettingsUpdate,
    SmsStreamEvent,
    SmsSuppression,
)

SendInput = Union[SmsSendParams, dict[str, Any]]
BatchInput = Union[SmsBatchCreateParams, dict[str, Any]]
ConversationSendInput = Union[SmsConversationSendParams, dict[str, Any]]
ConversationUpdateInput = Union[SmsConversationUpdate, dict[str, Any]]
ConsentInput = Union[SmsConsentInput, dict[str, Any]]
SettingsUpdateInput = Union[SmsSettingsUpdate, dict[str, Any]]


class SmsResource:
    messages: SmsMessagesResource
    batches: SmsBatchesResource
    conversations: SmsConversationsResource
    consents: SmsConsentsResource
    suppressions: SmsSuppressionsResource
    settings: SmsSettingsResource

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self.messages = SmsMessagesResource(client)
        self.batches = SmsBatchesResource(client)
        self.conversations = SmsConversationsResource(client)
        self.consents = SmsConsentsResource(client)
        self.suppressions = SmsSuppressionsResource(client)
        self.settings = SmsSettingsResource(client)

    def stream(self, *, last_event_id: Optional[str] = None) -> Iterator[SmsStreamEvent]:
        headers = {"Last-Event-ID": last_event_id} if last_event_id else None
        with self._client.stream("GET", "/v1/sms/stream", headers=headers) as resp:
            raise_for_status_streamed(resp)
            for event, event_id, data in iter_sse_with_id(resp.iter_text()):
                if event == "heartbeat":
                    continue
                payload = json.loads(data)
                yield SmsStreamEvent.model_validate(
                    {"event": event, "id": event_id, **payload}
                )


class SmsMessagesResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def send(self, params: SendInput, *, idempotency_key: str) -> SmsMessage:
        resp = self._client.post(
            "/v1/sms/messages",
            json=dump_params(params, SmsSendParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    def list(self, **filters: Any) -> tuple[list[SmsMessage], Optional[str]]:
        resp = self._client.get("/v1/sms/messages", params=query_string(filters))
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    def get(self, message_id: str) -> SmsMessage:
        resp = self._client.get(f"/v1/sms/messages/{path_id(message_id)}")
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    def cancel(self, message_id: str) -> SmsMessage:
        resp = self._client.post(f"/v1/sms/messages/{path_id(message_id)}/cancel", json={})
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())


class SmsBatchesResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def create(self, params: BatchInput, *, idempotency_key: str) -> SmsBatch:
        resp = self._client.post(
            "/v1/sms/batches",
            json=dump_params(params, SmsBatchCreateParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())

    def get(self, batch_id: str) -> SmsBatch:
        resp = self._client.get(f"/v1/sms/batches/{path_id(batch_id)}")
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())

    def messages(
        self, batch_id: str, *, cursor: Optional[str] = None, limit: Optional[int] = None
    ) -> tuple[list[SmsMessage], Optional[str]]:
        resp = self._client.get(
            f"/v1/sms/batches/{path_id(batch_id)}/messages",
            params=query_string({"cursor": cursor, "limit": limit}),
        )
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    def cancel(self, batch_id: str) -> SmsBatch:
        resp = self._client.post(f"/v1/sms/batches/{path_id(batch_id)}/cancel", json={})
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())


class SmsConversationsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(self, **filters: Any) -> tuple[list[SmsConversation], Optional[str]]:
        resp = self._client.get("/v1/sms/conversations", params=query_string(filters))
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsConversation.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    def get(self, conversation_id: str) -> SmsConversation:
        resp = self._client.get(f"/v1/sms/conversations/{path_id(conversation_id)}")
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    def messages(
        self,
        conversation_id: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[list[SmsMessage], Optional[str]]:
        resp = self._client.get(
            f"/v1/sms/conversations/{path_id(conversation_id)}/messages",
            params=query_string({"cursor": cursor, "limit": limit}),
        )
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    def send(
        self, conversation_id: str, params: ConversationSendInput, *, idempotency_key: str
    ) -> SmsMessage:
        resp = self._client.post(
            f"/v1/sms/conversations/{path_id(conversation_id)}/messages",
            json=dump_params(params, SmsConversationSendParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    def update(self, conversation_id: str, params: ConversationUpdateInput) -> SmsConversation:
        resp = self._client.patch(
            f"/v1/sms/conversations/{path_id(conversation_id)}",
            json=dump_params(params, SmsConversationUpdate),
        )
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    def mark_read(self, conversation_id: str) -> SmsConversation:
        resp = self._client.post(f"/v1/sms/conversations/{path_id(conversation_id)}/read", json={})
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    def add_note(self, conversation_id: str, body: str) -> SmsConversationNote:
        resp = self._client.post(
            f"/v1/sms/conversations/{path_id(conversation_id)}/notes", json={"body": body}
        )
        raise_for_status(resp)
        return SmsConversationNote.model_validate(resp.json())

    def notes(
        self, conversation_id: str, *, limit: Optional[int] = None
    ) -> list[SmsConversationNote]:
        resp = self._client.get(
            f"/v1/sms/conversations/{path_id(conversation_id)}/notes",
            params=query_string({"limit": limit}),
        )
        raise_for_status(resp)
        return [SmsConversationNote.model_validate(row) for row in resp.json().get("data", [])]

    def redact(self, conversation_id: str) -> None:
        resp = self._client.delete(f"/v1/sms/conversations/{path_id(conversation_id)}")
        raise_for_status(resp)


class SmsConsentsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(self, **filters: Any) -> list[SmsConsent]:
        resp = self._client.get("/v1/sms/consents", params=query_string(filters))
        raise_for_status(resp)
        return [SmsConsent.model_validate(row) for row in resp.json().get("data", [])]

    def create(self, params: ConsentInput) -> SmsConsent:
        resp = self._client.post("/v1/sms/consents", json=dump_params(params, SmsConsentInput))
        raise_for_status(resp)
        return SmsConsent.model_validate(resp.json())

    def import_records(self, records: list[ConsentInput]) -> list[SmsConsent]:
        payload = [dump_params(record, SmsConsentInput) for record in records]
        resp = self._client.post("/v1/sms/consents/import", json={"records": payload})
        raise_for_status(resp)
        return [SmsConsent.model_validate(row) for row in resp.json().get("data", [])]

    def revoke(self, consent_id: str, *, reason: str = "manual_revocation") -> SmsConsent:
        resp = self._client.post(
            f"/v1/sms/consents/{path_id(consent_id)}/revoke", json={"reason": reason}
        )
        raise_for_status(resp)
        return SmsConsent.model_validate(resp.json())


class SmsSuppressionsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(
        self, *, recipient: Optional[str] = None, active: bool = True, limit: Optional[int] = None
    ) -> list[SmsSuppression]:
        resp = self._client.get(
            "/v1/sms/suppressions",
            params=query_string(
                {"recipient": recipient, "active": str(active).lower(), "limit": limit}
            ),
        )
        raise_for_status(resp)
        return [SmsSuppression.model_validate(row) for row in resp.json().get("data", [])]


class SmsSettingsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(self) -> Optional[SmsSettings]:
        resp = self._client.get("/v1/sms/settings")
        raise_for_status(resp)
        return SmsSettings.model_validate(resp.json()) if resp.json() is not None else None

    def update(self, params: SettingsUpdateInput) -> SmsSettings:
        resp = self._client.patch("/v1/sms/settings", json=dump_params(params, SmsSettingsUpdate))
        raise_for_status(resp)
        return SmsSettings.model_validate(resp.json())


class AsyncSmsResource:
    messages: AsyncSmsMessagesResource
    batches: AsyncSmsBatchesResource
    conversations: AsyncSmsConversationsResource
    consents: AsyncSmsConsentsResource
    suppressions: AsyncSmsSuppressionsResource
    settings: AsyncSmsSettingsResource

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.messages = AsyncSmsMessagesResource(client)
        self.batches = AsyncSmsBatchesResource(client)
        self.conversations = AsyncSmsConversationsResource(client)
        self.consents = AsyncSmsConsentsResource(client)
        self.suppressions = AsyncSmsSuppressionsResource(client)
        self.settings = AsyncSmsSettingsResource(client)

    async def stream(self, *, last_event_id: Optional[str] = None) -> AsyncIterator[SmsStreamEvent]:
        headers = {"Last-Event-ID": last_event_id} if last_event_id else None
        async with self._client.stream("GET", "/v1/sms/stream", headers=headers) as resp:
            await araise_for_status_streamed(resp)
            async for event, event_id, data in aiter_sse_with_id(resp.aiter_text()):
                if event == "heartbeat":
                    continue
                yield SmsStreamEvent.model_validate(
                    {"event": event, "id": event_id, **json.loads(data)}
                )


class AsyncSmsMessagesResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, params: SendInput, *, idempotency_key: str) -> SmsMessage:
        resp = await self._client.post(
            "/v1/sms/messages",
            json=dump_params(params, SmsSendParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    async def list(self, **filters: Any) -> tuple[list[SmsMessage], Optional[str]]:
        resp = await self._client.get("/v1/sms/messages", params=query_string(filters))
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    async def get(self, message_id: str) -> SmsMessage:
        resp = await self._client.get(f"/v1/sms/messages/{path_id(message_id)}")
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    async def cancel(self, message_id: str) -> SmsMessage:
        resp = await self._client.post(f"/v1/sms/messages/{path_id(message_id)}/cancel", json={})
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())


class AsyncSmsBatchesResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def create(self, params: BatchInput, *, idempotency_key: str) -> SmsBatch:
        resp = await self._client.post(
            "/v1/sms/batches",
            json=dump_params(params, SmsBatchCreateParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())

    async def get(self, batch_id: str) -> SmsBatch:
        resp = await self._client.get(f"/v1/sms/batches/{path_id(batch_id)}")
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())

    async def messages(
        self, batch_id: str, *, cursor: Optional[str] = None, limit: Optional[int] = None
    ) -> tuple[list[SmsMessage], Optional[str]]:
        resp = await self._client.get(
            f"/v1/sms/batches/{path_id(batch_id)}/messages",
            params=query_string({"cursor": cursor, "limit": limit}),
        )
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    async def cancel(self, batch_id: str) -> SmsBatch:
        resp = await self._client.post(f"/v1/sms/batches/{path_id(batch_id)}/cancel", json={})
        raise_for_status(resp)
        return SmsBatch.model_validate(resp.json())


class AsyncSmsConversationsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> tuple[list[SmsConversation], Optional[str]]:
        resp = await self._client.get("/v1/sms/conversations", params=query_string(filters))
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsConversation.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    async def get(self, conversation_id: str) -> SmsConversation:
        resp = await self._client.get(f"/v1/sms/conversations/{path_id(conversation_id)}")
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    async def messages(
        self,
        conversation_id: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[list[SmsMessage], Optional[str]]:
        resp = await self._client.get(
            f"/v1/sms/conversations/{path_id(conversation_id)}/messages",
            params=query_string({"cursor": cursor, "limit": limit}),
        )
        raise_for_status(resp)
        body = resp.json()
        return (
            [SmsMessage.model_validate(row) for row in body.get("data", [])],
            body.get("next_cursor"),
        )

    async def send(
        self, conversation_id: str, params: ConversationSendInput, *, idempotency_key: str
    ) -> SmsMessage:
        resp = await self._client.post(
            f"/v1/sms/conversations/{path_id(conversation_id)}/messages",
            json=dump_params(params, SmsConversationSendParams),
            headers={"Idempotency-Key": idempotency_key},
        )
        raise_for_status(resp)
        return SmsMessage.model_validate(resp.json())

    async def update(
        self, conversation_id: str, params: ConversationUpdateInput
    ) -> SmsConversation:
        resp = await self._client.patch(
            f"/v1/sms/conversations/{path_id(conversation_id)}",
            json=dump_params(params, SmsConversationUpdate),
        )
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    async def mark_read(self, conversation_id: str) -> SmsConversation:
        resp = await self._client.post(
            f"/v1/sms/conversations/{path_id(conversation_id)}/read", json={}
        )
        raise_for_status(resp)
        return SmsConversation.model_validate(resp.json())

    async def add_note(self, conversation_id: str, body: str) -> SmsConversationNote:
        resp = await self._client.post(
            f"/v1/sms/conversations/{path_id(conversation_id)}/notes", json={"body": body}
        )
        raise_for_status(resp)
        return SmsConversationNote.model_validate(resp.json())

    async def notes(
        self, conversation_id: str, *, limit: Optional[int] = None
    ) -> list[SmsConversationNote]:
        resp = await self._client.get(
            f"/v1/sms/conversations/{path_id(conversation_id)}/notes",
            params=query_string({"limit": limit}),
        )
        raise_for_status(resp)
        return [SmsConversationNote.model_validate(row) for row in resp.json().get("data", [])]

    async def redact(self, conversation_id: str) -> None:
        resp = await self._client.delete(f"/v1/sms/conversations/{path_id(conversation_id)}")
        raise_for_status(resp)


class AsyncSmsConsentsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[SmsConsent]:
        resp = await self._client.get("/v1/sms/consents", params=query_string(filters))
        raise_for_status(resp)
        return [SmsConsent.model_validate(row) for row in resp.json().get("data", [])]

    async def create(self, params: ConsentInput) -> SmsConsent:
        resp = await self._client.post(
            "/v1/sms/consents", json=dump_params(params, SmsConsentInput)
        )
        raise_for_status(resp)
        return SmsConsent.model_validate(resp.json())

    async def import_records(self, records: list[ConsentInput]) -> list[SmsConsent]:
        resp = await self._client.post(
            "/v1/sms/consents/import",
            json={"records": [dump_params(record, SmsConsentInput) for record in records]},
        )
        raise_for_status(resp)
        return [SmsConsent.model_validate(row) for row in resp.json().get("data", [])]

    async def revoke(self, consent_id: str, *, reason: str = "manual_revocation") -> SmsConsent:
        resp = await self._client.post(
            f"/v1/sms/consents/{path_id(consent_id)}/revoke", json={"reason": reason}
        )
        raise_for_status(resp)
        return SmsConsent.model_validate(resp.json())


class AsyncSmsSuppressionsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(
        self, *, recipient: Optional[str] = None, active: bool = True, limit: Optional[int] = None
    ) -> list[SmsSuppression]:
        resp = await self._client.get(
            "/v1/sms/suppressions",
            params=query_string(
                {"recipient": recipient, "active": str(active).lower(), "limit": limit}
            ),
        )
        raise_for_status(resp)
        return [SmsSuppression.model_validate(row) for row in resp.json().get("data", [])]


class AsyncSmsSettingsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(self) -> Optional[SmsSettings]:
        resp = await self._client.get("/v1/sms/settings")
        raise_for_status(resp)
        return SmsSettings.model_validate(resp.json()) if resp.json() is not None else None

    async def update(self, params: SettingsUpdateInput) -> SmsSettings:
        resp = await self._client.patch(
            "/v1/sms/settings", json=dump_params(params, SmsSettingsUpdate)
        )
        raise_for_status(resp)
        return SmsSettings.model_validate(resp.json())
