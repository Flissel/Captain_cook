"""Run the deterministic Captain agent-runtime control-plane demonstration."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence
from uuid import UUID, uuid5

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_runtime.capabilities import derive_grant, validate_grant  # noqa: E402
from agenten.agent_runtime.contracts import (  # noqa: E402
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
    IntegrationIntent,
    RuntimeStatus,
)
from agenten.agent_runtime.control_plane import (  # noqa: E402
    AgentRuntimeControlPlane,
    ControlPlaneRunRequest,
    JsonControlPlaneRunStore,
    ValidationDisposition,
    ValidationRecord,
)
from agenten.agent_runtime.service import AgentRuntimeService  # noqa: E402
from agenten.agent_runtime.swarm import SwarmOrchestrator  # noqa: E402
from agenten.agent_runtime.tools import RuntimeToolset  # noqa: E402
from agenten.planning.alignment import AlignmentPlan, BatchDraft  # noqa: E402
from agenten.planning.captain_pipeline import (  # noqa: E402
    BatchEnrichment,
    CaptainPipeline,
    PlannedSubtask,
)
from agenten.planning.hermes_plan import HermesPlanReader  # noqa: E402
from agenten.planning.policy import PlanningPolicy  # noqa: E402
from agenten.validation.contracts import (  # noqa: E402
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


NOW = datetime(2026, 7, 18, 10, tzinfo=timezone.utc)
CORRELATION_ID = UUID("20000000-0000-4000-8000-000000000010")


class DemoArtifacts:
    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    def put(
        self,
        name: str,
        content: bytes,
        media_type: str = "text/markdown",
    ) -> ArtifactRef:
        reference = ArtifactRef(
            uri=f"artifact://demo/{name}",
            sha256=hashlib.sha256(content).hexdigest(),
            media_type=media_type,
        )
        self._values[reference.uri] = content
        return reference

    async def read(self, reference: ArtifactRef) -> bytes:
        return self._values[reference.uri]

    async def require(self, reference: ArtifactRef) -> None:
        content = self._values[reference.uri]
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise RuntimeError("demo artifact digest mismatch")


class DemoRuntimeState:
    def __init__(self) -> None:
        self.batches: dict[str, WorkBatch] = {}
        self.commands: dict[UUID, AgentRuntimeCommand] = {}
        self.grants: dict[UUID, CapabilityGrant] = {}
        self.results: dict[UUID, AgentRuntimeResult] = {}

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        if batch.batch_id != holdouts.batch_id:
            raise RuntimeError("demo batch and holdout do not match")
        existing = self.batches.get(batch.batch_id)
        if existing is not None and existing != batch:
            raise RuntimeError("demo batch replay conflict")
        if any(dependency not in self.batches for dependency in batch.depends_on):
            raise RuntimeError("demo batch dependency was not released")
        self.batches[batch.batch_id] = batch

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        existing = self.commands.get(command.event_id)
        if existing is not None and existing != command:
            raise RuntimeError("demo command replay conflict")
        self.commands[command.event_id] = command

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
        if command.payload.batch_id is None:
            raise RuntimeError("demo runtime command has no batch")
        return self.batches[command.payload.batch_id]

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return self.grants.get(command_id)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        existing = self.grants.get(grant.command_id)
        if existing is not None and existing != grant:
            raise RuntimeError("demo grant replay conflict")
        self.grants[grant.command_id] = grant
        return grant

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return self.results.get(command_id)

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        existing = self.results.get(result.command_id)
        if existing is not None and existing != result:
            raise RuntimeError("demo result replay conflict")
        self.results[result.command_id] = result
        return result


class DemoCapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
        now: datetime,
    ) -> CapabilityGrant:
        return derive_grant(command, batch, now)

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
    ) -> CapabilityGrant:
        return validate_grant(grant, command, now)


class DemoClock:
    def now(self) -> datetime:
        return NOW


class DemoHermes:
    async def plan(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult:
        del command, grant
        raise RuntimeError("Hermes planning is already represented by the demo envelope")

    async def design_agent(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult:
        del command, grant
        raise RuntimeError("Hermes planning is already represented by the demo envelope")


class DemoCodex:
    async def start(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def resume(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def status(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def cancel(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def heartbeat(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    @staticmethod
    def _result(
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        subject = command.subject_id
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "demo-result"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=subject,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id=f"demo-session-{subject}",
            artifact_refs=(
                ArtifactRef(
                    uri=f"artifact://demo/output/{subject}",
                    sha256=hashlib.sha256(subject.encode()).hexdigest(),
                    media_type="application/json",
                ),
            ),
            evidence_refs=(
                ArtifactRef(
                    uri=f"artifact://demo/evidence/{subject}",
                    sha256=hashlib.sha256(f"evidence:{subject}".encode()).hexdigest(),
                    media_type="application/json",
                ),
            ),
        )


class DemoSelector:
    async def select(self, task_id: str, tools: tuple[str, ...]) -> str:
        del task_id
        return "codex.resume" if "codex.resume" in tools else "codex.run"


class DemoValidator:
    def __init__(self, artifacts: DemoArtifacts) -> None:
        self._artifacts = artifacts

    async def validate(
        self,
        batch: WorkBatch,
        result: AgentRuntimeResult,
    ) -> ValidationRecord:
        content = json.dumps(
            {"subject_id": result.subject_id, "status": "passed"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return ValidationRecord(
            task_id=result.subject_id,
            disposition=ValidationDisposition.PASSED,
            artifact_ref=self._artifacts.put(
                f"validation/{result.subject_id}",
                content,
                "application/json",
            ),
            assertion_ids=tuple(
                assertion.assertion_id for assertion in batch.acceptance_criteria
            ),
            occurred_at=NOW,
        )


def _hermes_plan(artifacts: DemoArtifacts) -> HermesPlanResult:
    blueprint = json.dumps(
        {
            "schema": "captain.agent-blueprint.v1",
            "name": "demo_runtime_builder",
            "purpose": "Build bounded code and its isolated n8n integration.",
            "inputs": {"project_context": "object"},
            "outputs": {"implementation_result": "object"},
            "system_prompt_ref": {
                "uri": "artifact://demo/system-prompt",
                "sha256": "f" * 64,
                "media_type": "text/markdown",
            },
            "tools": ["knowledge.search"],
            "integration_intent": "n8n",
            "n8n_tool_families": ["workflow"],
            "handoffs": ["captain.decompose"],
            "limits": {"max_turns": 8, "wall_seconds": 300},
            "evaluation_cases": [
                {"case_id": "bounded-tools", "assertion": "tool_allowlist_enforced"}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    blueprint_ref = artifacts.put("blueprint", blueprint, "application/json")
    document = json.dumps(
        {
            "schema": "captain.hermes-planning-document.v1",
            "project_id": "runtime-demo",
            "correlation_id": str(CORRELATION_ID),
            "subject_version": 1,
            "objective": "Build code, then publish its isolated n8n integration.",
            "planner_id": "hermes-demo-planner",
            "blueprint_digests": [blueprint_ref.sha256],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return HermesPlanResult(
        schema_name="captain.hermes-plan-result.v1",
        project_id="runtime-demo",
        correlation_id=CORRELATION_ID,
        subject_version=1,
        plan_ref=artifacts.put("plan", document, "application/json"),
        decision_log_ref=artifacts.put(
            "decision-log",
            b"Approved deterministic demo plan.",
        ),
        blueprint_refs=(blueprint_ref,),
        integration_intents=(IntegrationIntent.N8N,),
        minibook={"project_id": "runtime-demo", "post_id": "demo-plan-1"},
        planner_id="hermes-demo-planner",
        runtime_provenance="deterministic-demo/hermes-envelope-v1",
        started_at=NOW,
        ended_at=NOW,
    )


def _captain(state: DemoRuntimeState, artifacts: DemoArtifacts) -> CaptainPipeline:
    async def decompose(description: str) -> list[PlannedSubtask]:
        if description != "Build code, then publish its isolated n8n integration.":
            raise RuntimeError("unexpected demo objective")
        return [
            PlannedSubtask(subtask_id="code-task", description="Build the code"),
            PlannedSubtask(subtask_id="n8n-task", description="Build the workflow"),
        ]

    async def align(
        description: str,
        subtasks: list[PlannedSubtask],
        feedback: str,
    ) -> AlignmentPlan:
        del description, subtasks, feedback
        return AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="code-batch",
                    title="Code build",
                    subtask_ids=["code-task"],
                    target="python",
                ),
                BatchDraft(
                    batch_id="n8n-batch",
                    title="n8n integration",
                    subtask_ids=["n8n-task"],
                    depends_on=["code-batch"],
                    target="n8n",
                ),
            ]
        )

    async def enrich(
        description: str,
        draft: BatchDraft,
        subtasks: list[PlannedSubtask],
    ) -> BatchEnrichment:
        del description, subtasks
        capability = "code-builder" if draft.batch_id == "code-batch" else "delivery"
        return BatchEnrichment(
            goal=f"Complete {draft.title} under the released contract.",
            capability_tags=[capability],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="runtime-result-valid",
                    kind=AssertionKind.STATUS_EQUALS,
                    path="status",
                    expected="succeeded",
                )
            ],
            golden_cases=[ExampleCase(case_id="visible", input={"case": "public"})],
            holdout_cases=[ExampleCase(case_id="sealed", input={"case": "private"})],
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=state,
        policy=PlanningPolicy(frozenset({"code-builder", "delivery", "n8n-builder"})),
        target="python",
        allowed_targets=frozenset({"python", "n8n"}),
        plan_reader=HermesPlanReader(artifacts),
    )


async def run_demo(state_dir: Path) -> dict[str, Any]:
    artifacts = DemoArtifacts()
    plan = _hermes_plan(artifacts)
    state = DemoRuntimeState()
    clock = DemoClock()
    service = AgentRuntimeService(
        state=state,
        hermes=DemoHermes(),
        codex=DemoCodex(),
        artifacts=artifacts,
        capabilities=DemoCapabilityPolicy(),
        clock=clock,
    )
    control_plane = AgentRuntimeControlPlane(
        captain=_captain(state, artifacts),
        swarm=SwarmOrchestrator(
            tools=RuntimeToolset(service=service, clock=clock),
            selector=DemoSelector(),
        ),
        validator=DemoValidator(artifacts),
        store=JsonControlPlaneRunStore(state_dir),
        clock=clock,
    )
    result = await control_plane.execute(
        ControlPlaneRunRequest(
            hermes_result=plan,
            workspace_refs={
                "code-task": "workspace://demo/code",
                "n8n-task": "workspace://demo/n8n",
            },
            prompt_refs={
                "code-task": artifacts.put("prompt/code", b"Build bounded demo code."),
                "n8n-task": artifacts.put("prompt/n8n", b"Build isolated demo workflow."),
            },
            wall_seconds=300,
            max_iterations=3,
        )
    )
    return {
        str(result.manifest.correlation_id): {
            "mode": "deterministic-offline",
            "external_execution": False,
            "manifest": result.manifest.model_dump(mode="json", by_alias=True),
        }
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the correlation-indexed JSON envelope to this path; default is stdout.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "captain-agent-runtime-demo",
        help="Directory for restart-safe demo checkpoints.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = asyncio.run(run_demo(args.state_dir.resolve()))
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
