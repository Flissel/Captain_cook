"""Test-only exact Captain runtime enforcement for the Task-9 hermetic proof.

The deterministic generator replaces only the unavailable external model.  A
separate executor enforces the V2 assignment, runs its canonical argv, and
hands the resulting workspace to the production Captain sealer and CAS.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Literal
from uuid import UUID
import zipfile

from agenten.agent_factory.codex_build_execution import (
    CaptainCodexAssignmentBuilder,
    CaptainCodexBuildSealer,
    CaptainV3CodexRequestMapper,
    CodexCliFactoryBuildSettings,
    CompletedCodexBuild,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.codex_build_recovery import canonical_factory_codex_model
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease
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
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef


_Variant = Literal["happy", "one_agent", "null_output", "timeout", "receipt_only"]
_EXPECTED_COMMANDS = (
    ("python", "-m", "compileall", "src"),
    ("python", "-m", "pytest"),
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


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 8, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return stream.getvalue()


class _PromptStore:
    def __init__(self, cas: CodexBuildArtifactCas) -> None:
        self._cas = cas

    def persist(self, _job_id: UUID, content: bytes) -> RuntimeArtifactRef:
        return RuntimeArtifactRef.model_validate(
            self._cas.put_bytes(
                content,
                media_type="application/json",
                namespace="v3-prompt",
            ).model_dump(mode="json")
        )


class _HermeticCaptainReceiptIssuer(CaptainCodexBuildReceiptIssuer):
    """Captain test authority that additionally binds the preliminary team file."""

    def __init__(self, cas: CodexBuildArtifactCas) -> None:
        super().__init__(cas)
        self._hermetic_cas = cas

    def issue(self, **kwargs) -> CodexBuildReceiptV1:  # type: ignore[no-untyped-def]
        receipt = super().issue(**kwargs)
        workspace = Path(kwargs["workspace_root"]).resolve(strict=True)
        team_bytes = (workspace / "team_manifest.json").read_bytes()
        team_ref = self._hermetic_cas.put_bytes(
            team_bytes,
            media_type="application/json",
            namespace="preliminary-team",
        )
        return receipt.model_copy(
            update={
                "candidate_manifest_ref": team_ref,
            }
        )


class _DeterministicExternalModel:
    """Only the external model replacement: it writes candidate workspace files."""

    def __init__(self, *, variant: _Variant, root: Path) -> None:
        self._variant = variant
        self._root = root
        self.sessions = 0

    @property
    def variant(self) -> _Variant:
        return self._variant

    def generate(
        self,
        assignment: FactoryBuildAssignmentV2,
        *,
        public_assertion_ids: tuple[str, ...],
    ) -> Path | None:
        self.sessions += 1
        if self._variant == "null_output":
            return None
        if self._variant == "timeout":
            raise TimeoutError("deterministic external model timeout")

        workspace = self._root / assignment.assignment_id.hex
        workspace.mkdir(parents=True, exist_ok=False)
        agents = assignment.required_agent_ids
        handoffs = assignment.ordered_handoffs
        if self._variant == "one_agent":
            agents = agents[:1]
            handoffs = ()
        manifest = self._team_manifest(agents, handoffs)
        files = self._workspace_files(
            agents=agents,
            public_assertion_ids=public_assertion_ids,
        )
        team_bytes = _canonical(manifest, newline=True)
        files["team_manifest.json"] = team_bytes
        team_sha256 = hashlib.sha256(team_bytes).hexdigest()
        team_ref = ArtifactRef(
            uri=f"artifact://minibook-creation/team-file/{team_sha256}",
            sha256=team_sha256,
            media_type="application/json",
        )
        candidate = {
            "schema": "captain.factory-candidate.v1",
            "candidate_id": "sales_pipeline_team",
            "team_manifest": {
                "reference": team_ref.model_dump(mode="json"),
                "relative_path": "team_manifest.json",
            },
            "workflow_artifacts": [],
            "tool_schema_artifacts": [],
            "n8n_tools": [],
            "n8n_tool_references": [],
            "host_tools": [],
            "build_command": list(_EXPECTED_COMMANDS[0]),
            "real_case_command": list(_EXPECTED_COMMANDS[1]),
            "timeout_seconds": 30,
        }
        files["factory-candidate.json"] = _canonical(candidate, newline=True)
        archive_files = {
            name: content
            for name, content in files.items()
            if name.startswith(("src/", "tests/"))
            or name == "factory-candidate.json"
        }
        files["candidate.zip"] = _zip_bytes(archive_files)
        for relative, content in files.items():
            target = workspace / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return workspace

    @staticmethod
    def _team_manifest(agents, handoffs) -> dict[str, object]:  # type: ignore[no-untyped-def]
        return {
            "schema": "minibook.team-manifest.v2",
            "agents": [{"agent_id": value} for value in agents],
            "handoffs": [item.model_dump(mode="json") for item in handoffs],
            "tools": [
                {
                    "tool_id": "account_snapshot_reader",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "implementation_path": "src/tools/account_snapshot_reader.py",
                    "test_path": "tests/test_team.py",
                },
                {
                    "tool_id": "opportunity_scoring_rules",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "implementation_path": "src/tools/opportunity_scoring_rules.py",
                    "test_path": "tests/test_team.py",
                },
            ],
            "source_files": [
                "src/__init__.py",
                "src/team.py",
                "src/tools/__init__.py",
                "src/tools/account_snapshot_reader.py",
                "src/tools/opportunity_scoring_rules.py",
            ],
            "test_files": ["tests/test_team.py"],
        }

    @staticmethod
    def _workspace_files(
        *, agents: tuple[str, ...], public_assertion_ids: tuple[str, ...]
    ) -> dict[str, bytes]:
        team = f'''\
from src.tools.account_snapshot_reader import read_account_snapshot
from src.tools.opportunity_scoring_rules import qualify_opportunity

AGENTS = {agents!r}
PUBLIC_ASSERTION_IDS = {public_assertion_ids!r}

def account_researcher(sales_input: dict[str, object], *, credential_ref: str) -> dict[str, object]:
    return read_account_snapshot(sales_input, credential_ref=credential_ref)

def opportunity_qualifier(account: dict[str, object]) -> dict[str, object]:
    return qualify_opportunity(account)

def deal_strategist(qualification: dict[str, object]) -> dict[str, object]:
    return {{"next_action": "Schedule an evidence review", "qualification": qualification["qualification"]}}

def executive_brief_writer(strategy: dict[str, object]) -> str:
    return f"{{strategy['qualification']}}; next action: {{strategy['next_action']}}"

def run_sales_pipeline(sales_input: dict[str, object], *, credential_ref: str) -> dict[str, object]:
    trace: list[str] = []
    account = account_researcher(sales_input, credential_ref=credential_ref)
    trace.append("account_researcher")
    if len(AGENTS) == 1:
        return {{
            "executive_brief": "incomplete",
            "handoff_trace": trace,
            "credential_ref_used": credential_ref,
            "verified_public_assertion_ids": list(PUBLIC_ASSERTION_IDS),
        }}
    qualification = opportunity_qualifier(account)
    trace.append("opportunity_qualifier")
    strategy = deal_strategist(qualification)
    trace.append("deal_strategist")
    brief = executive_brief_writer(strategy)
    trace.append("executive_brief_writer")
    return {{
        "executive_brief": brief,
        "handoff_trace": trace,
        "credential_ref_used": credential_ref,
        "verified_public_assertion_ids": list(PUBLIC_ASSERTION_IDS),
    }}
'''
        test = '''\
import json
import os
from pathlib import Path

from src.team import AGENTS, PUBLIC_ASSERTION_IDS, run_sales_pipeline
from src.tools.account_snapshot_reader import read_account_snapshot
from src.tools.opportunity_scoring_rules import qualify_opportunity

def test_generated_sales_pipeline_real_case():
    credential_ref = os.environ["FACTORY_CREDENTIAL_REF"]
    sales_input = {"account_id": "acct-42", "stage": "qualified", "account_name": "Acme"}
    result = run_sales_pipeline(sales_input, credential_ref=credential_ref)
    assert tuple(result["handoff_trace"]) == AGENTS
    assert result["credential_ref_used"] == credential_ref
    assert tuple(result["verified_public_assertion_ids"]) == PUBLIC_ASSERTION_IDS
    if len(AGENTS) == 1:
        assert result["executive_brief"] == "incomplete"
    else:
        assert "qualified" in result["executive_brief"]
        assert "next action" in result["executive_brief"]
    assert read_account_snapshot(sales_input, credential_ref=credential_ref)["account_id"] == "acct-42"
    assert qualify_opportunity({"stage": "qualified"}) == {"qualification": "qualified", "applied_rule": "published-stage-rule"}
    Path("evidence").mkdir(exist_ok=True)
    Path("evidence/real-case-output.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
'''
        return {
            "src/__init__.py": b'"""Generated sales pipeline package."""\n',
            "src/team.py": team.encode("utf-8"),
            "src/tools/__init__.py": b'"""Generated typed sales tools."""\n',
            "src/tools/account_snapshot_reader.py": (
                b"def read_account_snapshot(value: dict[str, object], *, credential_ref: str) -> dict[str, object]:\n"
                b"    if not credential_ref.startswith('credential://'):\n"
                b"        raise ValueError('approved account credential is required')\n"
                b"    return {key: value[key] for key in sorted(value)}\n"
            ),
            "src/tools/opportunity_scoring_rules.py": (
                b"def qualify_opportunity(value: dict[str, object]) -> dict[str, object]:\n"
                b"    qualification = 'qualified' if value.get('stage') == 'qualified' else 'not-qualified'\n"
                b"    return {'qualification': qualification, 'applied_rule': 'published-stage-rule'}\n"
            ),
            "tests/test_team.py": test.encode("utf-8"),
        }


class _ExactCaptainBuildExecutor:
    """Test implementation of the existing Captain executor port."""

    def __init__(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        creation_job: CreationJobV3,
        model: _DeterministicExternalModel,
        clock,
    ) -> None:  # type: ignore[no-untyped-def]
        self._assignment = assignment
        self._creation_job = creation_job
        self._model = model
        self._clock = clock
        self._credential_refs: tuple[str, ...] = ()
        self.executed_commands: tuple[tuple[str, ...], ...] = ()
        self.real_case_handoff_trace: tuple[str, ...] = ()
        self.verified_public_assertion_ids: tuple[str, ...] = ()
        self.tested_tools: set[str] = set()
        self.sealed_evidence = None

    @property
    def sessions(self) -> int:
        return self._model.sessions

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
        if tuple(self._assignment.allowed_commands) != _EXPECTED_COMMANDS:
            raise FactoryDispatchError("hermetic assignment command authority changed")
        workspace = self._model.generate(
            self._assignment,
            public_assertion_ids=self._creation_job.public_assertion_ids,
        )
        if workspace is None:
            raise ValueError("Hermes Factory requires a typed Codex build result")
        environment = os.environ.copy()
        environment["FACTORY_CREDENTIAL_REF"] = self._credential_refs[0]
        executed: list[tuple[str, ...]] = []
        for command in self._assignment.allowed_commands:
            canonical = tuple(command)
            if canonical not in _EXPECTED_COMMANDS:
                raise FactoryDispatchError("unauthorized hermetic command")
            subprocess.run(
                canonical,
                cwd=workspace,
                env=environment,
                check=True,
                capture_output=True,
                timeout=30,
            )
            executed.append(canonical)
        self.executed_commands = tuple(executed)
        real_case = json.loads(
            (workspace / "evidence" / "real-case-output.json").read_text(
                encoding="utf-8"
            )
        )
        trace = tuple(real_case["handoff_trace"])
        assertions = tuple(real_case["verified_public_assertion_ids"])
        if (
            real_case.get("credential_ref_used") != self._credential_refs[0]
            or trace != self._creation_job.required_agent_ids
            or assertions != self._creation_job.public_assertion_ids
        ):
            raise FactoryDispatchError("real-case execution evidence changed")
        self.real_case_handoff_trace = trace
        self.verified_public_assertion_ids = assertions
        self.tested_tools = {
            "account_snapshot_reader",
            "opportunity_scoring_rules",
        }
        test_evidence = {
            "schema": "captain.hermetic-command-evidence.v1",
            "status": "passed",
            "executed_commands": [list(item) for item in self.executed_commands],
            "handoff_trace": list(trace),
            "assertion_ids": list(assertions),
        }
        (workspace / "test-evidence.json").write_bytes(
            _canonical(test_evidence, newline=True)
        )
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


class _HermeticCaptainPackageEvidenceIssuer:
    """Exact test implementation of Captain's post-execution package authority."""

    def __init__(
        self,
        *,
        cas: CodexBuildArtifactCas,
        receipt_issuer: CaptainCodexBuildReceiptIssuer,
        creation_job: CreationJobV3,
        executor: _ExactCaptainBuildExecutor,
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
            self._executor.executed_commands != _EXPECTED_COMMANDS
            or self._executor.real_case_handoff_trace
            != self._creation_job.required_agent_ids
            or self._executor.verified_public_assertion_ids
            != self._creation_job.public_assertion_ids
            or not self._executor.build_credential_refs
        ):
            raise FactoryDispatchError(
                "Captain package evidence requires exact executed authority"
            )
        receipt = sealed.codex_build_receipt.model_copy(
            update={
                "workspace_ref": assignment.workspace_ref,
                "build_brief_ref": self._creation_job.blueprint_ref,
            }
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
            "source_archive_ref": receipt.source_archive_ref,
            "codex_session_ref": receipt.codex_session_ref,
            "completed_at": receipt.completed_at,
        }
        build_evidence = PackageBuildEvidenceV1(
            producer="captain",
            outcome="passed",
            workspace_snapshot_ref=receipt.workspace_snapshot_ref,
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
            assertion_ids=self._creation_job.public_assertion_ids,
            declared_test_paths=("tests/test_team.py",),
            evidence_refs=(build_ref,),
            **common,
        )
        real_ref = self._cas.put_bytes(
            _canonical(real_case.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="package-real-case",
        )
        receipt = receipt.model_copy(update={"test_evidence_refs": (real_ref,)})
        receipt_ref = self._receipt_issuer.persist_receipt(receipt)
        if self._receipt_issuer.read_receipt(receipt_ref) != receipt:
            raise FactoryDispatchError("Captain package receipt readback changed")
        return receipt, receipt_ref, build_ref


class CaptainAuthorizedHermeticCodexPort:
    """Compose exact test enforcement with the production mapper/sealer/CAS."""

    def __init__(
        self,
        *,
        variant: _Variant,
        root: Path,
        artifact_cas: CodexBuildArtifactCas,
        assignment: FactoryBuildAssignmentV2,
        creation_job: CreationJobV3,
        factory_job: AgentFactoryJobV3,
        lease: FactoryLease,
        released_skill: ReleasedHermesSkill,
        clock,
    ) -> None:  # type: ignore[no-untyped-def]
        self.variant = variant
        self._assignment = assignment
        self._artifact_cas = artifact_cas
        self._issuer = _HermeticCaptainReceiptIssuer(artifact_cas)
        model = _DeterministicExternalModel(
            variant=variant,
            root=root / "codex-workspaces",
        )
        self._executor = _ExactCaptainBuildExecutor(
            assignment=assignment,
            creation_job=creation_job,
            model=model,
            clock=clock,
        )
        self._package_issuer = _HermeticCaptainPackageEvidenceIssuer(
            cas=artifact_cas,
            receipt_issuer=self._issuer,
            creation_job=creation_job,
            executor=self._executor,
        )
        lease_sha256 = hashlib.sha256(canonical_factory_codex_model(lease)).hexdigest()
        mapped_assignment = assignment.model_copy(
            update={
                "workspace_ref": lease.workspace_ref,
                "lease_ref": ArtifactRef(
                    uri=f"artifact://captain/lease/{lease_sha256}",
                    sha256=lease_sha256,
                    media_type="application/json",
                ),
            }
        )
        self._mapped_assignment = mapped_assignment
        settings = CodexCliFactoryBuildSettings(
            state_root=root / ".captain-cook" / "hermetic-codex-state",
            maximum_runtime_seconds=assignment.limits.max_wall_clock_seconds,
            maximum_files=assignment.limits.max_files,
            maximum_provider_cost_usd=assignment.limits.max_provider_cost_usd,
            supported_output_roots=assignment.allowed_output_roots,
            supported_commands=assignment.allowed_commands,
            deadline_at=assignment.deadline_at,
            enforce_assignment_budgets=True,
        )
        mapper = CaptainV3CodexRequestMapper(
            job_for=lambda _job_id: factory_job,
            lease_for=lambda _job, _attempt, _now: lease,
            released_skill_for=lambda _job, step: (
                released_skill
                if step is FactorySkillStep.BRIEF_CODEX
                else released_skill.model_copy(
                    update={"skill_id": "captain-factory-seal-codex-build"}
                )
            ),
            prompt_store=_PromptStore(artifact_cas),
            artifact_cas=artifact_cas,
            worker_id="hermes-factory",
            runtime_skill_sha256=released_skill.content_sha256,
            clock=clock,
        )
        self._builder = CaptainCodexAssignmentBuilder(
            sealer=CaptainCodexBuildSealer(
                executor=self._executor,
                issuer=self._issuer,
            ),
            mapper=mapper,
            settings=settings,
        )
        self._creation_job = creation_job
        self._clock = clock

    async def build(
        self, assignment: FactoryBuildAssignmentV2
    ) -> HermesFactoryCodexBuild:
        if assignment != self._assignment:
            raise FactoryDispatchError("Hermes assignment changed before build")
        base = await self._builder.build(self._mapped_assignment)
        if self._issuer.read_receipt(base.codex_build_receipt_ref) != base.codex_build_receipt:
            raise FactoryDispatchError("Captain sealed receipt readback changed")
        receipt, receipt_ref, build_ref = self._package_issuer.issue(
            assignment=assignment,
            sealed=base,
        )
        gap_ref = self._artifact_cas.put_bytes(
            _canonical({"status": "unresolved", "kind": "optional"}),
            media_type="application/json",
            namespace="tool-gap",
        )
        hermes_usage = ForgeBuildSkillUsageReceiptV1(
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
        hermes_ref = self._artifact_cas.put_bytes(
            _canonical(hermes_usage.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="forge-hermes-usage",
        )
        build = HermesFactoryCodexBuild(
            hermes_usage_receipt_ref=hermes_ref,
            codex_build_receipt_ref=receipt_ref,
            codex_build_receipt=receipt,
            build_evidence_refs=(build_ref,),
            tool_gaps=(
                ToolGapMarkerV1(
                    schema_name="TODO_TOOL.v1",
                    gap_id="industry_signal_reader",
                    severity="optional",
                    evidence_ref=gap_ref,
                    status="unresolved",
                ),
            ),
        )
        if self.variant == "receipt_only":
            self._artifact_cas.local_path(receipt.source_archive_ref).unlink()
        return build

    def release_credential(self, credential_ref: str) -> None:
        self._executor.release_credential(credential_ref)

    @property
    def sessions(self) -> int:
        return self._executor.sessions

    @property
    def tested_tools(self) -> set[str]:
        return set(self._executor.tested_tools)

    @property
    def executed_commands(self) -> tuple[tuple[str, ...], ...]:
        return self._executor.executed_commands

    @property
    def real_case_handoff_trace(self) -> tuple[str, ...]:
        return self._executor.real_case_handoff_trace

    @property
    def verified_public_assertion_ids(self) -> tuple[str, ...]:
        return self._executor.verified_public_assertion_ids

    @property
    def build_credential_refs(self) -> tuple[str, ...]:
        return self._executor.build_credential_refs

    def read_receipt(self, reference: ArtifactRef) -> CodexBuildReceiptV1:
        return self._issuer.read_receipt(reference)


__all__ = ["CaptainAuthorizedHermeticCodexPort"]
