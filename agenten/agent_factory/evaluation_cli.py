"""Run one Captain-authorized candidate-validation lease outside the gateway process."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
    StaticFactoryCandidateProvider,
)
from agenten.agent_factory.contracts import AgentFactoryJob, FactoryLease, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind


_ACTION_ROLES: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_BUILD_VALIDATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        job = AgentFactoryJob.model_validate_json(Path(args.job).read_text(encoding="utf-8"))
        lease = FactoryLease.model_validate_json(Path(args.lease).read_text(encoding="utf-8"))
        candidate = FactoryCandidateManifest.model_validate_json(Path(args.candidate).read_text(encoding="utf-8"))
        action_kind = FactoryActionKind(args.action)
        role = _ACTION_ROLES[action_kind]
        active_lease = validate_factory_lease(
            lease,
            job=job,
            role=role,
            attempt=lease.attempt,
            now=datetime.now(timezone.utc),
        )
        validator = CandidateEvaluationFactory(
            provider=StaticFactoryCandidateProvider(
                {job.job_id: ResolvedFactoryCandidate(candidate=candidate, source_archive=Path(args.source_archive))}
            ),
            evidence_store=FilesystemFactoryEvidenceStore(Path(args.evidence_root)),
        )
        block = asyncio.run(
            validator.dispatch(
                FactoryDispatch(
                    job=job,
                    action=FactoryAction(kind=action_kind, attempt=active_lease.attempt, job_id=job.job_id),
                    role=role,
                    lease=active_lease,
                )
            )
        )
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}, sort_keys=True))
        return 1
    print(block.model_dump_json(by_alias=True))
    return 0 if block.status.value == "succeeded" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one sealed Agent-Factory candidate under a Captain lease.")
    parser.add_argument("--job", required=True, help="Path to a Captain AgentFactoryJob JSON envelope.")
    parser.add_argument("--lease", required=True, help="Path to the active Captain FactoryLease JSON envelope.")
    parser.add_argument("--candidate", required=True, help="Path to the sealed FactoryCandidateManifest JSON.")
    parser.add_argument("--source-archive", required=True, help="Path to the digest-verified generated source ZIP.")
    parser.add_argument("--evidence-root", required=True, help="Captain-owned local root for immutable evidence JSON.")
    parser.add_argument("--action", required=True, choices=tuple(kind.value for kind in _ACTION_ROLES))
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
