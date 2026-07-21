from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input


DEMO_ROOT = Path(__file__).parents[2] / "demo_inputs" / "agent_factory"
MANIFEST_PATH = DEMO_ROOT / "manifest.json"
EXPECTED_PATTERNS = {
    "sequential",
    "selector_group_chat",
    "handoff_swarm",
    "reflection_retry",
    "tool_led_n8n",
}
EXPECTED_INPUTS = (
    ("sales_pipeline_brief", "sequential"),
    ("incident_command", "selector_group_chat"),
    ("claims_resolution", "handoff_swarm"),
    ("proposal_refinement", "reflection_retry"),
    ("renewal_orchestration", "tool_led_n8n"),
)


def _manifest_entries() -> list[dict[str, object]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schema_name"] == "captain.demo-input-manifest.v1"
    entries = manifest["inputs"]
    assert isinstance(entries, list)
    return entries


def _section(document, heading: str) -> str:
    return next(section.markdown for section in document.extra_sections if section.heading == heading)


def test_demo_input_manifest_covers_distinct_conversation_patterns() -> None:
    entries = _manifest_entries()

    assert len(entries) == 5
    assert {entry["pattern"] for entry in entries} == EXPECTED_PATTERNS
    assert len({entry["input_id"] for entry in entries}) == len(entries)
    assert len({entry["path"] for entry in entries}) == len(entries)


@pytest.mark.parametrize(("input_id", "pattern"), EXPECTED_INPUTS, ids=[item[0] for item in EXPECTED_INPUTS])
def test_demo_input_is_strict_compilable_unique_and_manifest_aligned(input_id: str, pattern: str) -> None:
    entry = next(item for item in _manifest_entries() if item["input_id"] == input_id)
    relative_path = Path(str(entry["path"]))
    fixture_path = (DEMO_ROOT / relative_path).resolve()
    assert fixture_path.is_relative_to(DEMO_ROOT.resolve())
    assert fixture_path.name == "TO_BE_BUILT.md"

    document = load_factory_input(fixture_path)
    store = InMemoryPrivateHoldoutStore()
    compiled = FactoryInputCompiler(holdout_store=store).compile(document, subject_version=1)

    assert document.title == entry["title"]
    assert document.byte_length >= 4_000
    assert len(document.agents) >= 3
    assert len(document.acceptance_outcomes) >= 3
    assert len(document.real_cases) >= 3
    assert compiled.private_holdout_refs
    assert all(ref.uri.startswith("holdout://") for ref in compiled.private_holdout_refs)
    assert len(store) == len(compiled.private_holdout_refs)

    assert entry["pattern"] == pattern
    expected_behavior = str(entry["expected_demo_behavior"])
    required_tools = tuple(str(item) for item in entry["required_tools"])
    optional_tools = tuple(str(item) for item in entry["optional_tools"])
    holdout_policy = str(entry["holdout_policy"])
    tool_policy = _section(document, "Tool policy")

    assert pattern in _section(document, "AutoGen conversation pattern")
    assert expected_behavior in {case.observable_expected for case in document.real_cases}
    assert required_tools
    assert len(required_tools) == len(set(required_tools))
    assert set(required_tools).isdisjoint(optional_tools)
    assert all(tool in tool_policy for tool in required_tools + optional_tools)
    assert holdout_policy in _section(document, "Private holdout policy")


def test_demo_inputs_have_unique_source_and_compilation_identities() -> None:
    source_digests: set[str] = set()
    compilation_digests: set[str] = set()
    capability_keys: set[str] = set()

    for entry in _manifest_entries():
        document = load_factory_input(DEMO_ROOT / str(entry["path"]))
        compiled = FactoryInputCompiler(holdout_store=InMemoryPrivateHoldoutStore()).compile(
            document,
            subject_version=1,
        )
        source_digests.add(document.input_ref.sha256)
        compilation_digests.add(compiled.compilation_digest)
        capability_keys.add(compiled.capability_key)

    assert len(source_digests) == 5
    assert len(compilation_digests) == 5
    assert len(capability_keys) == 5
