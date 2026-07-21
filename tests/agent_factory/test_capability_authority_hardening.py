from __future__ import annotations

from uuid import UUID

import pytest

from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from agenten.delivery.projector import ProjectionResult
from tests.agent_factory.test_state_machine import v2_job
from tests.integration.test_to_be_built_capability_factory import (
    CORRELATION_ID,
    _harness,
)


@pytest.mark.parametrize("outcome", ("busy", "quarantined"))
@pytest.mark.asyncio
async def test_projection_rebuild_fails_closed_for_uncommitted_outcome(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None

    def blocked_rebuild(events):
        return [
            ProjectionResult(event_id=str(event.event_id), outcome=outcome)
            for event in events
        ]

    monkeypatch.setattr(harness.projector, "rebuild", blocked_rebuild)

    with pytest.raises(RuntimeError, match="Minibook projection rebuild did not commit"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )


@pytest.mark.asyncio
async def test_n8n_catalog_authority_is_reused_and_restart_safe(tmp_path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    created = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    authority = harness.gateway.catalog_records[created.capability_id]
    workflow_ref = ArtifactRef(
        uri=f"artifact://n8n-workflow/{'9' * 64}",
        sha256="9" * 64,
        media_type="application/json",
    )
    harness.gateway.catalog_records[created.capability_id] = authority.model_copy(
        update={
            "integration_intents": (IntegrationIntent.N8N,),
            "tool_contracts": ("n8n/workflow.json",),
            "promoted_capability": authority.promoted_capability.model_copy(
                update={"tool_refs": (workflow_ref,)}
            ),
        }
    )
    creation_effects = tuple(harness.creation.submission_effects)
    publication_effects = tuple(harness.gateway.release_effects)
    reuse_correlation = UUID("06db74bc-0672-4fab-83ad-a05933607f18")

    reused = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=reuse_correlation,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    resumed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=reuse_correlation,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert reused.execution_mode == "reused"
    assert resumed.execution_mode == "reused"
    assert resumed.execution_command_id == reused.execution_command_id
    assert resumed.execution_result_id == reused.execution_result_id
    assert resumed.projection_event_ids == reused.projection_event_ids
    assert resumed.minibook_projection_verified is True
    assert tuple(harness.creation.submission_effects) == creation_effects
    assert tuple(harness.gateway.release_effects) == publication_effects


@pytest.mark.parametrize(
    ("integration_intents", "tool_contracts"),
    (
        ((IntegrationIntent.N8N,), ()),
        ((), ("n8n/workflow.json",)),
        ((IntegrationIntent.N8N,), ("adapters/workflow.py",)),
    ),
)
def test_n8n_catalog_authority_rejects_incomplete_contract_pair(
    integration_intents,
    tool_contracts,
) -> None:
    from gateway.capability_catalog import (
        CapabilityCatalogRecord,
        GatewayCapabilityCatalog,
    )
    from tests.gateway.test_capability_catalog import _CatalogRepository, release_request

    record = CapabilityCatalogRecord.from_release(release_request(), catalog_fence=1)
    record = record.model_copy(
        update={
            "integration_intents": integration_intents,
            "tool_contracts": tool_contracts,
        }
    )
    assert GatewayCapabilityCatalog(_CatalogRepository(record)).compatible_record(v2_job()) is None


def test_generated_adapter_contract_is_preserved_for_catalog_reuse() -> None:
    from gateway.capability_catalog import (
        CapabilityCatalogRecord,
        GatewayCapabilityCatalog,
    )
    from tests.gateway.test_capability_catalog import _CatalogRepository, release_request

    record = CapabilityCatalogRecord.from_release(release_request(), catalog_fence=1)
    descriptor_ref = ArtifactRef(
        uri=f"artifact://adapter/{'8' * 64}",
        sha256="8" * 64,
        media_type="application/json",
    )
    record = record.model_copy(
        update={
            "tool_contracts": ("adapters/execution-team.json",),
            "promoted_capability": record.promoted_capability.model_copy(
                update={"tool_refs": (descriptor_ref,)}
            ),
        }
    )

    assert (
        GatewayCapabilityCatalog(_CatalogRepository(record)).compatible_record(v2_job())
        == record
    )
