import hashlib
from pathlib import Path

import pytest

from agenten.planning.input_parser import MarkdownProjectInputParser, ParsedProjectInput


def test_parser_preserves_source_and_builds_a_typed_heading_tree(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    raw = (
        b"# Agent Factory\r\n\r\n"
        b"Project goal.\r\n\r\n"
        b"## Planning\r\n\r\n"
        b"Plan autonomously.\r\n\r\n"
        b"### Acceptance\r\n\r\n"
        b"- deterministic\r\n"
    )
    source.write_bytes(raw)

    parsed = MarkdownProjectInputParser().parse(source, source_reference="input.md")

    assert source.read_bytes() == raw
    assert parsed.source_reference == "input.md"
    assert parsed.sha256 == hashlib.sha256(raw).hexdigest()
    assert parsed.byte_length == len(raw)
    assert parsed.content.encode("utf-8") == raw
    assert [(section.level, section.title, section.parent_titles) for section in parsed.sections] == [
        (1, "Agent Factory", ()),
        (2, "Planning", ("Agent Factory",)),
        (3, "Acceptance", ("Agent Factory", "Planning")),
    ]
    assert parsed.sections[1].body == "Plan autonomously."


def test_parsed_input_is_deeply_immutable(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")

    parsed = MarkdownProjectInputParser().parse(source)

    with pytest.raises(AttributeError):
        parsed.sections.append(parsed.sections[0])
    with pytest.raises(AttributeError):
        parsed.sections[0].parent_titles.append("Injected")


@pytest.mark.parametrize("raw", [b"", b" \r\n\t", b"\xff\xfe"])
def test_parser_rejects_invalid_project_input(tmp_path: Path, raw: bytes) -> None:
    source = tmp_path / "input.md"
    source.write_bytes(raw)

    with pytest.raises(ValueError, match="project input"):
        MarkdownProjectInputParser().parse(source)


@pytest.mark.parametrize(
    "reference",
    ["C:/Users/Alice/secret.md", "//server/share/input.md", "../input.md", "input.md\n# injected"],
)
def test_parser_rejects_unsafe_source_references(tmp_path: Path, reference: str) -> None:
    source = tmp_path / "input.md"
    source.write_text("# Goal\n\nBuild.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source_reference"):
        MarkdownProjectInputParser().parse(source, source_reference=reference)


def test_parsed_input_rejects_false_source_evidence() -> None:
    with pytest.raises(ValueError, match="sha256"):
        ParsedProjectInput(
            source_reference="input.md",
            sha256="a" * 64,
            byte_length=4,
            content="goal",
        )
