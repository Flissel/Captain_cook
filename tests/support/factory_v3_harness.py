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
import json
import os
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Iterator, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

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
from agenten.agent_factory.codex_build_provenance import CodexBuildArtifactCas
from agenten.agent_factory.codex_build_recovery import canonical_factory_codex_model
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
    CreationJobV3,
    CreationResultV2,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from agenten.agent_factory.hermes_factory_worker import (
    AuthenticatedMinibookV3WorkerClient,
    FilesystemHermesFactoryWorkerCheckpointStore,
    HermesFactoryAssignmentScope,
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
from agenten.agent_factory.orchestration import (
    FactoryDispatch,
    FactoryDispatchError,
    FactoryDispatcher,
)
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef
from gateway.factory_action_store import (
    FactoryActionRow,
    FactoryActionStore,
)
from minibook.swarm.api import create_creation_router
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import (
    ArtifactRef as MinibookArtifactRef,
    CreationResultV2 as MinibookCreationResultV2,
    FactoryActionRequestV1 as MinibookActionRequestV1,
    FactoryActionResponseV1 as MinibookActionResponseV1,
)
from minibook.swarm.job_store import CreationJobStore
from minibook.swarm.runner import CreationRunner
from minibook.swarm.scheduler import CreationScheduler
from minibook.swarm.team_package import verify_sealed_team_package
from minibook.swarm.v3_pipeline import TeamPackageV3Validator, V3CreationPipeline
from tests.support.factory_v3_authorized_build import (
    CaptainAuthorizedHermeticCodexPort,
)


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


class _HermeticCreationArtifactStore(FilesystemCreationArtifactStore):
    """Minibook CAS with read-only routing to the separate Captain build CAS."""

    _CAPTAIN_PREFIX = "artifact://captain-codex-build/"

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._captain_cas: CodexBuildArtifactCas | None = None

    def bind_captain_cas(self, cas: CodexBuildArtifactCas) -> None:
        self._captain_cas = cas

    def local_path(self, reference: MinibookArtifactRef) -> Path:
        if reference.uri.startswith(self._CAPTAIN_PREFIX):
            if self._captain_cas is None:
                raise ValueError("Captain build CAS is unavailable")
            return self._captain_cas.local_path(_agent_runtime_ref(reference))
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
        self._leases: dict[str, object] = {}

    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ):
        if now != self._now:
            raise ValueError("Hermetic Captain clock drifted")
        lease = issue_factory_lease(
            job=job,
            role=role,
            attempt=attempt,
            workspace_ref=f"workspace://captain/factory-v3/{job.job_id}",
            now=now,
        )
        digest = hashlib.sha256(canonical_factory_codex_model(lease)).hexdigest()
        self._leases[digest] = lease
        return lease

    def for_reference(self, reference: ArtifactRef):
        try:
            return self._leases[reference.sha256]
        except KeyError as exc:
            raise ValueError("Captain lease evidence is unavailable") from exc


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
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
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

    @property
    def tampered(self) -> bool:
        return self._tampered


class _CaptainArtifactReader:
    """Captain readback over the exact CAS router used by Minibook."""

    def __init__(self, store: _HermeticCreationArtifactStore) -> None:
        self._store = store

    def read_bytes(self, reference: RuntimeArtifactRef) -> bytes:
        return self._store.read_bytes(_minibook_ref(reference))

    def read_bytes_bounded(
        self, reference: RuntimeArtifactRef, *, max_bytes: int
    ) -> bytes:
        path = self.local_path(reference)
        if path.stat().st_size > max_bytes:
            raise ValueError("Captain artifact exceeds read bound")
        return self.read_bytes(reference)

    def local_path(self, reference: RuntimeArtifactRef) -> Path:
        return self._store.local_path(_minibook_ref(reference))


@dataclass(frozen=True)
class FactoryV3FailedOutcome:
    status: Literal["failed", "blocked"]
    minibook_status: str
    captain_status: Literal["failed"]
    failure_code: str
    failure_summary: str
    tamper_performed: bool


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

    @property
    def minibook_status(self) -> str:
        return self._harness.minibook_status

    @property
    def executed_commands(self) -> tuple[tuple[str, ...], ...]:
        return self._harness.executed_commands

    @property
    def real_case_handoff_trace(self) -> tuple[str, ...]:
        return self._harness.real_case_handoff_trace

    @property
    def verified_public_assertion_ids(self) -> tuple[str, ...]:
        return self._harness.verified_public_assertion_ids

    @property
    def captain_sealed_receipt_verified(self) -> bool:
        return self._harness.captain_sealed_receipt_verified

    @property
    def build_credential_refs(self) -> tuple[str, ...]:
        return self._harness.build_credential_refs


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
        self._codex: CaptainAuthorizedHermeticCodexPort | None = None
        self._worker: HermesFactoryWorker | None = None
        self._coordinator: FactoryCoordinator | None = None
        self._authority_store: FilesystemCaptainCreationJobV3Store | None = None
        self._worker_checkpoints: FilesystemHermesFactoryWorkerCheckpointStore | None = None
        self._tampering_reader: _TamperingCaptainArtifactReader | None = None
        self._build_lease = None
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

    async def run_variant(self, variant: str) -> FactoryV3FailedOutcome:
        if variant not in {"one_agent", "null_output", "timeout", "receipt_only", "tampered_cas"}:
            raise ValueError("unknown Factory V3 variant")
        codex_variant = "happy" if variant == "tampered_cas" else variant
        await self._begin(
            codex_variant,
            tamper_captain=variant == "tampered_cas",
            with_action=False,
        )
        if variant == "one_agent":
            try:
                await self._run_worker()
            except FactoryDispatchError as exc:
                if str(exc) != "real-case execution evidence changed":
                    raise
                return self._block_worker_failure(
                    expected_kind="runtime_error",
                    failure_code="topology_drift",
                    failure_summary=str(exc),
                )
            raise AssertionError("one-agent topology was accepted")
        if variant == "null_output":
            try:
                await self._run_worker()
            except ValueError as exc:
                if str(exc) != "Hermes Factory requires a typed Codex build result":
                    raise
                return self._block_worker_failure(
                    expected_kind="validation_error",
                    failure_code="codex_null_output",
                    failure_summary=str(exc),
                )
            raise AssertionError("null Codex output was accepted")
        if variant == "timeout":
            try:
                await self._run_worker()
            except TimeoutError as exc:
                return self._block_worker_failure(
                    expected_kind="timeout",
                    failure_code="codex_timeout",
                    failure_summary=str(exc),
                )
            raise AssertionError("timed-out Codex output was accepted")

        await self._run_worker()
        await self._scheduler.join()
        if variant == "receipt_only":
            try:
                await self._await_dispatch_task()
            except FactoryDispatchError as exc:
                if (
                    str(exc)
                    != "Minibook CreationResultV1 did not produce successful agent code"
                ):
                    raise
            else:
                raise AssertionError("invalid Minibook build reached Captain success")
            return self._persisted_minibook_failure()

        assert variant == "tampered_cas"
        try:
            await self._await_dispatch_task()
        except FactoryDispatchError as exc:
            if str(exc) != "Captain CAS readback digest mismatch":
                raise
            self._record_captain_failure(
                failure_code="captain_cas_readback_rejected",
                failure_summary=str(exc),
            )
            assert self._tampering_reader is not None
            return FactoryV3FailedOutcome(
                status="blocked",
                minibook_status=self.minibook_status,
                captain_status="failed",
                failure_code="captain_cas_readback_rejected",
                failure_summary=str(exc),
                tamper_performed=self._tampering_reader.tampered,
            )
        raise AssertionError("tampered CAS reached Captain success")

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
        gateway_reader = _CaptainArtifactReader(self._artifacts)
        candidate_reader = (
            _TamperingCaptainArtifactReader(gateway_reader)
            if tamper_captain
            else gateway_reader
        )
        self._tampering_reader = (
            candidate_reader
            if isinstance(candidate_reader, _TamperingCaptainArtifactReader)
            else None
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
        self._worker_checkpoints = checkpoint_store
        worker_client = AuthenticatedMinibookV3WorkerClient(
            client=self._client,
            worker_token=_WORKER_TOKEN,
            worker_id=_WORKER_ID,
            checkpoint_store=checkpoint_store,
        )
        captain_cas = CodexBuildArtifactCas(
            self._root / ".captain-cook" / "hermetic-build-cas"
        )
        self._artifacts.bind_captain_cas(captain_cas)
        self._build_lease = leases.for_reference(self._assignment.lease_ref)
        self._codex = CaptainAuthorizedHermeticCodexPort(
            variant=variant,
            root=self._root,
            artifact_cas=captain_cas,
            assignment=self._assignment,
            creation_job=self._creation_job,
            factory_job=self._job,
            lease=self._build_lease,
            released_skill=released,
            clock=lambda: self._base_time,
        )
        self._worker = HermesFactoryWorker(
            minibook=worker_client,
            codex=self._codex,
            skill_verifier=_SkillVerifier(digest=released.content_sha256),
            worker_id=_WORKER_ID,
            released_skill_name=_SKILL_NAME,
            released_skill_sha256=released.content_sha256,
            checkpoint_store=checkpoint_store,
            codex_receipts=self._codex,
            clock=lambda: self._base_time,
        )
        if with_action:
            self._ensure_build_prerequisites(document)
        else:
            self._codex.release_credential(
                "credential://tenant-a/preapproved-crm-readonly"
            )

    def _ensure_build_prerequisites(self, document: FactoryInputDocumentV2) -> None:
        assert self._assignment is not None and self._codex is not None
        credential_tools = {
            integration.integration_key
            for integration in document.integrations
            if integration.required and integration.credential_aliases
        }
        required_tools = {
            requirement.tool_id
            for requirement in self._assignment.tool_requirements
            if requirement.required
        }
        if credential_tools & required_tools:
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
        assert self._codex is not None
        self._codex.release_credential(credential_ref)
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

    def _block_worker_failure(
        self,
        *,
        expected_kind: str,
        failure_code: str,
        failure_summary: str,
    ) -> FactoryV3FailedOutcome:
        assert (
            self._assignment is not None
            and self._creation_job is not None
            and self._worker_checkpoints is not None
        )
        checkpoint = self._worker_checkpoints.load(self._assignment)
        if (
            checkpoint is None
            or checkpoint.state != "failed"
            or checkpoint.failure_kind != expected_kind
        ):
            raise AssertionError("durable Hermes worker failure evidence changed")
        progress = self._minibook_store.progress(
            self._creation_job.creation_job_id
        )
        self._minibook_store.transition(
            self._creation_job.creation_job_id,
            "blocked",
            expected_version=progress.version,
            checkpoint="build_handoff",
        )
        self._record_captain_failure(
            failure_code=failure_code,
            failure_summary=failure_summary,
        )
        return FactoryV3FailedOutcome(
            status="blocked",
            minibook_status=self.minibook_status,
            captain_status="failed",
            failure_code=failure_code,
            failure_summary=failure_summary,
            tamper_performed=False,
        )

    def _persisted_minibook_failure(self) -> FactoryV3FailedOutcome:
        assert self._creation_job is not None
        stored = self._minibook_store.result(self._creation_job.creation_job_id)
        if stored is None or stored.status != "failed" or stored.failure is None:
            raise AssertionError("Minibook did not persist its terminal failure")
        exception_type = stored.failure.exception_type or "unknown"
        failure_summary = f"{stored.failure.summary} ({exception_type})"
        self._record_captain_failure(
            failure_code=stored.failure.code,
            failure_summary=failure_summary,
        )
        return FactoryV3FailedOutcome(
            status="failed",
            minibook_status=self.minibook_status,
            captain_status="failed",
            failure_code=stored.failure.code,
            failure_summary=failure_summary,
            tamper_performed=False,
        )

    def _record_captain_failure(
        self, *, failure_code: str, failure_summary: str
    ) -> None:
        assert (
            self._job is not None
            and self._coordinator is not None
            and self._build_lease is not None
        )
        evidence_ref = _agent_runtime_ref(
            self._artifacts.put(
                _canonical(
                    {
                        "schema": "hermes.factory-build-failure.v1",
                        "status": "failed",
                        "factory_job_id": str(self._job.job_id),
                        "failure_code": failure_code,
                        "failure_summary": failure_summary,
                    },
                    newline=True,
                ),
                "application/json",
                namespace="build-failure",
            )
        )
        self._coordinator.record(
            FactoryEvidenceBlock(
                schema_name="captain.agent-factory-block.v1",
                event_id=uuid5(self._job.job_id, "task-9-hermetic-build-failed"),
                job_id=self._job.job_id,
                correlation_id=self._job.correlation_id,
                causation_id=self._job.event_id,
                occurred_at=self._base_time,
                producer="hermes",
                subject_version=self._job.subject_version,
                attempt=1,
                phase=FactoryPhase.AGENT_CODE_CREATED,
                role=FactoryRole.TOOL_INTEGRATOR,
                status=FactoryBlockStatus.FAILED,
                artifact_refs=(evidence_ref,),
                evidence_refs=(evidence_ref,),
                lease_id=self._build_lease.lease_id,
            )
        )

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
    def minibook_status(self) -> str:
        if self._creation_job is None:
            return "unsubmitted"
        return self._minibook_store.progress(
            self._creation_job.creation_job_id
        ).status

    @property
    def executed_commands(self) -> tuple[tuple[str, ...], ...]:
        return () if self._codex is None else self._codex.executed_commands

    @property
    def real_case_handoff_trace(self) -> tuple[str, ...]:
        return () if self._codex is None else self._codex.real_case_handoff_trace

    @property
    def verified_public_assertion_ids(self) -> tuple[str, ...]:
        return (
            ()
            if self._codex is None
            else self._codex.verified_public_assertion_ids
        )

    @property
    def build_credential_refs(self) -> tuple[str, ...]:
        return () if self._codex is None else self._codex.build_credential_refs

    @property
    def captain_sealed_receipt_verified(self) -> bool:
        if self._codex is None or self._effect is None:
            return False
        receipt = self._codex.read_receipt(self._effect.codex_build_receipt_ref)
        return (
            receipt.assignment_id == self._effect.assignment_id
            and receipt.source_archive_ref == self._effect.source_archive_ref
            and receipt.test_evidence_refs == self._effect.test_evidence_refs
        )

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

    @property
    def captain_failure_events(self) -> tuple[FactoryEvidenceBlock, ...]:
        if self._coordinator is None or self._job is None:
            return ()
        return tuple(
            block
            for block in self._coordinator.blocks(self._job.job_id)
            if block.phase is FactoryPhase.AGENT_CODE_CREATED
            and block.status is FactoryBlockStatus.FAILED
        )


__all__ = ["FactoryV3FailedOutcome", "FactoryV3Harness", "FactoryV3Run"]
