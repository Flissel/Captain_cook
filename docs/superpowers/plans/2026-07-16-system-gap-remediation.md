# System Gap Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Program routing:** Do not dispatch this plan standalone. Follow
> `2026-07-16-remediation-program-orchestration.md` for packet IDs,
> prerequisites, file locks, supersessions, and integration gates. Only the
> orchestrator updates this plan's checkboxes and Session Insights.

**Goal:** Close all seven audited system gaps so a clean Windows 11 checkout installs, starts, validates, repairs, and documents the complete Captain Cook system with mandatory real integration evidence.

**Architecture:** Treat checkpoints as cached observations backed by stage validators, preserve the selected external VibeMind n8n boundary, and route setup/start/status through shared health contracts. Add an isolated MariaDB integration gate, then align documentation and split the remaining runtime hotspots behind their existing public facades.

**Tech Stack:** PowerShell 7, Pester 5, Python 3.11–3.13, pytest, FastAPI, MariaDB 11.8, Docker Compose v2, AutoGen Core, GitHub Actions.

## Global Constraints

- Supported host: Windows 11 build 22000 or newer with PowerShell 7 or newer.
- Required tools: Git 2+, Python `>=3.11,<3.14`, Node 20+, Docker 20+, Docker Compose v2+.
- Never delete Docker volumes or adopt/migrate VibeMind volumes.
- Never stop a process unless its PID and start time match Captain-owned metadata.
- Never print or commit `.env`, API keys, database passwords, MCP tokens, or Minibook credentials.
- External VibeMind n8n is the configured default; Captain validates it but never starts, stops, adopts, migrates, or deletes its containers or volumes.
- Preserve `Invoke-GuidedSetup`, `Start-CaptainSystem`, `Stop-CaptainSystem`, `Get-CaptainSystemStatus`, `build_pipeline`, and `LedgerRecorderAgent` as public facades.
- A required MariaDB or gateway test that is skipped fails the integration gate.
- Update checkboxes immediately after the named verification passes.
- Record each implementation session in `Session Insights` and consolidate every actionable insight into an exact task step or acceptance criterion.

---

## Execution order

The task numbers preserve the design-workstream grouping. Implement them in
these dependency waves:

- [ ] **Integration prerequisite:** start implementation from a branch that
  contains both the latest local `main` and this plan's tip. At planning time,
  `main` and `docs/system-gap-remediation-plan` had diverged after common commit
  `8986bc4`; a read-only path-overlap and `merge-tree` audit found no shared
  changed file and no conflict marker. Re-run that audit immediately before an
  explicitly approved merge or fresh implementation branch; do not implement
  from a stale plan-only baseline.

1. **Evidence foundation:** Task 0, then Task 6. Establish the clean Python
   environment, isolated `captain_test` guard, real MariaDB/Gateway execution,
   and CI before changing lifecycle behavior.
2. **Windows lifecycle:** Tasks 1–4, then Task 5. Revalidation, repair,
   submodule/external-n8n handling, and preflight feed the shared ten-component
   health contract.
3. **Runtime modularity:** Tasks 7–10 in order. Capability segregation lands
   before recorder and pipeline decomposition.
4. **Truthful handoff:** Task 11 only after every preceding named gate is
   green.

Do not begin a later wave merely because an earlier code edit exists; its
verification and commit checkbox must also be complete.

---

## File and interface map

| File | Responsibility |
| --- | --- |
| `scripts/setup/StageValidation.psm1` | Validate completed setup stages and compute downstream invalidation. |
| `scripts/setup/Repository.psm1` | Initialize declared Git submodules without reset or cleanup. |
| `scripts/setup/Health.psm1` | Build the complete component health report used by setup, start, status, and acceptance. |
| `scripts/setup/Lifecycle.psm1` | Orchestrate stages and preserve the existing public lifecycle facade. |
| `scripts/test_gateway.ps1` | Run the isolated MariaDB/gateway contract gate and reject skips. |
| `docker-compose.test.yml` | Define the disposable MariaDB test service on a dedicated port and volume. |
| `agenten/runtime/event_bus.py` | Separate publish-only and locally subscribable event-bus capabilities. |
| `agenten/adapters/url_relevance.py` | Host URL extraction/relevance behavior outside the ledger package. |
| `agenten/ledger_bridge/handlers.py` | Translate events into recorder application commands. |
| `agenten/ledger_bridge/transitions.py` | Apply ledger lifecycle transitions. |
| `agenten/ledger_bridge/projections.py` | Maintain recorder indexes and query projections. |
| `agenten/ledger_bridge/autogen_adapter.py` | Contain optional AutoGen recorder wiring. |
| `agenten/orchestration/configuration.py` | Validate pipeline configuration and defaults. |
| `agenten/orchestration/components.py` | Construct injected runtime adapters and workers. |
| `agenten/orchestration/pipeline.py` | Keep `build_pipeline` as the thin public composition root. |

---

### Task 0: Establish a reproducible development and test environment

**Files:**
- Create: `requirements-dev.txt`
- Modify: `requirements.txt`
- Modify: `pytest.ini`
- Modify: `tests/test_import_boundaries.py`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the pinned runtime manifest and Python 3.11.
- Produces: a clean development install with pytest and a measured coverage
  floor; no global Python package is part of the test contract.

- [ ] **Step 1: Add failing manifest tests**

Parse non-comment requirement lines and assert normalized runtime package names
are unique. Assert `requirements-dev.txt` includes `-r requirements.txt`,
`pytest==9.0.2`, `pytest-asyncio==1.4.0`, and `pytest-cov==4.1.0`.

Run: `python -m pytest -q tests/test_import_boundaries.py`

Expected: FAIL because `fastapi==0.139.0` occurs twice and the development
manifest does not exist.

- [ ] **Step 2: Create the development manifest and coverage floor**

Remove only the duplicate FastAPI line. Create the development manifest with
the four exact lines above. Configure pytest-cov for `agenten`, `blockchain`,
and `gateway`, terminal missing-line output, and `--cov-fail-under=70`. The
audited service-independent baseline is 75%, so this prevents a five-point
regression without claiming complete legacy coverage.

- [ ] **Step 3: Prove a disposable clean install**

```powershell
python -m venv .captain-cook/verification-venv
.\.captain-cook\verification-venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.captain-cook\verification-venv\Scripts\python.exe -m pip check
.\.captain-cook\verification-venv\Scripts\python.exe -m pytest -q -rs
```

Expected: `pip check` reports no broken requirements; tests pass. Record all
service-dependent skips. Task 6 must later execute every MariaDB/gateway test
with zero skips.

- [ ] **Step 4: Update contributor commands and commit**

Point `AGENTS.md` at the project venv and development manifest while keeping
runtime setup on `requirements.txt`.

```powershell
git add requirements.txt requirements-dev.txt pytest.ini tests/test_import_boundaries.py AGENTS.md
git commit -m "build: add reproducible test environment"
```

---

### Task 1: Revalidate completed checkpoints

**Files:**
- Create: `scripts/setup/StageValidation.psm1`
- Modify: `scripts/setup/Lifecycle.psm1:7-55`
- Modify: `scripts/setup/Setup.Tests.ps1`
- Test: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: `Get-SetupStages`, setup result objects from `Common.psm1`.
- Produces: `Test-SetupStage -Stage string -Context hashtable`, `Get-InvalidatedSetupStages -Stages string[] -FirstInvalidStage string`.

- [ ] **Step 1: Add failing checkpoint revalidation tests**

Add Pester cases proving a completed valid stage is skipped, a completed invalid stage reruns, and every downstream stage reruns:

```powershell
It 'revalidates completed stages and reruns from the first invalid stage' {
    $called = [Collections.Generic.List[string]]::new()
    $checkpoint = @{}
    Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }

    $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint $checkpoint `
        -StageValidator {
            param($stage, $context)
            $stage -ne 'Minibook'
        } `
        -StageRunner {
            param($stage, $context)
            $called.Add($stage)
            [pscustomobject]@{ Status = 'Complete'; Message = 'ok' }
        }

    $result.Status | Should -Be 'Ready'
    $called | Should -Be @('Minibook', 'Services', 'Verification')
}

It 'does not rerun a completed stage whose validator succeeds' {
    $called = [Collections.Generic.List[string]]::new()
    $checkpoint = @{}
    Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }

    Invoke-GuidedSetup -Root $TestDrive -Checkpoint $checkpoint `
        -StageValidator { $true } `
        -StageRunner { param($stage) $called.Add($stage) } | Out-Null

    $called.Count | Should -Be 0
}
```

- [ ] **Step 2: Run the tests and verify the stale-checkpoint failure**

Run:

```powershell
$result = Invoke-Pester -Path scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -eq 0) { throw 'Expected checkpoint tests to fail before implementation.' }
```

Expected: the first test fails because `Invoke-GuidedSetup` has no `StageValidator` parameter.

- [ ] **Step 3: Implement stage validation and downstream invalidation**

Create `StageValidation.psm1` with this public contract:

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-InvalidatedSetupStages {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $Stages,
        [Parameter(Mandatory)][string] $FirstInvalidStage
    )
    $index = [Array]::IndexOf($Stages, $FirstInvalidStage)
    if ($index -lt 0) { throw "Unbekannte Setup-Stage: $FirstInvalidStage" }
    @($Stages[$index..($Stages.Count - 1)])
}

function Test-SetupStage {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Stage, [Parameter(Mandatory)][hashtable] $Context)
    switch ($Stage) {
        'Preflight' { return -not (@(Get-PreflightResults) | Where-Object Status -ne 'Ready') }
        'Configuration' { return Test-Path -LiteralPath (Join-Path $Context.Root '.env') }
        'Captain' { return Test-Path -LiteralPath (Join-Path $Context.Root '.captain-cook/demo-run.json') }
        'Hermes' { return Test-Path -LiteralPath (Join-Path $Context.Root '.captain-cook/hermes/Scripts/hermes.exe') }
        'Minibook' { return (Test-MinibookInstallation -Root $Context.Root) }
        'Services' { return (Get-CaptainServiceHealth -Root $Context.Root).Status -eq 'Ready' }
        'Verification' { return (Get-CaptainSystemStatus -Root $Context.Root).Status -eq 'Ready' }
        default { throw "Unbekannte Setup-Stage: $Stage" }
    }
}

Export-ModuleMember -Function @('Get-InvalidatedSetupStages', 'Test-SetupStage')
```

Update `Invoke-GuidedSetup` to accept `StageValidator`, validate completed stages in order, remove the first invalid stage and all successors from the in-memory checkpoint, persist the invalidated checkpoint, and execute the normal runner from that stage.

- [ ] **Step 4: Verify checkpoint behavior**

Run:

```powershell
$result = Invoke-Pester -Path scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
```

Expected: all Pester tests pass, including both new checkpoint tests.

- [ ] **Step 5: Commit the checkpoint contract**

```powershell
git add scripts/setup/StageValidation.psm1 scripts/setup/Lifecycle.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "fix: revalidate completed setup stages"
```

---

### Task 2: Make repair invalidate only broken stages

**Files:**
- Modify: `repair.ps1`
- Modify: `scripts/setup/Lifecycle.psm1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: `Test-SetupStage`, `Get-InvalidatedSetupStages` from Task 1.
- Produces: `Repair-CaptainSystem -Root string` returning a stable setup result with `Data.InvalidatedStages`.

- [ ] **Step 1: Add a failing repair test with a deliberately missing component**

```powershell
It 'invalidates a broken stage and all successors but preserves healthy predecessors' {
    $checkpoint = [ordered]@{
        Preflight='Complete'; Configuration='Complete'; Captain='Complete'
        Hermes='Complete'; Minibook='Complete'; Services='Complete'; Verification='Complete'
    }
    $checkpointPath = Join-Path $TestDrive '.captain-cook/checkpoint.json'
    Save-SetupCheckpoint -Path $checkpointPath -Stages $checkpoint

    $result = Repair-CaptainSystem -Root $TestDrive -StageValidator {
        param($stage, $context)
        $stage -ne 'Hermes'
    } -SetupRunner { New-SetupResult Setup Ready repaired None }

    $result.Data.InvalidatedStages | Should -Be @('Hermes','Minibook','Services','Verification')
    (Get-SetupCheckpoint -Path $checkpointPath).Captain | Should -Be 'Complete'
}
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed`

Expected: FAIL because `Repair-CaptainSystem` is undefined.

- [ ] **Step 3: Implement `Repair-CaptainSystem` and simplify `repair.ps1`**

Add `Repair-CaptainSystem` to `Lifecycle.psm1`. It must load the checkpoint, find the first completed stage whose validator returns false, remove it and successors, persist the reduced checkpoint, invoke the injected or default setup runner, and return the invalidated stage list. Replace the mutation logic in `repair.ps1` with:

```powershell
Import-Module (Join-Path $PSScriptRoot 'scripts/setup/Lifecycle.psm1') -Force
$result = Repair-CaptainSystem -Root $PSScriptRoot
Write-Host $result.Message
if ($result.Status -ne 'Ready') { exit 1 }
```

- [ ] **Step 4: Verify repair and regression behavior**

Run:

```powershell
$result = Invoke-Pester scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
```

Expected: all tests pass and the repair result lists exactly the invalid stage and successors.

- [ ] **Step 5: Commit repair behavior**

```powershell
git add repair.ps1 scripts/setup/Lifecycle.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "fix: repair invalid setup stages"
```

---

### Task 3: Bootstrap submodules and validate external n8n ownership

**Files:**
- Create: `scripts/setup/Repository.psm1`
- Modify: `.env.example`
- Modify: `scripts/setup/Lifecycle.psm1`
- Modify: `scripts/setup/Components.psm1`
- Modify: `scripts/setup/Setup.Tests.ps1`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: project root and `Common\Invoke-SetupCommand`.
- Produces: `Initialize-SetupSubmodules -Root string`, returning a setup result without resetting local changes.

- [ ] **Step 1: Add failing external-default and submodule tests**

```powershell
It 'defaults a new configuration to external n8n' {
    Set-Content (Join-Path $TestDrive '.env.example') 'N8N_MODE=external'
    $result = Initialize-SetupConfiguration -Root $TestDrive -SecretPathValidator { $true }
    $result.Data.Values.N8N_MODE | Should -Be 'external'
}

It 'initializes declared submodules without reset or clean' {
    $calls = [Collections.Generic.List[object]]::new()
    $result = Initialize-SetupSubmodules -Root $TestDrive -CommandRunner {
        param($file, $arguments, $directory)
        $calls.Add([pscustomobject]@{ File=$file; Arguments=@($arguments); Directory=$directory })
        [pscustomobject]@{ ExitCode=0; Output='' }
    } -HermesProbe { $true }

    $result.Status | Should -Be 'Ready'
    ($calls[0].Arguments -join ' ') | Should -Be 'submodule update --init --recursive'
    ($calls.Arguments -join ' ') | Should -Not -Match '(reset|clean|checkout)'
}
```

- [ ] **Step 2: Run tests and verify both contracts fail**

Run: `Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed`

Expected: external-default assertion or missing `Initialize-SetupSubmodules` fails.

- [ ] **Step 3: Implement safe repository bootstrap**

Create `Repository.psm1` with an injectable command runner. If `hermes-agent/pyproject.toml` exists, return `Ready` without Git mutation. Otherwise run exactly `git submodule update --init --recursive` in the repository root, then probe again. Return `Failed/Retry` for Git failure and `Missing/Manual` when the command succeeds but Hermes remains absent.

Change `.env.example` to:

```dotenv
N8N_MODE=external
N8N_URL=http://localhost:15678
N8N_CONTAINER_URL=http://host.docker.internal:15678
```

Import `Repository.psm1` in `Lifecycle.psm1` and call `Initialize-SetupSubmodules` immediately before `Install-Hermes`. Validate the configured external n8n health endpoint without invoking Compose for n8n.

- [ ] **Step 4: Verify both Compose modes**

Run:

```powershell
$env:MARIADB_PASSWORD='validation-only'
$env:MARIADB_ROOT_PASSWORD='validation-root-only'
docker compose --profile owned-n8n config --quiet
docker compose config --quiet
$result = Invoke-Pester scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
```

Expected: both Compose renders and all Pester tests pass.

- [ ] **Step 5: Commit standalone bootstrap**

```powershell
git add .env.example docker-compose.yml scripts/setup/Repository.psm1 scripts/setup/Lifecycle.psm1 scripts/setup/Components.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: bootstrap local dependencies safely"
```

---

### Task 4: Execute the complete preflight

**Files:**
- Modify: `scripts/setup/Preflight.psm1`
- Modify: `scripts/setup/Lifecycle.psm1:165-181`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: `Get-PreflightResults`.
- Produces: `Test-SetupPreflight -Root string -Configuration hashtable` with a single aggregate result and component details in `Data.Results`.

- [ ] **Step 1: Add failing version and port integration tests**

```powershell
It 'uses every aggregate preflight result in the real Preflight stage' {
    $results = @(
        New-SetupResult Python Invalid 'Python 3.10 erfüllt die Versionsanforderung nicht.' Install
        New-SetupResult 'Port 3456' Invalid 'Port 3456 wird verwendet.' Manual
    )
    $result = Test-SetupPreflight -Root $TestDrive -ResultProvider { $results }
    $result.Status | Should -Be 'Invalid'
    $result.Data.Results.Count | Should -Be 2
}

It 'returns RestartRequired when a newly installed executable is not visible' {
    $result = Confirm-InstalledPrerequisite -Name Python -Resolver { $null }
    $result.Status | Should -Be 'RestartRequired'
}
```

- [ ] **Step 2: Run focused tests and verify undefined contracts**

Run: `Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed`

Expected: FAIL because `Test-SetupPreflight` and `Confirm-InstalledPrerequisite` are undefined.

- [ ] **Step 3: Implement the aggregate preflight and wire it into setup**

Implement both functions in `Preflight.psm1`. `Test-SetupPreflight` returns `Ready` only when every supplied result is `Ready`; otherwise select the highest-impact status in this order: `RestartRequired`, `Missing`, `Invalid`, `Failed`, `Skipped`. Replace the hand-built checks in the `Preflight` setup stage with one `Test-SetupPreflight` call.

After `winget` succeeds in `setup.ps1`, call `Confirm-InstalledPrerequisite`. Print its remediation and exit non-zero on `RestartRequired` so the user can reopen PowerShell without rerunning completed work.

- [ ] **Step 4: Verify preflight behavior**

Run:

```powershell
$result = Invoke-Pester scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
```

Expected: all preflight and setup tests pass.

- [ ] **Step 5: Commit complete preflight wiring**

```powershell
git add setup.ps1 scripts/setup/Preflight.psm1 scripts/setup/Lifecycle.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "fix: enforce the complete setup preflight"
```

---

### Task 5: Share complete health across setup, start, and status

**Files:**
- Create: `scripts/setup/Health.psm1`
- Modify: `scripts/setup/Lifecycle.psm1`
- Modify: `scripts/setup/Configuration.psm1`
- Modify: `gateway/app.py`
- Modify: `tests/gateway/test_gateway.py`
- Modify: `.env.example`
- Modify: `status.ps1`
- Modify: `scripts/acceptance/setup-smoke.ps1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: `Read-DotEnv`, managed-process metadata, HTTP/TCP/MariaDB probes.
- Produces: `Get-CaptainHealthReport -Root string` and `Wait-CaptainSystemReady -Root string -TimeoutSeconds int`.

- [ ] **Step 1: Add a table-driven failing health test**

```powershell
It 'reports every promised component and fails when any one is unhealthy' {
    $components = @('Captain','Hermes CLI','Hermes Identity','Minibook Backend',
        'Minibook Frontend','Mailpit HTTP','Mailpit SMTP','MariaDB','Gateway','n8n')
    $probes = @{}
    foreach ($name in $components) {
        $probes[$name] = { New-SetupResult component Ready healthy None }
    }
    $probes['MariaDB'] = { New-SetupResult MariaDB Failed down Retry }

    $result = Get-CaptainHealthReport -Root $TestDrive -ProbeOverrides $probes

    $result.Status | Should -Be 'Failed'
    $result.Data.Results.Component | Should -Be $components
    ($result.Data.Results | Where-Object Component -eq MariaDB).Status | Should -Be 'Failed'
}
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed`

Expected: FAIL because `Get-CaptainHealthReport` is undefined.

- [ ] **Step 3: Implement complete health reporting**

Create `Health.psm1`. Each component probe returns one stable setup result.
Captain validation runs the demo verifier rather than checking only file
existence. Hermes CLI executes `hermes --help`; identity calls
`/api/v1/agents/me`; Minibook checks backend `/health` and frontend
`/api/v1/version`; Mailpit checks HTTP and SMTP; MariaDB uses
`Test-MariaDbService`; Gateway checks its database-backed `/healthz`; n8n
checks the configured `/healthz`.

Before wiring the Gateway probe, add an injected-store route test and implement
`GET /healthz` in `gateway/app.py`. It returns `{"status": "ready"}` only after
a `SELECT 1` through `GatewayStore`; storage failure returns 503 without DSN or
exception details. Add the real-MariaDB variant to the existing Gateway test
module; Task 6 is its mandatory execution gate. Add
`GATEWAY_URL=http://localhost:8090` to `.env.example`.

Add `New-CaptainLedgerDsn` to `Configuration.psm1`; percent-encode credentials
and keep the DSN in memory. Start Gateway through `Start-ManagedProcess` with
root `.venv/Scripts/python.exe -m gateway.app` after MariaDB is authenticated,
passing `LEDGER_DSN` only in the child environment. Store only PID, start time,
and process name. Stop it through the same PID/start-time ownership check used
for Minibook.

`Wait-CaptainSystemReady` polls the same report until all results are `Ready` or timeout expires. It returns the last report without swallowing component messages.

Replace `Get-CaptainSystemStatus` internals with a call to this module. After starting Minibook and Compose, `Start-CaptainSystem` must return `Wait-CaptainSystemReady`, not the Compose command result.

- [ ] **Step 4: Strengthen acceptance coverage**

In `setup-smoke.ps1`, assert that detailed status contains every component name. Add a start failure test with an injected health report where Minibook frontend is down and assert a non-ready result.

- [ ] **Step 5: Verify lifecycle health**

Run:

```powershell
$result = Invoke-Pester scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
pwsh -NoProfile -File scripts/acceptance/setup-smoke.ps1
```

Expected: all Pester and live acceptance items pass; the detailed table lists
ten components, including a Gateway row backed by MariaDB.

- [ ] **Step 6: Commit the shared health contract**

```powershell
git add scripts/setup/Health.psm1 scripts/setup/Lifecycle.psm1 scripts/setup/Configuration.psm1 gateway/app.py tests/gateway/test_gateway.py .env.example scripts/setup/Setup.Tests.ps1 scripts/acceptance/setup-smoke.ps1 status.ps1
git commit -m "feat: validate complete system health"
```

---

### Task 6: Add a mandatory isolated MariaDB and gateway gate

**Files:**
- Create: `docker-compose.test.yml`
- Create: `scripts/test_gateway.ps1`
- Create: `.github/workflows/integration.yml`
- Create: `tests/support/__init__.py`
- Create: `tests/support/mariadb.py`
- Create: `tests/test_mariadb_test_guard.py`
- Modify: `tests/gateway/test_gateway.py`
- Modify: `tests/blockchain/test_mariadb_storage.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Docker Compose v2 and existing `TEST_MARIADB_DSN` pytest fixtures.
- Produces: `pwsh -File scripts/test_gateway.ps1`, which exits non-zero on failures or skips.

- [ ] **Step 1: Add the isolated Compose service**

Create `docker-compose.test.yml`:

```yaml
name: captain-cook-test
services:
  mariadb-test:
    image: mariadb:11.8.8
    environment:
      MARIADB_DATABASE: captain_test
      MARIADB_USER: captain_test
      MARIADB_PASSWORD: "${MARIADB_TEST_PASSWORD:?required}"
      MARIADB_ROOT_PASSWORD: "${MARIADB_TEST_ROOT_PASSWORD:?required}"
    ports:
      - "127.0.0.1:${MARIADB_TEST_PORT:-33306}:3306"
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 2s
      timeout: 3s
      retries: 30
    tmpfs:
      - /var/lib/mysql
```

The tmpfs makes teardown disposable without touching Captain's `ledger_data` volume.

Before the script, add `tests/support/mariadb.py` with
`assert_isolated_test_database(dsn)`. Both destructive fixtures must call it
before `storage.clear()`. Accept only the exact database name `captain_test`;
reject `ledger`, `captain_ledger`, an empty path, and malformed DSNs. When
`REQUIRE_MARIADB_TESTS=1`, a missing DSN is a collection error instead of a
skip. Cover the guard in `tests/test_mariadb_test_guard.py`.

- [ ] **Step 2: Write the gate script and initially verify it detects skips**

Implement `scripts/test_gateway.ps1` to:

1. generate process-local test and root passwords with a cryptographic random
   generator and set the Compose interpolation variables without writing
   `.env`;
2. run `docker compose -f docker-compose.test.yml up -d --wait`;
3. construct a URL-encoded `TEST_MARIADB_DSN` only in the process environment
   and set `REQUIRE_MARIADB_TESTS=1`;
4. run `python -m pytest -q tests/blockchain/test_mariadb_storage.py tests/gateway/test_gateway.py -rs` and fail on any skip;
5. while the same isolated database and DSN are still active, run the complete
   pytest/coverage suite so its database tests execute rather than skip; only
   explicitly classified non-database degradation skips may remain;
6. fail on any test or coverage failure;
7. restore all previous environment values and always run
   `docker compose -f docker-compose.test.yml down --remove-orphans` in
   `finally`;
8. never pass `-v` or `--volumes` to `down` and never reference the production
   Compose file or `ledger_data`.

Run before setting the DSN inside the script once and confirm the script rejects the 22 skips. Then enable the DSN assignment.

- [ ] **Step 3: Run the real database contracts**

Run:

```powershell
pwsh -NoProfile -File scripts/test_gateway.ps1
```

Expected: 22 database/gateway tests execute, zero fail, zero skip, and the `captain-cook-test` Compose project is stopped.

- [ ] **Step 4: Add the CI integration job**

Create `.github/workflows/integration.yml` for `pull_request` and pushes to
`main`, with `contents: read` only and concurrency cancellation. Use two jobs:

- `python-integration` on `ubuntu-latest`: Python 3.11, install
  `requirements-dev.txt`, run `pip check`, the cross-platform PowerShell
  gateway script against the Linux MariaDB image (including the full
  pytest/coverage gate before teardown), then run `compileall`, Compose config,
  and submission verification.
- `windows-setup` on `windows-latest`: install Pester 6.0.0 and run
  `scripts/setup/Setup.Tests.ps1`, exiting non-zero on `FailedCount`.

Do not attempt to start the Linux MariaDB image on the Windows runner. Neither
job reads repository secrets.

- [ ] **Step 5: Verify workflow syntax and complete gate**

Run:

```powershell
$env:MARIADB_TEST_PASSWORD='validation-only'
$env:MARIADB_TEST_ROOT_PASSWORD='validation-root-only'
docker compose -f docker-compose.test.yml config --quiet
pwsh -NoProfile -File scripts/test_gateway.ps1
python -m pytest -q
```

Expected: Compose validates, database gate has zero skips, full suite passes.

- [ ] **Step 6: Commit the mandatory integration gate**

```powershell
git add docker-compose.test.yml scripts/test_gateway.ps1 .github/workflows/integration.yml tests/support tests/test_mariadb_test_guard.py tests/gateway/test_gateway.py tests/blockchain/test_mariadb_storage.py README.md
git commit -m "test: require real gateway integration evidence"
```

---

### Task 7: Segregate publish and local-subscription event-bus capabilities

**Files:**
- Modify: `agenten/runtime/event_bus.py`
- Modify: `agenten/runtime/autogen_bus.py`
- Modify: `agenten/ledger_bridge/recorder.py`
- Modify: `agenten/orchestration/pipeline.py`
- Modify: `tests/test_autogen_bus_integration.py`
- Modify: `tests/test_pipeline_autogen_subscription.py`
- Modify: `tests/ledger_bridge/test_recorder.py`
- Modify: `tests/test_e2e_smoke.py`
- Modify: `tests/test_architecture_fitness.py`

**Interfaces:**
- Consumes: the approved contract in
  `docs/superpowers/specs/2026-07-16-event-bus-capability-segregation-design.md`.
- Produces: publish-only `EventBus`, `SubscribableEventBus`, explicit
  `subscribe_recorder(...)`, and boot-time validation in `build_pipeline`.

- [ ] **Step 1: Add failing capability and composition tests**

```python
def test_autogen_bus_is_publish_only() -> None:
    bus = AutoGenEventBus(runtime=FakeRuntime())
    assert isinstance(bus, EventBus)
    assert not hasattr(bus, "subscribe")

def test_pipeline_rejects_publish_only_bus_before_wiring() -> None:
    with pytest.raises(TypeError, match="SubscribableEventBus"):
        build_pipeline(llm_decompose=fake_decompose, bus=PublishOnlyBus())
```

Also prove `InMemoryEventBus` is a `SubscribableEventBus`, explicit recorder
wiring covers every `RECORDER_TOPICS` entry, and no handler was registered when
the composition error is raised.

- [ ] **Step 2: Run the focused tests and verify the current contract mismatch**

Run:

```powershell
python -m pytest -q tests/test_autogen_bus_integration.py tests/test_pipeline_autogen_subscription.py tests/ledger_bridge/test_recorder.py -k "bus or subscrib"
```

Expected: FAIL because `EventBus` still requires `subscribe`,
`AutoGenEventBus.subscribe` still exists, and the recorder self-subscribes.

- [ ] **Step 3: Split the event-bus interfaces**

Implement the approved interface shape:

```python
class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: str, event: Any) -> None: ...


class SubscribableEventBus(EventBus, ABC):
    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None: ...
```

Derive `InMemoryEventBus` from `SubscribableEventBus`. Derive
`AutoGenEventBus` only from `EventBus` and remove its unsupported `subscribe`
method entirely. AutoGen topic registration remains in `subscribe_type` using
`TypeSubscription`.

- [ ] **Step 4: Extract recorder subscription ownership**

Rename `_SUBSCRIPTION_SPEC` to public `RECORDER_SUBSCRIPTION_SPEC`, derive
`RECORDER_TOPICS` from it, remove subscription calls from
`LedgerRecorderAgent.__init__`, and add:

```python
def subscribe_recorder(
    bus: SubscribableEventBus,
    recorder: LedgerRecorderAgent,
) -> None: ...
```

The helper rejects a publish-only bus with `TypeError`. The in-memory pipeline
calls it explicitly; `LedgerRecorderRoutedAgent` does not.

- [ ] **Step 5: Tighten the composition boundary**

Type `build_pipeline(..., bus: SubscribableEventBus | None = None)` and validate
an injected object before constructing any agent. Business agents that only
publish remain typed to `EventBus`. Add an AST fitness rule limiting production
callable `.subscribe()` usage to the in-memory composition/runtime boundary.

- [ ] **Step 6: Verify runtime, recorder, pipeline, and architecture tests**

Run:

```powershell
python -m pytest -q tests/test_autogen_bus_integration.py tests/test_pipeline_autogen_subscription.py tests/ledger_bridge/test_recorder.py tests/test_e2e_smoke.py tests/test_architecture_fitness.py tests/test_import_boundaries.py
python -m compileall -q agenten
```

Expected: all tests pass; unsupported composition fails before partial wiring;
real AutoGen `TypeSubscription` delivery remains green.

- [ ] **Step 7: Commit the capability segregation**

```powershell
git add agenten/runtime/event_bus.py agenten/runtime/autogen_bus.py agenten/ledger_bridge/recorder.py agenten/orchestration/pipeline.py tests/test_autogen_bus_integration.py tests/test_pipeline_autogen_subscription.py tests/ledger_bridge/test_recorder.py tests/test_e2e_smoke.py tests/test_architecture_fitness.py
git commit -m "refactor: segregate event bus capabilities"
```

---

### Task 8: Correct the URL relevance dependency direction

**Files:**
- Create: `agenten/adapters/__init__.py`
- Create: `agenten/adapters/url_relevance.py`
- Delete: `blockchain/web_scamler.py`
- Modify: `tests/test_architecture_fitness.py`
- Modify: `tests/architecture_fitness.py`
- Create: `tests/adapters/test_url_relevance.py`

**Interfaces:**
- Consumes: injected async content extraction, relevance scoring, and logging.
- Produces: typed `UrlRelevanceEvaluator` and `UrlEvaluationResult` in the
  adapter package. The legacy ledger module is removed after proving it has no
  active caller; no reverse-dependency shim remains under `blockchain/`.

- [ ] **Step 1: Prove there is no active legacy caller, then add red tests**

Run:

```powershell
rg -n "web_scamler|NestedChatForURLEvaluation" --glob "!docs/**" .
```

Expected before the move: only `blockchain/web_scamler.py` and boundary tests
match. If a real caller appears, add its explicit import migration to this task
before deleting the module.

```python
def test_blockchain_does_not_import_agenten() -> None:
    violations = find_boundary_violations(ROOT, rules_without_legacy_exception)
    assert not [v for v in violations if v.source.parts[0] == "blockchain"]

async def test_url_relevance_adapter_extracts_then_scores() -> None:
    async def extract(url: str) -> str:
        return "body"

    evaluator = UrlRelevanceEvaluator(extract=extract, score=lambda query, body: 0.75)
    result = await evaluator.evaluate_one("https://example.test", "query")
    assert result.score == 0.75
```

Also cover deterministic descending order, empty content, one failed URL not
aborting the batch, and use of the project logger instead of `print`.

- [ ] **Step 2: Run tests and verify the current reversed import fails**

Run: `python -m pytest -q tests/adapters/test_url_relevance.py tests/test_architecture_fitness.py`

Expected: FAIL because `blockchain/web_scamler.py` imports `agenten.functions`.

- [ ] **Step 3: Move implementation and remove the reverse dependency**

Implement `agenten/adapters/url_relevance.py`, delete
`blockchain/web_scamler.py`, remove that file from the legacy import-source list
in `tests/test_import_boundaries.py`, and remove the architecture-rule
exception. Keep extraction/scoring dependencies injectable and typed.

- [ ] **Step 4: Verify dependency direction and behavior**

Run:

```powershell
python -m pytest -q tests/adapters/test_url_relevance.py tests/test_architecture_fitness.py tests/test_import_boundaries.py
```

Expected: all tests pass and no `blockchain` module imports `agenten`.

- [ ] **Step 5: Commit the adapter boundary**

```powershell
git add agenten/adapters tests/adapters tests/architecture_fitness.py tests/test_architecture_fitness.py tests/test_import_boundaries.py docs/superpowers/plans/2026-07-15-architecture-gap-todos.md
git add -u blockchain/web_scamler.py
git commit -m "refactor: move URL relevance behind adapter boundary"
```

---

### Task 9: Split ledger recorder responsibilities behind its facade

**Files:**
- Create: `agenten/ledger_bridge/handlers.py`
- Create: `agenten/ledger_bridge/transitions.py`
- Create: `agenten/ledger_bridge/projections.py`
- Create: `agenten/ledger_bridge/autogen_adapter.py`
- Modify: `agenten/ledger_bridge/recorder.py`
- Modify: `tests/ledger_bridge/test_recorder.py`
- Modify: `tests/ledger_bridge/test_query.py`
- Create: `tests/ledger_bridge/test_recorder_facade.py`

**Interfaces:**
- Consumes: existing event schemas, stage machine, ledger storage, event bus.
- Produces: unchanged `LedgerRecorderAgent` constructor and handler methods; internal `LedgerTransitionApplier`, `LedgerProjectionIndex`, and `build_autogen_recorder_adapter`.

- [ ] **Step 1: Freeze the public recorder facade with contract tests**

Add tests that inspect the constructor signature, register the same event topics, process one successful terminal result and one unroutable result, restart from persisted storage, replay duplicate event IDs, and assert exactly one terminal block per event.

```python
def test_recorder_facade_keeps_public_constructor() -> None:
    parameters = inspect.signature(LedgerRecorderAgent).parameters
    assert tuple(parameters)[:3] == ("bus", "blockchain", "budget_ledger")
```

- [ ] **Step 2: Run the facade and existing recorder tests**

Run: `python -m pytest -q tests/ledger_bridge/test_recorder.py tests/ledger_bridge/test_query.py tests/ledger_bridge/test_recorder_facade.py`

Expected: existing behavior passes. The facade tests freeze current
at-least-once deduplication and recovery behavior; this refactor must not add a
new delivery guarantee.

- [ ] **Step 3: Extract projections without changing behavior**

Move projection/index state and query-update methods into `LedgerProjectionIndex`. Inject one instance into `LedgerRecorderAgent`. Run the focused suite and commit only after it remains green:

```powershell
git add agenten/ledger_bridge/projections.py agenten/ledger_bridge/recorder.py tests/ledger_bridge
git commit -m "refactor: extract ledger recorder projections"
```

- [ ] **Step 4: Extract transition application without changing behavior**

Move `_apply_*` lifecycle methods into `LedgerTransitionApplier`, preserving serialized sole-writer invocation through the recorder queue. Run the same focused suite, then commit:

```powershell
git add agenten/ledger_bridge/transitions.py agenten/ledger_bridge/recorder.py tests/ledger_bridge
git commit -m "refactor: extract ledger recorder transitions"
```

- [ ] **Step 5: Extract event intake and AutoGen wiring**

Move event-to-command translation into `handlers.py` and optional AutoGen classes/factories into `autogen_adapter.py`. Keep import degradation behavior when `autogen_core` is absent. Run:

```powershell
python -m pytest -q tests/ledger_bridge tests/test_autogen_bus_integration.py tests/test_import_boundaries.py
python -m compileall -q agenten/ledger_bridge
```

Expected: all tests pass and `recorder.py` contains the facade, queue/sole-writer lifecycle, and delegation only.

- [ ] **Step 6: Commit the final recorder split**

```powershell
git add agenten/ledger_bridge/handlers.py agenten/ledger_bridge/autogen_adapter.py agenten/ledger_bridge/recorder.py tests/ledger_bridge tests/test_autogen_bus_integration.py
git commit -m "refactor: isolate ledger recorder adapters"
```

---

### Task 10: Split pipeline construction behind `build_pipeline`

**Files:**
- Create: `agenten/orchestration/configuration.py`
- Create: `agenten/orchestration/components.py`
- Modify: `agenten/orchestration/pipeline.py`
- Modify: `tests/test_e2e_smoke.py`
- Modify: `tests/test_worker_factories.py`
- Modify: `tests/test_householder_runtime.py`
- Modify: `tests/test_architecture_fitness.py`
- Modify: `tests/test_import_boundaries.py`

**Interfaces:**
- Consumes: worker factories, event bus, ledger recorder/query, decomposition, constitution, supervision.
- Produces: unchanged `build_pipeline(...)`; internal immutable `PipelineConfiguration` and `build_pipeline_components(config)`.

- [ ] **Step 1: Freeze the composition-root behavior**

Add tests that build the deterministic offline pipeline using injected fakes, verify all expected subscriptions, run one four-role demo lifecycle, and compare terminal ledger output before refactoring. Add a test that invalid configuration fails before any subscription or worker construction.

- [ ] **Step 2: Run focused pipeline tests as the behavior baseline**

Run:

```powershell
python -m pytest -q tests/test_e2e_smoke.py tests/test_worker_factories.py tests/test_householder_runtime.py tests/test_architecture_fitness.py tests/test_import_boundaries.py
```

Expected: existing tests pass; the fail-before-construction assertion fails until configuration extraction exists.

- [ ] **Step 3: Extract immutable configuration**

Create `PipelineConfiguration` as a frozen, slotted dataclass. Validate bus capabilities, worker factory coverage, timing bounds, and required ledger ports in `__post_init__`. `build_pipeline` constructs this object before side effects.

- [ ] **Step 4: Extract component construction**

Move concrete coordinator, worker, supervisor, reaper, recorder, and adapter creation into `components.py`. Return a typed `PipelineComponents` dataclass. Keep subscription ordering and startup in `pipeline.py`.

- [ ] **Step 5: Verify composition and full runtime behavior**

Run:

```powershell
python -m pytest -q tests/test_e2e_smoke.py tests/test_worker_factories.py tests/test_householder_runtime.py tests/agenten tests/spawning tests/workers tests/ledger_bridge
python -m compileall -q agenten/orchestration
```

Expected: all tests pass; `build_pipeline` remains the public entry and invalid config has no construction side effects.

- [ ] **Step 6: Commit the pipeline split**

```powershell
git add agenten/orchestration/configuration.py agenten/orchestration/components.py agenten/orchestration/pipeline.py tests/test_e2e_smoke.py tests/test_worker_factories.py tests/test_householder_runtime.py tests/test_architecture_fitness.py tests/test_import_boundaries.py
git commit -m "refactor: separate pipeline configuration and construction"
```

---

### Task 11: Synchronize documentation, run clean-clone acceptance, and close the plan

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKSTREAMS.md`
- Modify: `docs/DEVPOST_CHECKLIST.md`
- Modify: `docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`
- Modify: `docs/superpowers/plans/2026-07-16-system-gap-remediation.md`
- Modify: `tests/test_workstream_docs.py`
- Modify: `tests/test_submission_docs.py`
- Modify: `tests/test_architecture_fitness.py`
- Modify: `scripts/verify_submission.py`

**Interfaces:**
- Consumes: proven behavior from Tasks 1–10.
- Produces: one consistent system description and final evidence checklist.

- [ ] **Step 1: Add failing documentation consistency assertions**

Assert all of the following:

```python
def test_docs_name_main_as_integration_baseline() -> None:
    assert "`main` is the current integration baseline" in WORKSTREAMS

def test_readme_preserves_external_n8n_ownership() -> None:
    assert "Existing VibeMind Compose project" in README
    assert "never delete or adopt either existing n8n volume" in README

def test_agent_guide_does_not_list_closed_unroutable_gap() -> None:
    assert "Permanently unroutable work lacks" not in AGENT_GUIDE
```

Extend `verify_submission.py` to reject the obsolete roadmap sentence that says the gateway, Hermes, n8n, Mailpit, and Minibook integrations are all absent.

- [ ] **Step 2: Run documentation tests and verify stale claims fail**

Run: `python -m pytest -q tests/test_workstream_docs.py tests/test_architecture_fitness.py`

Expected: FAIL on the old baseline, incorrect n8n ownership wording, or stale gap statement.

- [ ] **Step 3: Rewrite documentation to match proven behavior**

Document the offline demo and complete local system separately. Rewrite the architecture around:

```text
events → decomposition → constitution → spawn coordinator → workers
       → supervisor/reaper → sole-writer recorder → query/projections
```

Name `main` as the baseline, mark historical feature branches merged without deleting them, document external VibeMind n8n ownership, and check only backlog items whose named tests passed.

Refresh `docs/DEVPOST_CHECKLIST.md` with the current commit, dated commands,
and skip classification instead of treating old pass totals as permanent.
Keep root-license selection, repository publication, public video, Devpost
form submission, and `/feedback` session ID as explicit unchecked owner
actions. Do not select a license or claim publication in this branch.

- [ ] **Step 4: Run the full static and regression gate**

Run:

```powershell
$pester = Invoke-Pester scripts/setup/Setup.Tests.ps1 -PassThru
if ($pester.FailedCount -gt 0) { exit 1 }
pwsh -NoProfile -File scripts/test_gateway.ps1
python -m pytest -q
python scripts/verify_submission.py
python -m compileall -q agenten blockchain chats config gateway
$env:MARIADB_PASSWORD='validation-only'
$env:MARIADB_ROOT_PASSWORD='validation-root-only'
docker compose --profile owned-n8n config --quiet
```

Expected: zero failures and zero required integration skips.

- [ ] **Step 5: Run clean-clone Windows acceptance**

From a new temporary directory outside all existing worktrees:

```powershell
$sourceRepository = git rev-parse --show-toplevel
$sourceBranch = git branch --show-current
$cleanRoot = Join-Path $env:TEMP ("captain-cook-clean-" + [guid]::NewGuid())
git clone --recurse-submodules --branch $sourceBranch $sourceRepository $cleanRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Set-Location $cleanRoot
pwsh -NoProfile -File setup.ps1
pwsh -NoProfile -File status.ps1 -Detailed
pwsh -NoProfile -File repair.ps1
pwsh -NoProfile -File scripts/acceptance/setup-smoke.ps1
```

Expected: setup completes with `N8N_MODE=external` against the explicitly
configured reachable endpoint, ten health rows including Gateway are ready,
repair is idempotent, and no command starts, stops, or adopts VibeMind n8n.
Record only command, commit, timestamps, exit codes, and non-secret run
identifiers in the Session Insights entry.

- [ ] **Step 6: Audit every completion criterion and consolidate insights**

For each of the eight design completion criteria, add its exact evidence under `Final acceptance evidence`. Search the Session Insights section for `Consolidated into: none`; the search must return no actionable entry. Search for unchecked task boxes; only account-owned or explicitly out-of-scope actions may remain.

- [ ] **Step 7: Commit synchronized documentation and final evidence**

```powershell
git add README.md AGENTS.md docs/ARCHITECTURE.md docs/WORKSTREAMS.md docs/DEVPOST_CHECKLIST.md docs/superpowers/plans/2026-07-15-architecture-gap-todos.md docs/superpowers/plans/2026-07-16-system-gap-remediation.md tests/test_workstream_docs.py tests/test_submission_docs.py tests/test_architecture_fitness.py scripts/verify_submission.py
git commit -m "docs: align system claims with verified behavior"
```

---

## Final acceptance evidence

Populate this table only with fresh evidence from Task 11.

| Criterion | Evidence command or artifact | Result |
| --- | --- | --- |
| Clean development install is internally consistent | disposable venv `pip check` and pytest/coverage | Not run |
| Stale checkpoint repairs a removed component | `Invoke-Pester ... -Filter *revalidates*` | Not run |
| Clean checkout validates configured external n8n without adopting it | clean-clone acceptance manifest | Not run |
| Invalid versions and unknown ports fail preflight | Pester preflight cases | Not run |
| Every promised component can independently fail status | Pester table-driven health cases | Not run |
| MariaDB and gateway contracts execute with zero skips | `scripts/test_gateway.ps1` | Not run |
| Docs and architecture agree with tests and runtime | docs/fitness suite plus verifier | Not run |
| Runtime and module boundaries are closed | capability, import, recorder, pipeline suites | Not run |
| No actionable insight remains unconsolidated | insight audit command | Not run |

## Session Insights

### 2026-07-16 — Post-merge gap audit and plan design

- Evidence: `main` audit at `b0038a8`; 37 Pester tests passed; standard pytest reported 22 MariaDB/gateway skips; a completed-checkpoint probe returned `status=Ready; stages_called=0`.
- Insight: Checkpoint success is currently treated as permanent and the standard green suite does not execute the database-backed gateway contracts.
- Decision: Revalidate completed stages, make the database gate mandatory, and manage all seven findings through this master plan.
- Consolidated into: `Task 1, Steps 1–4`; `Task 6, Steps 1–5`; `Final acceptance evidence`.
- Supersedes: none.

### Example for the next implementation session

Copy the structure and replace its concrete evidence with the new session's
actual commands and observations:

```markdown
### 2026-07-17 09:00 Europe/Berlin — Checkpoint revalidation

- Evidence: `Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed` fails the stale-checkpoint test before implementation.
- Insight: A completed Minibook checkpoint bypasses its health validator.
- Decision: Invalidate Minibook and every downstream stage while preserving healthy predecessors.
- Consolidated into: `Task 1, Step 3`.
- Supersedes: none.
```

Every entry must point to an exact task or acceptance criterion before the
session ends. Never copy secret values, `.env` contents, credentials, tokens,
or raw logs that may contain them into this document.
