from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-business-benchmark-demo.ps1"
PREFLIGHT = ROOT / "scripts" / "preflight-business-benchmark-demo.py"


def test_runner_contract_is_opt_in_redacted_and_factory_gated() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.startswith("#requires -Version 7")
    assert "[ValidateSet('Plan', 'Run')]" in source
    assert "--maximum-usd-per-team', $maximumUsdPerTeam" in source
    assert "$maximumUsdPerTeam = '0.30'" in source
    assert "$maximumHermesUsd = '0.09'" in source
    assert "$environment['CAPTAIN_BENCHMARK_MAX_USD'] = '0.60'" in source
    assert "$seedVersion = 'business-benchmark-demo-2026-07-v15'" in source
    assert "'--suite-version', '15'" in source
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
@'
TEST_MARIADB_DSN=mariadb://captain_test:test-only@127.0.0.1:33306/captain_test
MARIADB_TEST_PORT=33306
CAPTAIN_BENCHMARK_MODEL=gpt-4.1-mini
CAPTAIN_N8N_URL=http://127.0.0.1:5679
'@ | Set-Content (Join-Path $root '.env') -Encoding utf8
@'
CAPTAIN_N8N_PORT=5679
CAPTAIN_N8N_API_KEY=file-api-secret
CAPTAIN_N8N_MCP_TOKEN=file-mcp-secret
CAPTAIN_N8N_MCP_BROKER_URL=http://127.0.0.1:5680
CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET=file-signing-secret
'@ | Set-Content (Join-Path $root '.env.captain-n8n') -Encoding utf8
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
        'suite': {'suite_version': 1},
        'candidate_id': candidate_id,
        'gateway_budget_remaining_usd': '0.30',
        'work_batch': None if batch is None else {'batch_id': batch},
        'production_scope_resolvable': True,
        'missing_gateway_evidence': ['team_execution_evidence'],
        'next_dispatch': {'action': 'dispatch_agent_architect'},
    }
print(json.dumps({
    'schema': 'captain.business-benchmark-demo-provisioning.v1',
    'mode': 'applied',
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
    assert arguments[arguments.index("--suite-version") + 1] == "15"
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

    (scripts / "preflight-business-benchmark-demo.py").write_text(
        r"""
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
""".strip(),
        encoding="utf-8",
    )
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
    environment["OPENAI_API_KEY"] = "process-only-demo-key"

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
    assert factory_arguments[factory_arguments.index("--hermes-max-usd") + 1] == "0.09"
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
