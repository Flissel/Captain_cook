"""Gateway-owned lease issuance for the opt-in Factory dispatch runner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import FactoryJob, FactoryLease, FactoryRole
from agenten.agent_factory.leases import (
    FactoryLeaseDenied,
    issue_factory_lease,
    validate_factory_lease,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.skill_sequence import FactoryRuntimeRetryAuthorizationV1
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.validation.contracts import WorkBatch
from gateway.contracts import FactoryJobProjection, FactoryWriteReceipt


_ACTION_ROLES: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.SUBMIT_FORGE_JOB: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_BUILD_VALIDATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}


class GatewayFactoryLeaseStorePort(Protocol):
    def factory_job(self, job_id: UUID) -> FactoryJobProjection: ...

    def record_factory_lease(self, lease: FactoryLease) -> FactoryWriteReceipt: ...

    def bundle(self, batch_id: str) -> dict[str, object]: ...


class GatewayNextActionLeaseIssuer:
    """Issue the narrow lease that Gateway independently admits as next.

    The Gateway store remains decisive: ``record_factory_lease`` rebuilds the
    projection under lock and rejects a role that is not the current action.
    An n8n ToolIntegrator grant additionally requires the exact job-derived
    released WorkBatch; an operator-provided job identifier alone is not
    authority.
    """

    def __init__(
        self,
        *,
        store: GatewayFactoryLeaseStorePort,
        workspace_namespace: str,
        n8n_work_batches: Mapping[UUID, str] | None = None,
    ) -> None:
        namespace = workspace_namespace.strip().strip("/")
        if not namespace or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in namespace
        ):
            raise ValueError("workspace_namespace is invalid")
        self._store = store
        self._workspace_namespace = namespace
        self._n8n_work_batches = dict(n8n_work_batches or {})
        self._authorized: dict[
            tuple[UUID, FactoryRole, int], tuple[FactoryJob, FactoryLease]
        ] = {}
        self._recovery_authorized: dict[
            tuple[UUID, FactoryRole, int],
            tuple[FactoryJob, FactoryLease, FactoryRuntimeRetryAuthorizationV1],
        ] = {}

    def ensure_for(
        self,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
        now: datetime,
    ) -> FactoryLease:
        expected_role = _ACTION_ROLES.get(action.kind)
        if expected_role is not role or action.attempt < 1:
            raise FactoryLeaseDenied(
                "Factory action does not authorize the requested role"
            )
        expected_intent = self._integration_intent(job, action, role)
        workspace_prefix = self._workspace_prefix(job, action)
        active = self._exact_active_lease(
            job=job,
            action=action,
            role=role,
            integration_intent=expected_intent,
            workspace_prefix=workspace_prefix,
            now=now,
        )
        lease = active
        if lease is None:
            lease = issue_factory_lease(
                job=job,
                role=role,
                attempt=action.attempt,
                workspace_ref=workspace_prefix + self._issuance_epoch(now),
                now=now,
                integration_intent=expected_intent,
            )
        try:
            self._store.record_factory_lease(lease)
        except Exception as exc:
            raise FactoryLeaseDenied(
                "Gateway rejected the next-action Factory lease"
            ) from exc
        persisted_bundle = self._store.factory_job(job.job_id)
        if lease not in persisted_bundle.leases:
            raise FactoryLeaseDenied(
                "Gateway did not persist the exact next-action Factory lease"
            )
        validated = validate_factory_lease(
            lease,
            job=job,
            role=role,
            attempt=action.attempt,
            now=now,
        )
        self._authorized[(job.job_id, role, action.attempt)] = (job, validated)
        return validated

    def active(
        self,
        job: FactoryJob,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ) -> FactoryLease:
        """Resolve only the lease immediately authorized for this dispatcher."""

        key = (job.job_id, role, attempt)
        recovery = self._recovery_authorized.pop(key, None)
        if recovery is not None:
            bound_job, lease, authorization = recovery
            if (
                bound_job != job
                or authorization.job_id != job.job_id
                or authorization.correlation_id != job.correlation_id
                or authorization.subject_version != job.subject_version
                or authorization.attempt != attempt
                or authorization.lease_id != lease.lease_id
                or authorization.workspace_ref != lease.workspace_ref
                or now < authorization.issued_at
                or now >= authorization.expires_at
            ):
                raise FactoryLeaseDenied(
                    "Factory recovery lease authority is stale or mismatched"
                )
            return lease
        authorized = self._authorized.pop(key, None)
        if authorized is None or authorized[0] != job:
            raise FactoryLeaseDenied(
                "Factory dispatcher lease was not immediately authorized"
            )
        lease = authorized[1]
        try:
            # Gateway rechecks next_action under lock even for this idempotent
            # replay, closing the ensure-to-dispatch race.
            self._store.record_factory_lease(lease)
        except Exception as exc:
            raise FactoryLeaseDenied(
                "Gateway rejected the immediately authorized Factory lease"
            ) from exc
        return validate_factory_lease(
            lease,
            job=job,
            role=role,
            attempt=attempt,
            now=now,
        )

    def ensure_recovery_for(
        self,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
        now: datetime,
        authorization: FactoryRuntimeRetryAuthorizationV1,
    ) -> FactoryLease:
        """Recover the original expired lease identity under exact successor authority."""

        if (
            _ACTION_ROLES.get(action.kind) is not role
            or role is not FactoryRole.TOOL_INTEGRATOR
            or action.attempt < 1
            or authorization.job_id != job.job_id
            or authorization.correlation_id != job.correlation_id
            or authorization.subject_version != job.subject_version
            or authorization.attempt != action.attempt
            or now < authorization.issued_at
            or now >= authorization.expires_at
        ):
            raise FactoryLeaseDenied(
                "Factory recovery authority does not match the next action"
            )
        matches = [
            lease
            for lease in self._store.factory_job(job.job_id).leases
            if lease.lease_id == authorization.lease_id
            and lease.workspace_ref == authorization.workspace_ref
        ]
        if len(matches) != 1:
            raise FactoryLeaseDenied(
                "Factory recovery authority does not identify one original lease"
            )
        lease = matches[0]
        if lease.expires_at > now:
            raise FactoryLeaseDenied(
                "Factory recovery successor cannot replace an active ordinary lease"
            )
        validated = validate_factory_lease(
            lease,
            job=job,
            role=role,
            attempt=action.attempt,
            now=lease.issued_at,
        )
        self._recovery_authorized[(job.job_id, role, action.attempt)] = (
            job,
            validated,
            authorization,
        )
        return validated

    def _exact_active_lease(
        self,
        *,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
        integration_intent: IntegrationIntent,
        workspace_prefix: str,
        now: datetime,
    ) -> FactoryLease | None:
        bundle = self._store.factory_job(job.job_id)
        stored_job = getattr(bundle, "job", job)
        if stored_job != job:
            raise FactoryLeaseDenied("Gateway Factory job binding changed")
        matches: list[FactoryLease] = []
        for candidate in bundle.leases:
            try:
                validate_factory_lease(
                    candidate,
                    job=job,
                    role=role,
                    attempt=action.attempt,
                    now=now,
                )
            except FactoryLeaseDenied:
                continue
            workspace_matches = candidate.workspace_ref.startswith(
                workspace_prefix
            )
            if action.kind is FactoryActionKind.DISPATCH_AGENT_ARCHITECT:
                # Provisioning writes the initial Architect lease before this
                # runner exists. Gateway already admitted it for this action.
                workspace_matches = True
            if (
                workspace_matches
                and candidate.integration_intent is integration_intent
            ):
                matches.append(candidate)
        if not matches:
            return None
        latest_issued_at = max(candidate.issued_at for candidate in matches)
        latest = [
            candidate
            for candidate in matches
            if candidate.issued_at == latest_issued_at
        ]
        if len(latest) != 1:
            raise FactoryLeaseDenied(
                "Gateway next-action Factory lease is ambiguous"
            )
        return latest[0]

    def _workspace_prefix(self, job: FactoryJob, action: FactoryAction) -> str:
        return (
            f"workspace://{self._workspace_namespace}/{job.job_id}/"
            f"{action.kind.value}/{action.attempt}/"
        )

    @staticmethod
    def _issuance_epoch(now: datetime) -> str:
        return now.strftime("%Y%m%dT%H%M%S%fZ")

    def _integration_intent(
        self,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
    ) -> IntegrationIntent:
        batch_id = self._n8n_work_batches.get(job.job_id)
        if (
            batch_id is None
            or role is not FactoryRole.TOOL_INTEGRATOR
            or action.kind is not FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
        ):
            return IntegrationIntent.NONE
        expected_batch_id = f"renewal-{job.job_id.hex[:24]}"
        try:
            batch = WorkBatch.model_validate(self._store.bundle(batch_id))
        except Exception as exc:
            raise FactoryLeaseDenied(
                "n8n Factory lease requires a released n8n WorkBatch"
            ) from exc
        if (
            batch.batch_id != batch_id
            or batch.batch_id != expected_batch_id
            or batch.target != "n8n"
            or batch.runtime != "n8n"
            or "n8n-builder" not in batch.capability_tags
            or "renewal_context_read" not in batch.subtask_ids
        ):
            raise FactoryLeaseDenied(
                "n8n Factory lease requires a released n8n WorkBatch"
            )
        return IntegrationIntent.N8N


__all__ = ["GatewayNextActionLeaseIssuer"]
