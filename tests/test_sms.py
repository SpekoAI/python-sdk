import json

import pytest
import respx

from spekoai import SpekoApiError
from tests.conftest import BASE

MESSAGE = {
    "id": "message_1",
    "conversation_id": "conversation_1",
    "batch_id": None,
    "from_phone_number_id": "number_1",
    "direction": "outbound",
    "origin": "api",
    "from": "+12025550100",
    "to": "+12025550123",
    "text": "Hello",
    "campaign_id": "campaign_1",
    "brand_id": "brand_1",
    "campaign_snapshot": {},
    "consent_id": "consent_1",
    "consent_basis": "explicit",
    "recipient_timezone": "America/New_York",
    "requested_send_at": None,
    "effective_send_at": "2026-09-01T12:00:00.000Z",
    "status": "queued",
    "provider_status": None,
    "encoding": "gsm7",
    "estimated_segments": 1,
    "segment_count": 1,
    "estimated": {"encoding": "gsm7", "units": 5, "segments": 1, "per_segment": 160},
    "charged_micro_usd": "0",
    "provider_cost_micro_usd": None,
    "metadata": {},
    "error": None,
    "created_at": "2026-09-01T12:00:00.000Z",
    "updated_at": "2026-09-01T12:00:00.000Z",
}


@respx.mock
def test_sms_send_uses_idempotency_and_snake_case(speko):
    route = respx.post(f"{BASE}/v1/sms/messages").respond(status_code=202, json=MESSAGE)

    result = speko.sms.messages.send(
        {
            "from_phone_number_id": "number_1",
            "to": "+12025550123",
            "text": "Hello",
            "recipient_timezone": "America/New_York",
        },
        idempotency_key="customer-message-1",
    )

    assert result.from_ == "+12025550100"
    assert result.estimated.per_segment == 160
    assert route.calls.last.request.headers["Idempotency-Key"] == "customer-message-1"
    assert json.loads(route.calls.last.request.content) == {
        "from_phone_number_id": "number_1",
        "to": "+12025550123",
        "text": "Hello",
        "recipient_timezone": "America/New_York",
    }


@respx.mock
def test_sms_resource_paths_and_filters(speko):
    messages = respx.get(f"{BASE}/v1/sms/messages").respond(
        json={"data": [MESSAGE], "next_cursor": "next"}
    )
    consents = respx.post(f"{BASE}/v1/sms/consents/import").respond(
        status_code=201, json={"data": [], "imported": 0}
    )
    suppressions = respx.get(f"{BASE}/v1/sms/suppressions").respond(
        json={"data": [], "next_cursor": None}
    )

    rows, cursor = speko.sms.messages.list(status="queued", limit=25)
    speko.sms.consents.import_records([])
    speko.sms.suppressions.list(active=False)

    assert rows[0].id == "message_1"
    assert cursor == "next"
    assert dict(messages.calls.last.request.url.params) == {"status": "queued", "limit": "25"}
    assert json.loads(consents.calls.last.request.content) == {"records": []}
    assert dict(suppressions.calls.last.request.url.params) == {"active": "false"}


@pytest.mark.asyncio
@respx.mock
async def test_async_sms_send(aspeko):
    route = respx.post(f"{BASE}/v1/sms/messages").respond(status_code=202, json=MESSAGE)

    result = await aspeko.sms.messages.send(
        {"from_phone_number_id": "number_1", "to": "+12025550123", "text": "Hello"},
        idempotency_key="async-message-1",
    )

    assert result.status == "queued"
    assert route.calls.last.request.headers["Idempotency-Key"] == "async-message-1"


@respx.mock
def test_sms_agent_inbox_filters_and_unpinning(speko):
    conversation = {
        "id": "conversation_1",
        "phone_number_id": "number_1",
        "remote_phone_number": "+12025550123",
        "status": "open",
        "automation_status": "enabled",
        "assigned_agent_id": "agent_b",
        "agent_override_id": None,
        "unread_count": 0,
        "last_message_at": "2026-09-01T12:00:00.000Z",
        "created_at": "2026-09-01T12:00:00.000Z",
        "updated_at": "2026-09-01T12:00:00.000Z",
    }
    inbox = respx.get(f"{BASE}/v1/sms/conversations").respond(
        json={"data": [conversation], "next_cursor": None}
    )
    stamped = respx.get(f"{BASE}/v1/sms/messages").respond(
        json={
            "data": [{**MESSAGE, "phone_number_id": "number_1", "agent_id": "agent_a"}],
            "next_cursor": None,
        }
    )
    unpin = respx.patch(f"{BASE}/v1/sms/conversations/conversation_1").respond(json=conversation)

    threads, _ = speko.sms.conversations.list(agent_id="agent_a", limit=50)
    rows, _ = speko.sms.messages.list(agent_id="agent_a")
    updated = speko.sms.conversations.update("conversation_1", {"assigned_agent_id": None})

    assert dict(inbox.calls.last.request.url.params) == {"agent_id": "agent_a", "limit": "50"}
    assert (threads[0].assigned_agent_id, threads[0].agent_override_id) == ("agent_b", None)
    assert dict(stamped.calls.last.request.url.params) == {"agent_id": "agent_a"}
    assert (rows[0].phone_number_id, rows[0].agent_id) == ("number_1", "agent_a")
    assert json.loads(unpin.calls.last.request.content) == {"assigned_agent_id": None}
    assert updated.agent_override_id is None


@respx.mock
def test_sms_agent_inbox_writes_carry_the_expected_agent(speko):
    reply = respx.post(f"{BASE}/v1/sms/conversations/conversation_1/messages").respond(
        status_code=202, json=MESSAGE
    )
    patch = respx.patch(f"{BASE}/v1/sms/conversations/conversation_1").respond(
        status_code=409,
        json={
            "error": "Another agent handles this conversation now.",
            "code": "SMS_THREAD_AGENT_CHANGED",
        },
    )
    first = respx.post(f"{BASE}/v1/sms/messages").respond(status_code=202, json=MESSAGE)

    speko.sms.messages.send(
        {
            "from_phone_number_id": "number_1",
            "to": "+12025550123",
            "text": "Your table is ready.",
            "expected_agent_id": "agent_a",
        },
        idempotency_key="first-1",
    )
    speko.sms.conversations.send(
        "conversation_1",
        {"text": "On my way.", "expected_agent_id": "agent_a"},
        idempotency_key="reply-1",
    )
    with pytest.raises(SpekoApiError) as refused:
        speko.sms.conversations.update(
            "conversation_1", {"automation_status": "paused", "expected_agent_id": "agent_a"}
        )

    assert json.loads(first.calls.last.request.content) == {
        "from_phone_number_id": "number_1",
        "to": "+12025550123",
        "text": "Your table is ready.",
        "expected_agent_id": "agent_a",
    }
    assert json.loads(reply.calls.last.request.content) == {
        "text": "On my way.",
        "expected_agent_id": "agent_a",
    }
    assert json.loads(patch.calls.last.request.content) == {
        "automation_status": "paused",
        "expected_agent_id": "agent_a",
    }
    assert (refused.value.status, refused.value.code) == (409, "SMS_THREAD_AGENT_CHANGED")
