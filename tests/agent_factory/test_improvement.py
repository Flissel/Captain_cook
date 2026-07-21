from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.improvement import ImprovementBuilder
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateChangedComponent,
    FactorySkillInvocationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_hermes_cli import _improvement_authorization
from tests.agent_factory.test_skill_workflow_contracts import (
    artifact,
    invocation_payload,
    lease_payload,
)


NOW = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc)


def _invocation() -> FactorySkillInvocationV1:
    authorization = _improvement_authorization()
    payload = invocation_payload(
        "improve_team",
        attempt=2,
        input_ref=authorization.authorization_ref.model_dump(mode="json"),
        input_sha256=authorization.authorization_ref.sha256,
        lease=lease_payload(
            "tool_integrator",
            "factory-tool-integrator",
            attempt=2,
            expires_at=NOW + timedelta(minutes=8),
        ),
    )
    return FactorySkillInvocationV1.model_validate(payload)


def _new_candidate() -> ArtifactRef:
    return ArtifactRef.model_validate(artifact("revised-candidate", "7" * 64))


def _codex_session() -> ArtifactRef:
    return ArtifactRef.model_validate(artifact("codex-session", "6" * 64))


def _builder() -> ImprovementBuilder:
    authorization = _improvement_authorization()
    return ImprovementBuilder(
        authorization=authorization,
        candidate_ref=_new_candidate(),
        codex_session_ref=_codex_session(),
        component_by_assertion={
            "real_case_green": CandidateChangedComponent.SYSTEM_PROMPT,
        },
        clock=lambda: NOW,
    )


def test_improvement_targets_failed_components_and_preserves_green_assertions() -> None:
    authorization = _improvement_authorization()

    revision = _builder().build(
        invocation=_invocation(),
        prior_candidate=authorization.prior_candidate_ref,
        evaluation=authorization.failed_evaluation,
    )

    assert revision.changed_components == (
        CandidateChangedComponent.SYSTEM_PROMPT,
    )
    assert revision.regression_assertion_ids == ("schema_valid",)
    assert revision.failed_assertion_ids == ("real_case_green",)
    assert revision.parent_candidate_ref == authorization.prior_candidate_ref
    assert revision.candidate_ref == _new_candidate()


def test_improvement_requires_the_exact_captain_request_block() -> None:
    authorization = _improvement_authorization()
    builder = ImprovementBuilder(
        candidate_ref=_new_candidate(),
        codex_session_ref=_codex_session(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="Captain.*IMPROVEMENT_REQUESTED"):
        builder.build(
            invocation=_invocation(),
            prior_candidate=authorization.prior_candidate_ref,
            evaluation=authorization.failed_evaluation,
        )


@pytest.mark.parametrize("binding", ["attempt", "candidate", "evaluation"])
def test_improvement_rejects_stale_or_changed_authority(binding: str) -> None:
    authorization = _improvement_authorization()
    invocation = _invocation()
    prior_candidate = authorization.prior_candidate_ref
    evaluation = authorization.failed_evaluation
    if binding == "attempt":
        invocation = invocation.model_construct(
            **{**invocation.__dict__, "attempt": 3}
        )
    elif binding == "candidate":
        prior_candidate = ArtifactRef.model_validate(artifact("other", "5" * 64))
    else:
        evaluation = evaluation.model_copy(
            update={"artifact_ref": ArtifactRef.model_validate(artifact("other", "5" * 64))}
        )

    with pytest.raises(ValueError, match="authorization|binding"):
        _builder().build(
            invocation=invocation,
            prior_candidate=prior_candidate,
            evaluation=evaluation,
        )


def test_improvement_never_uses_a_green_assertion_as_a_change_target() -> None:
    authorization = _improvement_authorization()
    builder = ImprovementBuilder(
        authorization=authorization,
        candidate_ref=_new_candidate(),
        codex_session_ref=_codex_session(),
        component_by_assertion={
            "schema_valid": CandidateChangedComponent.AGENT_CODE,
            "real_case_green": CandidateChangedComponent.SYSTEM_PROMPT,
        },
        clock=lambda: NOW,
    )

    revision = builder.build(
        invocation=_invocation(),
        prior_candidate=authorization.prior_candidate_ref,
        evaluation=authorization.failed_evaluation,
    )

    assert revision.changed_components == (
        CandidateChangedComponent.SYSTEM_PROMPT,
    )
    assert authorization.authorization_ref in revision.evidence_refs
    assert authorization.failed_evaluation.artifact_ref in revision.evidence_refs
