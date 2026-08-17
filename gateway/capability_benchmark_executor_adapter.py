"""Repository-owned benchmark-process executor bound only to Gateway."""

from __future__ import annotations

import json
import asyncio
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from agenten.agent_runtime.production_bootstrap import read_verified_repository_bytes

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
                 authority_id: UUID, repository_root: Path,
                 worker_sha256: str, repository_revision: str,
                 proxy_configuration: Mapping[str, object],
                 client: httpx.AsyncClient | None = None) -> None:
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
            or re.fullmatch(r"[0-9a-f]{64}", worker_sha256) is None
            or re.fullmatch(r"[0-9a-f]{40}", repository_revision) is None
        ):
            raise ValueError("benchmark Gateway authority is invalid")
        self.authority_id = authority_id
        root = repository_root.resolve(strict=True)
        read_verified_repository_bytes(
            "gateway/capability_benchmark_host_worker.py",
            expected_sha256=worker_sha256,
            expected_repository_revision=repository_revision,
            repository_root=root,
        )
        self._gateway_url = gateway_url.rstrip("/")
        self._token = gateway_token
        self._client = client or httpx.AsyncClient(timeout=300.0)
        self._repository_root = root
        self._proxy_configuration = dict(proxy_configuration)

    async def execute(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> GenericBenchmarkExecutionEvidence:
        expected = generic_execution_request_digest(
            job_id=request.job_id, capability_id=request.capability_id,
            input_ref=request.input_ref, case_ref=request.case_ref,
            team_kind=request.team_kind, model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            execution_case=request.execution_case,
            execution_plan=request.execution_plan,
            request_nonce=request.request_nonce,
        )
        if expected != request.request_digest:
            raise ValueError("benchmark execution request authority changed")
        payload = self._serialize_request(request)
        prepared_response = await self._client.post(
            self._gateway_url + "/v1/capability-benchmark/sessions/prepare",
            headers={"Authorization": "Bearer " + self._token},
            content=json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode(),
        )
        prepared_response.raise_for_status()
        prepared = prepared_response.json()
        result = await asyncio.to_thread(self._run_host, payload, prepared)
        completed = await self._client.post(
            self._gateway_url + "/v1/capability-benchmark/sessions/complete",
            headers={"Authorization": "Bearer " + self._token},
            json={
                "schema": "captain.capability-benchmark-completion.v1",
                "request": payload, "result": result,
            },
        )
        completed.raise_for_status()
        return GatewayIsolatedBenchmarkExecutorClient._parse_response(
            request, completed.content,
        )

    def _serialize_request(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> dict[str, object]:
        return {
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
            "execution_plan": {
                "candidate_ref": request.execution_plan.candidate_ref.model_dump(mode="json"),
                "candidate_manifest_ref": request.execution_plan.candidate_manifest_ref.model_dump(mode="json"),
                "team_manifest_ref": request.execution_plan.team_manifest_ref.model_dump(mode="json"),
                "baseline_policy_ref": request.execution_plan.baseline_policy_ref.model_dump(mode="json"),
                "model_ref": request.execution_plan.model_ref.model_dump(mode="json"),
                "execution_policy_ref": request.execution_plan.execution_policy_ref.model_dump(mode="json"),
                "team_agent_names": request.execution_plan.team_agent_names,
                "baseline_agent_name": request.execution_plan.baseline_agent_name,
            },
            "request_nonce": str(request.request_nonce),
            "request_digest": request.request_digest,
        }

    def _run_host(
        self, request: dict[str, object], prepared: dict[str, object],
    ) -> dict[str, object]:
        if prepared.get("schema") != "captain.capability-benchmark-prepared-session.v1" or prepared.get("request_digest") != request.get("request_digest"):
            raise ValueError("benchmark prepared authority is invalid")
        bootstrap = (
            "import runpy,sys;"
            f"sys.path.insert(0,{str(self._repository_root)!r});"
            "runpy.run_module('gateway.capability_benchmark_host_worker',run_name='__main__')"
        )
        environment = {
            key: value for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
            if (value := os.environ.get(key)) is not None
        }
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            (sys.executable, "-I", "-c", bootstrap),
            input=json.dumps({
                "request": request, "prepared": prepared,
                "configuration": {
                    **self._proxy_configuration,
                    "gateway_url": self._gateway_url,
                },
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            text=True, capture_output=True, timeout=300, check=False,
            cwd=self._repository_root, env=environment,
        )
        if completed.returncode != 0 or completed.stderr:
            raise ValueError("isolated HostAutoGen process failed")
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise ValueError("isolated HostAutoGen result is invalid")
        return value

    async def close(self) -> None:
        await self._client.aclose()


def build_capability_benchmark_executor(
    configuration: Mapping[str, str],
) -> GatewayOwnedBenchmarkExecutor:
    """Build from the process's strict, non-provider Gateway config only."""

    required = {
        "gateway_url", "gateway_token", "executor_authority_id",
        "repository_root", "repository_revision", "worker_sha256",
        "proxy_url", "proxy_token", "context_sha256", "policy_sha256",
        "price_card_sha256", "proxy_executable_sha256", "proxy_code_sha256",
        "paid_command_sha256", "hard_ceiling_enforced", "pricing_json",
    }
    if set(configuration) != required:
        raise ValueError("benchmark executor configuration is incomplete")
    try:
        authority_id = UUID(configuration["executor_authority_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("benchmark executor configuration is invalid") from None
    digests = (
        "context_sha256", "policy_sha256", "price_card_sha256",
        "proxy_executable_sha256", "proxy_code_sha256", "paid_command_sha256",
    )
    if any(re.fullmatch(r"[0-9a-f]{64}", configuration[name]) is None for name in digests) or configuration["hard_ceiling_enforced"] != "true":
        raise ValueError("benchmark executor configuration is invalid")
    try:
        pricing = json.loads(configuration["pricing_json"])
    except json.JSONDecodeError:
        raise ValueError("benchmark executor configuration is invalid") from None
    if not isinstance(pricing, dict):
        raise ValueError("benchmark executor configuration is invalid")
    return GatewayOwnedBenchmarkExecutor(
        gateway_url=configuration["gateway_url"],
        gateway_token=configuration["gateway_token"],
        authority_id=authority_id,
        repository_root=Path(configuration["repository_root"]),
        repository_revision=configuration["repository_revision"],
        worker_sha256=configuration["worker_sha256"],
        proxy_configuration={
            "proxy_url": configuration["proxy_url"],
            "proxy_token": configuration["proxy_token"],
            "context_sha256": configuration["context_sha256"],
            "policy_sha256": configuration["policy_sha256"],
            "price_card_sha256": configuration["price_card_sha256"],
            "proxy_executable_sha256": configuration["proxy_executable_sha256"],
            "proxy_code_sha256": configuration["proxy_code_sha256"],
            "paid_command_sha256": configuration["paid_command_sha256"],
            "hard_ceiling_enforced": True, "pricing": pricing,
        },
    )


__all__ = [
    "GatewayOwnedBenchmarkExecutor",
    "build_capability_benchmark_executor",
]
