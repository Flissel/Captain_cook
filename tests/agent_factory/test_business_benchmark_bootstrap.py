from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkPolicyV1,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CLAIMS_PROFILE_ID,
)
from agenten.agent_factory.business_benchmark_store import (
    FilesystemBusinessBenchmarkEvidenceStore,
)
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    load_production_business_benchmark_composition,
)
from agenten.agent_factory.business_benchmark_production import (
    CaptainBusinessBenchmarkPolicyBindingV1,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_runtime.contracts import ArtifactRef

from tests.agent_factory.test_business_benchmark_production_ports import live_job
from tests.agent_factory.test_business_benchmark_production import (
    execution as production_execution,
    run_receipt,
    suite,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://bootstrap-test/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _settings(job: AgentFactoryJobV3) -> LiveBusinessBenchmarkSettings:
    return LiveBusinessBenchmarkSettings(
        profile="claims",
        provider="openai",
        model="approved-model-id",
        redaction_policy_sha256="a" * 64,
        selections=(
            BusinessBenchmarkTeamSelectionV1(
                profile="claims",
                job_id=job.job_id,
                candidate_id="claims-candidate-v1",
                suite_version=1,
                attempt=1,
                maximum_usd=Decimal("1.00"),
                captain_remaining_usd=Decimal("5.00"),
            ),
        ),
        maximum_usd=Decimal("1.00"),
        allowed_models=("approved-model-id",),
        evidence_root=Path(".captain-cook/evidence/business-benchmarks/test"),
        runtime_url="http://127.0.0.1:8000",
        provider_secret_name="OPENAI_API_KEY",
    )


def _invocation(
    job: AgentFactoryJobV3,
    step: FactorySkillStep,
    *,
    input_ref: ArtifactRef | None = None,
) -> FactorySkillInvocationV1:
    role = {
        FactorySkillStep.EXECUTE_TEAM: FactoryRole.REAL_CASE_TESTER,
        FactorySkillStep.EVALUATE_TEAM: FactoryRole.QUALITY_WARDEN,
        FactorySkillStep.REPORT_CAPTAIN: FactoryRole.QUALITY_WARDEN,
    }[step]
    skill_id = {
        FactorySkillStep.EXECUTE_TEAM: "captain-factory-execute-team",
        FactorySkillStep.EVALUATE_TEAM: "captain-factory-evaluate-team",
        FactorySkillStep.REPORT_CAPTAIN: "captain-factory-report-captain",
    }[step]
    selected_input = input_ref or job.input_ref
    return FactorySkillInvocationV1(
        schema="captain.factory-skill-invocation.v1",
        invocation_id=uuid5(NAMESPACE_URL, f"bootstrap:{step.value}:{selected_input.sha256}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        step=step,
        released_skill=ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=1,
            capability="factory_workflow",
            content_ref=_ref(f"skill-{step.value}"),
            content_sha256=_ref(f"skill-{step.value}").sha256,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        input_ref=selected_input,
        input_sha256=selected_input.sha256,
        lease=issue_factory_lease(
            job=job,
            role=role,
            attempt=1,
            workspace_ref=f"workspace://bootstrap/{step.value}",
            now=NOW,
        ),
        idempotency_key=hashlib.sha256(
            f"{step.value}:{selected_input.sha256}".encode("utf-8")
        ).hexdigest(),
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        execution_scope_ref=(
            job.private_holdout_refs[0]
            if step is FactorySkillStep.EXECUTE_TEAM
            else None
        ),
    )


def test_default_loader_reports_exact_missing_authority_instead_of_bundle_todo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = live_job()
    monkeypatch.delenv("CAPTAIN_BENCHMARK_HUMAN_REVIEW_ADAPTER", raising=False)

    with pytest.raises(
        ProductionAdapterUnavailableError,
        match="CaptainHumanReviewPort",
    ) as caught:
        load_production_business_benchmark_composition(_settings(job))

    assert "production_adapter_bundle" not in str(caught.value)


def test_gateway_authority_uses_only_exact_persisted_workflow_evidence() -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        GatewayBusinessBenchmarkAuthority,
    )

    job = live_job()
    candidate_ref = _ref("candidate")
    execution_invocation = _invocation(job, FactorySkillStep.EXECUTE_TEAM)
    execution = production_execution(
        job,
        candidate_ref,
        execution_invocation,
    )

    class Repository:
        def job(self, job_id: UUID):
            return job if job_id == job.job_id else None

        def workflow_artifacts(self, job_id: UUID):
            return (execution,) if job_id == job.job_id else ()

        def workflow_budget_projection(self, job_id: UUID):
            assert job_id == job.job_id
            return FactoryBudgetProjection(
                job_id=job.job_id,
                limit_usd="5.00",
                consumed_usd="0",
                reserved_usd="0",
                remaining_usd="5.00",
            )

    authority = GatewayBusinessBenchmarkAuthority(Repository())

    assert authority.factory_job(job.job_id) == job
    assert authority.team_execution_evidence(job.job_id, 1) == (execution,)
    assert authority.candidate_ref(job.job_id, 1, "claims-candidate-v1") == candidate_ref
    assert authority.budget_projection(job.job_id).job_id == job.job_id


def test_gateway_authority_rejects_mixed_candidate_refs() -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        GatewayBusinessBenchmarkAuthority,
    )

    job = live_job()
    invocation = _invocation(job, FactorySkillStep.EXECUTE_TEAM)
    first = production_execution(job, _ref("first"), invocation)
    second = production_execution(job, _ref("second"), invocation).model_copy(
        update={"run_number": 2}
    )
    repository = SimpleNamespace(
        job=lambda _: job,
        workflow_artifacts=lambda _: (first, second),
        workflow_budget_projection=lambda _: None,
    )

    with pytest.raises(ValueError, match="candidate reference"):
        GatewayBusinessBenchmarkAuthority(repository).candidate_ref(
            job.job_id, 1, "claims-candidate-v1"
        )


def test_cas_policy_authority_requires_exact_job_attempt_binding(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        ContentAddressedBenchmarkPolicyAuthority,
    )

    job = live_job()
    artifacts = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    binding = CaptainBusinessBenchmarkPolicyBindingV1.create(
        job=job,
        attempt=1,
        policy=BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1"
        ),
    )
    binding_ref = artifacts.put(
        binding.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="benchmark-policy",
    )
    artifacts.bind("benchmark-policy", f"{job.job_id}:1", binding_ref)
    scope = SimpleNamespace(job=job, selection=SimpleNamespace(attempt=1))

    assert ContentAddressedBenchmarkPolicyAuthority(artifacts).policy_for(scope) == binding

    stale_scope = SimpleNamespace(job=job, selection=SimpleNamespace(attempt=2))
    with pytest.raises(ValueError, match="missing"):
        ContentAddressedBenchmarkPolicyAuthority(artifacts).policy_for(stale_scope)


def test_canonical_suite_authority_provisions_and_reloads_digest_bound_suite(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        CaptainCanonicalSuiteAuthority,
    )

    authority = CaptainCanonicalSuiteAuthority(
        root=tmp_path / ".captain-cook" / "private" / "business-benchmarks",
        seed_version_id="production-benchmark-seed-v1",
    )

    first_ref, first = authority.canonical_suite(
        profile_id=CLAIMS_PROFILE_ID,
        suite_version=1,
    )
    second_ref, second = authority.canonical_suite(
        profile_id=CLAIMS_PROFILE_ID,
        suite_version=1,
    )

    assert first_ref == second_ref
    assert first == second
    assert first.profile_id == CLAIMS_PROFILE_ID
    assert len(first.cases) == 15


def test_gateway_invocation_authority_builds_quality_chain_from_active_captain_data() -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        GatewayBenchmarkInvocationAuthority,
    )

    job = live_job()
    runtime = _invocation(job, FactorySkillStep.EXECUTE_TEAM)
    evaluation = _invocation(job, FactorySkillStep.EVALUATE_TEAM)
    report = _invocation(job, FactorySkillStep.REPORT_CAPTAIN, input_ref=_ref("evaluation"))

    class Catalog:
        def released_for(self, current_job, step):
            assert current_job == job
            return {
                FactorySkillStep.EVALUATE_TEAM: evaluation.released_skill,
                FactorySkillStep.REPORT_CAPTAIN: report.released_skill,
            }[step]

    class Leases:
        def active(self, current_job, role, attempt, now):
            assert current_job == job
            assert role is FactoryRole.QUALITY_WARDEN
            assert attempt == 1
            assert now == NOW
            return evaluation.lease

    repository = SimpleNamespace(
        workflow_artifacts=lambda _: (
            production_execution(job, _ref("candidate"), runtime),
        )
    )
    authority = GatewayBenchmarkInvocationAuthority(
        repository=repository,
        released_skills=Catalog(),
        leases=Leases(),
        clock=lambda: NOW,
    )

    assert authority.runtime_invocation(job=job, attempt=1) == runtime
    quality = authority.evaluation_invocation(job=job, attempt=1)
    assert quality.step is FactorySkillStep.EVALUATE_TEAM
    assert quality.input_ref == job.input_ref
    authority.require_active_report(job=job, attempt=1)
    evaluation_artifact = SimpleNamespace(artifact_ref=_ref("evaluation"))
    feedback = authority.report_invocation(
        job=job,
        attempt=1,
        evaluation=evaluation_artifact,
    )
    assert feedback.step is FactorySkillStep.REPORT_CAPTAIN
    assert feedback.input_ref == evaluation_artifact.artifact_ref


def test_gateway_invocation_authority_accepts_three_runs_of_same_invocation() -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        GatewayBenchmarkInvocationAuthority,
    )

    job = live_job()
    runtime = _invocation(job, FactorySkillStep.EXECUTE_TEAM)
    executions = tuple(
        production_execution(job, _ref("candidate"), runtime, run_number)
        for run_number in (1, 2, 3)
    )
    authority = GatewayBenchmarkInvocationAuthority(
        repository=SimpleNamespace(workflow_artifacts=lambda _: executions),
        released_skills=SimpleNamespace(),
        leases=SimpleNamespace(),
        clock=lambda: NOW,
    )

    assert authority.runtime_invocation(job=job, attempt=1) == runtime


def test_filesystem_receipt_finalizer_returns_exact_immutable_reference(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        FilesystemBenchmarkReceiptFinalizer,
    )

    job = live_job()
    candidate_ref = _ref("candidate")
    receipt = run_receipt(
        job=job,
        candidate_ref=candidate_ref,
        benchmark_case=suite().cases[0],
        variant="candidate",
    )
    finalizer = FilesystemBenchmarkReceiptFinalizer(
        FilesystemBusinessBenchmarkEvidenceStore(
            tmp_path / ".captain-cook" / "evidence" / "business-benchmarks"
        )
    )

    first = finalizer.finalize(profile="claims", receipt=receipt)
    second = finalizer.finalize(profile="claims", receipt=receipt)

    assert first == second
    assert first.receipt == receipt
    assert first.receipt_ref.sha256 == second.receipt_ref.sha256
