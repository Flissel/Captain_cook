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
    ) -> AutonomousPlanningResult:
        project_input = self._parser.parse(
            source_path,
            source_reference=source_reference,
        )
        compiled = await self._pipeline.compile(project_input.planning_context())
        plan = self._compiler.compile(project_input, compiled.batches, compiled.holdouts)
        if release_compiled:
            await self._pipeline.release(compiled)
        CanonicalPlanPublisher(self._output_dir).publish(
            project_input,
            plan,
            compiled.holdouts,
        )
        return AutonomousPlanningResult(
            plan=plan,
            output_dir=str(self._output_dir.resolve()),
        )
