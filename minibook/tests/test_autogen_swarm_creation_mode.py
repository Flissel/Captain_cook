from __future__ import annotations

from pathlib import Path

import autogen_swarm


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
