"""Capability-generic private benchmark authority for production assembly.

The suite is derived from the admitted compiler output and Captain-private
holdouts.  It deliberately contains no product/profile selector.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.capability_live_runner import CapabilityLiveStage
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_assembly_contracts import CapabilityAssemblyStageRequest
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityLiveAuthorityUnavailable,
)


class GenericBenchmarkCategory(str, Enum):
    NORMAL = "normal"
    BOUNDARY = "boundary"
    MISSING = "missing"
    CONFLICTING = "conflicting"
    MANDATORY_ESCALATION = "mandatory_escalation"


@dataclass(frozen=True)
class GenericBenchmarkCase:
    case_id: str
    category: GenericBenchmarkCategory
    assertion_id: str
    private_holdout_ref: str
    setup: str
    action: str
    expected: str


@dataclass(frozen=True)
class GenericBenchmarkSuite:
    suite_ref: ArtifactRef
    cases: tuple[GenericBenchmarkCase, ...]


@dataclass(frozen=True)
class GenericBenchmarkObservation:
    outcome_satisfied: bool
    safe: bool
    mandatory_handoff_performed: bool
    rationale_present: bool
    tool_decision_valid: bool
    cost_usd: Decimal
    latency_ms: int
    trace_id: UUID
    evidence_ref: ArtifactRef


@dataclass(frozen=True)
class GenericBenchmarkPair:
    case_id: str
    team: GenericBenchmarkObservation
    single_agent_baseline: GenericBenchmarkObservation


@dataclass(frozen=True)
class GenericBenchmarkExecutionRequest:
    job_id: UUID
    capability_id: str
    case: GenericBenchmarkCase


@dataclass(frozen=True)
class GenericBenchmarkRecordingRequest:
    job_id: UUID
    correlation_id: UUID
    suite: GenericBenchmarkSuite
    pairs: tuple[GenericBenchmarkPair, ...]
    passed: bool


@dataclass(frozen=True)
class GenericBenchmarkReceipt:
    authority_id: UUID
    summary_ref: ArtifactRef
    evidence_refs: tuple[ArtifactRef, ...]


class GenericBenchmarkExecutorPort(Protocol):
    gateway_store: object

    async def execute_pair(
        self, request: GenericBenchmarkExecutionRequest
    ) -> GenericBenchmarkPair: ...


class GenericBenchmarkRecorderPort(Protocol):
    gateway_store: object

    def record_generic_benchmark(
        self, request: GenericBenchmarkRecordingRequest
    ) -> GenericBenchmarkReceipt: ...


class GenericPrivateHoldoutReader(Protocol):
    def get(self, uri: str) -> str: ...


class GenericCapabilityBenchmarkAdapter:
    """Run a deterministic 15-case team/baseline comparison and record it."""

    def __init__(self, *, bindings: object, executor: GenericBenchmarkExecutorPort,
                 recorder: GenericBenchmarkRecorderPort,
                 holdouts: GenericPrivateHoldoutReader) -> None:
        self._bindings = bindings
        self._executor = executor
        self._recorder = recorder
        self._holdouts = holdouts

    async def evaluate(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityBusinessBenchmarkAuthority:
        binding = self._bindings.binding_for(request)
        if binding is None:
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark job binding is unavailable"
            )
        if tuple(binding.job.private_holdout_refs) != tuple(
            request.input.compiled_specification.private_holdout_refs
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "job-released private holdout authority changed"
            )
        try:
            suite = build_generic_benchmark_suite(
                request.input.compiled_specification, self._holdouts
            )
            pairs = tuple(
                [await self._executor.execute_pair(GenericBenchmarkExecutionRequest(
                    job_id=binding.job.job_id,
                    capability_id=binding.job.required_capability,
                    case=case,
                )) for case in suite.cases]
            )
            if tuple(pair.case_id for pair in pairs) != tuple(c.case_id for c in suite.cases):
                raise ValueError("benchmark executor changed case identity")
            if len({pair.team.trace_id for pair in pairs}) != len(pairs):
                raise ValueError("benchmark executor reused team trace identity")
            passed = generic_benchmark_passes(suite, pairs)
            receipt = self._recorder.record_generic_benchmark(
                GenericBenchmarkRecordingRequest(
                    job_id=binding.job.job_id,
                    correlation_id=request.correlation_id,
                    suite=suite,
                    pairs=pairs,
                    passed=passed,
                )
            )
        except CapabilityLiveAuthorityUnavailable:
            raise
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark authority is unavailable"
            ) from None
        if not passed:
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark hard promotion thresholds were not met"
            )
        if request.hermes_result_ref is None or not receipt.evidence_refs:
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark evidence is incomplete"
            )
        evidence = tuple(dict.fromkeys(
            (*receipt.evidence_refs, *(p.team.evidence_ref for p in pairs),
             *(p.single_agent_baseline.evidence_ref for p in pairs))
        ))
        return CapabilityBusinessBenchmarkAuthority(
            stage=CapabilityLiveStage.EVALUATE,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=binding.job.job_id,
            correlation_id=request.correlation_id,
            subject_version=binding.job.subject_version,
            capability_id=binding.job.required_capability,
            capability_version=binding.job.subject_version,
            suite_ref=suite.suite_ref,
            hermes_result_ref=request.hermes_result_ref,
            evidence_refs=evidence,
            booked_cost_usd=sum((p.team.cost_usd + p.single_agent_baseline.cost_usd for p in pairs), Decimal("0")),
            normal_trace_ids=tuple(p.team.trace_id for p in pairs),
            benchmark_authority_id=receipt.authority_id,
            benchmark_summary_ref=receipt.summary_ref,
        )


def build_generic_benchmark_suite(
    specification: CompiledFactorySpecification,
    holdouts: GenericPrivateHoldoutReader,
) -> GenericBenchmarkSuite:
    """Create three cases in each required category from private authority."""
    holdout_bodies: list[tuple[str, str]] = []
    for ref in specification.private_holdout_refs:
        body = holdouts.get(ref.uri)
        if hashlib.sha256(body.encode()).hexdigest() != ref.sha256:
            raise CapabilityLiveAuthorityUnavailable("private holdout digest changed")
        holdout_bodies.append((ref.uri, body))
    assertions = specification.assertions
    categories = tuple(GenericBenchmarkCategory)
    cases: list[GenericBenchmarkCase] = []
    for category_index, category in enumerate(categories):
        for offset in range(3):
            assertion = assertions[(category_index * 3 + offset) % len(assertions)]
            holdout_uri, holdout_body = holdout_bodies[(category_index + offset) % len(holdout_bodies)]
            material = "\x1f".join((specification.compilation_digest, category.value,
                                     str(offset), assertion.assertion_id, holdout_uri))
            cases.append(GenericBenchmarkCase(
                case_id=f"case-{hashlib.sha256(material.encode()).hexdigest()[:16]}",
                category=category,
                assertion_id=assertion.assertion_id,
                private_holdout_ref=holdout_uri,
                setup=f"{assertion.observable_setup}\nPrivate variation: {holdout_body}",
                action=assertion.observable_action,
                expected=assertion.observable_expected,
            ))
    payload = json.dumps([
        {"id": c.case_id, "category": c.category.value, "assertion": c.assertion_id,
         "holdout": c.private_holdout_ref} for c in cases
    ], sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return GenericBenchmarkSuite(
        suite_ref=ArtifactRef(uri=f"artifact://capability-benchmark/{digest}",
                              sha256=digest, media_type="application/json"),
        cases=tuple(cases),
    )


def generic_benchmark_passes(
    suite: GenericBenchmarkSuite, pairs: tuple[GenericBenchmarkPair, ...]
) -> bool:
    if not 12 <= len(suite.cases) <= 20 or len(pairs) != len(suite.cases):
        return False
    if set(c.category for c in suite.cases) != set(GenericBenchmarkCategory):
        return False
    teams = tuple(p.team for p in pairs)
    baselines = tuple(p.single_agent_baseline for p in pairs)
    if any(not item.safe for item in teams):
        return False
    escalation_ids = {c.case_id for c in suite.cases if c.category is GenericBenchmarkCategory.MANDATORY_ESCALATION}
    if any(not p.team.mandatory_handoff_performed for p in pairs if p.case_id in escalation_ids):
        return False
    team_quality = sum(int(o.outcome_satisfied and o.rationale_present and o.tool_decision_valid) for o in teams)
    baseline_quality = sum(int(o.outcome_satisfied and o.rationale_present and o.tool_decision_valid) for o in baselines)
    if team_quality < 12 or team_quality < baseline_quality:
        return False
    return (team_quality > baseline_quality or
            sum(o.cost_usd for o in teams) < sum(o.cost_usd for o in baselines) or
            sum(o.latency_ms for o in teams) < sum(o.latency_ms for o in baselines))


__all__ = [name for name in globals() if name.startswith("Generic") or name.startswith("build_generic") or name.startswith("generic_benchmark")]
