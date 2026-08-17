"""Authenticated process boundary for generic capability benchmark execution."""

from __future__ import annotations

import secrets
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID

import uvicorn

from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    load_verified_repository_module,
)

from fastapi import FastAPI, Header, HTTPException, status

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_generic_benchmark import (
    GenericBenchmarkCategory,
    GenericBenchmarkExecutionEvidence,
    GenericBenchmarkExecutionRequest,
    GenericExecutionCaseView,
    GenericPrivateCaseRef,
    generic_execution_request_digest,
)


class CapabilityBenchmarkProcessExecutor(Protocol):
    async def execute(
        self, request: GenericBenchmarkExecutionRequest
    ) -> GenericBenchmarkExecutionEvidence: ...


def create_capability_benchmark_process_app(
    *,
    executor: CapabilityBenchmarkProcessExecutor,
    bearer_token: str,
    authority_id: UUID,
) -> FastAPI:
    if len(bearer_token) < 16 or not isinstance(authority_id, UUID):
        raise ValueError("benchmark process authority is invalid")
    app = FastAPI(
        title="Captain Capability Benchmark Process",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(value: str | None) -> None:
        if value is None or not secrets.compare_digest(
            value, f"Bearer {bearer_token}"
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="benchmark process authentication failed",
            )

    @app.get("/healthz")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, str]:
        authorize(authorization)
        return {
            "schema": "captain.capability-benchmark-process-health.v1",
            "status": "ready",
            "authority_id": str(authority_id),
        }

    @app.post("/v1/execute")
    async def execute(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            request = _parse_request(payload)
        except Exception:
            raise HTTPException(
                status_code=409, detail="benchmark execution request is invalid"
            ) from None
        try:
            evidence = await executor.execute(request)
            _require_bound_evidence(request, evidence)
            return _serialize_evidence(evidence)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=502, detail="benchmark execution is unavailable"
            ) from None

    return app


def create_capability_benchmark_process_app_from_environment(
    environment: dict[str, str] | None = None,
    *,
    repository_root: Path | None = None,
) -> FastAPI:
    """Load the isolated executor only from digest-verified repository code."""

    source = dict(os.environ if environment is None else environment)
    root = (repository_root or Path.cwd()).resolve(strict=True)
    relative = source.get("CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR", "").strip()
    digest = source.get("CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR_SHA256", "").strip()
    token = source.get("CAPTAIN_CAPABILITY_BENCHMARK_TOKEN", "").strip()
    authority = source.get("CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID", "").strip()
    if (
        not relative
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or len(token) < 16
        or not authority
    ):
        raise ValueError("benchmark process configuration is incomplete")
    try:
        module = load_verified_repository_module(
            relative, expected_sha256=digest, repository_root=root
        )
        constructor = getattr(module, "build_capability_benchmark_executor", None)
        if not callable(constructor):
            raise ValueError("constructor")
        executor = constructor()
        authority_id = UUID(authority)
    except (RuntimeAdapterManifestError, TypeError, ValueError):
        raise ValueError("benchmark process configuration is invalid") from None
    if not callable(getattr(executor, "execute", None)):
        raise ValueError("benchmark process executor is invalid")
    return create_capability_benchmark_process_app(
        executor=executor,
        bearer_token=token,
        authority_id=authority_id,
    )


def main() -> None:
    source = dict(os.environ)
    try:
        port = int(source.get("CAPTAIN_CAPABILITY_BENCHMARK_PORT", "8092"))
    except ValueError:
        raise ValueError("benchmark process configuration is invalid") from None
    if port < 1 or port > 65535:
        raise ValueError("benchmark process configuration is invalid")
    application = create_capability_benchmark_process_app_from_environment(source)
    uvicorn.run(application, host="127.0.0.1", port=port, workers=1)


def _parse_request(payload: dict[str, object]) -> GenericBenchmarkExecutionRequest:
    required = {
        "schema", "job_id", "capability_id", "input_ref", "case", "team_kind",
        "model_id", "model_policy_digest", "execution_case", "request_nonce",
        "request_digest",
    }
    if set(payload) != required or payload["schema"] != "captain.capability-benchmark-execution-request.v1":
        raise ValueError("schema")
    case_payload = payload["case"]
    execution_payload = payload["execution_case"]
    if (
        not isinstance(case_payload, dict)
        or set(case_payload) != {"case_id", "category", "case_digest"}
        or not isinstance(execution_payload, dict)
        or set(execution_payload) != {"setup", "action", "opaque_source_refs"}
    ):
        raise ValueError("case")
    case_ref = GenericPrivateCaseRef(
        case_id=str(case_payload["case_id"]),
        category=GenericBenchmarkCategory(str(case_payload["category"])),
        assertion_ids=(),
        case_digest=str(case_payload["case_digest"]),
    )
    execution_case = GenericExecutionCaseView(
        case_ref=case_ref,
        setup=str(execution_payload["setup"]),
        action=str(execution_payload["action"]),
        opaque_source_refs=tuple(str(item) for item in execution_payload["opaque_source_refs"]),
    )
    values = {
        "job_id": UUID(str(payload["job_id"])),
        "capability_id": str(payload["capability_id"]),
        "input_ref": ArtifactRef.model_validate(payload["input_ref"]),
        "case_ref": case_ref,
        "team_kind": str(payload["team_kind"]),
        "model_id": str(payload["model_id"]),
        "model_policy_digest": str(payload["model_policy_digest"]),
        "execution_case": execution_case,
        "request_nonce": UUID(str(payload["request_nonce"])),
    }
    digest = generic_execution_request_digest(**values)  # type: ignore[arg-type]
    if digest != payload["request_digest"]:
        raise ValueError("digest")
    return GenericBenchmarkExecutionRequest(
        **values, request_digest=digest  # type: ignore[arg-type]
    )


def _require_bound_evidence(
    request: GenericBenchmarkExecutionRequest,
    evidence: GenericBenchmarkExecutionEvidence,
) -> None:
    if (
        evidence.request_nonce != request.request_nonce
        or evidence.request_digest != request.request_digest
        or evidence.case_ref.case_id != request.case_ref.case_id
        or evidence.case_ref.category != request.case_ref.category
        or evidence.case_ref.case_digest != request.case_ref.case_digest
        or evidence.team_kind != request.team_kind
        or evidence.model_id != request.model_id
        or evidence.model_policy_digest != request.model_policy_digest
        or evidence.input_ref != request.input_ref
        or evidence.capability_id != request.capability_id
    ):
        raise ValueError("benchmark response binding")


def _serialize_evidence(evidence: GenericBenchmarkExecutionEvidence) -> dict[str, object]:
    return {
        "schema": "captain.capability-benchmark-execution-response.v1",
        "request_nonce": str(evidence.request_nonce),
        "request_digest": evidence.request_digest,
        "agent_count": evidence.agent_count,
        "outcome_ref": evidence.outcome_ref.model_dump(mode="json"),
        "message_event_refs": [item.model_dump(mode="json") for item in evidence.message_event_refs],
        "handoff_event_refs": [item.model_dump(mode="json") for item in evidence.handoff_event_refs],
        "tool_calls": [
            {
                "tool_key": item.tool_key,
                "call_ref": item.call_ref.model_dump(mode="json"),
                "lease_ref": item.lease_ref.model_dump(mode="json"),
                "receipt_ref": item.receipt_ref.model_dump(mode="json"),
            }
            for item in evidence.tool_calls
        ],
        "rationale_ref": evidence.rationale_ref.model_dump(mode="json"),
        "cost_usd": format(Decimal(evidence.cost_usd), "f"),
        "latency_ms": evidence.latency_ms,
        "trace_id": str(evidence.trace_id),
        "run_id": str(evidence.run_id),
    }


__all__ = [
    "CapabilityBenchmarkProcessExecutor",
    "create_capability_benchmark_process_app",
    "create_capability_benchmark_process_app_from_environment",
]


if __name__ == "__main__":
    main()
