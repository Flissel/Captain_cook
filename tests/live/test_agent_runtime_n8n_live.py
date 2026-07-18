from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
import yaml


ROOT = Path(__file__).parents[2]
HERMES_ROOT = ROOT / "hermes-agent"
sys.path.insert(0, str(HERMES_ROOT))

from hermes_cli.captain_planner import (  # noqa: E402
    CaptainPlanner,
    PlanningDraft,
)
from hermes_cli.captain_worker import (  # noqa: E402
    CaptainWorker,
    HermesCodexRuntime,
    JsonFileWorkerStateStore,
    StaticWorkspaceResolver,
)
from hermes_cli.captain_worker_contracts import (  # noqa: E402
    AgentBlueprint as HermesAgentBlueprint,
    AgentRuntimeCommand as HermesAgentRuntimeCommand,
    ArtifactRef as HermesArtifactRef,
    CaptainWorkPackage,
    HermesWorkResult,
    MinibookReference,
)
from hermes_cli.n8n_worker_mcp import (  # noqa: E402
    HermesGenericMcpTransport,
    JsonFileMcpCallStore,
    N8nWorkerMcp,
    ScopedCodexEnvironment,
    ScopedCodexHomeFactory,
)

from agenten.agent_runtime.capabilities import derive_grant  # noqa: E402
from agenten.agent_runtime.contracts import (  # noqa: E402
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    HermesPlanResult,
    IntegrationIntent,
    RuntimeStatus,
)
from agenten.agent_runtime.control_plane import (  # noqa: E402
    ControlPlaneEvidenceManifest,
    EvidenceObservation,
)
from agenten.agent_runtime.tools import (  # noqa: E402
    AuthoritativeRuntimeState,
    RuntimeToolContext,
    RuntimeToolset,
)
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


pytestmark = [pytest.mark.live]


CODEX_PROMPT = b"""This is an authorized live acceptance check in a disposable Git repository.
Create exactly one file named captain_live_evidence.txt containing exactly:
captain-runtime-live-ok
Do not modify any other file, do not commit, do not use MCP or network tools, and stop after the file is written.
"""


def _workflow_code(correlation_id: UUID) -> str:
    identity = f"captain-{correlation_id.hex}"
    return f"""import {{ workflow, node, trigger }} from '@n8n/workflow-sdk';
const start = trigger({{ type: 'n8n-nodes-base.webhook', version: 2, config: {{ name: 'Captain Webhook', parameters: {{ httpMethod: 'POST', path: '{identity}', responseMode: 'lastNode' }} }} }});
const output = node({{ type: 'n8n-nodes-base.set', version: 3.4, config: {{ name: 'Captain Evidence', parameters: {{ mode: 'manual', assignments: {{ assignments: [{{ id: 'correlation', name: 'correlation_id', value: '{correlation_id}', type: 'string' }}] }} }} }} }});
export default workflow('{identity}', 'Captain Runtime {correlation_id.hex[:12]}').add(start).to(output);
"""


def _workflow_prompt(correlation_id: UUID) -> bytes:
    code = _workflow_code(correlation_id)
    return (
        "This is an authorized live acceptance check in a disposable Git repository.\n"
        "Create exactly one file named workflow.ts with exactly the TypeScript below. "
        "Do not modify any other file, do not commit, do not invoke MCP or network tools, "
        "and stop after writing the file.\n\n"
        f"{code}"
    ).encode()


def _require_live_environment(*, n8n: bool) -> None:
    if shutil.which("codex") is None:
        pytest.fail("required live gate: Codex CLI is unavailable")
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.is_file() and not os.environ.get("OPENAI_API_KEY"):
        pytest.fail("required live gate: Codex authentication is unavailable")
    if n8n and not os.environ.get("N8N_MCP_TOKEN"):
        pytest.fail("required live gate: N8N_MCP_TOKEN is unavailable")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    workspace = tmp_path / "authorized" / "worktree"
    workspace.mkdir(parents=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "captain-live@example.invalid")
    _git(workspace, "config", "user.name", "Captain Runtime Live")
    (workspace / "README.md").write_text("disposable runtime gate\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "test: initialize runtime gate")
    return workspace


class LiveArtifacts:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(
        self,
        namespace: str,
        content: bytes,
        media_type: str,
    ) -> HermesArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        reference = {
            "uri": f"artifact://live/{namespace}/{digest}",
            "sha256": digest,
            "media_type": media_type,
        }
        self.values[reference["uri"]] = content
        return HermesArtifactRef.model_validate(reference)

    async def read(self, reference: ArtifactRef) -> bytes:
        return self.values[reference.uri]


class LiveMinibookProjection:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish_plan(self, **kwargs: Any) -> MinibookReference:
        self.calls.append(dict(kwargs))
        correlation_id = UUID(str(kwargs["correlation_id"]))
        return MinibookReference(
            project_id=str(kwargs["project_id"]),
            post_id=f"live-plan-{correlation_id.hex[:20]}",
        )


class DeterministicPlanningRuntime:
    def __init__(self, *, correlation_id: UUID, n8n: bool) -> None:
        self.correlation_id = correlation_id
        self.n8n = n8n

    def create_plan(self, operation: str, prompt: bytes) -> PlanningDraft:
        del operation, prompt
        intent = "n8n" if self.n8n else "none"
        blueprint = HermesAgentBlueprint.model_validate(
            {
                "schema": "captain.agent-blueprint.v1",
                "name": "live_runtime_builder",
                "purpose": "Build one bounded live runtime artifact.",
                "inputs": {"project_context": "object"},
                "outputs": {"result": "object"},
                "system_prompt_ref": {
                    "uri": "artifact://live/system-prompt",
                    "sha256": "f" * 64,
                    "media_type": "text/markdown",
                },
                "tools": ["codex.run"],
                "integration_intent": intent,
                "n8n_tool_families": ["workflow"] if self.n8n else [],
                "handoffs": ["captain.decompose"],
                "limits": {"max_turns": 4, "wall_seconds": 300},
                "evaluation_cases": [
                    {"case_id": "live-output", "assertion": "artifact_created"}
                ],
            }
        )
        blueprint_bytes = yaml.safe_dump(
            blueprint.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            allow_unicode=True,
        ).encode()
        blueprint_digest = hashlib.sha256(blueprint_bytes).hexdigest()
        plan_document = json.dumps(
            {
                "schema": "captain.hermes-planning-document.v1",
                "project_id": f"live-{self.correlation_id.hex[:20]}",
                "correlation_id": str(self.correlation_id),
                "subject_version": 1,
                "objective": "Create one bounded live runtime artifact.",
                "planner_id": "hermes-live-planner",
                "blueprint_digests": [blueprint_digest],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return PlanningDraft(
            plan_markdown=plan_document,
            decision_log={"decision": "bounded live runtime"},
            blueprints=(blueprint,),
            integration_intents=(intent,) if self.n8n else (),
            planner_id="hermes-live-planner",
            runtime_provenance="hermes-agent/captain-planner-live",
        )


class ReleaseState:
    def __init__(self) -> None:
        self.batch: WorkBatch | None = None
        self.holdouts: HoldoutSuite | None = None

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        if self.batch is not None:
            assert self.batch == batch and self.holdouts == holdouts
            return
        self.batch = batch
        self.holdouts = holdouts


def _captain_pipeline(
    artifacts: LiveArtifacts,
    release: ReleaseState,
    *,
    n8n: bool,
) -> CaptainPipeline:
    async def decompose(description: str) -> list[PlannedSubtask]:
        assert description == "Create one bounded live runtime artifact."
        return [PlannedSubtask(subtask_id="live-task", description=description)]

    async def align(
        description: str,
        subtasks: list[PlannedSubtask],
        feedback: str,
    ) -> AlignmentPlan:
        del description, subtasks, feedback
        return AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="live-batch",
                    title="Live runtime gate",
                    subtask_ids=["live-task"],
                    target="n8n" if n8n else "python",
                )
            ]
        )

    async def enrich(
        description: str,
        draft: BatchDraft,
        subtasks: list[PlannedSubtask],
    ) -> BatchEnrichment:
        del description, draft, subtasks
        return BatchEnrichment(
            goal="Create exactly one authorized live evidence artifact.",
            capability_tags=["delivery" if n8n else "code-builder"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="artifact-created",
                    kind=AssertionKind.STATUS_EQUALS,
                    path="status",
                    expected="succeeded",
                )
            ],
            golden_cases=[ExampleCase(case_id="visible", input={"mode": "live"})],
            holdout_cases=[ExampleCase(case_id="sealed", input={"mode": "private"})],
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=release,
        policy=PlanningPolicy(frozenset({"code-builder", "delivery", "n8n-builder"})),
        target="n8n" if n8n else "python",
        allowed_targets=frozenset({"n8n" if n8n else "python"}),
        plan_reader=HermesPlanReader(artifacts),
    )


class NeverService:
    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        raise AssertionError(f"unexpected service execution: {command.event_id}")


class LiveClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


async def _released_command(
    *,
    correlation_id: UUID,
    workspace_ref: str,
    prompt_ref: ArtifactRef,
    n8n: bool,
    now: datetime,
) -> tuple[HermesPlanResult, WorkBatch, AgentRuntimeCommand, CapabilityGrant, LiveArtifacts, LiveMinibookProjection]:
    artifacts = LiveArtifacts()
    minibook = LiveMinibookProjection()
    planning_prompt = b"Plan one bounded live runtime task."
    planning_ref = ArtifactRef(
        uri="artifact://live/planning-prompt",
        sha256=hashlib.sha256(planning_prompt).hexdigest(),
        media_type="text/markdown",
    )
    planning_command = AgentRuntimeCommand.model_validate(
        {
            "schema": "captain.agent-runtime-command.v1",
            "event_id": str(uuid5(correlation_id, "hermes.plan")),
            "correlation_id": str(correlation_id),
            "occurred_at": now,
            "producer": "captain",
            "subject_id": f"live-{correlation_id.hex[:20]}",
            "subject_version": 1,
            "payload": {
                "operation": "hermes.plan",
                "project_id": f"live-{correlation_id.hex[:20]}",
                "batch_id": "planning-batch",
                "subtask_id": f"live-{correlation_id.hex[:20]}",
                "workspace_ref": workspace_ref,
                "prompt_ref": planning_ref.model_dump(mode="json"),
                "integration_intent": "none",
                "capability_profile": "planner",
                "limits": {"wall_seconds": 300, "max_iterations": 2},
            },
        }
    )
    planner = CaptainPlanner(
        runtime=DeterministicPlanningRuntime(correlation_id=correlation_id, n8n=n8n),
        artifacts=artifacts,
        minibook=minibook,
        clock=lambda: now,
    )
    hermes_wire = planner.execute(
        # Both repositories validate the same strict JSON contract.
        HermesAgentRuntimeCommand.model_validate(
            planning_command.model_dump(mode="json", by_alias=True)
        ),
        planning_prompt,
    )
    hermes_result = HermesPlanResult.model_validate(
        hermes_wire.model_dump(mode="json", by_alias=True)
    )
    release = ReleaseState()
    captain = _captain_pipeline(artifacts, release, n8n=n8n)
    compilation = await captain.compile_from_hermes_result(hermes_result)
    await captain.release(compilation.compiled)
    assert release.batch is not None
    batch = release.batch
    context = RuntimeToolContext(
        state=AuthoritativeRuntimeState.SUBTASK_READY,
        project_id=hermes_result.project_id,
        correlation_id=correlation_id,
        causation_id=planning_command.event_id,
        subject_id="live-task",
        subject_version=1,
        batch_id=batch.batch_id,
        subtask_id="live-task",
        workspace_ref=workspace_ref,
        prompt_ref=prompt_ref,
        integration_intent=IntegrationIntent.N8N if n8n else IntegrationIntent.NONE,
        wall_seconds=300,
        max_iterations=3,
    )
    command = RuntimeToolset(
        service=NeverService(),
        clock=LiveClock(now),
    ).command_for("codex.run", context)
    grant = derive_grant(command, batch, now)
    return hermes_result, batch, command, grant, artifacts, minibook


def _package(
    command: AgentRuntimeCommand,
    grant: CapabilityGrant,
    batch: WorkBatch,
    now: datetime,
) -> CaptainWorkPackage:
    return CaptainWorkPackage.model_validate(
        {
            "schema": "captain.work-package-released.v1",
            "event_id": str(uuid5(command.event_id, "released-package")),
            "correlation_id": str(command.correlation_id),
            "occurred_at": now,
            "producer": "captain",
            "subject_id": command.subject_id,
            "subject_version": command.subject_version,
            "command": command.model_dump(mode="json", by_alias=True),
            "grant": grant.model_dump(mode="json", by_alias=True),
            "acceptance_assertion_ids": [
                item.assertion_id for item in batch.acceptance_criteria
            ],
        }
    )


def _configured_n8n_server() -> dict[str, dict[str, Any]]:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.is_file():
        pytest.fail("required live gate: Codex MCP config is unavailable")
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    entry = (payload.get("mcp_servers") or {}).get("n8n")
    if not isinstance(entry, dict) or not entry.get("url"):
        pytest.fail("required live gate: Codex n8n MCP server is unavailable")
    return {
        "n8n-mcp": {
            "url": str(entry["url"]),
            "headers": {"Authorization": "Bearer ${N8N_MCP_TOKEN}"},
            "tools": {
                "include": [
                    "validate_workflow",
                    "create_workflow_from_code",
                    "test_workflow",
                    "publish_workflow",
                    "execute_workflow",
                    "archive_workflow",
                ]
            },
            "timeout": 45,
            "enabled": True,
        }
    }


class CountingRuntime:
    def __init__(self) -> None:
        self.inner = HermesCodexRuntime()
        self.starts = 0

    def start(self, **kwargs: Any) -> Any:
        self.starts += 1
        return self.inner.start(**kwargs)

    def resume(self, **kwargs: Any) -> Any:
        return self.inner.resume(**kwargs)

    def cancel(self, session_id: str) -> None:
        self.inner.cancel(session_id)

    def status(self, session_id: str) -> str:
        return self.inner.status(session_id)

    def close(self) -> None:
        self.inner.close()


def _worker(
    *,
    workspace: Path,
    package: CaptainWorkPackage,
    runtime: CountingRuntime,
    state_root: Path,
    codex_home_root: Path,
    configured_servers: dict[str, dict[str, Any]],
    now: datetime,
) -> CaptainWorker:
    return CaptainWorker(
        runtime=runtime,
        state=JsonFileWorkerStateStore(state_root),
        workspace_resolver=StaticWorkspaceResolver(
            {package.grant.workspace_ref: workspace}
        ),
        authorized_roots=(workspace.parent,),
        clock=lambda: now,
        codex_home_factory=ScopedCodexHomeFactory(
            codex_home_root,
            configured_servers,
            auth_source=(
                Path.home() / ".codex" / "auth.json"
                if (Path.home() / ".codex" / "auth.json").is_file()
                else None
            ),
        ),
        codex_environment_factory=lambda current: ScopedCodexEnvironment().for_grant(
            current.grant
        ),
    )


def _runtime_result(
    result: HermesWorkResult,
    command: AgentRuntimeCommand,
) -> AgentRuntimeResult:
    artifact = ArtifactRef(
        uri=f"artifact://live/build/{result.artifact_digest}",
        sha256=result.artifact_digest,
        media_type="application/json",
    )
    evidence = ArtifactRef(
        uri=f"artifact://live/codex/{result.codex_evidence.output_digest}",
        sha256=result.codex_evidence.output_digest,
        media_type="application/json",
    )
    return AgentRuntimeResult(
        schema_name="captain.agent-runtime-result.v1",
        event_id=result.event_id,
        command_id=command.event_id,
        correlation_id=command.correlation_id,
        occurred_at=result.occurred_at,
        producer="agent-runtime",
        subject_id=command.subject_id,
        subject_version=command.subject_version,
        grant_id=result.grant_id,
        operation=command.payload.operation,
        status=RuntimeStatus(result.status.value),
        session_id=result.session_id,
        artifact_refs=(artifact,),
        evidence_refs=(evidence,),
        error=result.error,
    )


def _manifest(
    *,
    hermes: HermesPlanResult,
    batch: WorkBatch,
    grant: CapabilityGrant,
    result: AgentRuntimeResult,
    now: datetime,
    extra_refs: tuple[ArtifactRef, ...] = (),
) -> ControlPlaneEvidenceManifest:
    profile = grant.profile
    observations = (
        EvidenceObservation(
            observation_id=uuid5(hermes.correlation_id, "live-hermes"),
            boundary="hermes",
            subject_id=hermes.project_id,
            subject_version=hermes.subject_version,
            status="succeeded",
            operation="hermes.plan",
            session_id=hermes.planner_id,
            artifact_refs=(hermes.plan_ref, *hermes.blueprint_refs),
            evidence_refs=(hermes.decision_log_ref,),
        ),
        EvidenceObservation(
            observation_id=uuid5(hermes.correlation_id, "live-runtime"),
            boundary="n8n" if profile is CapabilityProfile.N8N_BUILDER else "codex",
            subject_id=result.subject_id,
            subject_version=result.subject_version,
            status=result.status.value,
            batch_id=batch.batch_id,
            operation=result.operation.value,
            grant_id=grant.grant_id,
            capability_profile=profile,
            mcp_servers=grant.mcp_servers,
            session_id=result.session_id,
            artifact_refs=result.artifact_refs,
            evidence_refs=(*result.evidence_refs, *extra_refs),
        ),
    )
    return ControlPlaneEvidenceManifest(
        schema_name="captain.control-plane-evidence.v1",
        correlation_id=hermes.correlation_id,
        project_id=hermes.project_id,
        plan_version=hermes.subject_version,
        plan_digest=hermes.plan_ref.sha256,
        generated_at=now,
        status="succeeded",
        minibook_project_id=hermes.minibook.project_id,
        minibook_post_id=hermes.minibook.post_id,
        batch_order=(batch.batch_id,),
        completed_tasks=(result.subject_id,),
        behavioral_redos=0,
        infrastructure_failures=0,
        observations=observations,
    )


@pytest.mark.asyncio
async def test_real_codex_without_n8n_is_confined_and_restart_safe(tmp_path: Path) -> None:
    _require_live_environment(n8n=False)
    now = datetime.now(timezone.utc)
    correlation_id = uuid4()
    workspace = _worktree(tmp_path)
    workspace_ref = f"workspace://authorized/{correlation_id.hex}/code"
    prompt_ref = ArtifactRef(
        uri=f"artifact://live/prompt/{correlation_id}",
        sha256=hashlib.sha256(CODEX_PROMPT).hexdigest(),
        media_type="text/markdown",
    )
    hermes, batch, command, grant, _, minibook = await _released_command(
        correlation_id=correlation_id,
        workspace_ref=workspace_ref,
        prompt_ref=prompt_ref,
        n8n=False,
        now=now,
    )
    package = _package(command, grant, batch, now)
    runtime = CountingRuntime()
    state_root = tmp_path / "worker-state"
    codex_home_root = tmp_path / "codex-home"
    worker = _worker(
        workspace=workspace,
        package=package,
        runtime=runtime,
        state_root=state_root,
        codex_home_root=codex_home_root,
        configured_servers={},
        now=now,
    )
    try:
        record = worker.start(package, CODEX_PROMPT)
        result = worker.collect_result(package)
    finally:
        runtime.close()

    assert record.session_id
    assert record.changed_paths == ("captain_live_evidence.txt",)
    assert (workspace / "captain_live_evidence.txt").read_text(encoding="utf-8").strip() == "captain-runtime-live-ok"
    assert grant.mcp_servers == ()
    scoped_config = tomllib.loads(
        next(codex_home_root.rglob("config.toml")).read_text(encoding="utf-8")
    )
    scoped_mcp_servers = scoped_config.get("mcp_servers", {})
    assert isinstance(scoped_mcp_servers, dict)
    assert not any("n8n" in str(name).casefold() for name in scoped_mcp_servers)
    assert len(minibook.calls) == 1

    replay_runtime = CountingRuntime()
    replay_worker = _worker(
        workspace=workspace,
        package=package,
        runtime=replay_runtime,
        state_root=state_root,
        codex_home_root=codex_home_root,
        configured_servers={},
        now=now,
    )
    try:
        replay = replay_worker.start(package, CODEX_PROMPT)
    finally:
        replay_runtime.close()
    assert replay.session_id == record.session_id
    assert runtime.starts == 1
    assert replay_runtime.starts == 0
    parent_result = _runtime_result(result, command)
    manifest = _manifest(
        hermes=hermes,
        batch=batch,
        grant=grant,
        result=parent_result,
        now=now,
    )
    assert manifest.status == "succeeded"
    assert "n8n-mcp" not in manifest.model_dump_json()


@pytest.mark.asyncio
async def test_real_n8n_mcp_workflow_is_validated_published_executed_and_archived(
    tmp_path: Path,
) -> None:
    _require_live_environment(n8n=True)
    now = datetime.now(timezone.utc)
    correlation_id = uuid4()
    workspace = _worktree(tmp_path)
    workspace_ref = f"workspace://authorized/{correlation_id.hex}/n8n"
    prompt = _workflow_prompt(correlation_id)
    prompt_ref = ArtifactRef(
        uri=f"artifact://live/prompt/{correlation_id}",
        sha256=hashlib.sha256(prompt).hexdigest(),
        media_type="text/markdown",
    )
    hermes, batch, command, grant, _, minibook = await _released_command(
        correlation_id=correlation_id,
        workspace_ref=workspace_ref,
        prompt_ref=prompt_ref,
        n8n=True,
        now=now,
    )
    package = _package(command, grant, batch, now)
    configured = _configured_n8n_server()
    runtime = CountingRuntime()
    worker = _worker(
        workspace=workspace,
        package=package,
        runtime=runtime,
        state_root=tmp_path / "worker-state",
        codex_home_root=tmp_path / "codex-home",
        configured_servers=configured,
        now=now,
    )
    mcp = N8nWorkerMcp(
        grant=package.grant,
        configured_servers=configured,
        transport=HermesGenericMcpTransport(),
        state=JsonFileMcpCallStore(tmp_path / "mcp-state"),
        clock=lambda: now,
    )
    workflow_id: str | None = None
    cleanup_error: Exception | None = None
    try:
        assert set(await asyncio.to_thread(mcp.discover_capabilities)) == set(
            configured["n8n-mcp"]["tools"]["include"]
        )
        record = worker.start(package, prompt)
        workflow_path = workspace / "workflow.ts"
        assert record.session_id
        assert record.changed_paths == ("workflow.ts",)
        code = workflow_path.read_text(encoding="utf-8")
        assert code == _workflow_code(correlation_id)

        calls = []

        async def invoke(name: str, arguments: dict[str, Any]) -> Any:
            call = await asyncio.to_thread(
                mcp.invoke_tool,
                name,
                arguments,
                correlation_id,
                idempotency_key=f"{command.event_id}:{name}",
            )
            calls.append(call)
            worker.attach_mcp_evidence(package, call.evidence)
            return call

        validated = await invoke("validate_workflow", {"code": code})
        _require_n8n_success(validated.output)
        assert validated.output.get("valid") is True
        created = await invoke(
            "create_workflow_from_code",
            {
                "code": code,
                "name": f"Captain Runtime {correlation_id}",
                "description": f"Isolated Captain live gate {correlation_id}",
                "skillsUsed": ["captain-runtime"],
            },
        )
        _require_n8n_success(created.output)
        workflow_id = _find_id(created.output)
        assert workflow_id
        tested = await invoke(
            "test_workflow",
            {"workflowId": workflow_id, "pinData": {}},
        )
        published = await invoke("publish_workflow", {"workflowId": workflow_id})
        _require_n8n_success(tested.output)
        _require_n8n_success(published.output)
        executed = await invoke(
            "execute_workflow",
            {
                "workflowId": workflow_id,
                "executionMode": "production",
                "inputs": {
                    "type": "webhook",
                    "webhookData": {
                        "method": "POST",
                        "body": {"correlation_id": str(correlation_id)},
                        "headers": {},
                        "query": {},
                    },
                },
            },
        )
        _require_n8n_success(executed.output)
        assert tested.evidence.execution_id
        assert executed.evidence.execution_id
        assert all(call.evidence.call_id.startswith("captain-mcp-call-") for call in calls)
        assert all(call.evidence.input_digest and call.evidence.output_digest for call in calls)
        result = worker.collect_result(package)
        assert result.status.value == "succeeded"
        assert result.session_id == record.session_id
        assert result.mcp_evidence == tuple(call.evidence for call in calls)
        assert len(minibook.calls) == 1
        parent_result = _runtime_result(result, command)
        workflow_ref = ArtifactRef(
            uri=f"artifact://n8n/workflows/{workflow_id}",
            sha256=hashlib.sha256(code.encode()).hexdigest(),
            media_type="application/json",
        )
        execution_ref = ArtifactRef(
            uri=f"artifact://n8n/executions/{executed.evidence.execution_id}",
            sha256=executed.evidence.output_digest,
            media_type="application/json",
        )
        manifest = _manifest(
            hermes=hermes,
            batch=batch,
            grant=grant,
            result=parent_result,
            now=now,
            extra_refs=(workflow_ref, execution_ref),
        )
        serialized = manifest.model_dump_json()
        assert workflow_id in serialized
        assert executed.evidence.execution_id in serialized
        assert "N8N_MCP_TOKEN" not in serialized
    finally:
        runtime.close()
        if workflow_id is not None:
            try:
                archived = await asyncio.to_thread(
                    mcp.invoke_tool,
                    "archive_workflow",
                    {"workflowId": workflow_id},
                    correlation_id,
                    idempotency_key=f"{command.event_id}:archive_workflow",
                )
                _require_n8n_success(archived.output)
                assert archived.evidence.call_id.startswith("captain-mcp-call-")
            except Exception as exc:  # pragma: no cover - cleanup must report live failure
                cleanup_error = exc
        if cleanup_error is not None:
            pytest.fail(f"isolated n8n workflow cleanup failed: {type(cleanup_error).__name__}")


def _find_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "workflowId", "workflow_id"):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        for nested in value.values():
            found = _find_id(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_id(nested)
            if found is not None:
                return found
    return None


def _require_n8n_success(output: dict[str, Any]) -> None:
    status = str(output.get("status", "")).casefold()
    success = output.get("success")
    error = output.get("error")
    detail = error or output.get("message") or status or "unspecified n8n failure"
    assert status not in {"error", "failed", "failure"}, str(detail)
    assert success is not False, str(detail)
    assert not error, str(detail)
