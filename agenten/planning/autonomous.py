"""Autonomous source-input to canonical-plan application service."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agenten.planning.canonical_plan import (
    CanonicalPlan,
    CanonicalPlanCompiler,
    CanonicalPlanPublisher,
)
from agenten.planning.captain_pipeline import CaptainPipeline
from agenten.planning.input_parser import MarkdownProjectInputParser


class AutonomousPlanningResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: CanonicalPlan
    output_dir: str


class AutonomousCaptainPlanner:
    """Coordinate parsing and planning only; never execute or self-review."""

    def __init__(
        self,
        *,
        pipeline: CaptainPipeline,
        output_dir: Path | str,
        parser: MarkdownProjectInputParser | None = None,
        compiler: CanonicalPlanCompiler | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._output_dir = Path(output_dir)
        self._parser = parser or MarkdownProjectInputParser()
        self._compiler = compiler or CanonicalPlanCompiler(minimum_workers=5)

    async def run(
        self,
        source_path: Path | str,
        *,
        source_reference: str | None = None,
        release_compiled: bool = False,
        run_id: str | None = None,
    ) -> AutonomousPlanningResult:
        project_input = self._parser.parse(
            source_path,
            source_reference=source_reference,
        )
        planning_context = project_input.planning_context()
        if release_compiled and run_id is not None:
            compiled = await self._pipeline.compile_checkpoint(
                planning_context,
                run_id=run_id,
            )
        else:
            compiled = await self._pipeline.compile(planning_context)
        plan = self._compiler.compile(project_input, compiled.batches, compiled.holdouts)
        if release_compiled:
            if run_id is None:
                await self._pipeline.release(compiled)
            else:
                await self._pipeline.release_checkpoint(
                    planning_context,
                    run_id=run_id,
                )
        CanonicalPlanPublisher(self._output_dir).publish(
            project_input,
            plan,
            compiled.holdouts,
        )
        return AutonomousPlanningResult(
            plan=plan,
            output_dir=str(self._output_dir.resolve()),
        )
