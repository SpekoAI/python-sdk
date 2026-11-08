"""HTTP half of the Translation API: sessions, typed errors, idempotency."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from spekoai import (
    AsyncSpeko,
    Speko,
    SpekoApiError,
    SpekoTranslationAuthError,
    SpekoTranslationBalanceError,
    SpekoTranslationError,
    SpekoTranslationInvalidRequestError,
    SpekoTranslationPermissionError,
    SpekoTranslationRateLimitError,
    SpekoTranslationServerError,
    TranslationSession,
)
from spekoai.resources import translation as translation_module

GOLDENS = json.loads(
    (Path(__file__).parent / "fixtures" / "translation_goldens.json").read_text(encoding="utf-8")
)
SESSION = GOLDENS["translation_session"]
ENDED = GOLDENS["translation_session_ended"]
BASE = "https://translate.test"
SESSIONS = f"{BASE}/v1/translation/sessions"
ONE = f"{SESSIONS}/{SESSION['id']}"
END = f"{ONE}/end"


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(translation_module, "_RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def client():
    speko = Speko(api_key="sk-test", base_url="https://api.test", translation_base_url=BASE)
    yield speko
    speko.close()


@pytest.fixture
async def aclient():
    speko = AsyncSpeko(api_key="sk-test", base_url="https://api.test", translation_base_url=BASE)
    yield speko
    await speko.close()


def error_response(golden: str, status: int) -> httpx.Response:
    return httpx.Response(status, json=GOLDENS[golden])


@respx.mock
def test_create_sends_the_contract_headers_and_only_the_given_parameters(client):
    route = respx.post(SESSIONS).mock(return_value=httpx.Response(200, json=SESSION))

    session = client.translation.sessions.create(target_language="ru")

    assert isinstance(session, TranslationSession)
    assert session.id == SESSION["id"]
    assert session.status == "pending"
    assert session.client_secret == SESSION["client_secret"]
    assert session.stream_url == SESSION["stream_url"]
    assert session.usage.input_audio_ms == 0
    request = route.calls[0].request
    assert json.loads(request.content) == {"target_language": "ru"}
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["speko-version"] == "2026-11-08"
    assert request.headers["content-type"] == "application/json"
    assert 0 < len(request.headers["idempotency-key"]) <= 255


@respx.mock
def test_create_passes_optional_parameters_and_a_given_idempotency_key(client):
    route = respx.post(SESSIONS).mock(return_value=httpx.Response(200, json=SESSION))

    client.translation.sessions.create(
        target_language="ru", source_language="en", test=True, idempotency_key="my-key"
    )

    request = route.calls[0].request
    assert json.loads(request.content) == {
        "target_language": "ru",
        "source_language": "en",
        "test": True,
    }
    assert request.headers["idempotency-key"] == "my-key"


@respx.mock
def test_each_create_gets_its_own_idempotency_key(client):
    route = respx.post(SESSIONS).mock(return_value=httpx.Response(200, json=SESSION))

    client.translation.sessions.create(target_language="ru")
    client.translation.sessions.create(target_language="ru")

    first, second = (call.request.headers["idempotency-key"] for call in route.calls)
    assert first != second


@respx.mock
def test_retrieve_sends_the_version_and_no_idempotency_key(client):
    route = respx.get(ONE).mock(return_value=httpx.Response(200, json=ENDED))

    session = client.translation.sessions.retrieve(SESSION["id"])

    assert session.status == "ended"
    assert session.end_reason == "finished"
    assert session.ended_at == ENDED["ended_at"]
    assert session.client_secret is None
    assert session.usage.output_audio_ms == 48960
    request = route.calls[0].request
    assert request.headers["speko-version"] == "2026-11-08"
    assert "idempotency-key" not in request.headers


@respx.mock
def test_session_id_is_encoded_as_one_path_segment(client):
    route = respx.route(method="GET", host="translate.test").mock(
        return_value=httpx.Response(200, json=ENDED)
    )

    client.translation.sessions.retrieve("a/b")

    assert route.calls[0].request.url.raw_path == b"/v1/translation/sessions/a%2Fb"


@respx.mock
def test_end_posts_with_an_idempotency_key_and_outlasts_the_25_second_contract(monkeypatch):
    route = respx.post(END).mock(return_value=httpx.Response(200, json=ENDED))
    speko = Speko(api_key="sk-test", translation_base_url=BASE, timeout=1.0)
    try:
        session = speko.translation.sessions.end(SESSION["id"])
    finally:
        speko.close()

    assert session.status == "ended"
    request = route.calls[0].request
    assert request.headers["speko-version"] == "2026-11-08"
    assert request.headers["idempotency-key"]
    assert request.content == b""
    assert request.extensions["timeout"]["read"] > 25


def test_default_base_url_is_the_translation_host():
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://translate.speko.dev/v1/translation/sessions").mock(
            return_value=httpx.Response(200, json=SESSION)
        )
        speko = Speko(api_key="sk-test")
        try:
            speko.translation.sessions.create(target_language="ru")
        finally:
            speko.close()
    assert route.called


@pytest.mark.parametrize(
    ("golden", "status", "error_class"),
    [
        ("error_unsupported_language", 400, SpekoTranslationInvalidRequestError),
        ("error_parameter_invalid", 400, SpekoTranslationInvalidRequestError),
        ("error_resource_missing", 404, SpekoTranslationInvalidRequestError),
        ("error_idempotency_key_reused", 409, SpekoTranslationInvalidRequestError),
        ("error_key_invalid", 401, SpekoTranslationAuthError),
        ("error_permission_denied", 403, SpekoTranslationPermissionError),
        ("error_org_concurrency_limit", 429, SpekoTranslationRateLimitError),
        ("error_balance_exhausted", 402, SpekoTranslationBalanceError),
        ("error_internal", 500, SpekoTranslationServerError),
        ("error_service_unavailable", 503, SpekoTranslationServerError),
    ],
)
@respx.mock
def test_errors_are_typed_and_carry_every_contract_field(client, golden, status, error_class):
    respx.post(SESSIONS).mock(return_value=error_response(golden, status))

    with pytest.raises(error_class) as caught:
        client.translation.sessions.create(target_language="xx")

    body = GOLDENS[golden]["error"]
    error = caught.value
    assert isinstance(error, SpekoTranslationError)
    assert isinstance(error, SpekoApiError)
    assert error.status == status
    assert error.type == body["type"]
    assert error.code == body["code"]
    assert error.message == body["message"]
    assert str(error) == body["message"]
    assert error.doc_url == body["doc_url"]
    assert error.request_id == body["request_id"]
    assert error.param == body.get("param")


@respx.mock
def test_an_unknown_code_is_typed_by_its_type(client):
    body = {
        "error": {
            "type": "rate_limit_error",
            "code": "a_code_from_the_future",
            "message": "Slow down.",
            "doc_url": "https://docs.speko.dev/translation/errors#a_code_from_the_future",
            "request_id": "req_1",
        }
    }
    respx.post(SESSIONS).mock(return_value=httpx.Response(429, json=body))

    with pytest.raises(SpekoTranslationRateLimitError) as caught:
        client.translation.sessions.create(target_language="ru")

    assert caught.value.code == "a_code_from_the_future"


@respx.mock
def test_an_unknown_type_is_still_a_translation_error(client):
    body = {"error": {"type": "new_error", "code": "new_code", "message": "New."}}
    respx.post(SESSIONS).mock(return_value=httpx.Response(400, json=body))

    with pytest.raises(SpekoTranslationError) as caught:
        client.translation.sessions.create(target_language="ru")

    assert type(caught.value) is SpekoTranslationError
    assert caught.value.type == "new_error"


@respx.mock
def test_a_body_that_is_not_the_error_shape_keeps_the_status_and_request_id(client):
    respx.get(ONE).mock(
        return_value=httpx.Response(404, text="not json", headers={"Request-Id": "req_h"})
    )

    with pytest.raises(SpekoTranslationError) as caught:
        client.translation.sessions.retrieve(SESSION["id"])

    assert caught.value.status == 404
    assert caught.value.code == "UNKNOWN"
    assert caught.value.message == "not json"
    assert caught.value.request_id == "req_h"
    assert caught.value.doc_url is None


@respx.mock
def test_session_repr_hides_the_client_secret(client):
    respx.post(SESSIONS).mock(return_value=httpx.Response(200, json=SESSION))

    session = client.translation.sessions.create(target_language="ru")

    assert SESSION["client_secret"] not in repr(session)
    assert SESSION["client_secret"] not in str(session)


@pytest.mark.parametrize(
    "first",
    [
        error_response("error_service_unavailable", 503),
        error_response("error_internal", 500),
        error_response("error_idempotency_key_in_use", 409),
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["503", "500", "409-in-use", "connect-error", "read-timeout"],
)
@respx.mock
def test_a_retry_reuses_the_same_idempotency_key(client, first):
    route = respx.post(SESSIONS).mock(side_effect=[first, httpx.Response(200, json=SESSION)])

    session = client.translation.sessions.create(target_language="ru")

    assert session.id == SESSION["id"]
    first_key, second_key = (call.request.headers["idempotency-key"] for call in route.calls)
    assert first_key == second_key
    assert route.calls[0].request.content == route.calls[1].request.content


@pytest.mark.parametrize(
    ("golden", "status"),
    [
        ("error_unsupported_language", 400),
        ("error_key_invalid", 401),
        ("error_permission_denied", 403),
        ("error_idempotency_key_reused", 409),
        ("error_org_concurrency_limit", 429),
    ],
)
@respx.mock
def test_errors_that_a_retry_cannot_fix_are_not_retried(client, golden, status):
    route = respx.post(SESSIONS).mock(return_value=error_response(golden, status))

    with pytest.raises(SpekoTranslationError):
        client.translation.sessions.create(target_language="ru")

    assert route.call_count == 1


@respx.mock
def test_gives_up_after_three_attempts_with_the_last_error(client):
    route = respx.post(SESSIONS).mock(return_value=error_response("error_internal", 500))

    with pytest.raises(SpekoTranslationServerError):
        client.translation.sessions.create(target_language="ru")

    assert route.call_count == 3


@respx.mock
def test_network_failure_after_three_attempts_raises_the_network_error(client):
    route = respx.get(ONE).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(httpx.ConnectError):
        client.translation.sessions.retrieve(SESSION["id"])

    assert route.call_count == 3


@respx.mock
async def test_async_create_retrieve_end(aclient):
    create = respx.post(SESSIONS).mock(return_value=httpx.Response(200, json=SESSION))
    retrieve = respx.get(ONE).mock(return_value=httpx.Response(200, json=SESSION))
    end = respx.post(END).mock(return_value=httpx.Response(200, json=ENDED))

    created = await aclient.translation.sessions.create(target_language="ru", test=True)
    fetched = await aclient.translation.sessions.retrieve(created.id)
    ended = await aclient.translation.sessions.end(created.id)

    assert fetched.id == created.id
    assert ended.status == "ended"
    assert json.loads(create.calls[0].request.content) == {"target_language": "ru", "test": True}
    for route in (create, retrieve, end):
        assert route.calls[0].request.headers["speko-version"] == "2026-11-08"
    assert create.calls[0].request.headers["idempotency-key"]
    assert end.calls[0].request.headers["idempotency-key"]
    assert end.calls[0].request.extensions["timeout"]["read"] > 25


@respx.mock
async def test_async_retries_a_network_failure_with_the_same_key(aclient):
    route = respx.post(SESSIONS).mock(
        side_effect=[httpx.ConnectError("no route"), httpx.Response(200, json=SESSION)]
    )

    session = await aclient.translation.sessions.create(target_language="ru")

    assert session.id == SESSION["id"]
    first, second = (call.request.headers["idempotency-key"] for call in route.calls)
    assert first == second


@respx.mock
async def test_async_gives_up_after_three_attempts(aclient):
    route = respx.get(ONE).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(httpx.ConnectError):
        await aclient.translation.sessions.retrieve(SESSION["id"])

    assert route.call_count == 3


@respx.mock
async def test_async_errors_are_typed_and_retries_reuse_the_key(aclient):
    route = respx.post(SESSIONS).mock(
        side_effect=[
            error_response("error_service_unavailable", 503),
            error_response("error_unsupported_language", 400),
        ]
    )

    with pytest.raises(SpekoTranslationInvalidRequestError) as caught:
        await aclient.translation.sessions.create(target_language="xx")

    assert caught.value.code == "unsupported_language"
    assert caught.value.param == "target_language"
    first, second = (call.request.headers["idempotency-key"] for call in route.calls)
    assert first == second
