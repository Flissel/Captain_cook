"""Authenticated read-only host for content-addressed demo benchmark data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import FastAPI, Header, HTTPException

from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    read_verified_repository_bytes,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROUTES = {
    "/v1/sales/accounts": ("account_snapshot_reader", "read", "captain.sales-account-snapshot.v1", "sales"),
    "/v1/sales/opportunity-score": ("opportunity_scoring_rules", "read", "captain.sales-opportunity-score.v1", "sales"),
    "/v1/sales/industry-signals": ("industry_signal_reader", "read", "captain.sales-industry-signals.v1", "sales"),
    "/v1/alerts/context": ("alert_context_reader", "read", "captain.incident-alert-context.v1", "incident"),
    "/v1/services/dependencies": ("service_dependency_map", "read", "captain.incident-service-dependency-map.v1", "incident"),
    "/v1/audit/query": ("audit_log_query", "read", "captain.incident-audit-log.v1", "incident"),
    "/v1/status/drafts": ("status_page_draft", "draft", "captain.incident-status-page-draft.v1", "incident"),
    "/v1/policies/lookup": ("policy_lookup", "read", "captain.claim-policy-lookup.v1", "claims"),
    "/v1/claims/evidence": ("claim_evidence_reader", "read", "captain.claim-evidence.v1", "claims"),
    "/v1/ocr/extract": ("ocr_extractor", "read", "captain.claim-ocr-extraction.v1", "claims"),
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()


def _select(dataset: dict[str, object], arguments: Mapping[str, object]) -> dict[str, object]:
    records = dataset.get("records")
    if not isinstance(records, list):
        raise ValueError("dataset")
    identifiers = tuple(
        str(arguments[key]) for key in (
            "account_id", "alert_id", "claim_id", "policy_id", "document_id",
            "service",
        ) if key in arguments and isinstance(arguments[key], (str, int))
    )
    if not identifiers:
        selected = records
    else:
        selected = [
            record for record in records if isinstance(record, dict)
            and any(value in _canonical(record).decode("utf-8") for value in identifiers)
        ]
    return {"matches": selected, "match_count": len(selected)}


def create_capability_tool_host_app(
    *, bearer_token: str, datasets: Mapping[str, dict[str, object]],
    dataset_digests: Mapping[str, str], implementation_sha256: str,
) -> FastAPI:
    if len(bearer_token) < 16 or _SHA256.fullmatch(implementation_sha256) is None:
        raise ValueError("capability tool host authority is invalid")
    if set(datasets) != {"sales", "incident", "claims"} or set(dataset_digests) != set(datasets):
        raise ValueError("capability tool host datasets are incomplete")
    for name, value in datasets.items():
        if (
            value.get("dataset_kind") != "demo_fixture_evidence"
            or _SHA256.fullmatch(dataset_digests[name]) is None
        ):
            raise ValueError("capability tool host dataset authority is invalid")
    app = FastAPI(
        title="Captain Capability Tool Host", docs_url=None, redoc_url=None,
        openapi_url=None, redirect_slashes=False,
    )

    def authorize(value: str | None) -> None:
        if value is None or not secrets.compare_digest(value, "Bearer " + bearer_token):
            raise HTTPException(status_code=401, detail="tool host authentication failed")

    @app.get("/healthz")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, object]:
        authorize(authorization)
        return {
            "schema": "captain.capability-tool-host-health.v1", "status": "ready",
            "implementation_sha256": implementation_sha256,
            "dataset_sha256": dict(dataset_digests),
            "truth_scope": "demo_fixture_evidence",
        }

    def bind_route(
        path: str, tool_key: str, operation: str, schema: str, dataset_name: str,
    ) -> None:
        async def execute(
            payload: dict[str, object],
            authorization: str | None = Header(default=None),
        ) -> dict[str, object]:
            authorize(authorization)
            expected = {"schema", "tool_key", "operation", "endpoint_sha256", "registry_sha256", "arguments"}
            endpoint_sha256 = hashlib.sha256(
                ("http://127.0.0.1:8110" + path).encode()
            ).hexdigest()
            if (
                set(payload) != expected
                or payload.get("schema") != "captain.generic-tool-execution.v1"
                or payload.get("tool_key") != tool_key
                or payload.get("operation") != operation
                or payload.get("endpoint_sha256") != endpoint_sha256
                or not isinstance(payload.get("arguments"), dict)
            ):
                raise HTTPException(status_code=409, detail="tool execution binding changed")
            arguments = payload["arguments"]
            query_sha256 = hashlib.sha256(_canonical(arguments)).hexdigest()
            result = _select(datasets[dataset_name], arguments)
            result.update({
                "truth_scope": "demo_fixture_evidence",
                "dataset_sha256": dataset_digests[dataset_name],
                "implementation_sha256": implementation_sha256,
                "query_sha256": query_sha256,
            })
            return {
                "schema": schema, "tool_key": tool_key, "operation": operation,
                "endpoint_sha256": endpoint_sha256, "result": result,
            }

        execute.__name__ = "execute_" + tool_key
        app.post(path)(execute)

    for route, values in _ROUTES.items():
        bind_route(route, *values)
    return app


def create_capability_tool_host_app_from_environment(
    environment: Mapping[str, str] | None = None, *, repository_root: Path | None = None,
) -> FastAPI:
    source = dict(os.environ if environment is None else environment)
    root = (repository_root or Path.cwd()).resolve(strict=True)
    token = source.get("CAPTAIN_CAPABILITY_TOOL_HOST_TOKEN", "").strip()
    implementation_sha256 = source.get("CAPTAIN_CAPABILITY_TOOL_HOST_CODE_SHA256", "").strip()
    if len(token) < 16 or _SHA256.fullmatch(implementation_sha256) is None:
        raise ValueError("capability tool host configuration is incomplete")
    try:
        own_path = Path(__file__).resolve().relative_to(root).as_posix()
        read_verified_repository_bytes(
            own_path, expected_sha256=implementation_sha256,
            expected_repository_revision=source["CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION"],
            repository_root=root,
        )
        datasets: dict[str, dict[str, object]] = {}
        digests: dict[str, str] = {}
        for name in ("sales", "incident", "claims"):
            relative = source[f"CAPTAIN_CAPABILITY_TOOL_HOST_{name.upper()}_DATASET"]
            digest = source[f"CAPTAIN_CAPABILITY_TOOL_HOST_{name.upper()}_DATASET_SHA256"]
            _, body = read_verified_repository_bytes(
                relative, expected_sha256=digest,
                expected_repository_revision=source["CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION"],
                repository_root=root,
            )
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("dataset")
            datasets[name] = value
            digests[name] = digest
    except (KeyError, OSError, ValueError, RuntimeAdapterManifestError, json.JSONDecodeError):
        raise ValueError("capability tool host configuration is invalid") from None
    return create_capability_tool_host_app(
        bearer_token=token, datasets=datasets, dataset_digests=digests,
        implementation_sha256=implementation_sha256,
    )


def main() -> None:
    try:
        port = int(os.environ.get("CAPTAIN_CAPABILITY_TOOL_HOST_PORT", "8110"))
    except ValueError:
        raise ValueError("capability tool host port is invalid") from None
    if not 1 <= port <= 65535:
        raise ValueError("capability tool host port is invalid")
    uvicorn.run(
        create_capability_tool_host_app_from_environment(),
        host="127.0.0.1", port=port, workers=1,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "create_capability_tool_host_app",
    "create_capability_tool_host_app_from_environment",
]
