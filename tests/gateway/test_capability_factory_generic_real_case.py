from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
    GenericBenchmarkQualityEvidenceV1,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef, CapabilityProfile, IntegrationIntent
from gateway.capability_factory_generic_real_case import GatewayD10GenericCandidateValidator


def _ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://sha256/{digest}", sha256=digest,
        media_type="application/json",
    )


class _BuildValidator:
    def __init__(self, capability: str) -> None:
        self.capability = capability

    async def dispatch(self, _request):
        raise AssertionError("legacy build validator reached real-case dispatch")

    def candidate_for(self, _observed):
        return SimpleNamespace(candidate=SimpleNamespace(
            source_archive_ref=_ref(self.capability + "-candidate"),
            team_manifest=SimpleNamespace(reference=_ref(self.capability + "-team")),
            workflow_artifacts=(), tool_schema_artifacts=(),
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability",
    ("sales_pipeline", "incident_command", "claims_resolution"),
)
async def test_d10_real_case_admits_all_three_generic_inputs_without_legacy_tool(
    capability: str,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=timezone.utc)
    job_id, correlation_id = uuid4(), uuid4()
    assertions = (f"{capability}_correct", "safe_escalation")
    job = AgentFactoryJobV3.model_construct(
        job_id=job_id, event_id=uuid4(), correlation_id=correlation_id,
        causation_id=None, occurred_at=now, producer="captain",
        subject_version=1, input_ref=_ref(capability),
        compiled_spec_ref=_ref(capability + "-spec"),
        dependency_graph_ref=_ref(capability + "-graph"),
        required_capability=capability, acceptance_assertion_ids=assertions,
        private_holdout_refs=(), max_behavioral_iterations=5,
        deadline_at=now + timedelta(minutes=20),
        execution_policy=SimpleNamespace(
            live_execution=True, live_capabilities=(),
        ),
    )
    categories = (
        "normal", "boundary", "missing", "conflicting", "mandatory_escalation",
    ) * 3
    quality = GenericBenchmarkQualityEvidenceV1(
        schema="captain.generic-benchmark-quality-evidence.v1",
        authority_id=uuid4(), job_id=job_id, correlation_id=correlation_id,
        subject_version=1, attempt=1, suite_ref=_ref(capability + "-suite"),
        summary_ref=_ref(capability + "-summary"),
        case_ids=tuple(f"{capability}-case-{index}" for index in range(15)),
        case_digests=tuple(hashlib.sha256(f"{capability}-{index}".encode()).hexdigest() for index in range(15)),
        categories=categories,
        acceptance_assertion_ids=assertions,
        evaluation_refs=tuple(_ref(f"{capability}-evaluation-{index}") for index in range(30)),
        passed=True,
    )
    store = SimpleNamespace(
        generic_benchmark_quality_evidence=lambda observed, attempt: (
            quality if (observed, attempt) == (job_id, 1) else None
        )
    )
    delegate = _BuildValidator(capability)
    lease = issue_factory_lease(
        job=job, role=FactoryRole.REAL_CASE_TESTER, attempt=1,
        workspace_ref=f"workspace://factory/{job_id}", now=now,
    )
    request = FactoryDispatch(
        job=job,
        action=FactoryAction(kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER, attempt=1),
        role=FactoryRole.REAL_CASE_TESTER, lease=lease,
    )

    block = await GatewayD10GenericCandidateValidator(
        gateway_store=store, build_validator=delegate,
    ).dispatch(request)

    assert block.phase is FactoryPhase.REAL_CASE_EVIDENCE
    assert block.assertion_ids == assertions
    assert quality.summary_ref in block.evidence_refs
    assert len(block.evidence_refs) == 31
    assert "captain_business_decision" not in block.model_dump_json()


@pytest.mark.asyncio
async def test_d10_real_case_fails_closed_without_quality_before_delegate_effect() -> None:
    calls: list[str] = []
    validator = GatewayD10GenericCandidateValidator(
        gateway_store=SimpleNamespace(
            generic_benchmark_quality_evidence=lambda *_args: None
        ),
        build_validator=SimpleNamespace(
            dispatch=lambda *_args: calls.append("dispatch"),
            candidate_for=lambda *_args: calls.append("candidate"),
        ),
    )
    request = SimpleNamespace(
        action=SimpleNamespace(
            kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER, attempt=1,
        ),
        job=SimpleNamespace(), role=FactoryRole.REAL_CASE_TESTER, lease=None,
    )

    with pytest.raises(Exception, match="D10 generic quality evidence is unavailable"):
        await validator.dispatch(request)
    assert calls == []
