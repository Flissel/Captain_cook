from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from minibook.swarm.creation_export import CreationExportError
from minibook.swarm.pipeline import SwarmPipeline
from minibook.swarm.pipeline_adapter import CreationExportBundle


def _write_required_files(output_path: Path) -> None:
    (output_path / "evidence").mkdir(parents=True)
    (output_path / "factory-candidate.json").write_text(
        json.dumps(
            {
                "schema": "captain.factory-candidate.v1",
                "candidate_id": "sales-qualification-team",
            }
        ),
        encoding="utf-8",
    )
    (output_path / "evidence/hermes-factory-skill-usage-receipt.json").write_text(
        json.dumps(
            {
                "schema": "hermes.factory-skill-usage-receipt.v1",
                "skills": ["autogen-team-builder"],
            }
        ),
        encoding="utf-8",
    )


class _SessionMustNotBeUsed:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"creation export accessed session.{name}")


@pytest.mark.asyncio
async def test_creation_export_is_deterministic_safe_and_skips_legacy_export(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "candidate"
    _write_required_files(output_path)
    (output_path / "autogen").mkdir()
    (output_path / "autogen/team.py").write_text("TEAM = 'ready'\n", encoding="utf-8")
    (output_path / "README.md").write_text("# Candidate\n", encoding="utf-8")

    # These local build artifacts and secrets must never enter the package.
    (output_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (output_path / ".env.private").mkdir()
    (output_path / ".env.private/token.txt").write_text("secret", encoding="utf-8")
    (output_path / ".git").mkdir()
    (output_path / ".git/config").write_text("private", encoding="utf-8")
    (output_path / "run.log").write_text("secret diagnostic", encoding="utf-8")
    (output_path / "evidence/session-transcript.json").write_text(
        "{\"conversation\": \"private\"}", encoding="utf-8"
    )
    (output_path / "evidence/session-transcript").mkdir()
    (output_path / "evidence/session-transcript/raw.json").write_text(
        "{\"conversation\": \"private\"}", encoding="utf-8"
    )

    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.output_path = output_path

    async def forbidden_legacy_export(session: object) -> None:
        raise AssertionError("legacy step_export must not be called")

    pipeline.step_export = forbidden_legacy_export  # type: ignore[method-assign]

    first = await pipeline.step_creation_export(_SessionMustNotBeUsed())
    second = await pipeline.step_creation_export(_SessionMustNotBeUsed())

    assert isinstance(first, CreationExportBundle)
    assert first == second
    assert "source_archive_ref" not in first.candidate_manifest
    assert json.loads(first.skill_usage_receipt) == {
        "schema": "hermes.factory-skill-usage-receipt.v1",
        "skills": ["autogen-team-builder"],
    }

    with zipfile.ZipFile(BytesIO(first.source_archive)) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert names == [
            "README.md",
            "autogen/team.py",
            "evidence/hermes-factory-skill-usage-receipt.json",
            "factory-candidate.json",
        ]
        archived_manifest = json.loads(archive.read("factory-candidate.json"))
        assert "source_archive_ref" not in archived_manifest


@pytest.mark.asyncio
async def test_creation_export_fails_closed_without_output_path() -> None:
    pipeline = SwarmPipeline({}, "project", "task", interactive=False)

    with pytest.raises(CreationExportError, match="output path"):
        await pipeline.step_creation_export(_SessionMustNotBeUsed())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_content", "receipt_content", "expected"),
    [
        (None, "{}", "factory-candidate.json"),
        ("not-json", "{}", "factory-candidate.json"),
        ("[]", "{}", "factory-candidate.json"),
        ("{}", None, "hermes-factory-skill-usage-receipt.json"),
        ("{}", "not-json", "hermes-factory-skill-usage-receipt.json"),
        ("{}", "[]", "hermes-factory-skill-usage-receipt.json"),
        ('{"source_archive_ref": "publisher-owned"}', "{}", "source_archive_ref"),
    ],
)
async def test_creation_export_rejects_missing_or_invalid_required_json(
    tmp_path: Path,
    candidate_content: str | None,
    receipt_content: str | None,
    expected: str,
) -> None:
    output_path = tmp_path / "candidate"
    (output_path / "evidence").mkdir(parents=True)
    if candidate_content is not None:
        (output_path / "factory-candidate.json").write_text(
            candidate_content, encoding="utf-8"
        )
    if receipt_content is not None:
        (output_path / "evidence/hermes-factory-skill-usage-receipt.json").write_text(
            receipt_content, encoding="utf-8"
        )

    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.output_path = output_path

    with pytest.raises(CreationExportError, match=expected):
        await pipeline.step_creation_export(_SessionMustNotBeUsed())


@pytest.mark.asyncio
async def test_creation_export_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "candidate"
    _write_required_files(output_path)
    linked_file = output_path / "autogen/team.py"
    linked_file.parent.mkdir()
    linked_file.write_text("TEAM = 'unsafe'\n", encoding="utf-8")

    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == linked_file or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.output_path = output_path

    with pytest.raises(CreationExportError, match="symlink"):
        await pipeline.step_creation_export(_SessionMustNotBeUsed())
