"""Gateway composition for Captain-issued technical improvement authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.candidate_evaluation import GatewayForgeCandidateProvider
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from blockchain.mariadb_storage import MariaDBStorage
from gateway.factory_improvement_authority import (
    CaptainFactoryImprovementAuthorizationStore,
    CaptainTechnicalImprovementIssuer,
)
from gateway.factory_repository import GatewayFactoryRepository
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from gateway.store import GatewayStore


def issue_captain_technical_improvements(
    *,
    workspace: Path,
    authority_root: Path,
    test_mariadb_dsn: str,
    job_ids: tuple[UUID, ...],
    clock: Callable[[], datetime],
) -> tuple[FactoryImprovementAuthorizationV1, ...]:
    """Issue exact retry authority without constructing any paid provider."""

    assert_local_captain_test_dsn(test_mariadb_dsn)
    resolved_workspace = workspace.resolve(strict=True)
    resolved_authority = authority_root.resolve(strict=True)
    expected_authority = (
        resolved_workspace / ".captain-cook" / "private" / "business-benchmarks"
    ).resolve(strict=True)
    if resolved_authority != expected_authority:
        raise ValueError("improvement authority root does not match the workspace")
    if not job_ids or len(job_ids) != len(set(job_ids)):
        raise ValueError("improvement job IDs must be non-empty and unique")

    store = GatewayStore(MariaDBStorage(test_mariadb_dsn))
    repository = GatewayFactoryRepository(store)
    issuer = CaptainTechnicalImprovementIssuer(
        repository=repository,
        coordinator=FactoryCoordinator(repository),
        candidates=GatewayForgeCandidateProvider(
            repository=repository,
            artifacts=GatewayMinibookCreationArtifactStore(
                resolved_workspace / ".captain-cook" / "minibook-creation-cas"
            ),
        ),
        evidence=FilesystemFactoryEvidenceStore(
            resolved_authority / "runtime-state" / "factory-evidence"
        ),
        authorizations=CaptainFactoryImprovementAuthorizationStore(
            resolved_authority / "runtime-state" / "improvement-authorizations"
        ),
        clock=clock,
    )
    return tuple(issuer.issue(job_id) for job_id in job_ids)


__all__ = ["issue_captain_technical_improvements"]
