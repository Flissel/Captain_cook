from pathlib import Path

from scripts.verify_submission import validate_submission


def test_validate_submission_accepts_committed_evidence():
    assert validate_submission(Path(".")) == []


def test_validate_submission_requires_mcp_and_session_evidence(tmp_path):
    (tmp_path / "README.md").write_text("# Captain Cook\n", encoding="utf-8")

    errors = validate_submission(tmp_path)

    assert "Missing required file: docs/MCP_SETUP.md" in errors
    assert "Missing required file: docs/codex-sessions.md" in errors
