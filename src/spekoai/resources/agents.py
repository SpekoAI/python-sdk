"""Per-org agent definitions — the system prompt, voice, intent, and routing
constraints used when an agent answers (or places) a call.

Every agent created via ``create`` auto-provisions a ``Default`` knowledge
base, so callers can upload documents through ``speko.knowledge_bases``
without an extra setup step.
"""

from __future__ import annotations

from typing import Any, Optional, Union

import httpx

from spekoai._http import dump_params, path_id, query_string, raise_for_status
from spekoai.models import (
    AgentCallListPage,
    AgentCreateParams,
    AgentRow,
    AgentToolCreateParams,
    AgentToolRow,
    AgentToolUpdateParams,
    AgentUpdateParams,
    ChatTool,
    PhoneNumberRow,
)

AgentCreateInput = Union[AgentCreateParams, dict[str, Any]]
AgentUpdateInput = Union[AgentUpdateParams, dict[str, Any]]
ToolCreateInput = Union[AgentToolCreateParams, dict[str, Any]]
ToolUpdateInput = Union[AgentToolUpdateParams, dict[str, Any]]


def _to_chat_tool(row: AgentToolRow) -> ChatTool:
    """Convert a serialized ``AgentToolRow`` into a ``ChatTool``. The row's
    ``source`` is structurally identical to ``ChatToolSource`` for every
    kind; ``execution_mode`` is derived from ``source.kind``."""
    return ChatTool.model_validate(
        {
            "name": row.name,
            "description": row.description,
            "parameters": row.parameters,
            "executionMode": row.source.kind,
            "source": row.source.model_dump(by_alias=True, exclude_none=True),
            "preToolSpeech": row.pre_tool_speech,
        }
    )


class AgentsResource:
    tools: AgentToolsResource

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self.tools = AgentToolsResource(client)

    def list(self) -> list[AgentRow]:
        resp = self._client.get("/v1/agents")
        raise_for_status(resp)
        return [AgentRow.model_validate(row) for row in resp.json()]

    def create(self, params: AgentCreateInput) -> AgentRow:
        """Create an agent.

        Example::

            agent = speko.agents.create({
                "name": "Support Bot",
                "system_prompt": "You are a helpful support agent for Acme.",
                "voice": "sophia",
                "intent": {"language": "en", "optimize_for": "latency"},
            })
        """
        resp = self._client.post("/v1/agents", json=dump_params(params, AgentCreateParams))
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    def get(self, agent_id: str) -> AgentRow:
        resp = self._client.get(f"/v1/agents/{path_id(agent_id)}")
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    def update(self, agent_id: str, params: AgentUpdateInput) -> AgentRow:
        resp = self._client.patch(
            f"/v1/agents/{path_id(agent_id)}", json=dump_params(params, AgentUpdateParams)
        )
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    def delete(self, agent_id: str) -> bool:
        resp = self._client.delete(f"/v1/agents/{path_id(agent_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    def attach_phone_number(self, agent_id: str, phone_number_id: str) -> PhoneNumberRow:
        """Bind a phone number to this agent so inbound calls hydrate the
        agent's pipeline config from the agent row. Internally calls
        ``PATCH /v1/phone-numbers/:id`` with ``{"agentId": agent_id}``."""
        resp = self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}", json={"agentId": agent_id}
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def detach_phone_number(self, phone_number_id: str) -> PhoneNumberRow:
        """Unlink a phone number from any agent. Inbound calls fall back to
        the number's ``dispatch_metadata_template`` (or fail if neither is
        configured)."""
        resp = self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}", json={"agentId": None}
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def list_calls(
        self,
        agent_id: str,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        since: Optional[str] = None,
    ) -> AgentCallListPage:
        """List recent calls for an agent. Use ``next_cursor`` from the
        returned page as ``cursor`` to page backward in time."""
        resp = self._client.get(
            f"/v1/agents/{path_id(agent_id)}/calls",
            params=query_string({"limit": limit, "cursor": cursor, "since": since}),
        )
        raise_for_status(resp)
        return AgentCallListPage.model_validate(resp.json())


class AgentToolsResource:
    """Per-agent tool definitions exposed to the LLM mid-call. Four execution
    modes: ``inline`` (caller runs the tool), ``webhook`` (Speko POSTs to your
    URL with a Standard-Webhooks signature), ``builtin`` (Speko-managed tools
    like ``search_knowledge_base``, ``transfer_call``, ``end_call``), and
    ``integration`` (an org-installed Speko app action such as Google
    Calendar or Slack).

    Webhook secrets are encrypted server-side at creation; the returned row
    carries a ``secret_ref`` pointer instead of the plaintext.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(self, agent_id: str, *, available: bool = False) -> list[AgentToolRow]:
        """List an agent's tools.

        When ``available=True``, the server returns only tools the agent can
        actually run right now — integration tools whose backing installation
        is disconnected/missing are omitted. Use this on the runtime path so
        the model is never offered a tool that would fail.
        """
        params = {"available": "1"} if available else None
        resp = self._client.get(f"/v1/agents/{path_id(agent_id)}/tools", params=params)
        raise_for_status(resp)
        return [AgentToolRow.model_validate(row) for row in resp.json()]

    def create(self, agent_id: str, params: ToolCreateInput) -> AgentToolRow:
        resp = self._client.post(
            f"/v1/agents/{path_id(agent_id)}/tools",
            json=dump_params(params, AgentToolCreateParams),
        )
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    def get(self, agent_id: str, tool_id: str) -> AgentToolRow:
        resp = self._client.get(f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}")
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    def update(self, agent_id: str, tool_id: str, params: ToolUpdateInput) -> AgentToolRow:
        resp = self._client.patch(
            f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}",
            json=dump_params(params, AgentToolUpdateParams),
        )
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    def delete(self, agent_id: str, tool_id: str) -> bool:
        resp = self._client.delete(f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    def list_chat_tools(self, agent_id: str, *, available: bool = False) -> list[ChatTool]:
        """Fetch this agent's registered tools and convert them into the
        ``ChatTool`` shape that ``speko.complete(tools=...)`` accepts. Covers
        all four source kinds — load once and pass the result straight to
        ``complete``."""
        return [_to_chat_tool(row) for row in self.list(agent_id, available=available)]


class AsyncAgentsResource:
    tools: AsyncAgentToolsResource

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.tools = AsyncAgentToolsResource(client)

    async def list(self) -> list[AgentRow]:
        resp = await self._client.get("/v1/agents")
        raise_for_status(resp)
        return [AgentRow.model_validate(row) for row in resp.json()]

    async def create(self, params: AgentCreateInput) -> AgentRow:
        resp = await self._client.post("/v1/agents", json=dump_params(params, AgentCreateParams))
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    async def get(self, agent_id: str) -> AgentRow:
        resp = await self._client.get(f"/v1/agents/{path_id(agent_id)}")
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    async def update(self, agent_id: str, params: AgentUpdateInput) -> AgentRow:
        resp = await self._client.patch(
            f"/v1/agents/{path_id(agent_id)}", json=dump_params(params, AgentUpdateParams)
        )
        raise_for_status(resp)
        return AgentRow.model_validate(resp.json())

    async def delete(self, agent_id: str) -> bool:
        resp = await self._client.delete(f"/v1/agents/{path_id(agent_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    async def attach_phone_number(self, agent_id: str, phone_number_id: str) -> PhoneNumberRow:
        resp = await self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}", json={"agentId": agent_id}
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def detach_phone_number(self, phone_number_id: str) -> PhoneNumberRow:
        resp = await self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}", json={"agentId": None}
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def list_calls(
        self,
        agent_id: str,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        since: Optional[str] = None,
    ) -> AgentCallListPage:
        resp = await self._client.get(
            f"/v1/agents/{path_id(agent_id)}/calls",
            params=query_string({"limit": limit, "cursor": cursor, "since": since}),
        )
        raise_for_status(resp)
        return AgentCallListPage.model_validate(resp.json())


class AsyncAgentToolsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(self, agent_id: str, *, available: bool = False) -> list[AgentToolRow]:
        params = {"available": "1"} if available else None
        resp = await self._client.get(f"/v1/agents/{path_id(agent_id)}/tools", params=params)
        raise_for_status(resp)
        return [AgentToolRow.model_validate(row) for row in resp.json()]

    async def create(self, agent_id: str, params: ToolCreateInput) -> AgentToolRow:
        resp = await self._client.post(
            f"/v1/agents/{path_id(agent_id)}/tools",
            json=dump_params(params, AgentToolCreateParams),
        )
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    async def get(self, agent_id: str, tool_id: str) -> AgentToolRow:
        resp = await self._client.get(f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}")
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    async def update(self, agent_id: str, tool_id: str, params: ToolUpdateInput) -> AgentToolRow:
        resp = await self._client.patch(
            f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}",
            json=dump_params(params, AgentToolUpdateParams),
        )
        raise_for_status(resp)
        return AgentToolRow.model_validate(resp.json())

    async def delete(self, agent_id: str, tool_id: str) -> bool:
        resp = await self._client.delete(
            f"/v1/agents/{path_id(agent_id)}/tools/{path_id(tool_id)}"
        )
        raise_for_status(resp)
        return bool(resp.json().get("deleted", False))

    async def list_chat_tools(self, agent_id: str, *, available: bool = False) -> list[ChatTool]:
        rows = await self.list(agent_id, available=available)
        return [_to_chat_tool(row) for row in rows]
