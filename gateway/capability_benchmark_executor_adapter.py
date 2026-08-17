"""Repository-owned benchmark-process executor bound only to Gateway."""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from gateway.capability_benchmark_process_executor import (
    GatewayIsolatedBenchmarkExecutorClient,
)
from gateway.capability_generic_benchmark import (
    GenericBenchmarkExecutionEvidence,
    GenericBenchmarkExecutionRequest,
    generic_execution_request_digest,
)


class GatewayOwnedBenchmarkExecutor:
    """Ask the authenticated Gateway authority to execute one bound case."""

    def __init__(self, *, gateway_url: str, gateway_token: str,
                 authority_id: UUID, client: httpx.AsyncClient | None = None) -> None:
        parsed = urlsplit(gateway_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(gateway_token) < 16
            or not isinstance(authority_id, UUID)
        ):
            raise ValueError("benchmark Gateway authority is invalid")
        self.authority_id = authority_id
        self._endpoint = gateway_url.rstrip("/") + "/v1/capability-benchmark/executions"
        self._token = gateway_token
        self._client = client or httpx.AsyncClient(timeout=300.0)

    async def execute(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> GenericBenchmarkExecutionEvidence:
        expected = generic_execution_request_digest(
            job_id=request.job_id, capability_id=request.capability_id,
            input_ref=request.input_ref, case_ref=request.case_ref,
            team_kind=request.team_kind, model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            execution_case=request.execution_case,
            request_nonce=request.request_nonce,
        )
        if expected != request.request_digest:
            raise ValueError("benchmark execution request authority changed")
        payload = {
            "schema": "captain.capability-benchmark-execution-request.v1",
            "job_id": str(request.job_id),
            "capability_id": request.capability_id,
            "input_ref": request.input_ref.model_dump(mode="json"),
            "case": {
                "case_id": request.case_ref.case_id,
                "category": request.case_ref.category.value,
                "case_digest": request.case_ref.case_digest,
            },
            "team_kind": request.team_kind,
            "model_id": request.model_id,
            "model_policy_digest": request.model_policy_digest,
            "execution_case": {
                "setup": request.execution_case.setup,
                "action": request.execution_case.action,
                "opaque_source_refs": request.execution_case.opaque_source_refs,
                "declared_tool_keys": request.execution_case.declared_tool_keys,
                "tool_authority_sha256": request.execution_case.tool_authority_sha256,
            },
            "request_nonce": str(request.request_nonce),
            "request_digest": request.request_digest,
        }
        response = await self._client.post(
            self._endpoint,
            headers={"Authorization": "Bearer " + self._token},
            content=json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode(),
        )
        response.raise_for_status()
        return GatewayIsolatedBenchmarkExecutorClient._parse_response(
            request, response.content,
        )

    async def close(self) -> None:
        await self._client.aclose()


def build_capability_benchmark_executor(
    configuration: Mapping[str, str],
) -> GatewayOwnedBenchmarkExecutor:
    """Build from the process's strict, non-provider Gateway config only."""

    required = {
        "gateway_url", "gateway_token", "executor_authority_id",
    }
    if set(configuration) != required:
        raise ValueError("benchmark executor configuration is incomplete")
    try:
        authority_id = UUID(configuration["executor_authority_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("benchmark executor configuration is invalid") from None
    return GatewayOwnedBenchmarkExecutor(
        gateway_url=configuration["gateway_url"],
        gateway_token=configuration["gateway_token"],
        authority_id=authority_id,
    )


__all__ = [
    "GatewayOwnedBenchmarkExecutor",
    "build_capability_benchmark_executor",
]
