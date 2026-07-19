[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Hermes = Join-Path $Root "hermes-agent"
$RequiredEntrypoints = @(
    "hermes_cli/captain_planner.py",
    "hermes_cli/mcp_config.py"
)
$RequiredFixture = "tests/fixtures/captain_work_package_released.v1.json"
$FocusedTests = @(
    "tests/hermes_cli/test_captain_planner.py",
    "tests/hermes_cli/test_n8n_worker_mcp.py"
)

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repository,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = @(& git -C $Repository @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Git verification failed."
    }
    return @($output | ForEach-Object { [string]$_ })
}

if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required to verify the Hermes runtime."
}
if ($null -eq (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is required to verify the Hermes runtime."
}
if (-not (Test-Path -LiteralPath $Hermes -PathType Container)) {
    throw "Hermes submodule is not initialized."
}

$treeEntry = (Invoke-Git -Repository $Root -Arguments @("ls-tree", "HEAD", "--", "hermes-agent")) | Select-Object -First 1
if ($treeEntry -notmatch "^160000 commit ([0-9a-f]{40})\s+hermes-agent$") {
    throw "Parent repository does not pin a Hermes gitlink."
}
$PinnedCommit = $Matches[1]

$submoduleStatus = @(Invoke-Git -Repository $Root -Arguments @("submodule", "status", "--recursive", "--", "hermes-agent"))
if ($submoduleStatus.Count -eq 0) {
    throw "Hermes submodule is not initialized."
}
foreach ($line in $submoduleStatus) {
    if ($line -match "^[+\-U]") {
        throw "Hermes submodule is uninitialized, divergent, or conflicted."
    }
}

$CheckedOutCommit = ((Invoke-Git -Repository $Hermes -Arguments @("rev-parse", "HEAD")) | Select-Object -First 1).Trim()
if ($CheckedOutCommit -ne $PinnedCommit) {
    throw "Hermes checkout does not match the parent gitlink."
}

$superproject = ((Invoke-Git -Repository $Hermes -Arguments @("rev-parse", "--show-superproject-working-tree")) | Select-Object -First 1).Trim()
if ([string]::IsNullOrWhiteSpace($superproject) -or (Resolve-Path -LiteralPath $superproject).Path -ne $Root) {
    throw "Hermes checkout is not initialized as this parent submodule."
}

$dirtyPaths = @(Invoke-Git -Repository $Hermes -Arguments @("status", "--porcelain", "--untracked-files=all"))
if ($dirtyPaths.Count -gt 0) {
    throw "Hermes submodule has uncommitted or untracked changes."
}

foreach ($relativePath in @($RequiredEntrypoints + $RequiredFixture)) {
    if (-not (Test-Path -LiteralPath (Join-Path $Hermes $relativePath) -PathType Leaf)) {
        throw "Hermes required surface is missing."
    }
}

Push-Location -LiteralPath $Hermes
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & python -c "import hermes_cli.captain_planner; import hermes_cli.mcp_config" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes required entrypoint imports failed."
    }

    & python -m pytest -q @FocusedTests 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes focused tests failed."
    }
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $n8nServerIdentity = (& python -c "from agenten.agent_runtime.n8n_endpoint import HermesN8nReference; print(HermesN8nReference(endpoint_identity='redacted').server_name)" 2>$null).Trim()
    $n8nReferenceExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($n8nReferenceExitCode -ne 0 -or $n8nServerIdentity -ne "n8n-mcp") {
    throw "Captain Hermes n8n reference is unavailable."
}

Write-Output "hermes_commit=$PinnedCommit"
Write-Output "entrypoints=$($RequiredEntrypoints -join ',')"
Write-Output "tests=passed"
Write-Output "n8n_server=$n8nServerIdentity"
