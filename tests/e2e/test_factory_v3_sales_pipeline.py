from __future__ import annotations

import pytest
import pytest_asyncio

from tests.support.factory_v3_harness import FactoryV3Harness


@pytest_asyncio.fixture
async def factory_v3_harness(tmp_path):
    harness = await FactoryV3Harness.create(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_sales_pipeline_brief_builds_exact_team_and_resumes_credential_action(
    factory_v3_harness: FactoryV3Harness,
) -> None:
    run = await factory_v3_harness.submit_sales_pipeline_brief()
    action = await run.wait_for_action("credential_required")

    await run.answer(
        action,
        credential_ref="credential://tenant-a/crm-readonly",
    )
    result = await run.wait_for_result()

    assert result.status == "succeeded"
    assert run.creation_submissions == 1
    assert run.codex_sessions == 1
    assert run.build_effects == 1
    assert run.team_agent_ids == (
        "account_researcher",
        "opportunity_qualifier",
        "deal_strategist",
        "executive_brief_writer",
    )
    assert run.required_tools_with_passing_tests == {
        "account_snapshot_reader",
        "opportunity_scoring_rules",
    }
    assert run.minibook_readback_verified
    assert run.captain_readback_verified
    assert run.credential_refs == ("credential://tenant-a/crm-readonly",)
    assert "raw-secret-value" not in run.serialized_action_payloads


@pytest.mark.parametrize(
    "variant",
    ("one_agent", "null_output", "timeout", "receipt_only", "tampered_cas"),
)
@pytest.mark.asyncio
async def test_factory_v3_never_marks_invalid_build_successful(
    factory_v3_harness: FactoryV3Harness,
    variant: str,
) -> None:
    result = await factory_v3_harness.run_variant(variant)

    assert result.status in {"failed", "blocked"}
    assert factory_v3_harness.captain_success_events == ()
