from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

import pytest

from agenten.evaluation.cli import (
    async_main,
)
from agenten.evaluation.models import EvaluationManifest, EvaluationStatus


EXPECTED_AGENTFARM_SOURCE_SHA256 = (
    "e55e667474a3b6a3d1a1dc6f927fec9ea67a247ea30ea61141c5b994495623ac"
)


def require_live_evaluation_environment() -> Path:
    configured = os.environ.get("AGENTFARM_INPUT_PATH")
    if not configured:
        pytest.skip("AGENTFARM_INPUT_PATH is required for the live evaluation gate")
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is required for the live evaluation gate")
    path = Path(configured)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pytest.skip("AGENTFARM_INPUT_PATH must identify a readable immutable source")
    if digest != EXPECTED_AGENTFARM_SOURCE_SHA256:
        pytest.skip("AGENTFARM_INPUT_PATH digest does not match the approved immutable source")
    return path


@pytest.mark.live
def test_live_agentfarm_evaluation_requires_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTFARM_INPUT_PATH", raising=False)
    with pytest.raises(pytest.skip.Exception, match="AGENTFARM_INPUT_PATH"):
        require_live_evaluation_environment()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_agentfarm_evaluation_is_real_bounded_and_planning_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = require_live_evaluation_environment()
    output = tmp_path / "evaluation-artifacts"
    run_id = "agentfarm-live-smoke"

    exit_code = await async_main(
        [
            str(input_path),
            "--source-reference",
            "agentfarm/input.md",
            "--output",
            str(output),
            "--run-id",
            run_id,
            "--max-components",
            "1",
            "--max-rounds",
            "1",
            "--max-calls",
            "4",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    manifest_path = output / run_id / "run-manifest.json"
    manifest = EvaluationManifest.model_validate_json(manifest_path.read_bytes())
    report = (output / run_id / "evaluation.md").read_text(encoding="utf-8")

    assert manifest.source.sha256 == EXPECTED_AGENTFARM_SOURCE_SHA256
    assert manifest.status in {EvaluationStatus.ACCEPTED, EvaluationStatus.PARTIAL}
    assert manifest.model_identifier not in {"", "not-configured"}
    assert 0 < manifest.call_count <= 4
    assert manifest.token_total > 0
    assert manifest.planning_disclaimer in report
    assert summary["call_count"] == manifest.call_count
    assert summary["token_total"] == manifest.token_total
    assert summary["artifact_reference"] == f"{run_id}/evaluation.md"
    assert str(input_path) not in json.dumps(summary)
    assert str(output) not in json.dumps(summary)
    assert os.environ["OPENAI_API_KEY"] not in json.dumps(summary)
