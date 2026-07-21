"""Build sanitized live-gate evidence exclusively from Gateway-owned records.

The collector is deliberately read-only.  It does not accept caller supplied
session IDs, costs, or evidence references and it never imports test harnesses.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.factory_live_entrypoint import (
    FactoryLiveObservedEvidence,
    FactoryLiveProviderTrace,
    FactoryLiveRecoveryEvidence,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectRecord,
)
from agenten.agent_factory.service import FactoryWorkflowArtifact
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    TeamExecutionEvidenceV1,
)


class FactoryLiveEvidenceCollectionError(ValueError):
    """Sanitized fail-closed rejection of incomplete Gateway evidence."""


class FactoryLiveEvidenceRepository(Protocol):
    """Read subset implemented by ``GatewayFactoryRepository``."""

    def job(self, job_id: UUID) -> object: ...

    def workflow_artifacts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryWorkflowArtifact, ...]: ...

    def workflow_budget_projection(
        self,
        job_id: UUID,
    ) -> FactoryBudgetProjection | None: ...

    def workflow_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]: ...


class FactoryLiveEffectHistory(Protocol):
    """Read subset implemented by ``GatewayFactoryLiveEffectLedger``."""

    def history(self, job_id: UUID) -> tuple[FactoryLiveEffectRecord, ...]: ...


class GatewayFactoryLiveObservedEvidenceCollector:
    """Collect one report payload from the Gateway's four authority streams."""

    def __init__(
        self,
        *,
        repository: FactoryLiveEvidenceRepository,
        effect_history: FactoryLiveEffectHistory,
    ) -> None:
        self._repository = repository
        self._effect_history = effect_history

    def collect(self, job_id: UUID) -> FactoryLiveObservedEvidence:
        try:
            job = self._repository.job(job_id)
            artifacts = tuple(self._repository.workflow_artifacts(job_id))
            budget = self._repository.workflow_budget_projection(job_id)
            receipts = tuple(self._repository.workflow_usage_receipts(job_id))
            effects = tuple(self._effect_history.history(job_id))
        except FactoryLiveEvidenceCollectionError:
            raise
        except Exception as exc:
            raise _incomplete("Gateway read") from exc

        if not isinstance(job, AgentFactoryJobV3) or job.job_id != job_id:
            raise _binding("job")
        inventory = self._inventory(job, artifacts)
        runs = self._current_runs(job, artifacts)
        selected_receipts = self._receipts(job, runs, receipts)
        checked_budget = self._budget(job, budget, selected_receipts)
        traces, recovery = self._traces(job, runs, selected_receipts, effects)
        self._reject_untyped_n8n_claims(runs)

        try:
            return FactoryLiveObservedEvidence(
                context7_provenance_digest=inventory.documentation_refs[0].sha256,
                provider_traces=traces,
                gateway_total_cost_usd=checked_budget.consumed_usd,
                recovery=recovery,
                n8n_evidence=None,
            )
        except (TypeError, ValueError) as exc:
            raise FactoryLiveEvidenceCollectionError(
                "authoritative factory evidence is invalid"
            ) from exc

    @staticmethod
    def _inventory(
        job: AgentFactoryJobV3,
        artifacts: tuple[FactoryWorkflowArtifact, ...],
    ) -> CodebaseInventoryV1:
        inventories = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodebaseInventoryV1)
        )
        if len(inventories) != 1 or len(inventories[0].documentation_refs) != 1:
            raise _incomplete("Context7 provenance")
        inventory = inventories[0]
        _require_artifact_binding(job, inventory)
        return inventory

    @staticmethod
    def _current_runs(
        job: AgentFactoryJobV3,
        artifacts: tuple[FactoryWorkflowArtifact, ...],
    ) -> tuple[TeamExecutionEvidenceV1, ...]:
        all_runs = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
        )
        if not all_runs:
            raise _incomplete("live workflow runs")
        for run in all_runs:
            _require_artifact_binding(job, run)
        current_attempt = max(run.attempt for run in all_runs)
        runs = tuple(
            sorted(
                (run for run in all_runs if run.attempt == current_attempt),
                key=lambda run: run.run_number,
            )
        )
        required = job.execution_policy.required_live_runs
        if (
            len(runs) != required
            or tuple(run.run_number for run in runs) != tuple(range(1, required + 1))
            or any(
                run.status != "succeeded"
                or run.execution_outcome.status != "succeeded"
                or len(run.usage_receipt_refs) != 1
                for run in runs
            )
        ):
            raise _incomplete("successful live workflow runs")
        _require_unique(
            (run.invocation_id for run in runs),
            "Hermes session",
        )
        _require_unique(
            (run.invocation.idempotency_key for run in runs),
            "workflow invocation",
        )
        return runs

    @staticmethod
    def _receipts(
        job: AgentFactoryJobV3,
        runs: tuple[TeamExecutionEvidenceV1, ...],
        receipts: tuple[FactoryUsageReceiptV1, ...],
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        if not receipts:
            raise _incomplete("usage receipts")
        _require_unique((receipt.receipt_id for receipt in receipts), "receipt")
        _require_unique(
            (receipt.reservation_id for receipt in receipts),
            "reservation",
        )
        _require_unique(
            (receipt.evidence_ref for receipt in receipts),
            "usage receipt reference",
        )
        by_ref = {receipt.evidence_ref: receipt for receipt in receipts}
        selected: list[FactoryUsageReceiptV1] = []
        for run in runs:
            reference = run.usage_receipt_refs[0]
            receipt = by_ref.get(reference)
            if receipt is None:
                raise _incomplete("usage receipt coverage")
            if (
                receipt.job_id != job.job_id
                or receipt.correlation_id != job.correlation_id
                or receipt.attempt != run.attempt
                or receipt.invocation_id != run.invocation_id
                or receipt.lease_id != run.invocation.lease.lease_id
                or receipt.model not in job.execution_policy.allowed_models
            ):
                raise _binding("usage receipt")
            selected.append(receipt)
        if set(by_ref) != {run.usage_receipt_refs[0] for run in runs}:
            raise _incomplete("usage receipt cost coverage")
        return tuple(selected)

    @staticmethod
    def _budget(
        job: AgentFactoryJobV3,
        budget: FactoryBudgetProjection | None,
        receipts: tuple[FactoryUsageReceiptV1, ...],
    ) -> FactoryBudgetProjection:
        if budget is None:
            raise _incomplete("budget projection")
        total = sum((receipt.cost_usd for receipt in receipts), Decimal("0"))
        if (
            budget.job_id != job.job_id
            or budget.limit_usd != job.execution_policy.max_cost_usd
        ):
            raise _binding("budget projection")
        if (
            budget.reserved_usd != 0
            or budget.active_reservation_ids
            or budget.consumed_usd != total
            or total > job.execution_policy.max_cost_usd
        ):
            raise FactoryLiveEvidenceCollectionError(
                "authoritative factory cost evidence is incomplete"
            )
        return budget

    @staticmethod
    def _traces(
        job: AgentFactoryJobV3,
        runs: tuple[TeamExecutionEvidenceV1, ...],
        receipts: tuple[FactoryUsageReceiptV1, ...],
        effects: tuple[FactoryLiveEffectRecord, ...],
    ) -> tuple[
        tuple[FactoryLiveProviderTrace, ...],
        FactoryLiveRecoveryEvidence | None,
    ]:
        if not effects:
            raise _incomplete("live-effect history")
        for record in effects:
            request = record.request
            invocation = request.invocation
            if (
                request.job_id != job.job_id
                or request.correlation_id != job.correlation_id
                or request.subject_version != job.subject_version
                or invocation.job_id != request.job_id
                or invocation.correlation_id != request.correlation_id
                or invocation.subject_version != request.subject_version
                or invocation.attempt != request.attempt
                or invocation.idempotency_key != request.idempotency_key
                or invocation.input_ref != request.input_ref
            ):
                raise _binding("live effect")
            outcome = record.outcome
            if outcome is not None and (
                outcome.effect_id != request.effect_id
                or outcome.job_id != request.job_id
                or outcome.correlation_id != request.correlation_id
                or outcome.subject_version != request.subject_version
                or outcome.attempt != request.attempt
            ):
                raise _binding("live-effect outcome")

        successful = tuple(
            record
            for record in effects
            if record.outcome is not None and record.outcome.status == "succeeded"
        )
        provider_records = tuple(
            record
            for record in successful
            if record.request.kind is FactoryLiveEffectKind.PROVIDER
        )
        codex_records = tuple(
            record
            for record in successful
            if record.request.kind is FactoryLiveEffectKind.CODEX
        )
        _require_unique(
            (record.request.invocation.invocation_id for record in codex_records),
            "Codex session",
        )
        _require_unique(
            (record.request.invocation.invocation_id for record in provider_records),
            "Hermes session",
        )
        _require_unique(
            (record.outcome.outcome_id for record in provider_records),
            "provider trace",
        )
        _require_unique(
            (record.outcome.evidence_ref for record in provider_records),
            "budget receipt reference",
        )

        provider_by_invocation: dict[UUID, list[FactoryLiveEffectRecord]] = {}
        for record in provider_records:
            provider_by_invocation.setdefault(
                record.request.invocation.invocation_id,
                [],
            ).append(record)

        traces: list[FactoryLiveProviderTrace] = []
        selected_provider: list[FactoryLiveEffectRecord] = []
        selected_codex: list[FactoryLiveEffectRecord] = []
        for run, receipt in zip(runs, receipts, strict=True):
            matches = provider_by_invocation.get(run.invocation_id, [])
            if len(matches) != 1:
                raise _incomplete("provider live-effect binding")
            provider = matches[0]
            request = provider.request
            if (
                request.invocation != run.invocation
                or request.attempt != run.attempt
                or request.run_id is None
                or request.run_effect_count != 2
                or request.run_effect_index != 2
            ):
                raise _binding("provider live effect")
            group = tuple(
                record for record in effects if record.request.run_id == request.run_id
            )
            if (
                len(group) != 2
                or {record.request.run_effect_index for record in group} != {1, 2}
                or {record.request.kind for record in group}
                != {FactoryLiveEffectKind.CODEX, FactoryLiveEffectKind.PROVIDER}
                or any(
                    record.outcome is None or record.outcome.status != "succeeded"
                    for record in group
                )
            ):
                raise _incomplete("Codex/provider live-effect group")
            codex = next(
                record
                for record in group
                if record.request.kind is FactoryLiveEffectKind.CODEX
            )
            if (
                codex.request.run_effect_index != 1
                or codex.request.run_effect_count != 2
                or codex.request.attempt != run.attempt
            ):
                raise _binding("Codex live effect")
            outcome = provider.outcome
            if outcome is None or outcome.evidence_ref is None:
                raise _incomplete("provider outcome")
            traces.append(
                FactoryLiveProviderTrace(
                    trace_id=str(outcome.outcome_id),
                    codex_session_id=str(codex.request.invocation.invocation_id),
                    hermes_session_id=str(request.invocation.invocation_id),
                    provider=receipt.provider,
                    model=receipt.model,
                    status="succeeded",
                    cost_usd=receipt.cost_usd,
                    usage_receipt_ref=receipt.evidence_ref,
                    # The run claims this immutable ref and the Gateway budget
                    # ledger accepts the same ref in its typed usage receipt.
                    budget_receipt_ref=receipt.evidence_ref,
                )
            )
            selected_provider.append(provider)
            selected_codex.append(codex)

        _require_unique((record.request.run_id for record in selected_provider), "run")
        if len(set(selected_provider)) != len(selected_provider):
            raise FactoryLiveEvidenceCollectionError(
                "authoritative factory evidence contains duplicate provider effects"
            )
        if len(set(selected_codex)) != len(selected_codex):
            raise FactoryLiveEvidenceCollectionError(
                "authoritative factory evidence contains duplicate Codex effects"
            )
        if set(provider_records) != set(selected_provider):
            raise _incomplete("provider live-effect coverage")

        recovered = tuple(
            record
            for record in selected_provider
            if record.outcome is not None
            and record.outcome.completion_origin == "recover"
        )
        mode = job.execution_policy.mode.value
        if mode == "demo":
            if recovered:
                raise FactoryLiveEvidenceCollectionError(
                    "authoritative demo evidence contains unexpected recovery"
                )
            recovery = None
        else:
            if len(recovered) != 1 or recovered[0].outcome is None:
                raise FactoryLiveEvidenceCollectionError(
                    "authoritative release recovery evidence is incomplete"
                )
            recovery_ref = recovered[0].outcome.evidence_ref
            if recovery_ref is None:
                raise _incomplete("release recovery evidence")
            recovery = FactoryLiveRecoveryEvidence(
                status="recovered",
                evidence_digest=recovery_ref.sha256,
            )
        return tuple(traces), recovery

    @staticmethod
    def _reject_untyped_n8n_claims(
        runs: tuple[TeamExecutionEvidenceV1, ...],
    ) -> None:
        references = tuple(
            reference
            for run in runs
            for reference in (*run.tool_evidence_refs, *run.workflow_evidence_refs)
        )
        if any("n8n" in reference.uri.lower() for reference in references):
            raise FactoryLiveEvidenceCollectionError(
                "authoritative n8n identifiers are incomplete"
            )


def _require_artifact_binding(
    job: AgentFactoryJobV3,
    artifact: CodebaseInventoryV1 | TeamExecutionEvidenceV1,
) -> None:
    if (
        artifact.job_id != job.job_id
        or artifact.correlation_id != job.correlation_id
        or artifact.subject_version != job.subject_version
    ):
        raise _binding("workflow artifact")


def _require_unique(values: Iterable[object], label: str) -> None:
    identities = tuple(values)
    if len(identities) != len(set(identities)):
        raise FactoryLiveEvidenceCollectionError(
            f"authoritative factory evidence contains duplicate {label} identities"
        )


def _incomplete(label: str) -> FactoryLiveEvidenceCollectionError:
    return FactoryLiveEvidenceCollectionError(
        f"authoritative factory evidence is incomplete: {label}"
    )


def _binding(label: str) -> FactoryLiveEvidenceCollectionError:
    return FactoryLiveEvidenceCollectionError(
        f"authoritative factory evidence binding mismatch: {label}"
    )
