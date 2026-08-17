"""Gateway-side client for the isolated Factory runtime process."""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Literal
from urllib import request as urllib_request
from uuid import NAMESPACE_URL, UUID, uuid5
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.production_dispatch_runner import ProductionFactoryDispatchResult
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.state_machine import FactoryActionKind
from gateway.factory_repository import GatewayFactoryRepository


class _FactoryProcessExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_name: Literal["captain.factory-process-execution.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    job_id: UUID
    correlation_id: UUID
    input_id: str
    attempt: int
    operation: Literal["run"]
    request_id: UUID
    idempotency_key: str
    expected_revision: int
    maximum_dispatches: int
    stop_before_action: FactoryActionKind | None = None


class GatewayFactoryProcessClient:
    """Read through Gateway; dispatch role effects only in the child process."""

    def __init__(
        self, *, repository: GatewayFactoryRepository, process_url: str,
        factory_runtime_token: str, candidate_resolver: object,
        send: Callable[[dict[str, object]], dict[str, object]] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if (
            not process_url.startswith("http://127.0.0.1:")
            or process_url.endswith("/") or len(factory_runtime_token) < 16
            or not callable(getattr(candidate_resolver, "candidate_for", None))
        ):
            raise ValueError("Factory process client configuration is invalid")
        self.repository = repository
        self._coordinator = FactoryCoordinator(repository)
        self._url = process_url + "/v1/execute"
        self._token = factory_runtime_token
        self._candidate_resolver = candidate_resolver
        self._send_override = send
        self._timeout = timeout_seconds

    def projection(self, job_id: UUID):
        return self._coordinator.projection(job_id)

    def snapshot(self, job_id: UUID):
        return self.repository.snapshot(job_id)

    def workflow_artifacts(self, job_id: UUID):
        return self.repository.workflow_artifacts(job_id)

    def workflow_budget_projection(self, job_id: UUID):
        return self.repository.workflow_budget_projection(job_id)

    def business_benchmark_summary_by_artifact(self, reference):
        return self.repository.business_benchmark_summary_by_artifact(reference)

    def runtime_operation(self, command_id: UUID):
        return self.repository.runtime_operation(command_id)

    def benchmark_candidate_for(self, job):
        return self._candidate_resolver.candidate_for(job)

    def record_captain_transition(self, block) -> None:
        self._coordinator.record(block)

    async def run(
        self, job_id: UUID, *, maximum_dispatches: int = 12,
        stop_before_action: FactoryActionKind | None = None,
    ) -> ProductionFactoryDispatchResult:
        snapshot = self.repository.snapshot(job_id)
        job = snapshot.job
        next_action = self._coordinator.next_action(job_id)
        attempt = next_action.attempt
        revision = 1 + len(snapshot.blocks) + len(snapshot.leases) + len(
            self.repository.workflow_artifacts(job_id)
        )
        identity = "|".join((
            str(job_id), str(job.correlation_id), str(attempt),
            str(maximum_dispatches),
            "" if stop_before_action is None else stop_before_action.value,
            str(revision),
        ))
        key = hashlib.sha256(identity.encode()).hexdigest()
        command = _FactoryProcessExecutionRequest(
            schema="captain.factory-process-execution.v1",
            job_id=job_id, correlation_id=job.correlation_id,
            input_id=job.required_capability, attempt=attempt, operation="run",
            request_id=uuid5(NAMESPACE_URL, "factory-process|" + key),
            idempotency_key=key, expected_revision=revision,
            maximum_dispatches=maximum_dispatches,
            stop_before_action=stop_before_action,
        )
        raw = (
            self._send_override(command.model_dump(mode="json", by_alias=True))
            if self._send_override is not None else self._send_http(command)
        )
        if (
            raw.get("schema") != "captain.factory-process-result.v1"
            or raw.get("request_id") != str(command.request_id)
            or raw.get("job_id") != str(job_id)
            or not isinstance(raw.get("result"), dict)
        ):
            raise RuntimeError("Factory process response binding changed")
        return ProductionFactoryDispatchResult.model_validate(raw["result"])

    def _send_http(self, command: _FactoryProcessExecutionRequest) -> dict[str, object]:
        outbound = urllib_request.Request(
            self._url,
            data=command.model_dump_json(by_alias=True).encode(), method="POST",
            headers={"Authorization": "Bearer " + self._token,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(outbound, timeout=self._timeout) as response:
                value = json.loads(response.read())
        except Exception:
            raise RuntimeError("Factory process is unavailable") from None
        if not isinstance(value, dict):
            raise RuntimeError("Factory process response is invalid")
        return value


__all__ = ["GatewayFactoryProcessClient"]
