from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
import yaml

from agenten.agent_runtime.contracts import (
    ArtifactRef,
    HermesPlanResult,
)
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    CaptainPipeline,
    PlannedSubtask,
)
from agenten.planning.hermes_plan import (
    HermesPlanReader,
    HermesPlanningError,
    ValidatedPlanningInput,
)
from agenten.planning.policy import PlanningPolicy
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, ExampleCase


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def ref(name: str, content: bytes, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://hermes/{name}",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
    )


class FakeArtifacts:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.reads: list[str] = []

    async def read(self, reference: ArtifactRef) -> bytes:
        self.reads.append(reference.uri)
        return self.values[reference.uri]


def blueprint_bytes(*, intent: str = "n8n") -> bytes:
    value = {
        "schema": "captain.agent-blueprint.v1",
        "name": "integration_builder",
        "purpose": "Design the approved integration agent contract.",
        "inputs": {"project_context": "object"},
        "outputs": {"implementation_plan": "object"},
        "system_prompt_ref": {
            "uri": "artifact://hermes/system-prompt",
            "sha256": "f" * 64,
            "media_type": "text/markdown",
        },
        "tools": ["knowledge.search"],
        "integration_intent": intent,
        "n8n_tool_families": ["workflow"] if intent == "n8n" else [],
        "handoffs": ["captain.decompose"],
        "limits": {"max_turns": 8, "wall_seconds": 300},
        "evaluation_cases": [
            {
                "case_id": "bounded-tools",
                "assertion": "tool_allowlist_enforced",
            }
        ],
    }
    return yaml.safe_dump(value, sort_keys=True).encode("utf-8")


def plan_fixture(
    *,
    project_id: str = "project-1",
    blueprint_intent: str = "n8n",
) -> tuple[HermesPlanResult, FakeArtifacts]:
    blueprint = blueprint_bytes(intent=blueprint_intent)
    blueprint_ref = ref("blueprint-1", blueprint, "application/yaml")
    document = json.dumps(
        {
            "schema": "captain.hermes-planning-document.v1",
            "project_id": project_id,
            "correlation_id": str(UUID(int=2)),
            "subject_version": 3,
            "objective": "Build the approved integration from typed contracts.",
            "planner_id": "hermes-planner-1",
            "blueprint_digests": [blueprint_ref.sha256],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    decision_log = b"Approved assumptions and bounded risks."
    plan_ref = ref("plan-1", document, "application/json")
    decision_ref = ref("decision-1", decision_log, "text/markdown")
    result = HermesPlanResult(
        schema_name="captain.hermes-plan-result.v1",
        project_id="project-1",
        correlation_id=UUID(int=2),
        subject_version=3,
        plan_ref=plan_ref,
        decision_log_ref=decision_ref,
        blueprint_refs=(blueprint_ref,),
        integration_intents=(blueprint_intent,),
        minibook={"project_id": "project-1", "post_id": "post-1"},
        planner_id="hermes-planner-1",
        runtime_provenance="hermes-agent/worker-v1",
        started_at=NOW,
        ended_at=NOW,
    )
    artifacts = FakeArtifacts(
        {
            plan_ref.uri: document,
            decision_ref.uri: decision_log,
            blueprint_ref.uri: blueprint,
        }
    )
    return result, artifacts


@pytest.mark.asyncio
async def test_reader_validates_plan_blueprints_and_provenance() -> None:
    result, artifacts = plan_fixture()

    accepted = await HermesPlanReader(artifacts).read(result)

    assert accepted.project_id == result.project_id
    assert accepted.subject_version == result.subject_version
    assert accepted.plan_ref.sha256 == result.plan_ref.sha256
    assert accepted.planner_id == result.planner_id
    assert accepted.runtime_provenance == result.runtime_provenance
    assert accepted.blueprints[0].name == "integration_builder"
    assert artifacts.reads == [
        result.plan_ref.uri,
        result.decision_log_ref.uri,
        result.blueprint_refs[0].uri,
    ]


@pytest.mark.asyncio
async def test_reader_rejects_tampered_content_before_decomposition() -> None:
    result, artifacts = plan_fixture()
    artifacts.values[result.plan_ref.uri] += b"tampered"

    with pytest.raises(HermesPlanningError, match="digest"):
        await HermesPlanReader(artifacts).read(result)


@pytest.mark.asyncio
async def test_reader_rejects_project_and_intent_mismatch() -> None:
    wrong_project, project_artifacts = plan_fixture(project_id="project-other")
    with pytest.raises(HermesPlanningError, match="project"):
        await HermesPlanReader(project_artifacts).read(wrong_project)

    wrong_intent, intent_artifacts = plan_fixture(blueprint_intent="none")
    wrong_intent = wrong_intent.model_copy(update={"integration_intents": ("n8n",)})
    with pytest.raises(HermesPlanningError, match="integration intents"):
        await HermesPlanReader(intent_artifacts).read(wrong_intent)


class RecordingRelease:
    async def release(self, batch: Any, holdouts: Any) -> None:
        raise AssertionError("compile_from_plan must not release")


def pipeline(*, plan_reader: HermesPlanReader | None = None) -> CaptainPipeline:
    async def decompose(description: str) -> list[PlannedSubtask]:
        assert description == "Build the approved integration from typed contracts."
        return [
            PlannedSubtask(
                subtask_id="subtask-1",
                description="Build the integration",
            )
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
                    batch_id="batch-1",
                    title="Integration",
                    subtask_ids=["subtask-1"],
                    target="n8n",
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
            goal="Implement the typed integration.",
            capability_tags=["delivery"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="workflow-valid",
                    kind=AssertionKind.STATUS_EQUALS,
                    path="status",
                    expected="valid",
                )
            ],
            golden_cases=[
                ExampleCase(case_id="visible", input={"request": "sample"})
            ],
            holdout_cases=[
                ExampleCase(case_id="private", input={"request": "sealed"})
            ],
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=RecordingRelease(),
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="n8n",
        allowed_targets=frozenset({"n8n"}),
        plan_reader=plan_reader,
    )


@pytest.mark.asyncio
async def test_captain_decomposes_validated_hermes_plan() -> None:
    result, artifacts = plan_fixture()
    source = await HermesPlanReader(artifacts).read(result)

    compilation = await pipeline().compile_from_plan(source)

    assert compilation.source_plan_version == source.subject_version
    assert compilation.source_plan_digest == source.plan_ref.sha256
    assert compilation.compiled.batches


@pytest.mark.asyncio
async def test_n8n_intent_becomes_released_capability_not_secret_text() -> None:
    result, artifacts = plan_fixture()
    source = await HermesPlanReader(artifacts).read(result)

    compilation = await pipeline().compile_from_plan(source)

    batch = compilation.compiled.batches[0]
    assert batch.target == "n8n"
    assert batch.capability_tags == ["delivery", "n8n-builder"]
    assert "N8N_MCP_TOKEN" not in batch.model_dump_json()
    assert "system_prompt" not in batch.model_dump_json()


@pytest.mark.asyncio
async def test_injected_reader_supports_one_step_hermes_result_compilation() -> None:
    result, artifacts = plan_fixture()
    reader = HermesPlanReader(artifacts)

    compilation = await pipeline(plan_reader=reader).compile_from_hermes_result(result)

    assert compilation.source_plan_digest == result.plan_ref.sha256


def test_validated_input_cannot_be_constructed_with_unvalidated_dicts() -> None:
    with pytest.raises(ValueError):
        ValidatedPlanningInput.model_validate(
            {
                "project_id": "project-1",
                "correlation_id": str(UUID(int=2)),
                "subject_version": 3,
                "plan_ref": {"uri": "artifact://bad"},
                "objective": "unsafe",
                "blueprints": [{"name": "unsafe"}],
            }
        )
