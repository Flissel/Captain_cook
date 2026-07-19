"""Typed authenticated client for the gateway delivery lifecycle."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gateway.contracts import DeliveryEventEnvelope

if TYPE_CHECKING:
    from agenten.delivery.recovery import RecoveryDecision
    from agenten.review.gateway_controller import GatewayReviewDecision


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
    claim_id: str | None = None
    fencing_token: int | None = None
    claim_expires_at: datetime | None = None
    claim_iteration: int = Field(ge=0, strict=True)
    codex_session_recorded: bool
    validation_run_recorded: bool
    recovery_recorded: bool = False
    recovered_iteration: int | None = Field(default=None, ge=1, strict=True)
    passing_review_recorded: bool = False
    failed_review_count: int = Field(default=0, ge=0, strict=True)


class GatewayClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1, strict=True)
    expires_at: datetime
    iteration: int = Field(ge=1, strict=True)


class GatewayActiveCodexSession(BaseModel):
    """Gateway-derived trace needed for host-local terminal evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1, strict=True)
    session_id: str = Field(min_length=1)
    iteration: int = Field(ge=1, strict=True)
    process_ref: str = Field(pattern=r"^artifact://")
    started_at: datetime


class GatewayEvidence(BaseModel):
    """Closed evidence union accepted by the current gateway projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["codex_session", "validation_run"]
    iteration: int = Field(ge=1, strict=True)
    session_id: str | None = Field(default=None, min_length=1)
    artifact_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_kind_payload(self) -> "GatewayEvidence":
        if self.kind == "codex_session":
            if self.session_id is None:
                raise ValueError("codex_session evidence requires session_id")
            if self.artifact_ref is not None:
                raise ValueError("codex_session evidence must not include artifact_ref")
        else:
            if self.artifact_ref is None:
                raise ValueError("validation_run evidence requires artifact_ref")
            if self.session_id is not None:
                raise ValueError("validation_run evidence must not include session_id")
        return self

    def event_data(self, batch_id: str) -> dict[str, Any]:
        data: dict[str, Any] = {"batch_id": batch_id, "iteration": self.iteration}
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.artifact_ref is not None:
            data["artifact_ref"] = self.artifact_ref
        return data


class _ClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1, strict=True)
    claim_expires_at: datetime


class _HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_expires_at: datetime


class _BlockResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = Field(ge=0, strict=True)


class _AppendDeliveryEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: DeliveryEventEnvelope
    replayed: bool


class _BatchListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1)
    title: str


class _ReasoningSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1)
    iteration: int = Field(ge=1, strict=True)
    slice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    summary_ref: str = Field(pattern=r"^artifact://[^\\]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _RecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=1, strict=True)
    reason: Literal["claim_expired"]
    decision: Literal["requeue", "aborted_infra"]


class _ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=1, strict=True)
    review_id: str = Field(min_length=1, max_length=128)
    decision: Literal["passed", "failed"]
    evidence_refs: tuple[str, ...] = Field(min_length=1)


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

    async def append_delivery_event(
        self,
        event: DeliveryEventEnvelope,
    ) -> DeliveryEventEnvelope:
        appended, _ = await self.append_delivery_event_with_receipt(event)
        return appended

    async def append_delivery_event_with_receipt(
        self,
        event: DeliveryEventEnvelope,
    ) -> tuple[DeliveryEventEnvelope, bool]:
        """Append one idempotent event and retain whether Gateway replayed it."""

        response = await self._request(
            "POST",
            "/v1/delivery/events",
            operation=f"append {event.event_type} delivery event",
            json=event.model_dump(mode="json"),
        )
        self._require_status(
            response,
            {200, 201},
            operation=f"append {event.event_type} delivery event",
        )
        appended = self._validate(
            _AppendDeliveryEventResponse,
            response,
            operation=f"append {event.event_type} delivery event",
        )
        return appended.event, appended.replayed

    async def delivery_events(
        self,
        *,
        project_id: str,
        run_id: str,
    ) -> tuple[DeliveryEventEnvelope, ...]:
        response = await self._request(
            "GET",
            f"/v1/projects/{quote(project_id, safe='')}/runs/{quote(run_id, safe='')}/events",
            operation="read delivery events",
        )
        self._require_status(response, {200}, operation="read delivery events")
        try:
            return tuple(
                DeliveryEventEnvelope.model_validate(item)
                for item in response.json()
            )
        except (TypeError, ValueError, ValidationError):
            raise GatewayDeliveryError(
                "read delivery events returned an invalid response"
            ) from None

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

    async def list_batches(self, status: str) -> tuple[str, ...]:
        if not status:
            raise ValueError("status must not be empty")
        response = await self._request(
            "GET",
            "/batches",
            operation="list batches",
            params={"status": status},
        )
        self._require_status(response, {200}, operation="list batches")
        try:
            items = [_BatchListItem.model_validate(item) for item in response.json()]
        except (TypeError, ValueError, ValidationError):
            raise GatewayDeliveryError("list batches returned an invalid response") from None
        return tuple(item.batch_id for item in items)

    async def active_codex_sessions(
        self,
        batch_id: str,
    ) -> tuple[GatewayActiveCodexSession, ...]:
        response = await self._request(
            "GET",
            f"/batches/{self._batch_path(batch_id)}/active-codex-sessions",
            operation="read active Codex sessions",
        )
        self._require_status(response, {200}, operation="read active Codex sessions")
        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("active Codex session response must be an array")
            return tuple(GatewayActiveCodexSession.model_validate(item) for item in payload)
        except (TypeError, ValueError, ValidationError):
            raise GatewayDeliveryError(
                "read active Codex sessions returned an invalid response"
            ) from None

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
            claim_id=claimed.claim_id,
            fencing_token=claimed.fencing_token,
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
        await self.append_evidence(
            batch_id,
            claim_token,
            GatewayEvidence(
                kind="codex_session",
                iteration=iteration,
                session_id=session_id,
            ),
        )

    async def record_validation(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        artifact_ref: str,
    ) -> None:
        await self.append_evidence(
            batch_id,
            claim_token,
            GatewayEvidence(
                kind="validation_run",
                iteration=iteration,
                artifact_ref=artifact_ref,
            ),
        )

    async def record_reasoning_slice(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        slice_id: str,
        summary_ref: str,
        sha256: str,
    ) -> None:
        event = _ReasoningSlice(
            batch_id=batch_id,
            iteration=iteration,
            slice_id=slice_id,
            summary_ref=summary_ref,
            sha256=sha256,
        )
        await self._append_event(
            "reasoning_slice",
            batch_id,
            claim_token,
            event.model_dump(mode="json"),
        )

    async def record_recovery(self, decision: "RecoveryDecision") -> "RecoveryDecision":
        from agenten.delivery.recovery import RecoveryDecision

        response = await self._request(
            "POST",
            f"/batches/{self._batch_path(decision.batch_id)}/recovery",
            operation="record recovery decision",
            json=decision.model_dump(mode="json"),
        )
        self._require_status(response, {201}, operation="record recovery decision")
        validated = self._validate(
            _RecoveryResponse,
            response,
            operation="record recovery decision",
        )
        return RecoveryDecision.model_validate(validated.model_dump())

    async def record_review(
        self, decision: "GatewayReviewDecision"
    ) -> "GatewayReviewDecision":
        from agenten.review.gateway_controller import GatewayReviewDecision

        response = await self._request(
            "POST",
            f"/batches/{self._batch_path(decision.batch_id)}/review",
            operation="record review decision",
            json=decision.model_dump(mode="json"),
        )
        self._require_status(response, {201}, operation="record review decision")
        validated = self._validate(
            _ReviewResponse,
            response,
            operation="record review decision",
        )
        return GatewayReviewDecision.model_validate(validated.model_dump())

    async def record_codex_process(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        process_id: str,
        state: Literal["started", "heartbeat", "exited", "cancelled"],
        command_digest: str,
    ) -> None:
        if not process_id:
            raise ValueError("process_id must not be empty")
        if len(command_digest) != 64 or any(
            char not in "0123456789abcdef" for char in command_digest
        ):
            raise ValueError("command_digest must be a sha256 hex digest")
        await self._append_event(
            "codex_process",
            batch_id,
            claim_token,
            {
                "batch_id": batch_id,
                "iteration": iteration,
                "process_id": process_id,
                "state": state,
                "command_digest": command_digest,
            },
        )

    async def append_evidence(
        self,
        batch_id: str,
        claim_token: str,
        evidence: GatewayEvidence,
    ) -> None:
        await self._append_event(
            evidence.kind,
            batch_id,
            claim_token,
            evidence.event_data(batch_id),
        )

    async def complete(
        self,
        batch_id: str,
        claim_token: str,
        *,
        outcome: TerminalOutcome,
        capabilities: Sequence[str] = (),
        artifact_ref: str | None = None,
        target: str | None = None,
        runtime: str | None = None,
        runtime_version: str | None = None,
        interface_schema: str | None = None,
    ) -> None:
        canonical_capabilities = sorted(set(capabilities))
        if any(not capability for capability in canonical_capabilities):
            raise ValueError("capabilities must not contain empty values")
        if artifact_ref == "":
            raise ValueError("artifact_ref must not be empty")
        compatibility = (
            artifact_ref,
            target,
            runtime,
            runtime_version,
            interface_schema,
        )
        publishes_capability = bool(canonical_capabilities) or any(
            value is not None for value in compatibility
        )
        if publishes_capability and (
            not canonical_capabilities or any(not value for value in compatibility)
        ):
            raise ValueError(
                "validated artifact publication requires complete compatibility metadata"
            )
        data: dict[str, Any] = {"batch_id": batch_id, "outcome": outcome}
        if publishes_capability:
            data["capabilities"] = canonical_capabilities
            data["target"] = target
            data["runtime"] = runtime
            data["runtime_version"] = runtime_version
            data["interface_schema"] = interface_schema
        if artifact_ref is not None:
            data["artifact_ref"] = artifact_ref
        await self._append_event(
            "batch_done",
            batch_id,
            claim_token,
            data,
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
