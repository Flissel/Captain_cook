from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


def test_hermes_factory_build_live_requires_codex_and_conditional_n8n_evidence() -> None:
    required = ("HERMES_FACTORY_LIVE_EVIDENCE", "CODEX_PROVIDER_LIVE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip("missing live prerequisites: " + ", ".join(missing))
    evidence = json.loads(
        Path(os.environ["HERMES_FACTORY_LIVE_EVIDENCE"]).read_text(encoding="utf-8")
    )
    assert str(evidence["codex_session_id"]).strip()
    assert len(evidence["artifact_sha256"]) == 64
    if evidence.get("integration_declared"):
        assert str(evidence["n8n_mcp_call_id"]).strip()
        assert str(evidence["n8n_execution_id"]).strip()
