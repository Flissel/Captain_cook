from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest

from agenten.agent_factory.codebase_discovery import (
    CodebaseDiscoveryService,
    FilesystemRepositoryInspection,
    SourceMatch,
    WorktreeObservation,
)
from agenten.agent_factory.forge_contracts import DocumentationEvidence, DocumentationQuery
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
SECOND_ASSERTION_ID = "assert-cccccccccccc"
THIRD_ASSERTION_ID = "assert-dddddddddddd"
FOURTH_ASSERTION_ID = "assert-eeeeeeeeeeee"
ASSERTION_IDS = (
    ASSERTION_ID,
    SECOND_ASSERTION_ID,
    THIRD_ASSERTION_ID,
    FOURTH_ASSERTION_ID,
)


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

    def resolve(self, query: DocumentationQuery) -> DocumentationEvidence:
        self.queries.append(query)
        query_payload = query.model_dump(mode="json")
        query_sha256 = hashlib.sha256(
            json.dumps(query_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        source_ref = artifact(f"context7-{query.ecosystem}", media_type="text/html")
        return DocumentationEvidence(
            query=query,
            query_sha256=query_sha256,
            retrieved_version=("0.7.5" if query.ecosystem == "autogen" else "1.100.0"),
            retrieved_at=NOW,
            source_refs=(source_ref.model_dump(mode="json"),),
            content_sha256=source_ref.sha256,
        )


class RecordingPackageMetadata:
    def __init__(self, version: str = "0.7.5") -> None:
        self.version = version
        self.requests: list[str] = []

    def installed_version(self, distribution: str) -> str:
        self.requests.append(distribution)
        return self.version


class RecordingGitWorktrees:
    def __init__(
        self,
        observations: tuple[WorktreeObservation, ...] | None = None,
    ) -> None:
        self.observations = observations or (
            WorktreeObservation(
                revision=REVISION,
                relative_name=".",
                branch="fixture-branch",
                dirty=True,
            ),
        )
        self.roots: list[Path] = []

    def observe(self, root: Path) -> tuple[WorktreeObservation, ...]:
        self.roots.append(root)
        return self.observations


class DisorderedRepository:
    def __init__(
        self,
        delegate: FilesystemRepositoryInspection,
        observations: tuple[WorktreeObservation, ...],
    ) -> None:
        self._delegate = delegate
        self._observations = observations

    def revision(self) -> str:
        return self._delegate.revision()

    def worktrees(self) -> tuple[WorktreeObservation, ...]:
        return self._observations

    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]:
        matches = self._delegate.search(pattern, globs)
        return tuple(reversed(matches)) + matches[:2]

    def read_text(self, relative_path: PurePosixPath) -> str:
        return self._delegate.read_text(relative_path)


class MutatingRepository:
    def __init__(
        self,
        delegate: FilesystemRepositoryInspection,
        changed_path: Path,
    ) -> None:
        self._delegate = delegate
        self._changed_path = changed_path

    def revision(self) -> str:
        return self._delegate.revision()

    def worktrees(self) -> tuple[WorktreeObservation, ...]:
        return self._delegate.worktrees()

    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]:
        matches = self._delegate.search(pattern, globs)
        self._changed_path.write_text(
            self._changed_path.read_text(encoding="utf-8") + "\n# changed after search\n",
            encoding="utf-8",
        )
        return matches

    def read_text(self, relative_path: PurePosixPath) -> str:
        return self._delegate.read_text(relative_path)


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
        "requirements.txt": "autogen-agentchat>=0.1.0\nautogen-ext[openai]>=0.1.0\n",
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
        "agenten/workflows/existing_specialist_team.py": (
            "from autogen_agentchat.teams import Swarm as AgentSwarm\n\n"
            "def build_workflow():\n"
            "    workflow = AgentSwarm([])\n"
            "    return workflow\n"
        ),
        "agenten/workflows/cli.py": (
            "from autogen_agentchat.teams import Swarm\n\n"
            "def launch():\n"
            "    return Swarm([])\n\n"
            "if __name__ == '__main__':\n"
            "    launch()\n"
        ),
        "agenten/tools/customer_lookup.py": (
            "from pydantic import BaseModel\n\n"
            "class CustomerLookupInput(BaseModel):\n"
            "    customer_id: str\n\n"
            "class CustomerLookupTool:\n"
            "    name = 'customer_lookup'\n"
        ),
        "agenten/not_a_tool.py": (
            "from pydantic import BaseModel\n\n"
            "class OrdinaryInput(BaseModel):\n"
            "    value: str\n\n"
            "def tool():\n"
            "    return 'ordinary helper'\n\n"
            "@not_a_tool\n"
            "def ordinary_helper(value: OrdinaryInput):\n"
            "    return value.value\n\n"
            "def build_report():\n"
            "    return 'ordinary report'\n"
        ),
        "agenten/tools/decorated_lookup.py": (
            "from agenten.contracts.customer import DecoratedLookupInput\n"
            "from autogen_core.tools import tool\n\n"
            "@tool\n"
            "def lookup_customer(value: DecoratedLookupInput) -> str:\n"
            "    return value.customer_id\n"
        ),
        "agenten/contracts/customer.py": (
            "from pydantic import BaseModel\n\n"
            "class DecoratedLookupInput(BaseModel):\n"
            "    customer_id: str\n"
        ),
        "agenten/workflows/build_report_only.py": (
            "from autogen_agentchat.teams import Swarm\n\n"
            "def build_report():\n"
            "    return 'ordinary report'\n"
        ),
        "agenten/workflows/fake_swarm.py": (
            "from local_fake.teams import Swarm\n\n"
            "def build_workflow():\n"
            "    return Swarm([])\n"
        ),
        "agenten/workflows/graph_builder.py": (
            "from autogen_agentchat.teams import DiGraphBuilder\n\n"
            "def build_graph():\n"
            "    return DiGraphBuilder()\n"
        ),
        "agenten/workflows/graph_flow.py": (
            "from autogen_agentchat.teams import GraphFlow\n\n"
            "def build_workflow():\n"
            "    return GraphFlow()\n"
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
        ".ENV.production": "DO_NOT_READ=case-insensitive-fixture\n",
        "secrets/customer_api_key.txt": "AutoGen model_client must-not-be-read\n",
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
    *,
    capability_key: str = "support_triage",
    integration_intent: str = "none",
    assertion_count: int = 1,
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
    if assertion_count < 1 or assertion_count > len(ASSERTION_IDS):
        raise ValueError("fixture assertion count is out of range")
    assertions = [
        AcceptanceAssertion(
            assertion_id=assertion_id,
            source_path=("Acceptance outcomes", f"case-{index}"),
            observable_setup=f"Given acceptance case {index}",
            observable_action="Run the generated team",
            observable_expected="Return typed evidence",
            kind="business",
        )
        for index, assertion_id in enumerate(
            ASSERTION_IDS[:assertion_count],
            start=1,
        )
    ]
    return CompiledFactorySpecification(
        source_ref=artifact("compiled-input"),
        subject_version=1,
        capability_key=capability_key,
        assertions=tuple(assertions),
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


def invocation(
    specification: CompiledFactorySpecification,
    *,
    capabilities: tuple[str, ...] = ("repository.read", "context7.read"),
) -> FactorySkillInvocationV1:
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
                "capabilities": capabilities,
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
    git_worktrees: RecordingGitWorktrees | None = None,
    package_metadata: RecordingPackageMetadata | None = None,
) -> tuple[
    CodebaseDiscoveryService,
    FilesystemRepositoryInspection,
    RecordingDocumentationPort,
    RecordingToolCatalog,
    RecordingEvidenceStore,
]:
    repository = FilesystemRepositoryInspection(
        root,
        expected_revision=REVISION,
        git_worktrees=git_worktrees or RecordingGitWorktrees(),
    )
    documentation = docs or RecordingDocumentationPort()
    catalog = RecordingToolCatalog(matches)
    evidence = RecordingEvidenceStore()
    return (
        CodebaseDiscoveryService(
            repository=repository,
            documentation=documentation,
            tool_catalog=catalog,
            evidence_store=evidence,
            package_metadata=package_metadata or RecordingPackageMetadata(),
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
    assert "agenten.workflows.existing_specialist_team" in inventory.reusable_component_ids
    assert "agenten.workflows.cli" in inventory.reusable_component_ids
    assert "agenten.tools.customer_lookup" in inventory.reusable_component_ids
    assert "agenten.tools.decorated_lookup" in inventory.reusable_component_ids
    assert "agenten.workflows.graph_builder" in inventory.reusable_component_ids
    assert "agenten.workflows.graph_flow" in inventory.reusable_component_ids
    assert any("agenten/workflows/existing_team.py" in ref.uri for ref in inventory.entrypoint_refs)
    assert any("tests/test_existing_team.py" in ref.uri for ref in inventory.test_refs)
    assert any("schemas/team.schema.json" in ref.uri for ref in inventory.schema_refs)
    assert inventory.autogen_version == "0.7.5"
    assert inventory.tool_catalog_match_ids == ("released.customer_lookup",)
    assert [query.ecosystem for query in docs.queries] == ["autogen"]
    assert catalog.requests[0][0] == "support_triage"
    assert "agenten.not_a_tool" not in inventory.reusable_component_ids
    assert "agenten.workflows.build_report_only" not in inventory.reusable_component_ids
    assert "agenten.workflows.fake_swarm" not in inventory.reusable_component_ids
    assert not any(
        "agenten/workflows/graph_builder.py" in ref.uri
        for ref in inventory.entrypoint_refs
    )
    assert any(
        "agenten/workflows/graph_flow.py" in ref.uri
        for ref in inventory.entrypoint_refs
    )
    assert not any("agenten/not_a_tool.py" in ref.uri for ref in inventory.entrypoint_refs)
    assert ".env" not in repository.read_paths
    assert ".ENV.production" not in repository.read_paths
    assert "secrets/customer_api_key.txt" not in repository.read_paths
    assert all(
        ".env" not in ref.uri.lower()
        for ref in inventory.evidence_refs + inventory.source_refs
    )

    worktrees_ref = next(ref for ref in inventory.evidence_refs if "/worktrees/" in ref.uri)
    assert evidence.read(worktrees_ref) == [
        {
            "branch": "fixture-branch",
            "detached": False,
            "dirty": True,
            "relative_name": ".",
            "revision": REVISION,
        }
    ]
    search_ref = next(
        ref for ref in inventory.evidence_refs if "/semantic-search/" in ref.uri
    )
    search_evidence = evidence.read(search_ref)
    assert any(item["symbol"] == "build_team" for item in search_evidence)
    assert any(item["symbol"] == "build_workflow" for item in search_evidence)
    assert any(item["symbol"] == "lookup_customer" for item in search_evidence)

    documentation = evidence.read(inventory.documentation_refs[0])
    assert documentation["query"]["ecosystem"] == "autogen"
    assert documentation["query"]["installed_version"] == "0.7.5"
    assert documentation["retrieved_version"] == "0.7.5"
    assert len(documentation["query_sha256"]) == 64

    summary = evidence.read(inventory.artifact_ref)
    assert set(summary["categories"]) >= {
        "autogen",
        "autogen_component",
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
    discovery, _, _, _, service_evidence = service(root, docs=docs)

    inventory = discovery.discover(invocation(specification), specification)

    assert [query.ecosystem for query in docs.queries] == ["autogen", "n8n"]
    assert any("n8n/customer-sync.json" in ref.uri for ref in inventory.source_refs)
    provenance = [
        evidence
        for ref in inventory.documentation_refs
        if (evidence := service_evidence.read(ref))["query"]["ecosystem"] == "n8n"
    ]
    assert provenance[0]["query"]["installed_version"] == "declared-intent"
    assert provenance[0]["retrieved_version"] == "1.100.0"
    assert len(provenance[0]["query_sha256"]) == 64


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


@pytest.mark.parametrize(
    ("capabilities", "missing"),
    [
        (("context7.read",), "repository.read"),
        (("repository.read",), "context7.read"),
    ],
)
def test_discovery_rejects_missing_lease_capability_before_effects(
    tmp_path: Path,
    capabilities: tuple[str, ...],
    missing: str,
) -> None:
    root = factory_repo_fixture(tmp_path)
    specification = compiled_spec()
    discovery, repository, docs, catalog, _ = service(root)

    with pytest.raises(PermissionError, match=missing):
        discovery.discover(
            invocation(specification, capabilities=capabilities),
            specification,
        )

    assert repository.read_paths == ()
    assert docs.queries == []
    assert catalog.requests == []


def test_filesystem_adapter_uses_real_git_state_and_rejects_revision_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "git-repository"
    root.mkdir()
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    commands = (
        ("init", "-q"),
        ("config", "user.email", "factory@example.invalid"),
        ("config", "user.name", "Factory Test"),
        ("add", "tracked.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    )
    for args in commands:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    first_revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (root / "tracked.txt").write_text("second commit\n", encoding="utf-8")
    for args in (
        ("add", "tracked.txt"),
        ("commit", "-q", "-m", "test: advance fixture"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    secondary_root = tmp_path / "secondary-worktree"
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "worktree",
            "add",
            "--detach",
            "-q",
            str(secondary_root),
            first_revision,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (secondary_root / "tracked.txt").write_text("detached dirty\n", encoding="utf-8")

    repository = FilesystemRepositoryInspection(root, expected_revision=revision)
    clean = repository.worktrees()

    assert len(clean) == 2
    assigned = next(item for item in clean if item.relative_name == ".")
    detached = next(item for item in clean if item.relative_name != ".")
    assert assigned.revision == revision
    assert assigned.branch
    assert assigned.detached is False
    assert assigned.dirty is False
    assert detached.revision == first_revision
    assert detached.branch is None
    assert detached.detached is True
    assert detached.dirty is True

    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    dirty = FilesystemRepositoryInspection(root, expected_revision=revision).worktrees()
    assert next(item for item in dirty if item.relative_name == ".").dirty is True

    with pytest.raises(ValueError, match="revision mismatch"):
        FilesystemRepositoryInspection(root, expected_revision="f" * 40)


def test_service_sorts_and_deduplicates_port_observations(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    primary = WorktreeObservation(
        revision=REVISION,
        relative_name=".",
        branch="fixture-branch",
        dirty=True,
    )
    secondary = WorktreeObservation(
        revision=REVISION,
        relative_name="secondary",
        branch="secondary-branch",
        dirty=False,
    )
    base = FilesystemRepositoryInspection(
        root,
        expected_revision=REVISION,
        git_worktrees=RecordingGitWorktrees((primary,)),
    )
    repository = DisorderedRepository(base, (secondary, primary, secondary))
    docs = RecordingDocumentationPort()
    catalog = RecordingToolCatalog(("released.customer_lookup",))
    evidence = RecordingEvidenceStore()
    discovery = CodebaseDiscoveryService(
        repository=repository,
        documentation=docs,
        tool_catalog=catalog,
        evidence_store=evidence,
        package_metadata=RecordingPackageMetadata(),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    specification = compiled_spec()

    inventory = discovery.discover(invocation(specification), specification)

    worktree_ref = next(ref for ref in inventory.evidence_refs if "/worktrees/" in ref.uri)
    worktrees = evidence.read(worktree_ref)
    assert [item["relative_name"] for item in worktrees] == [".", "secondary"]
    search_ref = next(
        ref for ref in inventory.evidence_refs if "/semantic-search/" in ref.uri
    )
    matches = evidence.read(search_ref)
    identities = [
        (item["relative_path"], item["line"], item["symbol"], item["content_sha256"])
        for item in matches
    ]
    assert identities == sorted(set(identities))


def test_discovery_rejects_source_changed_between_search_and_read(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    base = FilesystemRepositoryInspection(
        root,
        expected_revision=REVISION,
        git_worktrees=RecordingGitWorktrees(),
    )
    repository = MutatingRepository(
        base,
        root / "agenten" / "workflows" / "existing_team.py",
    )
    docs = RecordingDocumentationPort()
    catalog = RecordingToolCatalog(("released.customer_lookup",))
    evidence = RecordingEvidenceStore()
    discovery = CodebaseDiscoveryService(
        repository=repository,
        documentation=docs,
        tool_catalog=catalog,
        evidence_store=evidence,
        package_metadata=RecordingPackageMetadata(),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    specification = compiled_spec()

    with pytest.raises(ValueError, match="changed|snapshot"):
        discovery.discover(invocation(specification), specification)

    assert docs.queries == []
    assert catalog.requests == []
    assert evidence.payloads == {}


def test_required_gap_options_cover_every_blocked_assertion(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path, integration_intent="n8n")
    specification = compiled_spec(
        capability_key="missing_crm_api",
        integration_intent="n8n",
        assertion_count=2,
    )
    discovery, _, _, _, evidence = service(root, matches=())

    inventory = discovery.discover(invocation(specification), specification)

    marker = ToolGapMarker.model_validate(evidence.read(inventory.gap_refs[0]))
    assert {
        option.acceptance_assertion_id for option in marker.implementation_options
    } == set(specification.assertion_ids)


def test_required_gaps_preserve_more_than_three_blocked_assertions(
    tmp_path: Path,
) -> None:
    root = factory_repo_fixture(tmp_path)
    specification = compiled_spec(
        capability_key="missing_crm_api",
        assertion_count=4,
    )
    discovery, _, _, _, evidence = service(root, matches=())

    inventory = discovery.discover(invocation(specification), specification)

    markers = [
        ToolGapMarker.model_validate(evidence.read(reference))
        for reference in inventory.gap_refs
    ]
    assert len(markers) == 2
    assert {
        assertion_id
        for marker in markers
        for assertion_id in marker.acceptance_assertion_ids
    } == set(specification.assertion_ids)
    for marker in markers:
        assert len(marker.implementation_options) <= 3
        assert {
            option.acceptance_assertion_id
            for option in marker.implementation_options
        } == set(marker.acceptance_assertion_ids)


def test_repository_reader_rejects_secrets_and_scope_escape(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    repository = FilesystemRepositoryInspection(
        root,
        expected_revision=REVISION,
        git_worktrees=RecordingGitWorktrees(),
    )

    with pytest.raises(ValueError, match="excluded"):
        repository.read_text(Path(".env"))
    with pytest.raises(ValueError, match="excluded"):
        repository.read_text(Path(".ENV.production"))
    with pytest.raises(ValueError, match="excluded"):
        repository.read_text(Path("secrets/customer_api_key.txt"))
    with pytest.raises(ValueError, match="relative|scope"):
        repository.read_text(Path("../outside.py"))


def test_repository_rejects_symlink_before_resolving_secret_target(tmp_path: Path) -> None:
    root = factory_repo_fixture(tmp_path)
    link = root / "public_source.py"
    link.symlink_to(root / ".env")
    repository = FilesystemRepositoryInspection(
        root,
        expected_revision=REVISION,
        git_worktrees=RecordingGitWorktrees(),
    )

    with pytest.raises(ValueError, match="symlink"):
        repository.read_text(Path("public_source.py"))
