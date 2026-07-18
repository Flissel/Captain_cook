# Guided Windows Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable one-command Windows 11 setup assistant that installs, configures, starts, and verifies the complete Captain Cook local system while guiding a new user through anything missing.

**Architecture:** Root lifecycle scripts call focused PowerShell modules under `scripts/setup/`. Modules return a shared structured result and never prompt directly; `setup.ps1` owns interaction, checkpoints, safe logging, and remediation. Pester tests isolate all system mutation behind injected command adapters, while a manual Windows acceptance script verifies the real stack.

**Tech Stack:** PowerShell 7, Pester 5, Python 3.11, Node.js 20+, Docker Desktop with Compose v2, Captain Cook, Hermes Agent, Minibook, n8n, Mailpit, MariaDB 11.8

## Global Constraints

- The first release supports native Windows 11 and PowerShell 7.
- The only onboarding command is `.\setup.ps1` from the repository root.
- Automatic installation always requires explicit confirmation.
- Avoid administrator elevation when a per-user install is available.
- Never echo, log, commit, or place a secret in a tracked file.
- Never stop, recreate, or delete an adopted external n8n service.
- Never remove Docker volumes from setup, repair, start, or stop commands.
- Existing valid configuration and healthy components are preserved.
- Every successful stage must be verified through its public interface.
- All root scripts resolve paths relative to `$PSScriptRoot`.

---

## File Structure

- `setup.ps1`: interactive orchestration, resume, and final summary.
- `start.ps1`: start Captain-owned processes and containers.
- `stop.ps1`: stop Captain-owned processes and containers without data loss.
- `status.ps1`: concise or detailed sanitized health report.
- `repair.ps1`: rerun setup in repair mode.
- `scripts/setup/Common.psm1`: result contract, command adapter, safe logging, checkpoints.
- `scripts/setup/Preflight.psm1`: platform, executable, disk, network, Docker, and port checks.
- `scripts/setup/Configuration.psm1`: `.env` parsing/writing, secret prompts, validation, n8n mode.
- `scripts/setup/Components.psm1`: Captain, Hermes, and Minibook installation and verification.
- `scripts/setup/Services.psm1`: Compose generation/start/stop and service health checks.
- `scripts/setup/Lifecycle.psm1`: shared start, stop, status, and repair operations.
- `scripts/setup/Setup.Tests.ps1`: Pester unit and orchestration tests.
- `scripts/acceptance/setup-smoke.ps1`: real Windows acceptance checks.
- `docker-compose.yml`: Captain-owned Mailpit, MariaDB, and optional n8n profiles.
- `.env.example`: documented names and non-secret defaults.
- `.gitignore`: setup runtime, logs, checkpoints, and generated process metadata.
- `README.md`: one-command onboarding, URLs, lifecycle, and troubleshooting.

---

### Task 1: Shared Result, Adapter, Logging, and Checkpoint Engine

**Files:**
- Create: `scripts/setup/Common.psm1`
- Create: `scripts/setup/Setup.Tests.ps1`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: repository root as `System.IO.DirectoryInfo`.
- Produces: `New-SetupResult`, `Invoke-SetupCommand`, `Write-SetupLog`, `Save-SetupCheckpoint`, `Get-SetupCheckpoint`, and `Test-InteractiveSession`.

- [ ] **Step 1: Write failing contract and redaction tests**

```powershell
BeforeAll { Import-Module "$PSScriptRoot/Common.psm1" -Force }

Describe 'Common setup contracts' {
    It 'creates a stable result object' {
        $result = New-SetupResult -Component Python -Status Missing -Message 'Python 3.11 fehlt' -Remediation Install
        $result.PSObject.Properties.Name | Should -Be @('Component','Status','Message','Remediation','Data')
        $result.Status | Should -Be 'Missing'
    }

    It 'redacts every supplied secret before writing a log' {
        $path = Join-Path $TestDrive 'setup.log'
        Write-SetupLog -Path $path -Message 'token=abc password=xyz' -Secrets @('abc','xyz')
        (Get-Content $path -Raw) | Should -BeLike '*token=*** password=***'
    }

    It 'round-trips a non-secret checkpoint' {
        $path = Join-Path $TestDrive 'checkpoint.json'
        Save-SetupCheckpoint -Path $path -Stages @{ Preflight = 'Complete' }
        (Get-SetupCheckpoint -Path $path).Preflight | Should -Be 'Complete'
    }
}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: FAIL because `Common.psm1` and its exported functions do not exist.

- [ ] **Step 3: Implement the shared engine**

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-SetupResult {
    param(
        [Parameter(Mandatory)][string]$Component,
        [Parameter(Mandatory)][ValidateSet('Ready','Missing','Invalid','Failed','Skipped','RestartRequired')][string]$Status,
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][ValidateSet('None','Install','Configure','Retry','Restart','Manual')][string]$Remediation,
        [hashtable]$Data = @{}
    )
    [pscustomobject][ordered]@{ Component=$Component; Status=$Status; Message=$Message; Remediation=$Remediation; Data=$Data }
}

function Invoke-SetupCommand {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$ArgumentList=@(), [string]$WorkingDirectory=$PWD.Path)
    $output = & $FilePath @ArgumentList 2>&1
    [pscustomobject]@{ ExitCode=$LASTEXITCODE; Output=($output -join [Environment]::NewLine) }
}

function Write-SetupLog {
    param([string]$Path,[string]$Message,[string[]]$Secrets=@())
    $safe = $Message
    foreach ($secret in ($Secrets | Where-Object { $_ })) { $safe = $safe.Replace($secret, '***') }
    $directory = Split-Path $Path -Parent
    if ($directory) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    Add-Content -LiteralPath $Path -Value "$(Get-Date -Format o) $safe"
}

function Save-SetupCheckpoint { param([string]$Path,[hashtable]$Stages) $Stages | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding utf8 }
function Get-SetupCheckpoint { param([string]$Path) if (Test-Path $Path) { Get-Content $Path -Raw | ConvertFrom-Json } else { [pscustomobject]@{} } }
function Test-InteractiveSession { [Environment]::UserInteractive -and -not [Console]::IsInputRedirected }

Export-ModuleMember -Function New-SetupResult,Invoke-SetupCommand,Write-SetupLog,Save-SetupCheckpoint,Get-SetupCheckpoint,Test-InteractiveSession
```

Add to `.gitignore`:

```gitignore
.captain-cook/
```

- [ ] **Step 4: Run contract tests**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add .gitignore scripts/setup/Common.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add setup runtime contracts"
```

---

### Task 2: Preflight Detection and Guided Prerequisite Installation

**Files:**
- Create: `scripts/setup/Preflight.psm1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: `New-SetupResult`, optional `CommandRunner` scriptblock.
- Produces: `Test-SetupPlatform`, `Test-SetupExecutable`, `Test-SetupPort`, `Get-PreflightResults`, `Install-SetupPrerequisite`.

- [ ] **Step 1: Add failing preflight tests**

```powershell
Describe 'Preflight' {
    BeforeAll { Import-Module "$PSScriptRoot/Preflight.psm1" -Force }

    It 'marks an absent executable as installable' {
        $result = Test-SetupExecutable -Name 'python' -MinimumVersion '3.11' -Resolver { $null }
        $result.Status | Should -Be 'Missing'
        $result.Remediation | Should -Be 'Install'
    }

    It 'reports a port owner without killing it' {
        $result = Test-SetupPort -Port 3457 -ConnectionProvider { [pscustomobject]@{ OwningProcess=4242 } }
        $result.Status | Should -Be 'Invalid'
        $result.Data.OwningProcess | Should -Be 4242
    }

    It 'uses only approved winget package identifiers' {
        $calls = [Collections.Generic.List[object]]::new()
        Install-SetupPrerequisite -Name Git -ConfirmInstall { $true } -CommandRunner { param($f,$a) $calls.Add(@($f,$a)); [pscustomobject]@{ExitCode=0;Output='ok'} }
        $calls[0][1] -join ' ' | Should -Match 'Git.Git'
    }
}
```

- [ ] **Step 2: Run the targeted tests**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*Preflight*'"`

Expected: FAIL because `Preflight.psm1` does not exist.

- [ ] **Step 3: Implement preflight checks and approved installs**

Implement an allowlist with exact IDs:

```powershell
$script:Packages = @{
    Git='Git.Git'; Python='Python.Python.3.11'; Node='OpenJS.NodeJS.LTS'; Docker='Docker.DockerDesktop'; PowerShell='Microsoft.PowerShell'
}

function Install-SetupPrerequisite {
    param([string]$Name,[scriptblock]$ConfirmInstall,[scriptblock]$CommandRunner=${function:Invoke-SetupCommand})
    if (-not $script:Packages.ContainsKey($Name)) { throw "Nicht unterstützte Voraussetzung: $Name" }
    if (-not (& $ConfirmInstall)) { return New-SetupResult $Name Skipped 'Installation übersprungen' Manual }
    & $CommandRunner 'winget' @('install','--id',$script:Packages[$Name],'-e','--accept-package-agreements','--accept-source-agreements')
}
```

Add platform validation for Windows 11 build `>= 22000`, PowerShell `>= 7.0`, Python `>= 3.11,<3.14`, Node `>= 20`, Docker engine and Compose v2, 4 GB free disk, network reachability, and ports `3456`, `3457`, `5678`, `8025`, `1025`, and `3306`. Return results; never call `Stop-Process`.

- [ ] **Step 4: Run all Pester tests**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add scripts/setup/Preflight.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add guided prerequisite checks"
```

---

### Task 3: Safe Local Configuration and n8n Mode Selection

**Files:**
- Create: `scripts/setup/Configuration.psm1`
- Modify: `.env.example`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: root `.env`, `.env.example`, masked prompt provider, HTTP probe.
- Produces: `Read-DotEnv`, `Write-DotEnv`, `Get-OrCreateSecret`, `Resolve-N8nMode`, `Test-TrackedSecretPath`.

- [ ] **Step 1: Add failing configuration tests**

```powershell
Describe 'Configuration' {
    BeforeAll { Import-Module "$PSScriptRoot/Configuration.psm1" -Force }

    It 'preserves existing values and safely quotes special characters' {
        $path = Join-Path $TestDrive '.env'
        Set-Content $path 'EXISTING=keep'
        Write-DotEnv -Path $path -Values @{ EXISTING='keep'; DB_PASSWORD='a# b' }
        (Read-DotEnv $path).DB_PASSWORD | Should -Be 'a# b'
    }

    It 'rejects a tracked secret path' {
        Test-TrackedSecretPath -Path '.env' -GitRunner { 'tracked.env' } | Should -BeFalse
    }

    It 'never adopts an external n8n endpoint without consent' {
        $mode = Resolve-N8nMode -Url 'http://localhost:15678' -Probe { $true } -ConfirmAdoption { $false }
        $mode | Should -Be 'Owned'
    }
}
```

- [ ] **Step 2: Verify tests fail**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*Configuration*'"`

Expected: FAIL because the configuration module is absent.

- [ ] **Step 3: Implement deterministic env parsing and secret protection**

Use double-quoted dotenv values with escaping for backslash, quote, carriage return, and newline. Generate passwords using `RandomNumberGenerator.GetBytes(32)` converted with Base64Url-safe substitutions. Use `Read-Host -AsSecureString` and in-memory conversion only at the point of validation/write. Run `git check-ignore -q -- <path>` and `git ls-files --error-unmatch -- <path>` before writing any secret.

Add these non-secret names/defaults to `.env.example`:

```dotenv
CAPTAIN_TIMEZONE=Europe/Berlin
MINIBOOK_PUBLIC_URL=http://localhost:3457
MINIBOOK_BACKEND_URL=http://localhost:3456
MAILPIT_WEB_PORT=8025
MAILPIT_SMTP_PORT=1025
MARIADB_PORT=3306
MARIADB_DATABASE=captain_ledger
MARIADB_USER=captain
N8N_MODE=owned
N8N_URL=http://localhost:5678
N8N_CONTAINER_URL=http://n8n:5678
```

Keep `OPENAI_API_KEY`, `N8N_MCP_TOKEN`, `MARIADB_PASSWORD`, `MARIADB_ROOT_PASSWORD`, `MINIBOOK_API_KEY`, and `MINIBOOK_PROJECTION_API_KEY` empty in the example.

- [ ] **Step 4: Run configuration and full tests**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS and no test output contains fixture secrets.

- [ ] **Step 5: Commit**

```powershell
git add .env.example scripts/setup/Configuration.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add safe setup configuration"
```

---

### Task 4: Captain-Owned Docker Services

**Files:**
- Create: `docker-compose.yml`
- Create: `scripts/setup/Services.psm1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: parsed environment, `N8N_MODE`, command and HTTP adapters.
- Produces: `Start-CaptainServices`, `Stop-CaptainServices`, `Get-ServiceHealth`, `Test-N8nReachability`.

- [ ] **Step 1: Add failing ownership and safety tests**

```powershell
Describe 'Services' {
    BeforeAll { Import-Module "$PSScriptRoot/Services.psm1" -Force }

    It 'does not start or stop n8n in external mode' {
        $calls = [Collections.Generic.List[string]]::new()
        Start-CaptainServices -N8nMode External -CommandRunner { param($f,$a) $calls.Add("$f $($a -join ' ')") }
        ($calls -join "`n") | Should -Not -Match 'profile owned-n8n'
    }

    It 'contains no destructive volume operation' {
        $content = Get-Content "$PSScriptRoot/../../docker-compose.yml" -Raw
        $content | Should -Not -Match 'down\s+-v|volume\s+rm|docker\s+rm'
    }
}
```

- [ ] **Step 2: Verify service tests fail**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*Services*'"`

Expected: FAIL because Compose and `Services.psm1` are absent.

- [ ] **Step 3: Define Compose services and health checks**

Create fixed services: `axllent/mailpit:v1.27.8`, `mariadb:11.8.5`, and `n8nio/n8n:2.4.8` behind profile `owned-n8n`. Use named volumes `ledger_data` and `n8n_data`, `restart: unless-stopped`, localhost-bound published ports, required password interpolation (`${NAME:?message}`), and service-native healthchecks. Do not set explicit `container_name` values.

Implement mode-specific commands:

```powershell
$arguments = @('compose','--env-file',$EnvPath)
if ($N8nMode -eq 'Owned') { $arguments += @('--profile','owned-n8n') }
$arguments += @('up','-d','--wait')
& $CommandRunner 'docker' $arguments
```

Stop uses `docker compose ... stop` and never `down -v`. Health probes cover Mailpit `/api/v1/info`, SMTP TCP, authenticated `SELECT 1`, n8n host URL, and owned-mode container DNS.

- [ ] **Step 4: Validate Compose and tests**

Run: `docker compose --env-file .env config --quiet`

Expected: exit 0 with a locally configured `.env`.

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add docker-compose.yml scripts/setup/Services.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add owned local support services"
```

---

### Task 5: Captain, Hermes, and Minibook Component Installers

**Files:**
- Create: `scripts/setup/Components.psm1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: repository root, command/HTTP adapters, local secret store.
- Produces: `Install-Captain`, `Install-Hermes`, `Install-Minibook`, `Install-MinibookSkill`, `Register-HermesIdentity`, `Test-ComponentHealth`.

- [ ] **Step 1: Add failing idempotency and verification tests**

```powershell
Describe 'Components' {
    BeforeAll { Import-Module "$PSScriptRoot/Components.psm1" -Force }

    It 'skips dependency installation after a verified healthy check' {
        $calls = [Collections.Generic.List[string]]::new()
        Install-Captain -Root $TestDrive -HealthCheck { $true } -CommandRunner { param($f,$a) $calls.Add($f) }
        $calls.Count | Should -Be 0
    }

    It 'registers Hermes only when no valid Minibook identity exists' {
        $posts = 0
        Register-HermesIdentity -CurrentIdentityProbe { $true } -RegistrationRequest { $script:posts++ }
        $posts | Should -Be 0
    }
}
```

- [ ] **Step 2: Verify tests fail**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*Components*'"`

Expected: FAIL because `Components.psm1` is absent.

- [ ] **Step 3: Implement isolated, verified component setup**

Captain commands:

```powershell
python -m venv "$Root/.venv"
& "$Root/.venv/Scripts/python.exe" -m pip install -r "$Root/requirements.txt"
& "$Root/.venv/Scripts/python.exe" "$Root/main.py" demo --output "$Root/artifacts/demo-run.json"
```

Hermes uses its checked-in Windows installer/local editable source path without invoking a remote pipe, then verifies `hermes --help`. Minibook uses `minibook/.venv`, installs `minibook/requirements.txt`, runs `npm ci` when a lockfile exists (otherwise `npm install`) in `minibook/frontend`, and builds the frontend. Start processes hidden with PID metadata below `.captain-cook/runtime/`.

Copy `minibook/skills/minibook/SKILL.md` to the discovered Hermes user skill directory. Register `Hermes` through `POST /api/v1/agents` only if `GET /api/v1/agents/me` cannot validate an existing stored credential. Store the one-time API key in the Hermes user profile and verify it immediately.

- [ ] **Step 4: Run component and full tests**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add scripts/setup/Components.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add verified component installers"
```

---

### Task 6: Interactive Orchestrator and Lifecycle Commands

**Files:**
- Create: `scripts/setup/Lifecycle.psm1`
- Create: `setup.ps1`
- Create: `start.ps1`
- Create: `stop.ps1`
- Create: `status.ps1`
- Create: `repair.ps1`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: all module interfaces from Tasks 1–5.
- Produces: stable root commands and `Invoke-GuidedSetup`, `Start-CaptainSystem`, `Stop-CaptainSystem`, `Get-CaptainSystemStatus`.

- [ ] **Step 1: Add failing orchestration tests**

```powershell
Describe 'Guided orchestration' {
    It 'resumes at the first incomplete stage' {
        $visited = [Collections.Generic.List[string]]::new()
        Invoke-GuidedSetup -Checkpoint @{Preflight='Complete';Configuration='Incomplete'} -StageRunner { param($stage) $visited.Add($stage); 'Complete' }
        $visited[0] | Should -Be 'Configuration'
    }

    It 'does not report success when verification fails' {
        $result = Invoke-GuidedSetup -StageRunner { param($stage) if ($stage -eq 'Verification') {'Failed'} else {'Complete'} }
        $result.Status | Should -Be 'Failed'
    }
}
```

- [ ] **Step 2: Verify orchestration tests fail**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*orchestration*'"`

Expected: FAIL because lifecycle/orchestration functions are absent.

- [ ] **Step 3: Implement the stage machine and root wrappers**

Use exact stage order:

```powershell
$stages = @('Preflight','Configuration','Captain','Hermes','Minibook','Services','Verification')
```

For each stage, skip `Complete`, run its check, display the safe result, obtain explicit confirmation before install/elevation, save the new checkpoint immediately, and stop with exit code `3010` for restart-required or `1` for unresolved required failures. Exit `0` only when Verification is complete.

Each root wrapper starts with:

```powershell
#requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Import-Module (Join-Path $root 'scripts/setup/Lifecycle.psm1') -Force
```

`repair.ps1` calls the same orchestrator with `-Repair`; `status.ps1` adds `[switch]$Detailed`; start and stop operate only on Captain-owned resources.

- [ ] **Step 4: Run Pester and non-interactive smoke checks**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS.

Run: `pwsh -NoProfile -File status.ps1 -Detailed`

Expected: exit 0 for healthy/incomplete diagnostic output, with no secret values.

- [ ] **Step 5: Commit**

```powershell
git add setup.ps1 start.ps1 stop.ps1 status.ps1 repair.ps1 scripts/setup/Lifecycle.psm1 scripts/setup/Setup.Tests.ps1
git commit -m "feat: add guided setup and lifecycle commands"
```

---

### Task 7: Documentation and End-to-End Acceptance

**Files:**
- Create: `scripts/acceptance/setup-smoke.ps1`
- Modify: `README.md`
- Modify: `scripts/setup/Setup.Tests.ps1`

**Interfaces:**
- Consumes: complete installed system and lifecycle commands.
- Produces: executable acceptance evidence and beginner onboarding documentation.

- [ ] **Step 1: Add failing documentation contract test**

```powershell
Describe 'Onboarding documentation' {
    It 'documents the only setup command and all lifecycle commands' {
        $readme = Get-Content "$PSScriptRoot/../../README.md" -Raw
        foreach ($command in @('.\setup.ps1','.\start.ps1','.\stop.ps1','.\status.ps1','.\repair.ps1')) {
            $readme | Should -Match ([regex]::Escape($command))
        }
    }
}
```

- [ ] **Step 2: Verify documentation test fails**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed -FullNameFilter '*Onboarding*'"`

Expected: FAIL because README does not contain the lifecycle workflow.

- [ ] **Step 3: Write onboarding, troubleshooting, and acceptance script**

Lead README with:

```markdown
## Einfache Einrichtung unter Windows 11

Öffne PowerShell 7 im Projektordner und starte:

```powershell
.\setup.ps1
```

Der Assistent prüft deinen Computer, erklärt fehlende Komponenten und fragt vor jeder Installation oder Änderung nach.
```

Document local URLs, optional credentials, resume behavior, the four lifecycle commands, remediation codes, logs, manual fallback, and the warning that no lifecycle command deletes Docker volumes.

The acceptance script must call the Captain demo, `hermes --help`, Minibook health/version/skill endpoints, authenticated identity endpoint, Mailpit API and SMTP, authenticated MariaDB `SELECT 1`, n8n host and container probes, every lifecycle command, a second setup run in non-interactive check mode, `git status --short`, and a scan of logs for known fixture secrets. It exits nonzero on any unmet requirement and prints one PASS/FAIL line per item.

- [ ] **Step 4: Run complete verification**

Run: `pwsh -NoProfile -Command "Invoke-Pester scripts/setup/Setup.Tests.ps1 -Output Detailed"`

Expected: all tests PASS.

Run: `python -m pytest -q`

Expected: existing Python regression suite PASS.

Run: `python scripts/verify_submission.py`

Expected: submission verification PASS.

Run on a configured Windows 11 host: `pwsh -NoProfile -File scripts/acceptance/setup-smoke.ps1`

Expected: every acceptance item prints PASS and the script exits 0. If Docker or credentials are intentionally unavailable, record the acceptance run as incomplete; do not claim full completion.

- [ ] **Step 5: Commit**

```powershell
git add README.md scripts/acceptance/setup-smoke.ps1 scripts/setup/Setup.Tests.ps1
git commit -m "docs: add guided setup onboarding"
```

---

## Completion Audit

Before declaring the goal complete, map every acceptance item in
`docs/superpowers/specs/2026-07-15-guided-windows-setup-design.md` to current
command output. Confirm both a clean-profile run and an idempotent second run,
verify no tracked or untracked generated secret appears in `git status`, and
inspect sanitized logs. Missing real-host evidence means the goal remains
incomplete even when unit tests pass.
