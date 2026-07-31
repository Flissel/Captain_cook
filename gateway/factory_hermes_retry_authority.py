"""Captain-owned, content-addressed authority for one failed Hermes replay."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from agenten.agent_factory.hermes_cli import (
    FactorySkillReplayRecord,
    factory_skill_replay_failure_ref,
)
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_sequence import (
    FactoryHermesReplayRetryAuthorizationV1,
    build_factory_hermes_replay_retry_authorization,
    validate_factory_hermes_replay_retry_authorization,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)


class FilesystemFactoryHermesRetryAuthority:
    """Issue and resolve single-use Captain Hermes retry evidence."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def issue(
        self,
        failed: FactorySkillReplayRecord,
        *,
        now: datetime,
        validity: timedelta = timedelta(hours=2),
        maximum_additional_cost_usd: Decimal = Decimal("0.25"),
        prior_attempt_reserve_usd: Decimal = Decimal("0.20"),
        benchmark_reserve_usd: Decimal = Decimal("0.30"),
        internal_total_cap_usd: Decimal = Decimal("0.75"),
        user_total_cap_eur: Decimal = Decimal("1.00"),
    ) -> FactoryHermesReplayRetryAuthorizationV1:
        invocation = failed.invocation
        if (
            failed.state != "failed"
            or failed.failure_kind != "FactoryDispatchError"
            or failed.resume_ordinal != 0
            or invocation.step is not FactorySkillStep.IMPROVE_TEAM
        ):
            raise FactoryDispatchError(
                "only an original failed improve_team replay can be authorized"
            )
        authorization = build_factory_hermes_replay_retry_authorization(
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            idempotency_key=invocation.idempotency_key,
            lease_id=invocation.lease.lease_id,
            failed_replay_ref=factory_skill_replay_failure_ref(failed),
            issued_at=now,
            expires_at=now + validity,
            maximum_additional_cost_usd=maximum_additional_cost_usd,
            prior_attempt_reserve_usd=prior_attempt_reserve_usd,
            benchmark_reserve_usd=benchmark_reserve_usd,
            internal_total_cap_usd=internal_total_cap_usd,
            user_total_cap_eur=user_total_cap_eur,
        )
        path = self._path_for(invocation.idempotency_key)
        content = authorization.model_dump_json(by_alias=True).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            existing = self._read(path)
            if existing != authorization:
                raise FactoryDispatchError(
                    "Hermes retry authority already exists with different content"
                )
        return authorization

    def active(
        self,
        failed: FactorySkillReplayRecord,
        *,
        requested_invocation: FactorySkillInvocationV1,
        now: datetime,
    ) -> FactoryHermesReplayRetryAuthorizationV1:
        authorization = self._read(
            self._path_for(failed.invocation.idempotency_key)
        )
        try:
            validate_factory_hermes_replay_retry_authorization(
                authorization,
                now=now,
            )
        except ValueError as exc:
            raise FactoryDispatchError("Hermes retry authority is invalid") from exc
        invocation = failed.invocation
        if (
            authorization.job_id != invocation.job_id
            or authorization.correlation_id != invocation.correlation_id
            or authorization.subject_version != invocation.subject_version
            or authorization.attempt != invocation.attempt
            or authorization.invocation_id != invocation.invocation_id
            or authorization.idempotency_key != invocation.idempotency_key
            or authorization.lease_id != invocation.lease.lease_id
            or authorization.step is not invocation.step
            or authorization.failure_kind != failed.failure_kind
            or authorization.failed_replay_ref
            != factory_skill_replay_failure_ref(failed)
            or requested_invocation.idempotency_key != invocation.idempotency_key
        ):
            raise FactoryDispatchError(
                "Hermes retry authority does not match failed replay"
            )
        return authorization

    def _path_for(self, idempotency_key: str) -> Path:
        path = (self._root / f"{idempotency_key}.json").resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise FactoryDispatchError(
                "Hermes retry authority path is outside its root"
            ) from exc
        return path

    @staticmethod
    def _read(path: Path) -> FactoryHermesReplayRetryAuthorizationV1:
        try:
            return FactoryHermesReplayRetryAuthorizationV1.model_validate_json(
                path.read_bytes()
            )
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise FactoryDispatchError("Hermes retry authority is unavailable") from exc
