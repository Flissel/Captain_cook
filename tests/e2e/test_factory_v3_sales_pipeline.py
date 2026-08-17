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

    assert run.codex_sessions == 0
    assert run.minibook_status == "awaiting_user_input"

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
    assert run.optional_tool_policy_evidence == {
        "industry_signal_reader": "typed_optional_gap",
    }
    assert run.external_write_attempts == 1
    assert run.external_write_blocks == 1
    assert run.child_sentinel_secret_seen is False
    assert run.executed_commands == (
        ("python", "-m", "compileall", "src"),
        ("python", "-m", "pytest"),
    )
    assert run.real_case_handoff_trace == (
        "account_researcher",
        "opportunity_qualifier",
        "deal_strategist",
        "executive_brief_writer",
    )
    assert run.verified_public_assertion_ids == (
        "assert-2b1d7c6a3666",
        "assert-7f77e654f907",
        "assert-b84ce004c78b",
        "assert-cc0a96c5fc02",
        "assert-9e6cdab86303",
        "assert-9a5995ebfcb8",
        "assert-3e8de9d245ae",
        "assert-3153de4c492a",
    )
    assert run.public_assertion_sources == {
        "assert-2b1d7c6a3666": ("Acceptance outcomes", "exact_team"),
        "assert-7f77e654f907": ("Acceptance outcomes", "required_tools"),
        "assert-b84ce004c78b": ("Real cases", "sales_pipeline"),
        "assert-cc0a96c5fc02": ("Integrations", "account_snapshot_reader"),
        "assert-9e6cdab86303": (
            "Integrations",
            "opportunity_scoring_rules",
        ),
        "assert-9a5995ebfcb8": ("Integrations", "industry_signal_reader"),
        "assert-3e8de9d245ae": (
            "Security requirements",
            "only_opaque_credential_references_may_cross_the_action_boundary",
        ),
        "assert-3153de4c492a": (
            "Security requirements",
            "external_writes_are_forbidden",
        ),
    }
    assert run.minibook_readback_verified
    assert run.captain_readback_verified
    assert run.captain_sealed_receipt_verified
    assert run.credential_refs == ("credential://tenant-a/crm-readonly",)
    assert run.build_credential_refs == ("credential://tenant-a/crm-readonly",)
    assert "raw-secret-value" not in run.serialized_action_payloads


@pytest.mark.parametrize(
    ("variant", "status", "minibook_status", "captain_status", "failure_code"),
    (
        ("one_agent", "blocked", "blocked", "failed", "topology_drift"),
        ("null_output", "blocked", "blocked", "failed", "codex_null_output"),
        ("timeout", "blocked", "blocked", "failed", "codex_timeout"),
        (
            "receipt_only",
            "failed",
            "failed",
            "failed",
            "internal_error",
        ),
        (
            "tampered_cas",
            "blocked",
            "succeeded",
            "failed",
            "captain_cas_readback_rejected",
        ),
    ),
)
@pytest.mark.asyncio
async def test_factory_v3_never_marks_invalid_build_successful(
    factory_v3_harness: FactoryV3Harness,
    variant: str,
    status: str,
    minibook_status: str,
    captain_status: str,
    failure_code: str,
) -> None:
    result = await factory_v3_harness.run_variant(variant)

    assert result.status == status
    assert result.minibook_status == minibook_status
    assert result.captain_status == captain_status
    assert result.failure_code == failure_code
    assert result.failure_summary
    assert result.tamper_performed is (variant == "tampered_cas")
    assert factory_v3_harness.captain_success_events == ()
    assert len(factory_v3_harness.captain_failure_events) == 1
