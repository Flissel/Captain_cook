from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.state_machine import FactoryLifecycleStatus


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "promote-business-benchmark-release.py"


def _module():
    spec = spec_from_file_location("promote_business_benchmark_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_promotes_with_env_only_token_and_redacted_json(
    monkeypatch,
    capsys,
) -> None:
    module = _module()
    job_id = UUID("82000000-0000-0000-0000-000000000001")
    event_id = UUID("82000000-0000-0000-0000-000000000002")
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def promote(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            job_id=job_id,
            promotion_event_id=event_id,
            replayed=False,
            status=FactoryLifecycleStatus.READY_TO_USE,
            phase=FactoryPhase.CAPABILITY_PROMOTED,
        )

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "promote_release_workflow", promote)
    monkeypatch.setenv("CAPTAIN_BENCHMARK_GATEWAY_URL", "http://127.0.0.1:8092")
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "captain-secret")
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--job-id",
            str(job_id),
            "--occurred-at",
            "2026-08-04T20:00:00Z",
        ],
    )

    assert module.main() == 0

    assert captured["base_url"] == "http://127.0.0.1:8092"
    assert captured["headers"] == {"Authorization": "Bearer captain-secret"}
    output_text = capsys.readouterr().out
    assert "captain-secret" not in output_text
    output = json.loads(output_text)
    assert output == {
        "job_id": str(job_id),
        "phase": "capability_promoted",
        "promotion_event_id": str(event_id),
        "replayed": False,
        "schema": "captain.workflow-promotion-result.v1",
        "status": "ready_to_use",
    }
