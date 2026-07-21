from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.codebase_discovery import (
    CodebaseDiscoveryService,
    FilesystemRepositoryInspection,
)
from agenten.agent_factory.input_compiler import (
    AcceptanceAssertion,
    CompiledFactorySpecification,
    FactoryWorkNode,
)
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
REVISION = "1" * 40
JOB_ID = "00000000-0000-0000-0000-000000000501"
CORRELATION_ID = "00000000-0000-0000-0000-000000000502"
INVOCATION_ID = "00000000-0000-0000-0000-000000000503"
ASSERTION_ID = "assert-aaaaaaaaaaaa"


def artifact(name: str, *, media_type: str = "application/json") -> ArtifactRef:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://test/{name}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


class RecordingDocumentationPort:
    def __init__(self) -> None:
        self.queries: list[object] = []

    def resolve(self, query: object) -> tuple[ArtifactRef, ...]:
        self.queries.append(query)
        ecosystem = str(getattr(query, "ecosystem"))
        return (artifact(f"context7-{ecosystem}"),)


class RecordingToolCatalog:
    def __init__(self, matches: tuple[str, ...]) -> None:
        self.matches = matches
        self.requests: list[tuple[str, tuple[str, ...]]] = []

    def match(
        self, capability_key: str, reusable_component_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        self.requests.append((capability_key, reusable_component_ids))
        return self.matches


class RecordingEvidenceStore:
    def __init__(self) -> None:
        self.payloads: dict[str, object] = {}

    def seal(self, kind: str, payload: object) -> ArtifactRef:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        reference = ArtifactRef(
            uri=f"artifact://factory-discovery/{kind}/{digest}",
            sha256=digest,
            media_type="application/json",
        )
        self.payloads[reference.uri] = json.loads(encoded)
        return reference

    def read(self, reference: ArtifactRef) -> object:
        return self.payloads[reference.uri]


def factory_repo_fixture(
    tmp_path: Path,
    *,
    integration_intent: str = "none",
    optional_gap: bool = False,
) -> Path:
    root = tmp_path / "fixture-repository"
    files = {
        "requirements.txt": "autogen-agentchat==0.7.5\nautogen-ext[openai]==0.7.5\n",
        "agenten/workflows/existing_team.py": (
            "from autogen_agentchat.teams import Swarm\n"
            "from autogen_agentchat.conditions import TextMentionTermination\n"
            "from autogen_ext.models.openai import OpenAIChatCompletionClient\n\n"
            "SYSTEM_PROMPT = 'Use the typed customer tool.'\n"
            "MEMORY_POLICY = 'session'\n"
            "HANDOFFS = ('reviewer',)\n\n"
            "def build_team():\n"
            "    model_client = OpenAIChatCompletionClient(model='approved-model')\n"
            "    termination = TextMentionTermination('DONE')\n"
            "    return Swarm([], termination_condition=termination)\n"
        ),
        "agenten/tools/customer_lookup.py": (
            "from pydantic import BaseModel\n\n"
            "class CustomerLookupInput(BaseModel):\n"
            "    customer_id: str\n\n"
            "class CustomerLookupTool:\n"
            "    name = 'customer_lookup'\n"
        ),
        "prompts/support.md": "# System prompt\nUse customer context and typed handoffs.\n",
        "tests/test_existing_team.py": (
            "from agenten.workflows.existing_team import build_team\n\n"
            "def test_existing_team_builds():\n"
            "    assert build_team() is not None\n"
        ),
        "schemas/team.schema.json": json.dumps(
            {"$schema": "https://json-schema.org/draft/2020-12/schema"}
        ),
        "docs/ARCHITECTURE.md": (
            "# Architecture\nAutoGen Swarm with typed handoffs.\n"
            "TODO_TOOL.v1 markers are JSON contracts.\n"
        ),
        ".env": "DO_NOT_READ=private-fixture\nautogen-agentchat==99.99.99\n",
    }
    if integration_intent == "n8n":
        files["n8n/customer-sync.json"] = json.dumps(
            {"name": "customer-sync", "nodes": [{"type": "n8n-nodes-base.httpRequest"}]}
        )
    if optional_gap:
        files["docs/optional-gap.json"] = json.dumps(
            {
                "schema": "TODO_TOOL.v1",
                "gap_id": "optional-enrichment",
                "severity": "optional",
                "input_contract_ref": artifact("optional-input").model_dump(mode="json"),
                "output_contract_ref": artifact("optional-output").model_dump(mode="json"),
                "least_privilege_capability": "enrichment.read",
                "implementation_options": [],
                "acceptance_assertion_ids": [ASSERTION_ID],
                "evidence_ref": artifact("optional-gap-evidence").model_dump(mode="json"),
                "status": "unresolved",
            }
        )
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def compiled_spec(
    *, capability_key: str = "support_triage", integration_intent: str = "none"
) -> CompiledFactorySpecification:
    nodes = [FactoryWorkNode(node_id="architecture", kind="architecture")]
    if integration_intent == "n8n":
        nodes.append(
            FactoryWorkNode(
                node_id="n8n-customer-sync",
                kind="n8n_workflow",
                dependencies=("architecture",),
            )
        )
    return CompiledFactorySpecification(
        source_ref=artifact("compiled-input"),
        subject_version=1,
        capability_key=capability_key,
        assertions=(
            AcceptanceAssertion(
                assertion_id=ASSERTION_ID,
                source_path=("Acceptance outcomes", "support"),
                observable_setup="Given a support request",
                observable_action="Run the generated team",
                observable_expected="Return the typed supported response",
                kind="business",
            ),
        ),
        private_holdout_refs=(
            {
                "holdout_id": "holdout-bbbbbbbbbbbb",
                "uri": "holdout://holdout-bbbbbbbbbbbb",
                "sha256": "2" * 64,
            },
        ),
        work_nodes=tuple(nodes),
        dependency_order=tuple(node.node_id for node in nodes),
        compilation_digest="3" * 64,
    )


def invocation(specification: CompiledFactorySpecification) -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(
        {
            "schema": "captain.factory-skill-invocation.v1",
            "invocation_id": INVOCATION_ID,
            "job_id": JOB_ID,
            "correlation_id": CORRELATION_ID,
            "subject_version": 1,
            "attempt": 1,
            "step": "discover",
            "released_skill": {
                "schema": "captain.released-hermes-skill.v1",
                "skill_id": "captain-factory-discover",
                "version": 1,
                "capability": "factory_discovery",
                "content_ref": artifact("discover-skill"),
                "content_sha256": artifact("discover-skill").sha256,
                "status": "released",
                "released_at": NOW,
                "producer": "captain",
            },
            "input_ref": specification.source_ref,
            "input_sha256": specification.source_ref.sha256,
            "lease": {
                "schema": "captain.factory-lease.v1",
                "lease_id": "lease-discovery",
                "job_id": JOB_ID,
                "correlation_id": CORRELATION_ID,
                "subject_version": 1,
                "attempt": 1,
                "role": "agent_architect",
                "capability_profile": "factory-architect",
                "capabilities": ["repository.read", "context7.read"],
                "workspace_ref": "workspace://factory/discovery",
                "issued_at": NOW,
                "expires_at": NOW + timedelta(minutes=10),
            },
            "idempotency_key": "4" * 64,
            "acceptance_assertion_ids": specification.assertion_ids,
        }
    )


def service(
    root: Path,
    *,
    docs: RecordingDocumentationPort | None = None,
    matches: tuple[str, ...] = ("released.customer_lookup",),
) -> tuple[
    CodebaseDiscoveryService,
    FilesystemRepositoryInspection,
    RecordingDocumentationPort,
    RecordingToolCatalog,
    RecordingEvidenceStore,
]:
    repository = FilesystemRepositoryInspection(root, revision=REVISION)
    documentation = docs or RecordingDocumentationPort()
    catalog = RecordingToolCatalog(matches)
    evidence = RecordingEvidenceStore()
    return (
        CodebaseDiscoveryService(
            repository=repository,
            documentation=documentation,
            tool_catalog=catalog,
            evidence_store=evidence,
            clock=lambda: NOW + timedelta(minutes=1),
        ),
        repository,
        documentation,
        catalog,
        evidence,
    )


def test_discovery_finds_semantic_reuse_without_reading_secrets(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    specification = compiled_spec()
    discovery, repository, docs, catalog, evidence = service(root)

    inventory = discovery.discover(invocation(specification), specification)

    assert "agenten.workflows.existing_team" in inventory.reusable_component_ids
    assert "agenten.tools.customer_lookup" in inventory.reusable_component_ids
    assert any("agenten/workflows/existing_team.py" in ref.uri for ref in inventory.entrypoint_refs)
    assert any("tests/test_existing_team.py" in ref.uri for ref in inventory.test_refs)
    assert any("schemas/team.schema.json" in ref.uri for ref in inventory.schema_refs)
    assert inventory.autogen_version == "0.7.5"
    assert inventory.tool_catalog_match_ids == ("released.customer_lookup",)
    assert [query.ecosystem for query in docs.queries] == ["autogen"]
    assert catalog.requests[0][0] == "support_triage"
    assert ".env" not in repository.read_paths
    assert all(".env" not in ref.uri for ref in inventory.evidence_refs + inventory.source_refs)

    summary = evidence.read(inventory.artifact_ref)
    assert set(summary["categories"]) >= {
        "autogen",
        "entrypoint",
        "handoff",
        "memory",
        "model_client",
        "prompt",
        "schema",
        "termination",
        "test",
        "typed_tool",
    }


def test_n8n_docs_are_queried_only_for_declared_integration(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path, integration_intent="n8n")
    specification = compiled_spec(integration_intent="n8n")
    docs = RecordingDocumentationPort()
    discovery, _, _, _, _ = service(root, docs=docs)

    inventory = discovery.discover(invocation(specification), specification)

    assert [query.ecosystem for query in docs.queries] == ["autogen", "n8n"]
    assert any("context7-n8n" in ref.uri for ref in inventory.documentation_refs)
    assert any("n8n/customer-sync.json" in ref.uri for ref in inventory.source_refs)


@pytest.mark.parametrize(
    ("integration_intent", "expected_option_ids"),
    [
        ("none", {"reuse-released-tool", "implement-typed-local-adapter"}),
        (
            "n8n",
            {
                "reuse-released-tool",
                "implement-typed-local-adapter",
                "implement-typed-n8n-integration",
            },
        ),
    ],
)
def test_absent_required_capability_emits_bound_todo_tool(
    tmp_path: Path,
    integration_intent: str,
    expected_option_ids: set[str],
) -> None:
    root = factory_repo_fixture(tmp_path, integration_intent=integration_intent)
    specification = compiled_spec(
        capability_key="missing_crm_api", integration_intent=integration_intent
    )
    discovery, _, _, _, evidence = service(root, matches=())

    inventory = discovery.discover(invocation(specification), specification)

    assert len(inventory.gap_refs) == 1
    marker = ToolGapMarker.model_validate(evidence.read(inventory.gap_refs[0]))
    assert marker.severity == "required"
    assert marker.status == "unresolved"
    assert marker.acceptance_assertion_ids == specification.assertion_ids
    assert {option.option_id for option in marker.implementation_options} == expected_option_ids
    local_option = next(
        option
        for option in marker.implementation_options
        if option.option_id == "implement-typed-local-adapter"
    )
    assert "schema" in local_option.description.lower()
    assert "auth" in local_option.description.lower()
    assert "health" in local_option.description.lower()
    assert "idempotency" in local_option.description.lower()


def test_optional_todo_tool_remains_separately_classified(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path, optional_gap=True)
    specification = compiled_spec()
    discovery, _, _, _, evidence = service(root)

    inventory = discovery.discover(invocation(specification), specification)

    assert len(inventory.gap_refs) == 1
    marker = ToolGapMarker.model_validate(evidence.read(inventory.gap_refs[0]))
    assert marker.gap_id == "optional-enrichment"
    assert marker.severity == "optional"
    assert marker.status == "unresolved"


def test_repository_reader_rejects_secrets_and_scope_escape(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    repository = FilesystemRepositoryInspection(root, revision=REVISION)

    with pytest.raises(ValueError, match="excluded"):
        repository.read_text(Path(".env"))
    with pytest.raises(ValueError, match="relative|scope"):
        repository.read_text(Path("../outside.py"))
