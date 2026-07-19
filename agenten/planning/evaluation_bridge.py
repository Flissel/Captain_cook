"""Deterministically turn Captain-approved LLM evaluation evidence into work batches."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Iterable

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentOutcome,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationStatus,
)
from agenten.planning.captain_pipeline import CaptainCompiledPlan, CaptainPipeline
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


class EvaluationBridgeError(ValueError):
    """Evaluation evidence cannot safely become Captain execution work."""


@dataclass(frozen=True)
class EvaluationBridgePolicy:
    """Captain-owned execution policy; no capability is inferred from model text."""

    target: str
    capability_tags: tuple[str, ...]
    allowed_targets: frozenset[str]
    allowed_capability_tags: frozenset[str]

    def validate(self) -> None:
        if self.target not in self.allowed_targets:
            raise EvaluationBridgeError(f"target {self.target!r} is not Captain-approved")
        if len(self.capability_tags) != len(set(self.capability_tags)):
            raise EvaluationBridgeError("duplicate capability tags are not allowed")
        unknown = sorted(set(self.capability_tags) - self.allowed_capability_tags)
        if unknown:
            raise EvaluationBridgeError(f"unknown capability tags: {unknown}")


def compile_accepted_evaluation(
    manifest: EvaluationManifest,
    *,
    policy: EvaluationBridgePolicy,
) -> CaptainCompiledPlan:
    """Compile only persisted QA-approved proposals into a versioned WorkBatch DAG."""

    policy.validate()
    if manifest.status is not EvaluationStatus.ACCEPTED:
        raise EvaluationBridgeError("only an accepted evaluation manifest may be released")
    outcomes = tuple(manifest.component_outcomes)
    if not outcomes:
        raise EvaluationBridgeError("accepted evaluation has no component outcomes")
    accepted = {outcome.component_key: _accepted_candidate(outcome) for outcome in outcomes}
    batch_ids = {
        component_key: _batch_id(manifest.source.sha256, component_key)
        for component_key in accepted
    }
    ordered_keys = _dependency_order(accepted.values())
    batches: list[WorkBatch] = []
    holdouts: list[HoldoutSuite] = []
    for component_key in ordered_keys:
        outcome = accepted[component_key]
        candidate = outcome.candidate
        assert candidate is not None  # established by _accepted_candidate
        batch_id = batch_ids[component_key]
        assertions = [_assertion(batch_id, test) for test in candidate.acceptance_tests]
        constraints = [
            f"Evaluation source SHA-256: {manifest.source.sha256}",
            f"Evaluation component: {component_key} revision {outcome.revision}",
            f"Source citations: {', '.join(candidate.source_citations)}",
            "Planning evidence is not execution evidence; run acceptance tests in the fenced worker.",
            *candidate.non_goals,
            *candidate.risks,
        ]
        batches.append(
            WorkBatch(
                batch_id=batch_id,
                title=component_key,
                goal="\n".join((*candidate.scope, *candidate.implementation_steps, *candidate.definition_of_done)),
                subtask_ids=[_subtask_id(batch_id)],
                target=policy.target,
                runtime=policy.target,
                runtime_version="v1",
                interface_schema=f"captain-{policy.target}-artifact/v1",
                capability_tags=sorted(policy.capability_tags),
                depends_on=[batch_ids[item] for item in candidate.dependencies],
                constraints=constraints,
                acceptance_criteria=assertions,
                golden_cases=[_golden_case(batch_id, test) for test in candidate.acceptance_tests],
            )
        )
        holdouts.append(
            HoldoutSuite(
                batch_id=batch_id,
                cases=[
                    ExampleCase(
                        case_id=_holdout_id(batch_id),
                        input={"batch_id": batch_id, "kind": "captain-evaluation-holdout"},
                        expected_observations={"source_sha256": manifest.source.sha256},
                    )
                ],
            )
        )
    return CaptainCompiledPlan(batches=tuple(batches), holdouts=tuple(holdouts))


async def release_accepted_evaluation(
    manifest: EvaluationManifest,
    *,
    policy: EvaluationBridgePolicy,
    pipeline: CaptainPipeline,
    run_id: str,
) -> CaptainCompiledPlan:
    """Compile and checkpoint an accepted LLM plan before the gateway release port runs."""

    compiled = compile_accepted_evaluation(manifest, policy=policy)
    return await pipeline.release_compiled_checkpoint(
        compiled,
        source_digest=_manifest_digest(manifest),
        run_id=run_id,
    )


def _accepted_candidate(outcome: ComponentOutcome) -> ComponentOutcome:
    if (
        outcome.outcome is not EvaluationOutcome.ACCEPTED
        or outcome.candidate is None
        or outcome.review is None
        or outcome.review.decision != "approved"
        or outcome.review.component_key != outcome.component_key
        or outcome.review.revision != outcome.revision
    ):
        raise EvaluationBridgeError(
            f"component {outcome.component_key!r} is not a persisted accepted QA outcome"
        )
    return outcome


def _dependency_order(outcomes: Iterable[ComponentOutcome]) -> list[str]:
    by_key = {outcome.component_key: outcome for outcome in outcomes}
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(component_key: str) -> None:
        if component_key in visited:
            return
        if component_key in visiting:
            raise EvaluationBridgeError("evaluation dependencies contain a cycle")
        outcome = by_key.get(component_key)
        if outcome is None:
            raise EvaluationBridgeError(f"dependency references unknown component {component_key!r}")
        candidate = outcome.candidate
        assert candidate is not None
        visiting.add(component_key)
        for dependency in candidate.dependencies:
            visit(dependency)
        visiting.remove(component_key)
        visited.add(component_key)
        ordered.append(component_key)

    for component_key in by_key:
        visit(component_key)
    return ordered


def _batch_id(source_digest: str, component_key: str) -> str:
    slug = _slug(component_key, fallback="component")
    suffix = hashlib.sha256(f"{source_digest}:{component_key}".encode()).hexdigest()[:8]
    return f"ev-{slug[:20]}-{suffix}"


def _subtask_id(batch_id: str) -> str:
    return f"sub-{batch_id[3:]}"[:64]


def _holdout_id(batch_id: str) -> str:
    return f"holdout-{batch_id[3:]}"[:64]


def _assertion(batch_id: str, test: AcceptanceTestPlan) -> AcceptanceAssertion:
    assertion_id = f"assert-{_slug(test.test_id, fallback='test')[:40]}-{_short_hash(test.test_id)}"
    return AcceptanceAssertion(
        assertion_id=assertion_id,
        kind=AssertionKind.STATUS_EQUALS,
        expected="passed",
        description=(
            f"{test.test_type} test: setup={test.setup}; action={test.action}; "
            f"expected={test.expected}; command={test.command}; batch={batch_id}"
        ),
    )


def _golden_case(batch_id: str, test: AcceptanceTestPlan) -> ExampleCase:
    return ExampleCase(
        case_id=f"golden-{_short_hash(f'{batch_id}:{test.test_id}')}",
        input={"command": test.command, "test_id": test.test_id},
        expected_observations={"expected": test.expected},
    )


def _slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _manifest_digest(manifest: EvaluationManifest) -> str:
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
