"""Planning tools for the Captain MCP server.

The planner (OpenClaw) supplies the decompose/align/enrich results; Captain
validates and compiles them deterministically (`agenten.planning.supplied_plan`).

- `compile_plan` is read-only: it may query the gateway's capability
  projection, but it never writes.
- `check_constitution` is read-only: the deterministic constitution layer
  over every subtask and every batch goal as released (`agenten.constitution.plan_check`). The semantic
  layer is not run; the planner must not judge its own plan.
- `publish_plan` exists only with the Captain token. It recompiles from
  the same inputs and releases what Captain produced, never a compiled
  plan handed in by the planner. It refuses a plan that fails the
  constitution check. Every batch is released explicitly as
  `pending_review`, whatever the gateway's default, so the operator
  approves before any worker can claim it. Approval needs
  `GATEWAY_APPROVAL_ENABLED=true`; without it the batches stay held.

Tool results carry the compiled batches but only holdout case *counts*:
holdout expectations are evaluation secrets and stay out of transcripts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from agenten.constitution.plan_check import PlanConstitutionReport, check_supplied_plan
from agenten.constitution.ruleset import ConstitutionRuleset, load_constitution
from agenten.planning.captain_pipeline import (
    BatchReleaseClient,
    CapabilityResolver,
    CaptainCompiledPlan,
)
from agenten.planning.supplied_plan import (
    PlanCompilerSettings,
    SuppliedPlan,
    compile_supplied_plan,
    publish_supplied_plan,
)

_COMPILE = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# A second publish of the same batches is refused by the gateway (409), so
# the call is not idempotent; it adds batches but removes nothing.
_PUBLISH = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


class PlanToolsConfigurationError(RuntimeError):
    """Planning tool settings are incomplete or inconsistent."""


class PlanRejectedError(RuntimeError):
    """The plan failed the constitution check and was not published."""


def plan_compiler_settings_from_env(environment: Mapping[str, str]) -> PlanCompilerSettings | None:
    """Read `CAPTAIN_PLAN_*`; `None` when planning tools are not configured."""

    target = environment.get("CAPTAIN_PLAN_TARGET", "").strip()
    tags_raw = environment.get("CAPTAIN_PLAN_CAPABILITY_TAGS", "").strip()
    allowed_raw = environment.get("CAPTAIN_PLAN_ALLOWED_TARGETS", "").strip()
    if not target:
        if tags_raw or allowed_raw:
            raise PlanToolsConfigurationError(
                "CAPTAIN_PLAN_TARGET must be set when other CAPTAIN_PLAN_* values are"
            )
        return None
    tags = _csv(tags_raw)
    if not tags:
        raise PlanToolsConfigurationError(
            "CAPTAIN_PLAN_CAPABILITY_TAGS must list the capability tags plans may use"
        )
    allowed = _csv(allowed_raw) or frozenset({target})
    if target not in allowed:
        raise PlanToolsConfigurationError("CAPTAIN_PLAN_TARGET must be one of CAPTAIN_PLAN_ALLOWED_TARGETS")
    return PlanCompilerSettings(target=target, allowed_targets=allowed, capability_tags=tags)


def _csv(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class CaptainPlanner:
    """Compile (and, with a release client, publish) planner-supplied plans."""

    settings: PlanCompilerSettings
    capability_resolver: CapabilityResolver | None = None
    release_client: BatchReleaseClient | None = None
    ruleset: ConstitutionRuleset = field(default_factory=load_constitution)

    def check(self, plan: SuppliedPlan) -> PlanConstitutionReport:
        return check_supplied_plan(
            plan.project_description,
            [(item.subtask_id, item.description, item.capability_tags) for item in plan.subtasks],
            self.ruleset,
            # The released WorkBatch takes goal and tags from the enrichment,
            # so the gate checks exactly that content too.
            batches=[
                (batch_id, enrichment.goal, enrichment.capability_tags)
                for batch_id, enrichment in plan.enrichments.items()
            ],
        )

    async def compile(self, plan: SuppliedPlan) -> dict[str, Any]:
        compiled = await compile_supplied_plan(
            plan, self.settings, capability_resolver=self.capability_resolver
        )
        return _summary(compiled)

    async def publish(self, plan: SuppliedPlan) -> dict[str, Any]:
        if self.release_client is None:
            raise RuntimeError("publishing requires the Captain token")
        report = self.check(plan)
        if not report.accepted:
            raise PlanRejectedError(
                "constitution check failed: " + "; ".join(report.rejections())
            )
        compiled = await publish_supplied_plan(
            plan,
            self.settings,
            release_client=self.release_client,
            capability_resolver=self.capability_resolver,
        )
        return {"published": True, **_summary(compiled)}


def _summary(compiled: CaptainCompiledPlan) -> dict[str, Any]:
    return {
        "batches": [batch.model_dump(mode="json") for batch in compiled.batches],
        "holdout_case_counts": {suite.batch_id: len(suite.cases) for suite in compiled.holdouts},
    }


def register_plan_tools(server: FastMCP, planner: CaptainPlanner) -> None:
    @server.tool(annotations=_COMPILE)
    async def compile_plan(plan: SuppliedPlan) -> dict[str, Any]:
        """Validate and compile a plan into canonical work batches without publishing.

        Supply the subtasks, their alignment into batches, and one enrichment
        (goal, capability tags, acceptance criteria, holdout cases) per batch.
        Errors name the rule that failed; fix the plan and compile again.
        """

        return await planner.compile(plan)

    @server.tool(annotations=_COMPILE)
    async def check_constitution(plan: SuppliedPlan) -> dict[str, Any]:
        """Check every subtask against Captain's constitution (deterministic layer).

        Reports the constitution version and each rejection with its reason.
        The semantic scope/quality review is not performed by this tool.
        publish_plan refuses plans that this check rejects.
        """

        return planner.check(plan).to_json()

    if planner.release_client is None:
        return

    @server.tool(annotations=_PUBLISH)
    async def publish_plan(plan: SuppliedPlan) -> dict[str, Any]:
        """Recompile the plan and release every batch to the gateway.

        The plan must pass check_constitution. Batches wait for operator
        review approval before workers can claim them. Publishing the same
        batch ids again is refused.
        """

        return await planner.publish(plan)
