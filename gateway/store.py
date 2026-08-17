"""MariaDB-backed append-only persistence for the gateway lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, Protocol, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from pymysql.err import IntegrityError, OperationalError

from gateway.capability_identity import generic_capability_subject_id
from agenten.validation.contracts import HoldoutSuite, WorkBatch
from agenten.agent_runtime.capabilities import CapabilityDenied, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    RuntimeCostEvidenceV1,
    RuntimeProviderCostSettlementV1,
    RuntimeProviderUsageReceiptV1,
    RuntimeUsagePricingSnapshotV1,
    RuntimeOperation,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementRequestV1,
    RuntimeResumeCostSettlementV1,
)
from agenten.provider_proxy.contracts import ProviderBudgetContextV1
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
    GenericBenchmarkQualityEvidenceV1,
    parse_factory_job,
)
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionRebuildReceiptV1,
)
from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkSummaryV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.leases import FactoryLeaseDenied, validate_factory_lease
from agenten.agent_factory.release_gate import (
    E2ERunEvidence,
    FactoryReleaseDecision,
    evaluate_factory_release,
    evaluate_factory_workflow_release,
    factory_evaluation_block_reason,
)
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    ReleasedHermesSkill,
)
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    CandidateRevisionV1,
    CodebaseInventoryV1,
    FactoryFeedbackV1,
    FactoryFeedbackRecommendation,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
    factory_runtime_retry_evidence_binding,
    factory_runtime_retry_evidence_binding_sha256,
)
from agenten.agent_factory.skill_sequence import FactoryRuntimeRetryAuthorizationV1
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryLifecycleError,
    FactoryLifecycleStatus,
    FactoryProjection,
    apply_block,
    next_action,
)
from agenten.agent_factory.workflow_promotion import build_release_workflow_promotion
from blockchain.Blockchain_modell import Block
from blockchain.mariadb_storage import MariaDBStorage
from gateway.contracts import (
    ActiveCodexSession,
    ArtifactBuiltPayload,
    BatchDoneEvent,
    BatchProjection,
    ClaimEvent,
    CodexSessionFinishedPayload,
    CodexSessionStartedPayload,
    CodexProcessEvent,
    DeliveryEventEnvelope,
    HeartbeatEvent,
    ReasoningSliceEvent,
    RecoveryDecisionEvent,
    ReleaseProjection,
    RuntimeOperationProjection,
    RuntimeWriteReceipt,
    FactoryJobProjection,
    FactoryBudgetReleaseRequest,
    FactoryBudgetReservationWriteReceipt,
    FactoryWorkflowArtifact,
    FactoryWorkflowArtifactWriteReceipt,
    BusinessBenchmarkSummaryWriteReceipt,
    CaptainBusinessBenchmarkValidatedPayload,
    FactoryUsageSubmissionV2,
    FactoryReleaseDecisionSubmission,
    FactorySkillAssignmentV1,
    FactoryWriteReceipt,
    FactorySkillEvaluationSubmission,
    FactorySkillWriteReceipt,
    PublishedHermesSkill,
    parse_factory_workflow_artifact,
    ReviewDecisionEvent,
    TraceContext,
    project_batch,
    project_release,
)
from gateway.release_policy import ReleaseReadiness, evaluate_release_readiness
from gateway.registry_feed import (
    factory_registry_mirror_event,
    integration_setup_registry_mirror_event,
)
from gateway.integration_setup_contracts import (
    IntegrationSetupMutationV1,
    IntegrationSetupSubmissionV1,
    IntegrationSetupWriteReceiptV1,
    PersistedIntegrationSetupV1,
    apply_integration_setup_mutation,
    validate_integration_setup_transition,
)
from agenten.agent_factory.input_contracts import RequestedIntegration
from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationConnectionV1,
    IntegrationCredentialRequirementV1,
    IntegrationSetupPlanner,
    IntegrationSetupStatus,
    N8nCredentialMetadataV1,
)
from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalSetupActionRequestV1,
    PortalSetupSelectionRequestV1,
    PortalSetupTicketUseV1,
    PortalSetupTicketV1,
    PortalTenantBindingV1,
    PortalTicketFenceV1,
    PortalTicketAction,
)
from gateway.portal_store import PortalTicketStore
from gateway.portal_live_contracts import (
    PortalLiveEvidenceQueryV1,
    PortalLiveEvidenceV1,
    PortalLiveRunDecisionV1,
    PortalLiveRunFinalizationV1,
    PortalProviderAuditQueryV1,
    PortalProviderAuditV1,
    PortalProviderProbeCompletionV1,
    PortalProviderProbeRequestV1,
    PortalProviderProbeStartedV1,
    PortalProviderProbeWriteReceiptV1,
    PortalRestartReceiptV1,
)


CAPTAIN_BLOCK_TYPES = frozenset({"problem", "work_batch", "holdout"})
GATEWAY_OWNED_EVENT_TYPES = frozenset(
    {"batch_claimed", "batch_heartbeat", "batch_approved", "recovery_decision", "review_decision"}
)
TRANSIENT_TRANSACTION_ERRORS = frozenset({1020, 1213})
TRANSACTION_ATTEMPTS = 3
TRANSACTION_RETRY_DELAYS_SECONDS = (0.05, 0.1)
WriteResult = TypeVar("WriteResult")


class BlockWrite(Protocol):
    block_type: str
    data: dict[str, Any]
    status: str
    parent_index: int | None
    metadata: dict[str, Any]


class LegacyImportWrite(Protocol):
    legacy_record_id: str
    batch_id: str
    record_type: str
    data: dict[str, Any]


class PortalCredentialMetadataSource(Protocol):
    """Injected n8n boundary that returns sanitized credential metadata only."""

    def list_credentials(
        self,
        *,
        requirement: IntegrationCredentialRequirementV1,
        job_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> tuple[N8nCredentialMetadataV1, ...]: ...


class PortalCredentialVerificationSource(Protocol):
    """Provider adapter for a harmless, digest-bound credential probe."""

    def verify_credential(
        self,
        *,
        requirement: IntegrationCredentialRequirementV1,
        credential: N8nCredentialMetadataV1,
        job_id: UUID,
        correlation_id: UUID,
        expected_content_sha256: str,
        expected_revision: int,
        expected_workflow_content_sha256: str,
        probe_id: UUID | None = None,
        now: datetime,
    ) -> CredentialVerificationReceiptV1: ...


class _IdempotentReplay(Exception):
    def __init__(self, block: dict[str, Any]):
        super().__init__("identical Captain block replay")
        self.block = block


@dataclass(frozen=True)
class AppendResult:
    event: DeliveryEventEnvelope
    replayed: bool


class _DeliveryEventReplay(Exception):
    def __init__(self, event: DeliveryEventEnvelope):
        super().__init__("identical delivery event replay")
        self.event = event


class _RuntimeReplay(Exception):
    def __init__(self, operation_id: UUID):
        super().__init__("identical runtime write replay")
        self.operation_id = operation_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _after(candidate: datetime, previous: datetime) -> datetime:
    candidate_utc = candidate.astimezone(timezone.utc)
    previous_utc = previous.astimezone(timezone.utc)
    return max(candidate_utc, previous_utc + timedelta(microseconds=1))


class RuntimeUsageArtifactReader(Protocol):
    def read_bytes(self, reference: ArtifactRef) -> bytes: ...


class GatewayStore:
    """Own all gateway queries and append-only ledger writes."""

    def __init__(
        self,
        storage: MariaDBStorage,
        *,
        claim_ttl: timedelta = timedelta(minutes=90),
        portal_credential_source: PortalCredentialMetadataSource | None = None,
        portal_verification_source: PortalCredentialVerificationSource | None = None,
        runtime_usage_artifacts: RuntimeUsageArtifactReader | None = None,
        runtime_usage_pricing_snapshots: tuple[
            RuntimeUsagePricingSnapshotV1, ...
        ] = (),
        runtime_provider_proxy_bindings: tuple[tuple[str, str, str], ...] = (),
        runtime_provider_bundle_bindings: tuple[
            tuple[str, str, str, str, str, str, str, bool], ...
        ] = (),
    ):
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        self.storage = storage
        self._claim_ttl = claim_ttl
        self._portal_credential_source = portal_credential_source
        self._portal_verification_source = portal_verification_source
        self._runtime_usage_artifacts = runtime_usage_artifacts
        self._runtime_usage_pricing_snapshots = runtime_usage_pricing_snapshots
        self._runtime_provider_proxy_bindings = frozenset(
            runtime_provider_proxy_bindings
        )
        self._runtime_provider_bundle_bindings = frozenset(
            runtime_provider_bundle_bindings
        )
        self._ensure_schema()
        self._portal_tickets = PortalTicketStore(storage)

    def configure_portal_sources(
        self,
        *,
        credential_source: PortalCredentialMetadataSource,
        verification_source: PortalCredentialVerificationSource,
    ) -> None:
        """Install the complete production pair exactly once during boot."""

        if (
            self._portal_credential_source is not None
            or self._portal_verification_source is not None
        ):
            raise RuntimeError("portal sources are already configured")
        self._portal_credential_source = credential_source
        self._portal_verification_source = verification_source

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_runtime_rpc_receipts (
                        idempotency_key CHAR(64) NOT NULL PRIMARY KEY,
                        request_id CHAR(36) NOT NULL UNIQUE,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        operation VARCHAR(64) NOT NULL,
                        request_sha256 CHAR(64) NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        INDEX idx_factory_rpc_job (job_id, created_at)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ledger_state (
                        id TINYINT NOT NULL PRIMARY KEY,
                        next_block_index BIGINT NOT NULL
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute("INSERT IGNORE INTO ledger_state (id, next_block_index) VALUES (1, 0)")
                cursor.execute("SELECT COALESCE(MAX(`index`) + 1, 0) AS next_index FROM blocks")
                next_index = cursor.fetchone()["next_index"]
                cursor.execute(
                    "UPDATE ledger_state SET next_block_index = GREATEST(next_block_index, %s) WHERE id = 1",
                    (next_index,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS validated_capabilities (
                        batch_id VARCHAR(32) NOT NULL PRIMARY KEY,
                        descriptor TEXT NOT NULL,
                        artifact_ref TEXT NULL,
                        block_index BIGINT NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        FULLTEXT INDEX idx_capability_descriptor (descriptor),
                        CONSTRAINT fk_capability_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_released_skills (
                        skill_id VARCHAR(128) NOT NULL,
                        version INT NOT NULL,
                        content_sha256 CHAR(64) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        PRIMARY KEY (skill_id, version),
                        CONSTRAINT fk_factory_released_skill_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_skill_evaluations (
                        evidence_id CHAR(36) NOT NULL PRIMARY KEY,
                        request_id CHAR(36) NOT NULL UNIQUE,
                        job_id CHAR(36) NOT NULL UNIQUE,
                        lease_id VARCHAR(128) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        CONSTRAINT fk_factory_skill_evaluation_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_skill_candidates (
                        candidate_id VARCHAR(128) NOT NULL PRIMARY KEY,
                        evidence_id CHAR(36) NOT NULL UNIQUE,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        CONSTRAINT fk_factory_skill_candidate_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_skill_tool_gaps (
                        evidence_id CHAR(36) NOT NULL,
                        gap_id VARCHAR(128) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        PRIMARY KEY (evidence_id, gap_id),
                        CONSTRAINT fk_factory_skill_gap_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_published_skills (
                        skill_id VARCHAR(128) NOT NULL,
                        version INT NOT NULL,
                        evaluation_id CHAR(36) NOT NULL UNIQUE,
                        candidate_id VARCHAR(128) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        PRIMARY KEY (skill_id, version),
                        CONSTRAINT fk_factory_published_skill_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_release_decisions (
                        decision_id CHAR(64) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL,
                        evaluation_id CHAR(36) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        INDEX idx_factory_release_decision_job (job_id, block_index),
                        CONSTRAINT fk_factory_release_decision_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_skill_assignments (
                        job_id CHAR(36) NOT NULL,
                        step VARCHAR(32) NOT NULL,
                        skill_id VARCHAR(128) NOT NULL,
                        skill_version INT NOT NULL,
                        content_sha256 CHAR(64) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        PRIMARY KEY (job_id, step),
                        CONSTRAINT fk_factory_skill_assignment_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_budget_events (
                        event_id CHAR(36) NOT NULL PRIMARY KEY,
                        reservation_id CHAR(36) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        event_kind VARCHAR(16) NOT NULL,
                        content_sha256 CHAR(64) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        INDEX idx_factory_budget_job (job_id, block_index),
                        INDEX idx_factory_budget_reservation (reservation_id, block_index),
                        CONSTRAINT fk_factory_budget_event_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_resume_authorizations (
                        authorization_receipt_id CHAR(36) NOT NULL PRIMARY KEY,
                        reservation_id CHAR(36) NOT NULL UNIQUE,
                        run_id CHAR(36) NOT NULL,
                        input_id VARCHAR(255) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        maximum_cost_usd DECIMAL(18, 6) NOT NULL,
                        booked_cost_usd DECIMAL(18, 6) NOT NULL,
                        resume_ceiling_usd DECIMAL(18, 6) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        UNIQUE KEY uq_capability_resume_run_input (run_id, input_id),
                        CONSTRAINT chk_capability_resume_costs CHECK (
                            maximum_cost_usd = booked_cost_usd + resume_ceiling_usd
                            AND booked_cost_usd >= 0
                            AND resume_ceiling_usd > 0
                        ),
                        CONSTRAINT fk_capability_resume_authorization_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_workflow_artifacts (
                        invocation_id CHAR(36) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        subject_version INT NOT NULL,
                        attempt INT NOT NULL,
                        schema_name VARCHAR(96) NOT NULL,
                        content_sha256 CHAR(64) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        INDEX idx_factory_workflow_job (job_id, block_index),
                        CONSTRAINT fk_factory_workflow_artifact_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_business_benchmark_summaries (
                        summary_id CHAR(36) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        subject_version INT NOT NULL,
                        attempt INT NOT NULL,
                        candidate_sha256 CHAR(64) NOT NULL,
                        artifact_sha256 CHAR(64) NOT NULL UNIQUE,
                        content_sha256 CHAR(64) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        UNIQUE KEY uq_factory_benchmark_identity
                            (job_id, correlation_id, subject_version, attempt, candidate_sha256),
                        INDEX idx_factory_benchmark_job (job_id, block_index),
                        CONSTRAINT fk_factory_benchmark_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_integration_setup_events (
                        event_id CHAR(36) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        subject_version INT NOT NULL,
                        revision INT NOT NULL,
                        content_sha256 CHAR(64) NOT NULL,
                        previous_content_sha256 CHAR(64) NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        UNIQUE KEY uq_factory_integration_setup_revision
                            (job_id, correlation_id, subject_version, revision),
                        INDEX idx_factory_integration_setup_latest
                            (job_id, revision),
                        CONSTRAINT fk_factory_integration_setup_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_provider_probe_starts (
                        probe_request_id CHAR(36) NOT NULL PRIMARY KEY,
                        run_id VARCHAR(128) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        INDEX idx_portal_probe_start_run
                            (run_id, job_id, correlation_id, block_index),
                        CONSTRAINT fk_portal_probe_start_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_provider_probe_completions (
                        probe_request_id CHAR(36) NOT NULL PRIMARY KEY,
                        trace_id CHAR(36) NOT NULL UNIQUE,
                        run_id VARCHAR(128) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        INDEX idx_portal_probe_completion_run
                            (run_id, job_id, correlation_id, block_index),
                        CONSTRAINT fk_portal_probe_completion_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE,
                        CONSTRAINT fk_portal_probe_completion_start
                            FOREIGN KEY (probe_request_id)
                            REFERENCES portal_provider_probe_starts (probe_request_id)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_restart_receipts (
                        restart_id CHAR(36) NOT NULL PRIMARY KEY,
                        restart_request_id CHAR(36) NOT NULL UNIQUE,
                        run_id VARCHAR(128) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        CONSTRAINT fk_portal_restart_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS minibook_projection_rebuild_receipts (
                        rebuild_id CHAR(36) NOT NULL PRIMARY KEY,
                        run_id VARCHAR(128) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        projection_event_id CHAR(36) NOT NULL,
                        acknowledgement_id CHAR(36) NOT NULL,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        CONSTRAINT fk_minibook_rebuild_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_live_run_decisions (
                        decision_id CHAR(36) NOT NULL PRIMARY KEY,
                        decision_request_id CHAR(36) NOT NULL UNIQUE,
                        run_id VARCHAR(128) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        decision_sha256 CHAR(64) NOT NULL UNIQUE,
                        block_index BIGINT NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        UNIQUE KEY uq_portal_live_run
                            (run_id, job_id, correlation_id),
                        CONSTRAINT fk_portal_live_run_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )

    @contextmanager
    def _integration_setup_lock(self, job_id: UUID) -> Iterator[None]:
        lock_name = f"captain:integration-setup:{job_id}"
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 30) AS acquired", (lock_name,))
                row = cursor.fetchone()
                if row is None or int(row["acquired"] or 0) != 1:
                    raise HTTPException(status_code=409, detail="integration setup is busy")
                try:
                    yield
                finally:
                    cursor.execute("SELECT RELEASE_LOCK(%s) AS released", (lock_name,))

    def record_integration_setup(
        self,
        submission: IntegrationSetupSubmissionV1,
        *,
        controlled_mutation: bool = False,
    ) -> IntegrationSetupWriteReceiptV1:
        with self._integration_setup_lock(submission.job_id):
            return self._record_integration_setup_locked(
                submission,
                controlled_mutation=controlled_mutation,
            )

    def _record_integration_setup_locked(
        self,
        submission: IntegrationSetupSubmissionV1,
        *,
        controlled_mutation: bool = False,
    ) -> IntegrationSetupWriteReceiptV1:
        if submission.change_kind != "observed" and not controlled_mutation:
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation requires its dedicated route",
            )
        canonical = submission.model_dump(mode="json", by_alias=True)
        digest = self._canonical_model_sha256(submission)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT payload, content_sha256, revision
                       FROM factory_integration_setup_events
                       WHERE event_id = %s FOR UPDATE""",
                    (str(submission.event_id),),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    if self._decode_json(replay["payload"]) != canonical:
                        raise HTTPException(
                            status_code=409,
                            detail="integration setup event already exists with different content",
                        )
                    return IntegrationSetupWriteReceiptV1(
                        event_id=submission.event_id,
                        job_id=submission.job_id,
                        revision=int(replay["revision"]),
                        content_sha256=str(replay["content_sha256"]),
                        replayed=True,
                    )

                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(submission.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                if (
                    job.correlation_id != submission.correlation_id
                    or job.subject_version != submission.subject_version
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="integration setup does not match its factory job",
                    )

                cursor.execute(
                    """SELECT revision, content_sha256, payload
                       FROM factory_integration_setup_events
                       WHERE job_id = %s
                       ORDER BY revision DESC LIMIT 1 FOR UPDATE""",
                    (str(submission.job_id),),
                )
                previous = cursor.fetchone()
                expected_revision = 1 if previous is None else int(previous["revision"]) + 1
                expected_digest = None if previous is None else str(previous["content_sha256"])
                if (
                    submission.revision != expected_revision
                    or submission.previous_content_sha256 != expected_digest
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="integration setup revision fence mismatch",
                    )
                if previous is not None:
                    prior_submission = IntegrationSetupSubmissionV1.model_validate(
                        self._decode_json(previous["payload"])
                    )
                    try:
                        validate_integration_setup_transition(
                            prior_submission,
                            submission,
                        )
                    except ValueError:
                        raise HTTPException(
                            status_code=409,
                            detail="integration setup transition is not allowed",
                        ) from None

                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_integration_setup",
                    data=canonical,
                    status="accepted",
                    parent_index=job_block["index"],
                    metadata={
                        "schema": submission.schema_name,
                        "content_sha256": digest,
                    },
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_integration_setup_events
                       (event_id, job_id, correlation_id, subject_version, revision,
                        content_sha256, previous_content_sha256, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(submission.event_id),
                        str(submission.job_id),
                        str(submission.correlation_id),
                        submission.subject_version,
                        submission.revision,
                        digest,
                        submission.previous_content_sha256,
                        index,
                        json.dumps(canonical, sort_keys=True, separators=(",", ":")),
                    ),
                )
        return IntegrationSetupWriteReceiptV1(
            event_id=submission.event_id,
            job_id=submission.job_id,
            revision=submission.revision,
            content_sha256=digest,
            replayed=False,
        )

    def mutate_integration_setup(
        self,
        job_id: UUID,
        mutation: IntegrationSetupMutationV1,
    ) -> IntegrationSetupWriteReceiptV1:
        with self._integration_setup_lock(job_id):
            return self._mutate_integration_setup_locked(job_id, mutation)

    def _mutate_integration_setup_locked(
        self,
        job_id: UUID,
        mutation: IntegrationSetupMutationV1,
    ) -> IntegrationSetupWriteReceiptV1:
        replay = self._integration_setup_mutation_replay(job_id, mutation)
        if replay is not None:
            return replay
        try:
            submission = apply_integration_setup_mutation(
                self.integration_setup(job_id),
                mutation,
            )
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation is not allowed",
            ) from None
        return self._record_integration_setup_locked(
            submission,
            controlled_mutation=True,
        )

    def _integration_setup_mutation_replay(
        self,
        job_id: UUID,
        mutation: IntegrationSetupMutationV1,
    ) -> IntegrationSetupWriteReceiptV1 | None:
        """Return only an exact persisted rotation/revoke mutation replay."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT job_id, revision, content_sha256, payload
                       FROM factory_integration_setup_events
                       WHERE event_id = %s FOR UPDATE""",
                    (str(mutation.event_id),),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        stored = IntegrationSetupSubmissionV1.model_validate(
            self._decode_json(row["payload"])
        )
        if (
            str(row["job_id"]) != str(job_id)
            or stored.change_kind != mutation.action
            or stored.previous_content_sha256 != mutation.expected_content_sha256
            or stored.occurred_at != mutation.occurred_at
        ):
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation event already exists with different content",
            )
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT payload
                       FROM factory_integration_setup_events
                       WHERE job_id = %s AND content_sha256 = %s
                       FOR UPDATE""",
                    (str(job_id), mutation.expected_content_sha256),
                )
                predecessor = cursor.fetchone()
        if predecessor is None:
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation event already exists with different content",
            )
        try:
            expected = apply_integration_setup_mutation(
                PersistedIntegrationSetupV1(
                    submission=IntegrationSetupSubmissionV1.model_validate(
                        self._decode_json(predecessor["payload"])
                    ),
                    content_sha256=mutation.expected_content_sha256,
                ),
                mutation,
            )
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation event already exists with different content",
            ) from None
        if expected != stored:
            raise HTTPException(
                status_code=409,
                detail="integration setup mutation event already exists with different content",
            )
        return IntegrationSetupWriteReceiptV1(
            event_id=mutation.event_id,
            job_id=job_id,
            revision=int(row["revision"]),
            content_sha256=str(row["content_sha256"]),
            replayed=True,
        )

    def integration_setup(self, job_id: UUID) -> PersistedIntegrationSetupV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT payload, content_sha256
                       FROM factory_integration_setup_events
                       WHERE job_id = %s
                       ORDER BY revision DESC LIMIT 1""",
                    (str(job_id),),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="integration setup not found")
        return PersistedIntegrationSetupV1(
            submission=IntegrationSetupSubmissionV1.model_validate(
                self._decode_json(row["payload"])
            ),
            content_sha256=str(row["content_sha256"]),
        )

    def portal_integration_setup(
        self,
        job_id: UUID,
        organization_id: str,
    ) -> PersistedIntegrationSetupV1:
        if not self._portal_tickets.organization_owns_setup(job_id, organization_id):
            raise HTTPException(status_code=404, detail="integration setup not found")
        return self.integration_setup(job_id)

    def provision_portal_tenant(self, binding: PortalTenantBindingV1) -> bool:
        """Provision immutable portal ownership after Captain validates the setup."""

        self.integration_setup(binding.job_id)
        try:
            return self._portal_tickets.provision_organization(
                binding.job_id,
                binding.organization_id,
            )
        except ValueError:
            raise HTTPException(status_code=409, detail="portal tenant binding conflict") from None

    def issue_portal_ticket(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        credential_alias: str,
        action: PortalTicketAction,
        now: datetime,
    ) -> PortalSetupTicketV1:
        try:
            return self._portal_tickets.issue(
                job_id=job_id,
                principal=principal,
                credential_alias=credential_alias,
                action=action,
                now=now,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="integration setup not found") from None

    def portal_discover(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupTicketUseV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        with self._integration_setup_lock(job_id):
            return self._portal_discover_locked(
                job_id=job_id,
                principal=principal,
                request=request,
                now=now,
            )

    def _portal_discover_locked(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupTicketUseV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        persisted, target_index = self._consume_portal_ticket(
            job_id=job_id,
            principal=principal,
            request=request,
            action="discover",
            now=now,
        )
        if self._portal_credential_source is None:
            raise HTTPException(status_code=503, detail="credential discovery unavailable")
        target = persisted.submission.plan.connections[target_index]
        credentials = self._portal_credential_source.list_credentials(
            requirement=target.requirement,
            job_id=job_id,
            correlation_id=persisted.submission.correlation_id,
            now=now.astimezone(timezone.utc),
        )
        replacement = self._resolve_portal_connection(
            target.requirement,
            credentials=credentials,
            selected_credential_id=None,
            verification_receipt=target.verification_receipt,
            now=now,
        )
        return self._record_portal_observation(persisted, target_index, replacement, now)

    def portal_select(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupSelectionRequestV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        with self._integration_setup_lock(job_id):
            return self._portal_select_locked(
                job_id=job_id,
                principal=principal,
                request=request,
                now=now,
            )

    def _portal_select_locked(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupSelectionRequestV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        persisted, target_index = self._consume_portal_ticket(
            job_id=job_id,
            principal=principal,
            request=request,
            action="select",
            now=now,
        )
        target = persisted.submission.plan.connections[target_index]
        try:
            replacement = self._resolve_portal_connection(
                target.requirement,
                credentials=target.candidate_credentials,
                selected_credential_id=request.credential_id,
                verification_receipt=None,
                now=now,
            )
        except ValueError:
            raise HTTPException(status_code=409, detail="credential selection is not allowed") from None
        return self._record_portal_observation(persisted, target_index, replacement, now)

    def portal_verify(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupTicketUseV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        with self._integration_setup_lock(job_id):
            return self._portal_verify_locked(
                job_id=job_id,
                principal=principal,
                request=request,
                now=now,
            )

    def _portal_verify_locked(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupTicketUseV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        persisted, target_index = self._consume_portal_ticket(
            job_id=job_id,
            principal=principal,
            request=request,
            action="verify",
            now=now,
        )
        if self._portal_verification_source is None:
            raise HTTPException(status_code=503, detail="credential verification unavailable")
        target = persisted.submission.plan.connections[target_index]
        selected = target.selected_credential
        if selected is None:
            raise HTTPException(status_code=409, detail="credential verification is not allowed")
        expected_workflow_sha256 = target.requirement.verification_workflow_sha256
        if expected_workflow_sha256 is None:
            raise HTTPException(status_code=409, detail="credential verification is not allowed")
        try:
            returned = self._portal_verification_source.verify_credential(
                requirement=target.requirement,
                credential=selected,
                job_id=job_id,
                correlation_id=persisted.submission.correlation_id,
                expected_content_sha256=persisted.content_sha256,
                expected_revision=persisted.submission.revision,
                expected_workflow_content_sha256=expected_workflow_sha256,
                now=now.astimezone(timezone.utc),
            )
            receipt = CredentialVerificationReceiptV1.model_validate(returned)
            if receipt.template_content_sha256 != expected_workflow_sha256:
                raise ValueError("verification template digest mismatch")
            replacement = self._resolve_portal_connection(
                target.requirement,
                credentials=target.candidate_credentials,
                selected_credential_id=selected.credential_id,
                verification_receipt=receipt,
                now=now,
            )
        except Exception:
            raise HTTPException(status_code=502, detail="credential verification failed") from None
        return self._record_portal_observation(persisted, target_index, replacement, now)

    def portal_mutate(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupActionRequestV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        with self._integration_setup_lock(job_id):
            return self._portal_mutate_locked(
                job_id=job_id,
                principal=principal,
                request=request,
                now=now,
            )

    def _portal_mutate_locked(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupActionRequestV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        persisted, _ = self._consume_portal_ticket(
            job_id=job_id,
            principal=principal,
            request=request,
            action=request.action,
            now=now,
        )
        occurred_at = _after(now, persisted.submission.occurred_at)
        self._mutate_integration_setup_locked(
            job_id,
            IntegrationSetupMutationV1(
                event_id=uuid4(),
                credential_alias=request.credential_alias,
                expected_content_sha256=persisted.content_sha256,
                occurred_at=occurred_at,
                action=request.action,
            ),
        )
        return self.portal_integration_setup(job_id, principal.organization_id)

    def record_portal_provider_probe_start(
        self,
        request: PortalProviderProbeRequestV1,
        *,
        occurred_at: datetime,
    ) -> PortalProviderProbeWriteReceiptV1:
        started = PortalProviderProbeStartedV1(request=request, occurred_at=occurred_at)
        canonical = started.model_dump(mode="json")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM portal_provider_probe_starts "
                    "WHERE probe_request_id = %s FOR UPDATE",
                    (str(request.probe_request_id),),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    stored_start = PortalProviderProbeStartedV1.model_validate(
                        self._decode_json(replay["payload"])
                    )
                    if stored_start.request != request:
                        raise HTTPException(
                            status_code=409,
                            detail="provider probe request already exists with different content",
                        )
                    return PortalProviderProbeWriteReceiptV1(
                        probe_request_id=request.probe_request_id,
                        status="started",
                        replayed=True,
                    )
                cursor.execute(
                    "SELECT payload, content_sha256 FROM factory_integration_setup_events "
                    "WHERE job_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
                    (str(request.job_id),),
                )
                setup_row = cursor.fetchone()
                if setup_row is None:
                    raise HTTPException(status_code=409, detail="integration setup is unavailable")
                setup = IntegrationSetupSubmissionV1.model_validate(
                    self._decode_json(setup_row["payload"])
                )
                self._assert_provider_probe_matches_setup(
                    request,
                    setup=setup,
                    content_sha256=str(setup_row["content_sha256"]),
                )
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(request.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job is unavailable")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="portal_provider_probe_started",
                    data=canonical,
                    status="started",
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.portal-provider-probe-started.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO portal_provider_probe_starts
                       (probe_request_id, run_id, job_id, correlation_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        str(request.probe_request_id),
                        request.run_id,
                        str(request.job_id),
                        str(request.correlation_id),
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return PortalProviderProbeWriteReceiptV1(
            probe_request_id=request.probe_request_id,
            status="started",
            replayed=False,
        )

    def record_portal_provider_probe_completion(
        self,
        completion: PortalProviderProbeCompletionV1,
    ) -> PortalProviderProbeWriteReceiptV1:
        canonical = completion.model_dump(mode="json")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM portal_provider_probe_completions "
                    "WHERE probe_request_id = %s FOR UPDATE",
                    (str(completion.probe_request_id),),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    if self._decode_json(replay["payload"]) != canonical:
                        raise HTTPException(
                            status_code=409,
                            detail="provider probe completion already exists with different content",
                        )
                    return PortalProviderProbeWriteReceiptV1(
                        probe_request_id=completion.probe_request_id,
                        trace_id=completion.trace_id,
                        status="passed",
                        replayed=True,
                    )
                cursor.execute(
                    "SELECT payload FROM portal_provider_probe_starts "
                    "WHERE probe_request_id = %s FOR UPDATE",
                    (str(completion.probe_request_id),),
                )
                start_row = cursor.fetchone()
                if start_row is None:
                    raise HTTPException(status_code=409, detail="provider probe start is unavailable")
                started = PortalProviderProbeStartedV1.model_validate(
                    self._decode_json(start_row["payload"])
                )
                self._assert_provider_probe_completion(started, completion)
                cursor.execute(
                    "SELECT probe_request_id FROM portal_provider_probe_completions "
                    "WHERE trace_id = %s FOR UPDATE",
                    (str(completion.trace_id),),
                )
                if cursor.fetchone() is not None:
                    raise HTTPException(status_code=409, detail="provider trace already exists")
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(completion.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job is unavailable")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="portal_provider_probe_completed",
                    data=canonical,
                    status="passed",
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.portal-provider-probe-completion.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO portal_provider_probe_completions
                       (probe_request_id, trace_id, run_id, job_id, correlation_id,
                        block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(completion.probe_request_id),
                        str(completion.trace_id),
                        completion.run_id,
                        str(completion.job_id),
                        str(completion.correlation_id),
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return PortalProviderProbeWriteReceiptV1(
            probe_request_id=completion.probe_request_id,
            trace_id=completion.trace_id,
            status="passed",
            replayed=False,
        )

    def portal_provider_audit(
        self,
        query: PortalProviderAuditQueryV1,
        *,
        observed_at: datetime,
    ) -> PortalProviderAuditV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                values = (query.run_id, str(query.job_id), str(query.correlation_id))
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM portal_provider_probe_starts "
                    "WHERE run_id = %s AND job_id = %s AND correlation_id = %s",
                    values,
                )
                invocation_count = int(cursor.fetchone()["count"])
                cursor.execute(
                    "SELECT trace_id FROM portal_provider_probe_completions "
                    "WHERE run_id = %s AND job_id = %s AND correlation_id = %s "
                    "ORDER BY block_index",
                    values,
                )
                trace_ids = tuple(UUID(str(row["trace_id"])) for row in cursor.fetchall())
        return PortalProviderAuditV1(
            **query.model_dump(),
            invocation_count=invocation_count,
            completion_count=len(trace_ids),
            trace_ids=trace_ids,
            observed_at=observed_at,
        )

    def portal_provider_probe_completion(
        self,
        probe_request_id: UUID,
    ) -> PortalProviderProbeCompletionV1 | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM portal_provider_probe_completions "
                    "WHERE probe_request_id = %s",
                    (str(probe_request_id),),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return PortalProviderProbeCompletionV1.model_validate(
            self._decode_json(row["payload"])
        )

    def record_portal_restart_receipt(
        self,
        receipt: PortalRestartReceiptV1,
    ) -> PortalRestartReceiptV1:
        canonical = receipt.model_dump(mode="json")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM portal_restart_receipts "
                    "WHERE restart_id = %s OR restart_request_id = %s FOR UPDATE",
                    (str(receipt.restart_id), str(receipt.restart_request_id)),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    stored = PortalRestartReceiptV1.model_validate(
                        self._decode_json(replay["payload"])
                    )
                    if stored != receipt:
                        raise HTTPException(
                            status_code=409,
                            detail="restart receipt already exists with different content",
                        )
                    return stored
                cursor.execute(
                    "SELECT payload, content_sha256 FROM factory_integration_setup_events "
                    "WHERE job_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
                    (str(receipt.job_id),),
                )
                setup_row = cursor.fetchone()
                if setup_row is None:
                    raise HTTPException(status_code=409, detail="integration setup is unavailable")
                setup = IntegrationSetupSubmissionV1.model_validate(
                    self._decode_json(setup_row["payload"])
                )
                if (
                    setup.correlation_id != receipt.correlation_id
                    or setup.revision != receipt.setup_revision
                    or str(setup_row["content_sha256"]) != receipt.setup_content_sha256
                ):
                    raise HTTPException(status_code=409, detail="restart setup fence mismatch")
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(receipt.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job is unavailable")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="portal_restart_completed",
                    data=canonical,
                    status="resumed",
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.portal-restart-receipt.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO portal_restart_receipts
                       (restart_id, restart_request_id, run_id, job_id,
                        correlation_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(receipt.restart_id),
                        str(receipt.restart_request_id),
                        receipt.run_id,
                        str(receipt.job_id),
                        str(receipt.correlation_id),
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return receipt

    def finalize_portal_live_run(
        self,
        request: PortalLiveRunFinalizationV1,
    ) -> PortalLiveRunDecisionV1:
        """Accept one fully fenced run after all immutable evidence exists."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT payload FROM portal_live_run_decisions
                       WHERE decision_request_id = %s
                          OR (run_id = %s AND job_id = %s AND correlation_id = %s)
                       FOR UPDATE""",
                    (
                        str(request.decision_request_id),
                        request.run_id,
                        str(request.job_id),
                        str(request.correlation_id),
                    ),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    decision = PortalLiveRunDecisionV1.model_validate(
                        self._decode_json(replay["payload"])
                    )
                    if (
                        decision.decision_request_id != request.decision_request_id
                        or decision.run_id != request.run_id
                        or decision.job_id != request.job_id
                        or decision.correlation_id != request.correlation_id
                        or tuple(trace.trace_id for trace in decision.provider_traces)
                        != request.provider_trace_ids
                        or decision.restart_receipt.restart_id != request.restart_id
                        or decision.minibook_rebuild_receipt.rebuild_id
                        != request.minibook_rebuild_id
                        or decision.policy_version != request.policy_version
                        or decision.occurred_at != request.occurred_at
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="portal live run already finalized with different content",
                        )
                    return decision

                cursor.execute(
                    """SELECT payload, content_sha256, revision
                       FROM factory_integration_setup_events
                       WHERE job_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE""",
                    (str(request.job_id),),
                )
                setup_row = cursor.fetchone()
                if setup_row is None:
                    raise HTTPException(status_code=409, detail="integration setup is unavailable")
                setup = IntegrationSetupSubmissionV1.model_validate(
                    self._decode_json(setup_row["payload"])
                )
                if setup.correlation_id != request.correlation_id:
                    raise HTTPException(status_code=409, detail="live run setup fence mismatch")

                cursor.execute(
                    """SELECT trace_id FROM portal_provider_probe_completions
                       WHERE run_id = %s AND job_id = %s AND correlation_id = %s
                       ORDER BY block_index FOR UPDATE""",
                    (request.run_id, str(request.job_id), str(request.correlation_id)),
                )
                stored_trace_ids = tuple(
                    UUID(str(row["trace_id"])) for row in cursor.fetchall()
                )
                if (
                    len(stored_trace_ids) != 3
                    or set(stored_trace_ids) != set(request.provider_trace_ids)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="live run requires exactly the three submitted provider traces",
                    )

                traces: list[PortalProviderProbeCompletionV1] = []
                for trace_id in request.provider_trace_ids:
                    cursor.execute(
                        """SELECT payload FROM portal_provider_probe_completions
                           WHERE trace_id = %s FOR UPDATE""",
                        (str(trace_id),),
                    )
                    trace_row = cursor.fetchone()
                    if trace_row is None:
                        raise HTTPException(
                            status_code=409,
                            detail="portal provider trace is unavailable",
                        )
                    traces.append(
                        PortalProviderProbeCompletionV1.model_validate(
                            self._decode_json(trace_row["payload"])
                        )
                    )

                cursor.execute(
                    "SELECT payload FROM portal_restart_receipts WHERE restart_id = %s FOR UPDATE",
                    (str(request.restart_id),),
                )
                restart_row = cursor.fetchone()
                if restart_row is None:
                    raise HTTPException(status_code=409, detail="portal restart receipt is unavailable")
                restart = PortalRestartReceiptV1.model_validate(
                    self._decode_json(restart_row["payload"])
                )

                cursor.execute(
                    """SELECT payload FROM minibook_projection_rebuild_receipts
                       WHERE rebuild_id = %s FOR UPDATE""",
                    (str(request.minibook_rebuild_id),),
                )
                rebuild_row = cursor.fetchone()
                if rebuild_row is None:
                    raise HTTPException(status_code=409, detail="Minibook rebuild receipt is unavailable")
                rebuild = MinibookProjectionRebuildReceiptV1.model_validate(
                    self._decode_json(rebuild_row["payload"])
                )

                execution_content = json.dumps(
                    [trace.execution_ref.model_dump(mode="json") for trace in traces],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                execution_sha256 = hashlib.sha256(execution_content).hexdigest()
                decision = PortalLiveRunDecisionV1(
                    decision_id=uuid5(
                        NAMESPACE_URL,
                        f"captain:portal-live-decision:{request.decision_request_id}",
                    ),
                    decision_request_id=request.decision_request_id,
                    run_id=request.run_id,
                    job_id=request.job_id,
                    correlation_id=request.correlation_id,
                    setup_revision=int(setup_row["revision"]),
                    setup_content_sha256=str(setup_row["content_sha256"]),
                    provider_traces=tuple(traces),
                    restart_receipt=restart,
                    minibook_rebuild_receipt=rebuild,
                    gateway_execution_ref=ArtifactRef(
                        uri=f"artifact://gateway-execution/{execution_sha256}",
                        sha256=execution_sha256,
                        media_type="application/json",
                    ),
                    policy_version=request.policy_version,
                    status="accepted",
                    occurred_at=request.occurred_at,
                )
                canonical = decision.model_dump(mode="json", by_alias=True)
                decision_sha256 = self._canonical_model_sha256(decision)
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(request.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job is unavailable")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="portal_live_run_accepted",
                    data=canonical,
                    status="accepted",
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.portal-live-run-decision.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO portal_live_run_decisions
                       (decision_id, decision_request_id, run_id, job_id,
                        correlation_id, decision_sha256, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(decision.decision_id),
                        str(decision.decision_request_id),
                        decision.run_id,
                        str(decision.job_id),
                        str(decision.correlation_id),
                        decision_sha256,
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return decision

    def portal_live_evidence(
        self,
        query: PortalLiveEvidenceQueryV1,
    ) -> PortalLiveEvidenceV1:
        """Return a deterministic aggregate without appending ledger state."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT decision_sha256, payload FROM portal_live_run_decisions
                       WHERE run_id = %s AND job_id = %s AND correlation_id = %s""",
                    (query.run_id, str(query.job_id), str(query.correlation_id)),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="portal live evidence is unavailable")
        decision = PortalLiveRunDecisionV1.model_validate(
            self._decode_json(row["payload"])
        )

        def reference(kind: str, value: object) -> ArtifactRef:
            content = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                uri=f"artifact://{kind}/{digest}",
                sha256=digest,
                media_type="application/json",
            )

        rebuild = decision.minibook_rebuild_receipt
        return PortalLiveEvidenceV1(
            **query.model_dump(),
            provider_traces=decision.provider_traces,
            gitea_release_sha256=tuple(
                sorted({trace.template_release.sha256 for trace in decision.provider_traces})
            ),
            gateway_decision_ref=ArtifactRef(
                uri=f"artifact://gateway-decision/{row['decision_sha256']}",
                sha256=str(row["decision_sha256"]),
                media_type="application/json",
            ),
            gateway_execution_ref=decision.gateway_execution_ref,
            restart_ref=reference(
                "portal-restart", decision.restart_receipt.model_dump(mode="json")
            ),
            minibook_projection_ref=reference(
                "minibook-projection", str(rebuild.acknowledgement_id)
            ),
            minibook_rebuild_ref=reference(
                "minibook-rebuild", rebuild.model_dump(mode="json")
            ),
            status="accepted",
        )

    def run_portal_provider_probe(
        self,
        request: PortalProviderProbeRequestV1,
        *,
        now: datetime,
    ) -> PortalProviderProbeCompletionV1:
        if self._portal_verification_source is None:
            raise HTTPException(status_code=503, detail="credential verification unavailable")
        started = self.record_portal_provider_probe_start(request, occurred_at=now)
        if started.replayed:
            completed = self.portal_provider_probe_completion(request.probe_request_id)
            if completed is None:
                raise HTTPException(
                    status_code=409,
                    detail="provider probe already started without completion",
                )
            return completed

        persisted = self.integration_setup(request.job_id)
        target = next(
            connection
            for connection in persisted.submission.plan.connections
            if connection.requirement.credential_alias == request.credential_alias
        )
        selected = target.selected_credential
        assert selected is not None
        probe_time = _after(now, now)
        try:
            returned = self._portal_verification_source.verify_credential(
                requirement=target.requirement,
                credential=selected,
                job_id=request.job_id,
                correlation_id=request.correlation_id,
                expected_content_sha256=request.setup_content_sha256,
                expected_revision=request.setup_revision,
                expected_workflow_content_sha256=request.verification_template_sha256,
                probe_id=request.probe_request_id,
                now=probe_time,
            )
            receipt = CredentialVerificationReceiptV1.model_validate(returned)
            if (
                receipt.provider_trace_id is None
                or receipt.provider_proof_sha256 is None
                or receipt.provider_kind != request.integration_kind
                or receipt.provider_probe_id is None
            ):
                raise ValueError("provider receipt is incomplete or mismatched")
            IntegrationConnectionV1.model_validate(
                target.model_copy(update={"verification_receipt": receipt})
            )
        except Exception:
            raise HTTPException(status_code=502, detail="credential verification failed") from None

        template_ref = receipt.template_ref or receipt.workflow_ref
        if receipt.verification_release is None:
            raise HTTPException(status_code=502, detail="credential verification failed")
        completion = PortalProviderProbeCompletionV1(
            probe_request_id=request.probe_request_id,
            trace_id=receipt.provider_trace_id,
            run_id=request.run_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            integration_kind=request.integration_kind,
            credential_alias=request.credential_alias,
            credential_id=request.credential_id,
            setup_revision=request.setup_revision,
            setup_content_sha256=request.setup_content_sha256,
            template_ref=template_ref,
            template_release=receipt.verification_release,
            deployed_workflow_ref=receipt.workflow_ref,
            execution_ref=receipt.execution_ref,
            provider_proof_sha256=receipt.provider_proof_sha256,
            provider_probe_id=receipt.provider_probe_id,
            oauth_grant_type=receipt.oauth_grant_type,
            oauth_exchange_id=receipt.oauth_exchange_id,
            oauth_exchange_ref=receipt.oauth_exchange_ref,
            status="passed",
            occurred_at=receipt.occurred_at,
        )
        self.record_portal_provider_probe_completion(completion)
        return completion

    @staticmethod
    def _assert_provider_probe_matches_setup(
        request: PortalProviderProbeRequestV1,
        *,
        setup: IntegrationSetupSubmissionV1,
        content_sha256: str,
    ) -> None:
        if (
            setup.job_id != request.job_id
            or setup.correlation_id != request.correlation_id
            or setup.revision != request.setup_revision
            or content_sha256 != request.setup_content_sha256
        ):
            raise HTTPException(status_code=409, detail="provider probe setup fence mismatch")
        matches = tuple(
            connection
            for connection in setup.plan.connections
            if connection.requirement.credential_alias == request.credential_alias
        )
        if len(matches) != 1:
            raise HTTPException(status_code=409, detail="provider probe credential is unavailable")
        connection = matches[0]
        selected = connection.selected_credential
        expected_kind = (
            "bearer"
            if connection.requirement.credential_type == "httpBearerAuth"
            else "oauth2"
            if connection.requirement.credential_type == "oAuth2Api"
            else None
        )
        if (
            connection.status is not IntegrationSetupStatus.READY
            or selected is None
            or selected.credential_id != request.credential_id
            or expected_kind != request.integration_kind
            or connection.requirement.verification_workflow_sha256
            != request.verification_template_sha256
        ):
            raise HTTPException(status_code=409, detail="provider probe credential is unavailable")

    @staticmethod
    def _assert_provider_probe_completion(
        started: PortalProviderProbeStartedV1,
        completion: PortalProviderProbeCompletionV1,
    ) -> None:
        request = started.request
        if (
            completion.probe_request_id != request.probe_request_id
            or completion.run_id != request.run_id
            or completion.job_id != request.job_id
            or completion.correlation_id != request.correlation_id
            or completion.integration_kind != request.integration_kind
            or completion.credential_alias != request.credential_alias
            or completion.credential_id != request.credential_id
            or completion.setup_revision != request.setup_revision
            or completion.setup_content_sha256 != request.setup_content_sha256
            or completion.template_ref.sha256 != request.verification_template_sha256
            or completion.occurred_at <= started.occurred_at
        ):
            raise HTTPException(status_code=409, detail="provider probe completion fence mismatch")

    def _consume_portal_ticket(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        request: PortalSetupTicketUseV1 | PortalSetupActionRequestV1,
        action: PortalTicketAction,
        now: datetime,
    ) -> tuple[PersistedIntegrationSetupV1, int]:
        persisted = self.integration_setup(job_id)
        target_index = self._portal_target_index(persisted, request.credential_alias)
        try:
            self._portal_tickets.consume(
                job_id=job_id,
                principal=principal,
                ticket_id=request.ticket_id,
                raw_ticket=request.ticket,
                credential_alias=request.credential_alias,
                action=action,
                now=now,
            )
        except PermissionError:
            raise HTTPException(status_code=403, detail="invalid portal setup ticket") from None
        return persisted, target_index

    def _portal_target(
        self,
        job_id: UUID,
        organization_id: str,
        credential_alias: str,
    ) -> tuple[PersistedIntegrationSetupV1, int]:
        persisted = self.portal_integration_setup(job_id, organization_id)
        return persisted, self._portal_target_index(persisted, credential_alias)

    @staticmethod
    def _portal_target_index(
        persisted: PersistedIntegrationSetupV1,
        credential_alias: str,
    ) -> int:
        matches = tuple(
            index
            for index, connection in enumerate(persisted.submission.plan.connections)
            if connection.requirement.credential_alias == credential_alias
        )
        if len(matches) != 1:
            raise HTTPException(status_code=404, detail="integration setup not found")
        return matches[0]

    @staticmethod
    def _portal_ticket_fence(
        persisted: PersistedIntegrationSetupV1,
        target_index: int,
    ) -> PortalTicketFenceV1:
        current = persisted.submission
        target = current.plan.connections[target_index]
        receipt = target.verification_receipt
        return PortalTicketFenceV1(
            revision=current.revision,
            content_sha256=persisted.content_sha256,
            correlation_id=current.correlation_id,
            credential_alias=target.requirement.credential_alias,
            credential_type=target.requirement.credential_type,
            requirement_project_id=target.requirement.project_id,
            selected_credential_id=(
                None if target.selected_credential is None else target.selected_credential.credential_id
            ),
            expected_verification_workflow_sha256=(
                target.requirement.verification_workflow_sha256
            ),
            verification_workflow_sha256=(
                None if receipt is None else receipt.workflow_content_sha256
            ),
        )

    @staticmethod
    def _resolve_portal_connection(
        requirement: IntegrationCredentialRequirementV1,
        *,
        credentials: tuple[N8nCredentialMetadataV1, ...],
        selected_credential_id: str | None,
        verification_receipt: CredentialVerificationReceiptV1 | None,
        now: datetime,
    ) -> IntegrationConnectionV1:
        integration = RequestedIntegration(
            integration_key=requirement.integration_key,
            purpose="portal credential setup",
            trigger="portal request",
            operation="credential metadata selection",
            required=requirement.required,
            credential_aliases=(requirement.credential_alias,),
            success_behavior="credential metadata is selected",
            failure_behavior="integration remains not ready",
        )
        receipts = () if verification_receipt is None else (verification_receipt,)
        plan = IntegrationSetupPlanner().plan(
            integrations=(integration,),
            requirements=(requirement,),
            credentials=credentials,
            selected_credential_ids=(
                None
                if selected_credential_id is None
                else {requirement.credential_alias: selected_credential_id}
            ),
            verification_receipts=receipts,
            now=now,
        )
        return plan.connections[0]

    def _record_portal_observation(
        self,
        persisted: PersistedIntegrationSetupV1,
        target_index: int,
        replacement: IntegrationConnectionV1,
        now: datetime,
    ) -> PersistedIntegrationSetupV1:
        current = persisted.submission
        connections = tuple(
            replacement if index == target_index else connection
            for index, connection in enumerate(current.plan.connections)
        )
        submission = IntegrationSetupSubmissionV1(
            event_id=uuid4(),
            job_id=current.job_id,
            correlation_id=current.correlation_id,
            subject_version=current.subject_version,
            revision=current.revision + 1,
            previous_content_sha256=persisted.content_sha256,
            occurred_at=_after(now, current.occurred_at),
            change_kind="observed",
            plan=current.plan.model_copy(update={"connections": connections}),
        )
        self._record_integration_setup_locked(submission)
        return self.integration_setup(current.job_id)

    def append_delivery_event(
        self,
        event: DeliveryEventEnvelope,
        *,
        require_current_claim: bool = False,
    ) -> AppendResult:
        try:
            stored = self._retry_write(
                lambda: self._append_delivery_event_once(
                    event,
                    require_current_claim=require_current_claim,
                )
            )
            return AppendResult(event=stored, replayed=False)
        except _DeliveryEventReplay as replay:
            return AppendResult(event=replay.event, replayed=True)

    def _append_delivery_event_once(
        self,
        event: DeliveryEventEnvelope,
        *,
        require_current_claim: bool,
    ) -> DeliveryEventEnvelope:
        canonical = event.model_dump(mode="json")
        trace = event.trace
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                batch_id = trace.batch_id
                if require_current_claim:
                    if batch_id is None:
                        raise HTTPException(
                            status_code=409,
                            detail="delivery event must match the current claim",
                        )
                    _, _, projection = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=_utcnow(),
                    )
                    if (
                        projection.status != "claimed"
                        or trace.claim_id != projection.claim_id
                        or trace.fencing_token != projection.fencing_token
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="delivery event must match the current claim",
                        )
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'delivery_event'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.event_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (str(event.event_id),),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing_data = existing_row["data"]
                    if isinstance(existing_data, str):
                        existing_data = json.loads(existing_data)
                    existing = DeliveryEventEnvelope.model_validate(existing_data)
                    if existing == event:
                        raise _DeliveryEventReplay(existing)
                    raise HTTPException(
                        status_code=409,
                        detail="event_id already exists with different content",
                    )

                if batch_id is not None:
                    cursor.execute(
                        """
                        SELECT data FROM blocks
                        WHERE block_type = 'delivery_event'
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.project_id')) = %s
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.run_id')) = %s
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.batch_id')) = %s
                        ORDER BY `index` FOR UPDATE
                        """,
                        (trace.project_id, trace.run_id, batch_id),
                    )
                    prior_tokens: list[int] = []
                    for row in cursor.fetchall():
                        data = row["data"]
                        if isinstance(data, str):
                            data = json.loads(data)
                        token = data.get("trace", {}).get("fencing_token")
                        if isinstance(token, int) and not isinstance(token, bool):
                            prior_tokens.append(token)
                    if prior_tokens and (
                        trace.fencing_token is None
                        or trace.fencing_token < max(prior_tokens)
                    ):
                        raise HTTPException(status_code=409, detail="stale fencing token")

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="delivery_event",
                    data=canonical,
                    status="recorded",
                    parent_index=None,
                    metadata={"schema": "captain-delivery-event/v1"},
                )
                self._insert(cursor, block)
        return event

    def delivery_events(
        self,
        *,
        project_id: str,
        run_id: str,
    ) -> tuple[DeliveryEventEnvelope, ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'delivery_event'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.project_id')) = %s
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.run_id')) = %s
                    ORDER BY `index`
                    """,
                    (project_id, run_id),
                )
                rows = cursor.fetchall()
        return tuple(
            DeliveryEventEnvelope.model_validate(
                json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            )
            for row in rows
        )

    def accept_runtime_command(
        self,
        command: AgentRuntimeCommand,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._accept_runtime_command_once(command))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=command.event_id, replayed=False)

    def _accept_runtime_command_once(self, command: AgentRuntimeCommand) -> None:
        canonical = command.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                existing = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(command.event_id),
                    for_update=True,
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(command.event_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command event_id already has different content",
                    )

                cursor.execute(
                    """
                    SELECT MAX(
                        CAST(JSON_UNQUOTE(JSON_EXTRACT(data, '$.subject_version')) AS UNSIGNED)
                    ) AS max_version
                    FROM blocks
                    WHERE block_type = 'agent_runtime_command'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.subject_id')) = %s
                    FOR UPDATE
                    """,
                    (command.subject_id,),
                )
                row = cursor.fetchone()
                max_version = row["max_version"] if row is not None else None
                if max_version is not None and command.subject_version < int(max_version):
                    raise HTTPException(status_code=409, detail="stale runtime subject version")

                payload = command.payload
                if payload.batch_id is not None:
                    parent = self._batch_row(cursor, payload.batch_id, for_update=True)
                    if parent is None:
                        raise HTTPException(status_code=409, detail="released batch not found")
                    batch = WorkBatch.model_validate(parent["data"])
                    if payload.subtask_id not in batch.subtask_ids:
                        raise HTTPException(
                            status_code=409,
                            detail="runtime subtask was not released in the batch",
                        )
                    if payload.capability_profile.value not in batch.capability_tags:
                        raise HTTPException(
                            status_code=409,
                            detail="runtime capability profile was not released",
                        )

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_command",
                    data=canonical,
                    status="accepted",
                    parent_index=None,
                    metadata={"schema": "captain.agent-runtime-command.v1"},
                )
                self._insert(cursor, block)

    def record_capability_grant(
        self,
        grant: CapabilityGrant,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._record_capability_grant_once(grant))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=grant.command_id, replayed=False)

    def _record_capability_grant_once(self, grant: CapabilityGrant) -> None:
        canonical = grant.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(grant.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="runtime command not found")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                try:
                    validate_grant(grant, command, grant.issued_at)
                except CapabilityDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

                existing = self._runtime_grant_block(cursor, grant, for_update=True)
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(grant.command_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command or grant already has different grant content",
                    )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_grant",
                    data=canonical,
                    status="active",
                    parent_index=command_block["index"],
                    metadata={"schema": "captain.capability-grant.v1"},
                )
                self._insert(cursor, block)

    def record_runtime_result(
        self,
        result: AgentRuntimeResult,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._record_runtime_result_once(result))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=result.command_id, replayed=False)

    def record_runtime_provider_settlement(
        self,
        settlement: RuntimeProviderCostSettlementV1,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(
                lambda: self._record_runtime_provider_settlement_once(settlement)
            )
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(
            operation_id=settlement.receipt.request_id,
            replayed=False,
        )

    def _record_runtime_provider_settlement_once(
        self,
        settlement: RuntimeProviderCostSettlementV1,
    ) -> None:
        canonical = settlement.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(settlement.receipt.request_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime provider settlement command not found",
                    )
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                self._validate_runtime_provider_settlement(settlement, command)
                for field, value in (
                    ("settlement_id", str(settlement.settlement_id)),
                    ("provider_call_id", settlement.provider_call_id),
                    ("receipt.request_id", str(settlement.receipt.request_id)),
                ):
                    existing = self._runtime_block_by_json_value(
                        cursor,
                        block_type="runtime_provider_cost_settlement",
                        field=field,
                        value=value,
                        for_update=True,
                    )
                    if existing is not None:
                        if existing["data"] == canonical:
                            raise _RuntimeReplay(settlement.receipt.request_id)
                        raise HTTPException(
                            status_code=409,
                            detail="runtime provider settlement already changed",
                        )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="runtime_provider_cost_settlement",
                    data=canonical,
                    status="settled",
                    parent_index=command_block["index"],
                    metadata={"schema": settlement.schema_name},
                )
                self._insert(cursor, block)

    def _validate_runtime_provider_settlement(
        self,
        settlement: RuntimeProviderCostSettlementV1,
        command: AgentRuntimeCommand,
    ) -> None:
        receipt = settlement.receipt
        payload = command.payload
        snapshot = next(
            (
                item
                for item in self._runtime_usage_pricing_snapshots
                if item.snapshot_id == receipt.pricing_snapshot_id
                and item.snapshot_sha256 == receipt.pricing_snapshot_sha256
                and item.provider == receipt.provider
                and item.model == receipt.model
            ),
            None,
        )
        if (
            command.payload.operation is not RuntimeOperation.CODEX_RESUME
            or command.payload.cost_authority_ref is None
            or settlement.settlement_id
            != uuid5(command.event_id, "provider-cost-settlement")
            or receipt.request_id != command.event_id
            or receipt.command_id != (command.causation_id or command.event_id)
            or receipt.reservation_id != payload.budget_reservation_id
            or receipt.job_id != payload.cost_job_id
            or receipt.run_id != payload.cost_run_id
            or receipt.input_id != payload.cost_input_id
            or receipt.correlation_id != command.correlation_id
            or receipt.capability_id != payload.cost_capability_id
            or receipt.capability_version != payload.cost_capability_version
            or snapshot is None
            or not (
                snapshot.effective_at
                <= receipt.started_at
                <= receipt.ended_at
                < snapshot.expires_at
            )
            or receipt.actual_cost_usd
            != snapshot.cost(
                input_units=receipt.input_units,
                cached_input_units=receipt.cached_input_units,
                output_units=receipt.output_units,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement binding is invalid",
            )

    def record_capability_grant_revocation(
        self,
        revocation: CapabilityGrantRevocation,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(
                lambda: self._record_capability_grant_revocation_once(revocation)
            )
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=revocation.command_id, replayed=False)

    def _record_capability_grant_revocation_once(
        self, revocation: CapabilityGrantRevocation
    ) -> None:
        canonical = revocation.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                grant_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant",
                    field="grant_id",
                    value=revocation.grant_id,
                    for_update=True,
                )
                if grant_block is None:
                    raise HTTPException(status_code=409, detail="runtime grant not found")
                grant = CapabilityGrant.model_validate(grant_block["data"])
                if grant.command_id != revocation.command_id:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime revocation belongs to a different command",
                    )
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(revocation.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="runtime command not found")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                try:
                    validate_grant(grant, command, revocation.revoked_at)
                except CapabilityDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_result",
                    field="command_id",
                    value=str(revocation.command_id),
                    for_update=True,
                ) is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="completed runtime grant cannot be revoked",
                    )
                existing = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant_revocation",
                    field="grant_id",
                    value=revocation.grant_id,
                    for_update=True,
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(revocation.command_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime grant already has a different revocation",
                    )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_grant_revocation",
                    data=canonical,
                    status="revoked",
                    parent_index=grant_block["index"],
                    metadata={"schema": "captain.capability-grant-revocation.v1"},
                )
                self._insert(cursor, block)

    def _record_runtime_result_once(self, result: AgentRuntimeResult) -> None:
        canonical = result.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(result.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="runtime command not found")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                self._assert_result_matches_command(result, command)
                self._require_provider_settlement_for_result(
                    cursor,
                    command,
                    result,
                )

                grant_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant",
                    field="grant_id",
                    value=result.grant_id,
                    for_update=True,
                )
                if grant_block is None:
                    raise HTTPException(status_code=409, detail="runtime grant not found")
                grant = CapabilityGrant.model_validate(grant_block["data"])
                if grant.command_id != command.event_id:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime grant belongs to a different command",
                    )
                revocation_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant_revocation",
                    field="grant_id",
                    value=grant.grant_id,
                    for_update=True,
                )
                if revocation_block is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="revoked runtime grant cannot record a result",
                    )

                existing = self._runtime_result_block(cursor, result, for_update=True)
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(result.command_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command or result event already has different content",
                    )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_result",
                    data=canonical,
                    status=result.status.value,
                    parent_index=command_block["index"],
                    metadata={"schema": "captain.agent-runtime-result.v1"},
                )
                self._insert(cursor, block)

    def _require_provider_settlement_for_result(
        self,
        cursor: Any,
        command: AgentRuntimeCommand,
        result: AgentRuntimeResult,
    ) -> RuntimeProviderCostSettlementV1 | None:
        if (
            command.payload.operation is not RuntimeOperation.CODEX_RESUME
            or command.payload.cost_authority_ref is None
        ):
            return None
        block = self._runtime_block_by_json_value(
            cursor,
            block_type="runtime_provider_cost_settlement",
            field="receipt.request_id",
            value=str(command.event_id),
            for_update=True,
        )
        if block is None:
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement is unavailable",
            )
        try:
            settlement = RuntimeProviderCostSettlementV1.model_validate(block["data"])
        except (ValidationError, ValueError):
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement is invalid",
            ) from None
        receipt = settlement.receipt
        evidence = result.cost_evidence
        if (
            receipt.request_id != command.event_id
            or receipt.command_id != (command.causation_id or command.event_id)
            or receipt.result_id != result.event_id
            or evidence is None
            or evidence.receipt_id != receipt.receipt_id
            or evidence.command_id != command.event_id
            or evidence.result_id != result.event_id
            or evidence.original_command_id != receipt.command_id
            or evidence.reservation_id != receipt.reservation_id
            or evidence.job_id != receipt.job_id
            or evidence.run_id != receipt.run_id
            or evidence.input_id != receipt.input_id
            or evidence.correlation_id != receipt.correlation_id
            or evidence.capability_id != receipt.capability_id
            or evidence.capability_version != receipt.capability_version
            or evidence.provider != receipt.provider
            or evidence.model != receipt.model
            or evidence.input_units != receipt.input_units
            or evidence.cached_input_units != receipt.cached_input_units
            or evidence.output_units != receipt.output_units
            or evidence.actual_cost_usd != receipt.actual_cost_usd
            or evidence.pricing_snapshot_id != receipt.pricing_snapshot_id
            or evidence.pricing_snapshot_sha256 != receipt.pricing_snapshot_sha256
            or evidence.started_at > receipt.started_at
            or evidence.ended_at < receipt.ended_at
        ):
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement does not match result",
            )
        return settlement

    def runtime_provider_settlement(
        self,
        command_id: UUID,
        result_id: UUID,
    ) -> RuntimeProviderCostSettlementV1 | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="runtime_provider_cost_settlement",
                    field="receipt.request_id",
                    value=str(command_id),
                )
        if block is None:
            return None
        settlement = RuntimeProviderCostSettlementV1.model_validate(block["data"])
        if settlement.receipt.result_id != result_id:
            return None
        return settlement

    def runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(operation_id),
                )
                if command_block is None:
                    raise HTTPException(status_code=404, detail="runtime operation not found")
                grant_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant",
                    field="command_id",
                    value=str(operation_id),
                )
                result_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_result",
                    field="command_id",
                    value=str(operation_id),
                )
                revocation_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant_revocation",
                    field="command_id",
                    value=str(operation_id),
                )
        return RuntimeOperationProjection(
            operation_id=operation_id,
            command=AgentRuntimeCommand.model_validate(command_block["data"]),
            grant=(
                CapabilityGrant.model_validate(grant_block["data"])
                if grant_block is not None
                else None
            ),
            revocation=(
                CapabilityGrantRevocation.model_validate(revocation_block["data"])
                if revocation_block is not None
                else None
            ),
            result=(
                AgentRuntimeResult.model_validate(result_block["data"])
                if result_block is not None
                else None
            ),
        )

    def validate_capability_resume_cost_authority(
        self,
        command: AgentRuntimeCommand,
        *,
        now: datetime,
    ) -> RuntimeResumeCostAuthorityV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                return self._validate_capability_resume_cost_authority_with_cursor(
                    cursor, command, now=now
                )

    def validate_provider_proxy_authority(
        self,
        context: ProviderBudgetContextV1,
        *,
        now: datetime,
    ) -> ProviderBudgetContextV1:
        """Authenticated exact readback for the proxy immediately before effect."""
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(context.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="provider authority is unavailable")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                authority = self._validate_capability_resume_cost_authority_with_cursor(
                    cursor, command, now=now
                )
                payload = command.payload
                if (
                    not authority.hard_ceiling_enforced
                    or context.sha256 != payload.provider_context_sha256
                    or context.cost_authority_ref != authority.cost_authority_ref
                    or context.reservation_id != authority.reservation_id
                    or context.job_id != authority.job_id
                    or context.run_id != authority.run_id
                    or context.input_id != authority.input_id
                    or context.correlation_id != authority.correlation_id
                    or context.capability_id != authority.capability_id
                    or context.capability_version != authority.capability_version
                    or context.command_id != authority.command_id
                    or context.result_id != authority.provider_result_id
                    or context.session_id != authority.provider_session_id
                    or context.policy_sha256 != authority.provider_policy_sha256
                    or context.price_card_sha256 != authority.provider_price_card_sha256
                    or context.ceiling_usd != authority.ceiling_usd
                    or context.expires_at != authority.expires_at
                    or context.original_command_id != (command.causation_id or command.event_id)
                ):
                    raise HTTPException(status_code=409, detail="provider authority is unavailable")
                return context

    def resolve_provider_proxy_authority(
        self,
        reference: str,
        *,
        now: datetime,
    ) -> ProviderBudgetContextV1:
        """Resolve one opaque context reference from authoritative command state."""

        prefix = "gateway://provider-budget-contexts/"
        digest = reference.removeprefix(prefix) if reference.startswith(prefix) else ""
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise HTTPException(status_code=409, detail="provider authority is unavailable")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="payload.provider_context_sha256",
                    value=digest,
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="provider authority is unavailable")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                authority = self._validate_capability_resume_cost_authority_with_cursor(
                    cursor, command, now=now
                )
                if (
                    not authority.hard_ceiling_enforced
                    or authority.provider_context_sha256 != digest
                    or authority.provider_session_id is None
                    or authority.provider_result_id is None
                    or authority.provider_policy_sha256 is None
                    or authority.provider_price_card_sha256 is None
                ):
                    raise HTTPException(status_code=409, detail="provider authority is unavailable")
                context = ProviderBudgetContextV1(
                    schema="captain.provider-budget-context.v1",
                    command_id=authority.command_id,
                    original_command_id=command.causation_id or command.event_id,
                    result_id=authority.provider_result_id,
                    reservation_id=authority.reservation_id,
                    job_id=authority.job_id,
                    run_id=authority.run_id,
                    input_id=authority.input_id,
                    correlation_id=authority.correlation_id,
                    capability_id=authority.capability_id,
                    capability_version=authority.capability_version,
                    session_id=authority.provider_session_id,
                    cost_authority_ref=authority.cost_authority_ref,
                    ceiling_usd=authority.ceiling_usd,
                    expires_at=authority.expires_at,
                    policy_sha256=authority.provider_policy_sha256,
                    price_card_sha256=authority.provider_price_card_sha256,
                )
                if context.sha256 != digest:
                    raise HTTPException(status_code=409, detail="provider authority is unavailable")
                return context

    def _validate_capability_resume_cost_authority_with_cursor(
        self,
        cursor: Any,
        command: AgentRuntimeCommand,
        *,
        now: datetime,
    ) -> RuntimeResumeCostAuthorityV1:
        payload = command.payload
        if (
            payload.operation is not RuntimeOperation.CODEX_RESUME
            or payload.cost_authority_ref is None
            or payload.budget_reservation_id is None
            or payload.maximum_cost_usd is None
            or payload.cost_job_id is None
            or payload.cost_run_id is None
            or payload.cost_input_id is None
            or payload.cost_capability_id is None
            or payload.cost_capability_version is None
        ):
            raise HTTPException(
                status_code=409, detail="runtime resume cost binding is incomplete"
            )
        try:
            receipt_id = UUID(payload.cost_authority_ref.rsplit("/", 1)[-1])
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=409, detail="runtime resume cost authority is invalid"
            ) from None
        cursor.execute(
            """SELECT payload FROM capability_resume_authorizations
               WHERE authorization_receipt_id = %s FOR UPDATE""",
            (str(receipt_id),),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=409, detail="runtime resume cost authority is unavailable"
            )
        authorization = self._decode_json(row["payload"])
        expected_command = AgentRuntimeCommand.model_validate(
            authorization["resume_command"]
        )
        request = authorization["request"]
        if (
            expected_command != command
            or request["reservation_id"] != str(payload.budget_reservation_id)
            or request["job_id"] != str(payload.cost_job_id)
            or request["run_id"] != str(payload.cost_run_id)
            or request["input_id"] != payload.cost_input_id
            or request["correlation_id"] != str(command.correlation_id)
            or request["capability_id"] != payload.cost_capability_id
            or request["capability_version"] != payload.cost_capability_version
        ):
            raise HTTPException(
                status_code=409, detail="runtime resume cost authority binding mismatch"
            )
        command_block = self._runtime_block_by_json_value(
            cursor,
            block_type="agent_runtime_command",
            field="event_id",
            value=str(command.event_id),
            for_update=True,
        )
        reservation = self._factory_reservation(
            cursor, payload.budget_reservation_id, for_update=True
        )
        if (
            command_block is None
            or AgentRuntimeCommand.model_validate(command_block["data"]) != command
            or reservation is None
            or reservation.job_id != payload.cost_job_id
            or reservation.correlation_id != command.correlation_id
            or reservation.requested_usd != payload.maximum_cost_usd
            or reservation.expires_at <= now
            or self._reservation_is_closed(cursor, reservation.reservation_id)
        ):
            raise HTTPException(
                status_code=409, detail="runtime resume cost reservation is inactive"
            )
        provider_binding = (
            (
                payload.provider_proxy_url.removesuffix("/v1")
                if payload.provider_proxy_url is not None
                else None
            ),
            payload.provider_policy_sha256,
            payload.provider_price_card_sha256,
            payload.provider_context_sha256,
            payload.provider_proxy_executable_sha256,
            payload.provider_proxy_code_sha256,
            payload.provider_paid_command_sha256,
            payload.provider_hard_ceiling_enforced,
        )
        provider_fields = (*provider_binding, payload.provider_session_id, payload.provider_result_id)
        hard_ceiling_enforced = (
            all(value is not None for value in provider_fields)
            and provider_binding
            in getattr(self, "_runtime_provider_bundle_bindings", frozenset())
            and payload.provider_hard_ceiling_enforced is True
            and payload.provider_result_id
            == uuid5(command.event_id, "captain.runtime-result")
        )
        return RuntimeResumeCostAuthorityV1(
            schema_name="captain.runtime-resume-cost-authority.v1",
            authorization_receipt_id=receipt_id,
            cost_authority_ref=payload.cost_authority_ref,
            reservation_id=reservation.reservation_id,
            job_id=payload.cost_job_id,
            run_id=payload.cost_run_id,
            input_id=payload.cost_input_id,
            correlation_id=command.correlation_id,
            capability_id=payload.cost_capability_id,
            capability_version=payload.cost_capability_version,
            command_id=command.event_id,
            ceiling_usd=reservation.requested_usd,
            expires_at=reservation.expires_at,
            hard_ceiling_enforced=hard_ceiling_enforced,
            metering_mode="provider_usage_receipt",
            provider_proxy_url=(payload.provider_proxy_url if hard_ceiling_enforced else None),
            provider_policy_sha256=(payload.provider_policy_sha256 if hard_ceiling_enforced else None),
            provider_price_card_sha256=(payload.provider_price_card_sha256 if hard_ceiling_enforced else None),
            provider_context_sha256=(payload.provider_context_sha256 if hard_ceiling_enforced else None),
            provider_proxy_executable_sha256=(payload.provider_proxy_executable_sha256 if hard_ceiling_enforced else None),
            provider_proxy_code_sha256=(payload.provider_proxy_code_sha256 if hard_ceiling_enforced else None),
            provider_paid_command_sha256=(payload.provider_paid_command_sha256 if hard_ceiling_enforced else None),
            provider_session_id=(payload.provider_session_id if hard_ceiling_enforced else None),
            provider_result_id=(payload.provider_result_id if hard_ceiling_enforced else None),
        )

    def settle_capability_resume_cost(
        self,
        request: RuntimeResumeCostSettlementRequestV1,
        *,
        now: datetime,
    ) -> RuntimeResumeCostSettlementV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                request = self._verified_capability_resume_cost_request_with_cursor(
                    cursor,
                    request,
                )
                settlement = self._derive_capability_resume_cost_settlement(request)
                existing = self._factory_budget_event(
                    cursor, settlement.settlement_id, for_update=True
                )
                canonical = settlement.model_dump(mode="json", by_alias=True)
                if existing is not None:
                    if self._decode_json(existing["payload"]) != canonical:
                        raise HTTPException(
                            status_code=409,
                            detail="runtime resume cost settlement changed",
                        )
                    return settlement
                current = self._validate_capability_resume_cost_authority_with_cursor(
                    cursor, request.command, now=now
                )
                if current != request.authority:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime resume cost authority changed",
                    )
                job, job_block = self._factory_budget_job(
                    cursor, request.authority.job_id, for_update=True
                )
                projection = self._factory_budget_projection(
                    cursor, job, for_update=True
                )
                if request.authority.reservation_id not in projection.active_reservation_ids:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime resume cost reservation is inactive",
                    )
                self._insert_factory_budget_event(
                    cursor,
                    event_id=settlement.settlement_id,
                    reservation_id=settlement.reservation_id,
                    job_id=request.authority.job_id,
                    event_kind="resume_cost",
                    digest=self._canonical_model_sha256(settlement),
                    payload=canonical,
                    parent_index=job_block["index"],
                    schema_name=settlement.schema_name,
                )
        return settlement

    def _verified_capability_resume_cost_request(
        self,
        request: RuntimeResumeCostSettlementRequestV1,
    ) -> RuntimeResumeCostSettlementRequestV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                return self._verified_capability_resume_cost_request_with_cursor(
                    cursor,
                    request,
                )

    def _verified_capability_resume_cost_request_with_cursor(
        self,
        cursor: Any,
        request: RuntimeResumeCostSettlementRequestV1,
    ) -> RuntimeResumeCostSettlementRequestV1:
        settlement = self._require_provider_settlement_for_result(
            cursor,
            request.command,
            request.result,
        )
        if settlement is None:
            raise HTTPException(
                status_code=409,
                detail="runtime provider settlement is unavailable",
            )
        return request

    @staticmethod
    def _derive_capability_resume_cost_settlement(
        request: RuntimeResumeCostSettlementRequestV1,
    ) -> RuntimeResumeCostSettlementV1:
        command = request.command
        result = request.result
        authority = request.authority
        payload = command.payload
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.operation is not RuntimeOperation.CODEX_RESUME
            or authority.command_id != command.event_id
            or authority.reservation_id != payload.budget_reservation_id
            or authority.job_id != payload.cost_job_id
            or authority.run_id != payload.cost_run_id
            or authority.input_id != payload.cost_input_id
            or authority.correlation_id != command.correlation_id
            or authority.capability_id != payload.cost_capability_id
            or authority.capability_version != payload.cost_capability_version
            or authority.ceiling_usd != payload.maximum_cost_usd
        ):
            raise HTTPException(
                status_code=409, detail="runtime resume settlement binding mismatch"
            )
        cost: RuntimeCostEvidenceV1 | None = result.cost_evidence
        references = tuple(
            dict.fromkeys(
                (
                    *result.evidence_refs,
                    *((cost.evidence_ref,) if cost is not None else ()),
                )
            )
        )
        if not references:
            raise HTTPException(
                status_code=409, detail="runtime resume settlement lacks evidence"
            )
        if cost is None:
            disposition = "unmetered"
            actual = None
            accounted = authority.ceiling_usd
        else:
            if (
                cost.command_id != command.event_id
                or cost.reservation_id != authority.reservation_id
                or cost.job_id != authority.job_id
                or cost.run_id != authority.run_id
                or cost.input_id != authority.input_id
                or cost.correlation_id != authority.correlation_id
                or cost.capability_id != authority.capability_id
                or cost.capability_version != authority.capability_version
            ):
                raise HTTPException(
                    status_code=409,
                    detail="runtime resume actual usage binding mismatch",
                )
            actual = cost.actual_cost_usd
            disposition = (
                "accounted" if actual <= authority.ceiling_usd else "overrun"
            )
            accounted = actual
        return RuntimeResumeCostSettlementV1(
            schema_name="captain.runtime-resume-cost-settlement.v1",
            settlement_id=uuid5(command.event_id, "resume-cost-settlement"),
            command_id=command.event_id,
            reservation_id=authority.reservation_id,
            disposition=disposition,
            actual_cost_usd=actual,
            accounted_cost_usd=accounted,
            evidence_refs=references,
        )

    @staticmethod
    def _verify_runtime_provider_usage(
        *,
        command: AgentRuntimeCommand,
        result_id: UUID,
        evidence: RuntimeCostEvidenceV1,
        receipt_bytes: bytes,
        pricing_snapshots: tuple[RuntimeUsagePricingSnapshotV1, ...],
    ) -> Decimal:
        digest = hashlib.sha256(receipt_bytes).hexdigest()
        if (
            evidence.evidence_ref.sha256 != digest
            or evidence.evidence_ref.uri != f"artifact://sha256/{digest}"
            or evidence.evidence_ref.media_type != "application/json"
        ):
            raise HTTPException(
                status_code=409, detail="runtime provider usage digest mismatch"
            )
        try:
            receipt = RuntimeProviderUsageReceiptV1.model_validate_json(
                receipt_bytes, strict=True
            )
        except (ValidationError, ValueError):
            raise HTTPException(
                status_code=409, detail="runtime provider usage receipt is invalid"
            ) from None
        snapshot = next(
            (
                item
                for item in pricing_snapshots
                if item.snapshot_id == receipt.pricing_snapshot_id
                and item.snapshot_sha256 == receipt.pricing_snapshot_sha256
                and item.model == receipt.model
                and item.provider == receipt.provider
            ),
            None,
        )
        original_command_id = command.causation_id or command.event_id
        payload = command.payload
        if (
            snapshot is None
            or receipt.request_id != command.event_id
            or receipt.command_id != original_command_id
            or receipt.result_id != result_id
            or receipt.receipt_id != evidence.receipt_id
            or receipt.reservation_id != payload.budget_reservation_id
            or receipt.job_id != payload.cost_job_id
            or receipt.run_id != payload.cost_run_id
            or receipt.input_id != payload.cost_input_id
            or receipt.correlation_id != command.correlation_id
            or receipt.capability_id != payload.cost_capability_id
            or receipt.capability_version != payload.cost_capability_version
            or not (
                snapshot.effective_at
                <= receipt.started_at
                <= receipt.ended_at
                < snapshot.expires_at
            )
        ):
            raise HTTPException(
                status_code=409, detail="runtime provider usage binding mismatch"
            )
        mirrored = (
            evidence.command_id == command.event_id
            and evidence.result_id == result_id
            and evidence.original_command_id == receipt.command_id
            and evidence.reservation_id == receipt.reservation_id
            and evidence.job_id == receipt.job_id
            and evidence.run_id == receipt.run_id
            and evidence.input_id == receipt.input_id
            and evidence.correlation_id == receipt.correlation_id
            and evidence.capability_id == receipt.capability_id
            and evidence.capability_version == receipt.capability_version
            and evidence.provider == receipt.provider
            and evidence.model == receipt.model
            and evidence.input_units == receipt.input_units
            and evidence.cached_input_units == receipt.cached_input_units
            and evidence.output_units == receipt.output_units
            and evidence.pricing_snapshot_id == receipt.pricing_snapshot_id
            and evidence.pricing_snapshot_sha256 == receipt.pricing_snapshot_sha256
            and evidence.started_at == receipt.started_at
            and evidence.ended_at == receipt.ended_at
        )
        actual = snapshot.cost(
            input_units=receipt.input_units,
            cached_input_units=receipt.cached_input_units,
            output_units=receipt.output_units,
        )
        if (
            not mirrored
            or receipt.actual_cost_usd != actual
            or evidence.actual_cost_usd != actual
        ):
            raise HTTPException(
                status_code=409, detail="runtime provider usage cost mismatch"
            )
        return actual

    def record_factory_job(self, job: FactoryJob) -> FactoryWriteReceipt:
        canonical = job.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                existing = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(job.job_id),
                    for_update=True,
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        return FactoryWriteReceipt(event_id=job.event_id, replayed=True)
                    raise HTTPException(status_code=409, detail="factory job already exists with different content")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_factory_job",
                    data=canonical,
                    status="accepted",
                    parent_index=None,
                    metadata={"schema": job.schema_name},
                )
                self._insert(cursor, block)
        return FactoryWriteReceipt(event_id=job.event_id, replayed=False)

    def reserve_factory_budget(
        self,
        reservation: FactoryBudgetReservationV1,
    ) -> FactoryBudgetReservationWriteReceipt:
        return self._retry_write(
            lambda: self._reserve_factory_budget_once(reservation)
        )

    def _reserve_factory_budget_once(
        self,
        reservation: FactoryBudgetReservationV1,
    ) -> FactoryBudgetReservationWriteReceipt:
        canonical = reservation.model_dump(mode="json", by_alias=True)
        digest = self._canonical_model_sha256(reservation)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                replay = self._factory_budget_event(cursor, reservation.reservation_id, for_update=True)
                if replay is not None:
                    if self._decode_json(replay["payload"]) != canonical:
                        raise HTTPException(status_code=409, detail="budget reservation already exists with different content")
                    return FactoryBudgetReservationWriteReceipt(
                        event_id=reservation.reservation_id,
                        job_id=reservation.job_id,
                        replayed=True,
                        reservation=reservation,
                    )
                job, job_block = self._factory_budget_job(cursor, reservation.job_id)
                self._assert_budget_reservation(job, reservation)
                self._assert_factory_effects_open(
                    self._factory_projection(cursor, job), effect="paid effects"
                )
                projection = self._factory_budget_projection(cursor, job, for_update=True)
                if reservation.requested_usd > projection.remaining_usd:
                    raise HTTPException(status_code=409, detail="factory USD budget is exhausted")
                self._insert_factory_budget_event(
                    cursor,
                    event_id=reservation.reservation_id,
                    reservation_id=reservation.reservation_id,
                    job_id=reservation.job_id,
                    event_kind="reservation",
                    digest=digest,
                    payload=canonical,
                    parent_index=job_block["index"],
                    schema_name=reservation.schema_name,
                )
        return FactoryBudgetReservationWriteReceipt(
            event_id=reservation.reservation_id,
            job_id=reservation.job_id,
            replayed=False,
            reservation=reservation,
        )

    def record_factory_usage(
        self,
        submission: FactoryUsageSubmissionV2,
    ) -> FactoryBudgetWriteReceipt:
        receipt = submission.receipt
        canonical = submission.model_dump(mode="json", by_alias=True)
        digest, schema_name = self._factory_budget_payload_identity(submission)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                replay = self._factory_budget_event(cursor, receipt.receipt_id, for_update=True)
                if replay is not None:
                    if self._decode_json(replay["payload"]) != canonical:
                        raise HTTPException(status_code=409, detail="factory usage receipt already exists with different content")
                    return FactoryBudgetWriteReceipt(event_id=receipt.receipt_id, job_id=receipt.job_id, replayed=True)
                job, job_block = self._factory_budget_job(cursor, receipt.job_id)
                reservation = self._factory_reservation(cursor, receipt.reservation_id, for_update=True)
                if reservation is None:
                    raise HTTPException(status_code=409, detail="factory budget reservation not found")
                self._assert_budget_usage(job, reservation, receipt)
                self._assert_budget_usage_lease(
                    cursor, job, submission
                )
                if self._reservation_is_closed(cursor, receipt.reservation_id):
                    raise HTTPException(status_code=409, detail="factory budget reservation is no longer active")
                projection = self._factory_budget_projection(cursor, job, for_update=True)
                if receipt.cost_usd > reservation.requested_usd or receipt.cost_usd > projection.remaining_usd + reservation.requested_usd:
                    raise HTTPException(status_code=409, detail="factory usage exceeds its reservation or job budget")
                self._insert_factory_budget_event(
                    cursor,
                    event_id=receipt.receipt_id,
                    reservation_id=receipt.reservation_id,
                    job_id=receipt.job_id,
                    event_kind="usage",
                    digest=digest,
                    payload=canonical,
                    parent_index=job_block["index"],
                    schema_name=schema_name,
                )
        return FactoryBudgetWriteReceipt(event_id=receipt.receipt_id, job_id=receipt.job_id, replayed=False)

    def release_factory_budget(
        self,
        request: FactoryBudgetReleaseRequest,
    ) -> FactoryBudgetWriteReceipt:
        canonical = request.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                replay = self._factory_budget_event(cursor, request.release_id, for_update=True)
                if replay is not None:
                    if self._decode_json(replay["payload"]) != canonical:
                        raise HTTPException(status_code=409, detail="factory budget release already exists with different content")
                    return FactoryBudgetWriteReceipt(event_id=request.release_id, job_id=request.job_id, replayed=True)
                job, job_block = self._factory_budget_job(cursor, request.job_id)
                reservation = self._factory_reservation(cursor, request.reservation_id, for_update=True)
                if reservation is None:
                    raise HTTPException(status_code=409, detail="factory budget reservation not found")
                if (
                    request.job_id != reservation.job_id
                    or request.correlation_id != reservation.correlation_id
                    or request.subject_version != reservation.subject_version
                    or request.attempt != reservation.attempt
                    or request.released_at < reservation.reserved_at
                ):
                    raise HTTPException(status_code=409, detail="factory budget release binding mismatch")
                if self._reservation_is_closed(cursor, request.reservation_id):
                    raise HTTPException(status_code=409, detail="factory budget reservation is no longer active")
                self._insert_factory_budget_event(
                    cursor,
                    event_id=request.release_id,
                    reservation_id=request.reservation_id,
                    job_id=request.job_id,
                    event_kind="release",
                    digest=hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    payload=canonical,
                    parent_index=job_block["index"],
                    schema_name=request.schema_name,
                )
        return FactoryBudgetWriteReceipt(event_id=request.release_id, job_id=request.job_id, replayed=False)

    def factory_budget(self, job_id: UUID) -> FactoryBudgetProjection:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job, _ = self._factory_budget_job(
                    cursor, job_id, missing_status=404, for_update=False
                )
                return self._factory_budget_projection(cursor, job)

    def factory_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        """Return every Gateway-accepted usage receipt in ledger order."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                self._factory_budget_job(
                    cursor,
                    job_id,
                    missing_status=404,
                    for_update=False,
                )
                return self._factory_usage_receipts_for_job(
                    cursor,
                    job_id,
                    for_update=False,
                )

    def record_factory_workflow_artifact(
        self,
        artifact: FactoryWorkflowArtifact,
    ) -> FactoryWorkflowArtifactWriteReceipt:
        canonical = artifact.model_dump(mode="json", by_alias=True)
        digest = self._canonical_model_sha256(artifact)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id", value=str(artifact.job_id), for_update=True
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                self._assert_released_skill(
                    cursor, artifact.invocation.released_skill
                )
                self._assert_workflow_skill_assignment(cursor, artifact)
                cursor.execute(
                    "SELECT payload, content_sha256 FROM factory_workflow_artifacts WHERE invocation_id = %s FOR UPDATE",
                    (str(artifact.invocation_id),),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if self._decode_json(existing["payload"]) != canonical:
                        raise HTTPException(status_code=409, detail="factory workflow invocation already exists with different content")
                    return FactoryWorkflowArtifactWriteReceipt(invocation_id=artifact.invocation_id, content_sha256=digest, replayed=True)
                projection = self._factory_projection(cursor, job)
                self._assert_factory_effects_open(
                    projection,
                    effect="workflow artifacts",
                )
                prior_artifacts = self._factory_workflow_artifacts_for_job(
                    cursor, artifact.job_id, for_update=True
                )
                self._assert_workflow_artifact(job, artifact, prior_artifacts)
                self._assert_workflow_sequence(
                    projection, artifact, prior_artifacts
                )
                lease_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_lease", field="lease_id", value=artifact.invocation.lease.lease_id, for_update=True
                )
                if lease_block is None or FactoryLease.model_validate(lease_block["data"]) != artifact.invocation.lease:
                    raise HTTPException(status_code=409, detail="missing matching factory workflow lease")
                try:
                    validate_factory_lease(
                        artifact.invocation.lease,
                        job=job,
                        role=artifact.invocation.lease.role,
                        attempt=artifact.attempt,
                        now=artifact.occurred_at,
                    )
                except FactoryLeaseDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_workflow_artifact",
                    data=canonical,
                    status="accepted",
                    parent_index=job_block["index"],
                    metadata={"schema": artifact.schema_name},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_workflow_artifacts
                       (invocation_id, job_id, correlation_id, subject_version, attempt,
                        schema_name, content_sha256, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(artifact.invocation_id), str(artifact.job_id), str(artifact.correlation_id),
                        artifact.subject_version, artifact.attempt, artifact.schema_name, digest,
                        index, json.dumps(canonical, sort_keys=True),
                    ),
                )
        return FactoryWorkflowArtifactWriteReceipt(invocation_id=artifact.invocation_id, content_sha256=digest, replayed=False)

    def factory_workflow_artifacts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                self._factory_budget_job(
                    cursor,
                    job_id,
                    missing_status=404,
                    require_v3=False,
                    for_update=False,
                )
                return self._factory_workflow_artifacts_for_job(cursor, job_id)

    def record_business_benchmark_summary(
        self,
        summary: BusinessBenchmarkSummaryV1,
    ) -> BusinessBenchmarkSummaryWriteReceipt:
        try:
            return self._retry_write(
                lambda: self._record_business_benchmark_summary_once(summary)
            )
        except IntegrityError as exc:
            error_code = exc.args[0] if exc.args else None
            if error_code != 1062:
                raise
        except OperationalError as exc:
            error_code = exc.args[0] if exc.args else None
            if error_code not in TRANSIENT_TRANSACTION_ERRORS:
                raise
        return self._resolve_business_benchmark_summary_conflict(summary)

    def _record_business_benchmark_summary_once(
        self,
        summary: BusinessBenchmarkSummaryV1,
    ) -> BusinessBenchmarkSummaryWriteReceipt:
        canonical = summary.model_dump(mode="json", by_alias=True)
        artifact_sha256 = summary.artifact_ref.sha256
        content_sha256 = hashlib.sha256(
            canonical_business_benchmark_model_bytes(summary)
        ).hexdigest()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(summary.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                if not isinstance(job, AgentFactoryJobV3):
                    raise HTTPException(
                        status_code=409,
                        detail="business benchmark summary requires a V3 Factory job",
                    )
                projection = self._factory_projection(cursor, job)
                if (
                    summary.correlation_id != job.correlation_id
                    or summary.subject_version != job.subject_version
                    or summary.attempt != projection.attempt
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="business benchmark summary does not match current Factory identity",
                    )
                if summary.suite_ref not in job.private_holdout_refs:
                    raise HTTPException(
                        status_code=409,
                        detail="business benchmark suite is not owned by the Factory job",
                    )
                executions = tuple(
                    artifact
                    for artifact in self._factory_workflow_artifacts_for_job(
                        cursor, job.job_id, for_update=True
                    )
                    if isinstance(artifact, TeamExecutionEvidenceV1)
                    and artifact.attempt == projection.attempt
                )
                if not executions or {
                    artifact.candidate_ref for artifact in executions
                } != {summary.candidate_ref}:
                    raise HTTPException(
                        status_code=409,
                        detail="business benchmark candidate is not the current workflow execution candidate",
                    )
                cursor.execute(
                    """SELECT summary_id, artifact_sha256, payload
                       FROM factory_business_benchmark_summaries
                       WHERE summary_id = %s OR artifact_sha256 = %s OR
                         (job_id = %s AND correlation_id = %s AND subject_version = %s
                          AND attempt = %s AND candidate_sha256 = %s)
                       FOR UPDATE""",
                    (
                        str(summary.summary_id),
                        artifact_sha256,
                        str(summary.job_id),
                        str(summary.correlation_id),
                        summary.subject_version,
                        summary.attempt,
                        summary.candidate_ref.sha256,
                    ),
                )
                existing = cursor.fetchall()
                if existing:
                    if len(existing) == 1 and self._decode_json(existing[0]["payload"]) == canonical:
                        return BusinessBenchmarkSummaryWriteReceipt(
                            summary_id=summary.summary_id,
                            artifact_sha256=artifact_sha256,
                            content_sha256=content_sha256,
                            replayed=True,
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="business benchmark summary identity already exists with different content",
                    )
                summary_index = self._next_index(cursor)
                summary_block = self._new_block(
                    cursor,
                    index=summary_index,
                    block_type="factory_business_benchmark_summary",
                    data=canonical,
                    status="validated",
                    parent_index=job_block["index"],
                    metadata={"schema": summary.schema_name},
                )
                self._insert(cursor, summary_block)
                cursor.execute(
                    """INSERT INTO factory_business_benchmark_summaries
                       (summary_id, job_id, correlation_id, subject_version, attempt,
                        candidate_sha256, artifact_sha256, content_sha256, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(summary.summary_id), str(summary.job_id), str(summary.correlation_id),
                        summary.subject_version, summary.attempt, summary.candidate_ref.sha256,
                        artifact_sha256, content_sha256, summary_index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
                event = self._business_benchmark_validated_event(
                    summary, content_sha256
                )
                event_index = self._next_index(cursor)
                event_block = self._new_block(
                    cursor,
                    index=event_index,
                    block_type="delivery_event",
                    data=event.model_dump(mode="json"),
                    status="recorded",
                    parent_index=summary_index,
                    metadata={"schema": "captain-delivery-event/v1"},
                )
                self._insert(cursor, event_block)
        return BusinessBenchmarkSummaryWriteReceipt(
            summary_id=summary.summary_id,
            artifact_sha256=artifact_sha256,
            content_sha256=content_sha256,
            replayed=False,
        )

    def _resolve_business_benchmark_summary_conflict(
        self,
        summary: BusinessBenchmarkSummaryV1,
    ) -> BusinessBenchmarkSummaryWriteReceipt:
        canonical = summary.model_dump(mode="json", by_alias=True)
        artifact_sha256 = summary.artifact_ref.sha256
        content_sha256 = hashlib.sha256(
            canonical_business_benchmark_model_bytes(summary)
        ).hexdigest()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT summary_id, job_id, correlation_id, subject_version,
                              attempt, candidate_sha256, artifact_sha256, payload
                       FROM factory_business_benchmark_summaries
                       WHERE summary_id = %s OR artifact_sha256 = %s OR
                         (job_id = %s AND correlation_id = %s AND subject_version = %s
                          AND attempt = %s AND candidate_sha256 = %s)
                       FOR UPDATE""",
                    (
                        str(summary.summary_id),
                        artifact_sha256,
                        str(summary.job_id),
                        str(summary.correlation_id),
                        summary.subject_version,
                        summary.attempt,
                        summary.candidate_ref.sha256,
                    ),
                )
                rows = cursor.fetchall()
        immutable_identity = (
            str(summary.summary_id),
            str(summary.job_id),
            str(summary.correlation_id),
            summary.subject_version,
            summary.attempt,
            summary.candidate_ref.sha256,
            artifact_sha256,
        )
        if len(rows) == 1:
            row = rows[0]
            stored_identity = (
                str(row["summary_id"]),
                str(row["job_id"]),
                str(row["correlation_id"]),
                row["subject_version"],
                row["attempt"],
                row["candidate_sha256"],
                row["artifact_sha256"],
            )
            if (
                stored_identity == immutable_identity
                and self._decode_json(row["payload"]) == canonical
            ):
                return BusinessBenchmarkSummaryWriteReceipt(
                    summary_id=summary.summary_id,
                    artifact_sha256=artifact_sha256,
                    content_sha256=content_sha256,
                    replayed=True,
                )
        raise HTTPException(
            status_code=409,
            detail="business benchmark summary identity already exists with different content",
        )

    def business_benchmark_summary(
        self, summary_id: UUID
    ) -> BusinessBenchmarkSummaryV1 | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM factory_business_benchmark_summaries WHERE summary_id = %s",
                    (str(summary_id),),
                )
                row = cursor.fetchone()
        return None if row is None else BusinessBenchmarkSummaryV1.model_validate(
            self._decode_json(row["payload"])
        )

    def business_benchmark_summary_by_artifact(
        self, artifact_ref: ArtifactRef
    ) -> BusinessBenchmarkSummaryV1 | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                return self._factory_business_benchmark_summary_by_artifact(
                    cursor, artifact_ref, for_update=False
                )

    def record_released_factory_skill(
        self,
        skill: ReleasedHermesSkill,
    ) -> FactorySkillWriteReceipt:
        canonical = skill.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM factory_released_skills WHERE skill_id = %s AND version = %s FOR UPDATE",
                    (skill.skill_id, skill.version),
                )
                row = cursor.fetchone()
                if row is not None:
                    if self._decode_json(row["payload"]) == canonical:
                        return FactorySkillWriteReceipt(
                            record_id=f"{skill.skill_id}:{skill.version}", replayed=True
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="released skill already exists with different content",
                    )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_released_skill",
                    data=canonical,
                    status="released",
                    parent_index=None,
                    metadata={"schema": "captain.released-hermes-skill.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_released_skills
                       (skill_id, version, content_sha256, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        skill.skill_id,
                        skill.version,
                        skill.content_sha256,
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return FactorySkillWriteReceipt(
            record_id=f"{skill.skill_id}:{skill.version}", replayed=False
        )

    def record_factory_skill_assignment(
        self,
        assignment: FactorySkillAssignmentV1,
    ) -> FactorySkillWriteReceipt:
        canonical = assignment.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(assignment.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                if (
                    not isinstance(job, AgentFactoryJobV3)
                    or assignment.released_skill.capability
                    != job.required_capability
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="factory skill assignment does not match its V3 job",
                    )
                self._assert_released_skill(cursor, assignment.released_skill)
                existing = self._factory_skill_assignment_for_step(
                    cursor,
                    assignment.job_id,
                    assignment.step,
                    for_update=True,
                )
                if existing is not None:
                    if existing == assignment:
                        return FactorySkillWriteReceipt(
                            record_id=(
                                f"{assignment.job_id}:{assignment.step.value}"
                            ),
                            replayed=True,
                        )
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "factory skill assignment already exists with different content"
                        ),
                    )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_skill_assignment",
                    data=canonical,
                    status="assigned",
                    parent_index=job_block["index"],
                    metadata={"schema": assignment.schema_name},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_skill_assignments
                       (job_id, step, skill_id, skill_version, content_sha256,
                        block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(assignment.job_id),
                        assignment.step.value,
                        assignment.released_skill.skill_id,
                        assignment.released_skill.version,
                        assignment.released_skill.content_sha256,
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return FactorySkillWriteReceipt(
            record_id=f"{assignment.job_id}:{assignment.step.value}",
            replayed=False,
        )

    def factory_skill_assignment(
        self,
        job_id: UUID,
        step: FactorySkillStep,
    ) -> FactorySkillAssignmentV1:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                assignment = self._factory_skill_assignment_for_step(
                    cursor,
                    job_id,
                    step,
                    for_update=False,
                )
                if assignment is None:
                    raise HTTPException(
                        status_code=404,
                        detail="factory skill assignment not found",
                    )
                return assignment

    def record_factory_skill_evaluation(
        self,
        submission: FactorySkillEvaluationSubmission,
    ) -> FactorySkillWriteReceipt:
        evidence = submission.evidence
        canonical = submission.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                existing = self._factory_skill_evaluation_row(
                    cursor, evidence.evidence_id, for_update=True
                )
                if existing is not None:
                    if self._decode_json(existing["payload"]) == canonical:
                        return FactorySkillWriteReceipt(
                            record_id=str(evidence.evidence_id), replayed=True
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="factory skill evaluation already exists with different content",
                    )
                cursor.execute(
                    """SELECT payload FROM factory_skill_evaluations
                       WHERE job_id = %s OR request_id = %s FOR UPDATE""",
                    (str(evidence.job_id), str(evidence.request_id)),
                )
                if cursor.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="factory job or request already has a different skill evaluation",
                    )
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(evidence.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                self._assert_factory_evaluation_job(evidence, job)
                lease_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_lease",
                    field="lease_id",
                    value=evidence.request.lease.lease_id,
                    for_update=True,
                )
                if lease_block is None:
                    raise HTTPException(
                        status_code=409,
                        detail="missing matching active factory lease for skill evaluation",
                    )
                lease = FactoryLease.model_validate(lease_block["data"])
                if lease != evidence.request.lease:
                    raise HTTPException(
                        status_code=409,
                        detail="skill evaluation lease differs from Captain ledger lease",
                    )
                try:
                    validate_factory_lease(
                        lease,
                        job=job,
                        role=FactoryRole.TOOL_INTEGRATOR,
                        attempt=lease.attempt,
                        now=evidence.occurred_at,
                    )
                except FactoryLeaseDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                self._assert_released_skill(cursor, evidence.request.released_skill)
                self._assert_evaluation_references(submission)

                evaluation_index = self._next_index(cursor)
                evaluation_block = self._new_block(
                    cursor,
                    index=evaluation_index,
                    block_type="factory_skill_evaluation",
                    data=canonical,
                    status=evidence.outcome,
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.factory-skill-evaluation-submission.v1"},
                )
                self._insert(cursor, evaluation_block)
                cursor.execute(
                    """INSERT INTO factory_skill_evaluations
                       (evidence_id, request_id, job_id, lease_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        str(evidence.evidence_id),
                        str(evidence.request_id),
                        str(evidence.job_id),
                        evidence.request.lease.lease_id,
                        evaluation_index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
                if evidence.candidate is not None:
                    candidate = evidence.candidate
                    candidate_index = self._next_index(cursor)
                    candidate_payload = candidate.model_dump(mode="json", by_alias=True)
                    candidate_block = self._new_block(
                        cursor,
                        index=candidate_index,
                        block_type="factory_skill_candidate",
                        data=candidate_payload,
                        status="private_candidate",
                        parent_index=evaluation_index,
                        metadata={"schema": "hermes.skill-candidate.v1"},
                    )
                    self._insert(cursor, candidate_block)
                    cursor.execute(
                        """INSERT INTO factory_skill_candidates
                           (candidate_id, evidence_id, block_index, payload)
                           VALUES (%s, %s, %s, %s)""",
                        (
                            candidate.candidate_id,
                            str(evidence.evidence_id),
                            candidate_index,
                            json.dumps(candidate_payload, sort_keys=True),
                        ),
                    )
                for marker in evidence.tool_gaps:
                    gap_index = self._next_index(cursor)
                    gap_payload = marker.model_dump(mode="json", by_alias=True)
                    gap_block = self._new_block(
                        cursor,
                        index=gap_index,
                        block_type="factory_skill_tool_gap",
                        data=gap_payload,
                        status=marker.status,
                        parent_index=evaluation_index,
                        metadata={"schema": "TODO_TOOL.v1"},
                    )
                    self._insert(cursor, gap_block)
                    cursor.execute(
                        """INSERT INTO factory_skill_tool_gaps
                           (evidence_id, gap_id, block_index, payload)
                           VALUES (%s, %s, %s, %s)""",
                        (
                            str(evidence.evidence_id),
                            marker.gap_id,
                            gap_index,
                            json.dumps(gap_payload, sort_keys=True),
                        ),
                    )
        return FactorySkillWriteReceipt(
            record_id=str(evidence.evidence_id), replayed=False
        )

    def factory_skill_evaluation(self, job_id: UUID) -> StoredSkillEvaluation | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM factory_skill_evaluations WHERE job_id = %s",
                    (str(job_id),),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                submission = FactorySkillEvaluationSubmission.model_validate(
                    self._decode_json(row["payload"])
                )
        return self._stored_factory_evaluation(submission)

    def record_factory_release_decision(
        self,
        submission: FactoryReleaseDecisionSubmission,
    ) -> FactorySkillWriteReceipt:
        canonical = submission.model_dump(mode="json", by_alias=True)
        decision_id = self._canonical_model_sha256(submission)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM factory_release_decisions WHERE decision_id = %s FOR UPDATE",
                    (decision_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if self._decode_json(existing["payload"]) == canonical:
                        return FactorySkillWriteReceipt(
                            record_id=decision_id,
                            replayed=True,
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="Factory release decision already exists with different content",
                    )
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(submission.decision.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                evaluation_row = self._factory_skill_evaluation_row_for_job(
                    cursor,
                    submission.decision.job_id,
                    for_update=True,
                )
                if evaluation_row is None:
                    raise HTTPException(
                        status_code=409,
                        detail="missing accepted Hermes skill evaluation evidence",
                    )
                evaluation_submission = FactorySkillEvaluationSubmission.model_validate(
                    self._decode_json(evaluation_row["payload"])
                )
                evaluation = self._stored_factory_evaluation(evaluation_submission)
                job = parse_factory_job(job_block["data"])
                self._assert_factory_release_decision_recordable(
                    self._factory_projection(cursor, job)
                )
                self._assert_factory_release_decision(
                    job,
                    evaluation,
                    submission.e2e_evidence,
                    submission.decision,
                )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_release_decision",
                    data=canonical,
                    status=submission.decision.status,
                    parent_index=evaluation_row["block_index"],
                    metadata={
                        "schema": "captain.factory-release-decision-submission.v1"
                    },
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_release_decisions
                       (decision_id, job_id, evaluation_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        decision_id,
                        str(job.job_id),
                        str(evaluation.evidence.evidence_id),
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return FactorySkillWriteReceipt(record_id=decision_id, replayed=False)

    def factory_release_decision(self, job_id: UUID) -> FactoryReleaseDecision | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                return self._factory_release_decision_for_job(cursor, job_id)

    def publish_factory_skill(
        self,
        publication: PublishedHermesSkill,
    ) -> FactorySkillWriteReceipt:
        canonical = publication.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM factory_published_skills WHERE skill_id = %s AND version = %s FOR UPDATE",
                    (publication.skill_id, publication.version),
                )
                row = cursor.fetchone()
                if row is not None:
                    if self._decode_json(row["payload"]) == canonical:
                        return FactorySkillWriteReceipt(
                            record_id=f"{publication.skill_id}:{publication.version}",
                            replayed=True,
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="published skill already exists with different content",
                    )
                evaluation_row = self._factory_skill_evaluation_row(
                    cursor, publication.evaluation_id, for_update=True
                )
                if evaluation_row is None:
                    raise HTTPException(status_code=409, detail="skill evaluation not found")
                submission = FactorySkillEvaluationSubmission.model_validate(
                    self._decode_json(evaluation_row["payload"])
                )
                evidence = submission.evidence
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(evidence.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                self._assert_publication_qualification(
                    publication,
                    submission,
                    parse_factory_job(job_block["data"]),
                )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="factory_published_skill",
                    data=canonical,
                    status="published",
                    parent_index=evaluation_row["block_index"],
                    metadata={"schema": "captain.published-hermes-skill.v1"},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO factory_published_skills
                       (skill_id, version, evaluation_id, candidate_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        publication.skill_id,
                        publication.version,
                        str(publication.evaluation_id),
                        publication.candidate_id,
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return FactorySkillWriteReceipt(
            record_id=f"{publication.skill_id}:{publication.version}", replayed=False
        )

    def record_factory_block(
        self,
        evidence: FactoryEvidenceBlock,
        *,
        runtime_retry_authorization: FactoryRuntimeRetryAuthorizationV1 | None = None,
    ) -> FactoryWriteReceipt:
        canonical = evidence.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(evidence.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                existing = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_block",
                    field="event_id",
                    value=str(evidence.event_id),
                    for_update=True,
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        return FactoryWriteReceipt(event_id=evidence.event_id, replayed=True)
                    raise HTTPException(status_code=409, detail="factory event_id already exists with different content")
                projection = self._factory_projection(cursor, parse_factory_job(job_block["data"]))
                lease = None
                if evidence.lease_id is not None:
                    lease_block = self._runtime_block_by_json_value(
                        cursor,
                        block_type="agent_factory_lease",
                        field="lease_id",
                        value=evidence.lease_id,
                        for_update=True,
                    )
                    if lease_block is not None:
                        lease = FactoryLease.model_validate(lease_block["data"])
                job = projection.job
                evaluation = None
                release_decision = None
                workflow_evaluation = None
                feedback = None
                benchmark_summary = None
                if isinstance(job, AgentFactoryJobV3) and evidence.phase in {
                    FactoryPhase.QUALITY_REVIEWED,
                    FactoryPhase.CAPABILITY_PROMOTED,
                }:
                    workflow_evaluation, feedback = self._factory_workflow_review(
                        cursor,
                        job.job_id,
                        attempt=evidence.attempt,
                        for_update=True,
                    )
                    if workflow_evaluation is not None and workflow_evaluation.benchmark_summary_ref is not None:
                        benchmark_summary = self._factory_business_benchmark_summary_by_artifact(
                            cursor,
                            workflow_evaluation.benchmark_summary_ref,
                            for_update=True,
                        )
                    if evidence.phase is FactoryPhase.CAPABILITY_PROMOTED:
                        release_decision = self._factory_workflow_release_decision(
                            cursor,
                            job,
                            attempt=evidence.attempt,
                            evaluation=workflow_evaluation,
                            for_update=True,
                        )
                        if workflow_evaluation is not None and feedback is not None:
                            required_refs = {
                                workflow_evaluation.artifact_ref,
                                feedback.artifact_ref,
                            }
                            if not required_refs.issubset(evidence.artifact_refs):
                                raise HTTPException(
                                    status_code=409,
                                    detail=(
                                        "capability promotion must reference its workflow "
                                        "evaluation and feedback"
                                    ),
                                )
                elif evidence.phase is FactoryPhase.CAPABILITY_PROMOTED:
                    evaluation = self._factory_skill_evaluation_for_job(cursor, evidence.job_id)
                    if evaluation is None:
                        raise HTTPException(
                            status_code=409,
                            detail="missing accepted Hermes skill evaluation evidence",
                        )
                    self._assert_evaluation_is_published(cursor, evaluation)
                    if evaluation.evidence_ref not in evidence.evidence_refs:
                        raise HTTPException(
                            status_code=409,
                            detail="capability promotion must reference its accepted skill evaluation",
                        )
                    release_decision = self._factory_release_decision_for_job(
                        cursor,
                        evidence.job_id,
                    )
                if evidence.phase is FactoryPhase.TECHNICAL_REVALIDATION_REQUESTED:
                    technical_blocks = tuple(
                        block
                        for block in self._factory_blocks(cursor, evidence.job_id)
                        if block.attempt == evidence.attempt
                        and block.phase
                        in {
                            FactoryPhase.REAL_CASE_EVIDENCE,
                            FactoryPhase.REAL_CASE_REVALIDATED,
                        }
                    )
                    if (
                        not technical_blocks
                        or technical_blocks[-1].status is not FactoryBlockStatus.FAILED
                        or evidence.artifact_refs[0]
                        not in technical_blocks[-1].evidence_refs
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "technical revalidation must supersede the latest "
                                "failed technical evidence"
                            ),
                        )
                try:
                    apply_block(
                        projection,
                        evidence,
                        evaluation=evaluation,
                        release_decision=release_decision,
                        workflow_evaluation=workflow_evaluation,
                        feedback=feedback,
                        benchmark_summary=benchmark_summary,
                    )
                except FactoryLifecycleError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                self._assert_evidence_lease(
                    evidence,
                    lease,
                    runtime_retry_authorization=runtime_retry_authorization,
                )
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_factory_block",
                    data=canonical,
                    status=evidence.status.value,
                    parent_index=job_block["index"],
                    metadata={"schema": "captain.agent-factory-block.v1", "phase": evidence.phase.value},
                )
                self._insert(cursor, block)
        return FactoryWriteReceipt(event_id=evidence.event_id, replayed=False)

    def factory_job(self, job_id: UUID) -> FactoryJobProjection:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id", value=str(job_id)
                )
                if job_block is None:
                    raise HTTPException(status_code=404, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                blocks = self._factory_blocks(cursor, job_id)
                leases = self._factory_leases(cursor, job_id)
                projection = FactoryProjection.from_job(job)
                for evidence in blocks:
                    (
                        evaluation,
                        release_decision,
                        workflow_evaluation,
                        feedback,
                        benchmark_summary,
                    ) = self._factory_block_context(
                        cursor,
                        job,
                        evidence,
                        for_update=False,
                    )
                    projection = apply_block(
                        projection,
                        evidence,
                        evaluation=evaluation,
                        release_decision=release_decision,
                        workflow_evaluation=workflow_evaluation,
                        feedback=feedback,
                        benchmark_summary=benchmark_summary,
                    )
        return FactoryJobProjection(job=job, blocks=blocks, leases=leases, projection=projection)

    def record_factory_lease(self, lease: FactoryLease) -> FactoryWriteReceipt:
        canonical = lease.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id", value=str(lease.job_id), for_update=True
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job not found")
                job = parse_factory_job(job_block["data"])
                projection = self._factory_projection(cursor, job)
                self._assert_lease_is_next_action(lease, projection)
                try:
                    validate_factory_lease(lease, job=job, role=lease.role, attempt=projection.attempt, now=lease.issued_at)
                except FactoryLeaseDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                existing = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_lease", field="lease_id", value=lease.lease_id, for_update=True
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        return FactoryWriteReceipt(event_id=job.event_id, replayed=True)
                    raise HTTPException(status_code=409, detail="factory lease already exists with different content")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor, index=index, block_type="agent_factory_lease", data=canonical,
                    status="active", parent_index=job_block["index"],
                    metadata={"schema": "captain.factory-lease.v1", "role": lease.role.value},
                )
                self._insert(cursor, block)
        return FactoryWriteReceipt(event_id=job.event_id, replayed=False)

    def _factory_projection(self, cursor: Any, job: FactoryJob) -> FactoryProjection:
        projection = FactoryProjection.from_job(job)
        for evidence in self._factory_blocks(cursor, job.job_id, for_update=True):
            (
                evaluation,
                release_decision,
                workflow_evaluation,
                feedback,
                benchmark_summary,
            ) = self._factory_block_context(
                cursor,
                job,
                evidence,
                for_update=True,
            )
            projection = apply_block(
                projection,
                evidence,
                evaluation=evaluation,
                release_decision=release_decision,
                workflow_evaluation=workflow_evaluation,
                feedback=feedback,
                benchmark_summary=benchmark_summary,
            )
        return projection

    def _factory_budget_job(
        self,
        cursor: Any,
        job_id: UUID,
        *,
        missing_status: int = 409,
        require_v3: bool = True,
        for_update: bool = True,
    ) -> tuple[FactoryJob, dict[str, Any]]:
        job_block = self._runtime_block_by_json_value(
            cursor,
            block_type="agent_factory_job",
            field="job_id",
            value=str(job_id),
            for_update=for_update,
        )
        if job_block is None:
            raise HTTPException(status_code=missing_status, detail="factory job not found")
        try:
            job = parse_factory_job(job_block["data"])
        except (TypeError, ValueError, ValidationError) as exc:
            raise HTTPException(status_code=409, detail="stored factory job is invalid") from exc
        if require_v3 and not isinstance(job, AgentFactoryJobV3):
            raise HTTPException(status_code=409, detail="paid effects require a V3 factory job")
        return job, job_block

    @staticmethod
    def _assert_budget_reservation(
        job: FactoryJob,
        reservation: FactoryBudgetReservationV1,
    ) -> None:
        if not isinstance(job, AgentFactoryJobV3):
            raise HTTPException(status_code=409, detail="paid effects require a V3 factory job")
        policy_digest = GatewayStore._factory_execution_policy_sha256(job)
        if (
            not job.execution_policy.live_execution
            or reservation.job_id != job.job_id
            or reservation.correlation_id != job.correlation_id
            or reservation.subject_version != job.subject_version
            or reservation.execution_policy_sha256 != policy_digest
            or reservation.attempt > job.max_behavioral_iterations
            or reservation.reserved_at < job.occurred_at
            or reservation.reserved_at >= job.deadline_at
            or reservation.expires_at != job.deadline_at
        ):
            raise HTTPException(status_code=409, detail="factory budget reservation does not match its V3 job policy")

    @staticmethod
    def _assert_budget_usage(
        job: FactoryJob,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
    ) -> None:
        if not isinstance(job, AgentFactoryJobV3):
            raise HTTPException(status_code=409, detail="paid effects require a V3 factory job")
        if (
            receipt.reservation_id != reservation.reservation_id
            or receipt.job_id != job.job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.attempt != reservation.attempt
        ):
            raise HTTPException(status_code=409, detail="factory usage receipt binding mismatch")
        if receipt.model not in job.execution_policy.allowed_models:
            raise HTTPException(status_code=409, detail="factory usage receipt names an unapproved model")
        if receipt.started_at < reservation.reserved_at or receipt.ended_at > reservation.expires_at:
            raise HTTPException(status_code=409, detail="factory usage receipt is outside its reservation window")

    def _assert_budget_usage_lease(
        self,
        cursor: Any,
        job: FactoryJob,
        submission: FactoryUsageSubmissionV2,
    ) -> None:
        receipt = submission.receipt
        lease_block = self._runtime_block_by_json_value(
            cursor,
            block_type="agent_factory_lease",
            field="lease_id",
            value=submission.lease_id,
            for_update=True,
        )
        if lease_block is None:
            raise HTTPException(
                status_code=409,
                detail="factory usage requires a matching active lease",
            )
        lease = FactoryLease.model_validate(lease_block["data"])
        if (
            lease.role is not FactoryRole.REAL_CASE_TESTER
            or "model.invoke" not in lease.capabilities
        ):
            raise HTTPException(
                status_code=409,
                detail="factory usage lease does not authorize the paid model effect",
            )
        if (
            submission.subject_version != job.subject_version
            or lease.lease_id != submission.lease_id
            or lease.job_id != job.job_id
            or lease.correlation_id != job.correlation_id
            or lease.subject_version != submission.subject_version
            or lease.attempt != receipt.attempt
            or receipt.ended_at >= lease.expires_at
        ):
            raise HTTPException(
                status_code=409,
                detail="factory usage lease or subject binding mismatch",
            )
        try:
            validate_factory_lease(
                lease,
                job=job,
                role=lease.role,
                attempt=receipt.attempt,
                now=receipt.started_at,
            )
        except FactoryLeaseDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @staticmethod
    def _assert_workflow_artifact(
        job: FactoryJob,
        artifact: FactoryWorkflowArtifact,
        prior_artifacts: tuple[FactoryWorkflowArtifact, ...],
    ) -> None:
        if (
            artifact.job_id != job.job_id
            or artifact.correlation_id != job.correlation_id
            or artifact.subject_version != job.subject_version
            or artifact.attempt > job.max_behavioral_iterations
            or artifact.acceptance_assertion_ids != job.acceptance_assertion_ids
            or artifact.invocation.released_skill.capability
            != job.required_capability
        ):
            raise HTTPException(status_code=409, detail="factory workflow artifact job binding mismatch")
        if (
            artifact.invocation.released_skill.skill_id
            != FACTORY_SKILL_ID_BY_STEP[artifact.invocation.step]
        ):
            raise HTTPException(
                status_code=409,
                detail="factory workflow artifact released skill ID does not match step",
            )
        expected_input = GatewayStore._workflow_input_ref(
            job,
            artifact,
            prior_artifacts,
        )
        if artifact.invocation.input_ref != expected_input:
            raise HTTPException(
                status_code=409,
                detail="factory workflow artifact input binding mismatch",
            )
        if isinstance(job, AgentFactoryJobV3) and isinstance(
            artifact, TeamExecutionEvidenceV1
        ):
            if artifact.holdout_ref not in job.private_holdout_refs:
                raise HTTPException(
                    status_code=409,
                    detail="workflow execution holdout is not authorized by the Factory job",
                )
            required_runs = job.execution_policy.required_live_runs
            release_trace = required_runs > 1
            if release_trace:
                valid_run = (
                    artifact.holdout_ref == job.private_holdout_refs[0]
                    and 1 <= artifact.run_number <= required_runs
                )
            else:
                valid_run = artifact.run_number == (
                    job.private_holdout_refs.index(artifact.holdout_ref) + 1
                )
            if not valid_run:
                raise HTTPException(
                    status_code=409,
                    detail="workflow execution run number does not match Captain release authority",
                )

    @staticmethod
    def _workflow_input_ref(
        job: FactoryJob,
        artifact: FactoryWorkflowArtifact,
        prior_artifacts: tuple[FactoryWorkflowArtifact, ...],
    ) -> ArtifactRef:
        current = tuple(
            candidate
            for candidate in prior_artifacts
            if candidate.attempt == artifact.attempt
        )
        step = artifact.invocation.step
        if step is FactorySkillStep.IMPROVE_TEAM:
            requests = tuple(
                reference
                for reference in artifact.evidence_refs
                if reference.uri.startswith(
                    "artifact://factory/improvement-request/"
                )
            )
            if len(requests) == 1:
                return requests[0]
        if step is FactorySkillStep.REPORT_CAPTAIN:
            evaluations = tuple(
                candidate
                for candidate in current
                if isinstance(candidate, TeamEvaluationV1)
            )
            if len(evaluations) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="report_captain requires one exact evaluation predecessor",
                )
            return evaluations[0].artifact_ref
        if step is FactorySkillStep.BRIEF_CODEX:
            predecessor_type = (
                CandidateRevisionV1
                if artifact.attempt > 1
                else CodebaseInventoryV1
            )
            predecessors = tuple(
                candidate
                for candidate in current
                if isinstance(candidate, predecessor_type)
            )
            if len(predecessors) == 1:
                return predecessors[0].artifact_ref
        return job.input_ref

    @staticmethod
    def _assert_workflow_sequence(
        projection: FactoryProjection,
        artifact: FactoryWorkflowArtifact,
        prior_artifacts: tuple[FactoryWorkflowArtifact, ...],
    ) -> None:
        step = artifact.invocation.step
        required_phase = {
            FactorySkillStep.DISCOVER: {FactoryPhase.FORGE_REQUESTED},
            FactorySkillStep.IMPROVE_TEAM: {FactoryPhase.IMPROVEMENT_REQUESTED},
            FactorySkillStep.BRIEF_CODEX: {FactoryPhase.BLUEPRINT_CREATED},
            FactorySkillStep.EXECUTE_TEAM: {
                FactoryPhase.BUILD_PASSED,
                FactoryPhase.TECHNICAL_REVALIDATION_REQUESTED,
            },
            FactorySkillStep.EVALUATE_TEAM: {
                FactoryPhase.REAL_CASE_EVIDENCE,
                FactoryPhase.REAL_CASE_REVALIDATED,
            },
            FactorySkillStep.REPORT_CAPTAIN: {
                FactoryPhase.REAL_CASE_EVIDENCE,
                FactoryPhase.REAL_CASE_REVALIDATED,
            },
        }[step]
        current_artifacts = tuple(
            candidate
            for candidate in prior_artifacts
            if candidate.attempt == artifact.attempt
        )
        if step is FactorySkillStep.DISCOVER and artifact.attempt > 1:
            required_phase = {
                *required_phase,
                FactoryPhase.IMPROVEMENT_REQUESTED,
            }
        if (
            step is FactorySkillStep.IMPROVE_TEAM
            and artifact.attempt > 1
            and any(
                isinstance(candidate, CodebaseInventoryV1)
                for candidate in current_artifacts
            )
        ):
            required_phase = {
                *required_phase,
                FactoryPhase.BLUEPRINT_CREATED,
            }
        if (
            step is FactorySkillStep.BRIEF_CODEX
            and artifact.attempt > 1
            and any(
                isinstance(candidate, CandidateRevisionV1)
                and candidate.attempt == artifact.attempt
                for candidate in prior_artifacts
            )
        ):
            required_phase = {
                *required_phase,
                FactoryPhase.IMPROVEMENT_REQUESTED,
            }
        if projection.phase not in required_phase:
            raise HTTPException(
                status_code=409,
                detail="workflow artifact does not match the current factory phase",
            )
        if artifact.attempt != projection.attempt:
            raise HTTPException(
                status_code=409,
                detail="workflow artifact attempt is not the current factory attempt",
            )
        if step is FactorySkillStep.IMPROVE_TEAM and artifact.attempt == 1:
            raise HTTPException(
                status_code=409,
                detail="improve_team is allowed only on a later attempt",
            )
        prior_failed_workflow_evaluation = any(
            isinstance(candidate, TeamEvaluationV1)
            and candidate.attempt == artifact.attempt - 1
            and candidate.failure_class is not None
            and candidate.recommendation
            == FactoryFeedbackRecommendation.RETRY_BUILD
            for candidate in prior_artifacts
        )
        bound_technical_failure = (
            isinstance(artifact, CandidateRevisionV1)
            and bool(artifact.failed_assertion_ids)
            and any(
                reference.uri.startswith(
                    "artifact://factory/technical-failure-evaluation/"
                )
                for reference in artifact.evidence_refs
            )
        )
        if (
            step is FactorySkillStep.IMPROVE_TEAM
            and not prior_failed_workflow_evaluation
            and not bound_technical_failure
        ):
            raise HTTPException(
                status_code=409,
                detail="improve_team requires the prior attempt failed evaluation",
            )
        prefix = (
            (
                FactorySkillStep.DISCOVER,
                FactorySkillStep.IMPROVE_TEAM,
                FactorySkillStep.BRIEF_CODEX,
            )
            if artifact.attempt > 1
            else (
                FactorySkillStep.DISCOVER,
                FactorySkillStep.BRIEF_CODEX,
            )
        )
        prior_steps = tuple(
            candidate.invocation.step
            for candidate in prior_artifacts
            if candidate.attempt == artifact.attempt
        )
        sequence_valid = False
        if step is FactorySkillStep.DISCOVER:
            sequence_valid = not prior_steps
        elif step is FactorySkillStep.IMPROVE_TEAM:
            sequence_valid = prior_steps == prefix[:1]
        elif step is FactorySkillStep.BRIEF_CODEX:
            sequence_valid = prior_steps == prefix[:-1]
        elif step is FactorySkillStep.EXECUTE_TEAM:
            sequence_valid = (
                prior_steps[: len(prefix)] == prefix
                and all(
                    prior_step is FactorySkillStep.EXECUTE_TEAM
                    for prior_step in prior_steps[len(prefix) :]
                )
            )
            executions = tuple(
                candidate
                for candidate in prior_artifacts
                if candidate.attempt == artifact.attempt
                and isinstance(candidate, TeamExecutionEvidenceV1)
            )
            if isinstance(artifact, TeamExecutionEvidenceV1):
                all_executions = (*executions, artifact)
                identities_unique = all(
                    len(values) == len(set(values))
                    for values in (
                        tuple(item.invocation_id for item in all_executions),
                        tuple(
                            item.invocation.idempotency_key
                            for item in all_executions
                        ),
                    )
                )
                run_numbers_unique = len(all_executions) == len(
                    {item.run_number for item in all_executions}
                )
                holdouts_unique = len(all_executions) == len(
                    {item.holdout_ref for item in all_executions}
                )
                repeated_release_holdout = (
                    isinstance(projection.job, AgentFactoryJobV3)
                    and projection.job.execution_policy.required_live_runs > 1
                    and all(
                        item.holdout_ref == projection.job.private_holdout_refs[0]
                        for item in all_executions
                    )
                )
                run_scope_valid = run_numbers_unique and (
                    holdouts_unique or repeated_release_holdout
                )
                authorized_revalidation = False
                if (
                    projection.phase
                    is FactoryPhase.TECHNICAL_REVALIDATION_REQUESTED
                    and projection.technical_revalidation_authorization_ref
                    is not None
                    and projection.technical_revalidation_supersedes_ref is not None
                    and bool(executions)
                ):
                    prior = executions[-1]
                    authorized_revalidation = (
                        prior.status != "succeeded"
                        and artifact.run_number == prior.run_number
                        and artifact.holdout_ref == prior.holdout_ref
                        and artifact.candidate_ref == prior.candidate_ref
                        and projection.technical_revalidation_authorization_ref
                        in artifact.evidence_refs
                        and projection.technical_revalidation_supersedes_ref
                        in artifact.evidence_refs
                    )
                sequence_valid = (
                    sequence_valid
                    and identities_unique
                    and (run_scope_valid or authorized_revalidation)
                )
        elif step is FactorySkillStep.EVALUATE_TEAM:
            sequence_valid = (
                prior_steps[: len(prefix)] == prefix
                and bool(prior_steps[len(prefix) :])
                and all(
                    prior_step is FactorySkillStep.EXECUTE_TEAM
                    for prior_step in prior_steps[len(prefix) :]
                )
            )
        elif step is FactorySkillStep.REPORT_CAPTAIN:
            sequence_valid = (
                prior_steps[: len(prefix)] == prefix
                and len(prior_steps) >= len(prefix) + 2
                and all(
                    prior_step is FactorySkillStep.EXECUTE_TEAM
                    for prior_step in prior_steps[len(prefix) : -1]
                )
                and prior_steps[-1] is FactorySkillStep.EVALUATE_TEAM
            )
        if not sequence_valid:
            raise HTTPException(
                status_code=409,
                detail="workflow artifact is missing its exact prior workflow artifact sequence",
            )

    def _factory_workflow_artifacts_for_job(
        self,
        cursor: Any,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        sql = (
            "SELECT payload FROM factory_workflow_artifacts "
            "WHERE job_id = %s ORDER BY block_index"
        )
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job_id),))
        return tuple(
            parse_factory_workflow_artifact(self._decode_json(row["payload"]))
            for row in cursor.fetchall()
        )

    def _factory_business_benchmark_summary_by_artifact(
        self,
        cursor: Any,
        artifact_ref: ArtifactRef,
        *,
        for_update: bool,
    ) -> BusinessBenchmarkSummaryV1 | None:
        sql = (
            "SELECT payload FROM factory_business_benchmark_summaries "
            "WHERE artifact_sha256 = %s"
        )
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (artifact_ref.sha256,))
        row = cursor.fetchone()
        if row is None:
            return None
        summary = BusinessBenchmarkSummaryV1.model_validate(
            self._decode_json(row["payload"])
        )
        return summary if summary.artifact_ref == artifact_ref else None

    @staticmethod
    def _business_benchmark_validated_event(
        summary: BusinessBenchmarkSummaryV1,
        content_sha256: str,
    ) -> DeliveryEventEnvelope:
        identity = "|".join(
            (
                "captain-business-benchmark-validated",
                str(summary.job_id),
                str(summary.correlation_id),
                str(summary.subject_version),
                str(summary.attempt),
                summary.candidate_ref.sha256,
                summary.artifact_ref.sha256,
                content_sha256,
            )
        )
        event_id = uuid5(NAMESPACE_URL, identity)
        return DeliveryEventEnvelope(
            event_id=event_id,
            event_type="captain_business_benchmark_validated",
            occurred_at=summary.evaluated_at,
            actor="captain",
            trace=TraceContext(
                project_id="agent-factory",
                run_id=str(summary.job_id),
                trace_id=str(uuid5(NAMESPACE_URL, "trace|" + identity)),
                job_id=summary.job_id,
                correlation_id=summary.correlation_id,
                subject_version=summary.subject_version,
                candidate_id=summary.candidate_ref.sha256,
                artifact_id=summary.artifact_ref.sha256,
            ),
            payload=CaptainBusinessBenchmarkValidatedPayload(
                event_type="captain_business_benchmark_validated",
                summary_id=summary.summary_id,
                attempt=summary.attempt,
                candidate_sha256=summary.candidate_ref.sha256,
                artifact_ref=summary.artifact_ref,
                content_sha256=content_sha256,
            ),
        )

    def _factory_skill_assignment_for_step(
        self,
        cursor: Any,
        job_id: UUID,
        step: FactorySkillStep,
        *,
        for_update: bool,
    ) -> FactorySkillAssignmentV1 | None:
        sql = (
            "SELECT payload FROM factory_skill_assignments "
            "WHERE job_id = %s AND step = %s"
        )
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job_id), step.value))
        row = cursor.fetchone()
        return (
            FactorySkillAssignmentV1.model_validate(
                self._decode_json(row["payload"])
            )
            if row is not None
            else None
        )

    def _assert_workflow_skill_assignment(
        self,
        cursor: Any,
        artifact: FactoryWorkflowArtifact,
    ) -> None:
        assignment = self._factory_skill_assignment_for_step(
            cursor,
            artifact.job_id,
            artifact.invocation.step,
            for_update=True,
        )
        if (
            assignment is None
            or assignment.released_skill
            != artifact.invocation.released_skill
        ):
            raise HTTPException(
                status_code=409,
                detail="factory workflow artifact does not match its skill assignment",
            )

    def _factory_workflow_review(
        self,
        cursor: Any,
        job_id: UUID,
        *,
        attempt: int,
        for_update: bool,
    ) -> tuple[TeamEvaluationV1 | None, FactoryFeedbackV1 | None]:
        artifacts = self._factory_workflow_artifacts_for_job(
            cursor,
            job_id,
            for_update=for_update,
        )
        evaluations = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamEvaluationV1)
            and artifact.attempt == attempt
        )
        feedback_items = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, FactoryFeedbackV1)
            and artifact.attempt == attempt
        )
        if len(evaluations) > 1 or len(feedback_items) > 1:
            raise HTTPException(
                status_code=409,
                detail="factory workflow review is ambiguous for the current attempt",
            )
        return (
            evaluations[0] if evaluations else None,
            feedback_items[0] if feedback_items else None,
        )

    def _factory_workflow_release_decision(
        self,
        cursor: Any,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        evaluation: TeamEvaluationV1 | None,
        for_update: bool,
    ) -> FactoryReleaseDecision | None:
        if evaluation is None:
            return None
        artifacts = self._factory_workflow_artifacts_for_job(
            cursor,
            job.job_id,
            for_update=for_update,
        )
        executions = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
            and artifact.attempt == attempt
        )
        return evaluate_factory_workflow_release(
            job,
            executions,
            evaluation,
            benchmark_summary=(
                None
                if evaluation.benchmark_summary_ref is None
                else self._factory_business_benchmark_summary_by_artifact(
                    cursor,
                    evaluation.benchmark_summary_ref,
                    for_update=for_update,
                )
            ),
            budget_projection=self._factory_budget_projection(
                cursor,
                job,
                for_update=for_update,
            ),
            usage_receipts=self._factory_usage_receipts_for_job(
                cursor,
                job.job_id,
                for_update=for_update,
            ),
        )

    def _factory_block_context(
        self,
        cursor: Any,
        job: FactoryJob,
        evidence: FactoryEvidenceBlock,
        *,
        for_update: bool,
    ) -> tuple[
        StoredSkillEvaluation | None,
        FactoryReleaseDecision | None,
        TeamEvaluationV1 | None,
        FactoryFeedbackV1 | None,
        BusinessBenchmarkSummaryV1 | None,
    ]:
        evaluation = None
        release_decision = None
        workflow_evaluation = None
        feedback = None
        benchmark_summary = None
        if isinstance(job, AgentFactoryJobV3) and evidence.phase in {
            FactoryPhase.QUALITY_REVIEWED,
            FactoryPhase.CAPABILITY_PROMOTED,
        }:
            workflow_evaluation, feedback = self._factory_workflow_review(
                cursor,
                job.job_id,
                attempt=evidence.attempt,
                for_update=for_update,
            )
            if workflow_evaluation is not None and workflow_evaluation.benchmark_summary_ref is not None:
                benchmark_summary = self._factory_business_benchmark_summary_by_artifact(
                    cursor,
                    workflow_evaluation.benchmark_summary_ref,
                    for_update=for_update,
                )
            if evidence.phase is FactoryPhase.CAPABILITY_PROMOTED:
                release_decision = self._factory_workflow_release_decision(
                    cursor,
                    job,
                    attempt=evidence.attempt,
                    evaluation=workflow_evaluation,
                    for_update=for_update,
                )
        elif evidence.phase is FactoryPhase.CAPABILITY_PROMOTED:
            evaluation = self._factory_skill_evaluation_for_job(cursor, job.job_id)
            release_decision = self._factory_release_decision_for_job(
                cursor,
                job.job_id,
            )
        return evaluation, release_decision, workflow_evaluation, feedback, benchmark_summary

    @staticmethod
    def _assert_factory_effects_open(
        projection: FactoryProjection,
        *,
        effect: str,
    ) -> None:
        if projection.status in {
            FactoryLifecycleStatus.READY_TO_USE,
            FactoryLifecycleStatus.ESCALATED,
        }:
            raise HTTPException(
                status_code=409,
                detail=f"terminal factory job refuses new {effect}",
            )

    def _factory_budget_event(
        self,
        cursor: Any,
        event_id: UUID,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = "SELECT event_id, reservation_id, job_id, event_kind, content_sha256, payload FROM factory_budget_events WHERE event_id = %s"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(event_id),))
        return cursor.fetchone()

    def _factory_reservation(
        self,
        cursor: Any,
        reservation_id: UUID,
        *,
        for_update: bool,
    ) -> FactoryBudgetReservationV1 | None:
        sql = "SELECT payload FROM factory_budget_events WHERE reservation_id = %s AND event_kind = 'reservation' ORDER BY block_index LIMIT 1"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(reservation_id),))
        row = cursor.fetchone()
        return (
            FactoryBudgetReservationV1.model_validate(self._decode_json(row["payload"]))
            if row is not None
            else None
        )

    @staticmethod
    def _reservation_is_closed(cursor: Any, reservation_id: UUID) -> bool:
        cursor.execute(
            "SELECT 1 FROM factory_budget_events WHERE reservation_id = %s AND event_kind IN ('usage', 'release', 'resume_cost') LIMIT 1 FOR UPDATE",
            (str(reservation_id),),
        )
        return cursor.fetchone() is not None

    def _factory_usage_receipts_for_job(
        self,
        cursor: Any,
        job_id: UUID,
        *,
        for_update: bool,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        sql = (
            "SELECT payload FROM factory_budget_events "
            "WHERE job_id = %s AND event_kind = 'usage' ORDER BY block_index"
        )
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job_id),))
        return tuple(
            self._factory_usage_receipt(row["payload"])
            for row in cursor.fetchall()
        )

    def _factory_budget_projection(
        self,
        cursor: Any,
        job: FactoryJob,
        *,
        for_update: bool = False,
    ) -> FactoryBudgetProjection:
        if not isinstance(job, AgentFactoryJobV3):
            raise HTTPException(status_code=409, detail="paid effects require a V3 factory job")
        sql = "SELECT event_kind, payload FROM factory_budget_events WHERE job_id = %s ORDER BY block_index"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job.job_id),))
        active: dict[UUID, Decimal] = {}
        consumed = Decimal("0")
        for row in cursor.fetchall():
            payload = self._decode_json(row["payload"])
            if row["event_kind"] == "reservation":
                reservation = FactoryBudgetReservationV1.model_validate(payload)
                active[reservation.reservation_id] = reservation.requested_usd
            elif row["event_kind"] == "usage":
                receipt = self._factory_usage_receipt(payload)
                consumed += receipt.cost_usd
                active.pop(receipt.reservation_id, None)
            elif row["event_kind"] == "resume_cost":
                settlement = RuntimeResumeCostSettlementV1.model_validate(payload)
                consumed += settlement.accounted_cost_usd
                active.pop(settlement.reservation_id, None)
            else:
                active.pop(UUID(payload["reservation_id"]), None)
        limit = job.execution_policy.max_cost_usd
        reserved = sum(active.values(), start=Decimal("0"))
        return FactoryBudgetProjection(
            job_id=job.job_id,
            limit_usd=limit,
            consumed_usd=consumed,
            reserved_usd=reserved,
            remaining_usd=limit - consumed - reserved,
            active_reservation_ids=tuple(active),
        )

    @staticmethod
    def _factory_usage_receipt(payload: object) -> FactoryUsageReceiptV1:
        decoded = GatewayStore._decode_json(payload)
        if isinstance(decoded, dict) and decoded.get("schema") == (
            "captain.factory-usage-submission.v2"
        ):
            return FactoryUsageSubmissionV2.model_validate(decoded).receipt
        return FactoryUsageReceiptV1.model_validate(decoded)

    @staticmethod
    def _factory_budget_payload_identity(
        payload: FactoryUsageSubmissionV2 | FactoryUsageReceiptV1,
    ) -> tuple[str, str]:
        return GatewayStore._canonical_model_sha256(payload), payload.schema_name

    def _insert_factory_budget_event(
        self,
        cursor: Any,
        *,
        event_id: UUID,
        reservation_id: UUID,
        job_id: UUID,
        event_kind: str,
        digest: str,
        payload: dict[str, Any],
        parent_index: int,
        schema_name: str,
    ) -> None:
        index = self._next_index(cursor)
        block = self._new_block(
            cursor,
            index=index,
            block_type="factory_budget_event",
            data=payload,
            status="accepted",
            parent_index=parent_index,
            metadata={"schema": schema_name, "event_kind": event_kind},
        )
        self._insert(cursor, block)
        cursor.execute(
            """INSERT INTO factory_budget_events
               (event_id, reservation_id, job_id, event_kind, content_sha256, block_index, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(event_id), str(reservation_id), str(job_id), event_kind, digest,
                index, json.dumps(payload, sort_keys=True),
            ),
        )

    @staticmethod
    def _factory_execution_policy_sha256(job: AgentFactoryJobV3) -> str:
        payload = job.execution_policy.model_dump(mode="json", by_alias=True)
        rendered = format(job.execution_policy.max_cost_usd, "f")
        payload["max_cost_usd"] = (
            "0"
            if job.execution_policy.max_cost_usd == 0
            else rendered.rstrip("0").rstrip(".")
            if "." in rendered
            else rendered
        )
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def _factory_blocks(
        self, cursor: Any, job_id: UUID, *, for_update: bool = False
    ) -> tuple[FactoryEvidenceBlock, ...]:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks WHERE block_type = 'agent_factory_block'
            AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.job_id')) = %s ORDER BY `index`
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job_id),))
        return tuple(
            FactoryEvidenceBlock.model_validate(self.storage._decode_row(row)["data"])
            for row in cursor.fetchall()
        )

    def minibook_projection_feed(
        self,
        *,
        after_index: int,
        limit: int,
    ) -> tuple[
        list[tuple[int, str, dict[str, Any], dict[str, Any] | None]],
        bool,
    ]:
        """Read every admitted Minibook projection from one ledger-ordered page."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event.`index`, event.parent_index,
                           event.block_type, event.data,
                           event.status, event.children,
                           event.metadata, event.hash,
                           event.previous_hash,
                           parent.data AS parent_data
                    FROM blocks AS event
                    LEFT JOIN blocks AS parent ON parent.`index` = event.parent_index
                    WHERE event.`index` > %s
                      AND (
                        event.block_type = 'agent_runtime_result'
                        OR (
                          event.block_type = 'agent_factory_block'
                          AND parent.block_type = 'agent_factory_job'
                          AND JSON_UNQUOTE(JSON_EXTRACT(event.data, '$.phase')) = 'capability_promoted'
                          AND JSON_UNQUOTE(JSON_EXTRACT(event.data, '$.status')) = 'succeeded'
                        )
                        OR (
                          event.block_type = 'factory_integration_setup'
                          AND parent.block_type = 'agent_factory_job'
                        )
                      )
                    ORDER BY event.`index`
                    LIMIT %s
                    """,
                    (after_index, limit + 1),
                )
                rows = list(cursor.fetchall())
        has_more = len(rows) > limit
        records: list[
            tuple[int, str, dict[str, Any], dict[str, Any] | None]
        ] = []
        for row in rows[:limit]:
            event = self.storage._decode_row(row)
            parent = (
                self._decode_json(row["parent_data"])
                if row["parent_data"] is not None
                else None
            )
            records.append(
                (int(row["index"]), str(event["block_type"]), event["data"], parent)
            )
        return records, has_more

    def record_minibook_projection_acknowledgement(
        self,
        acknowledgement: MinibookProjectionAcknowledgementV1,
    ) -> AppendResult:
        """Persist one exact Factory-to-Minibook acknowledgement as delivery evidence."""

        source = self.factory_promotion_source(
            acknowledgement.projection_event_id
        )
        integration_source = (
            self.integration_setup_source(acknowledgement.projection_event_id)
            if source is None
            else None
        )
        generic_payload = (
            self.generic_capability_promotion(acknowledgement.projection_event_id)
            if source is None and integration_source is None
            else None
        )
        if generic_payload is not None:
            from agenten.delivery.minibook_events import MinibookProjectionEvent
            from agenten.delivery.projector import MinibookProjector

            event = MinibookProjectionEvent.model_validate(generic_payload)
            rendered = MinibookProjector.render(event)
            expected_post_id = "captain-projection-" + hashlib.sha256(
                str(event.event_id).encode("utf-8")
            ).hexdigest()[:32]
            expected = {
                "projection_event_id": event.event_id,
                "correlation_id": event.correlation_id,
                "subject_id": event.subject_id,
                "subject_version": event.subject_version,
                "project_id": MinibookProjector.PROJECTION_PROJECT_ID,
                "post_id": expected_post_id,
                "content_sha256": rendered.content_hash,
                "outcome": "mirrored",
            }
            if acknowledgement.model_dump(include=set(expected)) != expected:
                raise HTTPException(
                    status_code=409,
                    detail="Minibook acknowledgement does not match generic promotion",
                )
            job_id = UUID(str(event.subject_id).removeprefix("subject:"))
            job = self.factory_job(job_id).job
            mirror = DeliveryEventEnvelope.model_validate({
                "event_id": acknowledgement.acknowledgement_id,
                "event_type": "registry_mirror",
                "occurred_at": acknowledgement.acknowledged_at,
                "actor": "captain-gateway",
                "trace": {
                    "project_id": f"factory:{job_id}",
                    "run_id": "generic-benchmark-promotion",
                    "trace_id": f"minibook-projection:{event.event_id}",
                    "artifact_id": acknowledgement.post_id,
                    "job_id": job_id,
                    "correlation_id": event.correlation_id,
                    "subject_version": event.subject_version,
                },
                "payload": {
                    "event_type": "registry_mirror",
                    "capability_id": job.required_capability,
                    "capability_version": str(event.subject_version),
                    "outcome": "mirrored",
                },
            })
            return self.append_delivery_event(mirror)

        if source is None:
            if integration_source is None:
                raise HTTPException(
                    status_code=409,
                    detail="Minibook projection event is unavailable",
                )
            submission, integration_job = integration_source
            try:
                mirror = integration_setup_registry_mirror_event(
                    acknowledgement,
                    submission,
                    integration_job,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return self.append_delivery_event(mirror)
        block, job = source
        benchmark_summary = None
        if job.get("schema") == "captain.agent-factory-job.v3":
            job_id = UUID(str(block["job_id"]))
            attempt = int(block["attempt"])
            evaluations = tuple(
                artifact
                for artifact in self.factory_workflow_artifacts(job_id)
                if isinstance(artifact, TeamEvaluationV1)
                and artifact.attempt == attempt
            )
            if (
                len(evaluations) != 1
                or evaluations[0].benchmark_summary_ref is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Factory promotion has no unambiguous business "
                        "benchmark evaluation"
                    ),
                )
            benchmark_summary = self.business_benchmark_summary_by_artifact(
                evaluations[0].benchmark_summary_ref
            )
            if benchmark_summary is None:
                raise HTTPException(
                    status_code=409,
                    detail="Factory promotion business benchmark summary is unavailable",
                )
        try:
            mirror = factory_registry_mirror_event(
                acknowledgement,
                block,
                job,
                benchmark_summary=benchmark_summary,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return self.append_delivery_event(mirror)

    def record_minibook_projection_rebuild_receipt(
        self,
        receipt: MinibookProjectionRebuildReceiptV1,
    ) -> MinibookProjectionRebuildReceiptV1:
        canonical = receipt.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM minibook_projection_rebuild_receipts "
                    "WHERE rebuild_id = %s FOR UPDATE",
                    (str(receipt.rebuild_id),),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    stored = MinibookProjectionRebuildReceiptV1.model_validate(
                        self._decode_json(replay["payload"])
                    )
                    if stored != receipt:
                        raise HTTPException(
                            status_code=409,
                            detail="Minibook rebuild receipt already exists with different content",
                        )
                    return stored
                cursor.execute(
                    """SELECT payload, content_sha256, revision, event_id
                       FROM factory_integration_setup_events
                       WHERE job_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE""",
                    (str(receipt.job_id),),
                )
                setup_row = cursor.fetchone()
                if setup_row is None:
                    raise HTTPException(status_code=409, detail="integration setup is unavailable")
                setup = IntegrationSetupSubmissionV1.model_validate(
                    self._decode_json(setup_row["payload"])
                )
                if (
                    setup.correlation_id != receipt.correlation_id
                    or int(setup_row["revision"]) != receipt.setup_revision
                    or str(setup_row["content_sha256"]) != receipt.setup_content_sha256
                    or UUID(str(setup_row["event_id"])) != receipt.projection_event_id
                ):
                    raise HTTPException(status_code=409, detail="Minibook rebuild setup fence mismatch")
                cursor.execute(
                    """SELECT data FROM blocks
                       WHERE block_type = 'delivery_event'
                         AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.event_id')) = %s
                       LIMIT 1 FOR UPDATE""",
                    (str(receipt.acknowledgement_id),),
                )
                ack_row = cursor.fetchone()
                if ack_row is None:
                    raise HTTPException(
                        status_code=409,
                        detail="integration setup projection acknowledgement is unavailable",
                    )
                acknowledgement = DeliveryEventEnvelope.model_validate(
                    self._decode_json(ack_row["data"])
                )
                if (
                    acknowledgement.event_type != "registry_mirror"
                    or acknowledgement.trace.job_id != receipt.job_id
                    or acknowledgement.trace.correlation_id != receipt.correlation_id
                    or acknowledgement.trace.run_id
                    != f"integration-setup:{receipt.setup_revision}"
                    or acknowledgement.trace.trace_id
                    != f"minibook-projection:{receipt.projection_event_id}"
                    or acknowledgement.occurred_at >= receipt.occurred_at
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="integration setup projection acknowledgement mismatch",
                    )
                job_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_factory_job",
                    field="job_id",
                    value=str(receipt.job_id),
                    for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="factory job is unavailable")
                index = self._next_index(cursor)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="minibook_projection_rebuild_completed",
                    data=canonical,
                    status="converged",
                    parent_index=job_block["index"],
                    metadata={"schema": receipt.schema_name},
                )
                self._insert(cursor, block)
                cursor.execute(
                    """INSERT INTO minibook_projection_rebuild_receipts
                       (rebuild_id, run_id, job_id, correlation_id,
                        projection_event_id, acknowledgement_id, block_index, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(receipt.rebuild_id),
                        receipt.run_id,
                        str(receipt.job_id),
                        str(receipt.correlation_id),
                        str(receipt.projection_event_id),
                        str(receipt.acknowledgement_id),
                        index,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
        return receipt

    def integration_setup_source(
        self,
        projection_event_id: UUID,
    ) -> tuple[IntegrationSetupSubmissionV1, dict[str, Any]] | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT event.payload AS event_payload, parent.data AS parent_data
                       FROM factory_integration_setup_events AS event
                       JOIN blocks AS event_block ON event_block.`index` = event.block_index
                       JOIN blocks AS parent ON parent.`index` = event_block.parent_index
                       WHERE event.event_id = %s
                         AND parent.block_type = 'agent_factory_job'
                       LIMIT 1""",
                    (str(projection_event_id),),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return (
            IntegrationSetupSubmissionV1.model_validate(
                self._decode_json(row["event_payload"])
            ),
            self._decode_json(row["parent_data"]),
        )

    def factory_promotion_source(
        self,
        projection_event_id: UUID,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Resolve only a successful Factory promotion admitted to the feed."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event.data AS event_data, parent.data AS parent_data
                    FROM blocks AS event
                    JOIN blocks AS parent ON parent.`index` = event.parent_index
                    WHERE event.block_type = 'agent_factory_block'
                      AND parent.block_type = 'agent_factory_job'
                      AND JSON_UNQUOTE(JSON_EXTRACT(event.data, '$.event_id')) = %s
                      AND JSON_UNQUOTE(JSON_EXTRACT(event.data, '$.phase')) = 'capability_promoted'
                      AND JSON_UNQUOTE(JSON_EXTRACT(event.data, '$.status')) = 'succeeded'
                    ORDER BY event.`index`
                    LIMIT 1
                    """,
                    (str(projection_event_id),),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return (
            self._decode_json(row["event_data"]),
            self._decode_json(row["parent_data"]),
        )

    def _factory_leases(self, cursor: Any, job_id: UUID) -> tuple[FactoryLease, ...]:
        cursor.execute(
            """SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks WHERE block_type = 'agent_factory_lease'
            AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.job_id')) = %s ORDER BY `index`""",
            (str(job_id),),
        )
        return tuple(
            FactoryLease.model_validate(self.storage._decode_row(row)["data"])
            for row in cursor.fetchall()
        )

    @staticmethod
    def _decode_json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value

    @staticmethod
    def _stored_factory_evaluation(
        submission: FactorySkillEvaluationSubmission,
    ) -> StoredSkillEvaluation:
        GatewayStore._assert_evaluation_references(submission)
        evidence = submission.evidence
        gap_refs = tuple(
            (reference.gap_id, reference.evidence_ref)
            for reference in submission.tool_gap_refs
        )
        return StoredSkillEvaluation(
            evidence=evidence,
            evidence_ref=submission.evidence_ref,
            receipt_ref=submission.receipt_ref,
            tool_gaps=evidence.tool_gaps,
            tool_gap_refs=gap_refs,
            candidate_ref=submission.candidate_ref,
        )

    def _factory_skill_evaluation_row(
        self,
        cursor: Any,
        evidence_id: UUID,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = "SELECT evidence_id, job_id, block_index, payload FROM factory_skill_evaluations WHERE evidence_id = %s"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(evidence_id),))
        return cursor.fetchone()

    def _factory_skill_evaluation_row_for_job(
        self,
        cursor: Any,
        job_id: UUID,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = (
            "SELECT evidence_id, job_id, block_index, payload "
            "FROM factory_skill_evaluations WHERE job_id = %s"
        )
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (str(job_id),))
        return cursor.fetchone()

    def _factory_skill_evaluation_for_job(
        self,
        cursor: Any,
        job_id: UUID,
    ) -> StoredSkillEvaluation | None:
        cursor.execute(
            "SELECT payload FROM factory_skill_evaluations WHERE job_id = %s",
            (str(job_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        submission = FactorySkillEvaluationSubmission.model_validate(
            self._decode_json(row["payload"])
        )
        return self._stored_factory_evaluation(submission)

    def _factory_release_decision_for_job(
        self,
        cursor: Any,
        job_id: UUID,
    ) -> FactoryReleaseDecision | None:
        cursor.execute(
            """SELECT payload FROM factory_release_decisions
               WHERE job_id = %s ORDER BY block_index DESC LIMIT 1""",
            (str(job_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        submission = FactoryReleaseDecisionSubmission.model_validate(
            self._decode_json(row["payload"])
        )
        return submission.decision

    @staticmethod
    def _assert_factory_evaluation_job(
        evidence: HermesSkillEvaluationEvidence,
        job: FactoryJob,
    ) -> None:
        request = evidence.request
        if request.released_skill.capability != job.required_capability:
            raise HTTPException(
                status_code=409,
                detail="released skill capability does not match the factory job",
            )
        if (
            evidence.job_id != job.job_id
            or evidence.correlation_id != job.correlation_id
            or evidence.subject_version != job.subject_version
            or request.job_id != job.job_id
            or request.correlation_id != job.correlation_id
            or request.subject_version != job.subject_version
            or request.subject_id != job.required_capability
            or request.candidate_source_ref != job.input_ref
            or request.acceptance_assertion_ids != job.acceptance_assertion_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="skill evaluation does not match its Captain factory job",
            )

    def _assert_released_skill(self, cursor: Any, skill: ReleasedHermesSkill) -> None:
        cursor.execute(
            "SELECT payload FROM factory_released_skills WHERE skill_id = %s AND version = %s",
            (skill.skill_id, skill.version),
        )
        row = cursor.fetchone()
        if row is None or self._decode_json(row["payload"]) != skill.model_dump(
            mode="json", by_alias=True
        ):
            raise HTTPException(
                status_code=409,
                detail="skill evaluation references an unknown released skill",
            )

    @staticmethod
    def _assert_evaluation_references(
        submission: FactorySkillEvaluationSubmission,
    ) -> None:
        evidence = submission.evidence
        expected_evidence_digest = GatewayStore._canonical_model_sha256(evidence)
        expected_receipt_digest = GatewayStore._canonical_model_sha256(evidence.receipt)
        if (
            submission.evidence_ref.sha256 != expected_evidence_digest
            or submission.evidence_ref.media_type != "application/json"
        ):
            raise HTTPException(
                status_code=409,
                detail="skill evaluation evidence_ref digest does not resolve to canonical evidence",
            )
        if (
            submission.receipt_ref.sha256 != expected_receipt_digest
            or submission.receipt_ref.media_type != "application/json"
        ):
            raise HTTPException(
                status_code=409,
                detail="skill evaluation receipt_ref digest does not resolve to canonical receipt",
            )
        if evidence.candidate is None:
            if submission.candidate_ref is not None:
                raise HTTPException(
                    status_code=409,
                    detail="skill evaluation contains an unknown candidate reference",
                )
        elif submission.candidate_ref != evidence.candidate.content_ref:
            raise HTTPException(
                status_code=409,
                detail="skill evaluation contains an unknown candidate reference",
            )
        expected_gaps = {
            marker.gap_id: marker.evidence_ref for marker in evidence.tool_gaps
        }
        supplied_gaps = {
            reference.gap_id: reference.evidence_ref
            for reference in submission.tool_gap_refs
        }
        if supplied_gaps != expected_gaps or len(supplied_gaps) != len(
            submission.tool_gap_refs
        ):
            raise HTTPException(
                status_code=409,
                detail="skill evaluation contains unknown tool-gap evidence references",
            )

    @staticmethod
    def _canonical_model_sha256(model: BaseModel) -> str:
        content = json.dumps(
            model.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _assert_publication_qualification(
        publication: PublishedHermesSkill,
        submission: FactorySkillEvaluationSubmission,
        job: FactoryJob,
    ) -> None:
        evidence = submission.evidence
        GatewayStore._assert_factory_evaluation_job(evidence, job)
        GatewayStore._assert_evaluation_references(submission)
        reason = factory_evaluation_block_reason(
            job,
            GatewayStore._stored_factory_evaluation(submission),
        )
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
        candidate = evidence.candidate
        if (
            candidate is None
            or publication.candidate_id != candidate.candidate_id
            or publication.content_ref != candidate.content_ref
            or publication.content_sha256 != candidate.content_sha256
            or publication.skill_id != evidence.request.released_skill.skill_id
            or publication.version != evidence.request.released_skill.version + 1
            or publication.published_at <= evidence.occurred_at
        ):
            raise HTTPException(
                status_code=409,
                detail="publication does not match an accepted private skill candidate",
            )

    @staticmethod
    def _assert_factory_release_decision(
        job: FactoryJob,
        evaluation: StoredSkillEvaluation,
        evidence: tuple[E2ERunEvidence, ...],
        decision: FactoryReleaseDecision,
    ) -> None:
        expected = evaluate_factory_release(job, evidence, evaluation)
        if decision != expected:
            raise HTTPException(
                status_code=409,
                detail="Factory release decision does not match the Gateway-recomputed decision",
            )

    @staticmethod
    def _assert_factory_release_decision_recordable(
        projection: FactoryProjection,
    ) -> None:
        if projection.status is FactoryLifecycleStatus.READY_TO_USE:
            raise HTTPException(
                status_code=409,
                detail="Factory release decisions are sealed after capability promotion",
            )

    @staticmethod
    def _assert_evaluation_is_published(
        cursor: Any,
        evaluation: StoredSkillEvaluation,
    ) -> None:
        cursor.execute(
            "SELECT payload FROM factory_published_skills WHERE evaluation_id = %s",
            (str(evaluation.evidence.evidence_id),),
        )
        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=409,
                detail="capability promotion requires a Captain-published skill",
            )

    @staticmethod
    def _assert_lease_is_next_action(lease: FactoryLease, projection: FactoryProjection) -> None:
        action = next_action(projection)
        if (
            lease.role is FactoryRole.REAL_CASE_TESTER
            and action.kind is FactoryActionKind.DISPATCH_QUALITY_WARDEN
            and lease.workspace_ref.startswith(
                "workspace://business-benchmark-suite/"
            )
        ):
            return
        role_actions = {
            FactoryRole.AGENT_ARCHITECT: frozenset({FactoryActionKind.DISPATCH_AGENT_ARCHITECT}),
            FactoryRole.TOOL_INTEGRATOR: frozenset(
                {
                    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
                    FactoryActionKind.SUBMIT_FORGE_JOB,
                    FactoryActionKind.DISPATCH_BUILD_VALIDATOR,
                }
            ),
            FactoryRole.REAL_CASE_TESTER: frozenset(
                {
                    FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                    FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION,
                }
            ),
            FactoryRole.QUALITY_WARDEN: frozenset({FactoryActionKind.DISPATCH_QUALITY_WARDEN}),
        }
        if action.kind not in role_actions[lease.role]:
            raise HTTPException(status_code=409, detail="factory lease role is not the next authorized action")

    @staticmethod
    def _assert_evidence_lease(
        evidence: FactoryEvidenceBlock,
        lease: FactoryLease | None,
        *,
        runtime_retry_authorization: FactoryRuntimeRetryAuthorizationV1 | None = None,
    ) -> None:
        """Bind Hermes evidence to one previously persisted, active role lease."""

        if evidence.role is None:
            return
        if lease is None or evidence.lease_id != lease.lease_id:
            raise HTTPException(status_code=409, detail="missing matching active factory lease for evidence")
        if (
            lease.job_id != evidence.job_id
            or lease.correlation_id != evidence.correlation_id
            or lease.subject_version != evidence.subject_version
            or lease.attempt != evidence.attempt
            or lease.role is not evidence.role
        ):
            raise HTTPException(status_code=409, detail="factory evidence is outside its active lease")
        if lease.issued_at <= evidence.occurred_at < lease.expires_at:
            return
        authorization = runtime_retry_authorization
        if (
            evidence.role is not FactoryRole.TOOL_INTEGRATOR
            or evidence.phase is not FactoryPhase.TOOL_CANDIDATE_TESTED
            or authorization is None
            or authorization.producer != "captain"
            or authorization.status != "succeeded"
            or authorization.job_id != evidence.job_id
            or authorization.correlation_id != evidence.correlation_id
            or authorization.subject_version != evidence.subject_version
            or authorization.attempt != evidence.attempt
            or authorization.lease_id != evidence.lease_id
            or not authorization.issued_at
            <= evidence.occurred_at
            < authorization.expires_at
            or authorization.authorization_ref not in evidence.evidence_refs
            or authorization.authorization_ref.sha256
            != factory_runtime_retry_evidence_binding_sha256(
                factory_runtime_retry_evidence_binding(authorization)
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="factory evidence is outside its active lease",
            )

    def _runtime_grant_block(
        self,
        cursor: Any,
        grant: CapabilityGrant,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        return self._runtime_block_by_two_json_values(
            cursor,
            block_type="agent_runtime_grant",
            first_field="grant_id",
            first_value=grant.grant_id,
            second_field="command_id",
            second_value=str(grant.command_id),
            for_update=for_update,
        )

    def _runtime_result_block(
        self,
        cursor: Any,
        result: AgentRuntimeResult,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        return self._runtime_block_by_two_json_values(
            cursor,
            block_type="agent_runtime_result",
            first_field="event_id",
            first_value=str(result.event_id),
            second_field="command_id",
            second_value=str(result.command_id),
            for_update=for_update,
        )

    def _runtime_block_by_json_value(
        self,
        cursor: Any,
        *,
        block_type: str,
        field: str,
        value: str,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = %s
              AND JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
            ORDER BY `index` LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (block_type, f"$.{field}", value))
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _runtime_block_by_two_json_values(
        self,
        cursor: Any,
        *,
        block_type: str,
        first_field: str,
        first_value: str,
        second_field: str,
        second_value: str,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = %s
              AND (
                JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
                OR JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
              )
            ORDER BY `index` LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(
            sql,
            (
                block_type,
                f"$.{first_field}",
                first_value,
                f"$.{second_field}",
                second_value,
            ),
        )
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    @staticmethod
    def _assert_result_matches_command(
        result: AgentRuntimeResult,
        command: AgentRuntimeCommand,
    ) -> None:
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.subject_id != command.subject_id
            or result.subject_version != command.subject_version
            or result.operation is not command.payload.operation
        ):
            raise HTTPException(
                status_code=409,
                detail="runtime result does not match its command",
            )

    def release_projection(self, *, project_id: str, run_id: str) -> ReleaseProjection:
        return project_release(self.delivery_events(project_id=project_id, run_id=run_id))

    def record_release_decision(
        self,
        *,
        project_id: str,
        run_id: str,
        policy_version: str,
    ) -> tuple[DeliveryEventEnvelope, ReleaseReadiness]:
        """Persist the Captain-only release decision after a fail-closed audit."""

        events = self.delivery_events(project_id=project_id, run_id=run_id)
        readiness = evaluate_release_readiness(events)
        decision = "accepted" if readiness.ready else "rejected"
        for event in events:
            if (
                event.event_type == "release_decision"
                and event.payload.policy_version == policy_version
                and event.payload.decision == decision
            ):
                return event, readiness

        reasons = (
            ("three_complete_provider_backed_e2e_runs",)
            if readiness.ready
            else readiness.reasons
        )
        event = DeliveryEventEnvelope.model_validate(
            {
                "event_id": uuid5(
                    NAMESPACE_URL,
                    (
                        "captain-release-decision:"
                        f"{project_id}:{run_id}:{policy_version}:{decision}"
                    ),
                ),
                "event_type": "release_decision",
                "occurred_at": _utcnow(),
                "actor": "captain-gateway",
                "trace": {
                    "project_id": project_id,
                    "run_id": run_id,
                    "trace_id": f"release:{policy_version}",
                },
                "payload": {
                    "event_type": "release_decision",
                    "decision": decision,
                    "policy_version": policy_version,
                    "reasons": reasons,
                },
            }
        )
        return self.append_delivery_event(event).event, readiness

    def delivery_holdout_case(
        self,
        *,
        project_id: str,
        run_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        events = self.delivery_events(project_id=project_id, run_id=run_id)
        sealed_batches = {
            event.trace.batch_id
            for event in events
            if isinstance(event.payload, ArtifactBuiltPayload)
            and event.trace.batch_id is not None
        }
        if not sealed_batches:
            raise HTTPException(status_code=404, detail="holdout not released")

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                for batch_id in sealed_batches:
                    cursor.execute(
                        """
                        SELECT data FROM blocks
                        WHERE block_type = 'holdout'
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                        ORDER BY `index` DESC LIMIT 1
                        """,
                        (batch_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        continue
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    for case in data.get("cases", []):
                        if case.get("case_id") == case_id:
                            return dict(case)
        raise HTTPException(status_code=404, detail="holdout not found")

    def record_capability_n8n_work_batch(
        self,
        *,
        job: AgentFactoryJobV3,
        batch: WorkBatch,
    ) -> str:
        """Atomically persist the exact Factory n8n root and its job binding."""

        expected = f"n8n-{job.job_id.hex[:24]}"
        canonical = batch.model_dump(mode="json")
        if (
            batch.batch_id != expected
            or batch.target != "n8n"
            or batch.runtime != "n8n"
            or "n8n-builder" not in batch.capability_tags
            or f"factory-job-id:{job.job_id}" not in batch.constraints
            or not batch.acceptance_criteria
            or {item.assertion_id for item in batch.acceptance_criteria}
            != set(job.acceptance_assertion_ids)
        ):
            raise HTTPException(status_code=409, detail="Factory n8n WorkBatch binding mismatch")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_n8n_work_batches (
                        job_id CHAR(36) NOT NULL PRIMARY KEY,
                        batch_id VARCHAR(128) NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        block_index BIGINT NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT batch_id, payload, block_index FROM capability_n8n_work_batches "
                    "WHERE job_id = %s FOR UPDATE",
                    (str(job.job_id),),
                )
                registered = cursor.fetchone()
                existing_batch = self._batch_row(cursor, batch.batch_id, for_update=True)
                if registered is not None or existing_batch is not None:
                    stored_payload = (
                        None if registered is None else self._decode_json(registered["payload"])
                    )
                    if (
                        registered is not None
                        and existing_batch is not None
                        and registered["batch_id"] == batch.batch_id
                        and int(registered["block_index"]) == int(existing_batch["index"])
                        and stored_payload == canonical
                        and existing_batch["data"] == canonical
                    ):
                        return batch.batch_id
                    raise HTTPException(
                        status_code=409,
                        detail="Factory n8n WorkBatch persistence is partial or conflicting",
                    )
                block = self._new_block(
                    cursor,
                    index=self._next_index(cursor),
                    block_type="work_batch",
                    data=canonical,
                    status="pending",
                    parent_index=None,
                    metadata={
                        "factory_job_id": str(job.job_id),
                        "purpose": "capability_assembly_n8n",
                    },
                )
                try:
                    project_batch([block], batch.batch_id, now=_utcnow())
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                self._insert(cursor, block)
                cursor.execute(
                    "INSERT INTO capability_n8n_work_batches "
                    "(job_id, batch_id, payload, block_index) VALUES (%s, %s, %s, %s)",
                    (
                        str(job.job_id),
                        batch.batch_id,
                        json.dumps(canonical, sort_keys=True),
                        block["index"],
                    ),
                )
        return batch.batch_id

    def put_capability_private_holdouts(
        self, entries: tuple[tuple[str, str], ...]
    ) -> None:
        """Atomically bind each opaque holdout URI to exact private body bytes."""

        if (
            not entries
            or len({uri for uri, _ in entries}) != len(entries)
            or any(
                re.fullmatch(r"holdout://holdout-[0-9a-f]{12}", uri) is None
                or not isinstance(body, str)
                or not body
                for uri, body in entries
            )
        ):
            raise HTTPException(status_code=409, detail="private holdout binding is invalid")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_private_holdouts (
                        uri VARCHAR(128) NOT NULL PRIMARY KEY,
                        body_sha256 CHAR(64) NOT NULL,
                        body LONGTEXT NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                for uri, body in entries:
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    cursor.execute(
                        "SELECT body_sha256, body FROM capability_private_holdouts "
                        "WHERE uri = %s FOR UPDATE",
                        (uri,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        if existing["body_sha256"] == digest and existing["body"] == body:
                            continue
                        raise HTTPException(status_code=409, detail="private holdout binding conflicts")
                    cursor.execute(
                        "INSERT INTO capability_private_holdouts (uri, body_sha256, body) "
                        "VALUES (%s, %s, %s)",
                        (uri, digest, body),
                    )

    def capability_private_holdout(self, uri: str) -> str:
        if re.fullmatch(r"holdout://holdout-[0-9a-f]{12}", uri) is None:
            raise HTTPException(status_code=404, detail="private holdout not found")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT body_sha256, body FROM capability_private_holdouts WHERE uri = %s",
                    (uri,),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="private holdout not found")
        body = str(row["body"])
        if hashlib.sha256(body.encode()).hexdigest() != row["body_sha256"]:
            raise HTTPException(status_code=409, detail="private holdout readback mismatch")
        return body

    def record_generic_capability_promotion(
        self,
        *,
        job_id: UUID,
        suite_sha256: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        """Atomically gate and issue one generic benchmark promotion event."""

        if (
            len(suite_sha256) != 64
            or any(character not in "0123456789abcdef" for character in suite_sha256)
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() != timezone.utc.utcoffset(occurred_at)
        ):
            raise HTTPException(status_code=409, detail="generic promotion binding is invalid")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id",
                    value=str(job_id), for_update=True,
                )
                if job_block is None:
                    raise HTTPException(status_code=409, detail="generic promotion job is unavailable")
                job = parse_factory_job(job_block["data"])
                if type(job) is not AgentFactoryJobV3:
                    raise HTTPException(status_code=409, detail="generic promotion job is invalid")
                projection = self._factory_projection(cursor, job)
                factory_blocks = self._factory_blocks(
                    cursor, job_id, for_update=True
                )
                promotions = tuple(
                    block for block in factory_blocks
                    if block.phase is FactoryPhase.CAPABILITY_PROMOTED
                )
                if (
                    projection.status not in {
                        FactoryLifecycleStatus.RUNNING,
                        FactoryLifecycleStatus.READY_TO_USE,
                    }
                    or projection.phase not in {
                        FactoryPhase.QUALITY_REVIEWED,
                        FactoryPhase.CAPABILITY_PROMOTED,
                    }
                ):
                    raise HTTPException(status_code=409, detail="generic promotion is not admissible")
                cursor.execute(
                    "SELECT summary_sha256, correlation_id, payload "
                    "FROM capability_generic_benchmarks "
                    "WHERE job_id = %s AND suite_sha256 = %s FOR UPDATE",
                    (str(job_id), suite_sha256),
                )
                row = cursor.fetchone()
                summary = None if row is None else self._decode_json(row["payload"])
                aggregate = summary.get("aggregate") if isinstance(summary, dict) else None
                if (
                    row is None
                    or not isinstance(summary, dict)
                    or not isinstance(aggregate, dict)
                    or summary.get("schema") != "captain.capability-generic-benchmark-summary.v1"
                    or summary.get("job_id") != str(job_id)
                    or summary.get("correlation_id") != str(job.correlation_id)
                    or summary.get("suite_digest") != suite_sha256
                    or summary.get("passed") is not True
                    or aggregate.get("case_count") != 15
                ):
                    raise HTTPException(status_code=409, detail="generic benchmark promotion gate failed")
                summary_sha256 = str(row["summary_sha256"])
                event_id = uuid5(
                    NAMESPACE_URL,
                    f"captain:generic-capability-promotion:{job_id}:{suite_sha256}:{summary_sha256}",
                )
                event = self._generic_minibook_promotion_event(
                    event_id=event_id,
                    job=job,
                    occurred_at=occurred_at,
                    summary_sha256=summary_sha256,
                    aggregate=aggregate,
                )
                canonical = event.model_dump(mode="json", by_alias=True)
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_generic_promotions (
                        event_id CHAR(36) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL UNIQUE,
                        suite_sha256 CHAR(64) NOT NULL,
                        summary_sha256 CHAR(64) NOT NULL,
                        projection_event JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT suite_sha256, summary_sha256, projection_event "
                    "FROM capability_generic_promotions WHERE job_id = %s FOR UPDATE",
                    (str(job_id),),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if (
                        existing["suite_sha256"] == suite_sha256
                        and existing["summary_sha256"] == summary_sha256
                        and self._decode_json(existing["projection_event"]) == canonical
                        and projection.status is FactoryLifecycleStatus.READY_TO_USE
                        and projection.phase is FactoryPhase.CAPABILITY_PROMOTED
                        and len(promotions) == 1
                    ):
                        return canonical
                    raise HTTPException(status_code=409, detail="generic promotion conflicts")
                if (
                    projection.status is not FactoryLifecycleStatus.RUNNING
                    or projection.phase is not FactoryPhase.QUALITY_REVIEWED
                    or promotions
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="generic promotion is not atomically admissible",
                    )
                promotion = build_release_workflow_promotion(
                    FactoryJobProjection(
                        job=job,
                        blocks=factory_blocks,
                        leases=self._factory_leases(cursor, job_id),
                        projection=projection,
                    ),
                    occurred_at=occurred_at,
                )
                (
                    evaluation,
                    release_decision,
                    workflow_evaluation,
                    feedback,
                    benchmark_summary,
                ) = self._factory_block_context(
                    cursor,
                    job,
                    promotion,
                    for_update=True,
                )
                try:
                    promoted_projection = apply_block(
                        projection,
                        promotion,
                        evaluation=evaluation,
                        release_decision=release_decision,
                        workflow_evaluation=workflow_evaluation,
                        feedback=feedback,
                        benchmark_summary=benchmark_summary,
                    )
                except FactoryLifecycleError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if (
                    promoted_projection.status is not FactoryLifecycleStatus.READY_TO_USE
                    or promoted_projection.phase is not FactoryPhase.CAPABILITY_PROMOTED
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="generic promotion did not reach ready_to_use",
                    )
                promotion_index = self._next_index(cursor)
                promotion_block = self._new_block(
                    cursor,
                    index=promotion_index,
                    block_type="agent_factory_block",
                    data=promotion.model_dump(mode="json", by_alias=True),
                    status=promotion.status.value,
                    parent_index=job_block["index"],
                    metadata={
                        "schema": "captain.agent-factory-block.v1",
                        "phase": promotion.phase.value,
                    },
                )
                self._insert(cursor, promotion_block)
                cursor.execute(
                    "INSERT INTO capability_generic_promotions "
                    "(event_id, job_id, suite_sha256, summary_sha256, projection_event) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        str(event_id), str(job_id), suite_sha256, summary_sha256,
                        json.dumps(canonical, sort_keys=True),
                    ),
                )
                return canonical

    def generic_benchmark_quality_evidence(
        self, job_id: UUID, attempt: int,
    ) -> GenericBenchmarkQualityEvidenceV1 | None:
        """Read one passed 15-case generic suite as public Factory evidence."""

        if not isinstance(job_id, UUID) or not isinstance(attempt, int) or attempt < 1:
            raise HTTPException(status_code=409, detail="generic quality binding is invalid")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                job_block = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id",
                    value=str(job_id), for_update=False,
                )
                if job_block is None:
                    return None
                job = parse_factory_job(job_block["data"])
                cursor.execute(
                    "SELECT suite_sha256, correlation_id, summary_sha256, payload "
                    "FROM capability_generic_benchmarks WHERE job_id = %s",
                    (str(job_id),),
                )
                rows = tuple(cursor.fetchall())
        if len(rows) != 1 or type(job) is not AgentFactoryJobV3:
            return None
        row = rows[0]
        payload = self._decode_json(row["payload"])
        cases = payload.get("case_receipts") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema")
            != "captain.capability-generic-benchmark-summary.v1"
            or payload.get("job_id") != str(job.job_id)
            or payload.get("correlation_id") != str(job.correlation_id)
            or payload.get("suite_digest") != row["suite_sha256"]
            or payload.get("passed") is not True
            or not isinstance(cases, list)
            or len(cases) != 15
        ):
            raise HTTPException(status_code=409, detail="generic quality evidence is invalid")
        try:
            case_ids = tuple(str(item["case_id"]) for item in cases)
            case_digests = tuple(str(item["case_digest"]) for item in cases)
            categories = tuple(str(item["category"]) for item in cases)
            observed_assertions = tuple(dict.fromkeys(
                assertion
                for item in cases
                for assertion in item["assertion_ids"]
            ))
            evaluation_refs = tuple(
                ArtifactRef.model_validate(item[name])
                for item in cases
                for name in ("team_evaluation_ref", "baseline_evaluation_ref")
            )
            suite_ref = ArtifactRef.model_validate(payload["suite_ref"])
            summary_sha256 = str(row["summary_sha256"])
            summary_ref = ArtifactRef(
                uri=f"artifact://sha256/{summary_sha256}",
                sha256=summary_sha256,
                media_type="application/json",
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise HTTPException(status_code=409, detail="generic quality evidence is invalid") from None
        if set(observed_assertions) != set(job.acceptance_assertion_ids):
            raise HTTPException(
                status_code=409,
                detail="generic quality assertion authority is incomplete",
            )
        assertions = job.acceptance_assertion_ids
        try:
            return GenericBenchmarkQualityEvidenceV1(
                schema="captain.generic-benchmark-quality-evidence.v1",
                authority_id=UUID("fefc0e43-6955-4d50-aae2-b2ef5051e9af"),
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=attempt,
                suite_ref=suite_ref,
                summary_ref=summary_ref,
                case_ids=case_ids,
                case_digests=case_digests,
                categories=categories,
                acceptance_assertion_ids=assertions,
                evaluation_refs=evaluation_refs,
                passed=True,
            )
        except ValidationError:
            raise HTTPException(status_code=409, detail="generic quality evidence is invalid") from None

    @staticmethod
    def _generic_minibook_promotion_event(
        *, event_id: UUID, job: AgentFactoryJobV3, occurred_at: datetime,
        summary_sha256: str, aggregate: dict[str, Any],
    ):
        from agenten.delivery.minibook_events import MinibookProjectionEvent

        def ratio(left: object, right: object) -> int:
            denominator = Decimal(str(right))
            numerator = Decimal(str(left))
            if denominator == 0:
                return 10000 if numerator == 0 else 1000000
            return int((numerator * Decimal(10000)) / denominator)

        return MinibookProjectionEvent.model_validate({
            "schema": "captain.minibook-projection.v2",
            "event_id": event_id,
            "correlation_id": job.correlation_id,
            "causation_id": job.event_id,
            "occurred_at": occurred_at,
            "producer": "captain-gateway",
            "subject_id": generic_capability_subject_id(job.job_id),
            "subject_version": job.subject_version,
            "event_type": "capability.promoted",
            "payload": {
                "view": "validation",
                "template_id": "factory_capability_ready_to_use",
                "status_id": "ready_to_use",
                "actor_role_id": "captain_gateway",
                "benchmark_disposition": "passed",
                "benchmark_reason_codes": [],
                "candidate_correctness_bps": int(aggregate["team_quality_count"]) * 10000 // 15,
                "baseline_correctness_bps": int(aggregate["baseline_quality_count"]) * 10000 // 15,
                "candidate_completion_bps": int(aggregate["team_quality_count"]) * 10000 // 15,
                "baseline_completion_bps": int(aggregate["baseline_quality_count"]) * 10000 // 15,
                "cost_ratio_bps": ratio(aggregate["team_cost_usd"], aggregate["baseline_cost_usd"]),
                "latency_ratio_bps": ratio(aggregate["team_latency_ms"], aggregate["baseline_latency_ms"]),
                "unsafe_tool_uses": int(aggregate["unsafe_team_count"]),
                "mandatory_handoff_misses": int(aggregate["mandatory_handoff_misses"]),
                "benchmark_summary_digest": f"sha256:{summary_sha256}",
            },
        })

    def generic_capability_promotion(self, event_id: UUID) -> dict[str, Any] | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_generic_promotions (
                        event_id CHAR(36) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL UNIQUE,
                        suite_sha256 CHAR(64) NOT NULL,
                        summary_sha256 CHAR(64) NOT NULL,
                        projection_event JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT projection_event FROM capability_generic_promotions "
                    "WHERE event_id = %s",
                    (str(event_id),),
                )
                row = cursor.fetchone()
        return None if row is None else dict(self._decode_json(row["projection_event"]))

    def capability_n8n_work_batch(self, job_id: UUID) -> str | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_n8n_work_batches (
                        job_id CHAR(36) NOT NULL PRIMARY KEY,
                        batch_id VARCHAR(128) NOT NULL UNIQUE,
                        payload JSON NOT NULL,
                        block_index BIGINT NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT batch_id, payload, block_index FROM capability_n8n_work_batches "
                    "WHERE job_id = %s",
                    (str(job_id),),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                batch = self._batch_row(cursor, str(row["batch_id"]))
        if (
            batch is None
            or int(batch["index"]) != int(row["block_index"])
            or batch["data"] != self._decode_json(row["payload"])
        ):
            raise HTTPException(status_code=409, detail="Factory n8n WorkBatch readback mismatch")
        return str(row["batch_id"])

    def record_capability_tool_authorization(
        self, *, job_id: str, case_digest: str, tool_key: str,
        operation: str, trace_id: str, run_id: str,
        model_policy_digest: str, batch_id: str, command_id: str,
        candidate_sha256: str, execution_sha256: str, call_id: str,
        lease_id: str, call_ref: ArtifactRef, lease_ref: ArtifactRef,
        scoped_token_sha256: str,
    ) -> None:
        """Persist the exact call and lease before the authorized tool effect."""

        payload = {
            "job_id": job_id, "case_digest": case_digest,
            "tool_key": tool_key, "operation": operation,
            "trace_id": trace_id, "run_id": run_id,
            "model_policy_digest": model_policy_digest,
            "batch_id": batch_id, "command_id": command_id,
            "candidate_sha256": candidate_sha256,
            "execution_sha256": execution_sha256,
            "call_id": call_id, "lease_id": lease_id,
            "scoped_token_sha256": scoped_token_sha256,
            "call_ref": call_ref.model_dump(mode="json"),
            "lease_ref": lease_ref.model_dump(mode="json"),
        }
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_authorizations (
                        call_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        completed_receipt_sha256 CHAR(64) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT job_id, correlation_id, case_digest, trace_id, run_id, "
                    "command_id, declared_tool_keys FROM capability_benchmark_work_batches "
                    "WHERE batch_id = %s FOR UPDATE", (batch_id,),
                )
                binding = cursor.fetchone()
                if (
                    binding is None
                    or str(binding["job_id"]) != job_id
                    or str(binding["case_digest"]) != case_digest
                    or str(binding["trace_id"]) != trace_id
                    or str(binding["run_id"]) != run_id
                    or str(binding["command_id"]) != command_id
                    or tool_key not in self._decode_json(binding["declared_tool_keys"])
                ):
                    raise HTTPException(status_code=409, detail="tool authorization is unavailable")
                cursor.execute(
                    "SELECT payload FROM capability_tool_authorizations "
                    "WHERE call_sha256 = %s FOR UPDATE", (call_ref.sha256,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        "INSERT INTO capability_tool_authorizations "
                        "(call_sha256, payload) VALUES (%s, %s)",
                        (call_ref.sha256, self._encode_json(payload)),
                    )
                elif self._decode_json(existing["payload"]) != payload:
                    raise HTTPException(status_code=409, detail="tool authorization conflicts")

    def record_capability_tool_evidence(
        self,
        *,
        job_id: UUID,
        case_digest: str,
        tool_key: str,
        operation: str,
        trace_id: UUID,
        run_id: UUID,
        model_policy_digest: str,
        call_ref: ArtifactRef,
        lease_ref: ArtifactRef,
        receipt_ref: ArtifactRef,
        batch_id: str,
        execution_result_id: UUID,
        result_ref_sha256: str,
    ) -> None:
        """Admit only a WorkBatch- and Runtime-result-bound tool receipt chain."""

        refs = (call_ref, lease_ref, receipt_ref)
        if (
            len(case_digest) != 64
            or any(character not in "0123456789abcdef" for character in case_digest)
            or not tool_key
            or not operation
            or len(model_policy_digest) != 64
            or any(character not in "0123456789abcdef" for character in model_policy_digest)
            or len({item.sha256 for item in refs}) != 3
            or any(
                item.media_type != "application/json"
                or item.uri != f"artifact://sha256/{item.sha256}"
                for item in refs
            )
            or len(result_ref_sha256) != 64
            or any(character not in "0123456789abcdef" for character in result_ref_sha256)
        ):
            raise HTTPException(status_code=409, detail="capability tool evidence is invalid")
        payload = {
            "job_id": str(job_id),
            "case_digest": case_digest,
            "tool_key": tool_key,
            "operation": operation,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": model_policy_digest,
            "call_ref": call_ref.model_dump(mode="json"),
            "lease_ref": lease_ref.model_dump(mode="json"),
            "receipt_ref": receipt_ref.model_dump(mode="json"),
            "batch_id": batch_id,
            "execution_result_id": str(execution_result_id),
            "result_ref_sha256": result_ref_sha256,
        }
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_evidence_registry (
                        receipt_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_authorizations (
                        call_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        completed_receipt_sha256 CHAR(64) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT job_id, correlation_id, case_digest, trace_id, run_id, "
                    "command_id, declared_tool_keys FROM capability_benchmark_work_batches "
                    "WHERE batch_id = %s FOR UPDATE",
                    (batch_id,),
                )
                batch_binding = cursor.fetchone()
                cursor.execute(
                    "SELECT payload, completed_receipt_sha256 FROM "
                    "capability_tool_authorizations WHERE call_sha256 = %s FOR UPDATE",
                    (call_ref.sha256,),
                )
                authorization = cursor.fetchone()
                if authorization is not None and "payload" in authorization:
                    authorization_payload = self._decode_json(authorization["payload"])
                    if (
                        authorization_payload.get("job_id") != str(job_id)
                        or authorization_payload.get("case_digest") != case_digest
                        or authorization_payload.get("tool_key") != tool_key
                        or authorization_payload.get("operation") != operation
                        or authorization_payload.get("trace_id") != str(trace_id)
                        or authorization_payload.get("run_id") != str(run_id)
                        or authorization_payload.get("batch_id") != batch_id
                        or authorization_payload.get("call_ref")
                        != call_ref.model_dump(mode="json")
                        or authorization_payload.get("lease_ref")
                        != lease_ref.model_dump(mode="json")
                        or authorization["completed_receipt_sha256"] not in {
                            None, receipt_ref.sha256,
                        }
                    ):
                        raise HTTPException(status_code=409, detail="tool authorization changed")
                job = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id",
                    value=str(job_id), for_update=True,
                )
                if (
                    batch_binding is None or job is None
                    or str(batch_binding["job_id"]) != str(job_id)
                    or str(batch_binding["correlation_id"])
                    != str(job["data"].get("correlation_id", ""))
                    or str(batch_binding["case_digest"]) != case_digest
                    or str(batch_binding["trace_id"]) != str(trace_id)
                    or str(batch_binding["run_id"]) != str(run_id)
                    or tool_key not in self._decode_json(
                        batch_binding["declared_tool_keys"]
                    )
                ):
                    raise HTTPException(status_code=409, detail="tool WorkBatch is unavailable")
                correlation_id = str(job["data"].get("correlation_id", ""))
                result = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_result",
                    field="event_id",
                    value=str(execution_result_id),
                    for_update=True,
                )
                result_data = {} if result is None else result["data"]
                command_id = result_data.get("command_id")
                command = self._runtime_block_by_json_value(
                    cursor, block_type="agent_runtime_command", field="event_id",
                    value=str(command_id), for_update=True,
                ) if command_id else None
                command_data = {} if command is None else command["data"]
                command_payload = command_data.get("payload", {})
                result_refs = (
                    *result_data.get("artifact_refs", []),
                    *result_data.get("evidence_refs", []),
                )
                if (
                    result is None or result_data.get("status") != "succeeded"
                    or result_data.get("correlation_id") != correlation_id
                    or result_data.get("operation")
                    != RuntimeOperation.AUTOGEN_BENCHMARK.value
                    or command is None
                    or command_data.get("correlation_id") != correlation_id
                    or str(command_data.get("event_id"))
                    != str(batch_binding["command_id"])
                    or command_payload.get("batch_id") != batch_id
                    or command_payload.get("operation")
                    != RuntimeOperation.AUTOGEN_BENCHMARK.value
                    or not any(ref.get("sha256") == result_ref_sha256 for ref in result_refs)
                ):
                    raise HTTPException(status_code=409, detail="tool execution result is unavailable")
                payload["correlation_id"] = correlation_id
                cursor.execute(
                    "SELECT payload FROM capability_tool_evidence_registry "
                    "WHERE receipt_sha256 = %s FOR UPDATE",
                    (receipt_ref.sha256,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if self._decode_json(existing["payload"]) == payload:
                        return
                    raise HTTPException(status_code=409, detail="tool evidence conflicts")
                cursor.execute(
                    "INSERT INTO capability_tool_evidence_registry "
                    "(receipt_sha256, payload) VALUES (%s, %s)",
                    (receipt_ref.sha256, json.dumps(payload, sort_keys=True)),
                )
                if authorization is not None and "payload" in authorization:
                    cursor.execute(
                        "UPDATE capability_tool_authorizations SET "
                        "completed_receipt_sha256 = %s WHERE call_sha256 = %s",
                        (receipt_ref.sha256, call_ref.sha256),
                    )

    def capability_tool_authorization(self, call_sha256: str) -> dict[str, Any] | None:
        """Read the exact pre-effect call/lease authority."""

        if len(call_sha256) != 64:
            return None
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_authorizations (
                        call_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        completed_receipt_sha256 CHAR(64) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT payload FROM capability_tool_authorizations "
                    "WHERE call_sha256 = %s", (call_sha256,),
                )
                row = cursor.fetchone()
        return None if row is None else dict(self._decode_json(row["payload"]))

    def record_capability_tool_effect(
        self, *, call_ref: ArtifactRef, lease_ref: ArtifactRef,
        result_ref: ArtifactRef, receipt_ref: ArtifactRef,
        arguments_sha256: str, scoped_token_sha256: str,
    ) -> None:
        """Persist terminal host readback before it can be returned to AutoGen."""

        payload = {
            "call_ref": call_ref.model_dump(mode="json"),
            "lease_ref": lease_ref.model_dump(mode="json"),
            "result_ref": result_ref.model_dump(mode="json"),
            "receipt_ref": receipt_ref.model_dump(mode="json"),
            "arguments_sha256": arguments_sha256,
            "scoped_token_sha256": scoped_token_sha256,
        }
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_effects (
                        call_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT payload FROM capability_tool_authorizations "
                    "WHERE call_sha256 = %s FOR UPDATE", (call_ref.sha256,),
                )
                row = cursor.fetchone()
                authority = None if row is None else self._decode_json(row["payload"])
                if (
                    not isinstance(authority, dict)
                    or authority.get("call_ref") != payload["call_ref"]
                    or authority.get("lease_ref") != payload["lease_ref"]
                    or authority.get("scoped_token_sha256") != scoped_token_sha256
                ):
                    raise HTTPException(status_code=409, detail="tool effect authority changed")
                cursor.execute(
                    "SELECT payload FROM capability_tool_effects "
                    "WHERE call_sha256 = %s FOR UPDATE", (call_ref.sha256,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        "INSERT INTO capability_tool_effects (call_sha256, payload) "
                        "VALUES (%s, %s)",
                        (call_ref.sha256, self._encode_json(payload)),
                    )
                elif self._decode_json(existing["payload"]) != payload:
                    raise HTTPException(status_code=409, detail="tool effect conflicts")

    def capability_tool_effect(self, call_sha256: str) -> dict[str, Any] | None:
        """Read a terminal tool result/receipt for retry reconciliation."""

        if len(call_sha256) != 64:
            return None
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_effects (
                        call_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT payload FROM capability_tool_effects "
                    "WHERE call_sha256 = %s", (call_sha256,),
                )
                row = cursor.fetchone()
        return None if row is None else dict(self._decode_json(row["payload"]))

    def record_capability_benchmark_provider_settlement(
        self, payload: Mapping[str, object],
    ) -> None:
        """Admit one provider-receipt-role benchmark call settlement."""

        required = {
            "schema", "settlement_id", "provider_call_id", "job_id",
            "correlation_id", "case_digest", "trace_id", "run_id",
            "command_id", "provider_result_id", "provider_session_id",
            "provider", "model", "input_units", "cached_input_units",
            "output_units", "actual_cost_usd", "provider_proxy_url",
            "provider_policy_sha256", "provider_price_card_sha256",
            "provider_context_sha256", "provider_proxy_executable_sha256",
            "provider_proxy_code_sha256", "provider_paid_command_sha256",
            "provider_hard_ceiling_enforced",
        }
        try:
            canonical = dict(payload)
            cost = Decimal(str(canonical["actual_cost_usd"]))
            if (
                set(canonical) != required
                or canonical["schema"]
                != "captain.capability-benchmark-provider-settlement.v1"
                or canonical["provider"] != "openai"
                or canonical["provider_hard_ceiling_enforced"] is not True
                or not cost.is_finite() or cost < 0
                or int(canonical["input_units"]) < 0
                or int(canonical["cached_input_units"]) < 0
                or int(canonical["output_units"]) < 0
                or int(canonical["cached_input_units"]) > int(canonical["input_units"])
            ):
                raise ValueError("shape")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=409, detail="benchmark provider settlement is invalid"
            ) from None
        binding = (
            str(canonical["provider_proxy_url"]).removesuffix("/v1"),
            canonical["provider_policy_sha256"],
            canonical["provider_price_card_sha256"],
            canonical["provider_context_sha256"],
            canonical["provider_proxy_executable_sha256"],
            canonical["provider_proxy_code_sha256"],
            canonical["provider_paid_command_sha256"], True,
        )
        if binding not in self._runtime_provider_bundle_bindings:
            raise HTTPException(
                status_code=409, detail="benchmark provider authority changed"
            )
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS capability_benchmark_provider_settlements (
                    settlement_id CHAR(36) NOT NULL PRIMARY KEY,
                    provider_call_id VARCHAR(128) NOT NULL UNIQUE,
                    command_id CHAR(36) NOT NULL,
                    job_id CHAR(36) NOT NULL,
                    payload JSON NOT NULL,
                    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB"""
                )
                command = self._runtime_block_by_json_value(
                    cursor, block_type="agent_runtime_command", field="event_id",
                    value=str(canonical["command_id"]), for_update=True,
                )
                cursor.execute(
                    "SELECT job_id, correlation_id, case_digest, trace_id, run_id, "
                    "command_id FROM capability_benchmark_work_batches "
                    "WHERE command_id = %s FOR UPDATE", (canonical["command_id"],),
                )
                batch = cursor.fetchone()
                job = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id",
                    value=str(canonical["job_id"]), for_update=True,
                )
                command_data = {} if command is None else command["data"]
                if (
                    command is None or batch is None or job is None
                    or command_data.get("payload", {}).get("operation")
                    != RuntimeOperation.AUTOGEN_BENCHMARK.value
                    or str(batch["job_id"]) != str(canonical["job_id"])
                    or str(batch["correlation_id"]) != str(canonical["correlation_id"])
                    or str(batch["case_digest"]) != str(canonical["case_digest"])
                    or str(batch["trace_id"]) != str(canonical["trace_id"])
                    or str(batch["run_id"]) != str(canonical["run_id"])
                    or command_data.get("correlation_id") != canonical["correlation_id"]
                    or canonical["model"] not in job["data"]["execution_policy"]["allowed_models"]
                ):
                    raise HTTPException(
                        status_code=409, detail="benchmark provider settlement binding is invalid"
                    )
                cursor.execute(
                    "SELECT payload FROM capability_benchmark_provider_settlements "
                    "WHERE settlement_id = %s OR provider_call_id = %s FOR UPDATE",
                    (canonical["settlement_id"], canonical["provider_call_id"]),
                )
                existing = cursor.fetchone()
                encoded = self._encode_json(canonical)
                if existing is not None:
                    if self._decode_json(existing["payload"]) == canonical:
                        return
                    raise HTTPException(
                        status_code=409, detail="benchmark provider settlement changed"
                    )
                cursor.execute(
                    "SELECT payload FROM capability_benchmark_provider_settlements "
                    "WHERE job_id = %s FOR UPDATE", (canonical["job_id"],),
                )
                total = sum(
                    (Decimal(str(self._decode_json(row["payload"])["actual_cost_usd"]))
                     for row in cursor.fetchall()), start=cost,
                )
                maximum = Decimal(str(job["data"]["execution_policy"]["max_cost_usd"]))
                if total > maximum:
                    raise HTTPException(
                        status_code=409, detail="benchmark provider settlement exceeds Factory budget"
                    )
                cursor.execute(
                    "INSERT INTO capability_benchmark_provider_settlements "
                    "(settlement_id, provider_call_id, command_id, job_id, payload) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (canonical["settlement_id"], canonical["provider_call_id"],
                     canonical["command_id"], canonical["job_id"], encoded),
                )

    def capability_benchmark_provider_settlements(
        self, command_id: UUID,
    ) -> tuple[dict[str, Any], ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS capability_benchmark_provider_settlements (
                    settlement_id CHAR(36) NOT NULL PRIMARY KEY,
                    provider_call_id VARCHAR(128) NOT NULL UNIQUE,
                    command_id CHAR(36) NOT NULL,
                    job_id CHAR(36) NOT NULL,
                    payload JSON NOT NULL,
                    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB"""
                )
                cursor.execute(
                    "SELECT payload FROM capability_benchmark_provider_settlements "
                    "WHERE command_id = %s ORDER BY created_at, settlement_id",
                    (str(command_id),),
                )
                rows = cursor.fetchall()
        return tuple(dict(self._decode_json(row["payload"])) for row in rows)

    def record_capability_benchmark_work_batch(
        self, *, job_id: UUID, correlation_id: UUID, batch_id: str,
        case_digest: str, trace_id: UUID, run_id: UUID, command_id: UUID,
        declared_tool_keys: tuple[str, ...], tool_authority_sha256: str,
    ) -> None:
        """Persist the exact execution-only benchmark authority before Runtime."""

        if (
            not batch_id or len(case_digest) != 64
            or not declared_tool_keys
            or len(set(declared_tool_keys)) != len(declared_tool_keys)
            or hashlib.sha256(json.dumps(
                list(declared_tool_keys), sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest() != tool_authority_sha256
        ):
            raise HTTPException(status_code=409, detail="benchmark WorkBatch is invalid")
        payload = {
            "job_id": str(job_id), "correlation_id": str(correlation_id),
            "batch_id": batch_id, "case_digest": case_digest,
            "trace_id": str(trace_id), "run_id": str(run_id),
            "command_id": str(command_id),
            "declared_tool_keys": list(declared_tool_keys),
            "tool_authority_sha256": tool_authority_sha256,
        }
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_benchmark_work_batches (
                        batch_id VARCHAR(96) NOT NULL PRIMARY KEY,
                        job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        case_digest CHAR(64) NOT NULL,
                        trace_id CHAR(36) NOT NULL,
                        run_id CHAR(36) NOT NULL,
                        command_id CHAR(36) NOT NULL,
                        declared_tool_keys JSON NOT NULL,
                        tool_authority_sha256 CHAR(64) NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        UNIQUE KEY capability_benchmark_command (command_id)
                    ) ENGINE=InnoDB
                    """
                )
                job = self._runtime_block_by_json_value(
                    cursor, block_type="agent_factory_job", field="job_id",
                    value=str(job_id), for_update=True,
                )
                if (
                    job is None
                    or str(job["data"].get("correlation_id")) != str(correlation_id)
                ):
                    raise HTTPException(
                        status_code=409, detail="benchmark Factory job is unavailable"
                    )
                cursor.execute(
                    "SELECT payload FROM capability_benchmark_work_batches "
                    "WHERE batch_id = %s FOR UPDATE", (batch_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if self._decode_json(existing["payload"]) == payload:
                        return
                    raise HTTPException(
                        status_code=409, detail="benchmark WorkBatch conflicts"
                    )
                cursor.execute(
                    "INSERT INTO capability_benchmark_work_batches "
                    "(batch_id, job_id, correlation_id, case_digest, trace_id, run_id, "
                    "command_id, declared_tool_keys, tool_authority_sha256, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        batch_id, str(job_id), str(correlation_id), case_digest,
                        str(trace_id), str(run_id), str(command_id),
                        json.dumps(list(declared_tool_keys)), tool_authority_sha256,
                        json.dumps(payload, sort_keys=True),
                    ),
                )

    def capability_tool_evidence(self, receipt_sha256: str) -> dict[str, Any] | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_tool_evidence_registry (
                        receipt_sha256 CHAR(64) NOT NULL PRIMARY KEY,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    "SELECT payload FROM capability_tool_evidence_registry "
                    "WHERE receipt_sha256 = %s",
                    (receipt_sha256,),
                )
                row = cursor.fetchone()
        return None if row is None else dict(self._decode_json(row["payload"]))

    def _batch_row(
        self,
        cursor: Any,
        batch_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = 'work_batch'
              AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
            ORDER BY `index` DESC LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (batch_id,))
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _row_by_index(self, cursor: Any, index: int) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE `index` = %s
            """,
            (index,),
        )
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _child_rows(
        self,
        cursor: Any,
        parent_index: int,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE parent_index = %s
            ORDER BY `index`
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (parent_index,))
        return [self.storage._decode_row(row) for row in cursor.fetchall()]

    def _batch_context(
        self,
        cursor: Any,
        batch_id: str,
        *,
        for_update: bool = False,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], BatchProjection]:
        parent = self._batch_row(cursor, batch_id, for_update=for_update)
        if parent is None:
            raise HTTPException(status_code=404, detail="batch not found")
        children = self._child_rows(cursor, parent["index"], for_update=for_update)
        projection = project_batch([parent, *children], batch_id, now=now)
        return parent, children, projection

    @staticmethod
    def _next_index(cursor: Any) -> int:
        cursor.execute("SELECT next_block_index FROM ledger_state WHERE id = 1 FOR UPDATE")
        index = int(cursor.fetchone()["next_block_index"])
        cursor.execute("UPDATE ledger_state SET next_block_index = next_block_index + 1 WHERE id = 1")
        return index

    @staticmethod
    def _insert(cursor: Any, block: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO blocks
                (`index`, parent_index, block_type, data, status, children,
                 metadata, hash, previous_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                block["index"],
                block["parent_index"],
                block["block_type"],
                json.dumps(block["data"], sort_keys=True),
                block["status"],
                json.dumps(block["children"]),
                json.dumps(block["metadata"], sort_keys=True),
                block["hash"],
                block["previous_hash"],
            ),
        )

    def _new_block(
        self,
        cursor: Any,
        *,
        index: int,
        block_type: str,
        data: dict[str, Any],
        status: str,
        parent_index: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cursor.execute("SELECT hash FROM blocks ORDER BY `index` DESC LIMIT 1 FOR UPDATE")
        previous = cursor.fetchone()
        return Block(
            index=index,
            block_type=block_type,
            data=data,
            status=status,
            previous_hash=previous["hash"] if previous else "0",
            parent_index=parent_index,
            metadata=metadata,
        ).to_dict()

    @staticmethod
    def _assert_live_claim(
        projection: BatchProjection,
        token: str | None,
        *,
        now: datetime,
    ) -> None:
        presented_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        expires = projection.claim_expires_at
        if (
            projection.status != "claimed"
            or not token
            or projection.claim_token_sha256 is None
            or not secrets.compare_digest(projection.claim_token_sha256, presented_hash)
            or expires is None
            or expires <= now
        ):
            raise HTTPException(status_code=409, detail="invalid or expired claim token")

    @staticmethod
    def _validate_candidate(
        parent: dict[str, Any],
        children: list[dict[str, Any]],
        block: dict[str, Any],
        batch_id: str,
        *,
        now: datetime,
    ) -> None:
        try:
            project_batch([parent, *children, block], batch_id, now=now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _retry_write(operation: Callable[[], WriteResult]) -> WriteResult:
        for attempt in range(TRANSACTION_ATTEMPTS):
            try:
                return operation()
            except OperationalError as exc:
                error_code = exc.args[0] if exc.args else None
                if (
                    error_code not in TRANSIENT_TRANSACTION_ERRORS
                    or attempt == TRANSACTION_ATTEMPTS - 1
                ):
                    raise
                time.sleep(TRANSACTION_RETRY_DELAYS_SECONDS[attempt])
        raise RuntimeError("unreachable transaction retry state")

    def append(self, request: BlockWrite, claim_token: str | None) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._append_once(request, claim_token))
        except _IdempotentReplay as replay:
            return replay.block

    @staticmethod
    def _has_identical_canonical_data(
        existing: dict[str, Any],
        data: dict[str, Any],
    ) -> bool:
        return existing["data"] == data

    def _append_once(self, request: BlockWrite, claim_token: str | None) -> dict[str, Any]:
        block_type = request.block_type
        if block_type in GATEWAY_OWNED_EVENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"{block_type} must use its dedicated gateway route",
            )
        data = dict(request.data)
        try:
            if block_type == "work_batch":
                data = WorkBatch.model_validate(data).model_dump(mode="json")
            elif block_type == "holdout":
                data = HoldoutSuite.model_validate(data).model_dump(mode="json")
            elif block_type == "codex_process":
                data = CodexProcessEvent.model_validate(data).model_dump(mode="json")
            elif block_type == "reasoning_slice":
                data = ReasoningSliceEvent.model_validate(data).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise HTTPException(status_code=422, detail="data.batch_id is required")

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent_index = request.parent_index
                parent: dict[str, Any] | None = None
                children: list[dict[str, Any]] = []
                now = _utcnow()

                if block_type not in CAPTAIN_BLOCK_TYPES:
                    parent, children, projection = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=now,
                    )
                    self._assert_live_claim(projection, claim_token, now=now)
                    if parent_index is None:
                        parent_index = parent["index"]
                    elif parent_index != parent["index"]:
                        raise HTTPException(status_code=409, detail="parent belongs to another batch")

                if block_type == "work_batch":
                    if parent_index is not None:
                        raise HTTPException(status_code=422, detail="work_batch must be a root block")
                    existing_batch = self._batch_row(cursor, batch_id, for_update=True)
                    if existing_batch is not None and self._has_identical_canonical_data(
                        existing_batch,
                        data,
                    ):
                        raise _IdempotentReplay(existing_batch)
                    if existing_batch is not None:
                        raise HTTPException(status_code=409, detail="batch_id already exists")

                if block_type == "holdout" and parent_index is None:
                    raise HTTPException(status_code=422, detail="holdout requires its work_batch parent")

                if block_type == "holdout":
                    parent, children, _ = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=now,
                    )
                    if parent_index != parent["index"]:
                        raise HTTPException(status_code=409, detail="holdout parent must be its work_batch")
                    existing_holdout = next(
                        (child for child in children if child["block_type"] == "holdout"),
                        None,
                    )
                    if existing_holdout is not None and self._has_identical_canonical_data(
                        existing_holdout,
                        data,
                    ):
                        raise _IdempotentReplay(existing_holdout)
                    if existing_holdout is not None:
                        raise HTTPException(status_code=409, detail="holdout suite already exists")
                elif parent_index is not None and block_type in CAPTAIN_BLOCK_TYPES - {"work_batch"}:
                    referenced_parent = self._row_by_index(cursor, parent_index)
                    if referenced_parent is None:
                        raise HTTPException(status_code=404, detail="parent block not found")
                    if referenced_parent["data"].get("batch_id") != batch_id:
                        raise HTTPException(status_code=409, detail="parent belongs to another batch")

                if block_type == "batch_done":
                    try:
                        done = BatchDoneEvent.model_validate(data)
                    except ValidationError as exc:
                        raise HTTPException(status_code=422, detail=exc.errors()) from exc
                    if request.status != done.outcome:
                        raise HTTPException(status_code=422, detail="batch_done status must match outcome")

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type=block_type,
                    data=data,
                    status=request.status,
                    parent_index=parent_index,
                    metadata=dict(request.metadata),
                )
                if block_type == "work_batch":
                    try:
                        project_batch([block], batch_id, now=now)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                elif parent is not None:
                    self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
                if block_type == "batch_done" and data["outcome"] == "succeeded":
                    self._upsert_capability(cursor, block)
        return block

    def recover(self, request: RecoveryDecisionEvent) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._recover_once(request))
        except _IdempotentReplay as replay:
            return replay.block

    def _recover_once(self, request: RecoveryDecisionEvent) -> dict[str, Any]:
        data = request.model_dump(mode="json")
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, children, projection = self._batch_context(
                    cursor,
                    request.batch_id,
                    for_update=True,
                    now=now,
                )
                existing = next(
                    (
                        child
                        for child in children
                        if child["block_type"] == "recovery_decision"
                        and child["data"].get("iteration") == request.iteration
                    ),
                    None,
                )
                if existing is not None and self._has_identical_canonical_data(existing, data):
                    raise _IdempotentReplay(existing)
                if existing is not None:
                    raise HTTPException(status_code=409, detail="recovery decision already exists")
                if (
                    projection.status != "pending"
                    or projection.claim_iteration != request.iteration
                    or projection.claim_expires_at is None
                    or projection.claim_expires_at > now
                ):
                    raise HTTPException(status_code=409, detail="claim is not expired")
                if self._active_codex_sessions(cursor, request.batch_id, request.iteration):
                    raise HTTPException(
                        status_code=409,
                        detail="active Codex session requires terminal evidence",
                    )
                block = self._new_block(
                    cursor,
                    index=self._next_index(cursor),
                    block_type="recovery_decision",
                    data=data,
                    status=request.decision,
                    parent_index=parent["index"],
                )
                self._validate_candidate(
                    parent,
                    children,
                    block,
                    request.batch_id,
                    now=now,
                )
                self._insert(cursor, block)
        return block

    @staticmethod
    def _active_codex_sessions(
        cursor: Any,
        batch_id: str,
        iteration: int,
    ) -> set[tuple[str, str, str]]:
        """Return sessions that Captain must terminalize before requeueing."""

        cursor.execute(
            """
            SELECT data FROM blocks
            WHERE block_type = 'delivery_event'
              AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.batch_id')) = %s
            ORDER BY `index`
            FOR UPDATE
            """,
            (batch_id,),
        )
        started: set[tuple[str, str, str]] = set()
        finished: set[tuple[str, str, str]] = set()
        for row in cursor.fetchall():
            event = row["data"]
            if isinstance(event, str):
                event = json.loads(event)
            trace = event.get("trace")
            payload = event.get("payload")
            if not isinstance(trace, dict) or not isinstance(payload, dict):
                continue
            project_id = trace.get("project_id")
            run_id = trace.get("run_id")
            session_id = payload.get("session_id")
            if not all(
                isinstance(value, str) and value
                for value in (project_id, run_id, session_id)
            ):
                continue
            identity = (project_id, run_id, session_id)
            if (
                event.get("event_type") == "codex_session_started"
                and payload.get("iteration") == iteration
            ):
                started.add(identity)
            elif event.get("event_type") == "codex_session_finished":
                finished.add(identity)
        return started - finished

    def batch_projection(self, batch_id: str) -> BatchProjection:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                _, _, projection = self._batch_context(cursor, batch_id, now=now)
        return projection

    def active_codex_sessions(self, batch_id: str) -> tuple[ActiveCodexSession, ...]:
        """Return only non-terminal Codex traces for the current batch iteration."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                _, _, projection = self._batch_context(cursor, batch_id)
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'delivery_event'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.batch_id')) = %s
                    ORDER BY `index`
                    """,
                    (batch_id,),
                )
                rows = cursor.fetchall()
        started: dict[str, ActiveCodexSession] = {}
        finished: set[str] = set()
        for row in rows:
            raw = row["data"]
            event = DeliveryEventEnvelope.model_validate(
                json.loads(raw) if isinstance(raw, str) else raw
            )
            payload = event.payload
            if isinstance(payload, CodexSessionStartedPayload):
                trace = event.trace
                if (
                    payload.iteration == projection.claim_iteration
                    and trace.worker_id is not None
                    and trace.claim_id is not None
                    and trace.fencing_token is not None
                    and trace.session_id is not None
                ):
                    started.setdefault(
                        payload.session_id,
                        ActiveCodexSession(
                            project_id=trace.project_id,
                            run_id=trace.run_id,
                            trace_id=trace.trace_id,
                            batch_id=batch_id,
                            worker_id=trace.worker_id,
                            claim_id=trace.claim_id,
                            fencing_token=trace.fencing_token,
                            session_id=payload.session_id,
                            iteration=payload.iteration,
                            process_ref=payload.process_ref,
                            started_at=payload.started_at,
                        ),
                    )
            elif isinstance(payload, CodexSessionFinishedPayload):
                finished.add(payload.session_id)
        return tuple(
            started[session_id]
            for session_id in sorted(set(started) - finished)
        )

    def review(self, request: ReviewDecisionEvent) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._review_once(request))
        except _IdempotentReplay as replay:
            return replay.block

    def _review_once(self, request: ReviewDecisionEvent) -> dict[str, Any]:
        data = request.model_dump(mode="json")
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, children, projection = self._batch_context(
                    cursor, request.batch_id, for_update=True, now=now
                )
                existing = next(
                    (
                        child
                        for child in children
                        if child["block_type"] == "review_decision"
                        and child["data"].get("review_id") == request.review_id
                    ),
                    None,
                )
                if existing is not None and self._has_identical_canonical_data(existing, data):
                    raise _IdempotentReplay(existing)
                if existing is not None:
                    raise HTTPException(status_code=409, detail="review decision already exists")
                if (
                    projection.status != "claimed"
                    or projection.claim_iteration != request.iteration
                    or not projection.validation_run_recorded
                ):
                    raise HTTPException(status_code=409, detail="review is not current")
                validation_refs = {
                    child["data"].get("artifact_ref")
                    for child in children
                    if child["block_type"] == "validation_run"
                    and child["data"].get("iteration") == request.iteration
                    and isinstance(child["data"].get("artifact_ref"), str)
                }
                if not set(request.evidence_refs).issubset(validation_refs):
                    raise HTTPException(status_code=409, detail="review evidence is not authoritative")
                block = self._new_block(
                    cursor,
                    index=self._next_index(cursor),
                    block_type="review_decision",
                    data=data,
                    status=request.decision,
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, request.batch_id, now=now)
                self._insert(cursor, block)
                projected = project_batch(
                    [parent, *children, block], request.batch_id, now=now
                )
                if request.decision == "failed" and projected.failed_review_count == 5:
                    terminal = self._new_block(
                        cursor,
                        index=self._next_index(cursor),
                        block_type="batch_done",
                        data={
                            "batch_id": request.batch_id,
                            "outcome": "failed_after_max_iterations",
                        },
                        status="failed_after_max_iterations",
                        parent_index=parent["index"],
                    )
                    self._validate_candidate(
                        parent,
                        [*children, block],
                        terminal,
                        request.batch_id,
                        now=now,
                    )
                    self._insert(cursor, terminal)
        return block

    def claim(self, batch_id: str) -> dict[str, str | int]:
        return self._retry_write(lambda: self._claim_once(batch_id))

    def _claim_once(self, batch_id: str) -> dict[str, str]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                claim_expired_without_recovery = (
                    projection.claim_iteration > 0
                    and projection.claim_expires_at is not None
                    and projection.claim_expires_at <= now
                    and not projection.recovery_recorded
                )
                if projection.status != "pending" or claim_expired_without_recovery:
                    raise HTTPException(status_code=409, detail="batch is not claimable")
                token = secrets.token_urlsafe(32)
                claim_id = f"claim-{secrets.token_urlsafe(18)}"
                fencing_token = projection.claim_iteration + 1
                expiry = now + self._claim_ttl
                event = ClaimEvent(
                    batch_id=batch_id,
                    claim_id=claim_id,
                    fencing_token=fencing_token,
                    claim_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    claim_expires_at=expiry,
                )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_claimed",
                    data=event.model_dump(mode="json"),
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
        return {
            "claim_token": token,
            "claim_id": claim_id,
            "fencing_token": fencing_token,
            "claim_expires_at": expiry.isoformat(),
        }

    def heartbeat(self, batch_id: str, token: str | None) -> dict[str, str]:
        return self._retry_write(lambda: self._heartbeat_once(batch_id, token))

    def _heartbeat_once(self, batch_id: str, token: str | None) -> dict[str, str]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                self._assert_live_claim(projection, token, now=now)
                expiry = now + timedelta(minutes=30)
                event = HeartbeatEvent(batch_id=batch_id, claim_expires_at=expiry)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_heartbeat",
                    data=event.model_dump(mode="json"),
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
        return {"claim_expires_at": expiry.isoformat()}

    def approve(self, batch_id: str) -> None:
        self._retry_write(lambda: self._approve_once(batch_id))

    def _approve_once(self, batch_id: str) -> None:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                if projection.status != "pending_review":
                    raise HTTPException(status_code=409, detail="batch is not pending review")
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_approved",
                    data={"batch_id": batch_id},
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)

    def list_batches(self, requested_status: str) -> list[dict[str, str]]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type = 'work_batch'
                    ORDER BY `index`
                    """
                )
                parents = [self.storage._decode_row(row) for row in cursor.fetchall()]
                result: list[dict[str, str]] = []
                for parent in parents:
                    batch_id = parent["data"]["batch_id"]
                    children = self._child_rows(cursor, parent["index"])
                    projection = project_batch([parent, *children], batch_id, now=now)
                    if projection.status == requested_status:
                        result.append(
                            {
                                "batch_id": batch_id,
                                "title": str(parent["data"].get("title", "")),
                            }
                        )
        return result

    def bundle(self, batch_id: str) -> dict[str, Any]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return {key: value for key, value in batch["data"].items() if "holdout" not in key.lower()}

    def blocks(self, batch_id: str, *, include_holdout: bool = False) -> list[dict[str, Any]]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                    ORDER BY `index`
                    """,
                    (batch_id,),
                )
                rows = cursor.fetchall()
        decoded = [self.storage._decode_row(row) for row in rows]
        for row in decoded:
            row["metadata"] = {
                key: value for key, value in row["metadata"].items() if not key.startswith("claim_")
            }
        return decoded if include_holdout else [row for row in decoded if row["block_type"] != "holdout"]

    def holdout(self, batch_id: str, token: str | None) -> dict[str, Any]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, _, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                self._assert_live_claim(projection, token, now=now)
                if not projection.codex_session_recorded:
                    raise HTTPException(status_code=404, detail="holdout not released")
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'holdout' AND parent_index = %s
                    ORDER BY `index` DESC LIMIT 1
                    """,
                    (parent["index"],),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="holdout not found")
        return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]

    @staticmethod
    def _upsert_capability(cursor: Any, block: dict[str, Any]) -> None:
        data = block["data"]
        capabilities = data.get("capabilities", [])
        descriptor = " ".join(str(value) for value in capabilities)
        cursor.execute(
            """
            INSERT INTO validated_capabilities
                (batch_id, descriptor, artifact_ref, block_index, payload)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE descriptor=VALUES(descriptor),
                artifact_ref=VALUES(artifact_ref), block_index=VALUES(block_index), payload=VALUES(payload)
            """,
            (
                data["batch_id"],
                descriptor,
                data.get("artifact_ref"),
                block["index"],
                json.dumps(data, sort_keys=True),
            ),
        )

    def capabilities(self, need: str) -> list[dict[str, Any]]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT batch_id, artifact_ref, payload FROM validated_capabilities
                    WHERE MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE)
                    ORDER BY MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE) DESC
                    """,
                    (need, need),
                )
                rows = cursor.fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    _, _, projection = self._batch_context(cursor, row["batch_id"], now=now)
                    if projection.status != "succeeded":
                        continue
                    result.append(
                        {
                            "batch_id": row["batch_id"],
                            "artifact_ref": row["artifact_ref"],
                            "data": (
                                json.loads(row["payload"])
                                if isinstance(row["payload"], str)
                                else row["payload"]
                            ),
                        }
                    )
        return result

    def import_legacy_record(
        self,
        request: LegacyImportWrite,
    ) -> tuple[dict[str, Any], bool]:
        try:
            block = self._retry_write(lambda: self._import_legacy_record_once(request))
            return block, True
        except _IdempotentReplay as replay:
            return replay.block, False

    def _import_legacy_record_once(self, request: LegacyImportWrite) -> dict[str, Any]:
        data = dict(request.data)
        if data.get("batch_id") != request.batch_id:
            raise HTTPException(status_code=422, detail="legacy data.batch_id must match batch_id")
        supplied_record_id = data.get("legacy_record_id")
        if supplied_record_id not in {None, request.legacy_record_id}:
            raise HTTPException(status_code=422, detail="legacy_record_id is reserved")
        data["legacy_record_id"] = request.legacy_record_id
        block_type = (
            "legacy_delivery_todo"
            if request.record_type == "todo"
            else "legacy_delivery_event"
        )

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type IN ('legacy_delivery_todo', 'legacy_delivery_event')
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.legacy_record_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (request.legacy_record_id,),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing = self.storage._decode_row(existing_row)
                    if existing["block_type"] == block_type and existing["data"] == data:
                        raise _IdempotentReplay(existing)
                    raise HTTPException(
                        status_code=409,
                        detail="legacy_record_id already exists with different content",
                    )

                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type = 'legacy_delivery_todo'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (request.batch_id,),
                )
                root_row = cursor.fetchone()
                root = self.storage._decode_row(root_row) if root_row is not None else None
                if request.record_type == "todo" and root is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="legacy batch already belongs to another todo record",
                    )
                if request.record_type == "event" and root is None:
                    raise HTTPException(
                        status_code=409,
                        detail="legacy todo must be imported before its events",
                    )

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type=block_type,
                    data=data,
                    status="archived",
                    parent_index=root["index"] if root is not None else None,
                    metadata={"source": "sqlite-delivery-legacy-import/v1"},
                )
                self._insert(cursor, block)
        return block

    def factory_runtime_rpc_receipt(
        self, idempotency_key: str, *, request_id: UUID, job_id: UUID,
        correlation_id: UUID, operation: str, request_sha256: str,
    ) -> dict[str, object] | None:
        """Read the exact durable Factory-process response, if already committed."""

        if re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Factory RPC key is invalid")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT request_id, job_id, correlation_id, operation,
                           request_sha256, payload
                    FROM factory_runtime_rpc_receipts
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        if (
            str(row["request_id"]) != str(request_id)
            or str(row["job_id"]) != str(job_id)
            or str(row["correlation_id"]) != str(correlation_id)
            or row["operation"] != operation
            or row["request_sha256"] != request_sha256
        ):
            raise HTTPException(status_code=409, detail="Factory RPC replay changed")
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=409, detail="Factory RPC receipt is invalid")
        return payload

    def record_factory_runtime_rpc_receipt(
        self,
        *,
        idempotency_key: str,
        request_id: UUID,
        job_id: UUID,
        correlation_id: UUID,
        operation: str,
        request_sha256: str,
        response: dict[str, object],
    ) -> dict[str, object]:
        """Persist a post-effect response; identical retries return byte-equivalent data."""

        if (
            re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None
            or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
            or not operation
        ):
            raise HTTPException(status_code=422, detail="Factory RPC receipt is invalid")
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT IGNORE INTO factory_runtime_rpc_receipts (
                        idempotency_key, request_id, job_id, correlation_id,
                        operation, request_sha256, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        idempotency_key, str(request_id), str(job_id),
                        str(correlation_id), operation, request_sha256, encoded,
                    ),
                )
                cursor.execute(
                    """
                    SELECT request_id, job_id, correlation_id, operation,
                           request_sha256, payload
                    FROM factory_runtime_rpc_receipts
                    WHERE idempotency_key = %s
                    FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        if row is None or (
            str(row["request_id"]) != str(request_id)
            or str(row["job_id"]) != str(job_id)
            or str(row["correlation_id"]) != str(correlation_id)
            or row["operation"] != operation
            or row["request_sha256"] != request_sha256
        ):
            raise HTTPException(status_code=409, detail="Factory RPC replay changed")
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload != response:
            raise HTTPException(status_code=409, detail="Factory RPC response changed")
        return response
