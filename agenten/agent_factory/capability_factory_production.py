"""Production HTTP ports and attested manifests for the capability factory.

The adapters are inert until called.  They never import Minibook internals and
never execute a provider during bootstrap; the 8091 runtime owns that effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityReleaseRunReceipt,
)
from agenten.agent_factory.contracts import AgentFactoryJobV2, FactoryEvidenceBlock
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationResultV1,
    CreationSubmissionReceipt,
)
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_runtime.http_server import RuntimeCommandExecutor, create_runtime_app


_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CapabilityProductionConfigurationError(ValueError):
    """A production port or manifest is incomplete or ambiguous."""


class AdapterManifestKind(str, Enum):
    ENTRYPOINT = "entrypoint"
    FACTORY_LIVE_RUNTIME = "factory_live_runtime"


_SCHEMA_BY_KIND = {
    AdapterManifestKind.ENTRYPOINT: (
        "captain.capability-factory-entrypoint-adapter-manifest.v1"
    ),
    AdapterManifestKind.FACTORY_LIVE_RUNTIME: (
        "captain.factory-live-runtime-adapter-manifest.v1"
    ),
}


class GeneratedAdapterManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: AdapterManifestKind


def generate_adapter_manifest(
    *,
    workspace_root: Path,
    module_path: Path,
    factory_symbol: str,
    target_path: Path,
    kind: AdapterManifestKind,
) -> GeneratedAdapterManifest:
    """Write canonical, digest-pinned adapter metadata without loading code."""

    root = workspace_root.resolve()
    module = module_path.resolve()
    target = target_path.resolve()
    try:
        relative_module = module.relative_to(root)
        target.relative_to(root)
    except ValueError as exc:
        raise CapabilityProductionConfigurationError(
            "adapter manifest paths must remain inside the workspace"
        ) from exc
    if module.suffix.casefold() != ".py" or not module.is_file():
        raise CapabilityProductionConfigurationError(
            "adapter module must be a readable Python workspace file"
        )
    if _SYMBOL.fullmatch(factory_symbol) is None:
        raise CapabilityProductionConfigurationError("adapter factory symbol is invalid")
    module_bytes = module.read_bytes()
    if len(module_bytes) > 1_048_576:
        raise CapabilityProductionConfigurationError("adapter module exceeds size limit")
    module_sha256 = hashlib.sha256(module_bytes).hexdigest()
    payload = {
        "schema": _SCHEMA_BY_KIND[kind],
        "module_path": relative_module.as_posix(),
        "module_sha256": module_sha256,
        "factory_symbol": factory_symbol,
    }
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return GeneratedAdapterManifest(
        path=target,
        sha256=hashlib.sha256(content).hexdigest(),
        module_sha256=module_sha256,
        kind=kind,
    )


class AsyncJsonHttpClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...
    async def post(self, url: str, **kwargs: Any) -> Any: ...


class MinibookSwarmCreationHttpPort:
    """Typed Minibook Swarm boundary; Hermes evidence stays lease-bound."""

    def __init__(self, base_url: str, token: SecretStr, http: AsyncJsonHttpClient) -> None:
        self._base_url = _service_url(base_url, "Minibook")
        self._token = token
        self._http = http

    async def preparation_blocks(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock]:
        response = await self._http.get(
            f"{self._base_url}/api/v1/creation-jobs/{creation_job.creation_job_id}/preparation-blocks",
            headers=self._headers(),
        )
        _raise_for_status(response)
        blocks = tuple(FactoryEvidenceBlock.model_validate(item) for item in response.json())
        if len(blocks) != 2 or any(block.job_id != job.job_id for block in blocks):
            raise ValueError("Minibook preparation evidence does not match the factory job")
        return blocks[0], blocks[1]

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt:
        response = await self._http.post(
            f"{self._base_url}/api/v1/creation-jobs",
            headers=self._headers(),
            json=creation_job.model_dump(mode="json", by_alias=True),
        )
        _raise_for_status(response)
        return CreationSubmissionReceipt.model_validate(response.json())

    async def result(self, creation_job_id: UUID) -> CreationResultV1:
        response = await self._http.get(
            f"{self._base_url}/api/v1/creation-jobs/{creation_job_id}/result",
            headers=self._headers(),
        )
        _raise_for_status(response)
        return CreationResultV1.model_validate(response.json())

    async def completion_block(
        self,
        job: AgentFactoryJobV2,
        result: CreationResultV1,
    ) -> FactoryEvidenceBlock:
        response = await self._http.get(
            f"{self._base_url}/api/v1/creation-jobs/{result.creation_job_id}/completion-block",
            headers=self._headers(),
        )
        _raise_for_status(response)
        block = FactoryEvidenceBlock.model_validate(response.json())
        if block.job_id != job.job_id:
            raise ValueError("Minibook completion evidence does not match the factory job")
        return block

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}


class EvidenceRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job: AgentFactoryJobV2
    creation_result: CreationResultV1
    candidate: ForgeCapabilityPackageCandidateV1
    run_number: int = Field(ge=1, le=4, strict=True)

    @model_validator(mode="after")
    def require_authority_and_content_addresses(self) -> "EvidenceRunRequest":
        job = self.job
        result = self.creation_result
        candidate = self.candidate
        if (
            result.creation_job_id != candidate.creation_job_id
            or result.correlation_id != job.correlation_id
            or candidate.factory_job_id != job.job_id
            or candidate.correlation_id != job.correlation_id
            or candidate.subject_version != job.subject_version
            or candidate.capability_id != job.required_capability
        ):
            raise ValueError("evidence run authority does not match the factory job")
        references = [
            candidate.source_ref,
            candidate.team_manifest_ref,
            candidate.skill_usage_receipt_ref,
            candidate.runbook_ref,
            *(artifact.reference for artifact in candidate.artifacts),
            *result.artifact_refs,
            *result.evidence_refs,
        ]
        if result.package_manifest_ref is not None:
            references.append(result.package_manifest_ref)
        if result.skill_usage_receipt_ref is not None:
            references.append(result.skill_usage_receipt_ref)
        if any(reference.uri.rsplit("/", 1)[-1] != reference.sha256 for reference in references):
            raise ValueError("evidence run requires content-addressed artifact references")
        return self


class EvidenceLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job: AgentFactoryJobV2
    receipts: tuple[CapabilityReleaseRunReceipt, ...] = Field(min_length=4)


class RuntimeCaptainEvidenceHttpPort:
    """Captain client for the authenticated provider-backed 8091 evidence API."""

    def __init__(self, base_url: str, token: SecretStr, http: AsyncJsonHttpClient) -> None:
        self._base_url = _service_url(base_url, "runtime")
        self._token = token
        self._http = http

    async def run(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseRunReceipt | None:
        request = EvidenceRunRequest(
            job=job,
            creation_result=creation_result,
            candidate=candidate,
            run_number=run_number,
        )
        response = await self._http.post(
            f"{self._base_url}/v1/capability-factory/evidence-runs",
            headers=self._headers(),
            json=request.model_dump(mode="json", by_alias=True),
        )
        if response.status_code == status.HTTP_409_CONFLICT:
            return None
        _raise_for_status(response)
        receipt = CapabilityReleaseRunReceipt.model_validate(response.json())
        if receipt.record.run_number != run_number:
            raise ValueError("runtime evidence run number does not match the request")
        return receipt

    async def lifecycle_blocks(
        self,
        job: AgentFactoryJobV2,
        receipts: tuple[CapabilityReleaseRunReceipt, ...],
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]:
        request = EvidenceLifecycleRequest(job=job, receipts=receipts)
        response = await self._http.post(
            f"{self._base_url}/v1/capability-factory/lifecycle-blocks",
            headers=self._headers(),
            json=request.model_dump(mode="json", by_alias=True),
        )
        _raise_for_status(response)
        blocks = tuple(FactoryEvidenceBlock.model_validate(item) for item in response.json())
        if len(blocks) != 3 or any(block.job_id != job.job_id for block in blocks):
            raise ValueError("runtime lifecycle evidence does not match the factory job")
        return blocks[0], blocks[1], blocks[2]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}


class CapabilityEvidenceBackend(Protocol):
    async def run(self, request: EvidenceRunRequest) -> CapabilityReleaseRunReceipt | None: ...
    async def lifecycle_blocks(
        self, request: EvidenceLifecycleRequest
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]: ...


def create_capability_factory_runtime_app(
    *,
    runtime_executor: RuntimeCommandExecutor,
    backend: CapabilityEvidenceBackend,
    token: SecretStr,
) -> FastAPI:
    """Build the 8091 evidence service without performing provider effects."""

    expected = token.get_secret_value()
    if not expected:
        raise CapabilityProductionConfigurationError("runtime token is missing")
    app = create_runtime_app(executor=runtime_executor, token=expected)

    def authorize(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        supplied = authorization or ""
        if not secrets.compare_digest(supplied, f"Bearer {expected}"):
            raise HTTPException(status_code=401, detail="invalid runtime credential")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/capability-factory/evidence-runs")
    async def run_evidence(
        request: EvidenceRunRequest,
        _: None = Depends(authorize),
    ) -> CapabilityReleaseRunReceipt:
        receipt = await backend.run(request)
        if receipt is None:
            raise HTTPException(status_code=409, detail="evidence run is not available")
        return receipt

    @app.post("/v1/capability-factory/lifecycle-blocks")
    async def lifecycle_blocks(
        request: EvidenceLifecycleRequest,
        _: None = Depends(authorize),
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]:
        return await backend.lifecycle_blocks(request)

    return app


def _service_url(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise CapabilityProductionConfigurationError(f"{label} URL is invalid")
    return value.rstrip("/")


def _raise_for_status(response: Any) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("production capability service request failed") from exc
