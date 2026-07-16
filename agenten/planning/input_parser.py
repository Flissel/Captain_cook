"""Pure, side-effect-free project input parsing for the Captain.

This module deliberately contains no model, Docker, Minibook, gateway, or
execution imports.  It turns one UTF-8 Markdown source into an immutable typed
document that downstream planning stages may consume.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


INPUT_SCHEMA_VERSION = "captain-project-input/v1"
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


class MarkdownSection(BaseModel):
    """One Markdown heading and the body directly owned by it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: int = Field(ge=1, le=6)
    title: str = Field(min_length=1)
    parent_titles: Tuple[str, ...] = ()
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    body: str = ""


class ParsedProjectInput(BaseModel):
    """Immutable source evidence plus a deterministic Markdown outline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = INPUT_SCHEMA_VERSION
    source_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    content: str = Field(min_length=1)
    sections: Tuple[MarkdownSection, ...] = ()

    @field_validator("source_reference")
    @classmethod
    def source_reference_is_logical_and_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        segments = normalized.split("/")
        if (
            normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("source_reference must be a safe logical relative path")
        return normalized

    @model_validator(mode="after")
    def source_evidence_matches_content(self) -> "ParsedProjectInput":
        raw = self.content.encode("utf-8")
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if self.sha256 != actual_sha256:
            raise ValueError("sha256 does not match project input content")
        if self.byte_length != len(raw):
            raise ValueError("byte_length does not match project input content")
        return self

    def planning_context(self) -> str:
        """Render provenance, parsed outline, and the unmodified source for Captain."""

        outline = [
            f"- {'  ' * (section.level - 1)}{section.title} (line {section.line_start})"
            for section in self.sections
        ]
        outline_text = "\n".join(outline) if outline else "- no Markdown headings detected"
        return (
            "# Captain parsed project input\n\n"
            f"Schema: `{self.schema_version}`\n\n"
            f"Source: `{self.source_reference}`\n\n"
            f"Input SHA-256: `{self.sha256}`\n\n"
            "## Parsed outline\n\n"
            f"{outline_text}\n\n"
            "## Verbatim source\n\n"
            f"{self.content}"
        )


class MarkdownProjectInputParser:
    """Parse a UTF-8 Markdown file without changing it or calling services."""

    def parse(
        self,
        path: Path | str,
        *,
        source_reference: str | None = None,
    ) -> ParsedProjectInput:
        source = Path(path)
        raw = source.read_bytes()
        if not raw:
            raise ValueError("project input must not be empty")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("project input must be valid UTF-8") from exc
        if not content.strip():
            raise ValueError("project input must contain non-whitespace text")

        reference = source_reference or source.name
        if not reference.strip():
            raise ValueError("source_reference must not be empty")

        return ParsedProjectInput(
            source_reference=reference.replace("\\", "/"),
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            content=content,
            sections=tuple(self._parse_sections(content)),
        )

    @staticmethod
    def _parse_sections(content: str) -> List[MarkdownSection]:
        lines = content.splitlines()
        headings: List[tuple[int, int, str, List[str]]] = []
        parents: List[str] = []
        fence_marker: str | None = None

        for line_number, line in enumerate(lines, start=1):
            fence = _FENCE.match(line)
            if fence:
                marker = fence.group(1)
                if fence_marker is None:
                    fence_marker = marker
                elif marker == fence_marker:
                    fence_marker = None
                continue
            if fence_marker is not None:
                continue

            match = _HEADING.match(line)
            if match is None:
                continue
            level = len(match.group(1))
            title = match.group(2).strip()
            if not title:
                continue
            parents = parents[: level - 1]
            parent_titles = list(parents)
            while len(parents) < level - 1:
                parents.append("")
            if len(parents) == level - 1:
                parents.append(title)
            else:
                parents[level - 1] = title
            headings.append((line_number, level, title, parent_titles))

        sections: List[MarkdownSection] = []
        for index, (line_start, level, title, parent_titles) in enumerate(headings):
            next_line = headings[index + 1][0] if index + 1 < len(headings) else len(lines) + 1
            body = "\n".join(lines[line_start: next_line - 1]).strip()
            sections.append(
                MarkdownSection(
                    level=level,
                    title=title,
                    parent_titles=[parent for parent in parent_titles if parent],
                    line_start=line_start,
                    line_end=max(line_start, next_line - 1),
                    body=body,
                )
            )
        return sections
