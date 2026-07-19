"""Captain release-readiness policy over authoritative Gateway delivery events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gateway.contracts import DeliveryEventEnvelope, E2ERunPayload, project_release


@dataclass(frozen=True)
class ReleaseReadiness:
    """A fail-closed decision that can be persisted by the Gateway authority."""

    ready: bool
    clean_e2e_run_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def evaluate_release_readiness(
    events: Iterable[DeliveryEventEnvelope],
) -> ReleaseReadiness:
    """Require three distinct clean E2E runs with complete provider evidence.

    ``project_release`` intentionally keeps its public projection small. This
    policy is the stricter Captain release fence: every counted E2E record must
    link to one successful Codex session, sealed artifact, deployment, and live
    validation in its own fenced batch.
    """

    history = tuple(events)
    projection = project_release(history)
    clean_ids = projection.clean_e2e_run_ids
    reasons: list[str] = []
    if projection.status != "ready":
        reasons.append("three_clean_e2e_runs_required")

    counted: dict[str, DeliveryEventEnvelope] = {}
    for event in sorted(history, key=lambda item: (item.occurred_at, item.event_id.int)):
        if (
            isinstance(event.payload, E2ERunPayload)
            and event.payload.e2e_run_id in clean_ids
            and event.payload.clean
            and event.payload.trace_complete
        ):
            counted.setdefault(event.payload.e2e_run_id, event)
    for e2e_id in clean_ids:
        event = counted[e2e_id]
        batch_id = event.trace.batch_id
        batch_events = tuple(
            candidate for candidate in history if candidate.trace.batch_id == batch_id
        )
        event_types = {candidate.event_type for candidate in batch_events}
        if not any(
            candidate.event_type == "codex_session_finished"
            and getattr(candidate.payload, "outcome", None) == "succeeded"
            for candidate in batch_events
        ):
            reasons.append(f"e2e:{e2e_id}:codex_completion_missing_or_failed")
        if "artifact_built" not in event_types:
            reasons.append(f"e2e:{e2e_id}:artifact_missing")
        if not any(
            candidate.event_type == "deploy"
            and getattr(candidate.payload, "result", None) == "succeeded"
            for candidate in batch_events
        ):
            reasons.append(f"e2e:{e2e_id}:deployment_missing_or_failed")
        if not any(
            candidate.event_type == "validation_run"
            and getattr(candidate.payload, "passed", None) is True
            and getattr(candidate.payload, "layer", None) == "live"
            for candidate in batch_events
        ):
            reasons.append(f"e2e:{e2e_id}:validation_missing_or_failed")

    return ReleaseReadiness(
        ready=not reasons,
        clean_e2e_run_ids=clean_ids,
        reasons=tuple(reasons),
    )
