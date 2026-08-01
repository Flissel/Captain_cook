from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import NAMESPACE_URL, UUID, uuid5

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_workflow_contracts import TeamExecutionEvidenceV1
from agenten.agent_factory.technical_revalidation import (
    FilesystemFactoryTechnicalRevalidationAuthority,
    TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
    TECHNICAL_REVALIDATION_RUNTIME_PATHS,
    build_technical_revalidation_authorization,
    technical_revalidation_runtime_sha256,
)
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Authorize one same-candidate technical revalidation after a host fix."
        )
    )
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument(
        "--maximum-additional-cost-usd",
        type=Decimal,
        default=Decimal("0.12"),
    )
    return parser


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    revision = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("current Factory Git revision is unavailable")
    return revision


def main() -> int:
    args = _parser().parse_args()
    dsn = os.environ.get("TEST_MARIADB_DSN", "").strip()
    assert_local_captain_test_dsn(dsn)
    authority_root = Path(
        os.environ.get("CAPTAIN_BENCHMARK_AUTHORITY_ROOT", "").strip()
    ).resolve()
    expected_root = (
        REPOSITORY_ROOT / ".captain-cook" / "private" / "business-benchmarks"
    ).resolve()
    if authority_root != expected_root:
        raise ValueError("technical revalidation authority root does not match")

    store = GatewayStore(MariaDBStorage(dsn))
    repository = GatewayFactoryRepository(store)
    coordinator = FactoryCoordinator(repository)
    stored = store.factory_job(args.job_id)
    job = stored.job
    if not isinstance(job, AgentFactoryJobV3):
        raise ValueError("technical revalidation requires a V3 job")
    projection = stored.projection
    if projection.phase not in {
        FactoryPhase.REAL_CASE_EVIDENCE,
        FactoryPhase.REAL_CASE_REVALIDATED,
        FactoryPhase.TECHNICAL_REVALIDATION_REQUESTED,
    }:
        raise ValueError("Factory job is not at a technical evidence checkpoint")
    technical_blocks = tuple(
        block
        for block in stored.blocks
        if block.attempt == projection.attempt
        and block.phase
        in {
            FactoryPhase.REAL_CASE_EVIDENCE,
            FactoryPhase.REAL_CASE_REVALIDATED,
        }
    )
    if not technical_blocks or technical_blocks[-1].status is not FactoryBlockStatus.FAILED:
        raise ValueError("latest technical evidence is not failed")
    source_block = technical_blocks[-1]
    executions = tuple(
        item
        for item in repository.workflow_artifacts(job.job_id)
        if isinstance(item, TeamExecutionEvidenceV1)
        and item.attempt == projection.attempt
    )
    if not executions or executions[-1].status == "succeeded":
        raise ValueError("failed technical workflow evidence is unavailable")
    execution = executions[-1]
    evidence_directory = (
        authority_root
        / "runtime-state"
        / "factory-evidence"
        / str(job.job_id)
    )
    sealed: list[tuple[object, Path]] = []
    for reference in source_block.evidence_refs:
        candidate_path = evidence_directory / f"{reference.sha256}.json"
        if not candidate_path.is_file():
            continue
        try:
            candidate_evidence = TeamExecutionEvidenceV1.model_validate_json(
                candidate_path.read_bytes()
            )
        except ValueError:
            continue
        if candidate_evidence == execution:
            sealed.append((reference, candidate_path))
    sealed_refs = tuple(item[0] for item in sealed)
    if len(sealed_refs) != 1:
        raise ValueError("technical source evidence is ambiguous")
    source_ref = sealed_refs[0]
    sealed_path = sealed[0][1]
    if TeamExecutionEvidenceV1.model_validate_json(sealed_path.read_bytes()) != execution:
        raise ValueError("technical source evidence does not match Gateway")

    now = datetime.now(timezone.utc)
    budget = repository.workflow_budget_projection(job.job_id)
    maximum = args.maximum_additional_cost_usd
    if (
        maximum <= 0
        or budget.reserved_usd != 0
        or maximum > budget.remaining_usd
        or not job.occurred_at <= now < job.deadline_at
    ):
        raise ValueError("technical revalidation budget or job window is unavailable")
    authorization = build_technical_revalidation_authorization(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=projection.attempt,
        source_block_id=source_block.event_id,
        source_evidence_ref=source_ref,
        candidate_ref=execution.candidate_ref,
        holdout_ref=execution.holdout_ref,
        reason="host_runtime_corrected",
        code_revision=_git_revision(),
        runtime_sha256=technical_revalidation_runtime_sha256(
            REPOSITORY_ROOT,
            TECHNICAL_REVALIDATION_RUNTIME_PATHS,
        ),
        evaluator_sha256=technical_revalidation_runtime_sha256(
            REPOSITORY_ROOT,
            TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
        ),
        maximum_additional_cost_usd=maximum,
        budget_remaining_usd=budget.remaining_usd,
        issued_at=now,
        expires_at=job.deadline_at,
    )
    authority = FilesystemFactoryTechnicalRevalidationAuthority(
        authority_root
        / "runtime-state"
        / "technical-revalidation-authorizations",
        repository_root=REPOSITORY_ROOT,
        runtime_paths=TECHNICAL_REVALIDATION_RUNTIME_PATHS,
        evaluator_paths=TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
    )
    authority.persist(authorization)
    request = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(
            NAMESPACE_URL,
            f"factory-technical-revalidation|{authorization.artifact_ref.sha256}",
        ),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=source_block.event_id,
        occurred_at=now,
        producer="captain",
        subject_version=job.subject_version,
        attempt=projection.attempt,
        phase=FactoryPhase.TECHNICAL_REVALIDATION_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(source_ref,),
        evidence_refs=(authorization.artifact_ref,),
    )
    coordinator.record(request)
    print(
        json.dumps(
            {
                "schema": "captain.factory-technical-revalidation-issued.v1",
                "database": "captain_test",
                "job_id": str(job.job_id),
                "attempt": projection.attempt,
                "source_block_id": str(source_block.event_id),
                "authorization_ref": authorization.artifact_ref.model_dump(
                    mode="json"
                ),
                "request_block_id": str(request.event_id),
                "maximum_additional_cost_usd": str(maximum),
                "budget_remaining_usd": str(budget.remaining_usd),
                "next_action": "dispatch_technical_revalidation",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
