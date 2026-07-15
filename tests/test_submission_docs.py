from pathlib import Path


def test_judge_documents_link_demo_and_evidence():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "python main.py demo" in readme
    assert "artifacts/demo-run.json" in readme
    assert Path("docs/DEVPOST_CHECKLIST.md").exists()
    assert Path("docs/VIDEO_SCRIPT.md").exists()
    assert Path("docs/THIRD_PARTY_NOTICES.md").exists()
    assert Path("docs/MCP_SETUP.md").exists()
    assert Path("docs/codex-sessions.md").exists()
