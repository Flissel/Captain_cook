"""Authenticated read-only host for content-addressed demo benchmark data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
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


class _ToolHostReceiptJournal:
    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._values: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)

    def read(
        self, effect_id: str, *, request_sha256: str,
    ) -> dict[str, object] | None:
        if _SHA256.fullmatch(effect_id) is None or _SHA256.fullmatch(request_sha256) is None:
            raise ValueError("tool effect identity is invalid")
        with self._lock:
            value = self._values.get(effect_id)
            if value is None and self._root is not None:
                path = self._root / f"{effect_id}.json"
                if path.is_file():
                    raw = json.loads(path.read_bytes())
                    if isinstance(raw, dict):
                        value = raw
                        self._values[effect_id] = raw
            if value is None:
                return None
            if value.get("request_sha256") != request_sha256:
                raise ValueError("tool effect replay changed request")
            response = value.get("response")
            if not isinstance(response, dict):
                raise ValueError("tool effect receipt is invalid")
            return dict(response)

    def write(
        self, effect_id: str, *, request_sha256: str,
        response: dict[str, object],
    ) -> None:
        record = {
            "schema": "captain.capability-tool-host-receipt.v1",
            "effect_id": effect_id, "request_sha256": request_sha256,
            "response": response,
        }
        body = _canonical(record)
        with self._lock:
            existing = self._values.get(effect_id)
            if existing is not None:
                if _canonical(existing) != body:
                    raise ValueError("tool effect receipt conflicts")
                return
            if self._root is not None:
                final = self._root / f"{effect_id}.json"
                temporary = self._root / f"{effect_id}.{os.getpid()}.tmp"
                if final.exists():
                    raw = json.loads(final.read_bytes())
                    if _canonical(raw) != body:
                        raise ValueError("tool effect receipt conflicts")
                else:
                    with temporary.open("xb") as handle:
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, final)
            self._values[effect_id] = record


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
    journal_root: Path | None = None,
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
    journal = _ToolHostReceiptJournal(journal_root)

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

    @app.post("/v1/effects/readback")
    async def readback(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if set(payload) != {"schema", "tool_key", "effect_id", "request_sha256"} or payload.get("schema") != "captain.generic-tool-effect-readback.v1":
            raise HTTPException(status_code=409, detail="tool effect readback binding changed")
        try:
            value = journal.read(
                str(payload["effect_id"]), request_sha256=str(payload["request_sha256"]),
            )
        except ValueError:
            raise HTTPException(status_code=409, detail="tool effect readback binding changed") from None
        if value is None or value.get("tool_key") != payload.get("tool_key"):
            raise HTTPException(status_code=404, detail="tool effect receipt is unavailable")
        return value

    def bind_route(
        path: str, tool_key: str, operation: str, schema: str, dataset_name: str,
    ) -> None:
        async def execute(
            payload: dict[str, object],
            authorization: str | None = Header(default=None),
        ) -> dict[str, object]:
            authorize(authorization)
            expected = {"schema", "tool_key", "operation", "endpoint_sha256", "registry_sha256", "arguments", "effect_id"}
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
                or _SHA256.fullmatch(str(payload.get("effect_id", ""))) is None
            ):
                raise HTTPException(status_code=409, detail="tool execution binding changed")
            arguments = payload["arguments"]
            request_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
            try:
                prior = journal.read(
                    str(payload["effect_id"]), request_sha256=request_sha256,
                )
            except ValueError:
                raise HTTPException(status_code=409, detail="tool effect replay changed request") from None
            if prior is not None:
                return prior
            query_sha256 = hashlib.sha256(_canonical(arguments)).hexdigest()
            result = _select(datasets[dataset_name], arguments)
            result.update({
                "truth_scope": "demo_fixture_evidence",
                "dataset_sha256": dataset_digests[dataset_name],
                "implementation_sha256": implementation_sha256,
                "query_sha256": query_sha256,
            })
            response = {
                "schema": schema, "tool_key": tool_key, "operation": operation,
                "endpoint_sha256": endpoint_sha256, "result": result,
            }
            journal.write(
                str(payload["effect_id"]), request_sha256=request_sha256,
                response=response,
            )
            return response

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
    try:
        journal_relative = Path(source["CAPTAIN_CAPABILITY_TOOL_HOST_JOURNAL_ROOT"])
        if journal_relative.is_absolute() or ".." in journal_relative.parts:
            raise ValueError("journal")
        journal_root = (root / journal_relative).resolve()
        journal_root.relative_to(root)
    except (KeyError, OSError, ValueError):
        raise ValueError("capability tool host configuration is invalid") from None
    return create_capability_tool_host_app(
        bearer_token=token, datasets=datasets, dataset_digests=digests,
        implementation_sha256=implementation_sha256,
        journal_root=journal_root,
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
