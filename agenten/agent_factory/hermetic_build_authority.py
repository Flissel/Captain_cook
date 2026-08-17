"""Injectable Captain authority for bounded offline Factory V3 builds.

This module is deliberately not part of the default or live composition.  It
provides bounded components used to compose the existing Captain Codex build
executor with the sealed, digest-frozen offline runtime.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

from agenten.agent_factory.codex_build_execution import (
    PreparedFactoryWorkspace,
)
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
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment


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


class FactoryV3SemanticEvidenceError(FactoryDispatchError):
    """Real execution evidence did not prove one frozen public assertion."""

    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


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


class CaptainStaticFactoryCredentialSession:
    """Release exactly one opaque action-approved credential reference."""

    def __init__(self) -> None:
        self._reference: str | None = None

    @property
    def reference(self) -> str:
        if self._reference is None:
            raise ValueError("approved account credential is required")
        return self._reference

    @property
    def references(self) -> tuple[str, ...]:
        return () if self._reference is None else (self._reference,)

    def release(self, credential_ref: str) -> None:
        if not credential_ref.startswith("credential://"):
            raise ValueError("only opaque credential references may be released")
        self._reference = credential_ref


class CaptainStaticFactoryWorkspacePreparer:
    """Create one empty, isolated workspace for the existing Codex executor."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def prepare_or_recover(  # type: ignore[no-untyped-def]
        self, request, invocation, brief, checkpoint
    ):
        del request, invocation, brief
        if checkpoint is not None:
            raise FactoryDispatchError("static Factory workspace cannot be resumed")
        self.root.mkdir(parents=True, exist_ok=False)
        return PreparedFactoryWorkspace(
            root=self.root,
            base_revision="0" * 40,
        )


class CaptainStaticFactoryExecutionAuthorizer:
    """Bind logical Codex launch metadata to one exact workspace and env."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        credential_session: CaptainStaticFactoryCredentialSession,
        host_environment: Mapping[str, str],
    ) -> None:
        self._root = workspace_root.resolve()
        self._credential_session = credential_session
        self._host_environment = host_environment

    def authorize(self, request) -> AuthorizedCodexRun:  # type: ignore[no-untyped-def]
        if request.workspace.resolve() != self._root or request.project_root != self._root:
            raise FactoryDispatchError("static Factory workspace authority changed")
        if request.command[:3] != ("codex", "exec", "--json"):
            raise FactoryDispatchError("static Factory logical command changed")
        environment = minimal_hermetic_subprocess_environment(
            credential_ref=self._credential_session.reference,
            host_environment=self._host_environment,
            temporary_directory=self._root,
        )
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        return AuthorizedCodexRun(
            workspace=self._root,
            command=request.command,
            environment=FrozenEnvironment(environment),
        )


class CaptainStaticFactoryBuildObserver:
    """Independently verify semantics emitted by the executed real case."""

    def __init__(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        compiled_specification: CompiledFactorySpecification,
        workspace_root: Path,
        credential_session: CaptainStaticFactoryCredentialSession,
    ) -> None:
        self._assignment = assignment
        self._compiled = compiled_specification
        self.workspace_root = workspace_root.resolve()
        self._credential_session = credential_session
        self.executed_commands: tuple[tuple[str, ...], ...] = ()
        self.real_case_handoff_trace: tuple[str, ...] = ()
        self.verified_public_assertion_ids: tuple[str, ...] = ()
        self.public_assertion_sources: dict[str, tuple[str, ...]] = {}
        self.tested_tools: set[str] = set()
        self.optional_tool_policy_evidence: dict[str, str] = {}
        self.external_write_attempts = 0
        self.external_write_blocks = 0
        self.child_sentinel_secret_seen = False

    @property
    def build_credential_refs(self) -> tuple[str, ...]:
        return self._credential_session.references

    def observe(self) -> None:
        command_evidence = json.loads(
            (self.workspace_root / "test-evidence.json").read_text(encoding="utf-8")
        )
        self.executed_commands = tuple(
            tuple(str(part) for part in command)
            for command in command_evidence["executed_commands"]
        )
        if self.executed_commands != _CANONICAL_COMMANDS:
            raise FactoryDispatchError("Captain command evidence is not exact")
        real_case = json.loads(
            (self.workspace_root / "evidence" / "real-case-output.json").read_text(
                encoding="utf-8"
            )
        )
        verification = FactoryV3AssertionVerifier().verify(
            compiled=self._compiled,
            assignment=self._assignment,
            evidence=real_case,
            credential_ref=self._credential_session.reference,
        )
        self.real_case_handoff_trace = tuple(real_case["handoff_trace"])
        self.verified_public_assertion_ids = verification.passed_ids
        self.public_assertion_sources = verification.sources
        tools = _dict(real_case, "tool_evidence")
        self.tested_tools = {
            tool_id
            for tool_id, value in tools.items()
            if isinstance(value, dict) and value.get("test_passed") is True
        }
        optional = _dict(real_case, "optional_tool_policy")
        self.optional_tool_policy_evidence = {
            "industry_signal_reader": str(optional["unavailable_result"])
        }
        self.external_write_attempts = int(real_case["external_write_attempts"])
        self.external_write_blocks = int(real_case["external_write_blocks"])
        self.child_sentinel_secret_seen = bool(real_case["sentinel_secret_seen"])


class CaptainFactoryV3PackageEvidenceIssuer:
    """Issue final V3 package receipts/evidence through Captain-owned CAS."""

    def __init__(
        self,
        *,
        cas: CodexBuildArtifactCas,
        receipt_issuer: CaptainCodexBuildReceiptIssuer,
        creation_job: CreationJobV3,
        observer: CaptainStaticFactoryBuildObserver,
    ) -> None:
        self._cas = cas
        self._receipt_issuer = receipt_issuer
        self._creation_job = creation_job
        self._observer = observer

    def issue(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        sealed: HermesFactoryCodexBuild,
    ) -> tuple[CodexBuildReceiptV1, ArtifactRef, ArtifactRef]:
        if (
            self._observer.executed_commands != _CANONICAL_COMMANDS
            or self._observer.verified_public_assertion_ids
            != self._creation_job.public_assertion_ids
        ):
            raise FactoryDispatchError(
                "Captain package evidence requires exact execution"
            )
        base = sealed.codex_build_receipt
        team_ref = self._cas.put_bytes(
            (self._observer.workspace_root / "team_manifest.json").read_bytes(),
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
            assertion_ids=self._observer.verified_public_assertion_ids,
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
    "CaptainStaticFactoryBuildObserver",
    "CaptainStaticFactoryCredentialSession",
    "CaptainStaticFactoryExecutionAuthorizer",
    "CaptainStaticFactoryWorkspacePreparer",
    "FactoryV3AssertionVerifier",
    "FactoryV3SemanticEvidenceError",
    "minimal_hermetic_subprocess_environment",
]
