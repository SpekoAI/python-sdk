"""Usage summary resource."""

from __future__ import annotations

from typing import Optional

import httpx

from spekoai._http import query_string, raise_for_status
from spekoai.models import UsageSummary


class UsageResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Usage summary for the current billing period.

        Example::

            usage = speko.usage.get()
            print(usage.total_minutes, usage.total_cost)
        """
        resp = self._client.get(
            "/v1/usage", params=query_string({"from": from_date, "to": to_date})
        )
        raise_for_status(resp)
        return UsageSummary.model_validate(resp.json())


class AsyncUsageResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> UsageSummary:
        """Usage summary for the current billing period."""
        resp = await self._client.get(
            "/v1/usage", params=query_string({"from": from_date, "to": to_date})
        )
        raise_for_status(resp)
        return UsageSummary.model_validate(resp.json())
