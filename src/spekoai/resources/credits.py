"""Prepaid credit balance + append-only ledger. Balance is reported in USD."""

from __future__ import annotations

from typing import Optional

import httpx

from spekoai._http import query_string, raise_for_status
from spekoai.models import CreditLedgerPage, OrganizationBalance


class CreditsResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get_balance(self) -> OrganizationBalance:
        """Current prepaid credit balance for the caller's org.

        Example::

            balance = speko.credits.get_balance()
            if balance.balance_usd < 0.5:
                print("Top up before running long sessions.")
        """
        resp = self._client.get("/v1/credits/balance")
        raise_for_status(resp)
        return OrganizationBalance.model_validate(resp.json())

    def get_ledger(
        self,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> CreditLedgerPage:
        """Most-recent-first page of credit movements.

        Pass back ``next_cursor`` as ``cursor`` to fetch the next page;
        ``next_cursor is None`` means the history is exhausted.
        """
        resp = self._client.get(
            "/v1/credits/ledger",
            params=query_string({"limit": limit, "cursor": cursor}),
        )
        raise_for_status(resp)
        return CreditLedgerPage.model_validate(resp.json())


class AsyncCreditsResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_balance(self) -> OrganizationBalance:
        """Current prepaid credit balance (async)."""
        resp = await self._client.get("/v1/credits/balance")
        raise_for_status(resp)
        return OrganizationBalance.model_validate(resp.json())

    async def get_ledger(
        self,
        *,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> CreditLedgerPage:
        """Most-recent-first page of credit movements (async)."""
        resp = await self._client.get(
            "/v1/credits/ledger",
            params=query_string({"limit": limit, "cursor": cursor}),
        )
        raise_for_status(resp)
        return CreditLedgerPage.model_validate(resp.json())
