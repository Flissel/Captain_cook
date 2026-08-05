from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agenten.agent_factory.capability_controlled_recovery import (
    DurableGatewayFactoryLiveEffectLedger,
    FactoryLiveControlledRecoveryPort,
)
from agenten.agent_factory.capability_v3_evidence_bridge import (
    PackageCV3CapabilityEvidenceBackend,
)
from agenten.agent_factory.production_evidence_composition import (
    FACTORY_SKILL_RELEASED_AT,
    DirectoryReleasedSkillSource,
    GatewayCapabilityV3Authority,
    ProductionV3EvidenceConfigurationError,
    ProductionV3EvidenceExternalPorts,
    build_production_v3_evidence_backend_from_environment,
    load_production_v3_evidence_settings,
)
from agenten.agent_factory.team_execution import TeamExecutionCandidateAdapter
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillStep,
    released_skill_capability_matches_job,
)
from gateway.factory_repository import GatewayFactoryRepository


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "TEST_MARIADB_DSN": "mysql://captain:secret@127.0.0.1:3307/captain_test",
        "CAPTAIN_RUNTIME_ARTIFACT_ROOT": str(tmp_path / "cas"),
        "CAPTAIN_FACTORY_SKILL_ROOT": str(
            Path(__file__).parents[2] / "agenten" / "agent_factory" / "skills"
        ),
        "CAPTAIN_FACTORY_WORKSPACE_REF": "workspace://captain-live-demo",
        "CAPTAIN_FACTORY_MODEL": "gpt-5.2",
        "CAPTAIN_FACTORY_PROVIDER": "openai",
        "CAPTAIN_FACTORY_MAX_COST_USD": "12.00",
        "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD": "1.25",
        "CAPTAIN_FACTORY_RUNTIME_SECONDS": "900",
    }


def _external_ports() -> ProductionV3EvidenceExternalPorts:
    return ProductionV3EvidenceExternalPorts(
        candidate_provider=Mock(name="candidate_provider"),
        candidate_attestation=Mock(name="candidate_attestation"),
        model_client_for=Mock(name="model_client_for"),
        pricing_source=Mock(name="pricing_source"),
        holdout_source=Mock(name="holdout_source"),
        holdout_evaluator=Mock(name="holdout_evaluator"),
        n8n_adapter=Mock(name="n8n_adapter"),
        n8n_authority=Mock(name="n8n_authority"),
        tools={},
    )


def test_settings_require_only_explicit_production_configuration(tmp_path: Path) -> None:
    settings = load_production_v3_evidence_settings(_environment(tmp_path))

    assert settings.database_name == "captain_test"
    assert settings.gateway_host in {"127.0.0.1", "localhost", "::1"}
    assert settings.execution_policy.required_live_runs == 3
    assert settings.execution_policy.live_execution is True
    assert settings.execution_policy.max_cost_usd == Decimal("12.00")
    assert settings.max_cost_per_call == Decimal("1.25")
    assert settings.artifact_root == (tmp_path / "cas").resolve()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("TEST_MARIADB_DSN", ""),
        ("TEST_MARIADB_DSN", "mysql://captain:secret@db.example/captain_test"),
        ("TEST_MARIADB_DSN", "mysql://captain:secret@127.0.0.1/production"),
        ("CAPTAIN_RUNTIME_ARTIFACT_ROOT", ""),
        ("CAPTAIN_FACTORY_WORKSPACE_REF", "C:/unsafe"),
        ("CAPTAIN_FACTORY_MAX_COST_USD", "unknown"),
    ),
)
def test_settings_fail_closed_with_todo_tool(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    environ = _environment(tmp_path)
    environ[name] = value

    with pytest.raises(ProductionV3EvidenceConfigurationError, match="TODO_TOOL.v1"):
        load_production_v3_evidence_settings(environ)


def test_builder_composes_gateway_cas_team_execution_and_recovery_without_effects(
    tmp_path: Path,
) -> None:
    fake_store = SimpleNamespace()
    store_factory = Mock(return_value=fake_store)
    ports = _external_ports()

    runtime = build_production_v3_evidence_backend_from_environment(
        _environment(tmp_path),
        external_ports=ports,
        gateway_store_factory=store_factory,
        clock=lambda: NOW,
    )

    assert isinstance(runtime.backend, PackageCV3CapabilityEvidenceBackend)
    assert isinstance(runtime.context.authority, GatewayCapabilityV3Authority)
    assert isinstance(runtime.context.team_execution, TeamExecutionCandidateAdapter)
    assert isinstance(runtime.context.controlled_recovery, FactoryLiveControlledRecoveryPort)
    assert isinstance(
        runtime.controlled_recovery_ledger,
        DurableGatewayFactoryLiveEffectLedger,
    )
    assert runtime.context.artifact_store.root == (tmp_path / "cas").resolve()
    store_factory.assert_called_once()
    ports.model_client_for.assert_not_called()
    ports.candidate_provider.assert_not_called()
    ports.candidate_attestation.assert_not_called()


def test_gateway_v3_authority_reuses_repository_lease_replay_guard() -> None:
    store = Mock(name="gateway_store")
    authority = GatewayCapabilityV3Authority(store)
    lease = Mock(name="lease")

    with patch.object(GatewayFactoryRepository, "record_lease") as record_lease:
        authority.record_lease(lease)

    record_lease.assert_called_once_with(lease)
    store.record_factory_lease.assert_not_called()


def test_released_skill_metadata_is_stable_across_distinct_factory_jobs() -> None:
    source = DirectoryReleasedSkillSource(
        Path(__file__).parents[2] / "agenten" / "agent_factory" / "skills"
    )
    step = next(iter(FactorySkillStep))
    first = SimpleNamespace(
        required_capability="enterprise_sales_pipeline_briefing_team",
        occurred_at=NOW,
    )
    second = SimpleNamespace(
        required_capability="enterprise_sales_pipeline_briefing_team",
        occurred_at=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
    )

    assert source.released_for(first, step).released_at == FACTORY_SKILL_RELEASED_AT
    assert source.released_for(second, step).released_at == FACTORY_SKILL_RELEASED_AT
    released = source.released_for(first, FactorySkillStep.EXECUTE_TEAM)
    assert released.version == 2
    assert released.capability == "factory_workflow"
    assert released.content_ref.uri == "artifact://released-skills/captain-factory-execute-team/v2"
    assert released.content_ref.media_type == "application/json"


def test_generic_factory_workflow_skill_is_compatible_with_a_specific_job() -> None:
    assert released_skill_capability_matches_job(
        "factory_workflow", "enterprise_sales_pipeline_briefing_team"
    )
    assert released_skill_capability_matches_job(
        "enterprise_sales_pipeline_briefing_team",
        "enterprise_sales_pipeline_briefing_team",
    )
    assert not released_skill_capability_matches_job(
        "unrelated_workflow", "enterprise_sales_pipeline_briefing_team"
    )


def test_builder_fails_closed_when_external_provider_ports_are_absent(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ProductionV3EvidenceConfigurationError,
        match="capability=production_v3_external_ports",
    ):
        build_production_v3_evidence_backend_from_environment(
            _environment(tmp_path),
            external_ports=None,
            gateway_store_factory=Mock(return_value=SimpleNamespace()),
            clock=lambda: NOW,
        )


def test_builder_checks_released_skills_before_gateway_connection(
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    environ["CAPTAIN_FACTORY_SKILL_ROOT"] = str(tmp_path / "missing-skills")
    store_factory = Mock()

    with pytest.raises(
        ProductionV3EvidenceConfigurationError,
        match="released_skill:captain-factory-discover",
    ):
        build_production_v3_evidence_backend_from_environment(
            environ,
            external_ports=_external_ports(),
            gateway_store_factory=store_factory,
            clock=lambda: NOW,
        )

    store_factory.assert_not_called()


def test_external_port_bundle_rejects_missing_authority() -> None:
    with pytest.raises(ValueError, match="n8n_authority"):
        ProductionV3EvidenceExternalPorts(
            candidate_provider=Mock(),
            candidate_attestation=Mock(),
            model_client_for=Mock(),
            pricing_source=Mock(),
            holdout_source=Mock(),
            holdout_evaluator=Mock(),
            n8n_adapter=Mock(),
            n8n_authority=None,  # type: ignore[arg-type]
            tools={},
        )
