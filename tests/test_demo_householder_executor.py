import json
from pathlib import Path

import pytest

import main
from agenten.demo import run_demo
from agenten.llm.model_client import build_replay_model_client

_LLM_PREFIX = "llm_reported:"


def _report() -> str:
    # Role-agnostic: householders run in arbitrary order and the executor
    # (not the model) decides which role a report belongs to.
    return json.dumps(
        {"decision": "accept", "artifacts": [], "evidence": ["checked"], "limitations": []}
    )


def _evidence(summary) -> list[str]:
    return [
        item
        for block in summary.blocks
        if block["block_type"] == "subproblem"
        for item in (block.get("result") or {}).get("evidence", [])
    ]


@pytest.mark.asyncio
async def test_demo_runs_householders_on_an_injected_model_client():
    # One identical reply per householder; the replay client never touches the network.
    client = build_replay_model_client([_report() for _ in range(4)])

    summary = await run_demo(householder_model_client=client)

    results = [
        block["result"] for block in summary.blocks if block["block_type"] == "subproblem"
    ]
    assert summary.success is True
    assert len(results) == 4
    # Every householder report carries the provenance marker as its first evidence item.
    assert all(result["evidence"][0].startswith(_LLM_PREFIX) for result in results)


@pytest.mark.asyncio
async def test_default_demo_stays_deterministic():
    summary = await run_demo()

    evidence = _evidence(summary)
    assert evidence
    assert not any(item.startswith(_LLM_PREFIX) for item in evidence)


def test_cli_defaults_to_the_deterministic_executor():
    assert main.parse_args(["demo"]).householder_executor == "deterministic"


def test_cli_accepts_the_llm_executor():
    assert main.parse_args(["demo", "--householder-executor", "llm"]).householder_executor == "llm"


async def _timeout_seen_by_pipeline(monkeypatch, **kwargs) -> float:
    from agenten.orchestration.pipeline import SupplyChainPipeline as Pipeline

    seen: list[float] = []
    original = Pipeline.wait_until_terminal

    async def spy(self, expected_subproblem_count, timeout=5.0, poll_interval=0.01):
        seen.append(timeout)
        return await original(self, expected_subproblem_count, timeout, poll_interval)

    monkeypatch.setattr(Pipeline, "wait_until_terminal", spy)
    await run_demo(**kwargs)
    return seen[0]


@pytest.mark.asyncio
async def test_deterministic_path_keeps_the_five_second_timeout(monkeypatch):
    assert await _timeout_seen_by_pipeline(monkeypatch) == 5.0


@pytest.mark.asyncio
async def test_llm_path_timeout_covers_the_executor_worst_case(monkeypatch):
    from agenten import demo

    client = build_replay_model_client([_report() for _ in range(4)])

    timeout = await _timeout_seen_by_pipeline(monkeypatch, householder_model_client=client)

    assert timeout == demo.LLM_CONVERGENCE_TIMEOUT_SECONDS == 270.0
    assert timeout >= 4 * demo.LLM_CALL_TIMEOUT_SECONDS * demo.LLM_CALL_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_summary_records_the_llm_executor_and_model(tmp_path):
    client = build_replay_model_client([_report() for _ in range(4)])
    output = tmp_path / "demo-run-llm.json"

    summary = await run_demo(
        output, householder_model_client=client, householder_model="nvidia/test-model"
    )

    assert summary.householder_executor == "llm"
    assert summary.model == "nvidia/test-model"
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["householder_executor"] == "llm"
    assert written["model"] == "nvidia/test-model"


@pytest.mark.asyncio
async def test_summary_records_the_deterministic_executor_without_a_model():
    summary = await run_demo()

    assert summary.householder_executor == "deterministic"
    assert summary.model is None


class _ClosingClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _stub_llm_demo(monkeypatch, *, fail: bool = False):
    from agenten.llm import model_client as model_client_module
    from config import llm_config
    from config.llm_provider import NEBIUS_BASE_URL, ProviderProfile

    profile = ProviderProfile("nebius", "nvidia/test-model", NEBIUS_BASE_URL, "nb-test", True, True)
    client = _ClosingClient()
    calls: list[dict[str, object]] = []

    async def fake_run_demo(output_path=None, **kwargs):
        calls.append({"output_path": output_path, **kwargs})
        if fail:
            raise RuntimeError("provider failed")
        return type("Summary", (), {"done_count": 4})()

    monkeypatch.setattr(llm_config, "get_profile", lambda: profile)
    monkeypatch.setattr(model_client_module, "build_model_client", lambda **_: client)
    monkeypatch.setattr(main, "run_demo", fake_run_demo)
    return client, calls


def test_llm_demo_defaults_to_its_own_evidence_file(monkeypatch):
    client, calls = _stub_llm_demo(monkeypatch)

    assert main.main(["demo", "--householder-executor", "llm"]) == 0

    # The committed deterministic evidence is never overwritten by an LLM run.
    assert calls[0]["output_path"] == main.LLM_DEMO_OUTPUT
    assert main.LLM_DEMO_OUTPUT != main.DEMO_OUTPUT
    assert calls[0]["householder_model_client"] is client
    assert calls[0]["householder_model"] == "nvidia/test-model"
    assert client.closed is True


def test_llm_demo_respects_an_explicit_output(monkeypatch, tmp_path):
    _client, calls = _stub_llm_demo(monkeypatch)

    main.main(["demo", "--householder-executor", "llm", "--output", str(tmp_path / "x.json")])

    assert calls[0]["output_path"] == tmp_path / "x.json"


def test_llm_demo_closes_the_client_when_the_run_fails(monkeypatch):
    client, _calls = _stub_llm_demo(monkeypatch, fail=True)

    with pytest.raises(RuntimeError, match="provider failed"):
        main.main(["demo", "--householder-executor", "llm"])
    assert client.closed is True


def test_deterministic_demo_keeps_the_committed_output_path(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_run_demo(output_path=None, **kwargs):
        calls.append({"output_path": output_path, **kwargs})
        return type("Summary", (), {"done_count": 4})()

    monkeypatch.setattr(main, "run_demo", fake_run_demo)

    assert main.main(["demo"]) == 0
    assert calls == [{"output_path": main.DEMO_OUTPUT}]


def test_llm_evidence_file_is_gitignored_but_committed_evidence_is_not():
    import subprocess

    def ignored(path: str) -> bool:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", "--no-index", path],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            ).returncode
            == 0
        )

    assert ignored("artifacts/demo-run-llm.json")
    assert not ignored("artifacts/demo-run.json")


def test_demo_help_no_longer_claims_offline_only(capsys):
    with pytest.raises(SystemExit):
        main.parse_args(["--help"])

    assert "offline audited pipeline demo" not in capsys.readouterr().out
