from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

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
