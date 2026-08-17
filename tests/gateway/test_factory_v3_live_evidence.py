from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import zipfile
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agenten.agent_factory.forge_contracts import (
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_v3_live_authority import (
    FactoryV3CodexSessionBinding,
    FactoryV3CredentialAudiences,
    FactoryV3ExecutableBinding,
    FactoryV3ExecutableBindings,
    FactoryV3HermesRuntimeBinding,
    FactoryV3LiveAuthorityBinding,
    FactoryV3ReleasedSkillBinding,
    FactoryV3RunnerAttestationBinding,
    FactoryV3ServiceBinding,
)
from gateway.factory_v3_live_evidence import (
    FactoryV3AuthorityRecord,
    FactoryV3LiveEvidenceError,
    assert_factory_v3_live_secret_free,
    issue_factory_v3_live_runtime_attestation,
    verify_factory_v3_live_evidence,
)
import gateway.factory_v3_live_evidence as live_evidence
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import ArtifactRef as MinibookArtifactRef


RUN_ID = UUID("a1000000-0000-0000-0000-000000000001")
PREFLIGHT_SHA256 = "9" * 64
AUTHORITY_SHA256 = "a" * 64
SKILL_SHA256 = "b" * 64
CORRELATION_ID = UUID("a1000000-0000-0000-0000-000000000002")
FACTORY_JOB_ID = UUID("a1000000-0000-0000-0000-000000000003")
CREATION_JOB_ID = UUID("a1000000-0000-0000-0000-000000000004")
ASSIGNMENT_ID = UUID("a1000000-0000-0000-0000-000000000005")


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass
class _Authority:
    store: object
    hermes: tuple[FactoryV3AuthorityRecord, ...]
    codex: tuple[FactoryV3AuthorityRecord, ...]
    result: tuple[FactoryV3AuthorityRecord, ...]
    acceptance: tuple[FactoryV3AuthorityRecord, ...]
    attestation: tuple[FactoryV3AuthorityRecord, ...]
    tamper_captain_sha256: str | None = None
    tamper_public_sha256: str | None = None
    missing_public_sha256: str | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def hermes_receipts(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("hermes", str(correlation_id)))
        return self.hermes

    def codex_receipts(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("codex", str(correlation_id)))
        return self.codex

    def minibook_results(self, creation_job_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("minibook-result", str(creation_job_id)))
        return self.result

    def captain_acceptances(self, factory_job_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("captain-acceptance", str(factory_job_id)))
        return self.acceptance

    def prohibited_lifecycle_events(self, correlation_id: UUID) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("prohibited-events", str(correlation_id)))
        return ()

    def live_attestations(
        self, correlation_id: UUID
    ) -> tuple[FactoryV3AuthorityRecord, ...]:
        self.calls.append(("live-attestation", str(correlation_id)))
        return self.attestation

    def minibook_cas(self, reference: ArtifactRef) -> bytes:
        self.calls.append(("minibook-cas", reference.sha256))
        converted = MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))
        return self.store.read_bytes(converted)  # type: ignore[attr-defined]

    def captain_cas(self, reference: ArtifactRef) -> bytes:
        self.calls.append(("captain-cas", reference.sha256))
        converted = MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))
        content = self.store.read_bytes(converted)  # type: ignore[attr-defined]
        if self.tamper_captain_sha256 == reference.sha256:
            return content + b"tampered"
        return content

    def public_cas(self, reference: ArtifactRef) -> bytes:
        self.calls.append(("public-cas", reference.sha256))
        if self.missing_public_sha256 == reference.sha256:
            raise FileNotFoundError("missing public CAS object")
        converted = MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))
        content = self.store.read_bytes(converted)  # type: ignore[attr-defined]
        if self.tamper_public_sha256 == reference.sha256:
            return content + b"tampered"
        return content


def _record(store: object, ref: ArtifactRef) -> FactoryV3AuthorityRecord:
    return FactoryV3AuthorityRecord(
        reference=ref,
        content=store.read_bytes(ref),  # type: ignore[attr-defined]
    )


def _zip(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return stream.getvalue()


def _binding(runner_public: bytes, *, skill_sha256: str) -> FactoryV3LiveAuthorityBinding:
    return FactoryV3LiveAuthorityBinding(
        authority_id="captain-factory-v3-production",
        binding_version=1,
        gateway=FactoryV3ServiceBinding(
            "https://gateway.example",
            "/healthz",
            "captain-gateway",
            "v3",
            ("factory-v3-live-authority", "action-inbox-read"),
            "captain-gateway-live-v3",
        ),
        minibook=FactoryV3ServiceBinding(
            "https://minibook.example",
            "/healthz",
            "minibook-creation",
            "v3",
            ("creation-jobs-v3", "cas-readback-v1"),
            "minibook-creation-live-v3",
        ),
        credential_audiences=FactoryV3CredentialAudiences(
            "captain-gateway-live-v3",
            "minibook-creation-live-v3",
            "minibook-creation-api-v3",
        ),
        executables=FactoryV3ExecutableBindings(
            FactoryV3ExecutableBinding("c" * 64, "codex-cli 1.2.3"),
            FactoryV3ExecutableBinding("d" * 64, "7.5.2"),
            FactoryV3ExecutableBinding("e" * 64, "3.11.9"),
        ),
        codex_session=FactoryV3CodexSessionBinding(
            "f" * 64, "captain.codex-session.v1"
        ),
        hermes_runtime=FactoryV3HermesRuntimeBinding(
            "1" * 64,
            "2" * 40,
            "hermes-agent",
            "0.9.0",
            "3" * 64,
            ("factory-v3-build", "released-skill-execution"),
        ),
        released_skill=FactoryV3ReleasedSkillBinding(
            "captain-factory-brief-codex", skill_sha256
        ),
        runner_attestation=FactoryV3RunnerAttestationBinding(
            "captain-factory-v3-runner-2026-01", runner_public
        ),
        sha256=AUTHORITY_SHA256,
    )


def _live_fixture(
    tmp_path: Path,
    *,
    workspace_policy_overrides: Mapping[str, object] | None = None,
) -> tuple[
    dict[str, object],
    _Authority,
    CreationResultV2,
    FactoryV3LiveAuthorityBinding,
    Ed25519PrivateKey,
]:
    store = FilesystemCreationArtifactStore(tmp_path / "live-cas")
    runner_key = Ed25519PrivateKey.generate()
    runner_public = runner_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    skill_ref = store.put(
        b"# captain-factory-brief-codex released live skill\n",
        "text/markdown",
        namespace="released-skill",
    )
    binding = _binding(runner_public, skill_sha256=skill_ref.sha256)
    lease_ref = store.put(
        _json(
            {
                "schema": "captain.factory-v3-lease.v1",
                "status": "active",
                "assignment_id": str(ASSIGNMENT_ID),
                "creation_job_id": str(CREATION_JOB_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "correlation_id": str(CORRELATION_ID),
                "worker_id": "hermes-live-worker",
                "issued_at": "2026-08-17T12:00:00Z",
                "expires_at": "2026-08-17T12:05:00Z",
            }
        ),
        "application/json",
        namespace="lease",
    )
    workspace_policy = {
        "schema": "captain.factory-v3-workspace-policy.v1",
        "assignment_id": str(ASSIGNMENT_ID),
        "creation_job_id": str(CREATION_JOB_ID),
        "factory_job_id": str(FACTORY_JOB_ID),
        "correlation_id": str(CORRELATION_ID),
        "attempt": 1,
        "worker_id": "hermes-live-worker",
        "lease_ref": lease_ref.model_dump(mode="json"),
        "released_skill": {
            "skill_id": binding.released_skill.name,
            "content_sha256": binding.released_skill.sha256,
        },
        "workspace_ref": "workspace://captain/factory-v3-live",
        "writable_roots": ["src", "tests", "evidence"],
        "allowed_output_roots": ["src", "tests", "evidence"],
        "allowed_commands": [
            ["python", "-m", "compileall", "src"],
            ["python", "-m", "pytest"],
        ],
        "network_allowed": False,
        "max_wall_clock_seconds": 300,
        "deadline_at": "2026-08-17T12:05:00Z",
        "max_provider_cost_usd": 1.5,
        "max_files": 4096,
        "max_retries": 0,
    }
    if workspace_policy_overrides:
        workspace_policy.update(workspace_policy_overrides)
    workspace_policy_ref = store.put(
        _json(workspace_policy),
        "application/json",
        namespace="workspace-policy",
    )
    compiled_ref = store.put(
        _json(
            {
                "schema": "captain.compiled-factory-spec.v3",
                "correlation_id": str(CORRELATION_ID),
                "required_agent_ids": list((
                    "account_researcher",
                    "opportunity_qualifier",
                    "deal_strategist",
                    "executive_brief_writer",
                )),
            }
        ),
        "application/json",
        namespace="compiled-spec",
    )
    tool_plan_ref = store.put(
        _json(
            {
                "schema": "captain.factory-tool-plan.v1",
                "correlation_id": str(CORRELATION_ID),
                "required_tool_ids": [
                    "account_snapshot_reader",
                    "opportunity_scoring_rules",
                ],
            }
        ),
        "application/json",
        namespace="tool-plan",
    )
    team_manifest_ref = store.put(
        _json(
            {
                "schema": "minibook.team-manifest.v2",
                "agents": [{"agent_id": agent} for agent in (
                    "account_researcher",
                    "opportunity_qualifier",
                    "deal_strategist",
                    "executive_brief_writer",
                )],
                "handoffs": [
                    {"from_agent_id": "account_researcher", "to_agent_id": "opportunity_qualifier"},
                    {"from_agent_id": "opportunity_qualifier", "to_agent_id": "deal_strategist"},
                    {"from_agent_id": "deal_strategist", "to_agent_id": "executive_brief_writer"},
                ],
                "tools": [
                    {"tool_id": "account_snapshot_reader", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "implementation_path": "src/tools/account_snapshot_reader.py", "test_path": "tests/test_team.py"},
                    {"tool_id": "opportunity_scoring_rules", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "implementation_path": "src/tools/opportunity_scoring_rules.py", "test_path": "tests/test_team.py"},
                ],
                "source_files": [
                    "src/team.py",
                    "src/tools/account_snapshot_reader.py",
                    "src/tools/opportunity_scoring_rules.py",
                ],
                "test_files": ["tests/test_team.py"],
            }
        ),
        "application/json",
        namespace="team-manifest",
    )
    source_ref = store.put(
        _zip(
            {
                "src/team.py": b"TEAM = 'sales_pipeline_brief'\n",
                "src/tools/account_snapshot_reader.py": b"def read_account():\n    return {}\n",
                "src/tools/opportunity_scoring_rules.py": b"def score():\n    return 1\n",
                "tests/test_team.py": b"def test_team():\n    assert True\n",
            }
        ),
        "application/zip",
        namespace="source",
    )
    assignment = FactoryBuildAssignmentV2.model_validate(
        {
            "assignment_id": str(ASSIGNMENT_ID),
            "creation_job_id": str(CREATION_JOB_ID),
            "factory_job_id": str(FACTORY_JOB_ID),
            "correlation_id": str(CORRELATION_ID),
            "subject_version": 1,
            "checkpoint_version": 1,
            "attempt": 1,
            "effect_key": "4" * 64,
            "workspace_ref": "workspace://captain/factory-v3-live",
            "workspace_policy_sha256": workspace_policy_ref.sha256,
            "required_agent_ids": list(("account_researcher", "opportunity_qualifier", "deal_strategist", "executive_brief_writer")),
            "ordered_handoffs": [
                {"from_agent_id": "account_researcher", "to_agent_id": "opportunity_qualifier"},
                {"from_agent_id": "opportunity_qualifier", "to_agent_id": "deal_strategist"},
                {"from_agent_id": "deal_strategist", "to_agent_id": "executive_brief_writer"},
            ],
            "tool_requirements": [
                {"tool_id": "account_snapshot_reader", "required": True},
                {"tool_id": "opportunity_scoring_rules", "required": True},
            ],
            "allowed_output_roots": ["src", "tests", "evidence"],
            "allowed_commands": [["python", "-m", "compileall", "src"], ["python", "-m", "pytest"]],
            "limits": {"max_wall_clock_seconds": 300, "max_provider_cost_usd": 1.5, "max_retries": 0, "max_files": 4096},
            "lease_ref": lease_ref.model_dump(mode="json"),
            "deadline_at": "2026-08-17T12:05:00Z",
        }
    )
    assignment_ref = store.put(
        _json(assignment.model_dump(mode="json", by_alias=True)),
        "application/json",
        namespace="assignment",
    )
    candidate_ref = store.put(
        _json(
            {
                "schema": "captain.factory-candidate.v1",
                "status": "sealed",
                "creation_job_id": str(CREATION_JOB_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "correlation_id": str(CORRELATION_ID),
                "subject_version": 1,
                "attempt": 1,
                "worker_id": "hermes-live-worker",
                "released_skill_sha256": binding.released_skill.sha256,
                "assignment_ref": assignment_ref.model_dump(mode="json"),
                "workspace_policy_ref": workspace_policy_ref.model_dump(mode="json"),
                "team_manifest_ref": team_manifest_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
            }
        ),
        "application/json",
        namespace="candidate",
    )
    blueprint_ref = store.put(
        _json(
            {
                "schema": "captain.factory-v3-live-build-brief.v1",
                "creation_job_id": str(CREATION_JOB_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "correlation_id": str(CORRELATION_ID),
                "subject_version": 1,
                "attempt": 1,
                "assignment_ref": assignment_ref.model_dump(mode="json"),
                "workspace_policy_ref": workspace_policy_ref.model_dump(mode="json"),
                "team_manifest_ref": team_manifest_ref.model_dump(mode="json"),
                "candidate_manifest_ref": candidate_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
            }
        ),
        "application/json",
        namespace="build-brief",
    )
    workspace_contents = {
        "evidence/build-plan.json": _json(
            {
                "schema": "captain.factory-v3-build-plan.v1",
                "assignment_id": str(ASSIGNMENT_ID),
            }
        ),
        "src/team.py": b"TEAM = 'sales_pipeline_brief'\n",
        "src/tools/account_snapshot_reader.py": b"def read_account():\n    return {}\n",
        "src/tools/opportunity_scoring_rules.py": b"def score():\n    return 1\n",
        "tests/test_team.py": b"def test_team():\n    assert True\n",
    }
    workspace_member_refs = {
        path: store.put(
            content,
            "application/json" if path.endswith(".json") else "text/x-python",
            namespace="workspace-member",
        )
        for path, content in workspace_contents.items()
    }
    workspace_snapshot_ref = store.put(
        _zip(
            {
                "factory-v3-workspace-manifest.json": _json(
                    {
                        "schema": "captain.factory-v3-workspace-snapshot.v1",
                        "mode": "live",
                        "correlation_id": str(CORRELATION_ID),
                        "assignment_ref": assignment_ref.model_dump(mode="json"),
                        "workspace_policy_ref": workspace_policy_ref.model_dump(mode="json"),
                        "files": [
                            {
                                "path": path,
                                **workspace_member_refs[path].model_dump(mode="json"),
                                "size_bytes": len(workspace_contents[path]),
                            }
                            for path in sorted(workspace_contents)
                        ],
                    }
                ),
                **workspace_contents,
            }
        ),
        "application/zip",
        namespace="workspace",
    )
    session_ref = store.put(
        _json(
            {
                "schema": "captain.codex-live-session-evidence.v1",
                "mode": "live",
                "provider": "codex",
                "outcome": "completed",
                "session_id": "a1000000-0000-0000-0000-000000000006",
                "preflight_run_id": str(RUN_ID),
                "correlation_id": str(CORRELATION_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "creation_job_id": str(CREATION_JOB_ID),
                "worker_id": "hermes-live-worker",
                "lease_ref": lease_ref.model_dump(mode="json"),
                "active_released_skill_sha256": binding.released_skill.sha256,
                "assignment_ref": assignment_ref.model_dump(mode="json"),
                "workspace_policy_ref": workspace_policy_ref.model_dump(mode="json"),
                "workspace_snapshot_ref": workspace_snapshot_ref.model_dump(mode="json"),
                "codex_binary_sha256": binding.executables.codex.sha256,
                "codex_script_sha256": binding.codex_session.sha256,
                "completed_at": "2026-08-17T12:00:00Z",
            }
        ),
        "application/json",
        namespace="codex-session",
    )
    common = {
        "assignment_id": str(ASSIGNMENT_ID),
        "creation_job_id": str(CREATION_JOB_ID),
        "factory_job_id": str(FACTORY_JOB_ID),
        "correlation_id": str(CORRELATION_ID),
        "subject_version": 1,
        "checkpoint_version": 1,
        "attempt": 1,
        "effect_key": assignment.effect_key,
        "source_archive_ref": source_ref.model_dump(mode="json"),
        "codex_session_ref": session_ref.model_dump(mode="json"),
        "completed_at": "2026-08-17T12:00:00Z",
    }
    build_ref = store.put(
        _json(
            {
                "schema": "captain.package-build-evidence.v1",
                "producer": "captain",
                "outcome": "passed",
                **common,
                "workspace_snapshot_ref": workspace_snapshot_ref.model_dump(mode="json"),
            }
        ),
        "application/json",
        namespace="build-evidence",
    )
    real_ref = store.put(
        _json(
            {
                "schema": "captain.package-real-case-evidence.v1",
                "producer": "captain",
                "outcome": "passed",
                **common,
                "assertion_ids": ["sales_pipeline_exact_topology"],
                "declared_test_paths": ["tests/test_team.py"],
                "evidence_refs": [build_ref.model_dump(mode="json")],
            }
        ),
        "application/json",
        namespace="real-evidence",
    )
    hermes_ref = store.put(
        _json(
            {
                "schema": "hermes.forge-build-skill-usage-receipt.v1",
                "producer": "hermes",
                "outcome": "fulfilled",
                "creation_job_id": str(CREATION_JOB_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "correlation_id": str(CORRELATION_ID),
                "subject_version": 1,
                "attempt": 1,
                "idempotency_key": "5" * 64,
                "released_skill": {
                    "skill_id": binding.released_skill.name,
                    "version": 1,
                    "content_ref": skill_ref.model_dump(mode="json"),
                    "content_sha256": binding.released_skill.sha256,
                },
                "public_assertion_ids": ["sales_pipeline_exact_topology"],
                "evidence_refs": [
                    compiled_ref.model_dump(mode="json"),
                    blueprint_ref.model_dump(mode="json"),
                    tool_plan_ref.model_dump(mode="json"),
                ],
            }
        ),
        "application/json",
        namespace="hermes-receipt",
    )
    codex_ref = store.put(
        _json(
            {
                "schema": "captain.codex-build-receipt.v1",
                "receipt_id": "a1000000-0000-0000-0000-000000000007",
                "producer": "captain",
                "outcome": "sealed",
                "factory_job_id": str(FACTORY_JOB_ID),
                "creation_job_id": str(CREATION_JOB_ID),
                "correlation_id": str(CORRELATION_ID),
                "subject_version": 1,
                "attempt": 1,
                "assignment_id": str(ASSIGNMENT_ID),
                "idempotency_key": assignment.effect_key,
                "seal_idempotency_key": "6" * 64,
                "build_brief_ref": blueprint_ref.model_dump(mode="json"),
                "workspace_ref": assignment.workspace_ref,
                "codex_session_ref": session_ref.model_dump(mode="json"),
                "workspace_snapshot_ref": workspace_snapshot_ref.model_dump(mode="json"),
                "candidate_manifest_ref": candidate_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
                "test_evidence_refs": [real_ref.model_dump(mode="json")],
                "acceptance_assertion_ids": ["sales_pipeline_exact_topology"],
                "completed_at": "2026-08-17T12:00:00Z",
            }
        ),
        "application/json",
        namespace="codex-receipt",
    )
    effect = FactoryBuildEffectV2.model_validate(
        {
            "assignment_id": str(ASSIGNMENT_ID),
            "creation_job_id": str(CREATION_JOB_ID),
            "factory_job_id": str(FACTORY_JOB_ID),
            "correlation_id": str(CORRELATION_ID),
            "subject_version": 1,
            "checkpoint_version": 1,
            "attempt": 1,
            "effect_key": assignment.effect_key,
            "hermes_usage_receipt_ref": hermes_ref.model_dump(mode="json"),
            "codex_build_receipt_ref": codex_ref.model_dump(mode="json"),
            "codex_session_ref": session_ref.model_dump(mode="json"),
            "workspace_snapshot_ref": workspace_snapshot_ref.model_dump(mode="json"),
            "source_archive_ref": source_ref.model_dump(mode="json"),
            "preliminary_team_manifest_ref": team_manifest_ref.model_dump(mode="json"),
            "build_evidence_refs": [build_ref.model_dump(mode="json")],
            "test_evidence_refs": [real_ref.model_dump(mode="json")],
        }
    )
    effect_ref = store.put(
        _json(effect.model_dump(mode="json", by_alias=True)),
        "application/json",
        namespace="effect",
    )
    attestation_payload = {
        "schema": "captain.factory-v3-live-runtime-attestation.v1",
        "runner_id": "captain-live-runner",
        "runner_key_id": binding.runner_attestation.key_id,
        "mode": "live",
        "preflight_run_id": str(RUN_ID),
        "preflight_report_sha256": PREFLIGHT_SHA256,
        "authority_binding_sha256": binding.sha256,
        "active_released_skill_name": binding.released_skill.name,
        "active_released_skill_sha256": binding.released_skill.sha256,
        "correlation_id": str(CORRELATION_ID),
        "factory_job_id": str(FACTORY_JOB_ID),
        "creation_job_id": str(CREATION_JOB_ID),
        "codex_binary_sha256": binding.executables.codex.sha256,
        "pwsh_binary_sha256": binding.executables.pwsh.sha256,
        "hermes_python_sha256": binding.executables.hermes_python.sha256,
        "codex_session_script_sha256": binding.codex_session.sha256,
        "hermes_runtime_sha256": binding.hermes_runtime.runtime_sha256,
        "worker_id": "hermes-live-worker",
        "workspace_policy_ref": workspace_policy_ref.model_dump(mode="json"),
        "workspace_policy_sha256": workspace_policy_ref.sha256,
        "assignment_ref": assignment_ref.model_dump(mode="json"),
        "effect_ref": effect_ref.model_dump(mode="json"),
        "codex_session_ref": session_ref.model_dump(mode="json"),
        "workspace_snapshot_ref": workspace_snapshot_ref.model_dump(mode="json"),
        "issued_at": "2026-08-17T12:00:00Z",
    }
    attestation = issue_factory_v3_live_runtime_attestation(
        attestation_payload,
        binding=binding,
        signer=runner_key.sign,
    )
    attestation_ref = store.put(
        _json(attestation), "application/json", namespace="live-attestation"
    )
    package_children = {
        "team_manifest.json": team_manifest_ref,
        "factory-candidate.json": candidate_ref,
        "source/source-archive.zip": source_ref,
        "evidence/build-brief.json": blueprint_ref,
        "evidence/hermes.json": hermes_ref,
        "evidence/codex.json": codex_ref,
        "evidence/build.json": build_ref,
        "evidence/real-case.json": real_ref,
        "evidence/codex-session.json": session_ref,
        "evidence/workspace-snapshot.zip": workspace_snapshot_ref,
        "evidence/workspace-policy.json": workspace_policy_ref,
        "evidence/assignment.json": assignment_ref,
        "evidence/effect.json": effect_ref,
        "evidence/lease.json": lease_ref,
    }
    package_manifest_ref = store.put(
        _json(
            {
                "schema": "minibook.team-package-manifest.v1",
                "files": [
                    {"path": path, **ref.model_dump(mode="json")}
                    for path, ref in package_children.items()
                ],
            }
        ),
        "application/json",
        namespace="package-manifest",
    )
    readback_refs = (
        *package_children.values(),
        package_manifest_ref,
        source_ref,
        workspace_snapshot_ref,
        attestation_ref,
    )
    readback_ref = store.put(
        _json(
            {
                "schema": "minibook.cas-readback-receipt.v1",
                "artifacts": [
                    {**ref.model_dump(mode="json"), "verified": True}
                    for ref in readback_refs
                ],
            }
        ),
        "application/json",
        namespace="readback",
    )
    result = CreationResultV2.model_validate(
        {
            "creation_job_id": str(CREATION_JOB_ID),
            "factory_job_id": str(FACTORY_JOB_ID),
            "correlation_id": str(CORRELATION_ID),
            "subject_version": 1,
            "attempt": 1,
            "status": "succeeded",
            "team_manifest_ref": team_manifest_ref.model_dump(mode="json"),
            "candidate_manifest_ref": candidate_ref.model_dump(mode="json"),
            "source_archive_ref": source_ref.model_dump(mode="json"),
            "package_manifest_ref": package_manifest_ref.model_dump(mode="json"),
            "hermes_usage_receipt_ref": hermes_ref.model_dump(mode="json"),
            "codex_build_receipt_ref": codex_ref.model_dump(mode="json"),
            "build_evidence_refs": [build_ref.model_dump(mode="json")],
            "real_case_evidence_refs": [real_ref.model_dump(mode="json")],
            "cas_readback_receipt_ref": readback_ref.model_dump(mode="json"),
        }
    )
    result_ref = store.put(
        _json(result.model_dump(mode="json", by_alias=True)),
        "application/json",
        namespace="live-result",
    )
    acceptance_ref = store.put(
        _json(
            {
                "schema": "captain.factory-v3-live-acceptance.v1",
                "producer": "captain",
                "outcome": "accepted",
                "preflight_run_id": str(RUN_ID),
                "preflight_report_sha256": PREFLIGHT_SHA256,
                "authority_binding_sha256": binding.sha256,
                "active_released_skill_sha256": binding.released_skill.sha256,
                "correlation_id": str(CORRELATION_ID),
                "factory_job_id": str(FACTORY_JOB_ID),
                "creation_job_id": str(CREATION_JOB_ID),
                "subject_version": 1,
                "minibook_result_ref": result_ref.model_dump(mode="json"),
                "team_manifest_ref": team_manifest_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
                "cas_readback_receipt_ref": readback_ref.model_dump(mode="json"),
                "live_attestation_ref": attestation_ref.model_dump(mode="json"),
                "accepted_at": "2026-08-17T12:00:00Z",
            }
        ),
        "application/json",
        namespace="live-acceptance",
    )
    authority = _Authority(
        store=store,
        hermes=(_record(store, hermes_ref),),
        codex=(_record(store, codex_ref),),
        result=(_record(store, result_ref),),
        acceptance=(_record(store, acceptance_ref),),
        attestation=(_record(store, attestation_ref),),
    )
    index = {
        "schema": "captain.factory-v3-live-evidence-index.v1",
        "preflight_run_id": str(RUN_ID),
        "correlation_id": str(CORRELATION_ID),
        "factory_job_id": str(FACTORY_JOB_ID),
        "creation_job_id": str(CREATION_JOB_ID),
        "preflight_report_sha256": PREFLIGHT_SHA256,
        "authority_binding_sha256": binding.sha256,
        "active_released_skill_sha256": binding.released_skill.sha256,
    }
    return index, authority, result, binding, runner_key


def _alias_artifact(
    authority: _Authority,
    reference_payload: Mapping[str, object],
    *,
    namespace: str,
) -> ArtifactRef:
    original = ArtifactRef.model_validate(reference_payload)
    converted = MinibookArtifactRef.model_validate(original.model_dump(mode="json"))
    content = authority.store.read_bytes(converted)  # type: ignore[attr-defined]
    alias = authority.store.put(  # type: ignore[attr-defined]
        content,
        original.media_type,
        namespace=namespace,
    )
    return ArtifactRef.model_validate(alias.model_dump(mode="json"))


def _rebind_codex_record_with_alias(
    authority: _Authority,
    *,
    field: str,
    binding: FactoryV3LiveAuthorityBinding,
    runner_key: Ed25519PrivateKey,
) -> tuple[ArtifactRef, ArtifactRef]:
    codex_payload = json.loads(authority.codex[0].content)
    original_payload = (
        codex_payload[field][0]
        if field == "test_evidence_refs"
        else codex_payload[field]
    )
    original = ArtifactRef.model_validate(original_payload)
    alias = _alias_artifact(
        authority,
        original_payload,
        namespace=f"alias-{field.replace('_', '-')}",
    )
    if field == "test_evidence_refs":
        codex_payload[field] = [alias.model_dump(mode="json")]
    else:
        codex_payload[field] = alias.model_dump(mode="json")
    codex_ref = authority.store.put(  # type: ignore[attr-defined]
        _json(codex_payload),
        "application/json",
        namespace="codex-receipt-alias",
    )

    attestation_payload = json.loads(authority.attestation[0].content)
    effect_ref = ArtifactRef.model_validate(attestation_payload["effect_ref"])
    effect_payload = json.loads(
        authority.store.read_bytes(  # type: ignore[attr-defined]
            MinibookArtifactRef.model_validate(effect_ref.model_dump(mode="json"))
        )
    )
    effect_payload["codex_build_receipt_ref"] = codex_ref.model_dump(mode="json")
    rebound_effect_ref = authority.store.put(  # type: ignore[attr-defined]
        _json(effect_payload),
        "application/json",
        namespace="effect-alias",
    )

    unsigned_attestation = {
        key: value
        for key, value in attestation_payload.items()
        if key != "signature_ed25519"
    }
    unsigned_attestation["effect_ref"] = rebound_effect_ref.model_dump(mode="json")
    rebound_attestation = issue_factory_v3_live_runtime_attestation(
        unsigned_attestation,
        binding=binding,
        signer=runner_key.sign,
    )
    rebound_attestation_ref = authority.store.put(  # type: ignore[attr-defined]
        _json(rebound_attestation),
        "application/json",
        namespace="live-attestation-alias",
    )

    result_payload = json.loads(authority.result[0].content)
    result_payload["codex_build_receipt_ref"] = codex_ref.model_dump(mode="json")
    rebound_result_ref = authority.store.put(  # type: ignore[attr-defined]
        _json(result_payload),
        "application/json",
        namespace="live-result-alias",
    )
    acceptance_payload = json.loads(authority.acceptance[0].content)
    acceptance_payload["minibook_result_ref"] = rebound_result_ref.model_dump(
        mode="json"
    )
    acceptance_payload["live_attestation_ref"] = (
        rebound_attestation_ref.model_dump(mode="json")
    )
    rebound_acceptance_ref = authority.store.put(  # type: ignore[attr-defined]
        _json(acceptance_payload),
        "application/json",
        namespace="live-acceptance-alias",
    )
    authority.codex = (_record(authority.store, codex_ref),)
    authority.result = (_record(authority.store, rebound_result_ref),)
    authority.attestation = (_record(authority.store, rebound_attestation_ref),)
    authority.acceptance = (_record(authority.store, rebound_acceptance_ref),)
    return original, alias


def _workspace_context(
    index_payload: Mapping[str, object],
    authority: _Authority,
    binding: FactoryV3LiveAuthorityBinding,
) -> tuple[object, FactoryBuildAssignmentV2, bytes]:
    index = live_evidence.FactoryV3LiveEvidenceIndex.parse(index_payload)
    attestation = live_evidence.FactoryV3LiveRuntimeAttestation.parse(
        json.loads(authority.attestation[0].content),
        binding=binding,
        index=index,
    )
    assignment = FactoryBuildAssignmentV2.model_validate(
        json.loads(
            authority.store.read_bytes(  # type: ignore[attr-defined]
                MinibookArtifactRef.model_validate(
                    attestation.assignment_ref.model_dump(mode="json")
                )
            )
        )
    )
    snapshot = authority.store.read_bytes(  # type: ignore[attr-defined]
        MinibookArtifactRef.model_validate(
            attestation.workspace_snapshot_ref.model_dump(mode="json")
        )
    )
    return attestation, assignment, snapshot


def _zip_members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_live_evidence_reads_each_typed_authority_and_both_cas_owners(tmp_path: Path) -> None:
    index, authority, result, binding, _ = _live_fixture(tmp_path)

    verified = verify_factory_v3_live_evidence(
        index,
        authority=authority,
        authority_binding=binding,
    )

    assert verified.preflight_run_id == RUN_ID
    assert verified.correlation_id == UUID(str(index["correlation_id"]))
    assert verified.source_archive_sha256 == result.source_archive_ref.sha256  # type: ignore[union-attr]
    assert [name for name, _ in authority.calls[:4]] == [
        "hermes",
        "codex",
        "minibook-result",
        "captain-acceptance",
    ]
    assert "live-attestation" in [name for name, _ in authority.calls[:6]]
    public_digests = {
        digest for name, digest in authority.calls if name == "public-cas"
    }
    hermes_payload = json.loads(authority.hermes[0].content)
    codex_payload = json.loads(authority.codex[0].content)
    expected_public = {
        *(item["sha256"] for item in hermes_payload["evidence_refs"]),
        codex_payload["build_brief_ref"]["sha256"],
        codex_payload["codex_session_ref"]["sha256"],
        codex_payload["workspace_snapshot_ref"]["sha256"],
        codex_payload["candidate_manifest_ref"]["sha256"],
        codex_payload["source_archive_ref"]["sha256"],
        *(item["sha256"] for item in codex_payload["test_evidence_refs"]),
        *(item.sha256 for item in result.build_evidence_refs),
        *(item.sha256 for item in result.real_case_evidence_refs),
        result.package_manifest_ref.sha256,  # type: ignore[union-attr]
    }
    assert expected_public.issubset(public_digests)
    assert verified.as_dict()["hermes_invocation_count"] == 1
    assert verified.as_dict()["codex_session_count"] == 1


def test_positive_codex_candidate_ref_is_the_creation_result_candidate(
    tmp_path: Path,
) -> None:
    _, authority, result, _, _ = _live_fixture(tmp_path)
    codex_payload = json.loads(authority.codex[0].content)

    assert result.candidate_manifest_ref is not None
    assert codex_payload["candidate_manifest_ref"] == (
        result.candidate_manifest_ref.model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "edge",
    [
        "build_brief_ref",
        "codex_session_ref",
        "workspace_snapshot_ref",
        "candidate_manifest_ref",
        "source_archive_ref",
        "test_evidence_refs",
        "build_evidence_refs",
    ],
)
@pytest.mark.parametrize("failure_mode", ["tampered", "missing"])
def test_every_codex_and_build_edge_requires_exact_public_cas_readback(
    tmp_path: Path,
    edge: str,
    failure_mode: str,
) -> None:
    index, authority, result, binding, _ = _live_fixture(tmp_path)
    codex_payload = json.loads(authority.codex[0].content)
    if edge == "build_evidence_refs":
        digest = result.build_evidence_refs[0].sha256
    else:
        value = codex_payload[edge]
        digest = value[0]["sha256"] if isinstance(value, list) else value["sha256"]
    if failure_mode == "tampered":
        authority.tamper_public_sha256 = digest
    else:
        authority.missing_public_sha256 = digest

    with pytest.raises(FactoryV3LiveEvidenceError, match="public CAS"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


@pytest.mark.parametrize(
    "field",
    [
        "build_brief_ref",
        "candidate_manifest_ref",
        "source_archive_ref",
        "codex_session_ref",
        "workspace_snapshot_ref",
        "test_evidence_refs",
    ],
)
def test_codex_edge_alias_drift_is_rejected_even_with_same_content_digest(
    tmp_path: Path,
    field: str,
) -> None:
    index, authority, _, binding, runner_key = _live_fixture(tmp_path)
    original, alias = _rebind_codex_record_with_alias(
        authority,
        field=field,
        binding=binding,
        runner_key=runner_key,
    )
    assert alias.sha256 == original.sha256
    assert alias.uri != original.uri

    with pytest.raises(FactoryV3LiveEvidenceError, match="differs"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_commands", [["python", "-m", "pytest"]]),
        ("writable_roots", ["src", "tests", "evidence", "scratch"]),
        ("allowed_output_roots", ["src", "tests"]),
        ("network_allowed", True),
        ("max_wall_clock_seconds", 301),
        ("max_provider_cost_usd", 99.0),
        ("max_files", 1),
        ("deadline_at", "2026-08-17T12:06:00Z"),
        ("max_retries", 1),
        ("attempt", 2),
        ("worker_id", "foreign-worker"),
        (
            "released_skill",
            {
                "skill_id": "captain-factory-brief-codex",
                "content_sha256": "f" * 64,
            },
        ),
        (
            "lease_ref",
            {
                "uri": "artifact://minibook-creation/lease/" + "f" * 64,
                "sha256": "f" * 64,
                "media_type": "application/json",
            },
        ),
    ],
)
def test_workspace_policy_drift_from_assignment_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    index, authority, _, binding, _ = _live_fixture(
        tmp_path,
        workspace_policy_overrides={field: value},
    )

    with pytest.raises(FactoryV3LiveEvidenceError, match="workspace policy"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


def test_workspace_zip_rejects_undeclared_relative_directory() -> None:
    content = _zip(
        {
            "factory-v3-workspace-manifest.json": _json(
                {
                    "schema": "captain.factory-v3-workspace-snapshot.v1",
                    "mode": "live",
                    "correlation_id": str(CORRELATION_ID),
                    "assignment_sha256": "1" * 64,
                }
            ),
            "src/team.py": b"TEAM = True\n",
            "scratch/notes.txt": b"untracked\n",
        }
    )

    with pytest.raises(FactoryV3LiveEvidenceError, match="ZIP evidence"):
        live_evidence._validate_live_zip(content, workspace=True)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "scratch/notes.txt",
        "src/.env",
        "src/CON.py",
        "src/../escape.py",
        "src/cafe\u0301.py",
    ],
)
def test_workspace_zip_rejects_unsafe_reserved_or_unicode_paths(
    unsafe_path: str,
) -> None:
    content = _zip(
        {
            "factory-v3-workspace-manifest.json": _json(
                {
                    "schema": "captain.factory-v3-workspace-snapshot.v1",
                    "mode": "live",
                    "correlation_id": str(CORRELATION_ID),
                    "assignment_ref": {
                        "uri": "artifact://minibook-creation/assignment/" + "1" * 64,
                        "sha256": "1" * 64,
                        "media_type": "application/json",
                    },
                    "workspace_policy_ref": {
                        "uri": "artifact://minibook-creation/policy/" + "2" * 64,
                        "sha256": "2" * 64,
                        "media_type": "application/json",
                    },
                    "files": [],
                }
            ),
            unsafe_path: b"unsafe\n",
        }
    )

    with pytest.raises(FactoryV3LiveEvidenceError, match="ZIP evidence"):
        live_evidence._validate_live_zip(content, workspace=True)


def test_workspace_zip_rejects_casefold_collision_and_compression_bomb() -> None:
    collision = _zip(
        {
            "factory-v3-workspace-manifest.json": _json({"schema": "x"}),
            "src/Team.py": b"A = 1\n",
            "src/team.py": b"A = 2\n",
        }
    )
    with pytest.raises(FactoryV3LiveEvidenceError, match="ZIP evidence"):
        live_evidence._validate_live_zip(collision, workspace=True)

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "factory-v3-workspace-manifest.json",
            _json({"schema": "x"}),
        )
        archive.writestr("src/bomb.txt", b"A" * (1024 * 1024))
    with pytest.raises(FactoryV3LiveEvidenceError, match="ZIP evidence"):
        live_evidence._validate_live_zip(stream.getvalue(), workspace=True)


@pytest.mark.parametrize("mutation", ["undeclared_member", "member_content"])
def test_workspace_snapshot_requires_exact_manifest_and_public_cas_members(
    tmp_path: Path,
    mutation: str,
) -> None:
    index, authority, _, binding, _ = _live_fixture(tmp_path)
    attestation, assignment, snapshot = _workspace_context(index, authority, binding)
    members = _zip_members(snapshot)
    manifest = json.loads(members["factory-v3-workspace-manifest.json"])
    if mutation == "undeclared_member":
        manifest["files"] = manifest["files"][:-1]
    else:
        member_path = manifest["files"][0]["path"]
        members[member_path] += b"tampered"
    members["factory-v3-workspace-manifest.json"] = _json(manifest)

    with pytest.raises(FactoryV3LiveEvidenceError, match="workspace snapshot"):
        live_evidence.validate_factory_v3_live_workspace_snapshot(
            _zip(members),
            authority=authority,
            assignment=assignment,
            authoritative_assignment_ref=attestation.assignment_ref,
            workspace_policy_ref=attestation.workspace_policy_ref,
        )


def test_signed_attestation_policy_digest_must_equal_exact_policy_ref(
    tmp_path: Path,
) -> None:
    _, authority, _, binding, runner_key = _live_fixture(tmp_path)
    payload = json.loads(authority.attestation[0].content)
    payload.pop("signature_ed25519")
    payload["workspace_policy_sha256"] = "f" * 64

    with pytest.raises(FactoryV3LiveEvidenceError, match="attestation authority"):
        issue_factory_v3_live_runtime_attestation(
            payload,
            binding=binding,
            signer=runner_key.sign,
        )


def test_public_json_edge_rejects_noncanonical_content_with_valid_digest(
    tmp_path: Path,
) -> None:
    _, authority, _, _, _ = _live_fixture(tmp_path)
    payload = {"schema": "captain.factory-v3-live-build-brief.v1", "status": "x"}
    noncanonical = json.dumps(payload, indent=2).encode("utf-8")
    reference = authority.store.put(  # type: ignore[attr-defined]
        noncanonical,
        "application/json",
        namespace="noncanonical-edge",
    )

    with pytest.raises(FactoryV3LiveEvidenceError, match="public CAS JSON"):
        live_evidence._public_json(
            authority,
            ArtifactRef.model_validate(reference.model_dump(mode="json")),
        )


def test_receipt_only_or_self_authored_scalar_evidence_fails_closed(tmp_path: Path) -> None:
    index, authority, _, binding, _ = _live_fixture(tmp_path)
    self_authored = dict(index)
    self_authored["captain_acceptance_status"] = "accepted"
    with pytest.raises(FactoryV3LiveEvidenceError, match="index"):
        verify_factory_v3_live_evidence(
            self_authored,
            authority=authority,
            authority_binding=binding,
        )

    authority.acceptance = ()
    with pytest.raises(FactoryV3LiveEvidenceError, match="Captain.*exactly one"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


def test_duplicate_hermes_receipt_and_tampered_captain_cas_fail_closed(tmp_path: Path) -> None:
    index, authority, result, binding, _ = _live_fixture(tmp_path)
    authority.hermes = authority.hermes * 2
    with pytest.raises(FactoryV3LiveEvidenceError, match="Hermes.*exactly one"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )

    authority.hermes = authority.hermes[:1]
    assert result.team_manifest_ref is not None
    authority.tamper_captain_sha256 = result.team_manifest_ref.sha256
    with pytest.raises(FactoryV3LiveEvidenceError, match="CAS readback differs"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


def test_deterministic_or_unsigned_runtime_attestation_fails_closed(
    tmp_path: Path,
) -> None:
    index, authority, _, binding, runner_key = _live_fixture(tmp_path)
    payload = json.loads(authority.attestation[0].content)
    payload.pop("signature_ed25519")
    payload["mode"] = "deterministic"
    payload["signature_ed25519"] = base64.b64encode(
        runner_key.sign(_canonical(payload))
    ).decode("ascii")
    ref = authority.store.put(  # type: ignore[attr-defined]
        _json(payload),
        "application/json",
        namespace="deterministic-attestation",
    )
    authority.attestation = (_record(authority.store, ref),)

    with pytest.raises(FactoryV3LiveEvidenceError, match="live attestation"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )

    authority.attestation = ()
    with pytest.raises(FactoryV3LiveEvidenceError, match="attestation.*exactly one"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


def test_authority_skill_binding_and_public_cas_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    index, authority, result, binding, _ = _live_fixture(tmp_path)
    drifted = dict(index)
    drifted["active_released_skill_sha256"] = "f" * 64
    with pytest.raises(FactoryV3LiveEvidenceError, match="authority binding"):
        verify_factory_v3_live_evidence(
            drifted,
            authority=authority,
            authority_binding=binding,
        )

    assert result.package_manifest_ref is not None
    authority.tamper_public_sha256 = result.package_manifest_ref.sha256
    with pytest.raises(FactoryV3LiveEvidenceError, match="public CAS"):
        verify_factory_v3_live_evidence(
            index,
            authority=authority,
            authority_binding=binding,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"outer": [{"token": "redacted"}]},
        {"outer": [{"accessToken": "redacted"}]},
        {"outer": [{"secretValue": "redacted"}]},
        {"outer": {"api_key": "redacted"}},
        {"outer": {"credential": "credential://tenant/value"}},
        {"outer": {"password": "redacted"}},
        {"outer": {"safe": "Bearer abcdef"}},
        {"outer": {"safe": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"}},
        {"outer": {"safe": "secret-canary-value"}},
        {"outer": {"safe": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk" * 5}},
    ],
)
def test_recursive_secret_key_and_value_scanner_fails_closed(payload: object) -> None:
    with pytest.raises(FactoryV3LiveEvidenceError, match="secret"):
        assert_factory_v3_live_secret_free(payload)


def test_secret_scanner_allows_ids_digests_and_display_safe_status() -> None:
    assert_factory_v3_live_secret_free(
        {
            "run_id": str(RUN_ID),
            "artifact_sha256": hashlib.sha256(b"artifact").hexdigest(),
            "status": "verified",
            "nested": [{"agent_id": "account_researcher"}],
        }
    )
