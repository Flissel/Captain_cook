from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agenten.agent_factory.input_document import FactoryInputError, load_factory_input


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
