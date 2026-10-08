"""Phone numbers Speko has provisioned as managed numbers or registered from
your own SIP trunk. Each number can be used for outbound dialing and/or
inbound, and carries an optional metadata template that's merged into the
worker dispatch payload when the number is used."""

from __future__ import annotations

from typing import Any, Optional, Union

import httpx

from spekoai._http import dump_params, path_id, query_string, raise_for_status
from spekoai.models import (
    AvailablePhoneNumber,
    PhoneNumberCreateParams,
    PhoneNumberImportSipTrunkParams,
    PhoneNumberKybDraftParams,
    PhoneNumberKybMinimalSubmitParams,
    PhoneNumberKybOverview,
    PhoneNumberKybSubmission,
    PhoneNumberKybSubmitParams,
    PhoneNumberRow,
    PhoneNumberUpdateParams,
)

CreateInput = Union[PhoneNumberCreateParams, dict[str, Any]]
ImportInput = Union[PhoneNumberImportSipTrunkParams, dict[str, Any]]
UpdateInput = Union[PhoneNumberUpdateParams, dict[str, Any]]
KybDraftInput = Union[PhoneNumberKybDraftParams, dict[str, Any]]
KybSubmitInput = Union[
    PhoneNumberKybMinimalSubmitParams, PhoneNumberKybSubmitParams, dict[str, Any]
]


def _dump_kyb_submit(params: KybSubmitInput) -> dict[str, Any]:
    if isinstance(params, (PhoneNumberKybMinimalSubmitParams, PhoneNumberKybSubmitParams)):
        return params.model_dump(by_alias=True, exclude_unset=True)
    model_cls = (
        PhoneNumberKybMinimalSubmitParams
        if "declaration" in params
        else PhoneNumberKybSubmitParams
    )
    return model_cls.model_validate(params).model_dump(by_alias=True, exclude_unset=True)


class PhoneNumbersResource:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def list(self) -> list[PhoneNumberRow]:
        resp = self._client.get("/v1/phone-numbers")
        raise_for_status(resp)
        return [PhoneNumberRow.model_validate(row) for row in resp.json()]

    def search_available(
        self,
        *,
        area_code: Optional[str] = None,
        locality: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[AvailablePhoneNumber]:
        """Search the platform-managed pool for orderable US numbers.

        Filter by 3-digit area code and/or locality. Results include cost so
        you can preview "$1 upfront + $2/month" before committing to
        ``create``.
        """
        resp = self._client.get(
            "/v1/phone-numbers/available",
            params=query_string(
                {"areaCode": area_code, "locality": locality, "limit": limit}
            ),
        )
        raise_for_status(resp)
        return [AvailablePhoneNumber.model_validate(row) for row in resp.json()]

    def get(self, phone_number_id: str) -> PhoneNumberRow:
        resp = self._client.get(f"/v1/phone-numbers/{path_id(phone_number_id)}")
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def create(self, params: CreateInput) -> PhoneNumberRow:
        resp = self._client.post(
            "/v1/phone-numbers", json=dump_params(params, PhoneNumberCreateParams)
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def import_sip_trunk(self, params: ImportInput) -> PhoneNumberRow:
        resp = self._client.post(
            "/v1/phone-numbers/import",
            json=dump_params(params, PhoneNumberImportSipTrunkParams),
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def update(self, phone_number_id: str, params: UpdateInput) -> PhoneNumberRow:
        """Update a number. Pass ``{"agent_id": None}`` explicitly to unlink
        the agent, a string to relink."""
        resp = self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}",
            json=dump_params(params, PhoneNumberUpdateParams),
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    def delete(self, phone_number_id: str) -> bool:
        resp = self._client.delete(f"/v1/phone-numbers/{path_id(phone_number_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("released", False))

    def get_kyb(self) -> PhoneNumberKybOverview:
        """Read business verification state used to gate managed phone-number
        purchases. Includes the latest submission plus any SMS 10DLC-derived
        prefill."""
        resp = self._client.get("/v1/phone-numbers/kyb")
        raise_for_status(resp)
        return PhoneNumberKybOverview.model_validate(resp.json())

    def save_kyb_draft(self, params: KybDraftInput) -> PhoneNumberKybSubmission:
        """Save a draft business verification submission without sending it
        for review."""
        resp = self._client.put(
            "/v1/phone-numbers/kyb/draft",
            json=dump_params(params, PhoneNumberKybDraftParams),
        )
        raise_for_status(resp)
        return PhoneNumberKybSubmission.model_validate(resp.json())

    def submit_kyb(self, params: KybSubmitInput) -> PhoneNumberKybSubmission:
        """Submit business verification for review. ``attestation_accepted``
        must be ``True``."""
        resp = self._client.post(
            "/v1/phone-numbers/kyb/submit",
            json=_dump_kyb_submit(params),
        )
        raise_for_status(resp)
        return PhoneNumberKybSubmission.model_validate(resp.json())


class AsyncPhoneNumbersResource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def list(self) -> list[PhoneNumberRow]:
        resp = await self._client.get("/v1/phone-numbers")
        raise_for_status(resp)
        return [PhoneNumberRow.model_validate(row) for row in resp.json()]

    async def search_available(
        self,
        *,
        area_code: Optional[str] = None,
        locality: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[AvailablePhoneNumber]:
        resp = await self._client.get(
            "/v1/phone-numbers/available",
            params=query_string(
                {"areaCode": area_code, "locality": locality, "limit": limit}
            ),
        )
        raise_for_status(resp)
        return [AvailablePhoneNumber.model_validate(row) for row in resp.json()]

    async def get(self, phone_number_id: str) -> PhoneNumberRow:
        resp = await self._client.get(f"/v1/phone-numbers/{path_id(phone_number_id)}")
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def create(self, params: CreateInput) -> PhoneNumberRow:
        resp = await self._client.post(
            "/v1/phone-numbers", json=dump_params(params, PhoneNumberCreateParams)
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def import_sip_trunk(self, params: ImportInput) -> PhoneNumberRow:
        resp = await self._client.post(
            "/v1/phone-numbers/import",
            json=dump_params(params, PhoneNumberImportSipTrunkParams),
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def update(self, phone_number_id: str, params: UpdateInput) -> PhoneNumberRow:
        resp = await self._client.patch(
            f"/v1/phone-numbers/{path_id(phone_number_id)}",
            json=dump_params(params, PhoneNumberUpdateParams),
        )
        raise_for_status(resp)
        return PhoneNumberRow.model_validate(resp.json())

    async def delete(self, phone_number_id: str) -> bool:
        resp = await self._client.delete(f"/v1/phone-numbers/{path_id(phone_number_id)}")
        raise_for_status(resp)
        return bool(resp.json().get("released", False))

    async def get_kyb(self) -> PhoneNumberKybOverview:
        resp = await self._client.get("/v1/phone-numbers/kyb")
        raise_for_status(resp)
        return PhoneNumberKybOverview.model_validate(resp.json())

    async def save_kyb_draft(self, params: KybDraftInput) -> PhoneNumberKybSubmission:
        resp = await self._client.put(
            "/v1/phone-numbers/kyb/draft",
            json=dump_params(params, PhoneNumberKybDraftParams),
        )
        raise_for_status(resp)
        return PhoneNumberKybSubmission.model_validate(resp.json())

    async def submit_kyb(self, params: KybSubmitInput) -> PhoneNumberKybSubmission:
        resp = await self._client.post(
            "/v1/phone-numbers/kyb/submit",
            json=_dump_kyb_submit(params),
        )
        raise_for_status(resp)
        return PhoneNumberKybSubmission.model_validate(resp.json())
