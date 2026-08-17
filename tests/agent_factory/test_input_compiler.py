from __future__ import annotations

from graphlib import TopologicalSorter
import json
from pathlib import Path

from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_factory.input_contracts import FactoryInputDocumentV2
from agenten.agent_factory.input_document import load_factory_input


FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "TO_BE_BUILT.valid.md"
SALES_PIPELINE_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "agent_factory"
    / "sales_pipeline_brief.v3.json"
)


def document(tmp_path: Path):
    path = tmp_path / "TO_BE_BUILT.md"
    path.write_bytes(FIXTURE.read_bytes())
    return load_factory_input(path)


def compiler() -> tuple[FactoryInputCompiler, InMemoryPrivateHoldoutStore]:
    store = InMemoryPrivateHoldoutStore()
    return FactoryInputCompiler(holdout_store=store), store


def test_compiler_emits_stable_assertions_holdout_refs_and_topological_dag(tmp_path: Path) -> None:
    service, store = compiler()
    first = service.compile(document(tmp_path), subject_version=1)
    second = service.compile(document(tmp_path), subject_version=1)

    assert first == second
    assert first.source_ref == document(tmp_path).input_ref
    assert set(first.assertion_ids) == {item.assertion_id for item in first.assertions}
    assert all(ref.uri.startswith("holdout://") for ref in first.private_holdout_refs)
    assert tuple(TopologicalSorter(first.dependencies).static_order())
    assert len(store) == len(first.private_holdout_refs)
    private_body = store.get(first.private_holdout_refs[0].uri)
    assert "observable_expected" in private_body
    assert private_body not in first.model_dump_json()


def test_compiler_preserves_business_oracles_and_assigns_each_requested_item_once(tmp_path: Path) -> None:
    compiled, _ = compiler()
    result = compiled.compile(document(tmp_path), subject_version=2)

    expected = {assertion.observable_expected for assertion in result.assertions}
    assert "The request is classified as billing and high urgency" in expected
    owned_agents = [key for node in result.work_nodes for key in node.agent_keys]
    owned_integrations = [key for node in result.work_nodes for key in node.integration_keys]
    assert owned_agents == ["triage_agent", "response_agent"]
    assert owned_integrations == ["crm"]
    assert any(node.kind == "n8n_workflow" for node in result.work_nodes)
    assert not any("response_agent" in node.agent_keys and node.kind == "n8n_workflow" for node in result.work_nodes)


def test_subject_version_changes_compilation_identity_not_source(tmp_path: Path) -> None:
    service, _ = compiler()
    first = service.compile(document(tmp_path), subject_version=1)
    second = service.compile(document(tmp_path), subject_version=2)
    assert first.source_ref == second.source_ref
    assert first.compilation_digest != second.compilation_digest


def test_public_handoff_fixture_contains_refs_but_no_holdout_body() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "compiled_factory_spec.v1.json"
    source = fixture.read_text(encoding="utf-8")
    parsed = CompiledFactorySpecification.model_validate(json.loads(source))
    assert parsed.private_holdout_refs[0].uri.startswith("holdout://")
    assert "controlled recovery" not in source.casefold()


def test_sales_pipeline_compiler_freezes_authoritative_v3_team_requirements() -> None:
    fixture = json.loads(SALES_PIPELINE_FIXTURE.read_text(encoding="utf-8"))
    source = FactoryInputDocumentV2.model_validate(fixture["document"])
    compiled, _ = compiler()

    result = compiled.compile(source, subject_version=3)

    expected = fixture["expected"]
    assert result.required_agent_ids == tuple(expected["required_agent_ids"])
    assert [item.model_dump(mode="json") for item in result.ordered_handoffs] == expected[
        "ordered_handoffs"
    ]
    assert [item.model_dump(mode="json") for item in result.tool_requirements] == expected[
        "tool_requirements"
    ]
    assert result.allowed_optional_tool_gaps == tuple(
        expected["allowed_optional_tool_gaps"]
    )
    assert result.public_assertion_ids == tuple(expected["public_assertion_ids"])
    assert result.public_assertion_ids == result.assertion_ids
