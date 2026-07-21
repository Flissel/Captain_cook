[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$HermesExecutable = 'hermes',
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$bundleName = 'captain-agent-factory-loop'
$bundleDescription = 'Captain-controlled six-skill AutoGen factory workflow'
$bundleInstruction = 'Use only the step released by the current Captain invocation.'
$skillNames = @(
    'captain-factory-discover'
    'captain-factory-brief-codex'
    'captain-factory-execute-team'
    'captain-factory-evaluate-team'
    'captain-factory-improve-team'
    'captain-factory-report-captain'
)

# Captain-owned release manifest. Each value hashes the sorted relative path and
# SHA-256 of every file in that skill directory.
$releasedSkillDigests = @{
    'captain-factory-discover' = '669c5b3208ab0779194fd79a70b3a8258eb6869767338fcf629b42cdcaddf19d'
    'captain-factory-brief-codex' = 'ab15e81bf383fe64ab9b1a7c018f5577025fcbed3eaec47ffdb1d1692808648b'
    'captain-factory-execute-team' = '5e885c4ab70985d7b4f41a1129b4e3d62e815e201da58e0d695b7caf35305897'
    'captain-factory-evaluate-team' = '468f49b870d19554812216eefd82542b51fd09e3563cfde3dc9d0332704157c7'
    'captain-factory-improve-team' = '4c1bd1a3981832d9ddd6051fd35a3885621f898c305cf99eb8f11b71ced0d35f'
    'captain-factory-report-captain' = '077dd7671601707aeb07aca32c1f84ed6d2ef34c90129e950c96a92c2d5d3827'
}

function Invoke-Hermes {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & $HermesExecutable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Hermes command failed: hermes $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return [string]::Join([Environment]::NewLine, @($output))
}

function ConvertTo-AbsolutePath {
    param([Parameter(Mandatory)][string]$Value)

    $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim())
    if ($expanded -eq '~' -or $expanded.StartsWith("~$([System.IO.Path]::DirectorySeparatorChar)")) {
        $profileRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
        $expanded = Join-Path $profileRoot $expanded.Substring(1).TrimStart('\', '/')
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Read-ExternalDirectories {
    $raw = (Invoke-Hermes -Arguments @('config', 'get', 'skills.external_dirs', '--json')).Trim()
    if (-not $raw) {
        throw 'Hermes returned an empty skills.external_dirs value.'
    }
    try {
        $decoded = $raw | ConvertFrom-Json
    }
    catch {
        throw "Hermes returned invalid JSON for skills.external_dirs: $($_.Exception.Message)"
    }

    if ($raw.StartsWith('[')) {
        return @($decoded | ForEach-Object { ConvertTo-AbsolutePath -Value ([string]$_) })
    }
    if ($raw.StartsWith('"') -and $decoded -is [string]) {
        if ($decoded.Trim().StartsWith('[')) {
            throw 'Hermes skills.external_dirs is a JSON array encoded as a string; refusing an unsafe merge.'
        }
        return @((ConvertTo-AbsolutePath -Value $decoded))
    }
    if ($null -eq $decoded) {
        return @()
    }
    throw 'Hermes skills.external_dirs must be a path or an array of paths.'
}

function Get-DirectoryManifestDigest {
    param([Parameter(Mandatory)][string]$Directory)

    $root = (Resolve-Path -LiteralPath $Directory).Path
    $entries = Get-ChildItem -LiteralPath $root -File -Recurse |
        ForEach-Object {
            $relative = [System.IO.Path]::GetRelativePath($root, $_.FullName).Replace('\', '/')
            $fileHash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$relative=$fileHash"
        } |
        Sort-Object
    $payload = [string]::Join("`n", @($entries))
    $digestBytes = [System.Security.Cryptography.SHA256]::HashData(
        [System.Text.Encoding]::UTF8.GetBytes($payload)
    )
    return [Convert]::ToHexString($digestBytes).ToLowerInvariant()
}

function Assert-JsonArrayConfigSupport {
    $temporaryRoot = [System.IO.Path]::GetFullPath(
        (Join-Path ([System.IO.Path]::GetTempPath()) ("captain-hermes-config-probe-" + [guid]::NewGuid().ToString('N')))
    )
    $systemTemporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $temporaryRoot.StartsWith($systemTemporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to create a Hermes config probe outside the system temporary directory.'
    }

    $previousHermesHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
    [System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    try {
        [Environment]::SetEnvironmentVariable('HERMES_HOME', $temporaryRoot, 'Process')
        $probeJson = ConvertTo-Json -InputObject @('C:\captain-probe-a', 'C:\captain-probe-b') -Compress
        Invoke-Hermes -Arguments @('config', 'set', 'skills.external_dirs', $probeJson) | Out-Null
        $roundTrip = (Invoke-Hermes -Arguments @('config', 'get', 'skills.external_dirs', '--json')).Trim()
        if (-not $roundTrip.StartsWith('[')) {
            throw 'Installed Hermes CLI cannot round-trip skills.external_dirs as a JSON array; no user config was changed.'
        }
        $values = @($roundTrip | ConvertFrom-Json)
        if ($values.Count -ne 2) {
            throw 'Installed Hermes CLI changed the skills.external_dirs array during its config round-trip.'
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable('HERMES_HOME', $previousHermesHome, 'Process')
        if ([System.IO.Directory]::Exists($temporaryRoot)) {
            [System.IO.Directory]::Delete($temporaryRoot, $true)
        }
    }
}

function Set-ExternalDirectories {
    param([Parameter(Mandatory)][string[]]$Directories)

    Assert-JsonArrayConfigSupport
    $externalDirsJson = ConvertTo-Json -InputObject @($Directories) -Compress
    Invoke-Hermes -Arguments @('config', 'set', 'skills.external_dirs', $externalDirsJson) | Out-Null
    $roundTripRaw = (Invoke-Hermes -Arguments @('config', 'get', 'skills.external_dirs', '--json')).Trim()
    if (-not $roundTripRaw.StartsWith('[')) {
        throw 'Hermes did not persist skills.external_dirs as an array.'
    }
    $roundTrip = @(Read-ExternalDirectories)
    if ($roundTrip.Count -ne $Directories.Count) {
        throw 'Hermes did not preserve every skills.external_dirs entry.'
    }
    for ($index = 0; $index -lt $Directories.Count; $index++) {
        if (-not [string]::Equals($roundTrip[$index], $Directories[$index], [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Hermes changed the ordered skills.external_dirs merge.'
        }
    }
}

$resolvedRepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$skillRoot = (Resolve-Path -LiteralPath (Join-Path $resolvedRepositoryRoot 'agenten\agent_factory\skills')).Path
$bundleManifestPath = Join-Path $skillRoot "$bundleName\bundle.yaml"

if (-not (Get-Command $HermesExecutable -ErrorAction SilentlyContinue)) {
    throw "Hermes executable was not found: $HermesExecutable"
}

$externalDirectories = @(Read-ExternalDirectories)
$otherDirectories = @(
    $externalDirectories | Where-Object {
        -not [string]::Equals($_, $skillRoot, [StringComparison]::OrdinalIgnoreCase)
    }
)

if ($Remove) {
    if ($otherDirectories.Count -eq 0) {
        Invoke-Hermes -Arguments @('config', 'unset', 'skills.external_dirs') | Out-Null
    }
    else {
        Set-ExternalDirectories -Directories $otherDirectories
    }
    Invoke-Hermes -Arguments @('bundles', 'delete', $bundleName) | Out-Null
    Write-Output "Removed $skillRoot and /$bundleName; unrelated external directories were preserved."
    exit 0
}

if (-not (Test-Path -LiteralPath $bundleManifestPath -PathType Leaf)) {
    throw "Repository bundle manifest is missing: $bundleManifestPath"
}
$bundleManifest = Get-Content -Raw -LiteralPath $bundleManifestPath
$manifestSkills = @(
    [regex]::Matches($bundleManifest, '(?m)^\s*-\s+(captain-factory-[a-z-]+)\s*$') |
        ForEach-Object { $_.Groups[1].Value }
)
if ([string]::Join('|', $manifestSkills) -ne [string]::Join('|', $skillNames)) {
    throw 'Repository bundle manifest does not contain the exact released six-skill order.'
}
foreach ($requiredText in @($bundleName, $bundleDescription, $bundleInstruction)) {
    if (-not $bundleManifest.Contains($requiredText)) {
        throw "Repository bundle manifest is missing required metadata: $requiredText"
    }
}

foreach ($skillName in $skillNames) {
    $skillDirectory = Join-Path $skillRoot $skillName
    $skillFile = Join-Path $skillDirectory 'SKILL.md'
    if (-not (Test-Path -LiteralPath $skillFile -PathType Leaf)) {
        throw "Released skill is missing: $skillName"
    }
    $actualDigest = Get-DirectoryManifestDigest -Directory $skillDirectory
    if ($actualDigest -ne $releasedSkillDigests[$skillName]) {
        throw "Released skill digest mismatch for $skillName."
    }
}

$candidateShadowRoots = @($otherDirectories)
$configuredHermesHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
if (-not $configuredHermesHome) {
    $configuredHermesHome = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)) '.hermes'
}
$candidateShadowRoots += (Join-Path ([System.IO.Path]::GetFullPath($configuredHermesHome)) 'skills')
foreach ($shadowRoot in $candidateShadowRoots) {
    foreach ($skillName in $skillNames) {
        if (Test-Path -LiteralPath (Join-Path $shadowRoot "$skillName\SKILL.md") -PathType Leaf) {
            throw "Released skill $skillName is shadowed by another path: $shadowRoot"
        }
    }
}

$targetCount = @(
    $externalDirectories | Where-Object {
        [string]::Equals($_, $skillRoot, [StringComparison]::OrdinalIgnoreCase)
    }
).Count
$configurationChanged = $targetCount -ne 1
if ($configurationChanged) {
    Set-ExternalDirectories -Directories @($otherDirectories + $skillRoot)
}

$skillsOutput = Invoke-Hermes -Arguments @('skills', 'list', '--enabled-only')
foreach ($skillName in $skillNames) {
    $matchingLines = @(
        $skillsOutput -split '\r?\n' | Where-Object { $_ -match [regex]::Escape($skillName) }
    )
    if ($matchingLines.Count -ne 1) {
        throw "Released skill is missing or disabled after Hermes reload: $skillName"
    }
    if ($matchingLines[0] -notmatch '(?i)enabled' -or $matchingLines[0] -notmatch '(?i)(external|local)') {
        throw "Released skill is not enabled from the external/local source: $skillName"
    }
}

$bundleArguments = @('bundles', 'create', $bundleName)
foreach ($skillName in $skillNames) {
    $bundleArguments += @('--skill', $skillName)
}
$bundleArguments += @(
    '--description', $bundleDescription,
    '--instruction', $bundleInstruction,
    '--force'
)
Invoke-Hermes -Arguments $bundleArguments | Out-Null

$bundleOutput = Invoke-Hermes -Arguments @('bundles', 'show', $bundleName)
if ($bundleOutput -notmatch [regex]::Escape("/$bundleName")) {
    throw "Hermes bundle /$bundleName was not created."
}
foreach ($skillName in $skillNames) {
    if ([regex]::Matches($bundleOutput, [regex]::Escape($skillName)).Count -ne 1) {
        throw "Hermes bundle must contain $skillName exactly once."
    }
}

if ($configurationChanged) {
    Write-Output "Configured $skillRoot and verified /$bundleName."
}
else {
    Write-Output "Already configured $skillRoot; verified /$bundleName."
}
