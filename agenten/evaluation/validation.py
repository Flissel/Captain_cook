"""Deterministic validation for source-bound evaluation candidates."""

from __future__ import annotations

import re
from graphlib import CycleError, TopologicalSorter

from .models import ComponentInventoryCandidate, ComponentPlanCandidate, EvaluationSource, ValidationIssue


_RUBRIC_CODES = frozenset(
    {
        "missing_citation",
        "duplicate_scope",
        "unknown_dependency",
        "dependency_cycle",
        "incomplete_implementation",
        "missing_test",
        "weak_test_oracle",
        "wrong_test_level",
        "false_execution_claim",
    }
)
_FALSE_EXECUTION_CLAIM = re.compile(
    r"\b(?:acceptance\s+tests?\s+(?:(?:were\s+)?(?:executed|run)|have\s+run|ran|passed|pass)|all\s+acceptance\s+tests?\s+pass|implementation\s+(?:(?:is\s+)?complete(?:d)?|completed))\b",
    re.IGNORECASE,
)


def validate_inventory(inventory: ComponentInventoryCandidate) -> tuple[ValidationIssue, ...]:
    """Return source, uniqueness, candidate, and dependency findings in stable order."""

    issues = list(_validate_citations(inventory.source_citations, inventory.source, None))
    for candidate in inventory.components:
        issues.extend(validate_candidate(candidate, inventory.source))
    issues.extend(validate_component_graph(inventory.components))
    return tuple(issues)


def validate_candidate(
    candidate: ComponentPlanCandidate,
    source: EvaluationSource,
) -> tuple[ValidationIssue, ...]:
    """Validate one plan proposal without creating a released work batch."""

    issues: list[ValidationIssue] = []
    required_fields = {
        "scope": candidate.scope,
        "non_goals": candidate.non_goals,
        "team_roles": candidate.team_roles,
        "implementation_steps": candidate.implementation_steps,
        "interfaces": candidate.interfaces,
        "acceptance_tests": candidate.acceptance_tests,
        "definition_of_done": candidate.definition_of_done,
        "risks": candidate.risks,
        "source_citations": candidate.source_citations,
    }
    for field_name, values in required_fields.items():
        if not values or any(not value.strip() for value in values if isinstance(value, str)):
            issues.append(_issue(f"missing_{field_name}", f"{field_name} must be non-empty", candidate.component_key))
    for test_plan in candidate.acceptance_tests:
        for field_name in ("setup", "action", "expected", "command"):
            if not getattr(test_plan, field_name).strip():
                issues.append(_issue(f"missing_{field_name}", f"test {field_name} must be non-empty", candidate.component_key))
    issues.extend(_validate_citations(candidate.source_citations, source, candidate.component_key))
    for review in candidate.qa_reviews:
        if review.component_key != candidate.component_key:
            issues.append(_issue("qa_component_mismatch", "QA review must name its candidate", candidate.component_key))
        if review.decision == "approved" and (review.score < 7 or review.defect_codes):
            issues.append(_issue("invalid_qa_approval", "approved QA requires score seven and no defects", candidate.component_key))
        if review.decision == "revision_required" and not _has_actionable_request(review.revision_requests):
            issues.append(_issue("missing_revision_request", "revision-required QA needs an actionable request", candidate.component_key))
        if any(code not in _RUBRIC_CODES for code in review.defect_codes):
            issues.append(_issue("unknown_rubric_code", "QA defects must use registered rubric codes", candidate.component_key))
    if any(_FALSE_EXECUTION_CLAIM.search(claim) for claim in candidate.claims):
        issues.append(_issue("false_execution_claim", "candidate must not claim execution or completion", candidate.component_key))
    return tuple(issues)


def validate_component_graph(
    candidates: tuple[ComponentPlanCandidate, ...],
) -> tuple[ValidationIssue, ...]:
    """Validate known dependencies and DAG structure with ``TopologicalSorter``."""

    vertices_by_key: dict[str, list[str]] = {}
    vertices: list[tuple[ComponentPlanCandidate, str]] = []
    for index, candidate in enumerate(candidates):
        vertex = f"{candidate.component_key}\x00{index:04d}"
        vertices_by_key.setdefault(candidate.component_key, []).append(vertex)
        vertices.append((candidate, vertex))

    graph: dict[str, set[str]] = {}
    issues: list[ValidationIssue] = []
    seen_keys: set[str] = set()
    for candidate, _ in vertices:
        if candidate.component_key in seen_keys:
            issues.append(_issue("duplicate_component_key", "component keys must be unique", candidate.component_key))
        seen_keys.add(candidate.component_key)
    for candidate, vertex in vertices:
        known_dependencies: set[str] = set()
        for dependency in candidate.dependencies:
            dependency_vertices = vertices_by_key.get(dependency)
            if dependency_vertices is None:
                issues.append(_issue("unknown_dependency", f"unknown dependency: {dependency}", candidate.component_key))
            else:
                known_dependencies.update(dependency_vertices)
        graph[vertex] = known_dependencies
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError:
        issues.append(_issue("dependency_cycle", "component dependencies must form a DAG", None))
    return tuple(issues)


def _validate_citations(
    citations: tuple[str, ...],
    source: EvaluationSource,
    component_key: str | None,
) -> tuple[ValidationIssue, ...]:
    block_ids = {block.block_id for block in source.blocks}
    return tuple(
        _issue("missing_source_citation", f"source block does not exist: {citation}", component_key)
        for citation in citations
        if citation not in block_ids
    )


def _has_actionable_request(requests: tuple[str, ...]) -> bool:
    return any(len(request.strip()) >= 3 for request in requests)


def _issue(code: str, message: str, component_key: str | None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, component_key=component_key)
