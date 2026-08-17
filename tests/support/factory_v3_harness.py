"""Hermetic Factory V3 proof using the real local authority boundaries.

Only the external Hermes/Codex model process is replaced.  The replacement
implements the production build port, writes one isolated workspace, executes
real Python compile/test commands, and returns the same typed receipts.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from threading import RLock
from typing import Iterator, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5
import zipfile

from fastapi import FastAPI
import httpx

from agenten.agent_factory.action_resume import (
    FactoryActionEffectReceipt,
    FactoryActionResumeCoordinator,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    GatewayForgeCandidateProvider,
)
from agenten.agent_factory.capability_resolution import CapabilityResolver
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
)
from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    CreationJobV3,
    CreationResultV2,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ToolGapMarkerV1,
    codex_build_receipt_sha256,
)
from agenten.agent_factory.hermes_factory_worker import (
    AuthenticatedMinibookV3WorkerClient,
    FilesystemHermesFactoryWorkerCheckpointStore,
    HermesFactoryAssignmentScope,
    HermesFactoryCodexBuild,
    HermesFactoryWorker,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_contracts import FactoryInputDocumentV2
from agenten.agent_factory.job_builder import build_factory_job_v3
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.minibook_forge import (
    CaptainCreationJobV3Authority,
    CaptainCreationJobV3Mapper,
    FilesystemCaptainCreationJobV3Store,
    MinibookForgeHttpClient,
    MinibookForgeHttpSettings,
)
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatcher
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef
from gateway.factory_action_store import (
    FactoryActionRow,
    FactoryActionStore,
)
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from minibook.swarm.api import create_creation_router
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import (
    ArtifactRef as MinibookArtifactRef,
    CreationFailure as MinibookCreationFailure,
    CreationResultV2 as MinibookCreationResultV2,
    FactoryActionRequestV1 as MinibookActionRequestV1,
    FactoryActionResponseV1 as MinibookActionResponseV1,
)
from minibook.swarm.job_store import CreationJobStore
from minibook.swarm.runner import CreationRunner
from minibook.swarm.scheduler import CreationScheduler
from minibook.swarm.team_package import verify_sealed_team_package
from minibook.swarm.v3_pipeline import TeamPackageV3Validator, V3CreationPipeline


_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "agent_factory"
    / "sales_pipeline_brief.v3.json"
)
_TENANT = "tenant-a"
_WORKER_ID = "hermes-factory"
_CREATION_TOKEN = "captain-hermetic-creation"
_WORKER_TOKEN = "hermes-hermetic-worker"
_SKILL_NAME = "captain-factory-brief-codex"
_VARIANTS = {"happy", "one_agent", "null_output", "timeout", "receipt_only"}


def _canonical(value: object, *, newline: bool = False) -> bytes:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _runtime_ref(reference: object) -> ArtifactRef:
    return ArtifactRef.model_validate(reference.model_dump(mode="json"))


def _agent_runtime_ref(reference: object) -> RuntimeArtifactRef:
    return RuntimeArtifactRef.model_validate(reference.model_dump(mode="json"))


def _minibook_ref(reference: object) -> MinibookArtifactRef:
    return MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 8, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return stream.getvalue()


class _HermeticCreationArtifactStore(FilesystemCreationArtifactStore):
    """One real filesystem object store with a read-only Captain receipt alias."""

    _CAPTAIN_RECEIPT_PREFIX = "artifact://captain-codex-build/build-receipt/"

    def put_captain_receipt(self, content: bytes) -> ArtifactRef:
        stored = super().put(
            content,
            "application/json",
            namespace="captain-receipt",
        )
        return ArtifactRef(
            uri=f"{self._CAPTAIN_RECEIPT_PREFIX}{stored.sha256}",
            sha256=stored.sha256,
            media_type=stored.media_type,
        )

    def local_path(self, reference: MinibookArtifactRef) -> Path:
        if reference.uri.startswith(self._CAPTAIN_RECEIPT_PREFIX):
            if reference.uri.removeprefix(self._CAPTAIN_RECEIPT_PREFIX) != reference.sha256:
                raise ValueError("Captain receipt reference digest changed")
            target = self.root / "objects" / reference.sha256[:2] / reference.sha256
            if not target.is_file():
                raise FileNotFoundError("Captain receipt object is unavailable")
            return target
        return super().local_path(reference)


class _JsonFactoryActionRepository:
    """Small restartable JSON repository exercising the production Action store."""

    def __init__(self, path: Path, *, created_at: datetime) -> None:
        self._path = path
        self._created_at = created_at
        self._lock = RLock()

    def create(self, row: FactoryActionRow) -> FactoryActionRow:
        with self._lock:
            rows = self._load()
            key = str(row.request.request_id)
            if key not in rows:
                row.created_at = self._created_at
                rows[key] = row
                self._save(rows)
            return rows[key]

    def list_rows(
        self,
        *,
        tenant_id: str,
        status: str,
        after_request_id: UUID | None,
        limit: int,
    ) -> tuple[FactoryActionRow, ...]:
        with self._lock:
            rows = (
                row
                for row in self._load().values()
                if row.tenant_id == tenant_id
                and row.status == status
                and (
                    after_request_id is None
                    or str(row.request.request_id) > str(after_request_id)
                )
            )
            return tuple(
                sorted(rows, key=lambda row: str(row.request.request_id))[:limit]
            )

    def resume_rows(
        self, *, after_request_id: UUID | None, limit: int
    ) -> tuple[FactoryActionRow, ...]:
        with self._lock:
            rows = (
                row
                for row in self._load().values()
                if row.status in {"answered", "denied"}
                and (
                    after_request_id is None
                    or str(row.request.request_id) > str(after_request_id)
                )
            )
            return tuple(
                sorted(rows, key=lambda row: str(row.request.request_id))[:limit]
            )

    def credential_rows(
        self, *, after_request_id: UUID | None, limit: int
    ) -> tuple[FactoryActionRow, ...]:
        with self._lock:
            rows = (
                row
                for row in self._load().values()
                if row.credential_reservation_key is not None
                and (
                    after_request_id is None
                    or str(row.request.request_id) > str(after_request_id)
                )
            )
            return tuple(
                sorted(rows, key=lambda row: str(row.request.request_id))[:limit]
            )

    @contextmanager
    def locked(
        self, request_id: UUID, *, tenant_id: str
    ) -> Iterator[FactoryActionRow | None]:
        with self._lock:
            rows = self._load()
            key = str(request_id)
            row = rows.get(key)
            selected = row if row is not None and row.tenant_id == tenant_id else None
            yield selected
            if selected is not None:
                rows[key] = selected
                self._save(rows)

    def expire(
        self,
        *,
        tenant_id: str,
        now: datetime,
        request_id: UUID | None = None,
    ) -> None:
        with self._lock:
            rows = self._load()
            changed = False
            for row in rows.values():
                if (
                    row.tenant_id == tenant_id
                    and row.status in {"pending", "answered", "denied"}
                    and row.request.deadline_at <= now
                    and (request_id is None or row.request.request_id == request_id)
                ):
                    row.status = "expired"
                    changed = True
            if changed:
                self._save(rows)

    def _load(self) -> dict[str, FactoryActionRow]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return {
            key: self._row_from_payload(value)
            for key, value in payload.items()
        }

    def _save(self, rows: dict[str, FactoryActionRow]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_bytes(
            _canonical(
                {
                    key: self._row_to_payload(row)
                    for key, row in sorted(rows.items())
                },
                newline=True,
            )
        )
        os.replace(temporary, self._path)

    @staticmethod
    def _row_to_payload(row: FactoryActionRow) -> dict[str, object]:
        datetimes = (
            "created_at",
            "projection_intent_at",
            "projection_acknowledged_at",
            "continuation_intent_at",
            "continuation_acknowledged_at",
            "consumed_at",
            "credential_reserved_at",
            "credential_revoke_at",
        )
        payload: dict[str, object] = {
            "request": row.request.model_dump(mode="json", by_alias=True),
            "tenant_id": row.tenant_id,
            "status": row.status,
            "response": (
                None
                if row.response is None
                else row.response.model_dump(mode="json", by_alias=True)
            ),
        }
        for field in (
            "response_sha256",
            "resume_phase",
            "projection_idempotency_key",
            "continuation_idempotency_key",
            "credential_reservation_key",
            "credential_alias",
            "credential_ref",
            "credential_revoke_phase",
            "credential_revoke_key",
            "credential_revoke_attempt",
        ):
            payload[field] = getattr(row, field)
        for field in datetimes:
            value = getattr(row, field)
            payload[field] = None if value is None else value.isoformat()
        return payload

    @staticmethod
    def _row_from_payload(payload: dict[str, object]) -> FactoryActionRow:
        def timestamp(name: str) -> datetime | None:
            value = payload.get(name)
            return None if value is None else datetime.fromisoformat(cast(str, value))

        response_payload = payload.get("response")
        return FactoryActionRow(
            request=FactoryActionRequestV1.model_validate(payload["request"]),
            tenant_id=cast(str, payload["tenant_id"]),
            created_at=timestamp("created_at"),
            status=cast(object, payload["status"]),  # type: ignore[arg-type]
            response=(
                None
                if response_payload is None
                else FactoryActionResponseV1.model_validate(response_payload)
            ),
            response_sha256=cast(str | None, payload.get("response_sha256")),
            resume_phase=cast(object, payload.get("resume_phase")),  # type: ignore[arg-type]
            projection_idempotency_key=cast(
                str | None, payload.get("projection_idempotency_key")
            ),
            projection_intent_at=timestamp("projection_intent_at"),
            projection_acknowledged_at=timestamp("projection_acknowledged_at"),
            continuation_idempotency_key=cast(
                str | None, payload.get("continuation_idempotency_key")
            ),
            continuation_intent_at=timestamp("continuation_intent_at"),
            continuation_acknowledged_at=timestamp(
                "continuation_acknowledged_at"
            ),
            consumed_at=timestamp("consumed_at"),
            credential_reservation_key=cast(
                str | None, payload.get("credential_reservation_key")
            ),
            credential_alias=cast(str | None, payload.get("credential_alias")),
            credential_reserved_at=timestamp("credential_reserved_at"),
            credential_ref=cast(str | None, payload.get("credential_ref")),
            credential_revoke_phase=cast(
                object, payload.get("credential_revoke_phase")
            ),  # type: ignore[arg-type]
            credential_revoke_key=cast(
                str | None, payload.get("credential_revoke_key")
            ),
            credential_revoke_attempt=cast(
                int, payload.get("credential_revoke_attempt", 0)
            ),
            credential_revoke_at=timestamp("credential_revoke_at"),
        )


class _EffectSink:
    def __init__(self) -> None:
        self.receipts: dict[str, FactoryActionEffectReceipt] = {}

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def project_factory_action(
        self, *, effect_key: str, payload: dict[str, object], payload_sha256: str
    ) -> FactoryActionEffectReceipt:
        del payload
        receipt = FactoryActionEffectReceipt(effect_key, payload_sha256)
        self.receipts[effect_key] = receipt
        return receipt


class _ContinuationSink:
    def __init__(
        self,
        *,
        store: CreationJobStore,
        scheduler: CreationScheduler,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self.receipts: dict[str, FactoryActionEffectReceipt] = {}
        self.response: FactoryActionResponseV1 | None = None
        self.credential_refs: list[str] = []

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def schedule_factory_continuation(
        self, *, effect_key: str, payload: dict[str, object], payload_sha256: str
    ) -> FactoryActionEffectReceipt:
        response = self.response
        if response is None:
            raise RuntimeError("Factory action response is unavailable")
        mirrored = MinibookActionResponseV1.model_validate(
            response.model_dump(mode="json", by_alias=True)
        )
        creation_job_id = cast(UUID, payload["creation_job_id"])
        progress = self._store.progress(creation_job_id)
        self._store.accept_action_response(
            mirrored,
            expected_version=progress.version,
        )
        self._scheduler.schedule(creation_job_id)
        if response.credential_ref is not None:
            self.credential_refs.append(response.credential_ref)
        receipt = FactoryActionEffectReceipt(effect_key, payload_sha256)
        self.receipts[effect_key] = receipt
        return receipt


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class _LeaseAuthority:
    def __init__(self, *, now: datetime) -> None:
        self._now = now

    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ):
        if now != self._now:
            raise ValueError("Hermetic Captain clock drifted")
        return issue_factory_lease(
            job=job,
            role=role,
            attempt=attempt,
            workspace_ref=f"workspace://captain/factory-v3/{job.job_id}",
            now=now,
        )


class _NoCapabilities:
    def compatible_capability(self, job: AgentFactoryJobV3):
        del job
        return None


class _HermeticHermes:
    """The deterministic replacement for the external Hermes model process."""

    def __init__(self, *, store: _HermeticCreationArtifactStore) -> None:
        self._store = store
        self.blueprint_ref: RuntimeArtifactRef | None = None
        self.tool_plan_ref: RuntimeArtifactRef | None = None

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        if request.lease is None or request.role is None:
            raise ValueError("Hermes requires Captain lease authority")
        if request.role is FactoryRole.AGENT_ARCHITECT:
            phase = FactoryPhase.BLUEPRINT_CREATED
            namespace = "blueprint"
            payload = {
                "schema": "hermes.factory-blueprint.v1",
                "required_agent_ids": [
                    "account_researcher",
                    "opportunity_qualifier",
                    "deal_strategist",
                    "executive_brief_writer",
                ],
            }
        elif request.role is FactoryRole.TOOL_INTEGRATOR:
            phase = FactoryPhase.TOOL_CANDIDATE_TESTED
            namespace = "tool-plan"
            payload = {
                "schema": "hermes.factory-tool-plan.v1",
                "required_tools": [
                    "account_snapshot_reader",
                    "opportunity_scoring_rules",
                ],
                "optional_tools": ["industry_signal_reader"],
            }
        else:
            raise ValueError("Hermetic Hermes received an unsupported role")
        reference = _runtime_ref(
            self._store.put(_canonical(payload), "application/json", namespace=namespace)
        )
        evidence_reference = _agent_runtime_ref(reference)
        if phase is FactoryPhase.BLUEPRINT_CREATED:
            self.blueprint_ref = evidence_reference
        else:
            self.tool_plan_ref = evidence_reference
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"factory-v3-hermetic:{request.job.job_id}:{phase.value}",
            ),
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id,
            occurred_at=request.lease.issued_at,
            producer="hermes",
            subject_version=request.job.subject_version,
            attempt=request.action.attempt,
            phase=phase,
            role=request.role,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=(evidence_reference,),
            evidence_refs=(evidence_reference,),
            lease_id=request.lease.lease_id,
        )


class _SkillVerifier:
    def __init__(self, *, digest: str) -> None:
        self._digest = digest

    def validate_runtime_skill(
        self, *, skill_name: str, released_skill_sha256: str
    ) -> None:
        if skill_name != _SKILL_NAME or released_skill_sha256 != self._digest:
            raise ValueError("Hermetic Hermes skill release changed")


class _ReceiptReader:
    def __init__(self, store: _HermeticCreationArtifactStore) -> None:
        self._store = store

    def read_receipt(self, reference: ArtifactRef) -> CodexBuildReceiptV1:
        return CodexBuildReceiptV1.model_validate_json(
            self._store.read_bytes(_minibook_ref(reference))
        )


class _DeterministicCodexAdapter:
    """Test-only external-process replacement implementing the authorized port."""

    def __init__(
        self,
        *,
        variant: str,
        store: _HermeticCreationArtifactStore,
        jobs: CreationJobStore,
        workspace_root: Path,
    ) -> None:
        if variant not in _VARIANTS:
            raise ValueError("unsupported hermetic Codex variant")
        self.variant = variant
        self._store = store
        self._jobs = jobs
        self._workspace_root = workspace_root
        self.sessions = 0
        self.tested_tools: set[str] = set()

    async def build(
        self, assignment: FactoryBuildAssignmentV2
    ) -> HermesFactoryCodexBuild:
        self.sessions += 1
        if self.variant == "null_output":
            return cast(HermesFactoryCodexBuild, None)
        if self.variant == "timeout":
            raise TimeoutError("deterministic external model timeout")

        job = CreationJobV3.model_validate(
            self._jobs.job(assignment.creation_job_id).model_dump(
                mode="json", by_alias=True
            )
        )
        workspace = self._workspace_root / assignment.assignment_id.hex
        (workspace / "src" / "tools").mkdir(parents=True)
        (workspace / "tests").mkdir(parents=True)
        files = self._workspace_files(assignment)
        for relative, content in files.items():
            target = workspace / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "src"],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.tested_tools = {
            "account_snapshot_reader",
            "opportunity_scoring_rules",
        }

        manifest = self._team_manifest(assignment)
        manifest_bytes = _canonical(manifest, newline=True)
        preliminary_team_ref = _runtime_ref(
            self._store.put(
                manifest_bytes,
                "application/json",
                namespace="team-manifest",
            )
        )
        final_team_ref = ArtifactRef(
            uri=(
                "artifact://minibook-creation/team-file/"
                f"{preliminary_team_ref.sha256}"
            ),
            sha256=preliminary_team_ref.sha256,
            media_type="application/json",
        )
        candidate = {
            "schema": "captain.factory-candidate.v1",
            "candidate_id": "sales_pipeline_team",
            "team_manifest": {
                "reference": final_team_ref.model_dump(mode="json"),
                "relative_path": "team_manifest.json",
            },
            "workflow_artifacts": [],
            "tool_schema_artifacts": [],
            "n8n_tools": [],
            "n8n_tool_references": [],
            "host_tools": [],
            "build_command": ["python", "-m", "compileall", "src"],
            "real_case_command": ["python", "-m", "pytest"],
            "timeout_seconds": 30,
        }
        archive_files = dict(files)
        archive_files["factory-candidate.json"] = _canonical(candidate, newline=True)
        source_ref = _runtime_ref(
            self._store.put(
                _zip_bytes(archive_files),
                "application/zip",
                namespace="source",
            )
        )
        session_ref = _runtime_ref(
            self._store.put(
                _canonical({"schema": "codex.session.v1", "status": "completed"}),
                "application/json",
                namespace="session",
            )
        )
        snapshot_ref = _runtime_ref(
            self._store.put(
                _zip_bytes(files),
                "application/zip",
                namespace="snapshot",
            )
        )
        completed_at = datetime.now(timezone.utc)
        common = {
            "assignment_id": str(assignment.assignment_id),
            "creation_job_id": str(assignment.creation_job_id),
            "factory_job_id": str(assignment.factory_job_id),
            "correlation_id": str(assignment.correlation_id),
            "subject_version": assignment.subject_version,
            "checkpoint_version": assignment.checkpoint_version,
            "attempt": assignment.attempt,
            "effect_key": assignment.effect_key,
            "source_archive_ref": source_ref.model_dump(mode="json"),
            "codex_session_ref": session_ref.model_dump(mode="json"),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        }
        build_ref = _runtime_ref(
            self._store.put(
                _canonical(
                    {
                        "schema": "captain.package-build-evidence.v1",
                        "producer": "captain",
                        "outcome": "passed",
                        **common,
                        "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json"),
                    }
                ),
                "application/json",
                namespace="evidence",
            )
        )
        real_case_ref = _runtime_ref(
            self._store.put(
                _canonical(
                    {
                        "schema": "captain.package-real-case-evidence.v1",
                        "producer": "captain",
                        "outcome": "passed",
                        **common,
                        "assertion_ids": list(job.public_assertion_ids),
                        "declared_test_paths": list(manifest["test_files"]),
                        "evidence_refs": [build_ref.model_dump(mode="json")],
                    }
                ),
                "application/json",
                namespace="evidence",
            )
        )
        hermes_ref = _runtime_ref(
            self._store.put(
                _canonical(
                    {
                        "schema": "hermes.forge-build-skill-usage-receipt.v1",
                        "producer": "hermes",
                        "outcome": "fulfilled",
                        "creation_job_id": str(job.creation_job_id),
                        "factory_job_id": str(job.factory_job_id),
                        "correlation_id": str(job.correlation_id),
                        "subject_version": job.subject_version,
                        "attempt": job.attempt,
                        "idempotency_key": job.idempotency_key,
                        "released_skill": job.released_skill.model_dump(mode="json"),
                        "public_assertion_ids": list(job.public_assertion_ids),
                        "evidence_refs": [
                            job.compiled_spec_ref.model_dump(mode="json"),
                            job.blueprint_ref.model_dump(mode="json"),
                            job.tool_plan_ref.model_dump(mode="json"),
                        ],
                    }
                ),
                "application/json",
                namespace="receipt",
            )
        )
        receipt = CodexBuildReceiptV1(
            receipt_id=uuid5(assignment.assignment_id, "codex-build-receipt"),
            producer="captain",
            outcome="sealed",
            factory_job_id=assignment.factory_job_id,
            creation_job_id=assignment.creation_job_id,
            correlation_id=assignment.correlation_id,
            subject_version=assignment.subject_version,
            attempt=assignment.attempt,
            assignment_id=assignment.assignment_id,
            idempotency_key=assignment.effect_key,
            seal_idempotency_key=assignment.effect_key,
            build_brief_ref=job.blueprint_ref,
            workspace_ref=assignment.workspace_ref,
            codex_session_ref=session_ref,
            workspace_snapshot_ref=snapshot_ref,
            candidate_manifest_ref=preliminary_team_ref,
            source_archive_ref=source_ref,
            test_evidence_refs=(real_case_ref,),
            acceptance_assertion_ids=job.public_assertion_ids,
            completed_at=completed_at,
        )
        receipt_bytes = _canonical(
            receipt.model_dump(mode="json", by_alias=True)
        )
        receipt_ref = self._store.put_captain_receipt(receipt_bytes)
        if receipt_ref.sha256 != codex_build_receipt_sha256(receipt):
            raise AssertionError("Captain receipt canonical digest drifted")
        gap_ref = _runtime_ref(
            self._store.put(
                _canonical({"status": "unresolved", "kind": "optional"}),
                "application/json",
                namespace="tool-gap",
            )
        )
        tool_gap = ToolGapMarkerV1(
            schema_name="TODO_TOOL.v1",
            gap_id="industry_signal_reader",
            severity="optional",
            evidence_ref=gap_ref,
            status="unresolved",
        )
        build = HermesFactoryCodexBuild(
            hermes_usage_receipt_ref=hermes_ref,
            codex_build_receipt_ref=receipt_ref,
            codex_build_receipt=receipt,
            build_evidence_refs=(build_ref,),
            tool_gaps=(tool_gap,),
        )
        if self.variant == "receipt_only":
            self._store.local_path(_minibook_ref(source_ref)).unlink()
        return build

    def _workspace_files(
        self, assignment: FactoryBuildAssignmentV2
    ) -> dict[str, bytes]:
        agents = assignment.required_agent_ids
        if self.variant == "one_agent":
            agents = agents[:1]
        return {
            "src/__init__.py": b"\"\"\"Generated sales pipeline package.\"\"\"\n",
            "src/team.py": (
                "AGENTS = " + repr(tuple(agents)) + "\n"
            ).encode("utf-8"),
            "src/tools/__init__.py": b"\"\"\"Generated typed sales tools.\"\"\"\n",
            "src/tools/account_snapshot_reader.py": (
                b"def read_account_snapshot(value: dict[str, object]) -> dict[str, object]:\n"
                b"    return {key: value[key] for key in sorted(value)}\n"
            ),
            "src/tools/opportunity_scoring_rules.py": (
                b"def qualify_opportunity(value: dict[str, object]) -> dict[str, object]:\n"
                b"    return {'qualified': value.get('stage') == 'qualified'}\n"
            ),
            "tests/test_team.py": (
                b"from src.team import AGENTS\n"
                b"from src.tools.account_snapshot_reader import read_account_snapshot\n"
                b"from src.tools.opportunity_scoring_rules import qualify_opportunity\n\n"
                b"def test_generated_team_and_required_tools():\n"
                b"    assert AGENTS\n"
                b"    assert read_account_snapshot({'b': 2, 'a': 1}) == {'a': 1, 'b': 2}\n"
                b"    assert qualify_opportunity({'stage': 'qualified'}) == {'qualified': True}\n"
            ),
        }

    def _team_manifest(
        self, assignment: FactoryBuildAssignmentV2
    ) -> dict[str, object]:
        agents = assignment.required_agent_ids
        handoffs = assignment.ordered_handoffs
        if self.variant == "one_agent":
            agents = agents[:1]
            handoffs = ()
        return {
            "schema": "minibook.team-manifest.v2",
            "agents": [{"agent_id": value} for value in agents],
            "handoffs": [item.model_dump(mode="json") for item in handoffs],
            "tools": [
                {
                    "tool_id": "account_snapshot_reader",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "implementation_path": "src/tools/account_snapshot_reader.py",
                    "test_path": "tests/test_team.py",
                },
                {
                    "tool_id": "opportunity_scoring_rules",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "implementation_path": "src/tools/opportunity_scoring_rules.py",
                    "test_path": "tests/test_team.py",
                },
            ],
            "source_files": [
                "src/__init__.py",
                "src/team.py",
                "src/tools/__init__.py",
                "src/tools/account_snapshot_reader.py",
                "src/tools/opportunity_scoring_rules.py",
            ],
            "test_files": ["tests/test_team.py"],
        }


class _CountingForge:
    def __init__(self, delegate: MinibookForgeHttpClient) -> None:
        self._delegate = delegate
        self.creation_submissions = 0

    async def submit(self, request: FactoryDispatch):
        receipt = await self._delegate.submit(request)
        self.creation_submissions += 1
        return receipt

    async def status(self, creation_job_id: UUID):
        return await self._delegate.status(creation_job_id)

    async def result(self, creation_job_id: UUID):
        return await self._delegate.result(creation_job_id)

    async def wait_for_result(self, creation_job_id: UUID):
        return await self._delegate.wait_for_result(creation_job_id)


class _TamperingCaptainArtifactReader:
    def __init__(self, delegate: GatewayMinibookCreationArtifactStore) -> None:
        self._delegate = delegate
        self._tampered = False

    def read_bytes(self, reference: RuntimeArtifactRef) -> bytes:
        return self._delegate.read_bytes(reference)

    def read_bytes_bounded(
        self, reference: RuntimeArtifactRef, *, max_bytes: int
    ) -> bytes:
        if not self._tampered and reference.media_type == "application/zip":
            self._delegate.local_path(reference).write_bytes(b"tampered-cas")
            self._tampered = True
        return self._delegate.read_bytes_bounded(reference, max_bytes=max_bytes)

    def local_path(self, reference: RuntimeArtifactRef) -> Path:
        return self._delegate.local_path(reference)


@dataclass(frozen=True)
class FactoryV3FailedOutcome:
    status: Literal["failed", "blocked"]


class FactoryV3Run:
    def __init__(self, harness: "FactoryV3Harness") -> None:
        self._harness = harness

    async def wait_for_action(self, request_type: str) -> FactoryActionRequestV1:
        return await self._harness._wait_for_action(request_type)

    async def answer(
        self,
        action: FactoryActionRequestV1,
        *,
        credential_ref: str,
    ) -> None:
        await self._harness._answer(action, credential_ref=credential_ref)

    async def wait_for_result(self) -> CreationResultV2:
        return await self._harness._wait_for_result()

    @property
    def creation_submissions(self) -> int:
        return self._harness.creation_submissions

    @property
    def codex_sessions(self) -> int:
        return self._harness.codex_sessions

    @property
    def build_effects(self) -> int:
        return self._harness.build_effects

    @property
    def team_agent_ids(self) -> tuple[str, ...]:
        return self._harness.team_agent_ids

    @property
    def required_tools_with_passing_tests(self) -> set[str]:
        return self._harness.required_tools_with_passing_tests

    @property
    def minibook_readback_verified(self) -> bool:
        return self._harness.minibook_readback_verified

    @property
    def captain_readback_verified(self) -> bool:
        return self._harness.captain_readback_verified

    @property
    def credential_refs(self) -> tuple[str, ...]:
        return tuple(self._harness._continuation.credential_refs)

    @property
    def serialized_action_payloads(self) -> str:
        return self._harness._action_repository_path.read_text(encoding="utf-8")


class FactoryV3Harness:
    """Own one hermetic Task-9 run and expose only observable evidence."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._base_time = datetime.now(timezone.utc).replace(microsecond=0)
        self._artifacts = _HermeticCreationArtifactStore(root / "minibook-cas")
        self._minibook_store = CreationJobStore(root / "creation.sqlite3")
        self._pipeline = V3CreationPipeline(
            store=self._minibook_store,
            artifact_store=self._artifacts,
            validator=TeamPackageV3Validator(),
        )
        self._scheduler = CreationScheduler(
            store=self._minibook_store,
            runner_for=lambda job_id: CreationRunner(
                self._minibook_store, self._pipeline
            ),
        )
        self._app = FastAPI()
        self._app.include_router(
            create_creation_router(
                self._minibook_store,
                schedule=self._scheduler.schedule,
                creation_api_key=_CREATION_TOKEN,
                worker_token=_WORKER_TOKEN,
                worker_id=_WORKER_ID,
                artifact_store=self._artifacts,
            )
        )
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://minibook.test",
        )
        self._action_repository_path = root / "action-inbox.json"
        self._action_store = FactoryActionStore(
            repository=_JsonFactoryActionRepository(
                self._action_repository_path,
                created_at=self._base_time,
            )
        )
        self._projection = _EffectSink()
        self._continuation = _ContinuationSink(
            store=self._minibook_store,
            scheduler=self._scheduler,
        )
        self._resume = FactoryActionResumeCoordinator(
            store=self._action_store,
            projection=self._projection,
            scheduler=self._continuation,
            clock=lambda: self._base_time + timedelta(seconds=10),
        )
        self._dispatch_task: asyncio.Task[object] | None = None
        self._job: AgentFactoryJobV3 | None = None
        self._creation_job: CreationJobV3 | None = None
        self._assignment: FactoryBuildAssignmentV2 | None = None
        self._effect: FactoryBuildEffectV2 | None = None
        self._result: CreationResultV2 | None = None
        self._forge: _CountingForge | None = None
        self._codex: _DeterministicCodexAdapter | None = None
        self._worker: HermesFactoryWorker | None = None
        self._coordinator: FactoryCoordinator | None = None
        self._authority_store: FilesystemCaptainCreationJobV3Store | None = None
        self._minibook_readback = False
        self._captain_readback = False

    @classmethod
    async def create(cls, root: Path) -> "FactoryV3Harness":
        harness = cls(root)
        await harness._scheduler.start(resume=False)
        return harness

    async def close(self) -> None:
        if self._dispatch_task is not None and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
        await self._scheduler.stop()
        await self._client.aclose()

    async def submit_sales_pipeline_brief(self) -> FactoryV3Run:
        await self._begin("happy", tamper_captain=False, with_action=True)
        return FactoryV3Run(self)

    async def run_variant(self, variant: str) -> CreationResultV2 | FactoryV3FailedOutcome:
        if variant not in {"one_agent", "null_output", "timeout", "receipt_only", "tampered_cas"}:
            raise ValueError("unknown Factory V3 variant")
        codex_variant = "happy" if variant == "tampered_cas" else variant
        await self._begin(
            codex_variant,
            tamper_captain=variant == "tampered_cas",
            with_action=False,
        )
        try:
            await self._run_worker()
        except (TimeoutError, TypeError, ValueError):
            self._finish_codex_failure()
        await self._scheduler.join()
        try:
            await self._await_dispatch_task()
        except Exception:
            pass
        assert self._creation_job is not None
        stored = self._minibook_store.result(self._creation_job.creation_job_id)
        if variant == "tampered_cas":
            return FactoryV3FailedOutcome(status="failed")
        if stored is None:
            return FactoryV3FailedOutcome(status="blocked")
        return CreationResultV2.model_validate(
            stored.model_dump(mode="json", by_alias=True)
        )

    async def _begin(
        self,
        variant: str,
        *,
        tamper_captain: bool,
        with_action: bool,
    ) -> None:
        fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        document = FactoryInputDocumentV2.model_validate(fixture["document"])
        compiled = FactoryInputCompiler(
            holdout_store=InMemoryPrivateHoldoutStore()
        ).compile(document, subject_version=3)
        compiled_ref = _runtime_ref(
            self._artifacts.put(
                _canonical(
                    compiled.model_dump(mode="json", by_alias=True),
                    newline=True,
                ),
                "application/json",
                namespace="compiled-spec",
            )
        )
        graph_ref = _runtime_ref(
            self._artifacts.put(
                _canonical(
                    {
                        "schema": "captain.factory-work-graph.v1",
                        "nodes": [
                            item.model_dump(mode="json") for item in compiled.work_nodes
                        ],
                        "dependency_order": list(compiled.dependency_order),
                    },
                    newline=True,
                ),
                "application/json",
                namespace="dependency-graph",
            )
        )
        policy = FactoryExecutionPolicyV1(
            schema_name="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.DEMO,
            live_execution=False,
            max_cost_usd=Decimal("0"),
            max_runtime_seconds=3600,
            required_live_runs=0,
        )
        built = build_factory_job_v3(
            compiled,
            correlation_id=UUID("00000000-0000-0000-0000-000000009302"),
            now=self._base_time,
            execution_policy=policy,
        )
        job_payload = built.model_dump(mode="json", by_alias=True)
        job_payload["compiled_spec_ref"] = compiled_ref.model_dump(mode="json")
        job_payload["dependency_graph_ref"] = graph_ref.model_dump(mode="json")
        self._job = AgentFactoryJobV3.model_validate(job_payload)

        skill_content = _canonical(
            {"schema": "hermes.released-skill.v1", "name": _SKILL_NAME},
            newline=True,
        )
        skill_ref = _runtime_ref(
            self._artifacts.put(
                skill_content,
                "application/json",
                namespace="released-skill",
            )
        )
        released = ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id=_SKILL_NAME,
            version=1,
            capability=self._job.required_capability,
            content_ref=_agent_runtime_ref(skill_ref),
            content_sha256=skill_ref.sha256,
            status="released",
            released_at=self._base_time,
            producer="captain",
        )
        workspace_policy_ref = _agent_runtime_ref(
            self._artifacts.put(
                _canonical(
                    {
                        "schema": "captain.workspace-policy.v1",
                        "allowed_roots": ["src", "tests", "evidence"],
                    },
                    newline=True,
                ),
                "application/json",
                namespace="workspace-policy",
            )
        )
        self._authority_store = FilesystemCaptainCreationJobV3Store(
            self._root / ".captain-cook" / "factory-v3-authority"
        )
        hermes = _HermeticHermes(store=self._artifacts)
        leases = _LeaseAuthority(now=self._base_time)

        def authority(request: FactoryDispatch) -> CaptainCreationJobV3Authority:
            if request.lease is None:
                raise ValueError("V3 mapping lease is unavailable")
            if hermes.blueprint_ref is None or hermes.tool_plan_ref is None:
                raise ValueError("Hermes planning evidence is unavailable")
            lease_ref = _agent_runtime_ref(
                self._artifacts.put(
                    _canonical(
                        request.lease.model_dump(mode="json", by_alias=True)
                    ),
                    "application/json",
                    namespace="captain-lease",
                )
            )
            return CaptainCreationJobV3Authority(
                command_id=request.job.event_id,
                capability_resolution=CapabilityResolver(_NoCapabilities()).resolve(
                    request.job
                ),
                compiled_spec_ref=request.job.compiled_spec_ref,
                lease_ref=lease_ref,
                blueprint_ref=hermes.blueprint_ref,
                tool_plan_ref=hermes.tool_plan_ref,
                required_agent_ids=compiled.required_agent_ids,
                ordered_handoffs=tuple(
                    item.model_dump(mode="json")
                    for item in compiled.ordered_handoffs
                ),
                tool_requirements=tuple(
                    item.model_dump(mode="json")
                    for item in compiled.tool_requirements
                ),
                workspace_policy_ref=workspace_policy_ref,
                clarification_policy={
                    "allowed_request_types": ["credential_required"],
                    "max_requests": 1,
                },
            )

        mapper = CaptainCreationJobV3Mapper(
            authority=authority,
            released_skill=lambda job, step: (
                released
                if step is FactorySkillStep.BRIEF_CODEX
                else cast(ReleasedHermesSkill, None)
            ),
            store=self._authority_store,
        )
        real_forge = MinibookForgeHttpClient(
            mapper=mapper,
            settings=MinibookForgeHttpSettings(
                base_url="http://minibook.test",
                api_key=_CREATION_TOKEN,
                poll_interval_seconds=0.01,
                max_polls=500,
            ),
            client=self._client,
        )
        self._forge = _CountingForge(real_forge)
        repository = InMemoryFactoryRepository()
        self._coordinator = FactoryCoordinator(repository)
        gateway_reader = GatewayMinibookCreationArtifactStore(
            self._root / "minibook-cas"
        )
        candidate_reader = (
            _TamperingCaptainArtifactReader(gateway_reader)
            if tamper_captain
            else gateway_reader
        )
        provider = GatewayForgeCandidateProvider(
            repository=self._coordinator,
            artifacts=candidate_reader,
            v3_jobs=self._authority_store,
        )
        validator = CandidateEvaluationFactory(
            provider=provider,
            evidence_store=FilesystemFactoryEvidenceStore(
                self._root / ".captain-cook" / "factory-evidence"
            ),
        )
        dispatcher = FactoryDispatcher(
            coordinator=self._coordinator,
            hermes=hermes,
            forge=self._forge,
            candidate_validator=validator,
            leases=leases,
            clock=_FixedClock(self._base_time),
        )
        self._coordinator.register(self._job)
        self._coordinator.record(
            FactoryEvidenceBlock(
                schema_name="captain.agent-factory-block.v1",
                event_id=uuid5(self._job.job_id, "forge-requested"),
                job_id=self._job.job_id,
                correlation_id=self._job.correlation_id,
                causation_id=self._job.event_id,
                occurred_at=self._base_time,
                producer="captain",
                subject_version=self._job.subject_version,
                attempt=1,
                phase=FactoryPhase.FORGE_REQUESTED,
                status=FactoryBlockStatus.SUCCEEDED,
            )
        )
        await dispatcher.dispatch_next(self._job.job_id)
        await dispatcher.dispatch_next(self._job.job_id)
        self._dispatch_task = asyncio.create_task(
            dispatcher.dispatch_next(self._job.job_id)
        )
        await self._wait_until(
            lambda: self._forge is not None
            and self._forge.creation_submissions == 1
        )
        self._creation_job = self._authority_store.creation_job_for(self._job, 1)
        await self._wait_until(
            lambda: self._minibook_store.progress(
                self._creation_job.creation_job_id
            ).status
            == "awaiting_build"
        )
        minibook_assignment = self._minibook_store.build_assignment_for_job(
            self._creation_job.creation_job_id
        )
        if minibook_assignment is None:
            raise AssertionError("Minibook did not persist the V3 assignment")
        self._assignment = FactoryBuildAssignmentV2.model_validate(
            minibook_assignment.model_dump(mode="json", by_alias=True)
        )
        self._authority_store.record_assignment(self._assignment)
        checkpoint_store = FilesystemHermesFactoryWorkerCheckpointStore(
            self._root / ".captain-cook" / "hermes-worker-checkpoints"
        )
        worker_client = AuthenticatedMinibookV3WorkerClient(
            client=self._client,
            worker_token=_WORKER_TOKEN,
            worker_id=_WORKER_ID,
            checkpoint_store=checkpoint_store,
        )
        self._codex = _DeterministicCodexAdapter(
            variant=variant,
            store=self._artifacts,
            jobs=self._minibook_store,
            workspace_root=self._root / "codex-workspaces",
        )
        self._worker = HermesFactoryWorker(
            minibook=worker_client,
            codex=self._codex,
            skill_verifier=_SkillVerifier(digest=released.content_sha256),
            worker_id=_WORKER_ID,
            released_skill_name=_SKILL_NAME,
            released_skill_sha256=released.content_sha256,
            checkpoint_store=checkpoint_store,
            codex_receipts=_ReceiptReader(self._artifacts),
            clock=lambda: self._base_time,
        )
        if with_action:
            self._create_credential_action()

    def _create_credential_action(self) -> None:
        assert self._creation_job is not None
        progress = self._minibook_store.progress(self._creation_job.creation_job_id)
        checkpoint_ref = _runtime_ref(
            self._artifacts.put(
                _canonical(
                    {
                        "schema": "minibook.creation-checkpoint.v1",
                        "creation_job_id": str(self._creation_job.creation_job_id),
                        "status": progress.status,
                        "version": progress.version,
                    },
                    newline=True,
                ),
                "application/json",
                namespace="checkpoint",
            )
        )
        request = FactoryActionRequestV1(
            schema_name="captain.factory-action-request.v1",
            request_id=uuid5(
                self._creation_job.creation_job_id, "crm-readonly-credential"
            ),
            factory_job_id=self._creation_job.factory_job_id,
            creation_job_id=self._creation_job.creation_job_id,
            correlation_id=self._creation_job.correlation_id,
            expected_subject_version=self._creation_job.subject_version,
            request_type="credential_required",
            safe_checkpoint_ref=checkpoint_ref,
            safe_checkpoint_sha256=checkpoint_ref.sha256,
            required_scope="crm.account_snapshot.read",
            allowed_decisions=("approve", "deny"),
            message="Connect the approved read-only CRM account source.",
            deadline_at=self._base_time + timedelta(minutes=30),
            status="pending",
        )
        mirrored = MinibookActionRequestV1.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        self._minibook_store.record_action_request(
            mirrored,
            expected_version=progress.version,
        )
        self._action_store.create(request, tenant_id=_TENANT)
        # Re-open the persistent repository before answering; the in-memory
        # object is not used as durability evidence.
        self._action_store = FactoryActionStore(
            repository=_JsonFactoryActionRepository(
                self._action_repository_path,
                created_at=self._base_time,
            )
        )
        self._resume = FactoryActionResumeCoordinator(
            store=self._action_store,
            projection=self._projection,
            scheduler=self._continuation,
            clock=lambda: self._base_time + timedelta(seconds=10),
        )

    async def _wait_for_action(self, request_type: str) -> FactoryActionRequestV1:
        await self._wait_until(
            lambda: bool(
                self._action_store.list(
                    tenant_id=_TENANT,
                    status="pending",
                    now=self._base_time + timedelta(seconds=1),
                )
            )
        )
        actions = self._action_store.list(
            tenant_id=_TENANT,
            status="pending",
            now=self._base_time + timedelta(seconds=1),
        )
        matches = tuple(item for item in actions if item.request_type == request_type)
        if len(matches) != 1:
            raise AssertionError("expected one pending Factory action")
        return matches[0]

    async def _answer(
        self,
        action: FactoryActionRequestV1,
        *,
        credential_ref: str,
    ) -> None:
        response = FactoryActionResponseV1(
            schema_name="captain.factory-action-response.v1",
            request_id=action.request_id,
            expected_subject_version=action.expected_subject_version,
            decision="approve",
            approved_scope=action.required_scope,
            credential_ref=credential_ref,
            answer_metadata={"credential_alias": "crm-readonly"},
            answered_at=self._base_time + timedelta(seconds=2),
            idempotency_key=hashlib.sha256(
                f"{action.request_id}:approve".encode("utf-8")
            ).hexdigest(),
        )
        self._action_store.answer(
            response,
            tenant_id=_TENANT,
            now=self._base_time + timedelta(seconds=3),
        )
        self._continuation.response = response
        self._resume.resume(request_id=action.request_id, tenant_id=_TENANT)
        await self._scheduler.join()
        await self._run_worker()

    async def _run_worker(self) -> None:
        assert self._worker is not None and self._job is not None
        effect = await self._worker.run_once(
            HermesFactoryAssignmentScope(
                factory_job_id=self._job.job_id,
                correlation_id=self._job.correlation_id,
            )
        )
        if effect is None:
            assert self._creation_job is not None
            progress = self._minibook_store.progress(
                self._creation_job.creation_job_id
            )
            claimable = self._minibook_store.next_claimable_build_assignment(
                _WORKER_ID,
                now=self._base_time,
            )
            raise ValueError(
                "Hermes worker did not return an effect "
                f"(status={progress.status}, version={progress.version}, "
                f"claimable={claimable is not None})"
            )
        self._effect = effect
        assert self._authority_store is not None
        self._authority_store.record_effect(effect)
        await self._scheduler.join()

    def _finish_codex_failure(self) -> None:
        assert self._creation_job is not None
        if self._minibook_store.result(self._creation_job.creation_job_id) is not None:
            return
        progress = self._minibook_store.progress(self._creation_job.creation_job_id)
        result = MinibookCreationResultV2(
            creation_job_id=self._creation_job.creation_job_id,
            factory_job_id=self._creation_job.factory_job_id,
            correlation_id=self._creation_job.correlation_id,
            subject_version=self._creation_job.subject_version,
            attempt=self._creation_job.attempt,
            status="failed",
            failure=MinibookCreationFailure(
                code="codex_failed",
                summary="authorized Codex build did not produce typed artifacts",
            ),
        )
        self._minibook_store.finish(result, expected_version=progress.version)

    async def _wait_for_result(self) -> CreationResultV2:
        await self._await_dispatch_task()
        assert self._creation_job is not None
        stored = self._minibook_store.result(self._creation_job.creation_job_id)
        if stored is None:
            raise AssertionError("Minibook result is unavailable")
        self._result = CreationResultV2.model_validate(
            stored.model_dump(mode="json", by_alias=True)
        )
        if self._result.status == "succeeded":
            assert self._assignment is not None
            verify_sealed_team_package(
                MinibookCreationResultV2.model_validate(
                    self._result.model_dump(mode="json", by_alias=True)
                ),
                authority_job=self._minibook_store.job(
                    self._creation_job.creation_job_id
                ),
                authority_assignment=self._minibook_store.build_assignment_for_job(
                    self._creation_job.creation_job_id
                ),
                artifact_store=self._artifacts,
            )
            self._minibook_readback = True
            assert self._authority_store is not None and self._job is not None
            accepted = self._authority_store.accepted_result_for(self._job, 1)
            self._captain_readback = accepted == self._result
        return self._result

    async def _await_dispatch_task(self) -> object:
        if self._dispatch_task is None:
            raise AssertionError("Captain dispatch was not started")
        return await asyncio.wait_for(self._dispatch_task, timeout=15)

    async def _wait_until(self, predicate) -> None:
        async def wait() -> None:
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), timeout=10)

    @property
    def creation_submissions(self) -> int:
        return 0 if self._forge is None else self._forge.creation_submissions

    @property
    def codex_sessions(self) -> int:
        return 0 if self._codex is None else self._codex.sessions

    @property
    def build_effects(self) -> int:
        if self._assignment is None:
            return 0
        return int(
            self._minibook_store.build_effect(self._assignment.assignment_id)
            is not None
        )

    @property
    def team_agent_ids(self) -> tuple[str, ...]:
        if self._result is None or self._result.team_manifest_ref is None:
            return ()
        manifest = json.loads(
            self._artifacts.read_bytes(_minibook_ref(self._result.team_manifest_ref))
        )
        return tuple(item["agent_id"] for item in manifest["agents"])

    @property
    def required_tools_with_passing_tests(self) -> set[str]:
        return set() if self._codex is None else set(self._codex.tested_tools)

    @property
    def minibook_readback_verified(self) -> bool:
        return self._minibook_readback

    @property
    def captain_readback_verified(self) -> bool:
        return self._captain_readback

    @property
    def captain_success_events(self) -> tuple[FactoryEvidenceBlock, ...]:
        if self._coordinator is None or self._job is None:
            return ()
        return tuple(
            block
            for block in self._coordinator.blocks(self._job.job_id)
            if block.phase is FactoryPhase.AGENT_CODE_CREATED
            and block.status is FactoryBlockStatus.SUCCEEDED
        )


__all__ = ["FactoryV3FailedOutcome", "FactoryV3Harness", "FactoryV3Run"]
