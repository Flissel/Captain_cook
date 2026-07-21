from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectReport,
    FactoryLiveRunReport,
)
from agenten.agent_factory.release_gate import FactoryReleaseDecision
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryLifecycleStatus,
)
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from tests.agent_factory.test_release_gate import workflow_job, workflow_run
from tests.agent_factory.test_skill_workflow_contracts import artifact as workflow_artifact


SKILLS = (
    "captain-factory-discover",
    "captain-factory-brief-codex",
    "captain-factory-execute-team",
    "captain-factory-evaluate-team",
    "captain-factory-improve-team",
    "captain-factory-report-captain",
)


class PreflightProbe:
    def verify_database(self, _dsn: str) -> str:
        return "captain_test"

    def verify_services(self) -> None:
        return None

    def verify_codex(self) -> None:
        return None

    def verify_hermes(self, expected_skill_digests) -> None:
        assert tuple(expected_skill_digests) == SKILLS

    def verify_n8n(self) -> None:
        return None


class AdapterFactory:
    def preflight(self, settings):
        from agenten.agent_factory.factory_live_entrypoint import (
            FactoryLiveAdapterPreflight,
        )

        return FactoryLiveAdapterPreflight(
            model=settings.model,
            hermes=True,
            codex=True,
            autogen=True,
            context7=True,
            forge=True,
            pricing=True,
            holdouts=True,
            hermes_usage_file_receipts=True,
            hermes_costs_persisted_by_captain=True,
            sealed_codex_brief_before_claim=True,
            dispatch_replays_claimed_effects=True,
            hermes_gateway_claims=True,
            quality_warden_runs_hermes_skills=True,
            workflow_artifacts_persisted_before_blocks=True,
            n8n=settings.with_n8n,
        )


def test_preflight_writes_exact_six_skill_redacted_contract(tmp_path: Path) -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactoryLivePreflightSettings,
        run_factory_live_preflight,
    )

    repository_root = Path(__file__).resolve().parents[2]
    report_directory = tmp_path / "external-report"
    report_directory.mkdir()
    output = report_directory / "preflight.json"
    secret = "database-password-must-not-leak"
    settings = FactoryLivePreflightSettings(
        mode="demo",
        max_cost_usd=Decimal("5.00"),
        model="approved-model",
        repository_root=repository_root,
        report_directory=report_directory,
        output=output,
        database_dsn=f"mysql://captain:{secret}@127.0.0.1:3306/captain_test",
        with_n8n=False,
    )

    result = run_factory_live_preflight(
        settings,
        probe=PreflightProbe(),
        adapter_factory=AdapterFactory(),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == result.model_dump(mode="json", by_alias=True)
    assert payload["schema"] == "captain.hermes-six-skill-factory-preflight.v1"
    assert payload["database_name"] == "captain_test"
    assert payload["prerequisites_confirmed"] is True
    assert payload["services_verified"] is True
    assert payload["codex_authenticated"] is True
    assert payload["skills_verified"] is True
    assert set(payload["skill_digests"]) == set(SKILLS)
    assert all(
        len(digest) == 64 and digest == digest.lower()
        for digest in payload["skill_digests"].values()
    )
    serialized = output.read_text(encoding="utf-8")
    assert secret not in serialized
    assert str(repository_root) not in serialized
    assert str(report_directory) not in serialized


@pytest.mark.parametrize(
    ("max_cost", "model"),
    ((Decimal("0"), "approved-model"), (Decimal("1"), "unknown model")),
)
def test_preflight_rejects_invalid_cost_or_model_without_writing(
    tmp_path: Path,
    max_cost: Decimal,
    model: str,
) -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactoryLivePreflightSettings,
    )

    repository_root = Path(__file__).resolve().parents[2]
    report_directory = tmp_path / "external-report"
    report_directory.mkdir()

    with pytest.raises(ValueError):
        FactoryLivePreflightSettings(
            mode="demo",
            max_cost_usd=max_cost,
            model=model,
            repository_root=repository_root,
            report_directory=report_directory,
            output=report_directory / "preflight.json",
            database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
            with_n8n=False,
        )


def test_preflight_requires_external_output_and_real_adapter_factory(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactoryLiveConfigurationError,
        FactoryLivePreflightSettings,
        run_factory_live_preflight,
    )

    repository_root = Path(__file__).resolve().parents[2]
    internal = repository_root / "artifacts"
    with pytest.raises(ValueError, match="outside"):
        FactoryLivePreflightSettings(
            mode="demo",
            max_cost_usd=Decimal("1.00"),
            model="approved-model",
            repository_root=repository_root,
            report_directory=internal,
            output=internal / "preflight.json",
            database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
            with_n8n=False,
        )

    external = tmp_path / "external"
    external.mkdir()
    settings = FactoryLivePreflightSettings(
        mode="demo",
        max_cost_usd=Decimal("1.00"),
        model="approved-model",
        repository_root=repository_root,
        report_directory=external,
        output=external / "preflight.json",
        database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
        with_n8n=False,
    )
    with pytest.raises(FactoryLiveConfigurationError, match="adapter factory"):
        run_factory_live_preflight(
            settings,
            probe=PreflightProbe(),
            adapter_factory=None,
        )


def test_preflight_fails_closed_when_hermes_usage_cost_is_unresolved(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactoryLiveConfigurationError,
        FactoryLivePreflightSettings,
        run_factory_live_preflight,
    )

    class UnaccountedHermesFactory:
        def preflight(self, settings):
            return {
                "model": settings.model,
                "hermes": True,
                "codex": True,
                "autogen": True,
                "context7": True,
                "forge": True,
                "pricing": True,
                "holdouts": True,
                "hermes_usage_file_receipts": False,
                "hermes_costs_persisted_by_captain": False,
                "n8n": False,
            }

    repository_root = Path(__file__).resolve().parents[2]
    report_directory = tmp_path / "external"
    report_directory.mkdir()
    settings = FactoryLivePreflightSettings(
        mode="demo",
        max_cost_usd=Decimal("1.00"),
        model="approved-model",
        repository_root=repository_root,
        report_directory=report_directory,
        output=report_directory / "preflight.json",
        database_dsn="mysql://captain:do-not-leak@127.0.0.1:3306/captain_test",
        with_n8n=False,
    )

    with pytest.raises(
        FactoryLiveConfigurationError,
        match="provider_cost_unresolved",
    ):
        run_factory_live_preflight(
            settings,
            probe=PreflightProbe(),
            adapter_factory=UnaccountedHermesFactory(),
        )

    assert not settings.output.exists()


def test_demo_report_is_exact_costed_redacted_and_content_addressed(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactoryLiveObservedEvidence,
        FactoryLivePreflightSettings,
        FactoryLiveProviderTrace,
        FactorySixSkillLiveResult,
        _build_live_report,
        _write_content_addressed_report,
    )

    job = workflow_job(mode="demo")
    report_directory = tmp_path / "external"
    report_directory.mkdir()
    settings = FactoryLivePreflightSettings(
        mode="demo",
        max_cost_usd=Decimal("1.00"),
        model="approved-model-id",
        repository_root=Path(__file__).resolve().parents[2],
        report_directory=report_directory,
        output=report_directory / "preflight.json",
        database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
    )
    runner_report = FactoryLiveRunReport(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        mode="demo",
        status="demo_ready",
        attempt=1,
        next_attempt=1,
    )
    result = FactorySixSkillLiveResult(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        mode="demo",
        status="demo_ready",
        attempt=1,
        skill_steps=(),
        runner_reports=(runner_report,),
        gateway_projection_status="running",
    )
    observed = FactoryLiveObservedEvidence(
        context7_provenance_digest="a" * 64,
        provider_traces=(
            FactoryLiveProviderTrace(
                trace_id="provider-trace-1",
                codex_session_id="codex-session-1",
                hermes_session_id="hermes-session-1",
                provider="openai",
                model="approved-model-id",
                status="succeeded",
                cost_usd="0.25",
                usage_receipt_ref=workflow_artifact("usage-live", "d" * 64),
                budget_receipt_ref=workflow_artifact("budget-live", "e" * 64),
            ),
        ),
        gateway_total_cost_usd="0.25",
    )

    report = _build_live_report(settings, job, result, observed)
    payload = report.model_dump(mode="json", by_alias=True, exclude_none=True)
    persisted = _write_content_addressed_report(report_directory, payload)

    assert payload["total_cost_usd"] == "0.25"
    assert payload["provider_traces"][0]["cost_usd"] == "0.25"
    assert "secret" not in json.dumps(payload)
    assert persisted.name == "sha256-" + __import__("hashlib").sha256(
        persisted.read_bytes()
    ).hexdigest() + ".json"


def test_provider_trace_rejects_unknown_float_cost() -> None:
    from agenten.agent_factory.factory_live_entrypoint import FactoryLiveProviderTrace

    with pytest.raises((TypeError, ValueError), match="provider_cost_unresolved"):
        FactoryLiveProviderTrace(
            trace_id="provider-trace-1",
            codex_session_id="codex-session-1",
            hermes_session_id="hermes-session-1",
            provider="openai",
            model="approved-model-id",
            status="succeeded",
            cost_usd=0.25,
            usage_receipt_ref=workflow_artifact("usage-live", "d" * 64),
            budget_receipt_ref=workflow_artifact("budget-live", "e" * 64),
        )


@pytest.mark.asyncio
async def test_environment_gate_uses_explicit_bindings_and_persists_one_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.factory_live_entrypoint as entrypoint

    job = workflow_job(mode="demo")
    repository = WorkflowRepository()
    lifecycle = ScriptedCoordinator(job, _coordinator_actions())
    timeline: list[str] = []
    dispatcher = ScriptedDispatcher(
        lifecycle,
        repository,
        required_live_runs=1,
        timeline=timeline,
    )
    live_runner = ScriptedLiveRunner(
        job,
        (
            ("blocked", (FactoryLiveEffectKind.CODEX,)),
            ("blocked", (FactoryLiveEffectKind.PROVIDER,)),
            ("demo_ready", ()),
        ),
        timeline=timeline,
    )
    coordinator = entrypoint.FactorySixSkillLiveCoordinator(
        coordinator=lifecycle,
        repository=repository,
        dispatcher=dispatcher,
        live_runner=live_runner,
        clock=lambda: job.occurred_at,
    )

    class Observer:
        async def observe(self, supplied_job, result):
            assert supplied_job == job
            assert result.status == "demo_ready"
            return entrypoint.FactoryLiveObservedEvidence(
                context7_provenance_digest="a" * 64,
                provider_traces=(
                    entrypoint.FactoryLiveProviderTrace(
                        trace_id="provider-trace-1",
                        codex_session_id="codex-session-1",
                        hermes_session_id="hermes-session-1",
                        provider="openai",
                        model="approved-model-id",
                        status="succeeded",
                        cost_usd="0.25",
                        usage_receipt_ref=workflow_artifact("usage-live", "d" * 64),
                        budget_receipt_ref=workflow_artifact("budget-live", "e" * 64),
                    ),
                ),
                gateway_total_cost_usd="0.25",
            )

    class RuntimeFactory(AdapterFactory):
        def build(self, settings, preflight):
            assert preflight.prerequisites_confirmed is True
            return entrypoint.FactoryLiveGateBindings(
                job=job,
                coordinator=coordinator,
                observer=Observer(),
            )

    report_directory = tmp_path / "external"
    report_directory.mkdir()
    preflight_path = report_directory / "preflight.json"
    settings = entrypoint.FactoryLivePreflightSettings(
        mode="demo",
        max_cost_usd="5.00",
        model="approved-model-id",
        repository_root=Path(__file__).resolve().parents[2],
        report_directory=report_directory,
        output=preflight_path,
        database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
    )
    entrypoint.run_factory_live_preflight(
        settings,
        probe=PreflightProbe(),
        adapter_factory=RuntimeFactory(),
    )
    monkeypatch.setattr(entrypoint, "_load_adapter_factory", RuntimeFactory)
    for name, value in {
        "CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED": "1",
        "CAPTAIN_FACTORY_GATE_MODE": "demo",
        "CAPTAIN_FACTORY_MAX_COST_USD": "5.00",
        "CAPTAIN_FACTORY_MODEL": "approved-model-id",
        "CAPTAIN_FACTORY_REPORT_DIRECTORY": str(report_directory),
        "CAPTAIN_FACTORY_PREFLIGHT_PATH": str(preflight_path),
        "CAPTAIN_FACTORY_WITH_N8N": "0",
        "TEST_MARIADB_DSN": "mysql://captain:secret@127.0.0.1:3306/captain_test",
    }.items():
        monkeypatch.setenv(name, value)

    payload = await entrypoint.run_factory_live_gate_from_environment()

    reports = tuple(report_directory.glob("sha256-*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8")) == payload
    assert payload["terminal_status"] == "demo_ready"


class WorkflowRepository:
    def __init__(self) -> None:
        self.artifacts: list[object] = []

    def workflow_artifacts(self, _job_id):
        return tuple(self.artifacts)


class ScriptedCoordinator:
    def __init__(self, job, actions: tuple[FactoryActionKind, ...]) -> None:
        self.job = job
        self.actions = actions
        self.index = 0
        self.blocks = []
        self.status = FactoryLifecycleStatus.RUNNING

    def next_action(self, _job_id):
        return FactoryAction(kind=self.actions[self.index], attempt=1)

    def projection(self, _job_id):
        return SimpleNamespace(
            job=self.job,
            attempt=1,
            status=self.status,
            phase=FactoryPhase.QUALITY_REVIEWED,
            workflow_evaluation_ref=self.job.compiled_spec_ref,
            feedback_ref=self.job.dependency_graph_ref,
        )

    def record(self, block):
        self.blocks.append(block)
        if block.phase is FactoryPhase.CAPABILITY_PROMOTED:
            self.status = FactoryLifecycleStatus.READY_TO_USE
        self.index += 1
        return True


class ScriptedDispatcher:
    _STEPS = {
        FactoryActionKind.DISPATCH_AGENT_ARCHITECT: (FactorySkillStep.DISCOVER,),
        FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: (FactorySkillStep.BRIEF_CODEX,),
        FactoryActionKind.DISPATCH_BUILD_VALIDATOR: (),
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER: (FactorySkillStep.EXECUTE_TEAM,),
        FactoryActionKind.DISPATCH_QUALITY_WARDEN: (
            FactorySkillStep.EVALUATE_TEAM,
            FactorySkillStep.REPORT_CAPTAIN,
        ),
        FactoryActionKind.SUBMIT_FORGE_JOB: (),
    }

    def __init__(
        self,
        coordinator: ScriptedCoordinator,
        repository: WorkflowRepository,
        *,
        required_live_runs: int,
        timeline: list[str],
        digest_valid: bool = True,
    ):
        self.coordinator = coordinator
        self.repository = repository
        self.required_live_runs = required_live_runs
        self.timeline = timeline
        self.digest_valid = digest_valid
        self.candidate_entrypoint_calls = 0
        self.dispatch_calls = 0

    def validate_next(self, job, action, expected_skill_digests):
        assert job == self.coordinator.job
        assert set(expected_skill_digests) == set(SKILLS)
        self.timeline.append(f"preflight:{action.kind.value}")
        if not self.digest_valid:
            raise ValueError("released Factory skill digest changed")
        return action

    async def dispatch_next(self, _job_id):
        action = self.coordinator.next_action(_job_id)
        self.dispatch_calls += 1
        self.timeline.append(f"dispatch:{action.kind.value}")
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            self.repository.artifacts.extend(
                workflow_run(number) for number in range(1, self.required_live_runs + 1)
            )
        else:
            for step in self._STEPS[action.kind]:
                self.repository.artifacts.append(
                    SimpleNamespace(invocation=SimpleNamespace(step=step))
                )
        self.coordinator.index += 1
        return action


class ScriptedLiveRunner:
    def __init__(self, job, scripts, *, timeline: list[str]) -> None:
        self.job = job
        self.scripts = list(scripts)
        self.calls = 0
        self.timeline = timeline

    async def run(self, supplied_job, *, mode):
        assert supplied_job == self.job
        self.calls += 1
        status, kinds = self.scripts.pop(0)
        self.timeline.append("claim:" + ",".join(kind.value for kind in kinds))
        decision_status = "ready" if status == "ready" else status
        if decision_status not in {"ready", "demo_ready"}:
            decision_status = "blocked"
        return FactoryLiveRunReport(
            job_id=self.job.job_id,
            correlation_id=self.job.correlation_id,
            mode=mode,
            status=status,
            attempt=1,
            next_attempt=1,
            effects=tuple(
                FactoryLiveEffectReport(
                    effect_id=UUID(int=self.calls * 10 + index),
                    kind=kind,
                    attempt=1,
                    status="succeeded",
                    evidence_ref=workflow_artifact(
                        f"live-effect-{self.calls}-{index}",
                        f"{self.calls}{index}".ljust(64, "0"),
                    ),
                    replayed=False,
                )
                for index, kind in enumerate(kinds, start=1)
            ),
            release_decision=FactoryReleaseDecision(
                job_id=self.job.job_id,
                correlation_id=self.job.correlation_id,
                status=decision_status,
                reasons=(),
                evaluation_id=(
                    self.job.event_id if decision_status == "ready" else None
                ),
                evaluation_ref=(
                    self.job.compiled_spec_ref if decision_status == "ready" else None
                ),
            ),
            reasons=(),
        )


def _coordinator_actions() -> tuple[FactoryActionKind, ...]:
    return (
        FactoryActionKind.APPEND_FORGE_REQUESTED,
        FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
        FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        FactoryActionKind.SUBMIT_FORGE_JOB,
        FactoryActionKind.DISPATCH_BUILD_VALIDATOR,
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        FactoryActionKind.DISPATCH_QUALITY_WARDEN,
        FactoryActionKind.VALIDATE_FOR_PROMOTION,
    )


@pytest.mark.asyncio
async def test_changed_skill_digest_fails_before_any_external_effect() -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactorySixSkillLiveCoordinator,
    )

    job = workflow_job(mode="demo")
    repository = WorkflowRepository()
    lifecycle = ScriptedCoordinator(job, _coordinator_actions())
    timeline: list[str] = []
    dispatcher = ScriptedDispatcher(
        lifecycle,
        repository,
        required_live_runs=1,
        timeline=timeline,
        digest_valid=False,
    )
    live_runner = ScriptedLiveRunner(job, (), timeline=timeline)
    coordinator = FactorySixSkillLiveCoordinator(
        coordinator=lifecycle,
        repository=repository,
        dispatcher=dispatcher,
        live_runner=live_runner,
        clock=lambda: job.occurred_at,
    )

    with pytest.raises(ValueError, match="digest changed"):
        await coordinator.run(job, "demo")

    assert dispatcher.dispatch_calls == 0
    assert live_runner.calls == 0
    assert not any(item.startswith("claim:") for item in timeline)


@pytest.mark.asyncio
async def test_six_skill_coordinator_runs_exact_steps_without_candidate_entrypoint() -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactorySixSkillLiveCoordinator,
    )

    job = workflow_job(mode="demo")
    repository = WorkflowRepository()
    lifecycle = ScriptedCoordinator(job, _coordinator_actions())
    timeline: list[str] = []
    dispatcher = ScriptedDispatcher(
        lifecycle,
        repository,
        required_live_runs=job.execution_policy.required_live_runs,
        timeline=timeline,
    )
    live_runner = ScriptedLiveRunner(
        job,
        (
            ("blocked", (FactoryLiveEffectKind.CODEX,)),
            ("blocked", (FactoryLiveEffectKind.PROVIDER,)),
            ("demo_ready", ()),
        ),
        timeline=timeline,
    )
    coordinator = FactorySixSkillLiveCoordinator(
        coordinator=lifecycle,
        repository=repository,
        dispatcher=dispatcher,
        live_runner=live_runner,
        clock=lambda: job.occurred_at,
    )

    result = await coordinator.run(job, "demo")

    assert result.status == "demo_ready"
    assert result.skill_steps == (
        FactorySkillStep.DISCOVER,
        FactorySkillStep.BRIEF_CODEX,
        FactorySkillStep.EXECUTE_TEAM,
        FactorySkillStep.EVALUATE_TEAM,
        FactorySkillStep.REPORT_CAPTAIN,
    )
    assert len(
        [artifact for artifact in repository.artifacts if hasattr(artifact, "run_number")]
    ) == 1
    assert result.promotion_block is None
    assert dispatcher.candidate_entrypoint_calls == 0
    assert live_runner.calls == 3
    assert timeline.index("claim:codex") < timeline.index(
        "dispatch:dispatch_tool_integrator"
    )
    assert timeline.index("claim:provider") < timeline.index(
        "dispatch:dispatch_real_case_tester"
    )


@pytest.mark.asyncio
async def test_release_coordinator_requires_recovery_then_promotes_and_rereads() -> None:
    from agenten.agent_factory.factory_live_entrypoint import (
        FactorySixSkillLiveCoordinator,
    )

    job = workflow_job(mode="release")
    repository = WorkflowRepository()
    lifecycle = ScriptedCoordinator(job, _coordinator_actions())
    timeline: list[str] = []
    dispatcher = ScriptedDispatcher(
        lifecycle,
        repository,
        required_live_runs=job.execution_policy.required_live_runs,
        timeline=timeline,
    )
    live_runner = ScriptedLiveRunner(
        job,
        (
            ("blocked", (FactoryLiveEffectKind.CODEX,)),
            ("infrastructure_recovery_required", ()),
            ("blocked", (FactoryLiveEffectKind.PROVIDER,) * 3),
            ("ready", ()),
        ),
        timeline=timeline,
    )
    coordinator = FactorySixSkillLiveCoordinator(
        coordinator=lifecycle,
        repository=repository,
        dispatcher=dispatcher,
        live_runner=live_runner,
        clock=lambda: job.occurred_at,
    )

    result = await coordinator.run(job, "release")

    assert result.status == "ready_to_use"
    assert result.gateway_projection_status == "ready_to_use"
    assert result.promotion_block is lifecycle.blocks[-1]
    assert result.promotion_block.phase is FactoryPhase.CAPABILITY_PROMOTED
    assert job.compiled_spec_ref in result.promotion_block.artifact_refs
    assert result.skill_steps.count(FactorySkillStep.EXECUTE_TEAM) == 1
    assert len(
        [artifact for artifact in repository.artifacts if hasattr(artifact, "run_number")]
    ) == 3
    assert live_runner.calls == 4
    assert timeline.index("claim:provider,provider,provider") < timeline.index(
        "dispatch:dispatch_real_case_tester"
    )
