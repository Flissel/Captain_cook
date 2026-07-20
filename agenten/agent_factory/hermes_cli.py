"""Concrete non-interactive Hermes CLI adapter for Captain factory roles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FactoryEvidenceStore, FilesystemFactoryEvidenceStore
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, HermesFactoryPort
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
)
from agenten.agent_runtime.contracts import ArtifactRef

if TYPE_CHECKING:
    from agenten.agent_factory.candidate_evaluation import FactoryCandidateEvaluationResult


@dataclass(frozen=True)
class HermesCliSettings:
    executable: str = "hermes"
    skill_path: Path = Path("agenten/agent_factory/skills/autogen-agent-factory")
    timeout_seconds: int = 900
    evidence_root: Path = Path("artifacts/agent-factory/evidence")
    released_skill_root: Path = Path("agenten/agent_factory/released-skills")


class HermesCliFactory(HermesFactoryPort):
    """Run one hermetic Hermes query and accept only a typed evidence response."""

    def __init__(
        self,
        settings: HermesCliSettings = HermesCliSettings(),
        evidence_store: FactoryEvidenceStore | None = None,
    ) -> None:
        self._settings = settings
        self._evidence_store = evidence_store or FilesystemFactoryEvidenceStore(settings.evidence_root)

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        if request.role is None or request.lease is None:
            raise FactoryDispatchError("Hermes factory dispatch requires a role and active lease")
        prompt = _prompt_for(request, self._settings.skill_path)
        try:
            process = await asyncio.create_subprocess_exec(
                self._settings.executable,
                "-z",
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._settings.timeout_seconds
            )
        except FileNotFoundError as exc:
            raise FactoryDispatchError("Hermes CLI executable is not available") from exc
        except TimeoutError as exc:
            raise FactoryDispatchError("Hermes factory role timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FactoryDispatchError(f"Hermes factory role failed: {detail[:500]}")
        try:
            payload = _parse_evidence_payload(stdout)
            if not isinstance(payload, dict):
                raise ValueError("Hermes output must be an object")
            transcript = await self._evidence_store.persist(
                request.job,
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            payload["evidence_refs"] = [transcript.model_dump(mode="json")]
            return FactoryEvidenceBlock.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise FactoryDispatchError("Hermes must return exactly one factory evidence JSON object") from exc

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
        try:
            process = await asyncio.create_subprocess_exec(
                self._settings.executable,
                "-z",
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_async_process_group_options(),
            )
        except FileNotFoundError as exc:
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
            raise FactoryDispatchError("Hermes skill evaluation timed out") from exc
        _remaining_deadline_seconds(deadline)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FactoryDispatchError(f"Hermes skill evaluation failed: {detail[:500]}")
        return stdout


def _parse_evidence_payload(stdout: bytes) -> object:
    """Accept one JSON object plus Hermes' non-semantic trailing tool telemetry."""

    text = stdout.decode("utf-8")
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text.lstrip())
    remainder = text.lstrip()[end:].strip()
    if remainder and any(not line.strip().startswith("[tool]") for line in remainder.splitlines() if line.strip()):
        raise ValueError("Hermes output contains non-telemetry content after its evidence object")
    return payload


def _prompt_for(request: FactoryDispatch, skill_path: Path) -> str:
    assert request.role is not None
    assert request.lease is not None
    phase = _ROLE_EVIDENCE_PHASE[request.role]
    response_shape = {
        "schema": "captain.agent-factory-block.v1",
        "event_id": "generate a new UUID",
        "job_id": str(request.job.job_id),
        "correlation_id": str(request.job.correlation_id),
        "causation_id": str(request.job.event_id),
        "occurred_at": request.lease.issued_at.isoformat(),
        "producer": "hermes",
        "subject_version": request.job.subject_version,
        "attempt": request.action.attempt,
        "phase": phase.value,
        "role": request.role.value,
        "status": "succeeded",
        "artifact_refs": [],
        "evidence_refs": [
            {
                "uri": "artifact://factory/replace-with-real-evidence",
                "sha256": "replace-with-sha256-of-real-evidence",
                "media_type": "application/json",
            }
        ],
        "assertion_ids": [],
        "lease_id": request.lease.lease_id,
    }
    return "\n".join(
        (
            f"Use the skill at {skill_path.as_posix()}.",
            "You are a leased Hermes factory role. Do not write Captain's ledger directly.",
            f"job_id={request.job.job_id}",
            f"correlation_id={request.job.correlation_id}",
            f"subject_version={request.job.subject_version}",
            f"attempt={request.action.attempt}",
            f"role={request.role.value}",
            f"lease_id={request.lease.lease_id}",
            f"workspace_ref={request.lease.workspace_ref}",
            f"input_ref={request.job.input_ref.uri}",
            f"required_capability={request.job.required_capability}",
            f"acceptance_assertion_ids={','.join(request.job.acceptance_assertion_ids)}",
            "Return exactly one JSON object and no markdown or prose.",
            "Use this exact evidence envelope; replace event_id, occurred_at, and evidence_refs with actual values.",
            "Every role block needs at least one real evidence_ref. Create and hash the evidence before returning; never claim success with a placeholder.",
            json.dumps(response_shape, separators=(",", ":")),
        )
    )


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
    if process.returncode is not None:
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
            await killer.wait()
        if killer.returncode not in {0, 128} and process.returncode is None:
            process.kill()
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        if os.name != "nt":
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()


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
