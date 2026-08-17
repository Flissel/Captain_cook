from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agenten.agent_factory.hermetic_build_authority as hermetic_authority
import agenten.agent_factory.codex_build_execution as codex_execution

from agenten.agent_factory.codex_build_execution import (
    CaptainStaticFactoryRunnerFactory,
    CaptainStaticFactorySourceBundle,
    FactoryCodexRuntimeAuthority,
)
from agenten.agent_factory.codex_build_recovery import (
    MAX_FACTORY_CODEX_JSON_ARTIFACT_BYTES,
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


def _static_authority(
    workspace: Path, *, maximum_files: int = 25
) -> FactoryCodexRuntimeAuthority:
    return FactoryCodexRuntimeAuthority(
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
        maximum_files=maximum_files,
    )


async def _run_static_bundle(
    *,
    tmp_path: Path,
    bundle: CaptainStaticFactorySourceBundle,
    maximum_files: int = 25,
    clock: Callable[[], datetime] | None = None,
) -> tuple[Path, object]:
    workspace = (tmp_path / "workspace").resolve()
    factory = CaptainStaticFactoryRunnerFactory(
        authority=_static_authority(workspace, maximum_files=maximum_files),
        bundle=bundle,
        **({} if clock is None else {"clock": clock}),
    )
    runner = factory(
        session_id="session",
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.jsonl",
        maximum_runtime_seconds=10,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    result = await runner.run(
        AuthorizedCodexRun(
            workspace=workspace,
            command=("codex", "exec", "--json", "bounded"),
            environment=FrozenEnvironment(
                {
                    "FACTORY_CREDENTIAL_REF": "credential://tenant-a/crm-readonly",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                }
            ),
        )
    )
    return workspace, result


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
    authority = _static_authority(workspace)
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
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=10),
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


@pytest.mark.asyncio
async def test_static_factory_rejects_assignment_file_count_before_parse_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_calls = 0
    real_parse = codex_execution.ast.parse

    def track_parse(*args: object, **kwargs: object):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(codex_execution.ast, "parse", track_parse)
    bundle = CaptainStaticFactorySourceBundle(
        (
            ("src/extra.py", b"EXTRA = True\n"),
            ("src/team.py", b"def run_sales_pipeline(value):\n    return value\n"),
            ("tests/test_team.py", b"def test_team():\n    assert True\n"),
        )
    )

    with pytest.raises(FactoryDispatchError, match="source bundle"):
        await _run_static_bundle(
            tmp_path=tmp_path,
            bundle=bundle,
            maximum_files=2,
        )

    assert parse_calls == 0
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize(
    "path,content",
    (
        (
            "src/team.py",
            b"MESSAGE = '"
            + (b"a" * (MAX_FACTORY_CODEX_JSON_ARTIFACT_BYTES + 1))
            + b"'\n",
        ),
        (
            "src/team.py",
            b"VALUE = " + (b"[" * 80) + b"0" + (b"]" * 80) + b"\n",
        ),
        (
            "src/team.py",
            b"MESSAGE = '" + (b"a" * (65 * 1024)) + b"'\n",
        ),
        ("src/team.py", b"MESSAGE = '\xff'\n"),
    ),
    ids=("oversized-source", "deep-syntax", "oversized-literal", "invalid-utf8"),
)
@pytest.mark.asyncio
async def test_static_factory_rejects_unsafe_source_before_ast_or_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    content: bytes,
) -> None:
    parse_calls = 0
    real_parse = codex_execution.ast.parse

    def track_parse(*args: object, **kwargs: object):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(codex_execution.ast, "parse", track_parse)
    bundle = CaptainStaticFactorySourceBundle(
        (
            (path, content),
            ("tests/test_team.py", b"def test_team():\n    assert True\n"),
        )
    )

    with pytest.raises(FactoryDispatchError, match="source bundle|restricted Python"):
        await _run_static_bundle(tmp_path=tmp_path, bundle=bundle)

    assert parse_calls == 0
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize(
    "source",
    (
        b"VALUE = " + (b"'a' + " * 80) + b"'a'\n",
        b"VALUE = [" + b",".join(b"0" for _index in range(257)) + b"]\n",
    ),
    ids=("ast-depth", "container-items"),
)
@pytest.mark.asyncio
async def test_static_factory_rejects_bounded_ast_abuse_before_materialization(
    tmp_path: Path,
    source: bytes,
) -> None:
    bundle = CaptainStaticFactorySourceBundle(
        (
            ("src/team.py", source),
            ("tests/test_team.py", b"def test_team():\n    assert True\n"),
        )
    )

    with pytest.raises(FactoryDispatchError, match="restricted Python"):
        await _run_static_bundle(tmp_path=tmp_path, bundle=bundle)

    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "src/\N{GREEK SMALL LETTER ALPHA}.py",
        "src/" + ("a" * 241) + ".py",
    ),
    ids=("unicode-path", "long-path"),
)
def test_static_factory_bundle_rejects_unsafe_paths(unsafe_path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        CaptainStaticFactorySourceBundle(((unsafe_path, b"VALUE = True\n"),))


@pytest.mark.asyncio
async def test_static_factory_checks_deadline_during_bounded_source_validation(
    tmp_path: Path,
) -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    clock_calls = 0

    def expiring_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return deadline - timedelta(seconds=1) if clock_calls < 12 else deadline

    bundle = CaptainStaticFactorySourceBundle(
        (
            (
                "src/team.py",
                b"\n".join(f"VALUE_{index} = {index}".encode() for index in range(100)),
            ),
            ("tests/test_team.py", b"def test_team():\n    assert True\n"),
        )
    )
    workspace = (tmp_path / "workspace").resolve()
    factory = CaptainStaticFactoryRunnerFactory(
        authority=_static_authority(workspace),
        bundle=bundle,
        clock=expiring_clock,
    )
    runner = factory(
        session_id="session",
        state_path=tmp_path / "state.json",
        journal_path=tmp_path / "journal.jsonl",
        maximum_runtime_seconds=10,
        deadline_at=deadline,
    )

    with pytest.raises(FactoryDispatchError, match="deadline"):
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

    assert clock_calls >= 12
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_generated_source_cannot_fabricate_success_evidence(
    tmp_path: Path,
) -> None:
    source = b"def run_sales_pipeline(sales_input):\n    return 'WRONG'\n"
    fabricated_test = b"""\
def test_fabricated_success():
    run_sales_pipeline({"account_id": "acct-42"})
    assert True
    blocked_external_write({"account_id": "acct-42"})
    emit_evidence({"executive_brief": "qualified; next action: Schedule an evidence review", "handoff_trace": ["account_researcher", "opportunity_qualifier", "deal_strategist", "executive_brief_writer"], "handoffs": [{"from": "account_researcher", "to": "opportunity_qualifier"}, {"from": "opportunity_qualifier", "to": "deal_strategist"}, {"from": "deal_strategist", "to": "executive_brief_writer"}], "credential_ref_used": "credential://tenant-a/crm-readonly", "pipeline_status": "passed", "qualification_preserved": True, "next_action_preserved": True, "tool_evidence": {"account_snapshot_reader": {"status": "passed", "typed_schema": True, "test_passed": True, "result": "approved_account_snapshot"}, "opportunity_scoring_rules": {"status": "passed", "typed_schema": True, "test_passed": True, "result": "versioned_published_rule"}}, "optional_tool_policy": {"available_result": "bounded_public_signal", "unavailable_result": "typed_optional_gap"}})
"""
    bundle = CaptainStaticFactorySourceBundle(
        (
            ("src/team.py", source),
            ("tests/test_team.py", fabricated_test),
        )
    )

    with pytest.raises(FactoryDispatchError, match="restricted Python"):
        await _run_static_bundle(tmp_path=tmp_path, bundle=bundle)

    workspace = tmp_path / "workspace"
    assert not (workspace / "test-evidence.json").exists()
    assert not (workspace / "evidence" / "real-case-output.json").exists()


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
