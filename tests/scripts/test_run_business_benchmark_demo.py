from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-business-benchmark-demo.ps1"
PREFLIGHT = ROOT / "scripts" / "preflight-business-benchmark-demo.py"


def test_runner_contract_is_opt_in_redacted_and_factory_gated() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.startswith("#requires -Version 7")
    assert "[ValidateSet('Plan', 'Build', 'Run')]" in source
    assert "--maximum-usd-per-team', $maximumUsdPerTeam" in source
    assert "$maximumUsdPerTeam = '0.30'" in source
    assert "$maximumHermesUsd = '0.06'" in source
    assert "$environment['CAPTAIN_BENCHMARK_MAX_USD'] = '0.60'" in source
    assert "$seedVersion = 'business-benchmark-demo-2026-07-v19'" in source
    assert "'--suite-version', '19'" in source
    assert "New-DryRunPlan" in source
    assert "provider_calls = $false" in source
    assert "gateway_mutation = $false" in source
    assert "minibook_mutation = $false" in source
    assert "run-agent-factory-business-demo.py" in source
    assert "Resolve-HermesPython" in source
    assert "'--hermes-python-executable', $hermesPython" in source
    assert "factory-operator-stderr.log" in source
    assert "@(& $python @factoryArguments 2>$factoryErrorPath)" in source
    assert "--apply" in source
    assert "preflight-business-benchmark-demo.py" in source
    assert "& $serviceRunner benchmark-start" in source
    assert "factory_dispatch_required" in source
    assert "infrastructure_required" in source
    assert source.index("preflight-business-benchmark-demo.py") < source.index(
        "$null = @(& $liveRunner"
    )
    assert "GetEnvironmentVariable('OPENAI_API_KEY', 'Process')" in source
    assert source.index("$rawPreflight = @(& $python $preflightScript)") < source.index(
        "GetEnvironmentVariable('OPENAI_API_KEY', 'Process')"
    )
    assert source.index("GetEnvironmentVariable('OPENAI_API_KEY', 'Process')") < source.index(
        "$null = @(& $liveRunner"
    )
    assert "OPENAI_API_KEY" not in source.split("$rootEnvAllowlist", 1)[1].split(
        ")", 1
    )[0]
    assert "$Action -ceq 'Run' -and" in source
    assert "Applied team Gateway budget does not match" in source
    assert "[decimal]$team.gateway_budget_remaining_usd" in source
    assert "docker compose down" not in source
    assert "VibeMind" not in source
    assert PREFLIGHT.is_file()


def test_runner_probes_a_process_start_launchable_native_codex_binary() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "function Resolve-LaunchableCodexExecutable" in source
    assert "@openai\\codex-win32-*" in source
    assert 'ArgumentList.Add("--version")' in source
    assert "$process.Start()" in source
    assert "$codexCommand = Resolve-LaunchableCodexExecutable" in source
    assert "$environment['CAPTAIN_CODEX_EXECUTABLE'] = $codexCommand" in source


def test_factory_operator_cli_emits_only_redacted_codex_interruption(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRef:
        def __init__(self, *, uri: str, sha256: str) -> None:
            self._uri = uri
            self._sha256 = sha256

        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {
                "uri": self._uri,
                "sha256": self._sha256,
                "media_type": "application/json",
            }

    class FakeBindings:
        def as_dict(self) -> dict[str, object]:
            return {
                "job_id": "71000000-0000-0000-0000-000000000001",
                "correlation_id": "72000000-0000-0000-0000-000000000001",
                "subject_version": 3,
                "attempt": 1,
                "invocation_id": "73000000-0000-0000-0000-000000000001",
                "idempotency_key": "a" * 64,
                "lease_id": "factory-lease-1",
                "workspace_ref": "workspace://business-benchmark-demo/claims/epoch-aaaaaaaaaaaaaaaa",
                "base_revision": "b" * 40,
                "scaffold_manifest_sha256": "c" * 64,
                "brief_sha256": "d" * 64,
            }

    class FakeInterrupted(Exception):
        def __init__(self) -> None:
            self.reason = "codex_timed_out"
            self.exit_code = 124
            self.checkpoint_ref = FakeRef(
                uri=f"artifact://factory/codex-checkpoint/{'e' * 64}",
                sha256="e" * 64,
            )
            self.terminal_receipt_ref = FakeRef(
                uri=f"artifact://factory/codex-terminal-receipt/{'f' * 64}",
                sha256="f" * 64,
            )
            self.resume_ordinal = 0
            self.authorization_binding = FakeBindings()

    gateway_module = ModuleType("gateway")
    operator_module = ModuleType("gateway.agent_factory_live_operator")
    operator_module.FactoryLiveOperatorSettings = object
    operator_module.run_business_demo_factory_jobs = object
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.agent_factory_live_operator", operator_module)
    agenten_module = ModuleType("agenten")
    factory_module = ModuleType("agenten.agent_factory")
    execution_module = ModuleType("agenten.agent_factory.codex_build_execution")
    execution_module.FactoryCodexBuildInterrupted = FakeInterrupted
    monkeypatch.setitem(sys.modules, "agenten", agenten_module)
    monkeypatch.setitem(sys.modules, "agenten.agent_factory", factory_module)
    monkeypatch.setitem(
        sys.modules, "agenten.agent_factory.codex_build_execution", execution_module
    )
    script = ROOT / "scripts" / "run-agent-factory-business-demo.py"
    spec = spec_from_file_location("factory_operator_cli", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    interruption = FakeInterrupted()

    async def interrupted_run(*_args: object, **_kwargs: object) -> object:
        raise interruption

    monkeypatch.setattr(module, "FactoryLiveOperatorSettings", lambda **_kwargs: object())
    monkeypatch.setattr(module, "run_business_demo_factory_jobs", interrupted_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--workspace-root",
            str(ROOT),
            "--python-executable",
            sys.executable,
            "--hermes-python-executable",
            sys.executable,
            "--job-id",
            "71000000-0000-0000-0000-000000000001",
            "--job-id",
            "71000000-0000-0000-0000-000000000002",
            "--hermes-model",
            "gpt-4.1-mini",
        ],
    )

    assert module.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "schema": "captain.business-demo-factory-operator.v1",
        "database": "captain_test",
        "status": "codex_build_interrupted",
        "exit_code": 124,
        "reason": "codex_timed_out",
        "checkpoint_ref": {
            "uri": f"artifact://factory/codex-checkpoint/{'e' * 64}",
            "sha256": "e" * 64,
            "media_type": "application/json",
        },
        "terminal_receipt_ref": {
            "uri": f"artifact://factory/codex-terminal-receipt/{'f' * 64}",
            "sha256": "f" * 64,
            "media_type": "application/json",
        },
        "next_resume_ordinal": 1,
        "captain_authorization_binding": {
            "job_id": "71000000-0000-0000-0000-000000000001",
            "correlation_id": "72000000-0000-0000-0000-000000000001",
            "subject_version": 3,
            "attempt": 1,
            "invocation_id": "73000000-0000-0000-0000-000000000001",
            "idempotency_key": "a" * 64,
            "lease_id": "factory-lease-1",
            "workspace_ref": "workspace://business-benchmark-demo/claims/epoch-aaaaaaaaaaaaaaaa",
            "base_revision": "b" * 40,
            "scaffold_manifest_sha256": "c" * 64,
            "brief_sha256": "d" * 64,
        },
    }
    interruption.resume_ordinal = 2
    assert module.main() == 2
    exhausted = json.loads(capsys.readouterr().out)
    assert exhausted["next_resume_ordinal"] is None
    serialized = json.dumps(result)
    for forbidden in ("workspace_root", "journal", "prompt", "stderr"):
        assert forbidden not in serialized


def test_unresolved_provisioning_returns_factory_checkpoint_without_provider(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    (repository / "examples" / "business_benchmark_candidates" / "customer_renewal_orchestration_team" / "workflows").mkdir(
        parents=True
    )
    (repository / "agenten" / "agent_factory" / "skills").mkdir(parents=True)
    (repository / "examples" / "business_benchmark_candidates" / "customer_renewal_orchestration_team" / "workflows" / "renewal_context_read.json").write_text(
        "{}", encoding="utf-8"
    )
    (scripts / "live-demo-services.ps1").write_text(
        r"""
param([string]$Action)
if ($Action -cne 'benchmark-start') { throw 'only benchmark-start is accepted' }
$root = Split-Path -Parent $PSScriptRoot
Set-Content (Join-Path $root 'service-called') 'unsafe'
""".strip(),
        encoding="utf-8",
    )
    (scripts / "provision-business-benchmark-demo.py").write_text(
        r"""
import json
from pathlib import Path
import sys

Path(__file__).resolve().parents[1].joinpath('provision-args.json').write_text(
    json.dumps(sys.argv[1:]), encoding='utf-8'
)
if '--apply' in sys.argv:
    Path(__file__).resolve().parents[1].joinpath('gateway-mutated').write_text(
        'unsafe', encoding='utf-8'
    )
suite_version = int(sys.argv[sys.argv.index('--suite-version') + 1])
def team(profile, job_id, candidate_id, batch=None):
    return {
        'profile': profile,
        'job': {
            'job_id': job_id,
            'execution_policy': {
                'allowed_models': ['gpt-4.1-mini'],
                'max_cost_usd': '0.30',
            },
        },
        'suite': {'suite_version': suite_version},
        'candidate_id': candidate_id,
        'gateway_budget_remaining_usd': '0.30',
        'work_batch': None if batch is None else {'batch_id': batch},
        'production_scope_resolvable': True,
        'missing_gateway_evidence': ['team_execution_evidence'],
        'next_dispatch': {'action': 'dispatch_agent_architect'},
    }
print(json.dumps({
    'schema': 'captain.business-benchmark-demo-provisioning.v1',
    'mode': 'applied' if '--apply' in sys.argv else 'dry_run',
    'issued_at': '2026-07-28T20:00:00Z',
    'database': 'captain_test',
    'teams': [
        team('claims', '71000000-0000-0000-0000-000000000001', 'claims-candidate'),
        team('renewal', '71000000-0000-0000-0000-000000000002', 'renewal-candidate', 'renewal-batch'),
    ],
}))
""".strip(),
        encoding="utf-8",
    )
    (repository / ".env").write_text(
        "\n".join(
            (
                "TEST_MARIADB_DSN=mariadb://captain_test:test-only@127.0.0.1:33306/captain_test",
                "MARIADB_TEST_PORT=33306",
                "CAPTAIN_BENCHMARK_MODEL=gpt-4.1-mini",
                "CAPTAIN_N8N_URL=http://127.0.0.1:5679",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (repository / ".env.captain-n8n").write_text(
        "\n".join(
            (
                "CAPTAIN_N8N_PORT=5679",
                "CAPTAIN_N8N_API_KEY=file-api-secret",
                "CAPTAIN_N8N_MCP_TOKEN=file-mcp-secret",
                "CAPTAIN_N8N_MCP_BROKER_URL=http://127.0.0.1:5680",
                "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET=file-signing-secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (scripts / "preflight-business-benchmark-demo.py").write_text(
        r"""
import json
from pathlib import Path

Path(__file__).resolve().parents[1].joinpath('preflight-called').write_text(
    'effect-free', encoding='utf-8'
)
print(json.dumps({
    'schema': 'captain.business-benchmark-default-preflight.v1',
    'status': 'factory_dispatch_required',
    'database': 'captain_test',
    'production_scope_resolvable': False,
}))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "run-business-benchmark-live.ps1").write_text(
        "Set-Content (Join-Path (Split-Path -Parent $PSScriptRoot) 'provider-called') 'unsafe'",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)

    planned = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Plan",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan == {
        "schema": "captain.business-benchmark-demo-run.v1",
        "status": "planned",
        "mode": "dry_run",
        "database": "captain_test",
        "issued_at": plan["issued_at"],
        "suite_version": 19,
        "seed_version_id": "business-benchmark-demo-2026-07-v19",
        "maximum_usd_per_team": "0.30",
        "jobs": [
            {"profile": "claims", "job_id": "71000000-0000-0000-0000-000000000001"},
            {"profile": "renewal", "job_id": "71000000-0000-0000-0000-000000000002"},
        ],
        "effects": {
            "provider_calls": False,
            "live_service_calls": False,
            "provisioning_apply": False,
            "gateway_mutation": False,
            "minibook_mutation": False,
        },
    }
    assert plan["issued_at"].endswith("Z")
    plan_arguments = json.loads((repository / "provision-args.json").read_text("utf-8"))
    assert "--apply" not in plan_arguments
    assert plan_arguments[plan_arguments.index("--suite-version") + 1] == "19"
    assert not (repository / "service-called").exists()
    assert not (repository / "preflight-called").exists()
    assert not (repository / "provider-called").exists()
    assert not (repository / "gateway-mutated").exists()

    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Run",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 2, completed.stderr
    checkpoint = json.loads(completed.stdout)
    assert checkpoint == {
        "schema": "captain.business-benchmark-demo-run.v1",
        "status": "factory_dispatch_required",
        "database": "captain_test",
        "issued_at": checkpoint["issued_at"],
        "maximum_usd_per_team": "0.30",
        "jobs": [
            {
                "profile": "claims",
                "job_id": "71000000-0000-0000-0000-000000000001",
                "candidate_id": "claims-candidate",
                "missing_gateway_evidence": ["team_execution_evidence"],
            },
            {
                "profile": "renewal",
                "job_id": "71000000-0000-0000-0000-000000000002",
                "candidate_id": "renewal-candidate",
                "missing_gateway_evidence": ["team_execution_evidence"],
            },
        ],
        "renewal_batch_id": "renewal-batch",
        "instruction": (
            "Run the Captain Factory dispatch composition for both job IDs, "
            "then rerun this command."
        ),
    }
    assert checkpoint["issued_at"].endswith("Z")
    assert (repository / "preflight-called").is_file()
    assert not (repository / "provider-called").exists()
    arguments = json.loads((repository / "provision-args.json").read_text("utf-8"))
    assert "--apply" in arguments
    assert arguments[arguments.index("--maximum-usd-per-team") + 1] == "0.30"
    assert arguments[arguments.index("--suite-version") + 1] == "19"
    issued_at = arguments[arguments.index("--issued-at") + 1]
    assert issued_at.endswith("Z")
    combined = completed.stdout + completed.stderr
    for secret in (
        "file-api-secret",
        "file-mcp-secret",
        "file-signing-secret",
        "test-only",
    ):
        assert secret not in combined

    (scripts / "preflight-business-benchmark-demo.py").write_text(
        r"""
import json
print(json.dumps({
    'schema': 'captain.business-benchmark-default-preflight.v1',
    'status': 'resolvable',
    'database': 'captain_test',
    'production_scope_resolvable': True,
    'jobs': [],
}))
""".strip(),
        encoding="utf-8",
    )
    provider_boundary = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Run",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert provider_boundary.returncode != 0
    assert "OPENAI_API_KEY must already exist in the process" in provider_boundary.stderr
    assert not (repository / "provider-called").exists()

    factory_preflight = r"""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
resolvable = root.joinpath('factory-called').is_file()
print(json.dumps({
    'schema': 'captain.business-benchmark-default-preflight.v1',
    'status': 'resolvable' if resolvable else 'factory_dispatch_required',
    'database': 'captain_test',
    'production_scope_resolvable': resolvable,
    **({'jobs': []} if resolvable else {}),
}))
"""
    (scripts / "preflight-business-benchmark-demo.py").write_text(
        factory_preflight.strip(),
        encoding="utf-8",
    )
    (scripts / "run-agent-factory-business-demo.py").write_text(
        r"""
import json

print(json.dumps({
    'schema': 'captain.business-demo-factory-operator.v1',
    'database': 'captain_test',
    'status': 'codex_build_interrupted',
    'exit_code': 124,
    'reason': 'codex_timed_out',
    'checkpoint_ref': {
        'uri': 'artifact://factory/codex-checkpoint/' + 'e' * 64,
        'sha256': 'e' * 64,
        'media_type': 'application/json',
    },
    'terminal_receipt_ref': {
        'uri': 'artifact://factory/codex-terminal-receipt/' + 'f' * 64,
        'sha256': 'f' * 64,
        'media_type': 'application/json',
    },
    'next_resume_ordinal': 1,
    'captain_authorization_binding': {
        'job_id': '71000000-0000-0000-0000-000000000001',
        'correlation_id': '72000000-0000-0000-0000-000000000001',
        'subject_version': 3,
        'attempt': 1,
        'invocation_id': '73000000-0000-0000-0000-000000000001',
        'idempotency_key': 'a' * 64,
        'lease_id': 'factory-lease-1',
        'workspace_ref': 'workspace://business-benchmark-demo/claims/epoch-aaaaaaaaaaaaaaaa',
        'base_revision': 'b' * 40,
        'scaffold_manifest_sha256': 'c' * 64,
        'brief_sha256': 'd' * 64,
    },
}))
raise SystemExit(2)
""".strip(),
        encoding="utf-8",
    )
    environment["OPENAI_API_KEY"] = "process-only-demo-key"
    interrupted = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Run",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert interrupted.returncode == 2, interrupted.stderr
    assert json.loads(interrupted.stdout)["status"] == "codex_build_interrupted"
    assert "process-only-demo-key" not in interrupted.stdout + interrupted.stderr
    assert not (repository / "provider-called").exists()

    (scripts / "run-agent-factory-business-demo.py").write_text(
        r"""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
root.joinpath('factory-called').write_text('yes', encoding='utf-8')
root.joinpath('factory-args.json').write_text(
    json.dumps(sys.argv[1:]), encoding='utf-8'
)
job_ids = [
    value for index, value in enumerate(sys.argv[1:])
    if index > 0 and sys.argv[index] == '--job-id'
]
print(json.dumps({
    'schema': 'captain.business-demo-factory-operator.v1',
    'database': 'captain_test',
    'results': [
        {
            'schema': 'captain.factory-dispatch-run-result.v1',
            'job_id': job_id,
            'status': 'captain_action_required',
        }
        for job_id in job_ids
    ],
}))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "run-business-benchmark-live.ps1").write_text(
        r"""
param([string]$Profile, [string]$PythonPath)
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root 'factory-called'))) {
    throw 'Factory must run before provider benchmark.'
}
Set-Content (Join-Path $root 'provider-called') 'yes'
""".strip(),
        encoding="utf-8",
    )
    built = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Build",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert built.returncode == 0, built.stderr
    candidates_ready = json.loads(built.stdout)
    assert candidates_ready == {
        "schema": "captain.business-benchmark-demo-run.v1",
        "status": "candidates_ready",
        "database": "captain_test",
        "issued_at": candidates_ready["issued_at"],
        "maximum_usd_per_team": "0.30",
        "jobs": [
            {
                "profile": "claims",
                "job_id": "71000000-0000-0000-0000-000000000001",
                "candidate_id": "claims-candidate",
            },
            {
                "profile": "renewal",
                "job_id": "71000000-0000-0000-0000-000000000002",
                "candidate_id": "renewal-candidate",
            },
        ],
        "renewal_batch_id": "renewal-batch",
    }
    assert candidates_ready["issued_at"].endswith("Z")
    assert (repository / "service-called").is_file()
    assert (repository / "factory-called").is_file()
    assert not (repository / "provider-called").exists()
    build_arguments = json.loads((repository / "provision-args.json").read_text("utf-8"))
    assert "--apply" in build_arguments
    build_factory_arguments = json.loads(
        (repository / "factory-args.json").read_text("utf-8")
    )
    assert build_factory_arguments.count("--job-id") == 2
    assert "process-only-demo-key" not in built.stdout + built.stderr

    (scripts / "preflight-business-benchmark-demo.py").write_text(
        r"""
import json
print(json.dumps({
    'schema': 'captain.business-benchmark-default-preflight.v1',
    'status': 'factory_dispatch_required',
    'database': 'captain_test',
    'production_scope_resolvable': False,
}))
""".strip(),
        encoding="utf-8",
    )
    blocked_build = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Build",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert blocked_build.returncode == 2, blocked_build.stderr
    assert json.loads(blocked_build.stdout)["status"] == "factory_dispatch_required"
    assert not (repository / "provider-called").exists()
    (scripts / "preflight-business-benchmark-demo.py").write_text(
        factory_preflight.strip(),
        encoding="utf-8",
    )
    (repository / "factory-called").unlink()

    successful = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Run",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert successful.returncode == 0, successful.stderr
    assert json.loads(successful.stdout)["status"] == "completed"
    assert (repository / "factory-called").is_file()
    assert (repository / "provider-called").is_file()
    factory_arguments = json.loads(
        (repository / "factory-args.json").read_text("utf-8")
    )
    assert factory_arguments[factory_arguments.index("--hermes-max-usd") + 1] == "0.06"
    assert factory_arguments[
        factory_arguments.index("--hermes-python-executable") + 1
    ] == sys.executable
    assert factory_arguments[factory_arguments.index("--hermes-model") + 1] == "gpt-4.1-mini"
    assert factory_arguments.count("--job-id") == 2
    assert "process-only-demo-key" not in successful.stdout + successful.stderr


def test_missing_provider_key_does_not_block_infrastructure_checkpoint(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    (repository / ".env").write_text(
        "OPENAI_API_KEY=file-openai-secret\n", encoding="utf-8"
    )
    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Run",
            "-PythonPath",
            sys.executable,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 4
    assert json.loads(completed.stdout)["status"] == "infrastructure_required"
    assert "OPENAI_API_KEY must already exist in the process" not in completed.stderr
    assert "file-openai-secret" not in completed.stdout + completed.stderr


def test_plan_isolated_from_run_only_credentials_tools_and_services(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")

    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    (repository / ".env").write_text("POISONED_ENV_MUST_NOT_BE_READ\n", encoding="utf-8")
    (repository / ".env.captain-n8n").write_text(
        "POISONED_N8N_ENV_MUST_NOT_BE_READ\n", encoding="utf-8"
    )
    (scripts / "provision-business-benchmark-demo.py").write_text(
        r"""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
root.joinpath('provision-called').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
print(json.dumps({
    'schema': 'captain.business-benchmark-demo-provisioning.v1',
    'mode': 'dry_run',
    'database': 'captain_test',
    'teams': [
        {'profile': 'claims', 'job': {'job_id': '71000000-0000-0000-0000-000000000001', 'execution_policy': {'allowed_models': ['gpt-4.1-mini'], 'max_cost_usd': '0.30'}}, 'suite': {'suite_version': 19}, 'candidate_id': 'claims-candidate'},
        {'profile': 'renewal', 'job': {'job_id': '71000000-0000-0000-0000-000000000002', 'execution_policy': {'allowed_models': ['gpt-4.1-mini'], 'max_cost_usd': '0.30'}}, 'suite': {'suite_version': 19}, 'candidate_id': 'renewal-candidate'},
    ],
}))
""".strip(),
        encoding="utf-8",
    )
    for name in (
        "live-demo-services.ps1",
        "preflight-business-benchmark-demo.py",
        "run-agent-factory-business-demo.py",
        "run-business-benchmark-live.ps1",
    ):
        (scripts / name).write_text(
            "Set-Content (Join-Path (Split-Path -Parent $PSScriptRoot) 'run-only-called') 'unsafe'",
            encoding="utf-8",
        )

    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Plan",
            "-PythonPath",
            sys.executable,
            "-HermesPythonPath",
            str(repository / "absent-hermes-python.exe"),
        ],
        cwd=repository,
        env={**os.environ, "PATH": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["jobs"] == [
        {"profile": "claims", "job_id": "71000000-0000-0000-0000-000000000001"},
        {"profile": "renewal", "job_id": "71000000-0000-0000-0000-000000000002"},
    ]
    assert not (repository / "run-only-called").exists()
    plan_arguments = json.loads((repository / "provision-called").read_text("utf-8"))
    assert "--apply" not in plan_arguments
    assert "--plan-only" in plan_arguments


def test_interruption_validator_rejects_noncanonical_recovery_payloads(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")

    function_source = SCRIPT.read_text(encoding="utf-8").split(
        "Push-Location $repositoryRoot", 1
    )[0]
    harness = tmp_path / "validate-interruption.ps1"
    harness.write_text(
        function_source
        + "\n$checkpoint = Get-Content -Raw $env:CHECKPOINT_PATH | ConvertFrom-Json -Depth 20\n"
        + "if (Test-CodexBuildInterruptedCheckpoint -Checkpoint $checkpoint) { exit 0 }\n"
        + "exit 1\n",
        encoding="utf-8",
    )
    digest = "a" * 64
    checkpoint = {
        "schema": "captain.business-demo-factory-operator.v1",
        "database": "captain_test",
        "status": "codex_build_interrupted",
        "exit_code": 124,
        "reason": "codex_timed_out",
        "checkpoint_ref": {
            "uri": f"artifact://factory/codex-checkpoint/{digest}",
            "sha256": digest,
            "media_type": "application/json",
        },
        "terminal_receipt_ref": {
            "uri": f"artifact://factory/codex-terminal-receipt/{digest}",
            "sha256": digest,
            "media_type": "application/json",
        },
        "next_resume_ordinal": 1,
        "captain_authorization_binding": {
            "job_id": "71000000-0000-0000-0000-000000000001",
            "correlation_id": "72000000-0000-0000-0000-000000000001",
            "subject_version": 3,
            "attempt": 1,
            "invocation_id": "73000000-0000-0000-0000-000000000001",
            "idempotency_key": digest,
            "lease_id": "factory-lease-1",
            "workspace_ref": "workspace://business-benchmark-demo/claims/epoch-aaaaaaaaaaaaaaaa",
            "base_revision": "b" * 40,
            "scaffold_manifest_sha256": "c" * 64,
            "brief_sha256": "d" * 64,
        },
    }

    def validate(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        checkpoint_path = tmp_path / "checkpoint.json"
        checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [pwsh, "-NoProfile", "-File", str(harness), "-Action", "Plan"],
            env={**os.environ, "CHECKPOINT_PATH": str(checkpoint_path)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert validate(checkpoint).returncode == 0
    factory_workspace = json.loads(json.dumps(checkpoint))
    factory_workspace["captain_authorization_binding"]["workspace_ref"] = (
        "workspace://business-benchmark-demo/71000000-0000-0000-0000-000000000001/"
        "dispatch_agent_architect/1/20260730T120000123456Z"
    )
    assert validate(factory_workspace).returncode == 0
    cancelled = json.loads(json.dumps(checkpoint))
    cancelled["reason"] = "runtime_cancelled"
    cancelled["exit_code"] = 130
    assert validate(cancelled).returncode == 0
    invalid_payloads = []
    host_path = json.loads(json.dumps(checkpoint))
    host_path["checkpoint_ref"]["uri"] = "file:///C:/captain/checkpoint.json"
    invalid_payloads.append(host_path)
    mismatched_hash = json.loads(json.dumps(checkpoint))
    mismatched_hash["terminal_receipt_ref"]["uri"] = (
        f"artifact://factory/codex-terminal-receipt/{'e' * 64}"
    )
    invalid_payloads.append(mismatched_hash)
    traversal_workspace = json.loads(json.dumps(checkpoint))
    traversal_workspace["captain_authorization_binding"]["workspace_ref"] = (
        "workspace://factory/../../host/attempt-1"
    )
    invalid_payloads.append(traversal_workspace)
    malformed_uuid_and_identifier = json.loads(json.dumps(checkpoint))
    malformed_uuid_and_identifier["captain_authorization_binding"]["job_id"] = "7100"
    malformed_uuid_and_identifier["captain_authorization_binding"]["lease_id"] = "../lease"
    invalid_payloads.append(malformed_uuid_and_identifier)
    invalid_reason_exit = json.loads(json.dumps(checkpoint))
    invalid_reason_exit["reason"] = "runtime_cancelled"
    invalid_reason_exit["exit_code"] = 1
    invalid_payloads.append(invalid_reason_exit)
    invalid_resume = json.loads(json.dumps(checkpoint))
    invalid_resume["next_resume_ordinal"] = 3
    invalid_payloads.append(invalid_resume)

    for payload in invalid_payloads:
        assert validate(payload).returncode == 1
