from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.agent_factory.business_benchmark_bootstrap import (
    ProductionBusinessBenchmarkBootstrapConfig,
)
from gateway.business_benchmark_composition import (
    GatewayBusinessBenchmarkCompositionAuthority,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
LOCAL_DSN = "mariadb://captain:redacted@127.0.0.1:3306/captain_test"


def _config(tmp_path: Path) -> ProductionBusinessBenchmarkBootstrapConfig:
    authority = tmp_path / ".captain-cook" / "private" / "business-benchmarks"
    return ProductionBusinessBenchmarkBootstrapConfig(
        seed_version_id="business-benchmark-demo-2026-07",
        authority_root=authority,
        cas_root=authority / "cas",
        private_suite_root=authority / "suites",
        evidence_store_root=(
            tmp_path
            / ".captain-cook"
            / "evidence"
            / "business-benchmarks"
            / "run"
            / "captain"
            / "receipts"
        ),
        human_review_root=authority / "human-review",
        replay_root=authority / "runtime-state" / "replay",
        provider_state_root=authority / "runtime-state" / "provider-state",
        human_review_timeout_seconds=0,
    )


def test_gateway_authority_constructs_all_captain_ports_from_one_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = object()
    store = SimpleNamespace(name="captain-test-store")
    observed: list[object] = []

    monkeypatch.setattr(
        "gateway.business_benchmark_composition.MariaDBStorage",
        lambda dsn: observed.append(dsn) or storage,
    )
    monkeypatch.setattr(
        "gateway.business_benchmark_composition.GatewayStore",
        lambda selected: observed.append(selected) or store,
    )

    authority = GatewayBusinessBenchmarkCompositionAuthority(LOCAL_DSN)

    assert observed == [LOCAL_DSN, storage]
    assert authority.repository._store is store
    assert authority.leases._store is store
    assert authority.budget._store is store


@pytest.mark.parametrize(
    "dsn",
    (
        "mariadb://captain:redacted@127.0.0.1:3306/ledger",
        "mariadb://captain:redacted@db:3306/captain_test",
    ),
)
def test_gateway_authority_rejects_non_isolated_database_before_connect(
    dsn: str,
) -> None:
    with pytest.raises(ValueError, match="local.*captain_test"):
        GatewayBusinessBenchmarkCompositionAuthority(dsn)


def test_gateway_authority_injects_repository_catalog_leases_and_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SimpleNamespace(name="captain-test-store")
    monkeypatch.setattr(
        "gateway.business_benchmark_composition.MariaDBStorage", lambda _dsn: object()
    )
    monkeypatch.setattr(
        "gateway.business_benchmark_composition.GatewayStore", lambda _storage: store
    )
    captured: dict[str, object] = {}

    def compose(settings, *, config, ports):
        captured.update(settings=settings, config=config, ports=ports)
        return "composition"

    monkeypatch.setattr(
        "gateway.business_benchmark_composition.compose_production_business_benchmark_composition",
        compose,
    )
    settings = SimpleNamespace(profile="claims")
    config = _config(tmp_path)
    executor_builder = object()
    policy_builder = object()
    authority = GatewayBusinessBenchmarkCompositionAuthority(LOCAL_DSN)

    result = authority.compose(
        settings,  # type: ignore[arg-type]
        config=config,
        executor_builder=executor_builder,  # type: ignore[arg-type]
        execution_policy_builder=policy_builder,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert result == "composition"
    ports = captured["ports"]
    assert ports.gateway_repository is authority.repository
    assert ports.released_skills is authority.repository
    assert ports.leases is authority.leases
    assert ports.executor_builder is executor_builder
    assert ports.execution_policy_builder is policy_builder
    assert ports.clock() == NOW
    assert captured["config"] is config
