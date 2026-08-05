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
    assert "$global:LASTEXITCODE = 0" in source
    assert "[ValidateSet('Plan', 'Authorize', 'Build', 'Run')]" in source
    assert "--maximum-usd-per-team', $maximumUsdPerTeam" in source
    assert "$maximumUsdPerTeam = '0.32'" in source
    assert "$maximumHermesUsd = '1.50'" in source
    assert "$maximumIncrementalHermesUsd = '1.50'" in source
    assert "$unresolvedHermesEffectReserveUsd = '0.25'" in source
    assert "$maximumTotalUsdPerTeam = '4.80'" in source
    assert "$priorActualUsdClaims = '1.448531'" in source
    assert "$priorActualUsdRenewal = '1.525231'" in source
    assert "$userMaximumEurPerTeam = '6.00'" in source
    assert "$budgetEurPerUsd = '1.25'" in source
    assert "[ValidateSet('ClaimsFirst', 'RenewalFirst')]" in source
    assert "[ValidateSet('All', 'Claims', 'Renewal')]" in source
    assert "[string]$TargetProfile = 'All'" in source
    assert "$targetProfile = $TargetProfile.ToLowerInvariant()" in source
    assert "$targetTeams = @($claims)" in source
    assert "$targetTeams = @($renewal)" in source
    assert "$humanReviewExpectedCompletions = if ($targetProfile -ceq 'claims')" in source
    assert "$environment['CAPTAIN_BENCHMARK_PROFILE'] = $targetProfile" in source
    assert "-Profile $targetProfile -PythonPath $python" in source
    assert "[array]::Reverse($orderedFactoryJobIds)" in source
    assert "$environment['CAPTAIN_BENCHMARK_MAX_USD'] = $aggregateRemainingUsd.ToString" in source
    assert '$environment["${prefix}_MAX_USD"] = $teamRemainingText' in source
    assert "Assert-CodexUsesChatGptSubscription" in source
    assert "$environment['CAPTAIN_CODEX_AUTH_MODE'] = 'chatgpt_subscription'" in source
    assert "$humanReviewTimeoutSeconds = if (" in source
    assert "$humanReviewAdapterTimeoutSeconds = '5400'" in source
    assert "[string]$HumanReviewOperatorId = ''" in source
    assert "Start-HumanReviewCompletionAdapter" in source
    assert "[string[]]$JobAttempts" in source
    assert "$arguments.Add('--job-attempt')" in source
    assert "-JobAttempts @($targetTeams | ForEach-Object" in source
    assert source.count("$humanReviewAdapter = Start-HumanReviewCompletionAdapter") == 1
    assert source.index(
        "$humanReviewAdapter = Start-HumanReviewCompletionAdapter"
    ) < source.index("$rawFactory = @(& $python @factoryArguments")
    assert "business_benchmark_human_review_cli" in source
    assert "[string]$TimeoutSeconds" in source
    assert "-TimeoutSeconds ([int]$humanReviewAdapterTimeoutSeconds)" in source
    assert (
        "$environment['CAPTAIN_BENCHMARK_HUMAN_REVIEW_TIMEOUT_SECONDS'] = "
        "$humanReviewTimeoutSeconds"
    ) in source
    assert "$environment['CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM']" in source
    assert "$environment['CAPTAIN_FACTORY_USER_MAX_EUR_PER_TEAM']" in source
    assert "$environment['CAPTAIN_FACTORY_BUDGET_EUR_PER_USD']" in source
    assert "$environment['CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_CLAIMS']" in source
    assert "$environment['CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_RENEWAL']" in source
    assert "CUSTOM_BASE_URL" not in source
    assert "'--hermes-provider', 'openai-api'" in source
    assert "'--hermes-model', 'gpt-5.6-terra'" in source
    assert "'--hermes-reasoning-effort', 'high'" in source
    assert '$seedVersion = "business-benchmark-demo-2026-08-v${SuiteVersion}"' in source
    assert "'--suite-version', [string]$suiteVersion" in source
    assert "New-DryRunPlan" in source
    assert "provider_calls = $false" in source
    assert "gateway_mutation = $false" in source
    assert "minibook_mutation = $false" in source
    assert "run-agent-factory-business-demo.py" in source
    assert "issue-factory-improvement.py" in source
    assert "--eligible-only" in source
    assert "captain.factory-improvement-issuance.v1" in source
    assert "$authorizedAttempt -ne ($failedAttempt + 1)" in source
    assert "@($authorization.authorizations).Count -lt 1" in source
    assert "Resolve-HermesPython" in source
    assert "'--hermes-python-executable', $hermesPython" in source
    assert "factory-operator-stderr.log" in source
    assert "Save-CodexBuildInterruptedCheckpoint" in source
    assert "runtime-state/codex/interruption-checkpoints" in source
    assert "@(& $python @factoryArguments 2>$factoryErrorPath)" in source
    assert "--apply" in source
    assert "preflight-business-benchmark-demo.py" in source
    assert "& $serviceRunner benchmark-start" in source
    assert "business-benchmark-runtime.env" in source
    assert "$benchmarkRuntimeAllowlist" in source
    assert "MARIADB_BENCHMARK_PORT" in source
    assert "CAPTAIN_BENCHMARK_GATEWAY_URL" in source
    assert "factory_dispatch_required" in source
    assert "factory_improvement_required" in source
    assert "append_improvement_requested" in source
    assert "Set-ResolvedPreflightAttempts" in source
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
    assert "$Action = $Action.ToUpperInvariant()" in source
    assert "Applied team Gateway budget does not match" in source
    assert "$gatewayBudgetRemaining -lt 0" in source
    assert "$gatewayBudgetRemaining -gt [decimal]$maximumUsdPerTeam" in source
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
                "gpt-5.6-terra",
                "--hermes-reasoning-effort",
                "high",
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


def test_factory_operator_cli_threads_stop_flag_and_emits_typed_results(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_settings: dict[str, object] = {}

    class FakeResult:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id

        def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
            assert mode == "json"
            assert by_alias is True
            return {
                "schema": "captain.factory-dispatch-run-result.v1",
                "job_id": self.job_id,
                "status": "stop_point_reached",
                "lifecycle_status": "running",
                "next_action": {
                    "kind": "dispatch_quality_warden",
                    "attempt": 1,
                    "job_id": self.job_id,
                },
                "dispatched_actions": ["dispatch_real_case_tester"],
            }

    gateway_module = ModuleType("gateway")
    operator_module = ModuleType("gateway.agent_factory_live_operator")

    def settings_factory(**kwargs: object) -> object:
        captured_settings.update(kwargs)
        return object()

    async def stopped_run(*_args: object, **_kwargs: object) -> tuple[FakeResult, FakeResult]:
        return (
            FakeResult("71000000-0000-0000-0000-000000000001"),
            FakeResult("71000000-0000-0000-0000-000000000002"),
        )

    operator_module.FactoryLiveOperatorSettings = settings_factory
    operator_module.run_business_demo_factory_jobs = stopped_run
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(sys.modules, "gateway.agent_factory_live_operator", operator_module)
    agenten_module = ModuleType("agenten")
    factory_module = ModuleType("agenten.agent_factory")
    execution_module = ModuleType("agenten.agent_factory.codex_build_execution")
    execution_module.FactoryCodexBuildInterrupted = RuntimeError
    monkeypatch.setitem(sys.modules, "agenten", agenten_module)
    monkeypatch.setitem(sys.modules, "agenten.agent_factory", factory_module)
    monkeypatch.setitem(
        sys.modules, "agenten.agent_factory.codex_build_execution", execution_module
    )
    script = ROOT / "scripts" / "run-agent-factory-business-demo.py"
    spec = spec_from_file_location("factory_operator_stop_cli", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
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
                "gpt-5.6-terra",
                "--hermes-reasoning-effort",
                "high",
            "--stop-before-quality-warden",
        ],
    )

    assert module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert captured_settings["stop_before_quality_warden"] is True
    assert [result["status"] for result in output["results"]] == [
        "stop_point_reached",
        "stop_point_reached",
    ]
    assert {
        result["next_action"]["kind"] for result in output["results"]
    } == {"dispatch_quality_warden"}


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
$runtime = Join-Path $root '.captain-cook/private/business-benchmarks/business-benchmark-runtime.env'
New-Item -ItemType Directory -Force (Split-Path $runtime -Parent) | Out-Null
@(
    'TEST_MARIADB_DSN=mariadb://captain_test:test-only@127.0.0.1:33316/captain_test',
    'MARIADB_BENCHMARK_PORT=33316',
    'CAPTAIN_BENCHMARK_GATEWAY_URL=http://127.0.0.1:8092'
) | Set-Content $runtime
""".strip(),
        encoding="utf-8",
    )
    (scripts / "provision-business-benchmark-demo.py").write_text(
        r"""
import json
import os
from pathlib import Path
import sys

Path(__file__).resolve().parents[1].joinpath('provision-args.json').write_text(
    json.dumps(sys.argv[1:]), encoding='utf-8'
)
Path(__file__).resolve().parents[1].joinpath('selected-dsn.txt').write_text(
    os.environ.get('TEST_MARIADB_DSN', ''), encoding='utf-8'
)
if '--apply' in sys.argv:
    Path(__file__).resolve().parents[1].joinpath('gateway-mutated').write_text(
        'unsafe', encoding='utf-8'
    )
suite_version = int(sys.argv[sys.argv.index('--suite-version') + 1])
execution_mode = sys.argv[sys.argv.index('--execution-mode') + 1]
def team(profile, job_id, candidate_id, batch=None):
    return {
        'profile': profile,
        'job': {
            'job_id': job_id,
                'execution_policy': {
                    'mode': execution_mode,
                    'required_live_runs': 1 if execution_mode == 'demo' else 3,
                    'allowed_models': ['gpt-4.1-mini'],
                'max_cost_usd': '0.32',
            },
        },
        'suite': {'suite_version': suite_version},
        'candidate_id': candidate_id,
        'gateway_budget_remaining_usd': '0.19',
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
    environment["TEST_MARIADB_DSN"] = (
        "mariadb://hostile:hostile@127.0.0.1:39999/captain_test"
    )

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
        "suite_version": 44,
        "seed_version_id": "business-benchmark-demo-2026-08-v44",
        "maximum_usd_per_team": "0.32",
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
    assert plan_arguments[plan_arguments.index("--suite-version") + 1] == "44"
    assert "--candidate-only-safety-gates" in plan_arguments
    assert "--relative-efficiency-diagnostics" in plan_arguments
    assert plan_arguments[
        plan_arguments.index("--minimum-correctness-uplift-bps") + 1
    ] == "500"
    assert plan_arguments[
        plan_arguments.index("--minimum-completion-uplift-bps") + 1
    ] == "1000"
    assert not (repository / "service-called").exists()
    assert not (repository / "preflight-called").exists()
    assert not (repository / "provider-called").exists()
    assert not (repository / "gateway-mutated").exists()

    release_plan = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Plan",
            "-TargetProfile",
            "Claims",
            "-ExecutionMode",
            "Release",
            "-SuiteVersion",
            "45",
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

    assert release_plan.returncode == 0, release_plan.stderr
    release_payload = json.loads(release_plan.stdout)
    assert release_payload["suite_version"] == 45
    assert release_payload["seed_version_id"] == "business-benchmark-demo-2026-08-v45"
    assert [item["profile"] for item in release_payload["jobs"]] == ["claims"]
    release_arguments = json.loads(
        (repository / "provision-args.json").read_text("utf-8")
    )
    assert release_arguments[release_arguments.index("--execution-mode") + 1] == "release"
    assert release_arguments[release_arguments.index("--suite-version") + 1] == "45"

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
        "maximum_usd_per_team": "0.32",
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
    assert arguments[arguments.index("--maximum-usd-per-team") + 1] == "0.32"
    assert arguments[arguments.index("--suite-version") + 1] == "44"
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
    'jobs': [
        {
            'job_id': '71000000-0000-0000-0000-000000000001',
            'candidate_id': 'claims-candidate',
            'attempt': 1,
        },
        {
            'job_id': '71000000-0000-0000-0000-000000000002',
            'candidate_id': 'renewal-candidate',
            'attempt': 1,
        },
    ],
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
    **({'jobs': [
        {
            'job_id': '71000000-0000-0000-0000-000000000001',
            'candidate_id': 'claims-candidate',
            'attempt': 1,
        },
        {
            'job_id': '71000000-0000-0000-0000-000000000002',
            'candidate_id': 'renewal-candidate',
            'attempt': 1,
        },
    ]} if resolvable else {}),
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

    factory_runner_source = r"""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
root.joinpath('factory-called').write_text('yes', encoding='utf-8')
root.joinpath('factory-args.json').write_text(
    json.dumps(sys.argv[1:]), encoding='utf-8'
)
stop_before_warden = '--stop-before-quality-warden' in sys.argv[1:]
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
            'status': 'stop_point_reached' if stop_before_warden else 'captain_action_required',
            'lifecycle_status': 'running',
            'next_action': {
                'kind': 'dispatch_quality_warden' if stop_before_warden else 'validate_for_promotion',
                'attempt': 1,
                'job_id': job_id,
            },
            'dispatched_actions': (
                ['dispatch_real_case_tester'] if stop_before_warden else
                ['dispatch_real_case_tester', 'dispatch_quality_warden']
            ),
        }
        for job_id in job_ids
    ],
}))
""".strip()
    (scripts / "run-agent-factory-business-demo.py").write_text(
        factory_runner_source,
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
            "build",
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
        "maximum_usd_per_team": "0.32",
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
    assert ":33316/captain_test" in (repository / "selected-dsn.txt").read_text("utf-8")
    assert "39999" not in (repository / "selected-dsn.txt").read_text("utf-8")
    build_factory_arguments = json.loads(
        (repository / "factory-args.json").read_text("utf-8")
    )
    assert build_factory_arguments.count("--job-id") == 2
    assert "--stop-before-quality-warden" in build_factory_arguments
    assert "process-only-demo-key" not in built.stdout + built.stderr

    for invalid_jobs in (
        [],
        [
            {
                "job_id": "71000000-0000-0000-0000-000000000001",
                "candidate_id": "wrong-claims-candidate",
                "attempt": 1,
            },
            {
                "job_id": "71000000-0000-0000-0000-000000000002",
                "candidate_id": "renewal-candidate",
                "attempt": 1,
            },
        ],
    ):
        (scripts / "preflight-business-benchmark-demo.py").write_text(
            "\n".join(
                (
                    "import json",
                    "print(json.dumps({",
                    "    'schema': 'captain.business-benchmark-default-preflight.v1',",
                    "    'status': 'resolvable',",
                    "    'database': 'captain_test',",
                    "    'production_scope_resolvable': True,",
                    f"    'jobs': {invalid_jobs!r},",
                    "}))",
                )
            ),
            encoding="utf-8",
        )
        invalid_scope = subprocess.run(
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
        assert invalid_scope.returncode != 0
        assert "candidates_ready" not in invalid_scope.stdout
        assert not (repository / "provider-called").exists()
    (scripts / "preflight-business-benchmark-demo.py").write_text(
        factory_preflight.strip(),
        encoding="utf-8",
    )

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
    invalid_factory_sources = {
        "wrong stop action": factory_runner_source.replace(
            "'dispatch_quality_warden' if stop_before_warden else 'validate_for_promotion'",
            "'dispatch_real_case_tester'",
        ),
        "mismatched next-action job": factory_runner_source.replace(
            "                'job_id': job_id,\n"
            "            },\n"
            "            'dispatched_actions'",
            "                'job_id': "
            "'71000000-0000-0000-0000-000000000099',\n"
            "            },\n"
            "            'dispatched_actions'",
        ),
    }
    for failure_name, invalid_source in invalid_factory_sources.items():
        (scripts / "run-agent-factory-business-demo.py").write_text(
            invalid_source,
            encoding="utf-8",
        )
        (repository / "factory-called").unlink(missing_ok=True)
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
        assert blocked_build.returncode != 0, failure_name
        assert "candidates_ready" not in blocked_build.stdout, failure_name
        assert not (repository / "provider-called").exists(), failure_name
    (scripts / "run-agent-factory-business-demo.py").write_text(
        factory_runner_source,
        encoding="utf-8",
    )
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
    assert not (repository / "provider-called").exists()
    factory_arguments = json.loads(
        (repository / "factory-args.json").read_text("utf-8")
    )
    assert factory_arguments[factory_arguments.index("--hermes-max-usd") + 1] == "1.50"
    assert factory_arguments[
        factory_arguments.index("--hermes-python-executable") + 1
    ] == sys.executable
    assert (
        factory_arguments[factory_arguments.index("--hermes-model") + 1]
        == "gpt-5.6-terra"
    )
    assert (
        factory_arguments[
            factory_arguments.index("--hermes-reasoning-effort") + 1
        ]
        == "high"
    )
    assert (
        factory_arguments[factory_arguments.index("--hermes-provider") + 1]
        == "openai-api"
    )
    assert factory_arguments.count("--job-id") == 2
    assert "--stop-before-quality-warden" not in factory_arguments
    assert "process-only-demo-key" not in successful.stdout + successful.stderr

    (scripts / "run-agent-factory-business-demo.py").write_text(
        factory_runner_source.replace(
            "'validate_for_promotion'",
            "'append_improvement_requested'",
        ),
        encoding="utf-8",
    )
    (repository / "factory-called").unlink()
    improvement = subprocess.run(
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

    assert improvement.returncode == 2, improvement.stderr
    assert json.loads(improvement.stdout)["status"] == "factory_improvement_required"
    assert not (repository / "provider-called").exists()


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
        {'profile': 'claims', 'job': {'job_id': '71000000-0000-0000-0000-000000000001', 'execution_policy': {'allowed_models': ['gpt-4.1-mini'], 'max_cost_usd': '0.32', 'mode': 'demo', 'required_live_runs': 1}}, 'suite': {'suite_version': 44}, 'candidate_id': 'claims-candidate'},
        {'profile': 'renewal', 'job': {'job_id': '71000000-0000-0000-0000-000000000002', 'execution_policy': {'allowed_models': ['gpt-4.1-mini'], 'max_cost_usd': '0.32', 'mode': 'demo', 'required_live_runs': 1}}, 'suite': {'suite_version': 44}, 'candidate_id': 'renewal-candidate'},
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

    claims_only = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(scripts / SCRIPT.name),
            "-Action",
            "Plan",
            "-TargetProfile",
            "Claims",
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

    assert claims_only.returncode == 0, claims_only.stderr
    assert json.loads(claims_only.stdout)["jobs"] == [
        {"profile": "claims", "job_id": "71000000-0000-0000-0000-000000000001"}
    ]


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
