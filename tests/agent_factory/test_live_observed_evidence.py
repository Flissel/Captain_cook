from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.factory_live_entrypoint import FactoryLiveObservedEvidence
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRecord,
    FactoryLiveEffectRequestV1,
)
from agenten.agent_factory.live_observed_evidence import (
    FactoryLiveEvidenceCollectionError,
    GatewayFactoryLiveObservedEvidenceCollector,
)
from agenten.agent_factory.service import FactoryWorkflowArtifact
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    FactorySkillInvocationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_release_gate import (
    workflow_budget,
    workflow_job,
    workflow_receipts,
    workflow_run,
)
from tests.agent_factory.test_skill_workflow_contracts import (
    NOW,
    artifact as artifact_payload,
    inventory_payload,
    invocation_payload,
)


def artifact(name: str, ordinal: int) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://factory-live/{name}-{ordinal}",
        sha256=f"{ordinal:064x}",
        media_type="application/json",
    )


@dataclass
class Repository:
    job_value: object
    artifacts: tuple[FactoryWorkflowArtifact, ...]
    budget: FactoryBudgetProjection | None
    receipts: tuple[FactoryUsageReceiptV1, ...]

    def job(self, job_id: UUID):
        if self.job_value.job_id != job_id:
            raise KeyError(job_id)
        return self.job_value

    def workflow_artifacts(self, job_id: UUID):
        self.job(job_id)
        return self.artifacts

    def workflow_budget_projection(self, job_id: UUID):
        self.job(job_id)
        return self.budget

    def workflow_usage_receipts(self, job_id: UUID):
        self.job(job_id)
        return self.receipts


@dataclass
class EffectHistory:
    records: tuple[FactoryLiveEffectRecord, ...]

    def history(self, job_id: UUID):
        return tuple(
            record for record in self.records if record.request.job_id == job_id
        )


@dataclass
class Scenario:
    repository: Repository
    effects: EffectHistory
    runs: tuple[TeamExecutionEvidenceV1, ...]


def _inventory() -> CodebaseInventoryV1:
    return CodebaseInventoryV1.model_validate(inventory_payload())


def _codex_invocation(run: TeamExecutionEvidenceV1) -> FactorySkillInvocationV1:
    ordinal = run.run_number
    payload = invocation_payload("brief_codex")
    payload.update(
        {
            "invocation_id": str(
                uuid5(NAMESPACE_URL, f"factory-observed-codex|{ordinal}")
            ),
            "attempt": run.attempt,
            "idempotency_key": f"{ordinal + 9:x}" * 64,
        }
    )
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease.update(
        {
            "attempt": run.attempt,
            "lease_id": f"lease-observed-codex-{ordinal}",
        }
    )
    return FactorySkillInvocationV1.model_validate(payload)


def _effect_record(
    run: TeamExecutionEvidenceV1,
    *,
    kind: FactoryLiveEffectKind,
    index: int,
    completion_origin: str = "execute",
) -> FactoryLiveEffectRecord:
    run_id = uuid5(NAMESPACE_URL, f"factory-observed-run|{run.run_number}")
    invocation = (
        run.invocation
        if kind is FactoryLiveEffectKind.PROVIDER
        else _codex_invocation(run)
    )
    request = FactoryLiveEffectRequestV1(
        schema_name="captain.factory-live-effect-request.v1",
        effect_id=uuid5(
            NAMESPACE_URL,
            f"factory-observed-effect|{run.run_number}|{kind.value}",
        ),
        job_id=run.job_id,
        correlation_id=run.correlation_id,
        subject_version=run.subject_version,
        attempt=run.attempt,
        kind=kind,
        idempotency_key=invocation.idempotency_key,
        input_ref=invocation.input_ref,
        invocation=invocation,
        run_id=run_id,
        run_effect_index=index,
        run_effect_count=2,
    )
    outcome = FactoryLiveEffectOutcomeV1(
        schema_name="captain.factory-live-effect-outcome.v1",
        outcome_id=uuid5(NAMESPACE_URL, f"factory-observed-outcome|{request.effect_id}"),
        effect_id=request.effect_id,
        job_id=request.job_id,
        correlation_id=request.correlation_id,
        subject_version=request.subject_version,
        attempt=request.attempt,
        status="succeeded",
        evidence_ref=artifact(f"{kind.value}-effect", 100 + run.run_number + index),
        completion_origin=completion_origin,
        completed_at=NOW + timedelta(minutes=2, seconds=run.run_number),
    )
    return FactoryLiveEffectRecord(request=request, outcome=outcome)


def scenario(mode: str = "demo") -> Scenario:
    job = workflow_job(mode=mode)
    required = job.execution_policy.required_live_runs
    runs = tuple(workflow_run(number) for number in range(1, required + 1))
    receipts = workflow_receipts(runs)
    effects = tuple(
        record
        for run in runs
        for record in (
            _effect_record(run, kind=FactoryLiveEffectKind.CODEX, index=1),
            _effect_record(
                run,
                kind=FactoryLiveEffectKind.PROVIDER,
                index=2,
                completion_origin=(
                    "recover"
                    if mode == "release" and run.run_number == 1
                    else "execute"
                ),
            ),
        )
    )
    return Scenario(
        repository=Repository(
            job_value=job,
            artifacts=(_inventory(), *runs),
            budget=workflow_budget(),
            receipts=receipts,
        ),
        effects=EffectHistory(effects),
        runs=runs,
    )


def collect(value: Scenario) -> FactoryLiveObservedEvidence:
    collector = GatewayFactoryLiveObservedEvidenceCollector(
        repository=value.repository,
        effect_history=value.effects,
    )
    return collector.collect(value.repository.job_value.job_id)


def test_collects_demo_evidence_only_from_gateway_authority_sources() -> None:
    value = scenario()

    observed = collect(value)

    assert observed.context7_provenance_digest == "2" * 64
    assert observed.gateway_total_cost_usd == Decimal("0.75")
    assert observed.recovery is None
    assert observed.n8n_evidence is None
    assert len(observed.provider_traces) == 1
    trace = observed.provider_traces[0]
    assert trace.codex_session_id == str(
        value.effects.records[0].request.invocation.invocation_id
    )
    assert trace.hermes_session_id == str(value.runs[0].invocation_id)
    assert trace.trace_id == str(value.effects.records[1].outcome.outcome_id)
    assert trace.provider == "approved-provider"
    assert trace.model == "approved-model-id"
    assert trace.cost_usd == Decimal("0.75")
    assert trace.usage_receipt_ref == value.repository.receipts[0].evidence_ref
    assert trace.budget_receipt_ref == value.repository.receipts[0].evidence_ref


@pytest.mark.parametrize("missing", ("inventory", "run", "receipt", "effect", "budget"))
def test_missing_authoritative_evidence_fails_closed(missing: str) -> None:
    value = scenario()
    if missing == "inventory":
        value.repository.artifacts = value.runs
    elif missing == "run":
        value.repository.artifacts = (value.repository.artifacts[0],)
    elif missing == "receipt":
        value.repository.receipts = ()
    elif missing == "effect":
        value.effects.records = value.effects.records[:-1]
    else:
        value.repository.budget = None

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="incomplete"):
        collect(value)


def test_tampered_receipt_binding_fails_closed() -> None:
    value = scenario()
    receipt = value.repository.receipts[0]
    value.repository.receipts = (
        receipt.model_copy(update={"invocation_id": UUID(int=999)}),
    )

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="binding"):
        collect(value)


def test_tampered_live_effect_outcome_binding_fails_closed() -> None:
    value = scenario()
    provider = value.effects.records[1]
    value.effects.records = (
        value.effects.records[0],
        provider.model_copy(
            update={
                "outcome": provider.outcome.model_copy(
                    update={"effect_id": UUID(int=998)}
                )
            }
        ),
    )

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="binding"):
        collect(value)


@pytest.mark.parametrize("duplicate", ("codex_session", "hermes_session", "budget_ref"))
def test_duplicate_session_and_cost_identities_fail_closed(duplicate: str) -> None:
    value = scenario("release")
    records = list(value.effects.records)
    if duplicate == "codex_session":
        first = records[0].request.invocation.invocation_id
        request = records[2].request
        invocation = request.invocation.model_copy(update={"invocation_id": first})
        records[2] = records[2].model_copy(
            update={"request": request.model_copy(update={"invocation": invocation})}
        )
    elif duplicate == "hermes_session":
        first = records[1].request.invocation.invocation_id
        request = records[3].request
        invocation = request.invocation.model_copy(update={"invocation_id": first})
        records[3] = records[3].model_copy(
            update={"request": request.model_copy(update={"invocation": invocation})}
        )
    else:
        first = value.repository.receipts[0].evidence_ref
        second_receipt = value.repository.receipts[1].model_copy(
            update={"evidence_ref": first}
        )
        second_run = value.runs[1].model_copy(update={"usage_receipt_refs": (first,)})
        value.repository.receipts = (
            value.repository.receipts[0],
            second_receipt,
            *value.repository.receipts[2:],
        )
        value.repository.artifacts = (
            value.repository.artifacts[0],
            value.runs[0],
            second_run,
            *value.runs[2:],
        )
    value.effects.records = tuple(records)

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="duplicate"):
        collect(value)


def test_gateway_total_must_exactly_cover_trace_receipts() -> None:
    value = scenario()
    value.repository.budget = workflow_budget("0.76")

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="cost"):
        collect(value)


def test_release_requires_exactly_one_controlled_recovery() -> None:
    value = scenario("release")

    observed = collect(value)

    assert observed.recovery is not None
    assert (
        observed.recovery.evidence_digest
        == value.effects.records[1].outcome.evidence_ref.sha256
    )

    first_provider = value.effects.records[1]
    value.effects.records = (
        value.effects.records[0],
        first_provider.model_copy(
            update={
                "outcome": first_provider.outcome.model_copy(
                    update={"completion_origin": "execute"}
                )
            }
        ),
        *value.effects.records[2:],
    )
    with pytest.raises(FactoryLiveEvidenceCollectionError, match="recovery"):
        collect(value)


def test_n8n_claim_without_typed_gateway_identifiers_fails_closed() -> None:
    value = scenario()
    run = value.runs[0]
    n8n_ref = ArtifactRef.model_validate(artifact_payload("n8n-workflow", "9" * 64))
    value.repository.artifacts = (
        value.repository.artifacts[0],
        run.model_copy(update={"workflow_evidence_refs": (n8n_ref,)}),
    )

    with pytest.raises(FactoryLiveEvidenceCollectionError, match="n8n"):
        collect(value)
