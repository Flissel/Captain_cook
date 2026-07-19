import hashlib

import pytest
from pydantic import ValidationError

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationSource,
    QaReview,
    SourceBlock,
    ValidationIssue,
)
from agenten.evaluation.validation import (
    validate_candidate,
    validate_component_graph,
    validate_inventory,
)


def _source() -> EvaluationSource:
    text = "# Delivery\nBuild the component."
    return EvaluationSource(
        source_reference="inputs/project.md",
        sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        blocks=(
            SourceBlock(
                block_id="block-0001",
                heading_path=("Delivery",),
                line_start=1,
                line_end=2,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
            ),
        ),
    )


def _candidate(**changes: object) -> ComponentPlanCandidate:
    values: dict[str, object] = {
        "component_key": "delivery-api",
        "scope": ("Deliver the API boundary.",),
        "non_goals": ("Do not deploy services.",),
        "team_roles": ("Delivery Builder",),
        "implementation_steps": ("Add the deterministic adapter.",),
        "interfaces": ("POST /deliveries",),
        "acceptance_tests": (
            AcceptanceTestPlan(
                test_id="delivery-api-unit-001",
                test_type="unit",
                setup="Create the adapter.",
                action="Submit a delivery.",
                expected="The adapter returns a typed result.",
                command="python -m pytest -q tests/evaluation",
            ),
        ),
        "definition_of_done": ("Validation passes.",),
        "risks": ("Interface drift.",),
        "dependencies": (),
        "source_citations": ("block-0001",),
        "claims": (),
        "qa_reviews": (),
    }
    values.update(changes)
    return ComponentPlanCandidate(**values)


def _inventory(*components: ComponentPlanCandidate, citations: tuple[str, ...] = ("block-0001",)) -> ComponentInventoryCandidate:
    return ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=_source(),
        source_citations=citations,
        components=components,
    )


def test_candidate_reports_a_missing_expected_test_oracle() -> None:
    candidate = _candidate(
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id="delivery-api-unit-001",
                test_type="unit",
                setup="Create the adapter.",
                action="Submit a delivery.",
                expected="",
                command="python -m pytest -q tests/evaluation",
            ),
        ),
    )

    assert [issue.code for issue in validate_candidate(candidate, _source())] == ["missing_expected"]


def test_candidate_reports_an_invalid_qa_approval() -> None:
    candidate = _candidate(
        qa_reviews=(
            QaReview(
                component_key="delivery-api",
                revision=1,
                decision="approved",
                score=6,
                defect_codes=(),
                revision_requests=(),
            ),
        ),
    )

    assert [issue.code for issue in validate_candidate(candidate, _source())] == ["invalid_qa_approval"]


def test_candidate_reports_an_unknown_rubric_code() -> None:
    candidate = _candidate(
        qa_reviews=(
            QaReview(
                component_key="delivery-api",
                revision=1,
                decision="revision_required",
                score=4,
                defect_codes=("invented_code",),
                revision_requests=("Use a registered defect code.",),
            ),
        ),
    )

    assert [issue.code for issue in validate_candidate(candidate, _source())] == ["unknown_rubric_code"]


def test_inventory_reports_a_missing_source_citation() -> None:
    inventory = _inventory(_candidate(source_citations=("block-9999",)), citations=("block-9999",))

    assert [issue.code for issue in validate_inventory(inventory)] == [
        "missing_source_citation",
        "missing_source_citation",
    ]


def test_inventory_reports_a_duplicate_component_key() -> None:
    inventory = _inventory(_candidate(), _candidate())

    assert [issue.code for issue in validate_inventory(inventory)] == ["duplicate_component_key"]


def test_component_graph_reports_an_unknown_dependency() -> None:
    candidate = _candidate(dependencies=("unknown-component",))

    assert [issue.code for issue in validate_component_graph((candidate,))] == ["unknown_dependency"]


def test_component_graph_reports_a_direct_cycle() -> None:
    first = _candidate(component_key="first", dependencies=("second",))
    second = _candidate(component_key="second", dependencies=("first",))

    assert [issue.code for issue in validate_component_graph((first, second))] == ["dependency_cycle"]


def test_component_graph_accepts_a_valid_dag() -> None:
    first = _candidate(component_key="first")
    second = _candidate(component_key="second", dependencies=("first",))

    assert validate_component_graph((first, second)) == ()


def test_candidate_reports_a_false_execution_claim() -> None:
    candidate = _candidate(claims=("Acceptance tests ran successfully.", "Implementation is complete."))

    assert [issue.code for issue in validate_candidate(candidate, _source())] == ["false_execution_claim"]


@pytest.mark.parametrize(
    "claim",
    (
        "Acceptance tests were executed.",
        "All acceptance tests pass.",
        "Implementation completed.",
    ),
)
def test_candidate_reports_reviewed_false_execution_claims(claim: str) -> None:
    assert [issue.code for issue in validate_candidate(_candidate(claims=(claim,)), _source())] == [
        "false_execution_claim"
    ]


def test_new_contracts_reject_coercible_wrong_types() -> None:
    with pytest.raises(ValidationError):
        AcceptanceTestPlan(
            test_id=1,
            test_type="unit",
            setup="Create the adapter.",
            action="Submit a delivery.",
            expected="The adapter returns a typed result.",
            command="python -m pytest -q tests/evaluation",
        )
    with pytest.raises(ValidationError):
        QaReview(
            component_key="delivery-api",
            revision="1",
            decision="approved",
            score=7,
            defect_codes=(),
            revision_requests=(),
        )
    with pytest.raises(ValidationError):
        _candidate(component_key=1)
    with pytest.raises(ValidationError):
        ComponentInventoryCandidate(
            inventory_id=1,
            source=_source(),
            source_citations=("block-0001",),
            components=(_candidate(),),
        )
    with pytest.raises(ValidationError):
        ValidationIssue(code=1, message="A deterministic finding.")


def test_component_graph_preserves_duplicate_candidate_edges() -> None:
    first = _candidate(component_key="shared", dependencies=("missing",))
    second = _candidate(component_key="shared", dependencies=("other",))
    other = _candidate(component_key="other", dependencies=("shared",))

    assert [issue.code for issue in validate_component_graph((first, second, other))] == [
        "duplicate_component_key",
        "unknown_dependency",
        "dependency_cycle",
    ]
