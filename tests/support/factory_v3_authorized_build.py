"""Test-only model generator composed behind production Captain authority."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Literal
from uuid import UUID
import zipfile

from agenten.agent_factory.codex_build_execution import (
    CaptainCodexAssignmentBuilder,
    CaptainCodexBuildSealer,
    CaptainStaticFactoryRunnerFactory,
    CaptainStaticFactorySourceBundle,
    CaptainV3CodexRequestMapper,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    FactoryCodexRuntimeAuthority,
    captain_static_factory_source_sha256,
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
)
from agenten.agent_factory.hermes_factory_worker import HermesFactoryCodexBuild
from agenten.agent_factory.hermetic_build_authority import (
    CaptainFactoryV3PackageEvidenceIssuer,
    CaptainStaticFactoryBuildObserver,
    CaptainStaticFactoryCredentialSession,
    CaptainStaticFactoryExecutionAuthorizer,
    CaptainStaticFactoryWorkspacePreparer,
)
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef


_Variant = Literal["happy", "one_agent", "null_output", "timeout", "receipt_only"]
_EXPECTED_COMMANDS = (("python", "-m", "compileall", "src"), ("python", "-m", "pytest"))
_AUTHORIZED_SOURCE_DIGESTS = {
    "four_agent": "2476885d5fa09c1b7f2c27a696ee5299c558a25b5ba57e30627eea7734f90d0a",
    "one_agent": "a55d9705d08740067e22788fcc93a5f4efc4a2e27576a28bb59c7bcb56e5c257",
}


def _canonical(value: object, *, newline: bool = False) -> bytes:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
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
                content, media_type="application/json", namespace="v3-prompt"
            ).model_dump(mode="json")
        )


class _DeterministicExternalModel:
    """The only test double: it replaces the unavailable external model."""

    def __init__(self, *, variant: _Variant) -> None:
        self._variant = variant
        self._sessions = 0

    @property
    def sessions(self) -> int:
        return self._sessions

    @property
    def provider_cost_usd(self) -> float:
        return 0.0

    def generate_bundle(
        self, assignment: FactoryBuildAssignmentV2
    ) -> CaptainStaticFactorySourceBundle:
        self._sessions += 1
        agents = assignment.required_agent_ids
        handoffs = assignment.ordered_handoffs
        if self._variant == "one_agent":
            agents, handoffs = agents[:1], ()
        manifest = {
            "schema": "minibook.team-manifest.v2",
            "agents": [{"agent_id": item} for item in agents],
            "handoffs": [item.model_dump(mode="json") for item in handoffs],
            "tools": [
                {
                    "tool_id": item,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "implementation_path": f"src/tools/{item}.py",
                    "test_path": "tests/test_team.py",
                }
                for item in ("account_snapshot_reader", "opportunity_scoring_rules")
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
        files = self._workspace_files(agents)
        team_bytes = _canonical(manifest, newline=True)
        files["team_manifest.json"] = team_bytes
        team_sha = hashlib.sha256(team_bytes).hexdigest()
        team_ref = ArtifactRef(
            uri=f"artifact://minibook-creation/team-file/{team_sha}",
            sha256=team_sha,
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
        files["candidate.zip"] = _zip_bytes(
            {
                name: content
                for name, content in files.items()
                if name.startswith(("src/", "tests/"))
                or name == "factory-candidate.json"
            }
        )
        return CaptainStaticFactorySourceBundle(tuple(sorted(files.items())))

    @staticmethod
    def _workspace_files(agents: tuple[str, ...]) -> dict[str, bytes]:
        team = f"""\
from src.tools.account_snapshot_reader import read_account_snapshot
from src.tools.opportunity_scoring_rules import qualify_opportunity
AGENTS = {agents!r}

class ExternalWriteGuard:
    def __init__(self) -> None:
        self.attempts = 0
        self.blocks = 0

    def write(self, _payload: object) -> None:
        self.attempts += 1
        self.blocks += 1
        raise PermissionError("external writes are forbidden")

def evaluate_industry_signal(*, available: bool) -> str:
    return "bounded_public_signal" if available else "typed_optional_gap"

def run_sales_pipeline(sales_input: dict[str, object], *, credential_ref: str) -> dict[str, object]:
    trace = ["account_researcher"]
    handoffs: list[dict[str, str]] = []
    account = read_account_snapshot(sales_input, credential_ref=credential_ref)
    if len(AGENTS) == 1:
        return {{"executive_brief": "incomplete", "handoff_trace": trace, "handoffs": handoffs, "credential_ref_used": credential_ref, "pipeline_status": "incomplete"}}
    handoffs.append({{"from": "account_researcher", "to": "opportunity_qualifier"}})
    qualification = qualify_opportunity(account)
    trace.append("opportunity_qualifier")
    handoffs.append({{"from": "opportunity_qualifier", "to": "deal_strategist"}})
    strategy = {{"next_action": "Schedule an evidence review", "qualification": qualification["qualification"]}}
    trace.append("deal_strategist")
    handoffs.append({{"from": "deal_strategist", "to": "executive_brief_writer"}})
    brief = f"{{strategy['qualification']}}; next action: {{strategy['next_action']}}"
    trace.append("executive_brief_writer")
    return {{"executive_brief": brief, "handoff_trace": trace, "handoffs": handoffs, "credential_ref_used": credential_ref, "pipeline_status": "passed", "qualification_preserved": qualification["qualification"] in brief, "next_action_preserved": strategy["next_action"] in brief}}
"""
        test = """\
import json
import os
from pathlib import Path
from src.team import AGENTS, ExternalWriteGuard, evaluate_industry_signal, run_sales_pipeline
from src.tools.account_snapshot_reader import read_account_snapshot
from src.tools.opportunity_scoring_rules import qualify_opportunity
def test_generated_sales_pipeline_real_case():
    credential_ref = os.environ["FACTORY_CREDENTIAL_REF"]
    sales_input = {"account_id": "acct-42", "stage": "qualified", "account_name": "Acme"}
    result = run_sales_pipeline(sales_input, credential_ref=credential_ref)
    assert tuple(result["handoff_trace"]) == AGENTS
    assert result["credential_ref_used"] == credential_ref
    if len(AGENTS) == 1:
        assert result["executive_brief"] == "incomplete"
    else:
        assert "qualified" in result["executive_brief"]
        assert "next action" in result["executive_brief"]
    assert read_account_snapshot(sales_input, credential_ref=credential_ref)["account_id"] == "acct-42"
    assert qualify_opportunity({"stage": "qualified"})["applied_rule"] == "published-stage-rule"
    available_signal = evaluate_industry_signal(available=True)
    unavailable_signal = evaluate_industry_signal(available=False)
    assert available_signal == "bounded_public_signal"
    assert unavailable_signal == "typed_optional_gap"
    guard = ExternalWriteGuard()
    try:
        guard.write({"account_id": "acct-42"})
    except PermissionError as error:
        assert str(error) == "external writes are forbidden"
    else:
        raise AssertionError("external write guard permitted a write")
    result.update({"tool_evidence": {"account_snapshot_reader": {"status": "passed", "typed_schema": True, "test_passed": True, "result": "approved_account_snapshot"}, "opportunity_scoring_rules": {"status": "passed", "typed_schema": True, "test_passed": True, "result": "versioned_published_rule"}}, "optional_tool_policy": {"available_result": available_signal, "unavailable_result": unavailable_signal}, "external_write_attempts": guard.attempts, "external_write_blocks": guard.blocks, "sentinel_secret_seen": os.environ.get("FACTORY_SENTINEL_SECRET") is not None})
    Path("evidence").mkdir(exist_ok=True)
    Path("evidence/real-case-output.json").write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
"""
        return {
            "src/__init__.py": b'"""Generated sales pipeline package."""\n',
            "src/team.py": team.encode(),
            "src/tools/__init__.py": b'"""Generated typed sales tools."""\n',
            "src/tools/account_snapshot_reader.py": b"def read_account_snapshot(value: dict[str, object], *, credential_ref: str) -> dict[str, object]:\n    if not credential_ref.startswith('credential://'):\n        raise ValueError('approved account credential is required')\n    return {key: value[key] for key in sorted(value)}\n",
            "src/tools/opportunity_scoring_rules.py": b"def qualify_opportunity(value: dict[str, object]) -> dict[str, object]:\n    qualification = 'qualified' if value.get('stage') == 'qualified' else 'not-qualified'\n    return {'qualification': qualification, 'applied_rule': 'published-stage-rule'}\n",
            "tests/test_team.py": test.encode(),
        }


class CaptainAuthorizedHermeticCodexPort:
    """Test composition: model double behind production Captain components."""

    def __init__(
        self,
        *,
        variant: _Variant,
        root: Path,
        artifact_cas: CodexBuildArtifactCas,
        artifact_reader,
        assignment: FactoryBuildAssignmentV2,
        creation_job: CreationJobV3,
        compiled_specification: CompiledFactorySpecification,
        factory_job: AgentFactoryJobV3,
        lease: FactoryLease,
        released_skill: ReleasedHermesSkill,
        clock,
    ) -> None:  # type: ignore[no-untyped-def]
        self.variant = variant
        self._sessions = 0
        self._assignment = assignment
        self._artifact_cas = artifact_cas
        self._issuer = CaptainCodexBuildReceiptIssuer(artifact_cas)
        self._model = _DeterministicExternalModel(variant=variant)
        bundle = self._model.generate_bundle(assignment)
        self.source_bundle_sha256 = captain_static_factory_source_sha256(bundle)
        expected_source_digest = _AUTHORIZED_SOURCE_DIGESTS[
            "one_agent" if variant == "one_agent" else "four_agent"
        ]
        workspace = (root / "codex-workspaces" / assignment.assignment_id.hex).resolve()
        credential_session = CaptainStaticFactoryCredentialSession()
        self._credential_session = credential_session
        self._observer = CaptainStaticFactoryBuildObserver(
            assignment=assignment,
            compiled_specification=compiled_specification,
            workspace_root=workspace,
            credential_session=credential_session,
        )
        self._package_issuer = CaptainFactoryV3PackageEvidenceIssuer(
            cas=artifact_cas,
            receipt_issuer=self._issuer,
            creation_job=creation_job,
            observer=self._observer,
        )
        lease_sha = hashlib.sha256(canonical_factory_codex_model(lease)).hexdigest()
        self._mapped_assignment = assignment.model_copy(
            update={
                "workspace_ref": lease.workspace_ref,
                "lease_ref": ArtifactRef(
                    uri=f"artifact://captain/lease/{lease_sha}",
                    sha256=lease_sha,
                    media_type="application/json",
                ),
            }
        )
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
        runtime_authority = FactoryCodexRuntimeAuthority(
            workspace_root=workspace,
            writable_roots=tuple(
                (workspace / item).resolve()
                for item in assignment.allowed_output_roots
            ),
            allowed_commands=assignment.allowed_commands,
            network_allowed=False,
            maximum_provider_cost_usd=assignment.limits.max_provider_cost_usd,
        )
        executor = CodexCliFactoryBuildExecutor(
            settings=settings,
            workspace_preparer=CaptainStaticFactoryWorkspacePreparer(workspace),
            artifact_reader=artifact_reader,
            authorizer=CaptainStaticFactoryExecutionAuthorizer(
                workspace_root=workspace,
                credential_session=credential_session,
                host_environment=os.environ,
            ),
            runner_factory=CaptainStaticFactoryRunnerFactory(
                authority=runtime_authority,
                bundle=bundle,
                expected_bundle_sha256=expected_source_digest,
                interpreter_path=Path(sys.executable),
                mode=(
                    "null_output"
                    if variant == "null_output"
                    else "timeout"
                    if variant == "timeout"
                    else "execute"
                ),
            ),
            clock=clock,
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
                executor=executor, issuer=self._issuer
            ),
            mapper=mapper,
            settings=settings,
        )

    async def build(
        self, assignment: FactoryBuildAssignmentV2
    ) -> HermesFactoryCodexBuild:
        if assignment != self._assignment:
            raise FactoryDispatchError("Hermes assignment changed before build")
        self._sessions += 1
        base = await self._builder.build(self._mapped_assignment)
        self._observer.observe()
        receipt, receipt_ref, build_ref = self._package_issuer.issue(
            assignment=assignment, sealed=base
        )
        build = HermesFactoryCodexBuild(
            hermes_usage_receipt_ref=self._package_issuer.issue_hermes_usage(),
            codex_build_receipt_ref=receipt_ref,
            codex_build_receipt=receipt,
            build_evidence_refs=(build_ref,),
            tool_gaps=(
                self._package_issuer.issue_optional_gap("industry_signal_reader"),
            ),
        )
        if self.variant == "receipt_only":
            self._artifact_cas.local_path(receipt.source_archive_ref).unlink()
        return build

    def release_credential(self, credential_ref: str) -> None:
        self._credential_session.release(credential_ref)

    def read_receipt(self, reference: ArtifactRef) -> CodexBuildReceiptV1:
        return self._issuer.read_receipt(reference)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        if name == "sessions":
            return self._sessions
        return getattr(self._observer, name)


__all__ = ["CaptainAuthorizedHermeticCodexPort"]
