from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory import production_adapter_bundle as production
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.forge_contracts import ArtifactRef, ReleasedSkillRefV1
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job
from agenten.agent_factory.skill_evaluation import HermesSkillUsageReceipt


NOW = datetime(2026, 7, 22, 0, 30, tzinfo=timezone.utc)


def _usage(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "estimated_cost_usd": "0.04",
        "cost_status": "estimated",
        "cost_source": "provider-usage",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 120,
        "api_calls": 1,
        "model": "approved-model",
        "provider": "approved-provider",
        "session_id": "hermes-creation-session",
        "completed": True,
        "failed": False,
        "service_tier": None,
    }
    value.update(updates)
    return value


def _job_and_creation(artifacts: ContentAddressedArtifactStore):
    source = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs"
        / "agent_factory"
        / "sales_pipeline_brief"
        / "TO_BE_BUILT.md"
    )
    document = load_factory_input(source)
    compiled = FactoryInputCompiler(
        holdout_store=InMemoryPrivateHoldoutStore()
    ).compile(document, 1)
    job = build_factory_job(
        compiled,
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        now=NOW,
        wall_clock_budget_seconds=600,
    )
    skill = artifacts.put(
        b"released skill bytes",
        "application/octet-stream",
        namespace="released-skill",
    )
    released = ReleasedSkillRefV1(
        skill_id="captain-agent-factory-loop",
        version=1,
        content_ref=ArtifactRef.model_validate(skill.model_dump(mode="json")),
        content_sha256=skill.sha256,
    )
    entrypoint = object.__new__(production.ProductionCapabilityFactoryEntrypoint)
    entrypoint._production_artifacts = artifacts
    artifacts.put(
        source.read_bytes(), document.input_ref.media_type, namespace="factory-input"
    )
    creation = entrypoint._build_creation_job(
        job,
        compiled=compiled,
        creation_key="factory-create-" + "a" * 64,
        released_skill=released,
    )
    return job, creation


@pytest.mark.asyncio
async def test_creation_analysis_materializes_exact_hermes_evidence_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(production, "ProductionHermesCreationAnalysis")
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    job, creation = _job_and_creation(artifacts)
    skill_path = tmp_path / "released-skills" / "captain-agent-factory-loop"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("# Real released skill\n", encoding="utf-8")
    calls: list[str] = []

    payload = {
        "schema": "captain.hermes-creation-analysis.v1",
        "creation_job_id": str(creation.creation_job_id),
        "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version,
        "receipt": {
            "receipt_id": "22222222-2222-4222-8222-222222222222",
            "request_id": "33333333-3333-4333-8333-333333333333",
            "lease_id": "hermes-creation-analysis",
            "occurred_at": NOW.isoformat(),
            "commands": [{"command_id": "hermes.creation-analysis", "max_seconds": 60}],
            "assertion_ids": list(job.acceptance_assertion_ids),
            "outcome": "blocked_tool_gap",
        },
        "tool_gaps": [
            {
                "schema": "TODO_TOOL.v1",
                "gap_id": "crm-write-api",
                "severity": "required",
                "input_contract": {"schema": "crm.write.input.v1", "type": "object"},
                "output_contract": {"schema": "crm.write.output.v1", "type": "object"},
                "least_privilege_capability": "crm.write",
                "implementation_options": [
                    {
                        "option_id": "n8n-crm-write",
                        "description": "Use the Captain-approved n8n workflow.",
                        "acceptance_assertion_id": job.acceptance_assertion_ids[0],
                    }
                ],
                "acceptance_assertion_ids": [job.acceptance_assertion_ids[0]],
                "evidence": {"reason": "The requested CRM write API is not configured."},
                "status": "unresolved",
            }
        ],
    }

    async def run_prompt(
        _self: HermesCliFactory,
        prompt: str,
        *,
        max_seconds: float,
        usage_file: Path | None = None,
    ) -> bytes:
        assert usage_file is not None
        assert max_seconds == 60
        assert json.loads(prompt)["released_skill_path"] == str(skill_path.resolve())
        request = json.loads(prompt)
        assert set(request["artifact_paths"]) == {
            "canonical_input",
            "compiled_spec",
            "dependency_graph",
            "released_skill_content",
        }
        assert all(Path(path).is_file() for path in request["artifact_paths"].values())
        assert {
            capability["capability"]: capability["status"]
            for capability in request["provided_capabilities"]
        } == {
            "artifact.read": "ready",
            "codex.run": "ready",
            "n8n.workflow.execute": "unavailable",
        }
        calls.append(prompt)
        usage_file.write_text(json.dumps(_usage()), encoding="utf-8")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    monkeypatch.setattr(HermesCliFactory, "_run_skill_prompt", run_prompt)
    analysis = production.ProductionHermesCreationAnalysis(
        artifacts=artifacts,
        hermes=HermesCliFactory(HermesCliSettings(executable="hermes")),
        released_skill_path=skill_path,
        max_cost_per_call_usd="0.25",
        timeout_seconds=60,
    )

    first = await analysis.analyze(job, creation)
    replayed = await analysis.analyze(job, creation)

    assert first == replayed
    assert len(calls) == 1
    bound = artifacts.binding("hermes-creation-evidence", str(creation.creation_job_id))
    assert bound == first.envelope_ref
    envelope = json.loads(artifacts.read_bytes(bound))
    assert set(envelope) == {
        "schema",
        "creation_job_id",
        "skill_usage_receipt_ref",
        "tool_gaps_ref",
    }
    receipt_ref = first.skill_usage_receipt_ref
    receipt = HermesSkillUsageReceipt.model_validate_json(artifacts.read_bytes(receipt_ref))
    assert receipt.job_id == job.job_id
    assert receipt.released_skill.content_ref.sha256 == creation.released_skill.content_sha256
    gaps = json.loads(artifacts.read_bytes(first.tool_gaps_ref))
    assert gaps["schema"] == "minibook.creation-tool-gaps.v1"
    marker = gaps["tool_gaps"][0]
    for name in ("input_contract_ref", "output_contract_ref", "evidence_ref"):
        reference = ArtifactRef.model_validate(marker[name])
        assert artifacts.read_bytes(reference)


@pytest.mark.asyncio
async def test_creation_analysis_fails_closed_when_paid_cost_exceeds_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(production, "ProductionHermesCreationAnalysis")
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    job, creation = _job_and_creation(artifacts)
    skill_path = tmp_path / "skill"
    skill_path.mkdir()

    async def run_prompt(
        _self: HermesCliFactory,
        _prompt: str,
        *,
        max_seconds: float,
        usage_file: Path | None = None,
    ) -> bytes:
        del max_seconds
        assert usage_file is not None
        usage_file.write_text(json.dumps(_usage(estimated_cost_usd="0.26")), encoding="utf-8")
        return b"{}"

    monkeypatch.setattr(HermesCliFactory, "_run_skill_prompt", run_prompt)
    analysis = production.ProductionHermesCreationAnalysis(
        artifacts=artifacts,
        hermes=HermesCliFactory(),
        released_skill_path=skill_path,
        max_cost_per_call_usd="0.25",
        timeout_seconds=60,
    )

    with pytest.raises(ValueError, match="cost exceeds"):
        await analysis.analyze(job, creation)
    assert artifacts.binding("hermes-creation-evidence", str(creation.creation_job_id)) is None
