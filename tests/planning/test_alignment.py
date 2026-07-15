import pytest

from agenten.planning.alignment import (
    AlignmentError,
    AlignmentPlan,
    BatchDraft,
    validate_alignment,
)


def test_alignment_accepts_exact_acyclic_subtask_coverage() -> None:
    plan = AlignmentPlan(
        batches=[
            BatchDraft(batch_id="foundation", title="Foundation", subtask_ids=["s1"]),
            BatchDraft(
                batch_id="delivery",
                title="Delivery",
                subtask_ids=["s2", "s3"],
                depends_on=["foundation"],
            ),
        ]
    )

    ordered = validate_alignment(plan, expected_subtask_ids=["s1", "s2", "s3"])

    assert [batch.batch_id for batch in ordered] == ["foundation", "delivery"]


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        (
            AlignmentPlan(
                batches=[BatchDraft(batch_id="only", title="Only", subtask_ids=["s1"])]
            ),
            "missing subtask ids: ['s2']",
        ),
        (
            AlignmentPlan(
                batches=[
                    BatchDraft(batch_id="one", title="One", subtask_ids=["s1"]),
                    BatchDraft(batch_id="two", title="Two", subtask_ids=["s1", "s2"]),
                ]
            ),
            "duplicate subtask ids: ['s1']",
        ),
        (
            AlignmentPlan(
                batches=[
                    BatchDraft(
                        batch_id="one",
                        title="One",
                        subtask_ids=["s1", "s2"],
                        depends_on=["unknown"],
                    )
                ]
            ),
            "unknown dependencies: ['unknown']",
        ),
        (
            AlignmentPlan(
                batches=[
                    BatchDraft(
                        batch_id="one",
                        title="One",
                        subtask_ids=["s1"],
                        depends_on=["two"],
                    ),
                    BatchDraft(
                        batch_id="two",
                        title="Two",
                        subtask_ids=["s2"],
                        depends_on=["one"],
                    ),
                ]
            ),
            "dependency cycle",
        ),
    ],
)
def test_alignment_rejects_invalid_plans(plan: AlignmentPlan, message: str) -> None:
    with pytest.raises(AlignmentError, match=message.replace("[", r"\[").replace("]", r"\]")):
        validate_alignment(plan, expected_subtask_ids=["s1", "s2"])
