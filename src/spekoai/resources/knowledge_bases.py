"""Per-agent knowledge bases. Each KB owns documents that get embedded so
the agent can retrieve relevant chunks during a call. Every agent created via
``agents.create`` auto-provisions a ``Default`` KB; additional KBs can be
created explicitly with ``create``."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional, Union

import httpx

from spekoai._http import dump_params, path_id, query_string, raise_for_status
from spekoai.errors import SpekoApiError
from spekoai.models import (
    KnowledgeBaseCreateParams,
    KnowledgeBaseDocumentCreateParams,
    KnowledgeBaseDocumentCreateResult,
    KnowledgeBaseDocumentRow,
    KnowledgeBaseDocumentUploadParams,
    KnowledgeBaseRow,
)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_TIMEOUT_SECONDS = 120.0
# The server's signed upload URL expires 600 s after issuance.
_UPLOAD_TIMEOUT_SECONDS = 600.0

CreateInput = Union[KnowledgeBaseCreateParams, dict[str, Any]]
DocumentCreateInput = Union[KnowledgeBaseDocumentCreateParams, dict[str, Any]]
DocumentUploadInput = Union[KnowledgeBaseDocumentUploadParams, dict[str, Any]]


def _upload_failed_error(resp: httpx.Response) -> SpekoApiError:
    try:
        text = resp.text
    except Exception:
        text = ""
    return SpekoApiError(
        f"Document upload failed: {resp.status_code} {text or resp.reason_phrase}",
        resp.status_code,
        "DOCUMENT_UPLOAD_FAILED",
    )


def _ingest_failed_error(doc: KnowledgeBaseDocumentRow) -> SpekoApiError:
    return SpekoApiError(
        doc.error_message or "Document ingest failed", 500, "DOCUMENT_INGEST_FAILED"
    )


def _ingest_timeout_error(timeout_seconds: float) -> SpekoApiError:
    return SpekoApiError(
        f"Document ingest timed out after {timeout_seconds}s", 408, "DOCUMENT_INGEST_TIMEOUT"
    )


def _create_params_for(
    upload_params: KnowledgeBaseDocumentUploadParams,
) -> dict[str, Any]:
    create_params: dict[str, Any] = {
        "filename": upload_params.filename,
        "content_type": upload_params.content_type,
        "size_bytes": len(upload_params.data),
    }
    if upload_params.metadata is not None:
        create_params["metadata"] = upload_params.metadata
    return create_params


class KnowledgeBasesResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def create(self, params: CreateInput) -> KnowledgeBaseRow:
        resp = self._client.post(
            "/v1/knowledge-bases", json=dump_params(params, KnowledgeBaseCreateParams)
        )
        raise_for_status(resp)
        return KnowledgeBaseRow.model_validate(resp.json())

    def list(self, *, agent_id: Optional[str] = None) -> list[KnowledgeBaseRow]:
        resp = self._client.get(
            "/v1/knowledge-bases", params=query_string({"agentId": agent_id})
        )
        raise_for_status(resp)
        return [KnowledgeBaseRow.model_validate(row) for row in resp.json()]

    def get(self, kb_id: str) -> KnowledgeBaseRow:
        resp = self._client.get(f"/v1/knowledge-bases/{path_id(kb_id)}")
        raise_for_status(resp)
        return KnowledgeBaseRow.model_validate(resp.json())

    def delete(self, kb_id: str) -> bool:
        resp = self._client.delete(f"/v1/knowledge-bases/{path_id(kb_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    def list_documents(self, kb_id: str) -> list[KnowledgeBaseDocumentRow]:
        resp = self._client.get(f"/v1/knowledge-bases/{path_id(kb_id)}/documents")
        raise_for_status(resp)
        return [KnowledgeBaseDocumentRow.model_validate(row) for row in resp.json()]

    def get_document(self, kb_id: str, doc_id: str) -> KnowledgeBaseDocumentRow:
        resp = self._client.get(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}"
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentRow.model_validate(resp.json())

    def create_document(
        self, kb_id: str, params: DocumentCreateInput
    ) -> KnowledgeBaseDocumentCreateResult:
        resp = self._client.post(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents",
            json=dump_params(params, KnowledgeBaseDocumentCreateParams),
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentCreateResult.model_validate(resp.json())

    def finalize_document(self, kb_id: str, doc_id: str) -> KnowledgeBaseDocumentRow:
        resp = self._client.post(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}/finalize",
            json={},
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentRow.model_validate(resp.json())

    def delete_document(self, kb_id: str, doc_id: str) -> bool:
        resp = self._client.delete(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}"
        )
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    def upload_document(
        self, kb_id: str, params: DocumentUploadInput
    ) -> KnowledgeBaseDocumentRow:
        """Convenience wrapper: register a document, upload its bytes to the
        signed PUT URL the server mints, then finalize. Returns the document
        with status flipped to ``processing``. Call ``poll_document_ready``
        to wait for ingest completion.

        Example::

            doc = speko.knowledge_bases.upload_document(kb.id, {
                "filename": "faq.md",
                "content_type": "text/markdown",
                "data": Path("faq.md").read_bytes(),
            })
            ready = speko.knowledge_bases.poll_document_ready(kb.id, doc.id)
        """
        upload_params = (
            params
            if isinstance(params, KnowledgeBaseDocumentUploadParams)
            else KnowledgeBaseDocumentUploadParams.model_validate(params)
        )
        created = self.create_document(kb_id, _create_params_for(upload_params))
        # Bare request on purpose: the signed URL must not receive the Speko
        # Authorization header, only the headers the server minted.
        put_resp = httpx.request(
            created.upload.method,
            created.upload.url,
            content=upload_params.data,
            headers=created.upload.headers,
            timeout=_UPLOAD_TIMEOUT_SECONDS,
        )
        if put_resp.status_code >= 400:
            raise _upload_failed_error(put_resp)
        return self.finalize_document(kb_id, created.document.id)

    def poll_document_ready(
        self,
        kb_id: str,
        doc_id: str,
        *,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> KnowledgeBaseDocumentRow:
        """Poll a document until it reaches ``ready`` or ``failed`` status,
        or the timeout elapses. Raises ``SpekoApiError`` on ``failed`` (with
        the server's ``error_message``) or on timeout."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            doc = self.get_document(kb_id, doc_id)
            if doc.status == "ready":
                return doc
            if doc.status == "failed":
                raise _ingest_failed_error(doc)
            time.sleep(interval_seconds)
        raise _ingest_timeout_error(timeout_seconds)


class AsyncKnowledgeBasesResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def create(self, params: CreateInput) -> KnowledgeBaseRow:
        resp = await self._client.post(
            "/v1/knowledge-bases", json=dump_params(params, KnowledgeBaseCreateParams)
        )
        raise_for_status(resp)
        return KnowledgeBaseRow.model_validate(resp.json())

    async def list(self, *, agent_id: Optional[str] = None) -> list[KnowledgeBaseRow]:
        resp = await self._client.get(
            "/v1/knowledge-bases", params=query_string({"agentId": agent_id})
        )
        raise_for_status(resp)
        return [KnowledgeBaseRow.model_validate(row) for row in resp.json()]

    async def get(self, kb_id: str) -> KnowledgeBaseRow:
        resp = await self._client.get(f"/v1/knowledge-bases/{path_id(kb_id)}")
        raise_for_status(resp)
        return KnowledgeBaseRow.model_validate(resp.json())

    async def delete(self, kb_id: str) -> bool:
        resp = await self._client.delete(f"/v1/knowledge-bases/{path_id(kb_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    async def list_documents(self, kb_id: str) -> list[KnowledgeBaseDocumentRow]:
        resp = await self._client.get(f"/v1/knowledge-bases/{path_id(kb_id)}/documents")
        raise_for_status(resp)
        return [KnowledgeBaseDocumentRow.model_validate(row) for row in resp.json()]

    async def get_document(self, kb_id: str, doc_id: str) -> KnowledgeBaseDocumentRow:
        resp = await self._client.get(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}"
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentRow.model_validate(resp.json())

    async def create_document(
        self, kb_id: str, params: DocumentCreateInput
    ) -> KnowledgeBaseDocumentCreateResult:
        resp = await self._client.post(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents",
            json=dump_params(params, KnowledgeBaseDocumentCreateParams),
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentCreateResult.model_validate(resp.json())

    async def finalize_document(self, kb_id: str, doc_id: str) -> KnowledgeBaseDocumentRow:
        resp = await self._client.post(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}/finalize",
            json={},
        )
        raise_for_status(resp)
        return KnowledgeBaseDocumentRow.model_validate(resp.json())

    async def delete_document(self, kb_id: str, doc_id: str) -> bool:
        resp = await self._client.delete(
            f"/v1/knowledge-bases/{path_id(kb_id)}/documents/{path_id(doc_id)}"
        )
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    async def upload_document(
        self, kb_id: str, params: DocumentUploadInput
    ) -> KnowledgeBaseDocumentRow:
        """Register + upload + finalize in one call (async)."""
        upload_params = (
            params
            if isinstance(params, KnowledgeBaseDocumentUploadParams)
            else KnowledgeBaseDocumentUploadParams.model_validate(params)
        )
        created = await self.create_document(kb_id, _create_params_for(upload_params))
        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as bare:
            put_resp = await bare.request(
                created.upload.method,
                created.upload.url,
                content=upload_params.data,
                headers=created.upload.headers,
            )
        if put_resp.status_code >= 400:
            raise _upload_failed_error(put_resp)
        return await self.finalize_document(kb_id, created.document.id)

    async def poll_document_ready(
        self,
        kb_id: str,
        doc_id: str,
        *,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> KnowledgeBaseDocumentRow:
        """Poll a document until ``ready``/``failed``/timeout (async)."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            doc = await self.get_document(kb_id, doc_id)
            if doc.status == "ready":
                return doc
            if doc.status == "failed":
                raise _ingest_failed_error(doc)
            await asyncio.sleep(interval_seconds)
        raise _ingest_timeout_error(timeout_seconds)
