from __future__ import annotations

from pathlib import Path

import pytest

from agenten.agent_factory.input_document import FactoryInputError, load_factory_input, parse_factory_input


def test_canonical_input_is_content_addressed_and_extracts_all_gates() -> None:
    input_path = Path(__file__).parents[2] / "input.md"

    document = load_factory_input(input_path)

    assert document.input_ref.uri.startswith("artifact://factory-input/")
    assert document.input_ref.sha256 in document.input_ref.uri
    assert len(document.required_outputs) == 6
    assert len(document.quality_gates) == 6


def test_input_rejects_missing_required_sections() -> None:
    with pytest.raises(FactoryInputError, match="missing required sections"):
        parse_factory_input("## Objective\n\nBuild it.\n")
