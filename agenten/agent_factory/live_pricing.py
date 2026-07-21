"""Fail-closed binding of external model pricing to one Captain job."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import FactoryPricingQuoteV1


class FactoryPricingQuoteSourcePort(Protocol):
    """Resolve a provider quote without exposing provider credentials."""

    def resolve_quote(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1 | None: ...


class CaptainPricingAuthorityAdapter:
    """Accept only a known quote bound to the current Captain policy."""

    def __init__(self, source: FactoryPricingQuoteSourcePort) -> None:
        if source is None:
            raise ValueError("pricing quote source is required")
        self._source = source

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1:
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
        ):
            raise ValueError("pricing invocation is not bound to the Captain job")
        quote = self._source.resolve_quote(
            job=job,
            invocation=invocation,
            provider=provider,
            model=model,
            now=now,
        )
        if quote is None:
            raise ValueError("pricing quote is unknown")
        if (
            quote.job_id != job.job_id
            or quote.subject_version != job.subject_version
            or quote.execution_policy_sha256 != _execution_policy_digest(job)
            or quote.provider != provider
            or quote.model != model
            or quote.effective_at > now
        ):
            raise ValueError("pricing quote is not bound to this Captain job and model")
        return quote


def _execution_policy_digest(job: AgentFactoryJobV3) -> str:
    encoded = json.dumps(
        job.execution_policy.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
