import json
from pathlib import Path

import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.cli import async_main
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


@pytest.mark.asyncio
async def test_cli_reads_project_and_writes_release_summary(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build the delivery", encoding="utf-8")
    output = tmp_path / "release"
    responses = [
        DecomposeResponse(
            subproblems=[
                SubproblemCandidate(
                    description="Deliver",
                    capability_tags=["delivery"],
                    atomic=True,
                )
            ]
        ).model_dump_json(),
        AlignmentPlan(
            batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["sub-01"])]
        ).model_dump_json(),
        BatchEnrichment(
            goal="Deliver",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
        ).model_dump_json(),
    ]

    exit_code = await async_main(
        [
            str(project),
            "--output",
            str(output),
            "--capability",
            "delivery",
        ],
        model_client=build_replay_model_client(responses),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["released_batches"] == ["delivery"]
    assert summary["canonical_plan_id"].startswith("plan-")
    assert len(summary["worker_pool"]) == 5
    assert (output / "source" / "input.md").exists()
    assert (output / "plans" / "index.md").exists()
    assert (output / "contracts" / "batches" / "delivery.json").exists()
    assert (output / "holdouts" / "delivery.json").exists()


@pytest.mark.asyncio
async def test_cli_can_publish_an_allowlisted_mixed_target_plan(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build an n8n tool and dependent AutoGen team", encoding="utf-8")
    output = tmp_path / "release"
    responses = [
        DecomposeResponse(
            subproblems=[
                SubproblemCandidate(
                    description="Build the tool",
                    capability_tags=["delivery"],
                    atomic=True,
                ),
                SubproblemCandidate(
                    description="Build the team",
                    capability_tags=["delivery"],
                    atomic=True,
                ),
            ]
        ).model_dump_json(),
        AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="lead-tool",
                    title="Lead Tool",
                    subtask_ids=["sub-01"],
                    target="n8n",
                ),
                BatchDraft(
                    batch_id="sales-team",
                    title="Sales Team",
                    subtask_ids=["sub-02"],
                    depends_on=["lead-tool"],
                    target="autogen",
                ),
            ]
        ).model_dump_json(),
        BatchEnrichment(
            goal="Build the tool",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="tool-done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="tool-hidden", input={"value": 1})],
        ).model_dump_json(),
        BatchEnrichment(
            goal="Build the team",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="team-done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[ExampleCase(case_id="team-hidden", input={"value": 2})],
        ).model_dump_json(),
    ]

    exit_code = await async_main(
        [
            str(project),
            "--output",
            str(output),
            "--target",
            "n8n",
            "--allowed-target",
            "n8n",
            "--allowed-target",
            "autogen",
            "--capability",
            "delivery",
        ],
        model_client=build_replay_model_client(responses),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["released_batches"] == ["lead-tool", "sales-team"]
    lead = json.loads(
        (output / "contracts" / "batches" / "lead-tool.json").read_text(encoding="utf-8")
    )
    team = json.loads(
        (output / "contracts" / "batches" / "sales-team.json").read_text(encoding="utf-8")
    )
    assert (lead["target"], team["target"]) == ("n8n", "autogen")
