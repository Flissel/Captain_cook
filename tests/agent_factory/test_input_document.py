from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
import agenten.agent_factory.input_document as input_document_module

from agenten.agent_factory.input_contracts import CredentialAlias, RequestedAgent
from agenten.agent_factory.input_document import (
    FactoryInputError,
    load_factory_input,
    parse_factory_input_bytes,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "agent_factory"


def test_canonical_input_is_byte_addressed_and_exposes_complete_structure(tmp_path: Path) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_bytes()
    canonical_path = tmp_path / "TO_BE_BUILT.md"
    canonical_path.write_bytes(source)

    document = load_factory_input(canonical_path)

    digest = hashlib.sha256(source).hexdigest()
    assert document.input_ref.sha256 == digest
    assert document.byte_length == len(source)
    assert document.title == "Customer Support Triage"
    assert len(document.sections) == 10
    assert len(document.agents) == 2
    assert len(document.integrations) == 1


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("TO_BE_BUILT.missing-section.md", "missing required section"),
        ("TO_BE_BUILT.credential-bearing.md", "credential value"),
    ],
)
def test_invalid_input_is_rejected_before_compilation(
    tmp_path: Path, fixture: str, reason: str
) -> None:
    canonical_path = tmp_path / "TO_BE_BUILT.md"
    canonical_path.write_bytes((FIXTURES / fixture).read_bytes())
    with pytest.raises(FactoryInputError, match=reason):
        load_factory_input(canonical_path)


def test_noncanonical_filename_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_bytes((FIXTURES / "TO_BE_BUILT.valid.md").read_bytes())
    with pytest.raises(FactoryInputError, match="TO_BE_BUILT.md"):
        load_factory_input(path)


@pytest.mark.parametrize("source", [b"\xef\xbb\xbf# Title", b"# Title\x00", b"\xff"])
def test_ambiguous_or_invalid_bytes_are_rejected(source: bytes) -> None:
    with pytest.raises(FactoryInputError):
        parse_factory_input_bytes(source, logical_name="TO_BE_BUILT.md")


def test_duplicate_required_heading_is_rejected() -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_bytes()
    source += b"\n## Objective\nOverride.\n"
    with pytest.raises(FactoryInputError, match="duplicate required section"):
        parse_factory_input_bytes(source, logical_name="TO_BE_BUILT.md")


def test_contracts_are_frozen_strict_and_aliases_are_uppercase(tmp_path: Path) -> None:
    path = tmp_path / "TO_BE_BUILT.md"
    path.write_bytes((FIXTURES / "TO_BE_BUILT.valid.md").read_bytes())
    document = load_factory_input(path)

    with pytest.raises(ValidationError):
        CredentialAlias(alias="crm_key")
    with pytest.raises(ValidationError):
        RequestedAgent.model_validate(document.agents[0].model_dump() | {"unknown": True})
    with pytest.raises(ValidationError):
        document.agents[0].purpose = "changed"  # type: ignore[misc]
    assert document.model_dump_json() == load_factory_input(path).model_dump_json()


def test_duplicate_agent_names_and_unknown_handoffs_fail(tmp_path: Path) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    duplicate = source.replace("### Agent: response_agent", "### Agent: triage_agent")
    with pytest.raises(FactoryInputError, match="duplicate agent"):
        parse_factory_input_bytes(duplicate.encode(), logical_name="TO_BE_BUILT.md")

    unknown = source.replace("- response_agent", "- absent_agent", 1)
    with pytest.raises(FactoryInputError, match="unknown handoff"):
        parse_factory_input_bytes(unknown.encode(), logical_name="TO_BE_BUILT.md")


def test_cyclic_handoffs_and_missing_success_case_fail() -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    cyclic = source.replace("#### Handoffs\n- none", "#### Handoffs\n- triage_agent")
    with pytest.raises(FactoryInputError, match="cyclic handoff"):
        parse_factory_input_bytes(cyclic.encode(), logical_name="TO_BE_BUILT.md")

    missing_case = source.replace(
        "- public_billing | Given an overdue invoice | Classify and draft | A billing classification and response draft are produced",
        "",
    )
    with pytest.raises(FactoryInputError, match="success case"):
        parse_factory_input_bytes(missing_case.encode(), logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize("level", (2, 3, 4, 5, 6))
@pytest.mark.parametrize(
    "heading",
    (
        "Private benchmark cases",
        "Hidden evaluation fixtures",
        "Expected outcome answers",
        "Holdout predicates",
        "Benchmark outcomes",
        "Holdout outcome",
        "Evaluation fixture",
    ),
)
def test_all_nested_reserved_headings_are_rejected_before_section_parsing(
    level: int,
    heading: str,
) -> None:
    sentinel = "CAPTAIN-NESTED-HELPFUL-PRIVATE-SENTINEL"
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    source = source.replace(
        "## Helpful resources",
        f"## Helpful resources\n{'#' * level} {heading}\n{sentinel}",
    )

    with pytest.raises(FactoryInputError, match="private benchmark cases"):
        parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize("indent", (" ", "  ", "   "))
def test_indented_commonmark_private_headings_are_rejected(indent: str) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    source = source.replace(
        "## Helpful resources",
        f"## Helpful resources\n- Public resource\n{indent}### Hidden evaluation fixtures",
    )

    with pytest.raises(FactoryInputError, match="private benchmark cases"):
        parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize(
    "heading",
    (
        "Confidential benchmark policy",
        "Secret holdout policy",
        "Internal evaluation policy",
        "Nonpublic benchmark policy",
    ),
)
def test_semantically_hidden_policy_aliases_are_rejected(heading: str) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    source += f"\n## {heading}\nCAPTAIN-HIDDEN-POLICY-SENTINEL\n"

    with pytest.raises(FactoryInputError, match="private benchmark cases"):
        parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")


def test_private_heading_alias_matrix_is_rejected_before_parser_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    parser_calls = 0
    original = input_document_module._level_sections

    def counted_parser(text: str):
        nonlocal parser_calls
        parser_calls += 1
        return original(text)

    monkeypatch.setattr(input_document_module, "_level_sections", counted_parser)
    for level in range(2, 7):
        for authority in ("Private", "Confidential", "Hidden"):
            for semantic in (
                "expected", "result", "outcome", "answer", "predicate",
                "benchmark", "holdout", "evaluation", "case", "fixture",
            ):
                malformed = source.replace(
                    "## Helpful resources",
                    f"## Helpful resources\n{'#' * level} {authority} {semantic}",
                )
                with pytest.raises(FactoryInputError, match="private benchmark cases"):
                    parse_factory_input_bytes(
                        malformed.encode(), logical_name="TO_BE_BUILT.md",
                    )

    assert parser_calls == 0


@pytest.mark.parametrize(
    "heading", ("Private benchmark policy", "Private holdout policy"),
)
def test_exact_public_policy_heading_is_allowed_only_at_level_two(heading: str) -> None:
    source = (FIXTURES / "TO_BE_BUILT.valid.md").read_text(encoding="utf-8")
    source += f"\n## {heading}\nPublic categories and dimensions only.\n"
    document = parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md")
    assert document.benchmark_policy.public_policy_statement is not None

    for level in (3, 4, 5, 6):
        nested = source.replace(f"## {heading}", f"{'#' * level} {heading}")
        with pytest.raises(FactoryInputError, match="private benchmark cases"):
            parse_factory_input_bytes(nested.encode(), logical_name="TO_BE_BUILT.md")


@pytest.mark.parametrize(
    ("relative_path", "required_tools", "optional_tools"),
    (
        (
            "sales_pipeline_brief/TO_BE_BUILT.md",
            {"account_snapshot_reader", "opportunity_scoring_rules"},
            {"industry_signal_reader"},
        ),
        (
            "incident_command/TO_BE_BUILT.md",
            {"alert_context_reader", "service_dependency_map", "audit_log_query"},
            {"status_page_draft"},
        ),
        (
            "claims_resolution/TO_BE_BUILT.md",
            {"policy_lookup", "claim_evidence_reader"},
            {"ocr_extractor"},
        ),
    ),
)
def test_tool_policy_is_typed_with_stable_required_and_optional_tools(
    relative_path: str,
    required_tools: set[str],
    optional_tools: set[str],
) -> None:
    root = Path(__file__).resolve().parents[2]
    document = load_factory_input(root / "demo_inputs" / "agent_factory" / relative_path)
    observed_required = {
        item.tool_key for item in document.declared_tools if item.requirement == "required"
    }
    observed_optional = {
        item.tool_key for item in document.declared_tools if item.requirement == "optional"
    }
    assert observed_required == required_tools
    assert observed_optional == optional_tools
    assert all(item.location in {"local", "integration"} for item in document.declared_tools)
    assert {
        f"tool_{item.tool_key}" for item in document.declared_tools
    }.issubset(document.benchmark_policy.source_case_keys)
