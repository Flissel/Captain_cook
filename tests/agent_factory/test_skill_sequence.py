from __future__ import annotations

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep


@pytest.mark.parametrize(
    ("role", "attempt", "expected"),
    [
        (FactoryRole.AGENT_ARCHITECT, 1, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.AGENT_ARCHITECT, 2, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.TOOL_INTEGRATOR, 1, (FactorySkillStep.BRIEF_CODEX,)),
        (
            FactoryRole.TOOL_INTEGRATOR,
            2,
            (FactorySkillStep.IMPROVE_TEAM, FactorySkillStep.BRIEF_CODEX),
        ),
        (FactoryRole.REAL_CASE_TESTER, 1, (FactorySkillStep.EXECUTE_TEAM,)),
        (
            FactoryRole.QUALITY_WARDEN,
            1,
            (FactorySkillStep.EVALUATE_TEAM, FactorySkillStep.REPORT_CAPTAIN),
        ),
    ],
)
def test_role_attempt_maps_to_exact_skill_sequence(
    role: FactoryRole,
    attempt: int,
    expected: tuple[FactorySkillStep, ...],
) -> None:
    assert SkillSequencePolicy().steps_for(role=role, attempt=attempt) == expected


@pytest.mark.parametrize("attempt", [0, 6])
def test_sequence_rejects_attempt_outside_captain_limit(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        SkillSequencePolicy().steps_for(
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=attempt,
        )
