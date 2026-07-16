"""Typed authenticated client for the gateway delivery lifecycle."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


BatchStatus: TypeAlias = Literal[
    "pending_review",
    "pending",
    "claimed",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
]
TerminalOutcome: TypeAlias = Literal[
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
]
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class GatewayDeliveryError(RuntimeError):
    """A delivery request failed without exposing bearer or claim tokens."""


class GatewayDeliveryConflictError(GatewayDeliveryError):
    """The gateway rejected a stale, invalid, or conflicting lifecycle write."""


class GatewayBatchProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1)
    parent_index: int = Field(ge=0, strict=True)
    status: BatchStatus
    claim_token_sha256: str | None = None
    claim_expires_at: datetime | None = None
    claim_iteration: int = Field(ge=0, strict=True)
    codex_session_recorded: bool
    validation_run_recorded: bool


class GatewayClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=1)
    expires_at: datetime
    iteration: int = Field(ge=1, strict=True)


class _ClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=1)
    claim_expires_at: datetime


class _HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_expires_at: datetime


class _BlockResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = Field(ge=0, strict=True)


class GatewayDeliveryClient:
    """Drive claims and fenced lifecycle writes through the sole-writer API."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip():
            raise ValueError("gateway base_url must not be empty")
        if not token:
            raise ValueError("gateway token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def get_batch(self, batch_id: str) -> GatewayBatchProjection:
        response = await self._request(
            "GET",
            f"/batches/{self._batch_path(batch_id)}",
            operation="read batch projection",
        )
        self._require_status(response, {200}, operation="read batch projection")
        return self._validate(
            GatewayBatchProjection,
            response,
            operation="read batch projection",
        )

    async def claim(self, batch_id: str) -> GatewayClaim:
        response = await self._request(
            "POST",
            f"/batches/{self._batch_path(batch_id)}/claim",
            operation="claim batch",
        )
        self._require_status(response, {200}, operation="claim batch")
        claimed = self._validate(_ClaimResponse, response, operation="claim batch")
        projection = await self.get_batch(batch_id)
        if projection.status != "claimed" or projection.claim_iteration < 1:
            raise GatewayDeliveryError("claim batch returned an inconsistent projection")
        return GatewayClaim(
            token=claimed.claim_token,
            expires_at=claimed.claim_expires_at,
            iteration=projection.claim_iteration,
        )

    async def heartbeat(self, batch_id: str, claim_token: str) -> datetime:
        response = await self._request(
            "POST",
            f"/batches/{self._batch_path(batch_id)}/claim/heartbeat",
            operation="renew claim",
            claim_token=claim_token,
        )
        self._require_status(response, {200}, operation="renew claim")
        heartbeat = self._validate(_HeartbeatResponse, response, operation="renew claim")
        return heartbeat.claim_expires_at

    async def record_codex_session(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        session_id: str,
    ) -> None:
        if iteration < 1:
            raise ValueError("iteration must be at least 1")
        if not session_id:
            raise ValueError("session_id must not be empty")
        await self._append_event(
            "codex_session",
            batch_id,
            claim_token,
            {"batch_id": batch_id, "iteration": iteration, "session_id": session_id},
        )

    async def record_validation(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        artifact_ref: str,
    ) -> None:
        if iteration < 1:
            raise ValueError("iteration must be at least 1")
        if not artifact_ref:
            raise ValueError("artifact_ref must not be empty")
        await self._append_event(
            "validation_run",
            batch_id,
            claim_token,
            {"batch_id": batch_id, "iteration": iteration, "artifact_ref": artifact_ref},
        )

    async def complete(
        self,
        batch_id: str,
        claim_token: str,
        *,
        outcome: TerminalOutcome,
    ) -> None:
        await self._append_event(
            "batch_done",
            batch_id,
            claim_token,
            {"batch_id": batch_id, "outcome": outcome},
            status=outcome,
        )

    async def _append_event(
        self,
        block_type: str,
        batch_id: str,
        claim_token: str,
        data: dict[str, Any],
        *,
        status: str = "recorded",
    ) -> None:
        response = await self._request(
            "POST",
            "/blocks",
            operation=f"append {block_type}",
            claim_token=claim_token,
            json={"block_type": block_type, "data": data, "status": status},
        )
        self._require_status(response, {201}, operation=f"append {block_type}")
        self._validate(_BlockResponse, response, operation=f"append {block_type}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        claim_token: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        if claim_token is not None:
            if not claim_token:
                raise ValueError("claim_token must not be empty")
            headers["X-Claim-Token"] = claim_token
        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError:
            raise GatewayDeliveryError(f"{operation} could not reach the gateway") from None

    @staticmethod
    def _batch_path(batch_id: str) -> str:
        if not batch_id:
            raise ValueError("batch_id must not be empty")
        return quote(batch_id, safe="")

    @staticmethod
    def _require_status(
        response: httpx.Response,
        expected: set[int],
        *,
        operation: str,
    ) -> None:
        if response.status_code in expected:
            return
        error_type = (
            GatewayDeliveryConflictError
            if response.status_code == 409
            else GatewayDeliveryError
        )
        raise error_type(f"{operation} failed with gateway status {response.status_code}")

    @staticmethod
    def _validate(
        model: type[ResponseModel],
        response: httpx.Response,
        *,
        operation: str,
    ) -> ResponseModel:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayDeliveryError(f"{operation} returned an invalid response") from None
