from __future__ import annotations

from pathlib import Path

import autogen_swarm
import pytest
from swarm.runtime_options import RuntimeOptions


def test_creation_mode_never_invokes_legacy_github_export(
    tmp_path: Path,
    monkeypatch,
) -> None:
    merged = tmp_path / "merged"
    merged.mkdir()
    calls: list[str] = []

    def forbidden_export(*args, **kwargs):
        calls.append("export")
        raise AssertionError("legacy GitHub export must not run")

    monkeypatch.setattr(autogen_swarm, "export_agent_team", forbidden_export)

    checkpoint: dict[str, object] = {}
    result = autogen_swarm._complete_input_file_output(
        merged,
        checkpoint,
        tmp_path,
        export_to_github=False,
    )

    assert result == merged
    assert checkpoint["merged_output"] == str(merged)
    assert "exported" not in checkpoint
    assert calls == []


def test_v2_creation_mode_imports_captain_archive_without_starting_swarm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    job = object()
    expected = object()
    calls: list[tuple[object, Path, Path, Path]] = []
    written: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        autogen_swarm,
        "publish_captain_sealed_creation_output",
        lambda actual_job, *, source_archive_path, skill_usage_receipt_path, artifact_root: (
            calls.append(
                (
                    actual_job,
                    source_archive_path,
                    skill_usage_receipt_path,
                    artifact_root,
                )
            )
            or expected
        ),
    )
    monkeypatch.setattr(
        autogen_swarm,
        "write_creation_result_atomic",
        lambda path, result: written.append((path, result)),
    )
    options = RuntimeOptions(
        interactive=False,
        creation_job_file="job.json",
        result_file="result.json",
        skill_usage_receipt_file="skill.json",
        artifact_root="cas",
        source_archive_file="source.zip",
    )

    autogen_swarm._publish_captain_sealed_import(job, options)

    assert calls == [(job, Path("source.zip"), Path("skill.json"), Path("cas"))]
    assert written == [(Path("result.json"), expected)]


def test_v2_creation_mode_requires_explicit_captain_source_path() -> None:
    options = RuntimeOptions(
        creation_job_file="job.json",
        result_file="result.json",
        skill_usage_receipt_file="skill.json",
        artifact_root="cas",
    )

    with pytest.raises(ValueError, match="source-archive-file"):
        autogen_swarm._publish_captain_sealed_import(object(), options)
