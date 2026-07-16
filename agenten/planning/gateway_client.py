"""Authenticated HTTP adapter for Captain planning releases."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agenten.planning.release import ReleaseConflictError
from agenten.validation.contracts import HoldoutSuite, WorkBatch


class GatewayPlanningError(RuntimeError):
    """A gateway planning operation failed without exposing credentials."""


class _BlockResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = Field(ge=0, strict=True)


class _CapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    artifact_ref: str | None = None


_CAPABILITIES = TypeAdapter(list[_CapabilityResponse])


class GatewayPlanningClient:
    """Publish immutable batches and query validated capability projections."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip():
            raise ValueError("gateway base_url must not be empty")
        if not token:
            raise ValueError("gateway token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        if batch.batch_id != holdouts.batch_id:
            raise ValueError("batch and holdout suite must have the same batch_id")

        parent = await self._post_block(
            {
                "block_type": "work_batch",
                "data": batch.model_dump(mode="json"),
            },
            operation="release work batch",
        )
        await self._post_block(
            {
                "block_type": "holdout",
                "parent_index": parent.index,
                "data": holdouts.model_dump(mode="json"),
            },
            operation="release holdout",
        )

    async def find_match(
        self,
        target: str,
        capability_tags: Sequence[str],
    ) -> str | None:
        need = " ".join([target, *sorted(capability_tags)])
        response = await self._request(
            "GET",
            "/capabilities",
            operation="query capabilities",
            params={"need": need},
        )
        self._require_status(response, {200}, operation="query capabilities")
        try:
            matches = _CAPABILITIES.validate_python(response.json())
        except (ValueError, ValidationError):
            raise GatewayPlanningError("query capabilities returned an invalid response") from None
        for match in matches:
            if match.artifact_ref:
                return match.artifact_ref
        return None

    async def _post_block(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> _BlockResponse:
        response = await self._request(
            "POST",
            "/blocks",
            operation=operation,
            json=payload,
        )
        if response.status_code == 409:
            raise ReleaseConflictError(f"{operation} failed with gateway status 409")
        self._require_status(response, {201}, operation=operation)
        try:
            return _BlockResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayPlanningError(f"{operation} returned an invalid response") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                **kwargs,
            )
        except httpx.HTTPError:
            raise GatewayPlanningError(f"{operation} could not reach the gateway") from None

    @staticmethod
    def _require_status(
        response: httpx.Response,
        expected: set[int],
        *,
        operation: str,
    ) -> None:
        if response.status_code not in expected:
            raise GatewayPlanningError(
                f"{operation} failed with gateway status {response.status_code}"
            )
