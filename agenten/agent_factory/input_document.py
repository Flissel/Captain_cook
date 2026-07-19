"""Canonical, content-addressed import of the Agent-Factory markdown input."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_runtime.contracts import ArtifactRef


_REQUIRED_SECTIONS = (
    "Objective",
    "Authority boundaries",
    "Required output",
    "Non-negotiable quality gates",
    "Stop conditions",
)
_SECTION_PATTERN = re.compile(r"^## (?P<name>.+?)\s*$", re.MULTILINE)


class FactoryInputError(ValueError):
    """The canonical input is incomplete or cannot be represented safely."""


class FactoryInputDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_ref: ArtifactRef
    objective: str = Field(min_length=1)
    authority_boundaries: tuple[str, ...] = Field(min_length=1)
    required_outputs: tuple[str, ...] = Field(min_length=1)
    quality_gates: tuple[str, ...] = Field(min_length=1)
    stop_conditions: tuple[str, ...] = Field(min_length=1)


def load_factory_input(path: Path) -> FactoryInputDocument:
    """Load the exact markdown bytes; never substitute a default input."""

    if path.name != "input.md":
        raise FactoryInputError("canonical factory input must be named input.md")
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FactoryInputError("canonical input.md is missing") from exc
    return parse_factory_input(source)


def parse_factory_input(source: str) -> FactoryInputDocument:
    """Parse the fixed specification headings into a sealed, typed document."""

    sections = _sections(source)
    missing = [name for name in _REQUIRED_SECTIONS if name not in sections]
    if missing:
        raise FactoryInputError(f"input.md is missing required sections: {', '.join(missing)}")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return FactoryInputDocument(
        input_ref=ArtifactRef(
            uri=f"artifact://factory-input/{digest}",
            sha256=digest,
            media_type="text/markdown",
        ),
        objective=_paragraph(sections["Objective"]),
        authority_boundaries=_items(sections["Authority boundaries"]),
        required_outputs=_items(sections["Required output"]),
        quality_gates=_items(sections["Non-negotiable quality gates"]),
        stop_conditions=_rules(sections["Stop conditions"]),
    )


def _sections(source: str) -> dict[str, str]:
    matches = tuple(_SECTION_PATTERN.finditer(source))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        sections[match.group("name")] = source[match.end():end].strip()
    return sections


def _paragraph(value: str) -> str:
    normalized = " ".join(line.strip() for line in value.splitlines() if line.strip())
    if not normalized:
        raise FactoryInputError("input.md objective must not be blank")
    return normalized


def _items(value: str) -> tuple[str, ...]:
    items = tuple(
        line.strip().lstrip("- ").split(". ", 1)[-1].strip()
        for line in value.splitlines()
        if line.strip().startswith(("- ", "* ")) or re.match(r"^\d+\. ", line.strip())
    )
    if not items or any(not item for item in items):
        raise FactoryInputError("input.md section must contain list items")
    return items


def _rules(value: str) -> tuple[str, ...]:
    try:
        return _items(value)
    except FactoryInputError:
        return (_paragraph(value),)
