"""Concrete non-interactive Hermes CLI adapter for Captain factory roles."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FactoryEvidenceStore, FilesystemFactoryEvidenceStore
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, HermesFactoryPort


@dataclass(frozen=True)
class HermesCliSettings:
    executable: str = "hermes"
    skill_path: Path = Path("agenten/agent_factory/skills/autogen-agent-factory")
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
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("Hermes output must be an object")
            transcript = await self._evidence_store.persist(request.job, stdout)
            payload["evidence_refs"] = [transcript.model_dump(mode="json")]
            return FactoryEvidenceBlock.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise FactoryDispatchError("Hermes must return exactly one factory evidence JSON object") from exc


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


_ROLE_EVIDENCE_PHASE = {
    FactoryRole.AGENT_ARCHITECT: FactoryPhase.BLUEPRINT_CREATED,
    FactoryRole.TOOL_INTEGRATOR: FactoryPhase.TOOL_CANDIDATE_TESTED,
    FactoryRole.REAL_CASE_TESTER: FactoryPhase.REAL_CASE_EVIDENCE,
    FactoryRole.QUALITY_WARDEN: FactoryPhase.QUALITY_REVIEWED,
}
