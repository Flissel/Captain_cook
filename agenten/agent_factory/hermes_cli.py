"""Concrete non-interactive Hermes CLI adapter for Captain factory roles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ValidationError

from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FactoryEvidenceStore, FilesystemFactoryEvidenceStore
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, HermesFactoryPort
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
    ReleasedHermesSkill,
)
from agenten.agent_factory.skill_sequence import (
    FactoryImprovementAuthorizationV1,
    SkillSequencePolicy,
)
from agenten.agent_factory.skill_store import reject_sensitive_data
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactoryFeedbackRecommendation,
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef

if TYPE_CHECKING:
    from agenten.agent_factory.candidate_evaluation import FactoryCandidateEvaluationResult


@dataclass(frozen=True)
class HermesCliSettings:
    executable: str = "hermes"
    skill_root: Path = Path("agenten/agent_factory/skills")
    timeout_seconds: int = 900
    evidence_root: Path = Path("artifacts/agent-factory/evidence")
    released_skill_root: Path = Path("agenten/agent_factory/released-skills")
    module_root: Path | None = None
    provider: str | None = None
    model: str | None = None
    maximum_total_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if (self.provider is None) != (self.model is None):
            raise ValueError("Hermes provider and model must be configured together")
        for value, label in ((self.provider, "provider"), (self.model, "model")):
            if value is not None and (
                not value.strip()
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) is None
            ):
                raise ValueError(f"Hermes {label} is invalid")
        maximum = self.maximum_total_cost_usd
        if maximum is not None and (
            isinstance(maximum, (bool, float))
            or not isinstance(maximum, Decimal)
            or not maximum.is_finite()
            or maximum <= 0
            or self.provider is None
        ):
            raise ValueError(
                "Hermes cost ceiling requires a positive Decimal and pinned model"
            )


class ReleasedFactorySkillCatalog(Protocol):
    """Captain-owned lookup for one released skill at one workflow step."""

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class CaptainCodexBuildSealerPort(Protocol):
    """Captain-only bridge from an approved brief to sealed build evidence."""

    async def seal(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CodexBuildEvidenceV1: ...


class FilesystemReleasedFactorySkillCatalog:
    """Load Captain release envelopes from an exact job/step catalog path."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill:
        root = self._root.resolve()
        path = (root / str(job.job_id) / f"{step.value}.json").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FactoryDispatchError(
                "released factory skill catalog path is outside its root"
            ) from exc
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return ReleasedHermesSkill.model_validate(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FactoryDispatchError(
                f"released factory skill metadata is unavailable for {step.value}"
            ) from exc


class HermesCliFactory(HermesFactoryPort):
    """Run one hermetic Hermes query and accept only a typed evidence response."""

    def __init__(
        self,
        settings: HermesCliSettings = HermesCliSettings(),
        evidence_store: FactoryEvidenceStore | None = None,
        released_skill_catalog: ReleasedFactorySkillCatalog | None = None,
        sequence_policy: SkillSequencePolicy | None = None,
        replay_store: FactorySkillReplayStore | None = None,
        codex_build_sealer: CaptainCodexBuildSealerPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._evidence_store = evidence_store or FilesystemFactoryEvidenceStore(settings.evidence_root)
        self._released_skill_catalog = released_skill_catalog
        self._sequence_policy = sequence_policy or SkillSequencePolicy()
        self._codex_build_sealer = codex_build_sealer
        self._replay_store = (
            replay_store
            if replay_store is not None
            else FilesystemFactorySkillReplayStore(
                settings.evidence_root / "skill-replays"
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observed_cost_usd = Decimal("0")

    @property
    def observed_cost_usd(self) -> Decimal:
        return self._observed_cost_usd

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        if request.role is None or request.lease is None:
            raise FactoryDispatchError("Hermes factory dispatch requires a role and active lease")
        if self._released_skill_catalog is None:
            raise FactoryDispatchError("released factory skill catalog is not configured")
        now = self._clock()
        _validate_factory_dispatch(request, now=now)
        steps = self._sequence_policy.steps_for(
            role=request.role,
            attempt=request.action.attempt,
        )
        improvement = _validated_improvement_authorization(request)

        deadline = _deadline(
            min(
                float(self._settings.timeout_seconds),
                (request.lease.expires_at - now).total_seconds(),
            )
        )
        input_ref = (
            improvement.authorization_ref
            if improvement is not None
            else request.job.input_ref
        )
        artifacts: list[_FactoryWorkflowArtifact] = []
        transcript_refs: list[ArtifactRef] = []
        for step in steps:
            released_skill = self._released_skill_catalog.released_for(request.job, step)
            skill_name = FACTORY_SKILL_ID_BY_STEP[step]
            _require_released_skill_directory(
                self._settings.skill_root,
                skill_name=skill_name,
                released_skill=released_skill,
                now=now,
            )
            invocation = _factory_invocation(
                request,
                step=step,
                released_skill=released_skill,
                input_ref=input_ref,
            )
            _validate_serialized_prompt_value(
                invocation.model_dump(mode="json", by_alias=True)
            )
            claim = await self._replay_store.claim(invocation)
            if not claim.acquired:
                accepted = claim.record
                assert accepted.artifact is not None
                assert accepted.transcript_ref is not None
                artifacts.append(accepted.artifact)
                transcript_refs.append(accepted.transcript_ref)
                input_ref = accepted.artifact.artifact_ref
                if not _may_continue_after(accepted.artifact):
                    break
                continue
            try:
                if step is FactorySkillStep.SEAL_CODEX_BUILD:
                    if self._codex_build_sealer is None:
                        raise FactoryDispatchError(
                            "Captain Codex build sealer is not configured"
                        )
                    if not artifacts or not isinstance(
                        artifacts[-1], CodexBuildBriefV1
                    ):
                        raise FactoryDispatchError(
                            "Codex build sealing requires the exact preceding brief"
                        )
                    artifact = await self._codex_build_sealer.seal(
                        request,
                        invocation,
                        artifacts[-1],
                    )
                else:
                    stdout = await self._run_skill_prompt(
                        _factory_skill_prompt(invocation, skill_name=skill_name),
                        max_seconds=_remaining_deadline_seconds(deadline),
                    )
                    artifact = _parse_workflow_artifact(stdout, step=step)
                if artifact.invocation != invocation:
                    raise FactoryDispatchError(
                        f"Hermes {step.value} artifact does not match the Captain invocation"
                    )
                if improvement is not None:
                    _require_improvement_artifact_binding(
                        artifact,
                        authorization=improvement,
                    )
                transcript_ref = await self._evidence_store.persist(
                    request.job,
                    artifact.model_dump_json(by_alias=True).encode("utf-8"),
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._replay_store.fail(
                        claim.record,
                        failure_kind="cancelled",
                    )
                )
                raise
            except Exception as exc:
                try:
                    await self._replay_store.fail(
                        claim.record,
                        failure_kind=type(exc).__name__,
                    )
                except Exception as replay_exc:
                    raise FactoryDispatchError(
                        "factory skill failure state could not be persisted"
                    ) from replay_exc
                raise
            accepted = await self._replay_store.complete(
                claim.record,
                artifact=artifact,
                transcript_ref=transcript_ref,
            )
            assert accepted.artifact is not None
            assert accepted.transcript_ref is not None
            transcript_refs.append(accepted.transcript_ref)
            artifact = accepted.artifact
            artifacts.append(artifact)
            input_ref = artifact.artifact_ref
            if not _may_continue_after(artifact):
                break
        return _factory_block_for(
            request,
            artifacts=tuple(artifacts),
            transcript_refs=tuple(transcript_refs),
        )

    def validate_dispatch_configuration(self, request: FactoryDispatch) -> None:
        """Fail before external setup when a released sequence cannot be resolved."""

        if request.role is None or request.lease is None:
            raise FactoryDispatchError(
                "Hermes factory dispatch requires a role and active lease"
            )
        if self._released_skill_catalog is None:
            raise FactoryDispatchError("released factory skill catalog is not configured")
        now = self._clock()
        _validate_factory_dispatch(request, now=now)
        _validated_improvement_authorization(request)
        if (
            request.role is FactoryRole.TOOL_INTEGRATOR
            and self._codex_build_sealer is None
        ):
            raise FactoryDispatchError("Captain Codex build sealer is not configured")
        for step in self._sequence_policy.steps_for(
            role=request.role,
            attempt=request.action.attempt,
        ):
            released_skill = self._released_skill_catalog.released_for(
                request.job,
                step,
            )
            _require_released_skill_directory(
                self._settings.skill_root,
                skill_name=FACTORY_SKILL_ID_BY_STEP[step],
                released_skill=released_skill,
                now=now,
            )

    async def evaluate_skill(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        receipt: HermesSkillUsageReceipt,
        candidate_result: "FactoryCandidateEvaluationResult",
        candidate_id: str,
        candidate_source_ref: ArtifactRef,
        max_seconds: float,
    ) -> HermesSkillEvaluationEvidence:
        """Request a final proposal only after Captain has sealed build/test results."""

        deadline = _deadline(max_seconds)
        _validate_skill_prompt_request(request)
        _validate_serialized_prompt_value(
            receipt.model_dump(mode="json", by_alias=True)
        )
        _validate_serialized_prompt_value(candidate_id)
        _validate_serialized_prompt_value(candidate_source_ref.model_dump(mode="json"))
        try:
            _require_matching_receipt(request, receipt)
        except ValueError as exc:
            raise FactoryDispatchError("staged skill usage receipt does not match the request") from exc
        if candidate_source_ref != request.candidate_source_ref:
            raise FactoryDispatchError("sealed candidate source does not match the Captain request")
        skill_path = _resolve_released_skill(self._settings.released_skill_root, request)
        _remaining_deadline_seconds(deadline)
        prompt = _skill_evaluation_prompt_for(
            request,
            receipt,
            candidate_result,
            candidate_id,
            candidate_source_ref,
            skill_path,
        )
        stdout = await self._run_skill_prompt(
            prompt,
            max_seconds=_remaining_deadline_seconds(deadline),
        )
        try:
            evidence = HermesSkillEvaluationEvidence.model_validate(_parse_evidence_payload(stdout))
            _remaining_deadline_seconds(deadline)
        except (TypeError, ValueError) as exc:
            raise FactoryDispatchError(
                "Hermes must return exactly one typed skill evaluation JSON object"
            ) from exc
        if evidence.request != request or evidence.receipt != receipt:
            raise FactoryDispatchError("Hermes evaluation does not match the staged request and receipt")
        return evidence

    async def issue_skill_usage(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        max_seconds: float,
    ) -> HermesSkillUsageReceipt:
        """Obtain only the digest-matching usage receipt before any candidate work."""

        deadline = _deadline(max_seconds)
        _validate_skill_prompt_request(request)
        skill_path = _resolve_released_skill(self._settings.released_skill_root, request)
        _remaining_deadline_seconds(deadline)
        stdout = await self._run_skill_prompt(
            _skill_usage_prompt_for(request, skill_path),
            max_seconds=_remaining_deadline_seconds(deadline),
        )
        try:
            receipt = HermesSkillUsageReceipt.model_validate(_parse_evidence_payload(stdout))
            _remaining_deadline_seconds(deadline)
            _require_matching_receipt(request, receipt)
        except (TypeError, ValueError) as exc:
            raise FactoryDispatchError(
                "Hermes must return exactly one typed skill usage receipt JSON object"
            ) from exc
        return receipt

    async def _run_skill_prompt(self, prompt: str, *, max_seconds: float) -> bytes:
        deadline = _deadline(min(float(self._settings.timeout_seconds), max_seconds))
        maximum_cost = self._settings.maximum_total_cost_usd
        if maximum_cost is not None and self._observed_cost_usd >= maximum_cost:
            raise FactoryDispatchError("Hermes cost ceiling is already exhausted")
        command_prefix = [self._settings.executable]
        process_options: dict[str, object] = _async_process_group_options()
        if self._settings.module_root is not None:
            module_root = _resolve_hermes_module_root(self._settings.module_root)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(module_root)
            command_prefix.extend(("-m", "hermes_cli.main"))
            process_options.update(
                cwd=str(module_root),
                env=environment,
            )
        if self._settings.provider is not None:
            assert self._settings.model is not None
            command_prefix.extend(
                ("--provider", self._settings.provider, "-m", self._settings.model)
            )
        usage_directory: tempfile.TemporaryDirectory[str] | None = None
        usage_path: Path | None = None
        if maximum_cost is not None:
            usage_directory = tempfile.TemporaryDirectory(
                prefix="captain-hermes-usage-"
            )
            usage_path = Path(usage_directory.name) / "usage.json"
            command_prefix.extend(("--usage-file", str(usage_path)))
        command = (*command_prefix, "-z", prompt)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
        except FileNotFoundError as exc:
            if usage_directory is not None:
                usage_directory.cleanup()
            raise FactoryDispatchError("Hermes CLI executable is not available") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_remaining_deadline_seconds(deadline),
            )
        except TimeoutError as exc:
            await _terminate_async_process_tree(
                process,
                executable=self._settings.executable,
            )
            if usage_directory is not None:
                usage_directory.cleanup()
            raise FactoryDispatchError("Hermes skill evaluation timed out") from exc
        except asyncio.CancelledError:
            await asyncio.shield(
                _terminate_async_process_tree(
                    process,
                    executable=self._settings.executable,
                )
            )
            if usage_directory is not None:
                usage_directory.cleanup()
            raise
        _remaining_deadline_seconds(deadline)
        try:
            if usage_path is not None:
                self._account_usage(usage_path)
        finally:
            if usage_directory is not None:
                usage_directory.cleanup()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FactoryDispatchError(f"Hermes skill evaluation failed: {detail[:500]}")
        return stdout

    def _account_usage(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
            if not isinstance(raw, dict):
                raise ValueError
            cost = Decimal(str(raw["estimated_cost_usd"]))
            api_calls = raw["api_calls"]
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
            json.JSONDecodeError,
        ) as exc:
            raise FactoryDispatchError(
                "Hermes usage evidence is missing or invalid"
            ) from exc
        if (
            not cost.is_finite()
            or cost < 0
            or isinstance(api_calls, bool)
            or not isinstance(api_calls, int)
            or api_calls < 1
            or raw.get("model") != self._settings.model
            or raw.get("provider") != self._settings.provider
        ):
            raise FactoryDispatchError("Hermes usage evidence does not match its pin")
        self._observed_cost_usd += cost
        maximum = self._settings.maximum_total_cost_usd
        assert maximum is not None
        if self._observed_cost_usd > maximum:
            raise FactoryDispatchError("Hermes cost ceiling was exceeded")


_FactoryWorkflowArtifact = (
    CodebaseInventoryV1
    | CodexBuildBriefV1
    | CodexBuildEvidenceV1
    | TeamExecutionEvidenceV1
    | TeamEvaluationV1
    | CandidateRevisionV1
    | FactoryFeedbackV1
)


class FactorySkillReplayPendingError(FactoryDispatchError):
    """A prior claimant may have executed the effect and requires recovery."""

    def __init__(self, record: "FactorySkillReplayRecord") -> None:
        super().__init__("factory skill replay is pending and requires recovery")
        self.record = record


@dataclass(frozen=True)
class FactorySkillReplayRecord:
    invocation: FactorySkillInvocationV1
    invocation_sha256: str
    claim_token: str
    state: Literal["pending", "completed", "failed"]
    artifact: _FactoryWorkflowArtifact | None = None
    transcript_ref: ArtifactRef | None = None
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        if self.invocation_sha256 != _factory_invocation_digest(self.invocation):
            raise FactoryDispatchError("factory skill replay invocation digest conflicts")
        if not self.claim_token:
            raise FactoryDispatchError("factory skill replay claim token is missing")
        if self.state == "pending" and any(
            item is not None
            for item in (self.artifact, self.transcript_ref, self.failure_kind)
        ):
            raise FactoryDispatchError("pending factory skill replay contains an outcome")
        if self.state == "completed" and (
            self.artifact is None
            or self.transcript_ref is None
            or self.failure_kind is not None
        ):
            raise FactoryDispatchError("completed factory skill replay is incomplete")
        if self.state == "failed" and (
            self.artifact is not None
            or self.transcript_ref is not None
            or self.failure_kind is None
        ):
            raise FactoryDispatchError("failed factory skill replay is incomplete")
        if self.artifact is not None and self.artifact.invocation != self.invocation:
            raise FactoryDispatchError(
                "factory skill replay artifact conflicts with its invocation"
            )


@dataclass(frozen=True)
class FactorySkillReplayClaim:
    record: FactorySkillReplayRecord
    acquired: bool


class FactorySkillReplayStore(Protocol):
    async def claim(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactorySkillReplayClaim: ...

    async def complete(
        self,
        pending: FactorySkillReplayRecord,
        *,
        artifact: _FactoryWorkflowArtifact,
        transcript_ref: ArtifactRef,
    ) -> FactorySkillReplayRecord: ...

    async def fail(
        self,
        pending: FactorySkillReplayRecord,
        *,
        failure_kind: str,
    ) -> FactorySkillReplayRecord: ...

    async def abandon(self, pending: FactorySkillReplayRecord) -> None: ...


class InMemoryFactorySkillReplayStore:
    """Process-local replay store for explicitly injected deterministic tests."""

    def __init__(self) -> None:
        self._records: dict[str, FactorySkillReplayRecord] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactorySkillReplayClaim:
        async with self._lock:
            existing = self._records.get(invocation.idempotency_key)
            if existing is not None:
                return _existing_replay_claim(existing, invocation)
            pending = _pending_replay_record(invocation)
            self._records[invocation.idempotency_key] = pending
            return FactorySkillReplayClaim(record=pending, acquired=True)

    async def complete(
        self,
        pending: FactorySkillReplayRecord,
        *,
        artifact: _FactoryWorkflowArtifact,
        transcript_ref: ArtifactRef,
    ) -> FactorySkillReplayRecord:
        completed = _completed_replay_record(
            pending,
            artifact=artifact,
            transcript_ref=transcript_ref,
        )
        return await self._transition(pending, completed)

    async def fail(
        self,
        pending: FactorySkillReplayRecord,
        *,
        failure_kind: str,
    ) -> FactorySkillReplayRecord:
        return await self._transition(
            pending,
            _failed_replay_record(pending, failure_kind=failure_kind),
        )

    async def abandon(self, pending: FactorySkillReplayRecord) -> None:
        async with self._lock:
            existing = self._records.get(pending.invocation.idempotency_key)
            if existing != pending or existing.state != "pending":
                raise FactoryDispatchError("factory skill replay claim is no longer pending")
            del self._records[pending.invocation.idempotency_key]

    async def _transition(
        self,
        pending: FactorySkillReplayRecord,
        outcome: FactorySkillReplayRecord,
    ) -> FactorySkillReplayRecord:
        async with self._lock:
            existing = self._records.get(pending.invocation.idempotency_key)
            if existing != pending or existing.state != "pending":
                raise FactoryDispatchError("factory skill replay claim is no longer pending")
            self._records[pending.invocation.idempotency_key] = outcome
            return outcome


class FilesystemFactorySkillReplayStore:
    """Durable state machine with an atomic claim before each external effect."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def claim(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactorySkillReplayClaim:
        path = self._path_for(invocation.idempotency_key)
        pending = _pending_replay_record(invocation)
        acquired = await asyncio.to_thread(
            self._create_exclusive,
            path,
            _factory_skill_replay_content(pending),
        )
        if acquired:
            return FactorySkillReplayClaim(record=pending, acquired=True)
        existing = await asyncio.to_thread(self._read_record, path)
        return _existing_replay_claim(existing, invocation)

    async def complete(
        self,
        pending: FactorySkillReplayRecord,
        *,
        artifact: _FactoryWorkflowArtifact,
        transcript_ref: ArtifactRef,
    ) -> FactorySkillReplayRecord:
        completed = _completed_replay_record(
            pending,
            artifact=artifact,
            transcript_ref=transcript_ref,
        )
        return await self._transition(pending, completed)

    async def fail(
        self,
        pending: FactorySkillReplayRecord,
        *,
        failure_kind: str,
    ) -> FactorySkillReplayRecord:
        return await self._transition(
            pending,
            _failed_replay_record(pending, failure_kind=failure_kind),
        )

    async def abandon(self, pending: FactorySkillReplayRecord) -> None:
        path = self._path_for(pending.invocation.idempotency_key)
        await asyncio.to_thread(self._remove_pending, path, pending)

    async def _transition(
        self,
        pending: FactorySkillReplayRecord,
        outcome: FactorySkillReplayRecord,
    ) -> FactorySkillReplayRecord:
        path = self._path_for(pending.invocation.idempotency_key)
        await asyncio.to_thread(self._replace_pending, path, pending, outcome)
        return outcome

    def _path_for(self, idempotency_key: str) -> Path:
        return self._root / f"{idempotency_key}.json"

    @staticmethod
    def _create_exclusive(path: Path, content: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _replace_pending(
        cls,
        path: Path,
        pending: FactorySkillReplayRecord,
        outcome: FactorySkillReplayRecord,
    ) -> None:
        if cls._read_record(path) != pending:
            raise FactoryDispatchError("factory skill replay claim is no longer pending")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_factory_skill_replay_content(outcome))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _remove_pending(
        cls,
        path: Path,
        pending: FactorySkillReplayRecord,
    ) -> None:
        if cls._read_record(path) != pending:
            raise FactoryDispatchError("factory skill replay claim is no longer pending")
        path.unlink()

    @staticmethod
    def _read_record(path: Path) -> FactorySkillReplayRecord:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("replay record must be an object")
            invocation = FactorySkillInvocationV1.model_validate(value["invocation"])
            state = value["state"]
            artifact = None
            transcript_ref = None
            if state == "completed":
                model = _STEP_RESULT_MODELS[invocation.step]
                artifact = model.model_validate(value["artifact"])
                transcript_ref = ArtifactRef.model_validate(value["transcript_ref"])
            return FactorySkillReplayRecord(
                invocation=invocation,
                invocation_sha256=value["invocation_sha256"],
                claim_token=value["claim_token"],
                state=state,
                artifact=artifact,
                transcript_ref=transcript_ref,
                failure_kind=value.get("failure_kind"),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise FactoryDispatchError("factory skill replay record is invalid") from exc


def _factory_skill_replay_content(record: FactorySkillReplayRecord) -> bytes:
    return _canonical_json(
        {
            "schema": "captain.factory-skill-replay.v2",
            "state": record.state,
            "invocation_sha256": record.invocation_sha256,
            "claim_token": record.claim_token,
            "invocation": record.invocation.model_dump(mode="json", by_alias=True),
            "artifact": (
                None
                if record.artifact is None
                else record.artifact.model_dump(mode="json", by_alias=True)
            ),
            "transcript_ref": (
                None
                if record.transcript_ref is None
                else record.transcript_ref.model_dump(mode="json")
            ),
            "failure_kind": record.failure_kind,
        }
    ).encode("utf-8")


def _factory_invocation_digest(invocation: FactorySkillInvocationV1) -> str:
    content = _canonical_json(invocation.model_dump(mode="json", by_alias=True))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pending_replay_record(
    invocation: FactorySkillInvocationV1,
) -> FactorySkillReplayRecord:
    return FactorySkillReplayRecord(
        invocation=invocation,
        invocation_sha256=_factory_invocation_digest(invocation),
        claim_token=uuid4().hex,
        state="pending",
    )


def _completed_replay_record(
    pending: FactorySkillReplayRecord,
    *,
    artifact: _FactoryWorkflowArtifact,
    transcript_ref: ArtifactRef,
) -> FactorySkillReplayRecord:
    if pending.state != "pending":
        raise FactoryDispatchError("factory skill replay claim is no longer pending")
    return FactorySkillReplayRecord(
        invocation=pending.invocation,
        invocation_sha256=pending.invocation_sha256,
        claim_token=pending.claim_token,
        state="completed",
        artifact=artifact,
        transcript_ref=transcript_ref,
    )


def _failed_replay_record(
    pending: FactorySkillReplayRecord,
    *,
    failure_kind: str,
) -> FactorySkillReplayRecord:
    if pending.state != "pending":
        raise FactoryDispatchError("factory skill replay claim is no longer pending")
    if not failure_kind or len(failure_kind) > 100:
        raise FactoryDispatchError("factory skill replay failure kind is invalid")
    return FactorySkillReplayRecord(
        invocation=pending.invocation,
        invocation_sha256=pending.invocation_sha256,
        claim_token=pending.claim_token,
        state="failed",
        failure_kind=failure_kind,
    )


def _existing_replay_claim(
    existing: FactorySkillReplayRecord,
    invocation: FactorySkillInvocationV1,
) -> FactorySkillReplayClaim:
    if (
        existing.invocation_sha256 != _factory_invocation_digest(invocation)
        or existing.invocation != invocation
    ):
        raise FactoryDispatchError("factory skill replay invocation conflicts")
    if existing.state == "pending":
        raise FactorySkillReplayPendingError(existing)
    if existing.state == "failed":
        raise FactoryDispatchError("factory skill replay previously failed")
    return FactorySkillReplayClaim(record=existing, acquired=False)


_STEP_RESULT_MODELS: dict[FactorySkillStep, type[BaseModel]] = {
    FactorySkillStep.DISCOVER: CodebaseInventoryV1,
    FactorySkillStep.BRIEF_CODEX: CodexBuildBriefV1,
    FactorySkillStep.SEAL_CODEX_BUILD: CodexBuildEvidenceV1,
    FactorySkillStep.EXECUTE_TEAM: TeamExecutionEvidenceV1,
    FactorySkillStep.EVALUATE_TEAM: TeamEvaluationV1,
    FactorySkillStep.IMPROVE_TEAM: CandidateRevisionV1,
    FactorySkillStep.REPORT_CAPTAIN: FactoryFeedbackV1,
}


def _validate_factory_dispatch(request: FactoryDispatch, *, now: datetime) -> None:
    assert request.role is not None
    assert request.lease is not None
    lease = request.lease
    expected_action = {
        FactoryRole.AGENT_ARCHITECT: FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
        FactoryRole.TOOL_INTEGRATOR: FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        FactoryRole.REAL_CASE_TESTER: FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        FactoryRole.QUALITY_WARDEN: FactoryActionKind.DISPATCH_QUALITY_WARDEN,
    }[request.role]
    if request.action.kind is not expected_action:
        raise FactoryDispatchError("factory action does not match the leased role")
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise FactoryDispatchError("Hermes factory dispatch requires a UTC clock")
    if request.action.job_id not in {None, request.job.job_id}:
        raise FactoryDispatchError("factory action belongs to a different job")
    if (
        lease.job_id != request.job.job_id
        or lease.correlation_id != request.job.correlation_id
        or lease.subject_version != request.job.subject_version
        or lease.attempt != request.action.attempt
        or lease.role is not request.role
    ):
        raise FactoryDispatchError("Hermes factory dispatch lease is stale or mismatched")
    if lease.issued_at > now or lease.expires_at <= now:
        raise FactoryDispatchError("Hermes factory dispatch requires an active lease")


def _validated_improvement_authorization(
    request: FactoryDispatch,
) -> FactoryImprovementAuthorizationV1 | None:
    authorization = request.improvement_authorization
    is_retry = (
        request.role is FactoryRole.TOOL_INTEGRATOR
        and request.action.attempt > 1
    )
    if is_retry and authorization is None:
        raise FactoryDispatchError(
            "improve_team requires Captain IMPROVEMENT_REQUESTED authority"
        )
    if not is_retry and authorization is not None:
        raise FactoryDispatchError("improvement authority is invalid outside a retry")
    if authorization is None:
        return None
    request_block = authorization.request_block
    if (
        authorization.authorized_attempt != request.action.attempt
        or request_block.job_id != request.job.job_id
        or request_block.correlation_id != request.job.correlation_id
        or request_block.subject_version != request.job.subject_version
    ):
        raise FactoryDispatchError("improvement authority does not match the factory dispatch")
    return authorization


def _require_improvement_artifact_binding(
    artifact: _FactoryWorkflowArtifact,
    *,
    authorization: FactoryImprovementAuthorizationV1,
) -> None:
    if isinstance(artifact, CandidateRevisionV1):
        failed_ids = tuple(
            outcome.assertion_id
            for outcome in authorization.failed_evaluation.assertion_outcomes
            if outcome.status == "failed"
        )
        if (
            artifact.parent_candidate_ref != authorization.prior_candidate_ref
            or artifact.failed_assertion_ids != failed_ids
            or artifact.failed_benchmark_metric_ids
            != authorization.failed_evaluation.failed_benchmark_metric_ids
            or artifact.regression_assertion_ids
            != authorization.prior_green_assertion_ids
            or artifact.regression_benchmark_metric_ids
            != authorization.prior_green_benchmark_metric_ids
        ):
            raise FactoryDispatchError(
                "improve_team artifact does not bind the authorized failed candidate"
            )
    if isinstance(artifact, CodexBuildBriefV1):
        if (
            artifact.failed_benchmark_metric_ids
            != authorization.failed_evaluation.failed_benchmark_metric_ids
            or artifact.regression_benchmark_metric_ids
            != authorization.prior_green_benchmark_metric_ids
        ):
            raise FactoryDispatchError(
                "Codex brief benchmark guards do not match improvement authorization"
            )
        required_refs = {
            authorization.authorization_ref,
            authorization.failed_evaluation.artifact_ref,
            authorization.prior_candidate_ref,
        }
        if not required_refs.issubset(set(artifact.context_refs)):
            raise FactoryDispatchError(
                "Codex brief does not bind the authorized improvement evidence"
            )


def _require_released_skill_directory(
    skill_root: Path,
    *,
    skill_name: str,
    released_skill: ReleasedHermesSkill,
    now: datetime,
) -> None:
    if released_skill.skill_id != skill_name:
        raise FactoryDispatchError("Captain released the wrong skill for the requested step")
    expected_ref = f"artifact://released-skills/{skill_name}/v{released_skill.version}"
    if (
        released_skill.content_ref.uri != expected_ref
        or released_skill.content_ref.media_type != "application/json"
    ):
        raise FactoryDispatchError(
            "released factory skill metadata does not match its directory and version"
        )
    if released_skill.released_at > now:
        raise FactoryDispatchError("Captain skill release is stale for this dispatch")
    root = skill_root.resolve()
    directory = (root / skill_name).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise FactoryDispatchError("factory skill directory is outside the configured root") from exc
    if not directory.is_dir() or not (directory / "SKILL.md").is_file():
        raise FactoryDispatchError("released factory skill directory is missing")
    digest = _skill_directory_digest(directory)
    if digest != released_skill.content_sha256:
        raise FactoryDispatchError("released factory skill digest does not match Captain's release")


def _skill_directory_digest(directory: Path) -> str:
    manifest: list[dict[str, object]] = []
    entries = sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    if any(item.is_symlink() for item in entries):
        raise FactoryDispatchError("released factory skill cannot contain symlinks")
    files = sorted(
        (item for item in entries if item.is_file()),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    for path in files:
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    encoded = _canonical_json(manifest).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _factory_invocation(
    request: FactoryDispatch,
    *,
    step: FactorySkillStep,
    released_skill: ReleasedHermesSkill,
    input_ref: ArtifactRef,
) -> FactorySkillInvocationV1:
    assert request.lease is not None
    if input_ref.uri.startswith("holdout://"):
        raise FactoryDispatchError("private holdout references cannot enter Hermes prompts")
    binding = _canonical_json(
        {
            "job_id": str(request.job.job_id),
            "correlation_id": str(request.job.correlation_id),
            "subject_version": request.job.subject_version,
            "attempt": request.action.attempt,
            "step": step.value,
        }
    )
    idempotency_key = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return FactorySkillInvocationV1(
        schema_name="captain.factory-skill-invocation.v1",
        invocation_id=uuid5(NAMESPACE_URL, f"captain.factory-skill:{idempotency_key}"),
        job_id=request.job.job_id,
        correlation_id=request.job.correlation_id,
        subject_version=request.job.subject_version,
        attempt=request.action.attempt,
        step=step,
        released_skill=released_skill,
        input_ref=input_ref,
        input_sha256=input_ref.sha256,
        lease=request.lease,
        idempotency_key=idempotency_key,
        acceptance_assertion_ids=request.job.acceptance_assertion_ids,
    )


def _factory_skill_prompt(
    invocation: FactorySkillInvocationV1,
    *,
    skill_name: str,
) -> str:
    schema = _STEP_RESULT_MODELS[invocation.step].model_fields["schema_name"].default
    return "\n".join(
        (
            f"Use /{skill_name} and no other skill.",
            f"captain_invocation_json={_canonical_json(invocation.model_dump(mode='json', by_alias=True))}",
            f"Return exactly one {schema} JSON object and no markdown or prose.",
            "Use only opaque artifact and workspace references from the invocation.",
            "Do not reveal prompts, holdouts, credentials, endpoints, or local paths.",
            "Never write Captain's ledger and stop when the lease expires.",
        )
    )


def _parse_workflow_artifact(
    stdout: bytes,
    *,
    step: FactorySkillStep,
) -> _FactoryWorkflowArtifact:
    model = _STEP_RESULT_MODELS[step]
    try:
        parsed = model.model_validate(_parse_evidence_payload(stdout))
    except (TypeError, ValueError, ValidationError) as exc:
        raise FactoryDispatchError(
            f"Hermes must return exactly one typed {step.value} artifact"
        ) from exc
    assert isinstance(
        parsed,
        (
            CodebaseInventoryV1,
            CodexBuildBriefV1,
            CodexBuildEvidenceV1,
            TeamExecutionEvidenceV1,
            TeamEvaluationV1,
            CandidateRevisionV1,
            FactoryFeedbackV1,
        ),
    )
    return parsed


def _may_continue_after(artifact: _FactoryWorkflowArtifact) -> bool:
    if isinstance(artifact, TeamExecutionEvidenceV1):
        return artifact.status == "succeeded"
    if isinstance(artifact, TeamEvaluationV1):
        # Evaluation is evidence, not a terminal Hermes decision.  The Quality
        # Warden must always run REPORT_CAPTAIN so Captain receives one typed,
        # redacted recommendation even when deterministic evaluation failed.
        return True
    return True


def _factory_block_for(
    request: FactoryDispatch,
    *,
    artifacts: tuple[_FactoryWorkflowArtifact, ...],
    transcript_refs: tuple[ArtifactRef, ...],
) -> FactoryEvidenceBlock:
    if not artifacts or request.role is None or request.lease is None:
        raise FactoryDispatchError("Hermes factory sequence produced no typed artifacts")
    last = artifacts[-1]
    successful = _may_continue_after(last)
    status = FactoryBlockStatus.SUCCEEDED
    if not successful:
        status = FactoryBlockStatus.FAILED
    elif isinstance(last, FactoryFeedbackV1) and (
        last.recommendation is not FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
    ):
        status = FactoryBlockStatus.RECOMMENDED
    phase = _ROLE_EVIDENCE_PHASE[request.role]
    event_binding = _canonical_json(
        {
            "job_id": str(request.job.job_id),
            "subject_version": request.job.subject_version,
            "attempt": request.action.attempt,
            "role": request.role.value,
            "artifact_sha256": [item.artifact_ref.sha256 for item in artifacts],
        }
    )
    evidence_refs = _unique_artifact_refs(
        (
            *(ref for artifact in artifacts for ref in artifact.evidence_refs),
            *transcript_refs,
        )
    )
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(NAMESPACE_URL, f"captain.factory-block:{event_binding}"),
        job_id=request.job.job_id,
        correlation_id=request.job.correlation_id,
        causation_id=request.job.event_id,
        occurred_at=last.occurred_at,
        producer="hermes",
        subject_version=request.job.subject_version,
        attempt=request.action.attempt,
        phase=phase,
        role=request.role,
        status=status,
        artifact_refs=_unique_artifact_refs(
            tuple(item.artifact_ref for item in artifacts)
        ),
        evidence_refs=evidence_refs,
        assertion_ids=last.acceptance_assertion_ids,
        lease_id=request.lease.lease_id,
    )


def _unique_artifact_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        unique.setdefault(
            (reference.uri, reference.sha256, reference.media_type),
            reference,
        )
    return tuple(unique.values())


def _parse_evidence_payload(stdout: bytes) -> object:
    """Accept one JSON object plus Hermes' non-semantic trailing tool telemetry."""

    text = stdout.decode("utf-8")
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text.lstrip())
    remainder = text.lstrip()[end:].strip()
    if remainder and any(not line.strip().startswith("[tool]") for line in remainder.splitlines() if line.strip()):
        raise ValueError("Hermes output contains non-telemetry content after its evidence object")
    return payload


def _resolve_released_skill(
    released_skill_root: Path,
    request: HermesSkillEvaluationRequest,
) -> Path:
    prefix = "artifact://released-skills/"
    reference = request.released_skill.content_ref.uri
    if not reference.startswith(prefix):
        raise FactoryDispatchError("released skill reference is outside the configured root")
    relative = Path(reference.removeprefix(prefix))
    root = released_skill_root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FactoryDispatchError("released skill path is outside the configured root") from exc
    if not resolved.is_file():
        raise FactoryDispatchError("released skill file is missing")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if digest != request.released_skill.content_sha256:
        raise FactoryDispatchError("released skill digest does not match Captain's reference")
    return resolved


def _skill_usage_prompt_for(
    request: HermesSkillEvaluationRequest,
    skill_path: Path,
) -> str:
    response_shape = {
        "schema": "hermes.skill-usage-receipt.v1",
        "receipt_id": "generate a new UUID",
        "request_id": str(request.request_id),
        "job_id": str(request.job_id),
        "correlation_id": str(request.correlation_id),
        "lease_id": request.lease.lease_id,
        "occurred_at": "UTC timestamp within the active lease",
        "producer": "hermes",
        "released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
        "used_skill_id": request.released_skill.skill_id,
        "used_skill_version": request.released_skill.version,
        "used_skill_sha256": request.released_skill.content_sha256,
        "commands": [{"command_id": "python.compileall", "max_seconds": 60}],
        "evidence_refs": [
            {
                "uri": "artifact://factory-skill-usage/replace-with-receipt-evidence",
                "sha256": "replace-with-64-lowercase-hex-digest",
                "media_type": "application/json",
            }
        ],
        "assertion_ids": list(request.acceptance_assertion_ids),
        "outcome": "unresolved",
    }
    return "\n".join(
        (
            "Use the supplied released skill first and emit its usage receipt only.",
            f"released_skill_path={skill_path.as_posix()}",
            "Do not build, test, repair, or propose a candidate in this stage.",
            "Return exactly one hermes.skill-usage-receipt.v1 JSON object and no markdown or prose.",
            f"captain_request_json={_canonical_json(request.model_dump(mode='json', by_alias=True))}",
            f"response_shape_json={_canonical_json(response_shape)}",
            "Never publish a skill and never write Captain's ledger.",
        )
    )


def _skill_evaluation_prompt_for(
    request: HermesSkillEvaluationRequest,
    receipt: HermesSkillUsageReceipt,
    candidate_result: "FactoryCandidateEvaluationResult",
    candidate_id: str,
    candidate_source_ref: ArtifactRef,
    skill_path: Path,
) -> str:
    command = receipt.commands[0].model_dump(mode="json")
    test_command = receipt.commands[-1].model_dump(mode="json")
    artifact_placeholder = {
        "uri": "artifact://factory-skill-evaluation/replace-with-sealed-evidence",
        "sha256": "replace-with-64-lowercase-hex-digest",
        "media_type": "application/json",
    }
    candidate_shape = {
        "schema": "hermes.skill-candidate.v1",
        "candidate_id": candidate_id,
        "request_id": str(request.request_id),
        "created_at": "UTC timestamp after receipt and before lease expiry",
        "producer": "hermes",
        "content_ref": candidate_source_ref.model_dump(mode="json"),
        "content_sha256": candidate_source_ref.sha256,
        "parent_released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
        "creation_reason": "describe the bounded successful improvement",
        "status": "private_candidate",
    }
    tool_gap_shape = {
        "schema": "TODO_TOOL.v1",
        "gap_id": "stable-gap-identifier",
        "severity": "required or optional",
        "input_contract_ref": artifact_placeholder,
        "output_contract_ref": artifact_placeholder,
        "least_privilege_capability": "required.capability",
        "implementation_options": [
            {
                "option_id": "bounded-option",
                "description": "one bounded implementation option",
                "acceptance_assertion_id": request.acceptance_assertion_ids[0],
            }
        ],
        "acceptance_assertion_ids": list(request.acceptance_assertion_ids),
        "evidence_ref": artifact_placeholder,
        "status": "unresolved or resolved",
    }
    def check_shape(
        check_id: str,
        kind: str,
        bounded_command: dict[str, object],
        assertions: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "check_id": check_id,
            "kind": kind,
            "command": bounded_command,
            "status": "passed, failed, or skipped",
            "occurred_at": "UTC timestamp after receipt and before lease expiry",
            "evidence_ref": artifact_placeholder,
            "assertion_ids": list(assertions),
        }
    response_shape = {
        "schema": "hermes.skill-evaluation-evidence.v1",
        "evidence_id": "generate a new UUID",
        "request_id": str(request.request_id),
        "job_id": str(request.job_id),
        "correlation_id": str(request.correlation_id),
        "subject_id": request.subject_id,
        "subject_version": request.subject_version,
        "occurred_at": "UTC timestamp after receipt and before lease expiry",
        "producer": "hermes",
        "request": request.model_dump(mode="json", by_alias=True),
        "receipt": receipt.model_dump(mode="json", by_alias=True),
        "candidate": candidate_shape,
        "tool_gaps": [tool_gap_shape],
        "checks": [
            check_shape("build", "build", command, ()),
            check_shape("test", "test", test_command, request.acceptance_assertion_ids),
        ],
        "assertion_ids": list(request.acceptance_assertion_ids),
        "outcome": "passed, redo, blocked_tool_gap, unresolved, or failed",
    }
    return "\n".join(
        (
            "Use the supplied released skill first; do not substitute or load another skill.",
            f"released_skill_path={skill_path.as_posix()}",
            "Write only in the leased workspace.",
            "Return exactly one hermes.skill-evaluation-evidence.v1 JSON object and no markdown or prose.",
            f"captain_request_json={_canonical_json(request.model_dump(mode='json', by_alias=True))}",
            f"sealed_candidate_result_json={_canonical_json({'status': candidate_result.status, 'assertion_ids': list(candidate_result.assertion_ids), 'check_names': [check.name for check in candidate_result.checks]})}",
            f"response_shape_json={_canonical_json(response_shape)}",
            "When required access is unavailable, record TODO_TOOL.v1 instead of inventing access.",
            "Retain a private candidate only after the task is successful.",
            "Never publish a skill and never write Captain's ledger.",
        )
    )


_PROMPT_ENDPOINT = re.compile(r"(?i)https?://")
_PROMPT_N8N_ENDPOINT = re.compile(
    r"(?i)(?:\bn8n(?:[._-][a-z0-9-]+)*:\d+|\bn8n[_-]?endpoint\s*=)"
)
_PROMPT_SECRET = re.compile(
    r"(?i)(?:api[-_]?key|authorization|credential|password|private[-_]?key|secret|token)(?:\b|[=:_?-])"
)


def _validate_skill_prompt_request(request: HermesSkillEvaluationRequest) -> None:
    _validate_serialized_prompt_value(request.model_dump(mode="json", by_alias=True))


def _validate_serialized_prompt_value(value: object) -> None:
    try:
        reject_sensitive_data(value, "skill prompt")
    except ValueError as exc:
        raise FactoryDispatchError(
            "skill evaluation request contains an unsafe prompt value"
        ) from exc
    if isinstance(value, dict):
        for key, nested in value.items():
            _validate_serialized_prompt_value(key)
            _validate_serialized_prompt_value(nested)
        return
    if isinstance(value, (tuple, list)):
        for nested in value:
            _validate_serialized_prompt_value(nested)
        return
    if not isinstance(value, str):
        return
    if (
        "\r" in value
        or "\n" in value
        or _PROMPT_ENDPOINT.search(value)
        or _PROMPT_N8N_ENDPOINT.search(value)
        or _PROMPT_SECRET.search(value)
    ):
        raise FactoryDispatchError("skill evaluation request contains an unsafe prompt value")


def _deadline(max_seconds: float) -> float:
    if max_seconds <= 0:
        raise FactoryDispatchError("Hermes skill evaluation has no remaining lease time")
    return time.monotonic() + max_seconds


def _remaining_deadline_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FactoryDispatchError("Hermes skill evaluation timed out")
    return remaining


def _async_process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _resolve_hermes_module_root(module_root: Path) -> Path:
    try:
        root = module_root.resolve(strict=True)
        entrypoint = (root / "hermes_cli" / "main.py").resolve(strict=True)
        entrypoint.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FactoryDispatchError(
            "Hermes module root is not a valid checkout"
        ) from exc
    if not root.is_dir() or not entrypoint.is_file():
        raise FactoryDispatchError("Hermes module root is not a valid checkout")
    return root


async def _terminate_async_process_tree(
    process: asyncio.subprocess.Process,
    *,
    executable: str,
) -> None:
    """Terminate only the tree rooted at the process this adapter just spawned."""

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        process.terminate()
        await process.wait()
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(killer.communicate(), timeout=5)
        except TimeoutError:
            killer.kill()
            await asyncio.wait_for(killer.wait(), timeout=5)
        if killer.returncode not in {0, 128} and process.returncode is None:
            process.kill()
    else:
        group_found = True
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            group_found = False
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass
    finally:
        if os.name != "nt" and group_found:
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass
        elif os.name == "nt" and process.returncode is None:
            process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError as exc:
        raise FactoryDispatchError(
            f"could not terminate Hermes process tree for {executable}"
        ) from exc


def _require_matching_receipt(
    request: HermesSkillEvaluationRequest,
    receipt: HermesSkillUsageReceipt,
) -> None:
    if (
        receipt.request_id != request.request_id
        or receipt.job_id != request.job_id
        or receipt.correlation_id != request.correlation_id
        or receipt.lease_id != request.lease.lease_id
        or receipt.released_skill != request.released_skill
        or receipt.used_skill_id != request.released_skill.skill_id
        or receipt.used_skill_version != request.released_skill.version
        or receipt.used_skill_sha256 != request.released_skill.content_sha256
    ):
        raise ValueError("skill usage receipt does not match the Captain request")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_ROLE_EVIDENCE_PHASE = {
    FactoryRole.AGENT_ARCHITECT: FactoryPhase.BLUEPRINT_CREATED,
    FactoryRole.TOOL_INTEGRATOR: FactoryPhase.TOOL_CANDIDATE_TESTED,
    FactoryRole.REAL_CASE_TESTER: FactoryPhase.REAL_CASE_EVIDENCE,
    FactoryRole.QUALITY_WARDEN: FactoryPhase.QUALITY_REVIEWED,
}
