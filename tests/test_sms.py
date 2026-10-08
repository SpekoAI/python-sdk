import json

import pytest
import respx

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
