from pathlib import Path

import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.autonomous import AutonomousCaptainPlanner
from agenten.planning.captain_pipeline import BatchEnrichment, CaptainCompiledPlan
from agenten.planning.factory import build_captain_pipeline
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, ExampleCase
from agenten.validation.contracts import HoldoutSuite, WorkBatch


@pytest.mark.asyncio
async def test_autonomous_planner_parses_compiles_and_publishes_without_execution(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild one validated integration tool.\n", encoding="utf-8")
    model = build_replay_model_client(
        [
            DecomposeResponse(
                subproblems=[
                    SubproblemCandidate(
                        description="Build the tool",
                        capability_tags=["delivery"],
                        atomic=True,
                    )
                ]
            ).model_dump_json(),
            AlignmentPlan(
                batches=[BatchDraft(batch_id="tool", title="Tool", subtask_ids=["sub-01"])]
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
                golden_cases=[ExampleCase(case_id="visible", input={"value": 1})],
                holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
            ).model_dump_json(),
        ]
    )
    legacy_output = tmp_path / "must-not-be-used"
    pipeline = build_captain_pipeline(
        model_client=model,
        output_dir=legacy_output,
        target="n8n",
        known_capability_tags=["delivery"],
    )
    output = tmp_path / "autonomous-run"

    result = await AutonomousCaptainPlanner(pipeline=pipeline, output_dir=output).run(
        source,
        source_reference="Autogen_AgentFarm/input.md",
    )

    assert result.plan.source_reference == "Autogen_AgentFarm/input.md"
    assert len(result.plan.worker_pool) == 5
    assert not legacy_output.exists()
    assert (output / "source" / "input.md").exists()
    assert (output / "plans" / "index.md").exists()
    assert (output / "contracts" / "batches" / "tool.json").exists()
    assert (output / "holdouts" / "tool.json").exists()
    assert result.plan.work_packages[0].holdout_digest is not None


@pytest.mark.asyncio
async def test_parser_provenance_and_outline_are_available_to_captain(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Sales Org\n\n## Qualification Agent\n\nQualifies leads.\n", encoding="utf-8")
    batch = WorkBatch(
        batch_id="qualification",
        title="Qualification",
        goal="Build qualification",
        subtask_ids=["sub-1"],
        target="autogen",
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="qualification-done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )
    holdout = HoldoutSuite(
        batch_id="qualification",
        cases=[ExampleCase(case_id="hidden", input={"lead": "novel"})],
    )

    class RecordingPipeline:
        context = ""

        async def compile(self, project_description: str) -> CaptainCompiledPlan:
            self.context = project_description
            return CaptainCompiledPlan(batches=(batch,), holdouts=(holdout,))

    pipeline = RecordingPipeline()
    await AutonomousCaptainPlanner(pipeline=pipeline, output_dir=tmp_path / "run").run(source)

    assert "captain-project-input/v1" in pipeline.context
    assert "Qualification Agent" in pipeline.context
    assert "Qualifies leads." in pipeline.context
    assert "Input SHA-256:" in pipeline.context
