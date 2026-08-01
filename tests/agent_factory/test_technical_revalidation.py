from datetime import timedelta
from decimal import Decimal

import pytest

from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.technical_revalidation import (
    FilesystemFactoryTechnicalRevalidationAuthority,
    build_technical_revalidation_authorization,
    technical_revalidation_runtime_sha256,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_state_machine import NOW, job_v3


def _ref(kind: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://factory/{kind}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def test_filesystem_authority_revalidates_code_candidate_scope_and_budget(
    tmp_path,
) -> None:
    repository = tmp_path / "repository"
    (repository / "runtime.py").parent.mkdir(parents=True)
    (repository / "runtime.py").write_text("runtime-v2\n", encoding="utf-8")
    (repository / "evaluator.py").write_text("evaluator-v2\n", encoding="utf-8")
    job = job_v3(mode="demo")
    source_ref = _ref("team-execution", "4" * 64)
    candidate_ref = _ref("candidate", "5" * 64)
    authorization = build_technical_revalidation_authorization(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        source_block_id=job.event_id,
        source_evidence_ref=source_ref,
        candidate_ref=candidate_ref,
        holdout_ref=job.private_holdout_refs[0],
        reason="host_runtime_corrected",
        code_revision="6" * 40,
        runtime_sha256=technical_revalidation_runtime_sha256(
            repository, ("runtime.py",)
        ),
        evaluator_sha256=technical_revalidation_runtime_sha256(
            repository, ("evaluator.py",)
        ),
        maximum_additional_cost_usd=Decimal("0.12"),
        budget_remaining_usd=Decimal("0.27"),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    authority = FilesystemFactoryTechnicalRevalidationAuthority(
        tmp_path / "authority",
        repository_root=repository,
        runtime_paths=("runtime.py",),
        evaluator_paths=("evaluator.py",),
    )
    authority.persist(authorization)
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION,
        attempt=1,
        job_id=job.job_id,
        authorization_ref=authorization.artifact_ref,
        supersedes_ref=source_ref,
    )
    budget = FactoryBudgetProjection(
        job_id=job.job_id,
        limit_usd=Decimal("0.40"),
        consumed_usd=Decimal("0.13"),
        reserved_usd=Decimal("0"),
        remaining_usd=Decimal("0.27"),
    )

    assert authority.active(
        job=job,
        action=action,
        budget=budget,
        now=NOW + timedelta(minutes=1),
        code_revision="6" * 40,
        candidate_ref=candidate_ref,
        holdout_ref=job.private_holdout_refs[0],
    ) == authorization

    (repository / "runtime.py").write_text("runtime-drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale or mixed"):
        authority.active(
            job=job,
            action=action,
            budget=budget,
            now=NOW + timedelta(minutes=1),
            code_revision="6" * 40,
            candidate_ref=candidate_ref,
            holdout_ref=job.private_holdout_refs[0],
        )
