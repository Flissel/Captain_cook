"""Digest-verifying facade for Forge-produced candidate archives."""

from __future__ import annotations

import hashlib

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateProvider,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJob


class SealedForgeCandidateProvider(FactoryCandidateProvider):
    """Materialize one candidate and reject missing or substituted archives."""

    def __init__(self, delegate: FactoryCandidateProvider) -> None:
        if delegate is None:
            raise ValueError("Forge candidate provider is required")
        self._delegate = delegate

    def candidate_for(self, job: AgentFactoryJob) -> ResolvedFactoryCandidate:
        candidate = self._delegate.candidate_for(job)
        if not isinstance(candidate, ResolvedFactoryCandidate):
            raise ValueError("Forge returned an untyped candidate")
        archive = candidate.source_archive
        if not archive.is_file():
            raise ValueError("Forge sealed candidate archive is missing")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != candidate.candidate.source_archive_ref.sha256:
            raise ValueError("Forge sealed candidate archive digest does not match")
        return candidate
