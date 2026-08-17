"""Injectable Captain authority for bounded offline Factory V3 builds.

This module is deliberately not part of the default or live composition.  It
implements the existing Captain build-executor port for callers that provide a
workspace generator and explicitly opt into network-free execution.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import sys
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from agenten.agent_factory.codex_build_execution import CompletedCodexBuild
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    CreationJobV3,
    FactoryBuildAssignmentV2,
    ForgeBuildSkillUsageReceiptV1,
    PackageBuildEvidenceV1,
    PackageRealCaseEvidenceV1,
    ToolGapMarkerV1,
)
from agenten.agent_factory.hermes_factory_worker import HermesFactoryCodexBuild
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_factory.orchestration import FactoryDispatchError


_CANONICAL_COMMANDS = (
    ("python", "-m", "compileall", "src"),
    ("python", "-m", "pytest"),
)
_HOST_ENVIRONMENT_KEYS = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
)


def _canonical(value: object, *, newline: bool = False) -> bytes:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return content + (b"\n" if newline else b"")


def minimal_hermetic_subprocess_environment(
    *,
    credential_ref: str,
    host_environment: Mapping[str, str],
    temporary_directory: Path,
) -> dict[str, str]:
    """Return only system launch basics and the approved opaque reference."""

    if not credential_ref.startswith("credential://"):
        raise ValueError("only an opaque credential reference may enter the build")
    environment = {
        key: host_environment[key]
        for key in _HOST_ENVIRONMENT_KEYS
        if host_environment.get(key)
    }
    environment.update(
        {
            "FACTORY_CREDENTIAL_REF": credential_ref,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": str(temporary_directory),
            "TMP": str(temporary_directory),
        }
    )
    executable_directory = str(Path(sys.executable).resolve().parent)
    inherited_path = environment.get("PATH", "")
    environment["PATH"] = (
        executable_directory
        if not inherited_path
        else os.pathsep.join((executable_directory, inherited_path))
    )
    return environment


class FactoryWorkspaceGeneratorPort(Protocol):
    @property
    def sessions(self) -> int: ...

    @property
    def provider_cost_usd(self) -> float: ...

    def generate(self, assignment: FactoryBuildAssignmentV2) -> Path | None: ...


class FactoryV3SemanticEvidenceError(FactoryDispatchError):
    """Real execution evidence did not prove one frozen public assertion."""

    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class FactoryV3NullOutputError(ValueError):
    failure_code = "codex_null_output"


@dataclass(frozen=True, slots=True)
class FactoryV3AssertionVerification:
    passed_ids: tuple[str, ...]
    sources: dict[str, tuple[str, ...]]


class FactoryV3AssertionVerifier:
    """Map frozen compiled assertions to independently observed semantics."""

    def verify(
        self,
        *,
        compiled: CompiledFactorySpecification,
        assignment: FactoryBuildAssignmentV2,
        evidence: Mapping[str, object],
        credential_ref: str,
    ) -> FactoryV3AssertionVerification:
        trace = tuple(str(item) for item in _list(evidence, "handoff_trace"))
        handoffs = tuple(
            (str(item["from"]), str(item["to"]))
            for item in _dict_list(evidence, "handoffs")
        )
        expected_handoffs = tuple(
            (item.from_agent_id, item.to_agent_id)
            for item in assignment.ordered_handoffs
        )
        tools = _dict(evidence, "tool_evidence")
        optional = _dict(evidence, "optional_tool_policy")
        required_tools = {
            item.tool_id for item in assignment.tool_requirements if item.required
        }
        tool_pass = all(
            isinstance(tools.get(tool_id), dict)
            and tools[tool_id].get("status") == "passed"
            and tools[tool_id].get("typed_schema") is True
            and tools[tool_id].get("test_passed") is True
            for tool_id in required_tools
        )
        checks: dict[tuple[str, ...], bool] = {
            ("Acceptance outcomes", "exact_team"): (
                trace == assignment.required_agent_ids and handoffs == expected_handoffs
            ),
            ("Acceptance outcomes", "required_tools"): tool_pass,
            ("Real cases", "sales_pipeline"): (
                evidence.get("pipeline_status") == "passed"
                and bool(evidence.get("qualification_preserved"))
                and bool(evidence.get("next_action_preserved"))
            ),
            ("Integrations", "account_snapshot_reader"): (
                isinstance(tools.get("account_snapshot_reader"), dict)
                and tools["account_snapshot_reader"].get("result")
                == "approved_account_snapshot"
            ),
            ("Integrations", "opportunity_scoring_rules"): (
                isinstance(tools.get("opportunity_scoring_rules"), dict)
                and tools["opportunity_scoring_rules"].get("result")
                == "versioned_published_rule"
            ),
            ("Integrations", "industry_signal_reader"): (
                optional.get("available_result") == "bounded_public_signal"
                and optional.get("unavailable_result") == "typed_optional_gap"
            ),
            (
                "Security requirements",
                "only_opaque_credential_references_may_cross_the_action_boundary",
            ): (
                evidence.get("credential_ref_used") == credential_ref
                and credential_ref.startswith("credential://")
            ),
            ("Security requirements", "external_writes_are_forbidden"): (
                evidence.get("external_write_attempts") == 1
                and evidence.get("external_write_blocks") == 1
            ),
        }
        passed: list[str] = []
        sources: dict[str, tuple[str, ...]] = {}
        for assertion in compiled.assertions:
            source = tuple(assertion.source_path)
            if checks.get(source) is not True:
                code = (
                    "topology_drift"
                    if source
                    == (
                        "Acceptance outcomes",
                        "exact_team",
                    )
                    else "public_assertion_failed"
                )
                raise FactoryV3SemanticEvidenceError(
                    code,
                    f"public assertion {assertion.assertion_id} was not proven",
                )
            passed.append(assertion.assertion_id)
            sources[assertion.assertion_id] = source
        if tuple(passed) != assignment_public_assertion_ids(compiled):
            raise FactoryV3SemanticEvidenceError(
                "public_assertion_failed",
                "compiled public assertion identity changed",
            )
        return FactoryV3AssertionVerification(tuple(passed), sources)


def assignment_public_assertion_ids(
    compiled: CompiledFactorySpecification,
) -> tuple[str, ...]:
    return tuple(item.assertion_id for item in compiled.assertions)


class CaptainHermeticBuildExecutor:
    """Exact network-free implementation behind Captain's existing build port."""

    def __init__(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        creation_job: CreationJobV3,
        compiled_specification: CompiledFactorySpecification,
        generator: FactoryWorkspaceGeneratorPort,
        clock: Callable[[], datetime],
        network_enabled: bool = False,
    ) -> None:
        if network_enabled:
            raise ValueError("hermetic Factory builds must disable network access")
        if assignment.allowed_commands != _CANONICAL_COMMANDS:
            raise ValueError("hermetic Factory command authority must be exact")
        if (
            creation_job.public_assertion_ids
            != compiled_specification.public_assertion_ids
        ):
            raise ValueError("compiled assertion authority does not match creation job")
        self._assignment = assignment
        self._creation_job = creation_job
        self._compiled = compiled_specification
        self._generator = generator
        self._clock = clock
        self._credential_refs: tuple[str, ...] = ()
        self.executed_commands: tuple[tuple[str, ...], ...] = ()
        self.real_case_handoff_trace: tuple[str, ...] = ()
        self.verified_public_assertion_ids: tuple[str, ...] = ()
        self.public_assertion_sources: dict[str, tuple[str, ...]] = {}
        self.tested_tools: set[str] = set()
        self.optional_tool_policy_evidence: dict[str, str] = {}
        self.external_write_attempts = 0
        self.external_write_blocks = 0
        self.child_sentinel_secret_seen = False
        self.sealed_evidence = None
        self.workspace_root: Path | None = None

    @property
    def sessions(self) -> int:
        return self._generator.sessions

    @property
    def build_credential_refs(self) -> tuple[str, ...]:
        return self._credential_refs

    def release_credential(self, credential_ref: str) -> None:
        if not credential_ref.startswith("credential://"):
            raise ValueError("only opaque credential references may be released")
        self._credential_refs = (credential_ref,)

    async def execute(self, request, invocation, brief) -> CompletedCodexBuild:  # type: ignore[no-untyped-def]
        if not self._credential_refs:
            raise ValueError("approved account credential is required")
        if (
            self._generator.provider_cost_usd
            > self._assignment.limits.max_provider_cost_usd
        ):
            raise FactoryDispatchError(
                "hermetic provider cost exceeded assignment limit"
            )
        workspace = self._generator.generate(self._assignment)
        if workspace is None:
            raise FactoryV3NullOutputError(
                "Hermes Factory requires a typed Codex build result"
            )
        workspace = workspace.resolve(strict=True)
        for relative_root in self._assignment.allowed_output_roots:
            authorized_root = (workspace / relative_root).resolve()
            if workspace not in authorized_root.parents or not authorized_root.is_dir():
                raise FactoryDispatchError(
                    "hermetic workspace did not materialize an authorized output root"
                )
        files = tuple(path for path in workspace.rglob("*") if path.is_file())
        if len(files) > self._assignment.limits.max_files:
            raise FactoryDispatchError("hermetic workspace exceeded file limit")
        environment = minimal_hermetic_subprocess_environment(
            credential_ref=self._credential_refs[0],
            host_environment=os.environ,
            temporary_directory=workspace,
        )
        executed: list[tuple[str, ...]] = []
        for command in self._assignment.allowed_commands:
            canonical = tuple(command)
            if canonical not in _CANONICAL_COMMANDS:
                raise FactoryDispatchError("unauthorized hermetic command")
            subprocess.run(
                canonical,
                executable=sys.executable,
                cwd=workspace,
                env=environment,
                check=True,
                capture_output=True,
                timeout=self._assignment.limits.max_wall_clock_seconds,
            )
            executed.append(canonical)
        self.executed_commands = tuple(executed)
        real_case = json.loads(
            (workspace / "evidence" / "real-case-output.json").read_text(
                encoding="utf-8"
            )
        )
        verification = FactoryV3AssertionVerifier().verify(
            compiled=self._compiled,
            assignment=self._assignment,
            evidence=real_case,
            credential_ref=self._credential_refs[0],
        )
        self.real_case_handoff_trace = tuple(real_case["handoff_trace"])
        self.verified_public_assertion_ids = verification.passed_ids
        self.public_assertion_sources = verification.sources
        tool_evidence = _dict(real_case, "tool_evidence")
        self.tested_tools = {
            tool_id
            for tool_id, value in tool_evidence.items()
            if isinstance(value, dict) and value.get("test_passed") is True
        }
        optional = _dict(real_case, "optional_tool_policy")
        self.optional_tool_policy_evidence = {
            "industry_signal_reader": str(optional["unavailable_result"])
        }
        self.external_write_attempts = int(real_case["external_write_attempts"])
        self.external_write_blocks = int(real_case["external_write_blocks"])
        self.child_sentinel_secret_seen = bool(real_case["sentinel_secret_seen"])
        evidence = {
            "schema": "captain.hermetic-command-evidence.v1",
            "status": "passed",
            "executed_commands": [list(item) for item in self.executed_commands],
            "assertion_sources": {
                key: list(value) for key, value in verification.sources.items()
            },
        }
        (workspace / "test-evidence.json").write_bytes(
            _canonical(evidence, newline=True)
        )
        self.workspace_root = workspace
        return CompletedCodexBuild(
            workspace_root=workspace,
            codex_session_receipt=_canonical(
                {
                    "schema": "codex.session.v1",
                    "session_id": f"hermetic-{self._assignment.assignment_id}",
                    "status": "completed",
                }
            ),
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=self._clock(),
            output_snapshot_root=workspace,
        )

    async def execute_authorized_resume(self, request, invocation, brief):  # type: ignore[no-untyped-def]
        raise FactoryDispatchError("hermetic execution does not authorize retries")

    async def reconcile_pending(self, request, invocation, brief):  # type: ignore[no-untyped-def]
        raise FactoryDispatchError("hermetic execution has no pending reconciliation")

    def reconcile_failed(self, request, invocation, brief, **kwargs):  # type: ignore[no-untyped-def]
        raise FactoryDispatchError("hermetic execution has no failed reconciliation")

    def replay_sealed(self, _invocation):  # type: ignore[no-untyped-def]
        return self.sealed_evidence

    def validate_replay_authority(self, request, invocation) -> None:  # type: ignore[no-untyped-def]
        if request.job.job_id != self._assignment.factory_job_id:
            raise FactoryDispatchError("Captain replay authority changed")

    def validate_completed_outputs(self, invocation, completed) -> None:  # type: ignore[no-untyped-def]
        if completed.output_snapshot_root != completed.workspace_root:
            raise FactoryDispatchError("Captain output snapshot changed")

    def validate_issued_receipt(self, invocation, completed, receipt) -> None:  # type: ignore[no-untyped-def]
        if receipt.factory_job_id != self._assignment.factory_job_id:
            raise FactoryDispatchError("Captain issued receipt authority changed")

    def persist_sealed(self, invocation, completed, evidence):  # type: ignore[no-untyped-def]
        self.sealed_evidence = evidence
        return evidence


class CaptainFactoryV3PackageEvidenceIssuer:
    """Issue final V3 package receipts/evidence through Captain-owned CAS."""

    def __init__(
        self,
        *,
        cas: CodexBuildArtifactCas,
        receipt_issuer: CaptainCodexBuildReceiptIssuer,
        creation_job: CreationJobV3,
        executor: CaptainHermeticBuildExecutor,
    ) -> None:
        self._cas = cas
        self._receipt_issuer = receipt_issuer
        self._creation_job = creation_job
        self._executor = executor

    def issue(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        sealed: HermesFactoryCodexBuild,
    ) -> tuple[CodexBuildReceiptV1, ArtifactRef, ArtifactRef]:
        if (
            self._executor.executed_commands != _CANONICAL_COMMANDS
            or self._executor.verified_public_assertion_ids
            != self._creation_job.public_assertion_ids
            or self._executor.workspace_root is None
        ):
            raise FactoryDispatchError(
                "Captain package evidence requires exact execution"
            )
        base = sealed.codex_build_receipt
        team_ref = self._cas.put_bytes(
            (self._executor.workspace_root / "team_manifest.json").read_bytes(),
            media_type="application/json",
            namespace="preliminary-team",
        )
        common = {
            "assignment_id": assignment.assignment_id,
            "creation_job_id": assignment.creation_job_id,
            "factory_job_id": assignment.factory_job_id,
            "correlation_id": assignment.correlation_id,
            "subject_version": assignment.subject_version,
            "checkpoint_version": assignment.checkpoint_version,
            "attempt": assignment.attempt,
            "effect_key": assignment.effect_key,
            "source_archive_ref": base.source_archive_ref,
            "codex_session_ref": base.codex_session_ref,
            "completed_at": base.completed_at,
        }
        build_evidence = PackageBuildEvidenceV1(
            producer="captain",
            outcome="passed",
            workspace_snapshot_ref=base.workspace_snapshot_ref,
            **common,
        )
        build_ref = self._cas.put_bytes(
            _canonical(build_evidence.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="package-build",
        )
        real_case = PackageRealCaseEvidenceV1(
            producer="captain",
            outcome="passed",
            assertion_ids=self._executor.verified_public_assertion_ids,
            declared_test_paths=("tests/test_team.py",),
            evidence_refs=(build_ref,),
            **common,
        )
        real_ref = self._cas.put_bytes(
            _canonical(real_case.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="package-real-case",
        )
        payload = base.model_dump(mode="json", by_alias=True)
        payload.update(
            {
                "receipt_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{assignment.effect_key}:{team_ref.sha256}:{real_ref.sha256}",
                    )
                ),
                "workspace_ref": assignment.workspace_ref,
                "build_brief_ref": self._creation_job.blueprint_ref.model_dump(
                    mode="json"
                ),
                "candidate_manifest_ref": team_ref.model_dump(mode="json"),
                "test_evidence_refs": [real_ref.model_dump(mode="json")],
            }
        )
        receipt = CodexBuildReceiptV1.model_validate(payload)
        receipt_ref = self._receipt_issuer.persist_receipt(receipt)
        if self._receipt_issuer.read_receipt(receipt_ref) != receipt:
            raise FactoryDispatchError("Captain package receipt readback changed")
        return receipt, receipt_ref, build_ref

    def issue_hermes_usage(self) -> ArtifactRef:
        usage = ForgeBuildSkillUsageReceiptV1(
            producer="hermes",
            outcome="fulfilled",
            creation_job_id=self._creation_job.creation_job_id,
            factory_job_id=self._creation_job.factory_job_id,
            correlation_id=self._creation_job.correlation_id,
            subject_version=self._creation_job.subject_version,
            attempt=self._creation_job.attempt,
            idempotency_key=self._creation_job.idempotency_key,
            released_skill=self._creation_job.released_skill,
            public_assertion_ids=self._creation_job.public_assertion_ids,
            evidence_refs=(
                self._creation_job.compiled_spec_ref,
                self._creation_job.blueprint_ref,
                self._creation_job.tool_plan_ref,
            ),
        )
        return self._cas.put_bytes(
            _canonical(usage.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="forge-hermes-usage",
        )

    def issue_optional_gap(self, gap_id: str) -> ToolGapMarkerV1:
        gap_ref = self._cas.put_bytes(
            _canonical({"status": "unresolved", "kind": "optional"}),
            media_type="application/json",
            namespace="tool-gap",
        )
        return ToolGapMarkerV1(
            schema_name="TODO_TOOL.v1",
            gap_id=gap_id,
            severity="optional",
            evidence_ref=gap_ref,
            status="unresolved",
        )


def _dict(value: Mapping[str, object], key: str) -> dict[str, object]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise FactoryV3SemanticEvidenceError(
            "public_assertion_failed", f"real-case {key} evidence is invalid"
        )
    return result


def _list(value: Mapping[str, object], key: str) -> list[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise FactoryV3SemanticEvidenceError(
            "public_assertion_failed", f"real-case {key} evidence is invalid"
        )
    return result


def _dict_list(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    result = _list(value, key)
    if any(not isinstance(item, dict) for item in result):
        raise FactoryV3SemanticEvidenceError(
            "public_assertion_failed", f"real-case {key} evidence is invalid"
        )
    return result  # type: ignore[return-value]


__all__ = [
    "CaptainFactoryV3PackageEvidenceIssuer",
    "CaptainHermeticBuildExecutor",
    "FactoryV3AssertionVerifier",
    "FactoryV3SemanticEvidenceError",
    "FactoryWorkspaceGeneratorPort",
    "minimal_hermetic_subprocess_environment",
]
