from __future__ import annotations

import hashlib
import inspect
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.candidate_evaluation import FactoryCandidateManifest
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from autogen_core.models import ModelFamily, ModelInfo
from autogen_ext.models.replay import ReplayChatCompletionClient


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def replay_client(responses: list[str]) -> ReplayChatCompletionClient:
    return ReplayChatCompletionClient(
        responses,
        model_info=ModelInfo(
            vision=False,
            function_calling=False,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )


def _put_same_artifact_from_process(root: str) -> dict[str, str]:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    reference = BusinessBenchmarkContentAddressedArtifactStore(Path(root)).put(
        b"process-safe-content",
        "application/octet-stream",
        namespace="concurrent",
    )
    return reference.model_dump(mode="json")


def artifact(label: str, digest: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://tests/{label}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def live_job() -> AgentFactoryJobV3:
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "71000000-0000-0000-0000-000000000001",
            "correlation_id": "71000000-0000-0000-0000-000000000002",
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "71000000-0000-0000-0000-000000000003",
            "subject_version": 2,
            "input_ref": artifact("input", "a" * 64, "text/markdown"),
            "compiled_spec_ref": artifact("spec", "b" * 64),
            "dependency_graph_ref": artifact("graph", "c" * 64),
            "required_capability": "benchmark_claims",
            "acceptance_assertion_ids": ["business_value"],
            "private_holdout_refs": [
                {
                    "holdout_id": "holdout-dddddddddddd",
                    "uri": "holdout://holdout-dddddddddddd",
                    "sha256": "d" * 64,
                }
            ],
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "demo",
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": 1,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def invocation(job: AgentFactoryJobV3) -> FactorySkillInvocationV1:
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://business-benchmark/claims",
        now=NOW,
    )
    return FactorySkillInvocationV1(
        schema="captain.factory-skill-invocation.v1",
        invocation_id=UUID("71000000-0000-0000-0000-000000000004"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id="captain-factory-execute-team",
            version=1,
            capability="factory_workflow",
            content_ref=artifact("skill", "e" * 64),
            content_sha256="e" * 64,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        input_ref=job.input_ref,
        input_sha256=job.input_ref.sha256,
        lease=lease,
        idempotency_key="f" * 64,
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        execution_scope_ref=job.private_holdout_refs[0],
    )


def test_content_store_round_trips_exact_reference_and_local_path(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    content = b"sealed candidate bytes"
    reference = store.put(
        content,
        "application/zip",
        namespace="candidate-archive",
    )

    assert reference.sha256 == hashlib.sha256(content).hexdigest()
    assert reference.media_type == "application/zip"
    assert store.read_bytes(reference) == content
    assert store.local_path(reference).read_bytes() == content


def test_content_store_rejects_changed_media_type_and_external_reference(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    reference = store.put(b"evidence", "application/json", namespace="evidence")

    with pytest.raises(ValueError, match="reference metadata"):
        store.read_bytes(
            reference.model_copy(update={"media_type": "text/plain"})
        )
    with pytest.raises(ValueError, match="outside"):
        store.read_bytes(
            ArtifactRef(
                uri=f"artifact://external/{reference.sha256}",
                sha256=reference.sha256,
                media_type=reference.media_type,
            )
        )


def test_content_store_binding_is_write_once_and_digest_verified(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    first = store.put(b"one", "application/json", namespace="manifest")
    second = store.put(b"two", "application/json", namespace="manifest")

    assert store.bind("candidate-for-job", "job-1", first) == first
    assert store.bind("candidate-for-job", "job-1", first) == first
    assert store.binding("candidate-for-job", "job-1") == first
    with pytest.raises(ValueError, match="binding changed"):
        store.bind("candidate-for-job", "job-1", second)


def test_content_store_concurrent_writers_observe_one_complete_artifact(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    root = tmp_path / ".captain-cook" / "benchmark-cas"
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(_put_same_artifact_from_process, (str(root),) * 4))

    assert len({json.dumps(item, sort_keys=True) for item in results}) == 1
    reference = ArtifactRef.model_validate(results[0])
    assert BusinessBenchmarkContentAddressedArtifactStore(root).read_bytes(
        reference
    ) == b"process-safe-content"


def test_content_store_rejects_traversal_components(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    with pytest.raises(ValueError, match="namespace"):
        store.put(b"escape", "text/plain", namespace="../escape")
    with pytest.raises(ValueError, match="binding kind"):
        store.binding("../escape", "job")


@pytest.mark.parametrize(
    "root",
    [
        Path("ordinary-root"),
        Path("..") / ".captain-cook-pretender" / "cas",
    ],
)
def test_content_store_requires_explicit_private_namespace(
    tmp_path: Path, root: Path
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    with pytest.raises(ValueError, match="gitignored .captain-cook"):
        BusinessBenchmarkContentAddressedArtifactStore(tmp_path / root)


def test_openai_builder_is_job_bound_and_keeps_secret_out_of_repr() -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        OpenAIBusinessBenchmarkModelClientBuilder,
    )

    created: list[dict[str, str]] = []
    replay = replay_client(["unused"])

    def build_client(*, api_key: str, model: str):
        created.append({"api_key": api_key, "model": model})
        return replay

    builder = OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
        {
            "CAPTAIN_BENCHMARK_PROVIDER": "openai",
            "CAPTAIN_BENCHMARK_MODEL": "approved-model-id",
            "OPENAI_API_KEY": "test-secret-never-log",
        },
        client_factory=build_client,
    )
    job = live_job()

    assert builder(job, invocation(job)) is replay
    assert created == [
        {"api_key": "test-secret-never-log", "model": "approved-model-id"}
    ]
    assert "test-secret-never-log" not in repr(builder)


def test_openai_builder_rejects_wrong_provider_and_non_live_job() -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        OpenAIBusinessBenchmarkModelClientBuilder,
    )

    with pytest.raises(ValueError, match="provider must be openai"):
        OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
            {
                "CAPTAIN_BENCHMARK_PROVIDER": "other",
                "CAPTAIN_BENCHMARK_MODEL": "approved-model-id",
                "OPENAI_API_KEY": "present",
            }
        )
    builder = OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
        {
            "CAPTAIN_BENCHMARK_PROVIDER": "openai",
            "CAPTAIN_BENCHMARK_MODEL": "approved-model-id",
            "OPENAI_API_KEY": "present",
        },
        client_factory=lambda **_: replay_client(["unused"]),
    )
    job = live_job()
    offline = job.model_copy(
        update={
            "execution_policy": job.execution_policy.model_copy(
                update={
                    "live_execution": False,
                    "max_cost_usd": Decimal("0"),
                    "required_live_runs": 0,
                    "allowed_models": (),
                    "live_capabilities": (),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="not Captain-authorized"):
        builder(offline, invocation(job))


def test_production_module_does_not_import_global_llm_environment_config() -> None:
    import agenten.agent_factory.business_benchmark_production_ports as ports

    assert "agenten.llm.model_client" not in inspect.getsource(ports)


def pricing_environment() -> dict[str, str]:
    return {
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "approved-model-id",
        "CAPTAIN_BENCHMARK_PRICING_VERSION": "openai-2026-07-28",
        "CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT": "2026-07-28T00:00:00Z",
        "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD": "0.50",
        "CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD": "1.25",
        "CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD": "10.00",
        "CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD": "0.01",
    }


def test_pricing_authority_returns_job_policy_model_and_time_bound_quote(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
        BusinessBenchmarkPricingAuthority,
        ConfiguredBusinessBenchmarkPricingSource,
        factory_execution_policy_sha256,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    source = ConfiguredBusinessBenchmarkPricingSource.from_environment(
        pricing_environment(), artifacts=store
    )
    authority = BusinessBenchmarkPricingAuthority(source)
    job = live_job()
    quote = authority.resolve(
        job=job,
        invocation=invocation(job),
        provider="openai",
        model="approved-model-id",
        now=NOW,
    )

    assert quote.job_id == job.job_id
    assert quote.subject_version == job.subject_version
    assert quote.execution_policy_sha256 == factory_execution_policy_sha256(job)
    assert quote.max_cost_per_call == Decimal("0.50")
    evidence = json.loads(store.read_bytes(quote.evidence_ref))
    assert evidence["pricing_version"] == "openai-2026-07-28"
    assert "secret" not in json.dumps(evidence).lower()


def test_pricing_authority_fails_closed_for_changed_invocation_model_or_time(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkContentAddressedArtifactStore,
        BusinessBenchmarkPricingAuthority,
        ConfiguredBusinessBenchmarkPricingSource,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    authority = BusinessBenchmarkPricingAuthority(
        ConfiguredBusinessBenchmarkPricingSource.from_environment(
            pricing_environment(), artifacts=store
        )
    )
    job = live_job()
    with pytest.raises(ValueError, match="unknown"):
        authority.resolve(
            job=job,
            invocation=invocation(job),
            provider="openai",
            model="different-model",
            now=NOW,
        )
    changed = invocation(job).model_copy(
        update={"job_id": UUID("71000000-0000-0000-0000-000000000099")}
    )
    with pytest.raises(ValueError, match="invocation"):
        authority.resolve(
            job=job,
            invocation=changed,
            provider="openai",
            model="approved-model-id",
            now=NOW,
        )
    with pytest.raises(ValueError, match="unknown"):
        authority.resolve(
            job=job,
            invocation=invocation(job),
            provider="openai",
            model="approved-model-id",
            now=NOW - timedelta(days=1),
        )


def stored_candidate(
    store,
    *,
    candidate_id: str = "claims_resolution_v1",
    source_ref: ArtifactRef | None = None,
) -> tuple[FactoryCandidateManifest, ArtifactRef]:
    source = source_ref or store.put(
        b"sealed archive bytes",
        "application/zip",
        namespace="candidate-archive",
    )
    team = store.put(b'{"schema":"autogen-team.v1"}', "application/json", namespace="team")
    workflow = store.put(b"{}", "application/json", namespace="workflow")
    input_schema = store.put(
        b'{"type":"object","title":"input"}', "application/json", namespace="tool-schema"
    )
    output_schema = store.put(
        b'{"type":"object","title":"output"}', "application/json", namespace="tool-schema"
    )
    manifest = FactoryCandidateManifest(
        candidate_id=candidate_id,
        source_archive_ref=source,
        team_manifest={"reference": team, "relative_path": "team.json"},
        workflow_artifacts=(
            {"reference": workflow, "relative_path": "workflows/claims.json"},
        ),
        tool_schema_artifacts=(
            {"reference": input_schema, "relative_path": "schemas/input.json"},
            {"reference": output_schema, "relative_path": "schemas/output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="claims_lookup",
                description="Read a synthetic claims record.",
                input_schema_ref=input_schema.uri,
                output_schema_ref=output_schema.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=60,
    )
    manifest_ref = store.put(
        manifest.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="candidate-manifest",
    )
    return manifest, manifest_ref


def test_candidate_authority_resolves_only_exact_job_id_and_candidate_ref(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkCandidateAuthority,
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    job = live_job()
    manifest, manifest_ref = stored_candidate(store)
    authority = BusinessBenchmarkCandidateAuthority(store)
    binding_ref = authority.bind_candidate(job=job, manifest_ref=manifest_ref)

    resolved = authority.resolve(
        job=job,
        expected_candidate_id=manifest.candidate_id,
        expected_candidate_ref=manifest.source_archive_ref,
    )

    assert binding_ref.media_type == "application/json"
    assert resolved.candidate == manifest
    assert resolved.source_archive == store.local_path(manifest.source_archive_ref)


@pytest.mark.parametrize("changed", ["id", "reference", "job"])
def test_candidate_authority_rejects_changed_expected_scope(
    tmp_path: Path, changed: str
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkCandidateAuthority,
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    job = live_job()
    manifest, manifest_ref = stored_candidate(store)
    authority = BusinessBenchmarkCandidateAuthority(store)
    authority.bind_candidate(job=job, manifest_ref=manifest_ref)
    requested_job = (
        job.model_copy(
            update={"job_id": UUID("71000000-0000-0000-0000-000000000099")}
        )
        if changed == "job"
        else job
    )
    candidate_id = "different_candidate" if changed == "id" else manifest.candidate_id
    candidate_ref = (
        store.put(b"different", "application/zip", namespace="candidate-archive")
        if changed == "reference"
        else manifest.source_archive_ref
    )

    with pytest.raises(ValueError, match="candidate"):
        authority.resolve(
            job=requested_job,
            expected_candidate_id=candidate_id,
            expected_candidate_ref=candidate_ref,
        )


def test_candidate_authority_rejects_manifest_with_archive_outside_cas(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkCandidateAuthority,
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    external = artifact("external-archive", "9" * 64, "application/zip")
    manifest, manifest_ref = stored_candidate(store, source_ref=external)
    authority = BusinessBenchmarkCandidateAuthority(store)

    with pytest.raises(ValueError, match="outside"):
        authority.bind_candidate(job=live_job(), manifest_ref=manifest_ref)


def test_candidate_authority_detects_source_archive_digest_change(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_production_ports import (
        BusinessBenchmarkCandidateAuthority,
        BusinessBenchmarkContentAddressedArtifactStore,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    job = live_job()
    manifest, manifest_ref = stored_candidate(store)
    authority = BusinessBenchmarkCandidateAuthority(store)
    authority.bind_candidate(job=job, manifest_ref=manifest_ref)
    store.local_path(manifest.source_archive_ref).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest changed"):
        authority.resolve(
            job=job,
            expected_candidate_id=manifest.candidate_id,
            expected_candidate_ref=manifest.source_archive_ref,
        )
