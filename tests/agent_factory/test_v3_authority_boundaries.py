from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agenten.agent_factory.hermetic_build_authority as hermetic_authority
import agenten.agent_factory.codex_build_execution as codex_execution

from agenten.agent_factory.codex_build_execution import (
    CaptainStaticFactoryRunnerFactory,
    CaptainStaticFactorySourceBundle,
    FactoryCodexRuntimeAuthority,
)
from agenten.agent_factory.hermetic_build_authority import (
    CaptainFactoryV3PackageEvidenceIssuer,
    CaptainStaticFactoryBuildObserver,
    FactoryV3AssertionVerifier,
    minimal_hermetic_subprocess_environment,
)
from agenten.agent_factory.request_observer import (
    CaptainFactoryActionRequestCoordinator,
    FactoryCredentialRequiredObservation,
)
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.worker_failure_projection import (
    CaptainFactoryWorkerFailureCoordinator,
)
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment


def test_v3_authority_implementations_are_production_components() -> None:
    assert CaptainStaticFactoryRunnerFactory.__module__.startswith(
        "agenten.agent_factory"
    )
    assert CaptainStaticFactoryBuildObserver.__module__.startswith(
        "agenten.agent_factory"
    )
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
    assert not hasattr(hermetic_authority, "CaptainHermeticBuildExecutor")


@pytest.mark.asyncio
async def test_static_factory_runner_rejects_network_source_before_host_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malicious bundle must never reach a host interpreter or the network."""

    workspace = (tmp_path / "workspace").resolve()
    host_process_calls = 0

    def reject_host_process(*_args: object, **_kwargs: object) -> None:
        nonlocal host_process_calls
        host_process_calls += 1
        raise AssertionError("host process execution crossed the hermetic boundary")

    monkeypatch.setattr(codex_execution.subprocess, "run", reject_host_process)
    authority = FactoryCodexRuntimeAuthority(
        workspace_root=workspace,
        writable_roots=(
            (workspace / "src").resolve(),
            (workspace / "tests").resolve(),
            (workspace / "evidence").resolve(),
        ),
        allowed_commands=(
            ("python", "-m", "compileall", "src"),
            ("python", "-m", "pytest"),
        ),
        network_allowed=False,
        maximum_provider_cost_usd=1.0,
    )
    bundle = CaptainStaticFactorySourceBundle(
        (
            ("src/team.py", b"def safe_team():\n    return 'bounded'\n"),
            (
                "tests/test_network.py",
                b"import socket\n"
                b"socket.create_connection(('example.com', 443), timeout=1)\n",
            ),
        )
    )

    with pytest.raises(FactoryDispatchError, match="restricted Python"):
        runner = CaptainStaticFactoryRunnerFactory(
            authority=authority,
            bundle=bundle,
        )(
            session_id="session",
            state_path=tmp_path / "state.json",
            journal_path=tmp_path / "journal.jsonl",
            maximum_runtime_seconds=10,
            deadline_at=datetime.now(timezone.utc),
        )
        await runner.run(
            AuthorizedCodexRun(
                workspace=workspace,
                command=("codex", "exec", "--json", "bounded"),
                environment=FrozenEnvironment(
                    {
                        "FACTORY_CREDENTIAL_REF": (
                            "credential://tenant-a/crm-readonly"
                        ),
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    }
                ),
            )
        )

    assert host_process_calls == 0


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
