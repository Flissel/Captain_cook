"""Deterministic constitution layer over a whole planner-supplied plan."""

from __future__ import annotations

from dataclasses import replace

from agenten.constitution.plan_check import check_supplied_plan
from agenten.constitution.ruleset import load_constitution
from agenten.decomposition.budget import DecompositionBudget

RULESET = load_constitution()
PROJECT = "Build a weekly report mailer for the sales team"


def test_a_sound_plan_passes_the_deterministic_layer_only() -> None:
    report = check_supplied_plan(
        PROJECT,
        [("s1", "Collect sales data", ["python"]), ("s2", "Send the report", ["email"])],
        RULESET,
    )

    assert report.accepted is True
    assert report.to_json() == {
        "constitution_version": RULESET.version,
        "accepted": True,
        "layer": "deterministic",
        "semantic_review": "not_performed",
        "rejections": [],
    }


def test_each_failing_subtask_is_named_with_its_reason() -> None:
    report = check_supplied_plan(
        PROJECT,
        [
            ("short", "tiny", ["python"]),
            ("untagged", "Collect sales data", []),
            ("long", PROJECT + " and also everything else", ["python"]),
            ("ok", "Send the report", ["email"]),
            ("dup", "  send the REPORT ", ["email"]),
        ],
        RULESET,
    )

    assert report.accepted is False
    assert [verdict.subtask_id for verdict in report.verdicts if not verdict.accepted] == [
        "short",
        "untagged",
        "long",
        "dup",
    ]
    reasons = report.rejections()
    assert reasons[0].startswith("short: malformed")
    assert reasons[-1].startswith("dup: duplicate")


def test_the_total_subproblem_budget_applies_to_the_plan() -> None:
    tight = replace(RULESET, default_budget=DecompositionBudget(max_total_subproblems=1))

    report = check_supplied_plan(
        PROJECT, [("s1", "Collect sales data", ["python"]), ("s2", "Send the report", ["email"])], tight
    )

    assert report.accepted is False
    assert report.rejections()[0].startswith("plan: budget_exceeded")
