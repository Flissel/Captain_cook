[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CodexExecutable = 'codex',
    [string]$HermesExecutable = 'hermes',
    [string]$GitExecutable = 'git'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Codex {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & $CodexExecutable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Codex command failed: codex $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return [string]::Join([Environment]::NewLine, @($output))
}

function Invoke-Hermes {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & $HermesExecutable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes command failed: hermes $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return [string]::Join([Environment]::NewLine, @($output))
}

function Assert-HermesArrayConfigSupport {
    $temporaryHome = Join-Path ([System.IO.Path]::GetTempPath()) (
        'captain-n8n-hermes-probe-' + [guid]::NewGuid().ToString('N')
    )
    $previousHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
    [System.IO.Directory]::CreateDirectory($temporaryHome) | Out-Null
    try {
        [Environment]::SetEnvironmentVariable('HERMES_HOME', $temporaryHome, 'Process')
        $probeJson = ConvertTo-Json -InputObject @('C:\captain-n8n-a', 'C:\captain-n8n-b') -Compress
        Invoke-Hermes -Arguments @('config', 'set', 'skills.external_dirs', $probeJson) | Out-Null
        $roundTrip = (Invoke-Hermes -Arguments @(
            'config', 'get', 'skills.external_dirs', '--json'
        )).Trim()
        if (-not $roundTrip.StartsWith('[') -or @($roundTrip | ConvertFrom-Json).Count -ne 2) {
            throw 'Installed Hermes cannot safely persist multiple external skill directories.'
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable('HERMES_HOME', $previousHome, 'Process')
        if ([System.IO.Directory]::Exists($temporaryHome)) {
            [System.IO.Directory]::Delete($temporaryHome, $true)
        }
    }
}

function Convert-ToVersion {
    param([Parameter(Mandatory)][string]$Value)

    try {
        return [version]$Value
    }
    catch {
        throw "Invalid semantic version in official n8n skill configuration: $Value"
    }
}

$resolvedRepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$lockPath = Join-Path $resolvedRepositoryRoot 'agenten\agent_factory\n8n_official_skills.lock.json'
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Official n8n skill lock is missing: $lockPath"
}

try {
    $lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
}
catch {
    throw "Official n8n skill lock is invalid JSON: $($_.Exception.Message)"
}

if ($lock.schema -ne 'captain.n8n-official-skills-lock.v1' -or
    $lock.repository -ne 'https://github.com/n8n-io/skills' -or
    $lock.source -ne 'n8n-io/skills' -or
    $lock.commit -notmatch '^[0-9a-f]{40}$' -or
    $lock.marketplace -ne 'n8n-io' -or
    $lock.plugin -ne 'n8n-skills@n8n-io') {
    throw 'Official n8n skill lock failed source validation.'
}

if (-not (Get-Command $CodexExecutable -ErrorAction SilentlyContinue)) {
    throw "Codex executable was not found: $CodexExecutable"
}
if (-not (Get-Command $HermesExecutable -ErrorAction SilentlyContinue)) {
    throw "Hermes executable was not found: $HermesExecutable"
}
if (-not (Get-Command $GitExecutable -ErrorAction SilentlyContinue)) {
    throw "Git executable was not found: $GitExecutable"
}

$versionOutput = Invoke-Codex -Arguments @('--version')
if ($versionOutput -notmatch '(\d+\.\d+\.\d+)') {
    throw 'Codex did not report a semantic version.'
}
$installedCodexVersion = Convert-ToVersion -Value $Matches[1]
$minimumCodexVersion = Convert-ToVersion -Value ([string]$lock.minimum_codex_version)
if ($installedCodexVersion -lt $minimumCodexVersion) {
    throw "Codex $installedCodexVersion is below the official n8n minimum $minimumCodexVersion."
}

$changed = $false
$marketplaces = (Invoke-Codex -Arguments @('plugin', 'marketplace', 'list', '--json')) | ConvertFrom-Json
$marketplaceEntry = @($marketplaces.marketplaces | Where-Object { $_.name -eq [string]$lock.marketplace }) | Select-Object -First 1
if ($null -eq $marketplaceEntry) {
    Invoke-Codex -Arguments @(
        'plugin', 'marketplace', 'add', [string]$lock.source,
        '--ref', [string]$lock.commit, '--json'
    ) | Out-Null
    $changed = $true
    $marketplaces = (Invoke-Codex -Arguments @('plugin', 'marketplace', 'list', '--json')) | ConvertFrom-Json
    $marketplaceEntry = @($marketplaces.marketplaces | Where-Object { $_.name -eq [string]$lock.marketplace }) | Select-Object -First 1
}
if ($null -eq $marketplaceEntry -or
    $marketplaceEntry.marketplaceSource.sourceType -ne 'git' -or
    $marketplaceEntry.marketplaceSource.source -ne 'https://github.com/n8n-io/skills.git') {
    throw 'Official n8n marketplace source does not match the Captain lock.'
}
$marketplaceRoot = (Resolve-Path -LiteralPath ([string]$marketplaceEntry.root)).Path
$marketplaceCommit = [string]::Join('', @(& $GitExecutable -C $marketplaceRoot rev-parse HEAD 2>&1)).Trim()
if ($LASTEXITCODE -ne 0 -or $marketplaceCommit -ne [string]$lock.commit) {
    throw "Official n8n marketplace commit mismatch; expected $($lock.commit)."
}
$officialSkillsRoot = Join-Path $marketplaceRoot 'skills'
foreach ($skillName in @($lock.skills)) {
    if (-not (Test-Path -LiteralPath (Join-Path $officialSkillsRoot "$skillName\SKILL.md") -PathType Leaf)) {
        throw "Official n8n skill is missing at the locked marketplace commit: $skillName"
    }
}

$plugins = Invoke-Codex -Arguments @('plugin', 'list')
$pluginPattern = '(?mi)^' + [regex]::Escape([string]$lock.plugin) +
    '\s+installed,\s*enabled\s+' + [regex]::Escape([string]$lock.plugin_version) + '\s+'
$anyPluginPattern = '(?mi)^' + [regex]::Escape([string]$lock.plugin) + '\s+'
$notInstalledPluginPattern = '(?mi)^' + [regex]::Escape([string]$lock.plugin) + '\s+not installed(?:\s|$)'
if ($plugins -notmatch $pluginPattern) {
    if (($plugins -match $anyPluginPattern) -and ($plugins -notmatch $notInstalledPluginPattern)) {
        throw "Official n8n plugin is present but not enabled at locked version $($lock.plugin_version)."
    }
    Invoke-Codex -Arguments @('plugin', 'add', [string]$lock.plugin, '--json') | Out-Null
    $changed = $true
    $plugins = Invoke-Codex -Arguments @('plugin', 'list')
    if ($plugins -notmatch $pluginPattern) {
        throw 'Official n8n plugin was not enabled after installation.'
    }
}

$mcpOutput = & $CodexExecutable 'mcp' 'get' ([string]$lock.mcp_server_name) '--json' 2>&1
$mcpExitCode = $LASTEXITCODE
if ($mcpExitCode -ne 0) {
    Invoke-Codex -Arguments @(
        'mcp', 'add', [string]$lock.mcp_server_name,
        '--url', [string]$lock.mcp_url,
        '--bearer-token-env-var', [string]$lock.mcp_bearer_token_env_var
    ) | Out-Null
    $changed = $true
    $mcpOutput = Invoke-Codex -Arguments @(
        'mcp', 'get', [string]$lock.mcp_server_name, '--json'
    )
}
else {
    $mcpOutput = [string]::Join([Environment]::NewLine, @($mcpOutput))
}

try {
    $mcp = $mcpOutput | ConvertFrom-Json
}
catch {
    throw 'Codex returned invalid JSON for the n8n MCP server.'
}
if ($mcp.enabled -ne $true -or
    $mcp.transport.type -ne 'streamable_http' -or
    $mcp.transport.url -ne [string]$lock.mcp_url) {
    throw "n8n MCP URL mismatch; expected $($lock.mcp_url)."
}
if ($mcp.transport.bearer_token_env_var -ne [string]$lock.mcp_bearer_token_env_var) {
    throw "n8n MCP bearer-token environment mismatch; expected $($lock.mcp_bearer_token_env_var)."
}

try {
    $hermesExternalDirs = @((Invoke-Hermes -Arguments @(
        'config', 'get', 'skills.external_dirs', '--json'
    )) | ConvertFrom-Json)
}
catch {
    throw 'Hermes returned invalid skills.external_dirs JSON.'
}
$officialSkillsResolved = [System.IO.Path]::GetFullPath($officialSkillsRoot)
$hasOfficialSkills = @(
    $hermesExternalDirs | Where-Object {
        [System.IO.Path]::GetFullPath([string]$_) -eq $officialSkillsResolved
    }
).Count -gt 0
if (-not $hasOfficialSkills) {
    Assert-HermesArrayConfigSupport
    $mergedExternalDirs = @($hermesExternalDirs | ForEach-Object { [string]$_ }) + $officialSkillsResolved
    $externalDirsJson = ConvertTo-Json -InputObject @($mergedExternalDirs) -Compress
    Invoke-Hermes -Arguments @(
        'config', 'set', 'skills.external_dirs', $externalDirsJson
    ) | Out-Null
    $changed = $true
}
$verifiedHermesDirs = @((Invoke-Hermes -Arguments @(
    'config', 'get', 'skills.external_dirs', '--json'
)) | ConvertFrom-Json)
if (@(
    $verifiedHermesDirs | Where-Object {
        [System.IO.Path]::GetFullPath([string]$_) -eq $officialSkillsResolved
    }
).Count -ne 1) {
    throw 'Hermes did not retain the pinned official n8n skill directory exactly once.'
}

if ($changed) {
    Write-Output "Configured official n8n skills $($lock.plugin_version) at $($lock.commit) for Codex and Hermes with MCP server $($lock.mcp_server_name). Restart Codex before the next Factory build."
}
else {
    Write-Output "Official n8n skills are already configured for Codex and Hermes at $($lock.plugin_version) with the expected MCP server."
}
