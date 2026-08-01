from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from agenten.agent_factory.business_benchmark_candidate_seeds import (
    CLAIMS_SEED_PROFILE,
    RENEWAL_SEED_PROFILE,
    package_business_benchmark_seed,
    public_business_benchmark_build_contract,
    validate_public_business_benchmark_candidate,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    FactoryCandidateEvaluator,
)


@pytest.mark.parametrize(
    ("profile_id", "candidate_id", "pattern", "agent_names"),
    [
        (
            CLAIMS_SEED_PROFILE,
            "insurance_claims_resolution_swarm_v1",
            "swarm",
            ("intake_specialist", "coverage_specialist", "escalation_specialist"),
        ),
        (
            RENEWAL_SEED_PROFILE,
            "customer_renewal_orchestration_team_v1",
            "swarm",
            ("renewal_analyst", "commercial_advisor", "human_review_coordinator"),
        ),
    ],
)
def test_seed_candidate_is_reproducible_and_evaluator_verified(
    tmp_path: Path,
    profile_id: str,
    candidate_id: str,
    pattern: str,
    agent_names: tuple[str, ...],
) -> None:
    first = package_business_benchmark_seed(profile_id, tmp_path / "first")
    second = package_business_benchmark_seed(profile_id, tmp_path / "second")

    assert first.source_archive.read_bytes() == second.source_archive.read_bytes()
    assert first.candidate == second.candidate
    assert first.candidate.candidate_id == candidate_id
    assert first.candidate.source_archive_ref.sha256 == second.candidate.source_archive_ref.sha256

    evaluator = FactoryCandidateEvaluator()
    result = evaluator.validate(first, max_seconds=10)
    assert result.status == "succeeded"
    assert result.team_execution_manifest is not None
    topology = result.team_execution_manifest
    assert topology.conversation_pattern == pattern
    assert tuple(agent.name for agent in topology.agents) == agent_names
    validate_public_business_benchmark_candidate(profile_id, first, topology)


@pytest.mark.parametrize("profile_id", [CLAIMS_SEED_PROFILE, RENEWAL_SEED_PROFILE])
def test_public_build_contract_is_normative_complete_and_holdout_free(
    profile_id: str,
) -> None:
    contract = public_business_benchmark_build_contract(profile_id)
    encoded = json.dumps(contract, sort_keys=True)

    assert contract["schema"] == "captain.business-benchmark-public-build-contract.v1"
    assert contract["profile_id"] == profile_id
    assert contract["conversation_pattern"] == "swarm"
    assert len(contract["agents"]) == 3
    assert contract["terminal_output"]["schema"] == (
        "captain.business-benchmark-terminal.v1"
    )
    assert len(contract["public_acceptance_categories"]) == 5
    assert "expected_decision" not in encoded
    assert "required_rationale_fact_ids" not in encoded
    assert "case_id" not in encoded


def test_public_candidate_guard_rejects_claims_team_without_escalation_agent(
    tmp_path: Path,
) -> None:
    resolved = package_business_benchmark_seed(CLAIMS_SEED_PROFILE, tmp_path)
    result = FactoryCandidateEvaluator().validate(resolved, max_seconds=10)
    assert result.team_execution_manifest is not None
    manifest = result.team_execution_manifest
    invalid = FactoryAutoGenTeamManifestV1.model_validate(
        {
            **manifest.model_dump(mode="json", by_alias=True),
            "agents": [
                {
                    **manifest.agents[0].model_dump(mode="json"),
                    "handoffs": ["coverage_specialist"],
                },
                {
                    **manifest.agents[1].model_dump(mode="json"),
                    "handoffs": [],
                },
            ],
            "max_messages": 2,
            "max_handoffs": 1,
        },
        context={"allowed_tools": set()},
    )

    with pytest.raises(ValueError, match="public build contract"):
        validate_public_business_benchmark_candidate(
            CLAIMS_SEED_PROFILE,
            resolved,
            invalid,
        )


def test_claims_seed_is_a_tool_free_swarm_with_sealed_business_rules(
    tmp_path: Path,
) -> None:
    resolved = package_business_benchmark_seed(CLAIMS_SEED_PROFILE, tmp_path)
    result = FactoryCandidateEvaluator().validate(resolved, max_seconds=10)
    assert result.team_execution_manifest is not None
    team = result.team_execution_manifest

    assert team.conversation_pattern == "swarm"
    assert all(agent.tools == () for agent in team.agents)
    assert team.agents[0].handoffs == ("coverage_specialist",)
    assert team.agents[1].handoffs == ("escalation_specialist",)
    assert team.agents[2].handoffs == ()
    assert resolved.candidate.workflow_artifacts == ()
    assert resolved.candidate.tool_schema_artifacts == ()
    assert resolved.candidate.n8n_tools == ()
    assert resolved.candidate.n8n_tool_references == ()

    with zipfile.ZipFile(resolved.source_archive) as archive:
        archived_names = set(archive.namelist())
        prompt_text = "\n".join(
            archive.read(name).decode("utf-8")
            for name in sorted(archive.namelist())
            if name.startswith("prompts/")
        )

    assert "captain.business-benchmark-terminal.v1" in prompt_text
    assert "route_standard_review" in prompt_text
    assert "request_information" in prompt_text
    assert "escalate_coverage" in prompt_text
    assert "coverage_state_verified" in prompt_text
    assert "critical_coverage_question_detected" in prompt_text
    assert "expected_decision" not in prompt_text
    assert "required_rationale_fact_ids" not in prompt_text
    assert "case_id" not in prompt_text
    assert not any(name.startswith("workflows/") for name in archived_names)
    assert not any(name.startswith("schemas/") for name in archived_names)

    source = (
        Path(__file__).parents[2]
        / "examples"
        / "business_benchmark_candidates"
        / CLAIMS_SEED_PROFILE
    )
    config = json.loads((source / "seed.json").read_text(encoding="utf-8"))
    assert not {"workflow_path", "input_schema_path", "output_schema_path", "tool"} & set(
        config
    )
    assert not any(path.is_file() for path in (source / "workflows").rglob("*"))
    assert not any(path.is_file() for path in (source / "schemas").rglob("*"))


def test_renewal_seed_has_one_read_only_idempotent_n8n_workflow(
    tmp_path: Path,
) -> None:
    resolved = package_business_benchmark_seed(RENEWAL_SEED_PROFILE, tmp_path)
    with FactoryCandidateEvaluator().verified_team_workspace(resolved) as (_, team):
        assert team.conversation_pattern == "swarm"

    assert tuple(tool.name for tool in resolved.candidate.n8n_tools) == (
        "renewal_context_read",
    )
    assert tuple(agent.tools for agent in team.agents) == (
        ("renewal_context_read",),
        (),
        (),
    )
    assert len(resolved.candidate.workflow_artifacts) == 1
    assert len(resolved.candidate.tool_schema_artifacts) == 2
    assert len(resolved.candidate.n8n_tool_references) == 1

    with zipfile.ZipFile(resolved.source_archive) as archive:
        workflow = json.loads(archive.read("workflows/renewal_context_read.json"))
        input_schema = json.loads(archive.read("schemas/renewal_context_read.input.json"))
        output_schema = json.loads(archive.read("schemas/renewal_context_read.output.json"))
        prompts = "\n".join(
            archive.read(name).decode("utf-8")
            for name in sorted(archive.namelist())
            if name.startswith("prompts/")
        )

    assert workflow["contract"]["intent"] == "n8n"
    assert workflow["contract"]["effect"] == "read_only"
    assert workflow["contract"]["idempotency"] == "required"
    assert workflow["contract"]["allowed_partitions"] == ["ordinary", "boundary"]
    assert workflow["contract"]["mutation_operations"] == []
    assert [node["type"] for node in workflow["nodes"]] == [
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.code",
    ]
    webhook = workflow["nodes"][0]
    assert webhook["name"] == "Typed Renewal Input"
    assert webhook["typeVersion"] == 2
    assert webhook["parameters"] == {
        "httpMethod": "POST",
        "path": "captain-renewal-context-read-v1",
        "responseMode": "lastNode",
        "options": {},
    }
    code = workflow["nodes"][1]["parameters"]["jsCode"]
    assert "const envelope = $json" in code
    assert "const input = envelope.body" in code
    assert "isPlainObject(envelope.body)" in code
    assert "const inputKeys" in code
    assert "const snapshotKeys" in code
    assert "inputKeys.every" in code
    assert "snapshotKeys.every" in code
    assert "const input = $json" not in code
    assert "envelope.body ||" not in code
    assert workflow["settings"] == {
        "availableInMCP": True,
        "executionOrder": "v1",
    }
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["operation"]["const"] == "read_renewal_context"
    assert set(input_schema["required"]) == {
        "operation",
        "idempotency_key",
        "evidence_partition",
        "synthetic_subject_id",
        "commercial_snapshot",
    }
    assert output_schema["additionalProperties"] is False
    assert set(output_schema["required"]) == {
        "operation",
        "idempotency_key",
        "status",
        "facts",
    }
    assert "Call renewal_context_read exactly once" in prompts
    assert "ordinary or boundary" in prompts
    assert "Never call it" in prompts
    assert "captain.business-benchmark-terminal.v1" in prompts
    assert "expected_decision" not in prompts
    assert "required_rationale_fact_ids" not in prompts
    assert "case_id" not in prompts


@pytest.mark.parametrize("profile_id", [CLAIMS_SEED_PROFILE, RENEWAL_SEED_PROFILE])
def test_seed_archives_contain_no_secret_or_deployment_material(
    tmp_path: Path,
    profile_id: str,
) -> None:
    resolved = package_business_benchmark_seed(profile_id, tmp_path)
    forbidden = (
        "openai_api_key",
        "authorization",
        "credential",
        "password",
        "private_key",
        "http://",
        "https://",
        "localhost",
    )

    with zipfile.ZipFile(resolved.source_archive) as archive:
        text = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in sorted(archive.namelist())
        ).lower()

    assert not any(marker in text for marker in forbidden)
