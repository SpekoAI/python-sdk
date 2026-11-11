"""Internal HTTP helpers shared by the client and resource classes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any, Optional, TypeVar, Union
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from spekoai.errors import SpekoApiError, SpekoAuthError, SpekoRateLimitError

ModelT = TypeVar("ModelT", bound=BaseModel)


def path_id(value: str) -> str:
    """Percent-encode a path segment (mirror of JS encodeURIComponent)."""
    return quote(value, safe="")


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        data = resp.json()
        message = data.get("error", resp.text)
        code = data.get("code", "UNKNOWN")
    except Exception:
        try:
            message = resp.text or resp.reason_phrase
        except Exception:
            message = resp.reason_phrase
        code = "UNKNOWN"
    if resp.status_code == 401:
        raise SpekoAuthError(message)
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After")
        retry_after = int(retry) if retry is not None and retry.isdigit() else None
        raise SpekoRateLimitError(message, retry_after)
    raise SpekoApiError(message, resp.status_code, code)


def raise_for_status_streamed(resp: httpx.Response) -> None:
    """Error check for streamed responses — reads the body first so the
    error payload is available to ``raise_for_status``."""
    if resp.status_code < 400:
        return
    resp.read()
    raise_for_status(resp)


async def araise_for_status_streamed(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    await resp.aread()
    raise_for_status(resp)


def decode_sse_block(block: str) -> tuple[str, Any]:
    event = "message"
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    raw_data = "\n".join(data_lines)
    try:
        data: Any = json.loads(raw_data)
    except Exception:
        data = raw_data
    return event, data


def decode_sse_block_with_id(block: str) -> tuple[str, Optional[str], Any]:
    """Decode an SSE frame while preserving its resumable ``id`` field."""
    event, data = decode_sse_block(block)
    event_id: Optional[str] = None
    for line in block.splitlines():
        if line.startswith("id:"):
            event_id = line[len("id:") :].strip() or None
    return event, event_id, data


def iter_sse(chunks: Iterable[str]) -> Iterator[tuple[str, Any]]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if block.strip():
                yield decode_sse_block(block)
    if buffer.strip():
        yield decode_sse_block(buffer)


async def aiter_sse(chunks: AsyncIterator[str]) -> AsyncIterator[tuple[str, Any]]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if block.strip():
                yield decode_sse_block(block)
    if buffer.strip():
        yield decode_sse_block(buffer)


def iter_sse_with_id(chunks: Iterable[str]) -> Iterator[tuple[str, Optional[str], Any]]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if block.strip():
                yield decode_sse_block_with_id(block)
    if buffer.strip():
        yield decode_sse_block_with_id(buffer)


async def aiter_sse_with_id(
    chunks: AsyncIterator[str],
) -> AsyncIterator[tuple[str, Optional[str], Any]]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if block.strip():
                yield decode_sse_block_with_id(block)
    if buffer.strip():
        yield decode_sse_block_with_id(buffer)


def dump_params(params: Union[ModelT, dict[str, Any]], model_cls: type[ModelT]) -> dict[str, Any]:
    """Serialize a request-params model (or plain dict) to its wire shape.

    ``exclude_unset`` (not ``exclude_none``) so explicitly-passed ``None``
    values survive — e.g. ``{"agent_id": None}`` on a phone-number update
    means "unlink the agent" and must reach the server as ``agentId: null``.
    """
    model = params if isinstance(params, model_cls) else model_cls.model_validate(params)
    return model.model_dump(by_alias=True, exclude_unset=True)


def query_string(params: dict[str, Optional[object]]) -> dict[str, str]:
    """Build query params, dropping Nones and stringifying the rest."""
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "1" if value else "0"
        else:
            out[key] = str(value)
    return out
