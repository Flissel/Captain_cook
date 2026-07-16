import pytest

from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.policy import PlanningPolicy, PlanningPolicyError
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


def enrichment_fixture(
    *,
    capability_tags: list[str] | None = None,
    golden_cases: list[ExampleCase] | None = None,
    holdout_cases: list[ExampleCase] | None = None,
) -> BatchEnrichment:
    return BatchEnrichment(
        goal="Deliver a verified result",
        capability_tags=["delivery"] if capability_tags is None else capability_tags,
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        golden_cases=(
            [ExampleCase(case_id="visible", input={"score": 82})]
            if golden_cases is None
            else golden_cases
        ),
        holdout_cases=(
            [ExampleCase(case_id="hidden", input={"score": 91})]
            if holdout_cases is None
            else holdout_cases
        ),
    )


def test_policy_rejects_enrichment_capability_outside_vocabulary() -> None:
    policy = PlanningPolicy(frozenset({"delivery"}))
    enrichment = enrichment_fixture(capability_tags=["invented"])

    with pytest.raises(PlanningPolicyError, match="unknown capability tags.*invented"):
        policy.validate_enrichment(enrichment)


def test_policy_rejects_same_case_content_under_different_ids() -> None:
    policy = PlanningPolicy(frozenset({"delivery"}))
    enrichment = enrichment_fixture(
        golden_cases=[ExampleCase(case_id="visible", input={"score": 82})],
        holdout_cases=[ExampleCase(case_id="hidden", input={"score": 82})],
    )

    with pytest.raises(PlanningPolicyError, match="holdout content overlaps"):
        policy.validate_enrichment(enrichment)


def test_policy_rejects_duplicate_capability_tags() -> None:
    policy = PlanningPolicy(frozenset({"delivery"}))

    with pytest.raises(PlanningPolicyError, match="duplicate capability tags.*delivery"):
        policy.validate_enrichment(
            enrichment_fixture(capability_tags=["delivery", "delivery"])
        )


def test_policy_fingerprint_is_canonical_for_nested_json_ordering() -> None:
    first = ExampleCase(
        case_id="first",
        input={"outer": {"beta": [2, {"right": True, "left": None}], "alpha": 1}},
        expected_observations={"result": {"z": 3, "a": [1, 2]}},
    )
    second = ExampleCase(
        case_id="second",
        input={"outer": {"alpha": 1, "beta": [2, {"left": None, "right": True}]}},
        expected_observations={"result": {"a": [1, 2], "z": 3}},
    )

    assert PlanningPolicy.fingerprint_case(first) == PlanningPolicy.fingerprint_case(second)


def test_policy_accepts_known_tags_and_isolated_case_content() -> None:
    policy = PlanningPolicy(frozenset({"delivery", "quality"}))

    policy.validate_enrichment(
        enrichment_fixture(capability_tags=["quality", "delivery"])
    )
