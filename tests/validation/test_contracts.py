import pytest
from pydantic import ValidationError

from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


def test_work_batch_is_build_visible_and_cannot_contain_holdouts() -> None:
    batch = WorkBatch(
        batch_id="lead-intake",
        title="Build lead intake",
        goal="Accept and classify a lead",
        subtask_ids=["sub-1"],
        target="workflow",
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="route-hot",
                kind=AssertionKind.OUTPUT_EQUALS,
                path="route",
                expected="hot",
            )
        ],
        golden_cases=[ExampleCase(case_id="golden-1", input={"score": 95})],
    )

    assert "holdout" not in batch.model_dump()
    with pytest.raises(ValidationError):
        WorkBatch.model_validate({**batch.model_dump(), "holdout_cases": []})


def test_holdouts_are_a_separate_batch_bound_contract() -> None:
    suite = HoldoutSuite(
        batch_id="lead-intake",
        cases=[ExampleCase(case_id="hidden-1", input={"score": 82})],
    )

    assert suite.batch_id == "lead-intake"
    assert suite.cases[0].case_id == "hidden-1"


@pytest.mark.parametrize("batch_id", ["UPPER", "spaces here", "a" * 33])
def test_batch_id_rejects_values_outside_the_public_slug_contract(batch_id: str) -> None:
    with pytest.raises(ValidationError):
        WorkBatch(
            batch_id=batch_id,
            title="Invalid",
            goal="Invalid identifier",
            subtask_ids=["sub-1"],
            target="workflow",
            acceptance_criteria=[],
        )


def test_assertion_requires_an_expected_value_for_value_comparisons() -> None:
    with pytest.raises(ValidationError):
        AcceptanceAssertion(
            assertion_id="missing-expected",
            kind=AssertionKind.OUTPUT_EQUALS,
            path="route",
        )


def test_work_batch_rejects_duplicate_subtasks_and_dependencies() -> None:
    with pytest.raises(ValidationError):
        WorkBatch(
            batch_id="duplicate-input",
            title="Invalid duplicates",
            goal="Reject ambiguous planning output",
            subtask_ids=["sub-1", "sub-1"],
            depends_on=["other", "other"],
            target="workflow",
            acceptance_criteria=[],
        )
