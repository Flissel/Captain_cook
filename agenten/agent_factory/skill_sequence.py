"""Pure selection of Captain-authorized Hermes factory skill steps."""

from __future__ import annotations

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep


class SkillSequencePolicy:
    """Map one leased factory role and attempt to its exact released steps."""

    def steps_for(
        self,
        *,
        role: FactoryRole,
        attempt: int,
    ) -> tuple[FactorySkillStep, ...]:
        if isinstance(attempt, bool) or not 1 <= attempt <= 5:
            raise ValueError("factory skill attempt must be between 1 and 5")
        if role is FactoryRole.AGENT_ARCHITECT:
            return (FactorySkillStep.DISCOVER,)
        if role is FactoryRole.TOOL_INTEGRATOR:
            if attempt > 1:
                return (
                    FactorySkillStep.IMPROVE_TEAM,
                    FactorySkillStep.BRIEF_CODEX,
                )
            return (FactorySkillStep.BRIEF_CODEX,)
        if role is FactoryRole.REAL_CASE_TESTER:
            return (FactorySkillStep.EXECUTE_TEAM,)
        if role is FactoryRole.QUALITY_WARDEN:
            return (
                FactorySkillStep.EVALUATE_TEAM,
                FactorySkillStep.REPORT_CAPTAIN,
            )
        raise ValueError("unsupported factory role")
