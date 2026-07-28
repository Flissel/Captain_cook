from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
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
    monkeypatch.setenv(
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID",
        "production-benchmark-seed-v1",
    )

    with pytest.raises(
        ValueError,
        match="CAPTAIN_BENCHMARK_CASE_MAX_COST_USD",
    ) as caught:
        load_production_business_benchmark_composition(_settings(job))

    assert "production_adapter_bundle" not in str(caught.value)


def test_bootstrap_config_builds_only_gitignored_captain_roots(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        ProductionBusinessBenchmarkBootstrapConfig,
    )

    job = live_job()
    settings = _settings(job).model_copy(
        update={
            "evidence_root": (
                tmp_path
                / ".captain-cook"
                / "evidence"
                / "business-benchmarks"
                / "run-1"
            )
        }
    )

    config = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        settings,
        {
            "CAPTAIN_BENCHMARK_SEED_VERSION_ID": (
                "production-benchmark-seed-v1"
            ),
            "CAPTAIN_BENCHMARK_AUTHORITY_ROOT": str(
                tmp_path / ".captain-cook" / "private" / "business-benchmarks"
            ),
            "CAPTAIN_BENCHMARK_HUMAN_REVIEW_TIMEOUT_SECONDS": "30",
        },
    )

    assert config.seed_version_id == "production-benchmark-seed-v1"
    assert config.human_review_timeout_seconds == 30
    authority_root = tmp_path / ".captain-cook" / "private" / "business-benchmarks"
    assert config.cas_root == authority_root / "cas"
    assert config.private_suite_root == authority_root / "suites"
    assert config.human_review_root == authority_root / "human-review"
    assert config.replay_root == authority_root / "runtime-state" / "replay"

    restarted_settings = settings.model_copy(
        update={"evidence_root": settings.evidence_root.parent / "run-2"}
    )
    restarted = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        restarted_settings,
        {
            "CAPTAIN_BENCHMARK_SEED_VERSION_ID": "production-benchmark-seed-v1",
            "CAPTAIN_BENCHMARK_AUTHORITY_ROOT": str(authority_root),
        },
    )
    assert restarted.cas_root == config.cas_root
    assert restarted.private_suite_root == config.private_suite_root
    assert restarted.human_review_root == config.human_review_root
    assert restarted.replay_root == config.replay_root
    assert restarted.evidence_store_root != config.evidence_store_root


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({}, "CAPTAIN_BENCHMARK_SEED_VERSION_ID"),
        (
            {"CAPTAIN_BENCHMARK_SEED_VERSION_ID": "../escape"},
            "seed version",
        ),
        (
            {
                "CAPTAIN_BENCHMARK_SEED_VERSION_ID": "seed-v1",
                "CAPTAIN_BENCHMARK_HUMAN_REVIEW_TIMEOUT_SECONDS": "-1",
            },
            "human review timeout",
        ),
    ),
)
def test_bootstrap_config_fails_closed_before_creating_directories(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        ProductionBusinessBenchmarkBootstrapConfig,
    )

    job = live_job()
    evidence_root = (
        tmp_path / ".captain-cook" / "evidence" / "business-benchmarks" / "run-1"
    )
    configured = _settings(job).model_copy(update={"evidence_root": evidence_root})

    with pytest.raises(ValueError, match=message):
        ProductionBusinessBenchmarkBootstrapConfig.from_environment(
            configured,
            environment,
        )

    assert not evidence_root.exists()


def test_composition_wires_durable_authorities_without_external_effects(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        GatewayBusinessBenchmarkAuthority,
        ProductionBusinessBenchmarkBootstrapConfig,
        ProductionBusinessBenchmarkBootstrapPorts,
        compose_production_business_benchmark_composition,
    )
    from agenten.agent_factory.business_benchmark_production import (
        ProductionBusinessBenchmarkComposition,
    )
    from agenten.agent_factory.business_benchmark_replay import (
        FilesystemBusinessBenchmarkReplayStore,
    )

    job = live_job()
    evidence_root = (
        tmp_path / ".captain-cook" / "evidence" / "business-benchmarks" / "run-1"
    )
    configured = _settings(job).model_copy(update={"evidence_root": evidence_root})
    config = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        configured,
        {
            "CAPTAIN_BENCHMARK_SEED_VERSION_ID": "seed-v1",
            "CAPTAIN_BENCHMARK_AUTHORITY_ROOT": str(
                tmp_path / ".captain-cook" / "private" / "business-benchmarks"
            ),
        },
    )
    executor_calls: list[object] = []
    policy_calls: list[object] = []
    repository = SimpleNamespace()
    ports = ProductionBusinessBenchmarkBootstrapPorts(
        gateway_repository=repository,
        released_skills=SimpleNamespace(),
        leases=SimpleNamespace(),
        executor_builder=lambda scope, authorities: executor_calls.append(
            (scope, authorities)
        )
        or object(),
        execution_policy_builder=lambda scope: policy_calls.append(scope)
        or (lambda case: object()),
        clock=lambda: NOW,
    )

    composition = compose_production_business_benchmark_composition(
        configured,
        config=config,
        ports=ports,
    )

    assert isinstance(composition, ProductionBusinessBenchmarkComposition)
    assert isinstance(composition._resolver._gateway, GatewayBusinessBenchmarkAuthority)
    assert composition._resolver._gateway._repository is repository
    replay = composition._replay_store_factory(
        SimpleNamespace(job=job, selection=SimpleNamespace(attempt=1))
    )
    assert isinstance(replay, FilesystemBusinessBenchmarkReplayStore)
    assert executor_calls == []
    assert policy_calls == []
    assert config.cas_root.is_dir()
    assert not config.human_review_root.exists()


def test_configured_case_policy_preserves_each_canonical_tool_intent() -> None:
    import json

    from agenten.agent_factory.business_benchmark_bootstrap import (
        ConfiguredBusinessBenchmarkExecutionPolicyBuilder,
    )

    job = live_job()
    version = "benchmark-redaction-v1"
    redaction_sha = hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    configured = _settings(job).model_copy(
        update={"redaction_policy_sha256": redaction_sha}
    )
    builder = ConfiguredBusinessBenchmarkExecutionPolicyBuilder.from_environment(
        configured,
        {
            "CAPTAIN_BENCHMARK_CASE_MAX_COST_USD": "0.001",
            "CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS": "2500",
            "CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION": version,
        },
    )
    policy_for = builder(SimpleNamespace(settings=configured))

    policies = tuple(policy_for(item) for item in suite().cases)

    assert all(item.model_version == configured.model for item in policies)
    assert all(item.maximum_cost_micro_usd == 1000 for item in policies)
    assert all(item.maximum_latency_ms == 2500 for item in policies)
    assert tuple(item.allowed_tool_intents for item in policies) == tuple(
        item.allowed_tool_intents for item in suite().cases
    )


def _complete_bootstrap_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    redaction_version: str,
) -> None:
    values = {
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID": "seed-v1",
        "CAPTAIN_BENCHMARK_AUTHORITY_ROOT": str(
            tmp_path / ".captain-cook" / "private" / "business-benchmarks"
        ),
        "CAPTAIN_BENCHMARK_CASE_MAX_COST_USD": "0.001",
        "CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS": "2500",
        "CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION": redaction_version,
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "approved-model-id",
        "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD": "0.001",
        "CAPTAIN_BENCHMARK_PRICING_VERSION": "test-price-v1",
        "CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT": "2026-07-28T00:00:00Z",
        "CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD": "1.00",
        "CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD": "2.00",
        "CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD": "0",
        "OPENAI_API_KEY": "test-only-never-rendered",
    }
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    values["CAPTAIN_FACTORY_SKILL_ROOT"] = str(skill_root)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_default_claims_build_stops_at_exact_gateway_client_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import agenten.agent_factory.business_benchmark_bootstrap as bootstrap

    version = "benchmark-redaction-v1"
    redaction_sha = hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    job = live_job()
    configured = _settings(job).model_copy(
        update={
            "redaction_policy_sha256": redaction_sha,
            "evidence_root": (
                tmp_path / ".captain-cook" / "evidence" / "business-benchmarks" / "run-1"
            ),
        }
    )
    _complete_bootstrap_environment(
        monkeypatch,
        tmp_path=tmp_path,
        redaction_version=version,
    )
    monkeypatch.setenv("CAPTAIN_GATEWAY_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "test-only-never-rendered")

    with pytest.raises(
        ProductionAdapterUnavailableError,
        match="CaptainBusinessBenchmarkGatewayClientPort",
    ) as caught:
        bootstrap.build_production_business_benchmark_composition(configured)

    assert "TODO_TOOL.v1" in str(caught.value)
    assert "direct MariaDB access outside gateway is forbidden" in str(caught.value)


def test_default_renewal_build_stops_at_exact_grant_bound_baseline_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import agenten.agent_factory.business_benchmark_bootstrap as bootstrap

    version = "benchmark-redaction-v1"
    redaction_sha = hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    job = live_job()
    selection = _settings(job).selections[0].model_copy(update={"profile": "renewal"})
    configured = _settings(job).model_copy(
        update={
            "profile": "renewal",
            "selections": (selection,),
            "redaction_policy_sha256": redaction_sha,
            "evidence_root": (
                tmp_path / ".captain-cook" / "evidence" / "business-benchmarks" / "run-1"
            ),
        }
    )
    _complete_bootstrap_environment(
        monkeypatch,
        tmp_path=tmp_path,
        redaction_version=version,
    )
    with pytest.raises(
        ProductionAdapterUnavailableError,
        match="CaptainRenewalBaselineN8nGrantPort",
    ) as caught:
        bootstrap.build_production_business_benchmark_composition(configured)

    assert "TODO_TOOL.v1" in str(caught.value)
    assert "authority-free fallback is forbidden" in str(caught.value)


def test_claims_executor_builder_creates_durable_host_runtime_without_provider_call(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        CaptainClaimsBusinessBenchmarkExecutorBuilder,
        ConfiguredBusinessBenchmarkExecutionPolicyBuilder,
        ProductionBusinessBenchmarkRuntimeAuthorities,
    )
    from agenten.agent_factory.business_benchmark_candidate_seeds import (
        CLAIMS_SEED_PROFILE,
        package_business_benchmark_seed,
    )
    from agenten.agent_factory.business_benchmark_human_review import (
        CaptainHumanReviewStore,
    )
    from agenten.agent_factory.business_benchmark_live import BusinessBenchmarkLiveAdapter
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )
    from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
    from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
    from agenten.agent_factory.team_execution import _sealed_text

    job = live_job()
    now = job.deadline_at - timedelta(minutes=1)
    artifacts = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "private" / "business-benchmarks" / "cas"
    )
    packaged = package_business_benchmark_seed(
        CLAIMS_SEED_PROFILE,
        tmp_path / "seed",
    )
    source_ref = artifacts.put(
        packaged.source_archive.read_bytes(),
        "application/zip",
        namespace="candidate-archive",
    )
    candidate = ResolvedFactoryCandidate(
        candidate=packaged.candidate.model_copy(
            update={"source_archive_ref": source_ref}
        ),
        source_archive=artifacts.local_path(source_ref),
    )
    configured = _settings(job)
    policy_builder = ConfiguredBusinessBenchmarkExecutionPolicyBuilder(
        model=configured.model,
        redaction_policy_version="benchmark-redaction-v1",
        maximum_cost_micro_usd=1000,
        maximum_latency_ms=2500,
    )
    scope = SimpleNamespace(
        selection=SimpleNamespace(profile="claims", attempt=1),
        job=job,
        candidate=candidate,
        candidate_ref=source_ref,
        runtime_invocation=_invocation(job, FactorySkillStep.EXECUTE_TEAM),
        suite=suite(),
        suite_ref=job.private_holdout_refs[0],
        suite_id=suite().suite_id,
        settings=configured,
    )
    runtime_authorities = ProductionBusinessBenchmarkRuntimeAuthorities(
        artifacts=artifacts,
        human_review=CaptainHumanReviewStore(
            tmp_path / ".captain-cook" / "private" / "business-benchmarks" / "human-review"
        ),
        provider_state_root=(
            tmp_path
            / ".captain-cook"
            / "private"
            / "business-benchmarks"
            / "runtime-state"
            / "provider-state"
        ),
    )
    builder = CaptainClaimsBusinessBenchmarkExecutorBuilder(
        model_client_builder=SimpleNamespace(),
        budget=SimpleNamespace(),
        pricing_authority=SimpleNamespace(),
        paid_effect_authority=SimpleNamespace(),
        evidence_store=FilesystemFactoryEvidenceStore(
            tmp_path / ".captain-cook" / "evidence" / "factory"
        ),
        policy_builder=policy_builder,
        provider="openai",
        model=configured.model,
        max_cost_per_call=Decimal("0.001"),
        clock=lambda: now,
    )

    executor = builder(scope, runtime_authorities)

    assert isinstance(executor, BusinessBenchmarkLiveAdapter)
    runtime_scope = executor._runtime_bundle._scopes[job.job_id]
    assert runtime_scope.team_manifest.conversation_pattern == "swarm"
    assert runtime_scope.allowed_host_tools == ()
    assert runtime_scope.baseline_policy.allowed_tools == ()
    assert runtime_scope.candidate_workspace == artifacts.root
    assert _sealed_text(
        runtime_scope.candidate_workspace,
        runtime_scope.baseline_policy.system_prompt_ref,
    ) == CaptainClaimsBusinessBenchmarkExecutorBuilder._BASELINE_PROMPT.decode(
        "utf-8"
    )
    prompt_matches = tuple(
        path
        for path in runtime_scope.candidate_workspace.rglob("*")
        if path.is_file()
        and hashlib.sha256(path.read_bytes()).hexdigest()
        == runtime_scope.baseline_policy.system_prompt_ref.sha256
    )
    assert prompt_matches == (
        artifacts.local_path(runtime_scope.baseline_policy.system_prompt_ref),
    )


def test_claims_baseline_prompt_has_equal_public_policy_without_private_labels() -> None:
    from agenten.agent_factory.business_benchmark_bootstrap import (
        CaptainClaimsBusinessBenchmarkExecutorBuilder,
    )

    prompt = CaptainClaimsBusinessBenchmarkExecutorBuilder._BASELINE_PROMPT.decode(
        "utf-8"
    )

    assert "claims-public-policy-v1" in prompt
    assert "ordinary" in prompt
    assert "near-boundary" in prompt
    assert "Missing or unverified" in prompt
    assert "Conflicting sources" in prompt
    assert "explicit escalation trigger" in prompt
    assert {
        "route_standard_review",
        "request_information",
        "escalate_coverage",
        "coverage_state_verified",
        "evidence_complete",
        "boundary_condition_identified",
        "required_evidence_missing",
        "decision_deferred",
        "evidence_conflict_detected",
        "specialist_review_required",
        "critical_coverage_question_detected",
        "human_authority_required",
    }.issubset(set(prompt.replace(".", " ").replace(",", " ").split()))
    assert '"schema":"captain.business-benchmark-terminal.v1"' in prompt
    assert "exactly one JSON object and nothing else" in prompt
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "93cb72b6151834b0a82edf9fc524e8bc4c9d1f035e3e7bb544de4e2bd11acd08"
    )
    forbidden = (
        "claims-ordinary-",
        "claims-boundary-",
        "claims-incomplete-",
        "claims-contradictory-",
        "claims-mandatory-escalation-",
        "expected_decision",
        "required_rationale_fact_ids",
        "case_id",
        "synthetic_subject_id",
        "Swarm",
        "coverage_specialist",
        "escalation_specialist",
    )
    assert not any(item in prompt for item in forbidden)


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
