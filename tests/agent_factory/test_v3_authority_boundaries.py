from __future__ import annotations

import os
from pathlib import Path

from agenten.agent_factory.hermetic_build_authority import (
    CaptainFactoryV3PackageEvidenceIssuer,
    CaptainHermeticBuildExecutor,
    FactoryV3AssertionVerifier,
    minimal_hermetic_subprocess_environment,
)
from agenten.agent_factory.request_observer import (
    CaptainFactoryActionRequestCoordinator,
    FactoryCredentialRequiredObservation,
)
from agenten.agent_factory.worker_failure_projection import (
    CaptainFactoryWorkerFailureCoordinator,
)


def test_v3_authority_implementations_are_production_components() -> None:
    assert CaptainHermeticBuildExecutor.__module__.startswith("agenten.agent_factory")
    assert CaptainFactoryV3PackageEvidenceIssuer.__module__.startswith(
        "agenten.agent_factory"
    )
    assert FactoryV3AssertionVerifier.__module__.startswith("agenten.agent_factory")
    assert CaptainFactoryActionRequestCoordinator.__module__.startswith(
        "agenten.agent_factory"
    )
    assert FactoryCredentialRequiredObservation.__module__.startswith(
        "agenten.agent_factory"
    )
    assert CaptainFactoryWorkerFailureCoordinator.__module__.startswith(
        "agenten.agent_factory"
    )


def test_hermetic_subprocess_environment_excludes_unrelated_host_secrets(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FACTORY_SENTINEL_SECRET", "must-not-cross")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "must-not-cross-either")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    environment = minimal_hermetic_subprocess_environment(
        credential_ref="credential://tenant-a/crm-readonly",
        host_environment=os.environ,
        temporary_directory=Path(os.environ.get("TEMP", ".")),
    )

    assert environment["FACTORY_CREDENTIAL_REF"] == (
        "credential://tenant-a/crm-readonly"
    )
    assert "PATH" in environment
    assert "FACTORY_SENTINEL_SECRET" not in environment
    assert "UNRELATED_API_TOKEN" not in environment
