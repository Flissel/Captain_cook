"""Deterministic, non-authoritative legacy-input migration preflight."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.input_document import REQUIRED_SECTIONS


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MigrationFinding(_FrozenContract):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    section: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["review_required"] = "review_required"


class InputMigrationReport(_FrozenContract):
    schema_name: Literal["captain.input-migration-report.v1"] = "captain.input-migration-report.v1"
    candidate: str = Field(min_length=1)
    findings: tuple[MigrationFinding, ...] = Field(min_length=1)


_MAPPING = {
    "Project Overview": "Objective",
    "Agents": "Agents",
    "Shared Workflows": "Shared workflows",
    "Security": "Security requirements",
    "Success Metrics": "Acceptance outcomes",
    "Resources and Links": "Helpful resources",
}


def render_migration_candidate(source: bytes | str) -> InputMigrationReport:
    if isinstance(source, bytes):
        text = source.decode("utf-8", errors="strict")
    else:
        text = source
    legacy = _sections(text)
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Migration Candidate"
    mapped: dict[str, str] = {}
    for old, new in _MAPPING.items():
        if old in legacy:
            mapped[new] = f"<!-- Source: {old} -->\n{legacy[old]}"
    findings = tuple(
        MigrationFinding(code="decision_required", section=name, message=f"Human review must complete {name} without invented authority or behavior")
        for name in REQUIRED_SECTIONS
        if name not in mapped
    )
    if not findings:
        findings = (MigrationFinding(code="canonical_review_required", section="document", message="Human must approve migrated semantics"),)
    parts = [f"# {title}", "", "<!-- CAPTAIN_REVIEW_REQUIRED -->"]
    for section in REQUIRED_SECTIONS:
        parts.extend(["", f"## {section}", mapped.get(section, f"<!-- REVIEW: decide {section} from cited source; do not infer. -->")])
    unmapped = [name for name in legacy if name not in _MAPPING]
    if unmapped:
        parts.extend(["", "### Preserved unmapped source"])
        for name in unmapped:
            parts.extend([f"#### Source: {name}", legacy[name]])
    return InputMigrationReport(candidate="\n".join(parts).rstrip() + "\n", findings=findings)


def _sections(text: str) -> dict[str, str]:
    matches = tuple(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match.group(1)] = text[match.end():end].strip()
    return result
