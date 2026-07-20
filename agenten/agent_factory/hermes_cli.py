"""Concrete non-interactive Hermes CLI adapter for Captain factory roles."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FactoryEvidenceStore, FilesystemFactoryEvidenceStore
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, HermesFactoryPort
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
)


@dataclass(frozen=True)
class HermesCliSettings:
    executable: str = "hermes"
    skill_path: Path = Path("agenten/agent_factory/skills/autogen-agent-factory")
    released_skill_root: Path = Path("agenten/agent_factory/released-skills")
    timeout_seconds: int = 900
    evidence_root: Path = Path("artifacts/agent-factory/evidence")


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
    ) -> HermesSkillEvaluationEvidence:
        """Run the additive typed skill-evaluation path under one released skill."""

        skill_path = _resolve_released_skill(self._settings.released_skill_root, request)
        prompt = _skill_evaluation_prompt_for(request, skill_path)
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
            raise FactoryDispatchError("Hermes skill evaluation timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FactoryDispatchError(f"Hermes skill evaluation failed: {detail[:500]}")
        try:
            return HermesSkillEvaluationEvidence.model_validate(_parse_evidence_payload(stdout))
        except (TypeError, ValueError) as exc:
            raise FactoryDispatchError(
                "Hermes must return exactly one typed skill evaluation JSON object"
            ) from exc


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


def _skill_evaluation_prompt_for(
    request: HermesSkillEvaluationRequest,
    skill_path: Path,
) -> str:
    return "\n".join(
        (
            "Use the supplied released skill first; do not substitute or load another skill.",
            f"released_skill_path={skill_path.as_posix()}",
            f"released_skill_ref={request.released_skill.content_ref.uri}",
            f"released_skill_sha256={request.released_skill.content_sha256}",
            f"lease_id={request.lease.lease_id}",
            f"workspace_ref={request.lease.workspace_ref}",
            f"acceptance_assertion_ids={','.join(request.acceptance_assertion_ids)}",
            "Write only in the leased workspace.",
            "Return exactly one hermes.skill-evaluation-evidence.v1 JSON object and no markdown or prose.",
            "When required access is unavailable, record TODO_TOOL.v1 instead of inventing access.",
            "Retain a private candidate only after the task is successful.",
            "Never publish a skill and never write Captain's ledger.",
        )
    )


_ROLE_EVIDENCE_PHASE = {
    FactoryRole.AGENT_ARCHITECT: FactoryPhase.BLUEPRINT_CREATED,
    FactoryRole.TOOL_INTEGRATOR: FactoryPhase.TOOL_CANDIDATE_TESTED,
    FactoryRole.REAL_CASE_TESTER: FactoryPhase.REAL_CASE_EVIDENCE,
    FactoryRole.QUALITY_WARDEN: FactoryPhase.QUALITY_REVIEWED,
}
