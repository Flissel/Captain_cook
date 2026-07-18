"""Build redacted evaluation blocks from the canonical project-input parser."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agenten.planning.input_parser import MarkdownProjectInputParser, ParsedProjectInput

from .models import EvaluationSource, SourceBlock, _SECRET_ASSIGNMENT


class EvaluationSourceError(ValueError):
    """The parsed project input cannot form a safe evaluation inventory."""


def load_evaluation_source(
    path: Path | str,
    source_reference: str,
    max_block_bytes: int,
) -> EvaluationSource:
    """Parse once, then derive bounded redacted blocks from parsed sections."""

    if max_block_bytes < 1:
        raise EvaluationSourceError("max_block_bytes must be positive")

    parsed = MarkdownProjectInputParser().parse(path, source_reference=source_reference)
    return _to_evaluation_source(parsed, max_block_bytes=max_block_bytes)


def _to_evaluation_source(parsed: ParsedProjectInput, *, max_block_bytes: int) -> EvaluationSource:
    if not parsed.sections:
        raise EvaluationSourceError("project input must contain at least one Markdown heading")

    lines = parsed.content.splitlines()
    blocks: list[SourceBlock] = []
    for section in parsed.sections:
        section_lines = lines[section.line_start - 1 : section.line_end]
        if any(len(line.encode("utf-8")) > max_block_bytes for line in section_lines):
            raise EvaluationSourceError("single source line exceeds max_block_bytes")
        redacted_lines = [_redact(line) for line in section_lines]
        for chunk, line_start, line_end in _split_at_line_boundaries(
            redacted_lines,
            section.line_start,
            max_block_bytes,
        ):
            blocks.append(
                SourceBlock(
                    block_id=f"block-{len(blocks) + 1:04d}",
                    heading_path=(*section.parent_titles, section.title),
                    line_start=line_start,
                    line_end=line_end,
                    sha256=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                    text=chunk,
                )
            )
    return EvaluationSource(
        source_reference=parsed.source_reference,
        sha256=parsed.sha256,
        byte_length=parsed.byte_length,
        blocks=tuple(blocks),
    )


def _redact(line: str) -> str:
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('indent')}{match.group('name')}=[REDACTED]",
        line,
    )


def _split_at_line_boundaries(
    lines: list[str],
    first_line: int,
    max_block_bytes: int,
) -> list[tuple[str, int, int]]:
    chunks: list[tuple[str, int, int]] = []
    current: list[str] = []
    current_start = first_line
    for offset, line in enumerate(lines):
        candidate = "\n".join([*current, line])
        if current and len(candidate.encode("utf-8")) > max_block_bytes:
            chunks.append(("\n".join(current), current_start, first_line + offset - 1))
            current = [line]
            current_start = first_line + offset
        else:
            current.append(line)
    if current:
        chunks.append(("\n".join(current), current_start, first_line + len(lines) - 1))
    return chunks
