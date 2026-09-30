import json

import httpx
import pytest
import respx

from spekoai import SpekoAuthError, SpekoRateLimitError
from tests.conftest import BASE

AGENT = {
    "id": "ag_1",
    "organizationId": "org_1",
    "name": "Support Bot",
    "systemPrompt": "You help.",
    "voice": None,
    "intent": {"language": "en", "optimizeFor": "latency"},
    "createdAt": "2026-07-01T00:00:00.000Z",
    "updatedAt": "2026-07-01T00:00:00.000Z",
}

PHONE_NUMBER = {
    "id": "pn_1",
    "organizationId": "org_1",
    "e164": "+12015551234",
    "source": "managed",
    "direction": "both",
    "setupStatus": {
        "status": "ready",
        "inboundReady": True,
        "outboundReady": True,
        "agentReady": True,
        "forwardingRequired": False,
        "forwardingSipUri": None,
        "sipConnectionReady": True,
        "issues": [],
    },
    "nextChargeAt": "2026-08-01T00:00:00.000Z",
    "createdAt": "2026-07-01T00:00:00.000Z",
    "updatedAt": "2026-07-01T00:00:00.000Z",
}


@respx.mock
def test_agents_crud_paths(speko):
    respx.get(f"{BASE}/v1/agents").respond(json=[AGENT])
    respx.post(f"{BASE}/v1/agents").respond(json=AGENT)
    respx.get(f"{BASE}/v1/agents/ag_1").respond(json=AGENT)
    respx.patch(f"{BASE}/v1/agents/ag_1").respond(json=AGENT)
    respx.delete(f"{BASE}/v1/agents/ag_1").respond(json={"deleted": True})

    agents = speko.agents.list()
    assert agents[0].intent.optimize_for == "latency"

    created = speko.agents.create(
        {
            "name": "Support Bot",
            "system_prompt": "You help.",
            "intent": {"language": "en", "optimize_for": "latency"},
        }
    )
    assert created.id == "ag_1"
    sent = json.loads(respx.routes[1].calls.last.request.content)
    assert sent == {
        "name": "Support Bot",
        "systemPrompt": "You help.",
        "intent": {"language": "en", "optimizeFor": "latency"},
    }

    speko.agents.get("ag_1")
    speko.agents.update("ag_1", {"voice": "sophia"})
    assert json.loads(respx.routes[3].calls.last.request.content) == {"voice": "sophia"}
    assert speko.agents.delete("ag_1") is True


@respx.mock
def test_agent_attach_detach_phone_number(speko):
    route = respx.patch(f"{BASE}/v1/phone-numbers/pn_1").respond(json=PHONE_NUMBER)

    speko.agents.attach_phone_number("ag_1", "pn_1")
    assert json.loads(route.calls.last.request.content) == {"agentId": "ag_1"}

    speko.agents.detach_phone_number("pn_1")
    assert json.loads(route.calls.last.request.content) == {"agentId": None}


@respx.mock
def test_phone_number_update_explicit_none_unlinks(speko):
    route = respx.patch(f"{BASE}/v1/phone-numbers/pn_1").respond(json=PHONE_NUMBER)
    speko.phone_numbers.update("pn_1", {"agent_id": None, "label": "Sales"})
    assert json.loads(route.calls.last.request.content) == {"agentId": None, "label": "Sales"}


@respx.mock
def test_phone_number_search_query_params(speko):
    route = respx.get(f"{BASE}/v1/phone-numbers/available").respond(
        json=[
            {
                "e164": "+14155550123",
                "friendlyName": "+1 (415) 555-0123",
                "monthlyCostUsd": 1.0,
                "upfrontCostUsd": 1.0,
                "features": ["voice"],
                "region": {"state": "CA", "locality": "San Francisco", "rateCenter": None},
            }
        ]
    )
    results = speko.phone_numbers.search_available(area_code="415", limit=5)
    assert results[0].region.locality == "San Francisco"
    params = dict(route.calls.last.request.url.params)
    assert params == {"areaCode": "415", "limit": "5"}


@respx.mock
def test_agent_tools_available_flag_and_chat_tools(speko):
    tool_row = {
        "id": "tool_1",
        "agentId": "ag_1",
        "name": "lookup",
        "description": "Look up an order",
        "parameters": {"type": "object"},
        "source": {"kind": "webhook", "url": "https://x.test/hook", "secretRef": "sr_1"},
        "preToolSpeech": "always",
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-01T00:00:00.000Z",
    }
    route = respx.get(f"{BASE}/v1/agents/ag_1/tools").respond(json=[tool_row])

    chat_tools = speko.agents.tools.list_chat_tools("ag_1", available=True)
    assert dict(route.calls.last.request.url.params) == {"available": "1"}
    assert chat_tools[0].execution_mode == "webhook"
    assert chat_tools[0].source.secret_ref == "sr_1"
    assert chat_tools[0].pre_tool_speech == "always"


@respx.mock
def test_agent_tool_simulation_round_trips(speko):
    from spekoai import (
        AgentToolCreateParams,
        AgentToolSimulationLive,
        AgentToolSimulationMock,
        AgentToolUpdateParams,
    )

    base_row = {
        "id": "tool_1",
        "agentId": "ag_1",
        "name": "get_next_question",
        "description": "Fetch the next question",
        "parameters": {"type": "object"},
        "source": {"kind": "webhook", "url": "https://x.test/hook", "secretRef": "sr_1"},
        "preToolSpeech": "auto",
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-01T00:00:00.000Z",
    }
    mock_row = {
        **base_row,
        "simulation": {"mode": "mock", "response": {"question_text": "Start date?"}},
    }
    create_route = respx.post(f"{BASE}/v1/agents/ag_1/tools").respond(json=mock_row)
    row = speko.agents.tools.create(
        "ag_1",
        AgentToolCreateParams(
            name="get_next_question",
            description="Fetch the next question",
            parameters={"type": "object"},
            source={"kind": "webhook", "url": "https://x.test/hook", "secret": "whsec_12345678"},
            simulation=AgentToolSimulationMock(
                mode="mock", response={"question_text": "Start date?"}
            ),
        ),
    )
    sent = json.loads(create_route.calls.last.request.content)
    # The canned response is opaque JSON: its keys are NOT camel-cased.
    assert sent["simulation"] == {"mode": "mock", "response": {"question_text": "Start date?"}}
    assert isinstance(row.simulation, AgentToolSimulationMock)
    assert row.simulation.response == {"question_text": "Start date?"}

    # Mock without a response omits the key; unset rows read back as None.
    create_route.respond(json=base_row)
    row = speko.agents.tools.create(
        "ag_1",
        {
            "name": "x",
            "description": "x",
            "parameters": {},
            "source": {"kind": "inline"},
            "simulation": {"mode": "mock"},
        },
    )
    assert json.loads(create_route.calls.last.request.content)["simulation"] == {"mode": "mock"}
    assert row.simulation is None

    update_route = respx.patch(f"{BASE}/v1/agents/ag_1/tools/tool_1").respond(
        json={**base_row, "simulation": {"mode": "live"}}
    )
    row = speko.agents.tools.update(
        "ag_1", "tool_1", AgentToolUpdateParams(simulation=AgentToolSimulationLive(mode="live"))
    )
    assert json.loads(update_route.calls.last.request.content) == {"simulation": {"mode": "live"}}
    assert isinstance(row.simulation, AgentToolSimulationLive)

    # Explicit None clears the override.
    speko.agents.tools.update("ag_1", "tool_1", {"simulation": None})
    assert json.loads(update_route.calls.last.request.content) == {"simulation": None}


@respx.mock
def test_calls_snake_case_payloads(speko):
    detail = {
        "id": "sess_1",
        "call_id": "sess_1",
        "resource_uri": "/v1/calls/sess_1",
        "agent_id": None,
        "status": "ended",
        "kind": "phone",
        "room_name": "room",
        "language": "en",
        "pipeline_config": {},
        "metadata": {},
        "created_at": "2026-07-01T00:00:00.000Z",
        "updated_at": "2026-07-01T00:00:00.000Z",
        "ended_at": "2026-07-01T00:05:00.000Z",
        "duration_seconds": 300,
        "recording_status": "ready",
        "recording_duration_ms": 300000,
        "recording_resource_uri": "/v1/calls/sess_1/recording",
        "report": None,
        "transfers": [],
        "transcript": {"entries": []},
        "span_tree": {},
    }
    respx.get(f"{BASE}/v1/calls/sess_1").respond(json=detail)
    result = speko.calls.get("sess_1")
    assert result.duration_seconds == 300
    assert result.recording_status == "ready"

    respx.post(f"{BASE}/v1/calls/sess_1/end").respond(
        json={"ok": True, "status": "already_ended", "ended_at": "2026-07-01T00:05:00.000Z"}
    )
    ended = speko.calls.end("sess_1")
    assert ended.status == "already_ended"

    join_route = respx.post(f"{BASE}/v1/calls/sess_1/web-join").respond(
        json={
            "token": "tok",
            "url": "wss://lk.test",
            "identity": "web_1",
            "roomName": "room",
            "expiresAt": "2026-07-01T00:10:00.000Z",
        }
    )
    joined = speko.calls.web_join("sess_1", {"display_name": "Reviewer"})
    assert json.loads(join_route.calls.last.request.content) == {"displayName": "Reviewer"}
    assert joined.room_name == "room"


@respx.mock
def test_warm_transfer_from_alias(speko):
    transfer = {
        "id": "tr_1",
        "session_id": "sess_1",
        "organization_id": "org_1",
        "kind": "warm",
        "status": "requested",
        "transfer_to": "+15551234567",
        "metadata": {},
        "created_at": "2026-07-01T00:00:00.000Z",
        "updated_at": "2026-07-01T00:00:00.000Z",
        "completed_at": None,
    }
    route = respx.post(f"{BASE}/v1/calls/sess_1/transfers/warm").respond(json=transfer)
    speko.calls.warm_transfer(
        "sess_1", {"to": "+15551234567", "from": "+12015551234", "wait_until_answered": True}
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"to": "+15551234567", "from": "+12015551234", "waitUntilAnswered": True}


@respx.mock
def test_callbacks_list_query(speko):
    route = respx.get(f"{BASE}/v1/callbacks").respond(json={"callbacks": []})
    assert speko.callbacks.list(status="scheduled", source_session_id="sess_1") == []
    assert dict(route.calls.last.request.url.params) == {
        "status": "scheduled",
        "source_session_id": "sess_1",
    }


@respx.mock
def test_webhook_deliveries_query_mapping(speko):
    route = respx.get(f"{BASE}/v1/webhook-deliveries").respond(
        json={"data": [], "nextCursor": None, "endpointOptions": []}
    )
    page = speko.webhooks.deliveries.list(
        endpoint_id="we_1", event="call.report", from_date="2026-07-01", limit=10
    )
    assert page.next_cursor is None
    assert dict(route.calls.last.request.url.params) == {
        "endpointId": "we_1",
        "event": "call.report",
        "from": "2026-07-01",
        "limit": "10",
    }


@respx.mock
def test_webhooks_list_unwraps_data(speko):
    respx.get(f"{BASE}/v1/webhooks").respond(
        json={
            "data": [
                {
                    "id": "we_1",
                    "name": "reports",
                    "url": "https://x.test/wh",
                    "events": ["call.report"],
                    "allAgents": True,
                    "agentIds": [],
                    "filterTags": {},
                    "headers": {},
                    "authHeaders": [{"name": "Authorization", "configured": True}],
                    "timeoutMs": 10000,
                    "signingSecretSource": "workspace",
                    "hasCustomSigningSecret": False,
                    "extractionFields": [],
                    "legacyManaged": False,
                    "createdAt": "2026-07-01T00:00:00.000Z",
                    "updatedAt": "2026-07-01T00:00:00.000Z",
                }
            ]
        }
    )
    endpoints = speko.webhooks.list()
    assert endpoints[0].auth_headers[0].name == "Authorization"


@respx.mock
def test_voice_dial_wire_shape(speko):
    route = respx.post(f"{BASE}/v1/sessions/phone").respond(
        json={
            "sessionId": "sess_1",
            "callControlId": "cc_1",
            "roomName": "room",
            "status": "dialing",
            "to": "+12015551234",
            "from": "+16465550000",
        }
    )
    result = speko.voice.dial(
        {
            "to": "+12015551234",
            "agent_id": "ag_1",
            "variables": {"customer": "Mr. Lee"},
            "turn_handling": {"greet_first": False},
            "max_duration_seconds": 600,
            "stt_options": {"keywords": ["Lee"], "prompt": "Returning customer, order follow-up."},
        }
    )
    assert result.from_ == "+16465550000"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "to": "+12015551234",
        "agentId": "ag_1",
        "variables": {"customer": "Mr. Lee"},
        "turnHandling": {"greetFirst": False},
        "maxDurationSeconds": 600,
        "sttOptions": {"keywords": ["Lee"], "prompt": "Returning customer, order follow-up."},
    }


@respx.mock
def test_voice_dial_turn_handling_audio_options_wire_shape(speko):
    route = respx.post(f"{BASE}/v1/sessions/phone").respond(
        json={
            "sessionId": "sess_2",
            "callControlId": "cc_2",
            "roomName": "room",
            "status": "dialing",
            "to": "+12015551234",
            "from": "+16465550000",
        }
    )
    speko.voice.dial(
        {
            "to": "+12015551234",
            "agent_id": "ag_1",
            "turn_handling": {
                "vad": {"provider": "ai-coustics"},
                "noise_cancellation": {"enabled": True, "model": "quail-voice-focus"},
            },
        }
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["turnHandling"] == {
        "vad": {"provider": "ai-coustics"},
        "noiseCancellation": {"enabled": True, "model": "quail-voice-focus"},
    }

    speko.voice.dial(
        {
            "to": "+12015551234",
            "agent_id": "ag_1",
            "turn_handling": {"noise_cancellation": {"enabled": False}},
        }
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["turnHandling"] == {"noiseCancellation": {"enabled": False}}


@respx.mock
def test_voices_and_sessions_transcript(speko):
    respx.get(f"{BASE}/v1/voices").respond(
        json={
            "voices": [{"vendor": "cartesia", "id": "v1", "name": "Sophia"}],
            "providers": [
                {
                    "key": "cartesia",
                    "name": "Cartesia",
                    "models": ["sonic-3.5"],
                    "voicesFetchedLive": False,
                }
            ],
        }
    )
    catalog = speko.voices.list(provider="cartesia")
    assert catalog.voices[0].vendor == "cartesia"

    respx.get(f"{BASE}/v1/sessions/sess_1/transcript").respond(
        json={
            "entries": [
                {
                    "id": "t_1",
                    "index": 0,
                    "source": "agent",
                    "text": "Hello!",
                    "startedAt": "2026-07-01T00:00:00.000Z",
                    "endedAt": None,
                    "provider": "openai",
                    "model": "gpt-4o",
                    "eouMs": 120,
                    "llmTtftMs": 300,
                    "ttsTtfbMs": 80,
                    "latencyStatus": "complete",
                    "conversationalLatencyMs": 500,
                    "toolCalls": [{"name": "end_call", "args": "{}"}],
                }
            ]
        }
    )
    transcript = speko.sessions.transcript("sess_1")
    assert transcript.entries[0].tool_calls[0].name == "end_call"
    assert transcript.entries[0].conversational_latency_ms == 500


@respx.mock
def test_error_mapping(speko):
    respx.get(f"{BASE}/v1/usage").respond(
        status_code=401, json={"error": "bad key", "code": "AUTH_ERROR"}
    )
    with pytest.raises(SpekoAuthError):
        speko.usage.get()

    respx.get(f"{BASE}/v1/credits/balance").respond(
        status_code=429,
        json={"error": "slow down", "code": "RATE_LIMITED"},
        headers={"Retry-After": "7"},
    )
    with pytest.raises(SpekoRateLimitError) as exc:
        speko.credits.get_balance()
    assert exc.value.retry_after == 7


@respx.mock
async def test_async_resources_mirror(aspeko):
    respx.get(f"{BASE}/v1/agents").respond(json=[AGENT])
    agents = await aspeko.agents.list()
    assert agents[0].id == "ag_1"

    route = respx.patch(f"{BASE}/v1/phone-numbers/pn_1").respond(json=PHONE_NUMBER)
    await aspeko.phone_numbers.update("pn_1", {"agent_id": None})
    assert json.loads(route.calls.last.request.content) == {"agentId": None}

    respx.get(f"{BASE}/v1/callbacks").respond(json={"callbacks": []})
    assert await aspeko.callbacks.list(limit=1) == []


@respx.mock
def test_kyb_flow(speko):
    respx.get(f"{BASE}/v1/phone-numbers/kyb").respond(
        json={"status": "missing", "submission": None, "prefill": None}
    )
    overview = speko.phone_numbers.get_kyb()
    assert overview.status == "missing"

    submission = {
        "id": "kyb_1",
        "organizationId": "org_1",
        "status": "submitted",
        "businessProfile": None,
        "authorizedRepresentative": None,
        "attestationAccepted": True,
        "attestedAt": None,
        "submittedByUserId": None,
        "submittedByEmail": None,
        "submittedByApiKeyId": None,
        "submittedAt": "2026-07-01T00:00:00.000Z",
        "reviewerUserId": None,
        "reviewerEmail": None,
        "reviewedAt": None,
        "rejectionReason": None,
        "slackNotificationStatus": "queued",
        "slackNotificationJobId": None,
        "slackNotificationError": None,
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-01T00:00:00.000Z",
    }
    route = respx.post(f"{BASE}/v1/phone-numbers/kyb/submit").respond(json=submission)
    business = {
        "legal_name": "Acme Inc",
        "display_name": "Acme",
        "entity_type": "llc",
        "country": "US",
        "website": "https://acme.test",
        "address": {
            "street": "1 Main St",
            "city": "SF",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
        },
        "use_case": "support",
        "expected_usage": "low",
    }
    rep = {"name": "Jo", "title": "CEO", "email": "jo@acme.test"}
    result = speko.phone_numbers.submit_kyb(
        {
            "business_profile": business,
            "authorized_representative": rep,
            "attestation_accepted": True,
        }
    )
    assert result.status == "submitted"
    sent = json.loads(route.calls.last.request.content)
    assert sent["attestationAccepted"] is True
    assert sent["businessProfile"]["legalName"] == "Acme Inc"

    speko.phone_numbers.submit_kyb(
        {
            "declaration": {
                "business_name": "Acme Inc",
                "use_case": "Inbound support and opted-in reminders",
            },
            "attestation_accepted": True,
            "attestation_version": "phone-communications-v1",
        }
    )
    minimal_sent = json.loads(route.calls.last.request.content)
    assert minimal_sent == {
        "declaration": {
            "businessName": "Acme Inc",
            "useCase": "Inbound support and opted-in reminders",
        },
        "attestationAccepted": True,
        "attestationVersion": "phone-communications-v1",
    }
    assert sent["businessProfile"]["address"]["postalCode"] == "94105"


def test_httpx_client_has_user_agent(speko):
    assert speko._client.headers["User-Agent"].startswith("spekoai-python/")
    assert isinstance(speko._client, httpx.Client)
