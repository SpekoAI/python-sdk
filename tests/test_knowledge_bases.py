import json

import httpx
import pytest
import respx

from spekoai import SpekoApiError
from tests.conftest import BASE

SIGNED_URL = "https://storage.test/signed-put"


def _doc(status: str = "pending") -> dict:
    return {
        "id": "doc_1",
        "knowledgeBaseId": "kb_1",
        "filename": "faq.md",
        "contentType": "text/markdown",
        "sizeBytes": 5,
        "status": status,
        "errorMessage": "boom" if status == "failed" else None,
        "chunkCount": 0,
        "metadata": None,
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-01T00:00:00.000Z",
        "ingestedAt": None,
    }


@respx.mock
def test_upload_document_flow(speko):
    create_route = respx.post(f"{BASE}/v1/knowledge-bases/kb_1/documents").respond(
        json={
            "document": _doc("pending"),
            "upload": {
                "url": SIGNED_URL,
                "method": "PUT",
                "headers": {"Content-Type": "text/markdown"},
                "expiresInSeconds": 600,
            },
        }
    )
    put_route = respx.put(SIGNED_URL).respond(status_code=200)
    respx.post(f"{BASE}/v1/knowledge-bases/kb_1/documents/doc_1/finalize").respond(
        json=_doc("processing")
    )

    doc = speko.knowledge_bases.upload_document(
        "kb_1", {"filename": "faq.md", "content_type": "text/markdown", "data": b"# faq"}
    )
    assert doc.status == "processing"

    sent = json.loads(create_route.calls.last.request.content)
    assert sent == {"filename": "faq.md", "contentType": "text/markdown", "sizeBytes": 5}

    put_request = put_route.calls.last.request
    assert put_request.content == b"# faq"
    assert put_request.headers["Content-Type"] == "text/markdown"
    # The signed URL must NOT receive the Speko API key.
    assert "authorization" not in put_request.headers


@respx.mock
def test_upload_document_put_failure(speko):
    respx.post(f"{BASE}/v1/knowledge-bases/kb_1/documents").respond(
        json={
            "document": _doc("pending"),
            "upload": {
                "url": SIGNED_URL,
                "method": "PUT",
                "headers": {},
                "expiresInSeconds": 600,
            },
        }
    )
    respx.put(SIGNED_URL).respond(status_code=403, text="expired")
    with pytest.raises(SpekoApiError) as exc:
        speko.knowledge_bases.upload_document(
            "kb_1", {"filename": "faq.md", "content_type": "text/markdown", "data": b"# faq"}
        )
    assert exc.value.code == "DOCUMENT_UPLOAD_FAILED"


@respx.mock
def test_poll_document_ready(speko):
    route = respx.get(f"{BASE}/v1/knowledge-bases/kb_1/documents/doc_1")
    route.side_effect = [
        httpx.Response(200, json=_doc("processing")),
        httpx.Response(200, json=_doc("ready")),
    ]
    doc = speko.knowledge_bases.poll_document_ready(
        "kb_1", "doc_1", interval_seconds=0.01
    )
    assert doc.status == "ready"


@respx.mock
def test_poll_document_failed_raises(speko):
    respx.get(f"{BASE}/v1/knowledge-bases/kb_1/documents/doc_1").respond(
        json=_doc("failed")
    )
    with pytest.raises(SpekoApiError) as exc:
        speko.knowledge_bases.poll_document_ready("kb_1", "doc_1", interval_seconds=0.01)
    assert exc.value.code == "DOCUMENT_INGEST_FAILED"
    assert "boom" in exc.value.message


@respx.mock
async def test_async_upload_document_flow(aspeko):
    respx.post(f"{BASE}/v1/knowledge-bases/kb_1/documents").respond(
        json={
            "document": _doc("pending"),
            "upload": {
                "url": SIGNED_URL,
                "method": "PUT",
                "headers": {},
                "expiresInSeconds": 600,
            },
        }
    )
    put_route = respx.put(SIGNED_URL).respond(status_code=200)
    respx.post(f"{BASE}/v1/knowledge-bases/kb_1/documents/doc_1/finalize").respond(
        json=_doc("processing")
    )
    doc = await aspeko.knowledge_bases.upload_document(
        "kb_1", {"filename": "faq.md", "content_type": "text/markdown", "data": b"# faq"}
    )
    assert doc.status == "processing"
    assert "authorization" not in put_route.calls.last.request.headers
