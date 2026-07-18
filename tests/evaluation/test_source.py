import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.evaluation import (
    EvaluationRun,
    EvaluationSource,
    EvaluationSourceError,
    EvaluationStatus,
    SourceBlock,
    load_evaluation_source,
)


def test_load_evaluation_source_uses_stable_heading_paths_and_redacted_block_digests(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "private-input.md"
    source_path.write_text(
        "# Team\n\nOwn the delivery.\n\n## CRM\n\nTrack prospects.\n",
        encoding="utf-8",
    )

    source = load_evaluation_source(
        source_path,
        source_reference="inputs/project.md",
        max_block_bytes=1024,
    )

    assert source.source_reference == "inputs/project.md"
    assert source.sha256 == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert [(block.block_id, block.heading_path) for block in source.blocks] == [
        ("block-0001", ("Team",)),
        ("block-0002", ("Team", "CRM")),
    ]
    assert [(block.line_start, block.line_end) for block in source.blocks] == [(1, 4), (5, 7)]
    assert all(block.sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest() for block in source.blocks)


def test_load_evaluation_source_redacts_credentials_without_changing_source_provenance(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "input.md"
    raw = b"# Delivery\nOPENAI_API_KEY=sk-test-secret\npassword=hunter2\n"
    source_path.write_bytes(raw)

    source = load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=1024)

    assert source.sha256 == hashlib.sha256(raw).hexdigest()
    assert "[REDACTED]" in source.blocks[0].text
    assert "sk-test-secret" not in source.blocks[0].text
    assert "hunter2" not in source.blocks[0].text


def test_load_evaluation_source_redacts_indented_and_fenced_secret_assignments(tmp_path: Path) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_text(
        "# Delivery\n\n    OPENAI_API_KEY=indented-secret\n\n```sh\nSERVICE_TOKEN=fenced-secret\npassword=code-password\n```\n",
        encoding="utf-8",
    )

    source = load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=1024)

    stored_text = "\n".join(block.text for block in source.blocks)
    assert stored_text.count("[REDACTED]") == 3
    assert "indented-secret" not in stored_text
    assert "fenced-secret" not in stored_text
    assert "code-password" not in stored_text


def test_source_block_rejects_unredacted_high_confidence_credential_assignment() -> None:
    raw_text = "    SERVICE_TOKEN=raw-secret"

    with pytest.raises(ValidationError, match="redacted"):
        SourceBlock(
            block_id="block-0001",
            heading_path=("Delivery",),
            line_start=1,
            line_end=1,
            sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            text=raw_text,
        )


def test_load_evaluation_source_splits_only_at_stable_line_boundaries(tmp_path: Path) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_text("# Team\nalpha\nbeta\ngamma\n", encoding="utf-8")

    source = load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=12)

    assert [block.text for block in source.blocks] == ["# Team\nalpha", "beta\ngamma"]
    assert [(block.line_start, block.line_end) for block in source.blocks] == [(1, 2), (3, 4)]


def test_load_evaluation_source_rejects_a_line_larger_than_the_block_limit(tmp_path: Path) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_text("# Team\nthis line is too long\n", encoding="utf-8")

    with pytest.raises(EvaluationSourceError, match="single source line"):
        load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=10)


@pytest.mark.parametrize("raw", [b"", b" \r\n\t", b"\xff\xfe"])
def test_load_evaluation_source_preserves_parser_invalid_input_behavior(tmp_path: Path, raw: bytes) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_bytes(raw)

    with pytest.raises(ValueError, match="project input"):
        load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=64)


def test_models_are_frozen_and_source_reference_is_safe(tmp_path: Path) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_text("# Team\nBuild.\n", encoding="utf-8")

    source = load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=64)
    run = EvaluationRun(
        run_id="run-1",
        idempotency_key="idem-1",
        source=source,
        status=EvaluationStatus.CREATED,
        max_rounds=2,
        max_calls=4,
    )

    with pytest.raises(ValidationError):
        SourceBlock(
            block_id="invalid",
            heading_path=("Team",),
            line_start=1,
            line_end=1,
            sha256="a" * 64,
            text="content",
        )
    with pytest.raises(ValidationError):
        load_evaluation_source(source_path, source_reference="C:/Users/Alice/input.md", max_block_bytes=64)
    with pytest.raises(ValidationError, match="source_reference"):
        EvaluationSource(
            source_reference="../private/input.md",
            sha256="a" * 64,
            byte_length=1,
            blocks=source.blocks,
        )
    with pytest.raises(ValidationError):
        source.blocks[0].text = "changed"
    with pytest.raises(ValidationError):
        run.status = EvaluationStatus.FAILED


def test_load_evaluation_source_rejects_sections_without_headings(tmp_path: Path) -> None:
    source_path = tmp_path / "input.md"
    source_path.write_text("Plain project text.\n", encoding="utf-8")

    with pytest.raises(EvaluationSourceError, match="heading"):
        load_evaluation_source(source_path, source_reference="input.md", max_block_bytes=64)
