from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agenten.agent_factory.input_document import FactoryInputError, parse_factory_input_bytes
from agenten.agent_factory.input_migration import render_migration_candidate


FIXTURES = Path(__file__).parents[1] / "fixtures" / "agent_factory"
SCRIPT = Path(__file__).parents[2] / "scripts" / "migrate_to_be_built.py"


def test_legacy_input_renders_review_only_candidate_with_all_headings() -> None:
    report = render_migration_candidate((FIXTURES / "legacy-input.md").read_bytes())

    assert report.findings
    assert "<!-- CAPTAIN_REVIEW_REQUIRED -->" in report.candidate
    for heading in (
        "Objective", "Authority boundaries", "Agents", "Integrations",
        "Shared workflows", "Security requirements", "Acceptance outcomes",
        "Real cases", "Helpful resources", "Stop conditions",
    ):
        assert f"## {heading}" in report.candidate
    assert "Source: Project Overview" in report.candidate
    with pytest.raises(FactoryInputError, match="review marker"):
        parse_factory_input_bytes(report.candidate.encode(), logical_name="TO_BE_BUILT.md")


def test_cli_requires_explicit_overwrite_and_never_targets_canonical_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy-input.md"
    source.write_bytes((FIXTURES / "legacy-input.md").read_bytes())
    output = tmp_path / "TO_BE_BUILT.candidate.md"

    first = subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output)], capture_output=True, text=True)
    assert first.returncode == 2
    assert output.exists()
    assert str(output) in first.stdout
    assert "Sales Playbook" not in first.stdout

    second = subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output)], capture_output=True, text=True)
    assert second.returncode == 1
    assert "already exists" in second.stderr

    overwritten = subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output), "--overwrite-candidate"], capture_output=True, text=True)
    assert overwritten.returncode == 2

    canonical = tmp_path / "TO_BE_BUILT.md"
    canonical.write_bytes(source.read_bytes())
    forbidden = subprocess.run([sys.executable, str(SCRIPT), "--source", str(canonical), "--output", str(canonical), "--overwrite-candidate"], capture_output=True, text=True)
    assert forbidden.returncode == 1
    assert "canonical source" in forbidden.stderr
