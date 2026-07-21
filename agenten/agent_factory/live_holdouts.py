"""Captain-owned selection and materialization of private holdout cases."""

from __future__ import annotations

from typing import Any, Protocol

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.team_execution import (
    FactoryHoldoutEvaluationReceiptV1,
    ResolvedFactoryHoldoutCase,
)


class CaptainPrivateHoldoutSourcePort(Protocol):
    """Read one opaque Captain holdout reference from authoritative storage."""

    async def read(self, reference: PrivateHoldoutRef) -> bytes: ...


class CaptainPrivateHoldoutEvaluationPort(Protocol):
    async def evaluate(
        self,
        reference: PrivateHoldoutRef,
        result: Any,
        assertion_ids: tuple[str, ...],
    ) -> FactoryHoldoutEvaluationReceiptV1: ...


class CaptainPrivateHoldoutSelector:
    """Select one immutable holdout already released by the exact job."""

    def __init__(self, *, job: AgentFactoryJobV3, holdout_id: str) -> None:
        selected = tuple(
            reference
            for reference in job.private_holdout_refs
            if reference.holdout_id == holdout_id
        )
        if len(selected) != 1:
            raise ValueError("Captain holdout selector received an unknown holdout ID")
        self._job = job
        self._selected = selected[0]

    def __call__(self, job: AgentFactoryJobV3) -> PrivateHoldoutRef:
        if job != self._job:
            raise ValueError("Captain holdout selector received a different job")
        return self._selected


class CaptainPrivateHoldoutResolver:
    """Resolve only job-authorized bytes and verify their Captain digest."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        source: CaptainPrivateHoldoutSourcePort,
    ) -> None:
        if source is None:
            raise ValueError("Captain holdout source is required")
        self._job = job
        self._source = source

    async def resolve(
        self,
        reference: PrivateHoldoutRef,
    ) -> ResolvedFactoryHoldoutCase:
        if reference not in self._job.private_holdout_refs:
            raise ValueError("private holdout reference is not authorized by Captain")
        body = await self._source.read(reference)
        if not isinstance(body, bytes):
            raise ValueError("private holdout source must return bytes")
        try:
            return ResolvedFactoryHoldoutCase(reference=reference, body=body)
        except ValueError as exc:
            raise ValueError("private holdout body digest does not match Captain") from exc


class CaptainPrivateHoldoutAdapter:
    """Combine authoritative private bytes with an injected Captain evaluator."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        source: CaptainPrivateHoldoutSourcePort,
        evaluator: CaptainPrivateHoldoutEvaluationPort,
    ) -> None:
        if evaluator is None:
            raise ValueError("Captain holdout evaluator is required")
        self._job = job
        self._resolver = CaptainPrivateHoldoutResolver(job=job, source=source)
        self._evaluator = evaluator

    async def resolve(
        self,
        reference: PrivateHoldoutRef,
    ) -> ResolvedFactoryHoldoutCase:
        return await self._resolver.resolve(reference)

    async def evaluate(
        self,
        reference: PrivateHoldoutRef,
        result: Any,
        assertion_ids: tuple[str, ...],
    ) -> FactoryHoldoutEvaluationReceiptV1:
        if reference not in self._job.private_holdout_refs:
            raise ValueError("private holdout reference is not authorized by Captain")
        if assertion_ids != self._job.acceptance_assertion_ids:
            raise ValueError("holdout evaluation must use exactly Captain's assertions")
        receipt = await self._evaluator.evaluate(reference, result, assertion_ids)
        if not isinstance(receipt, FactoryHoldoutEvaluationReceiptV1):
            raise ValueError("Captain holdout evaluator returned an untyped receipt")
        if receipt.holdout_ref != reference or receipt.assertion_ids != assertion_ids:
            raise ValueError("Captain holdout receipt is not bound to the requested case")
        return receipt
