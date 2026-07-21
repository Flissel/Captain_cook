[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$HermesExecutable = 'hermes',
    [string]$PythonExecutable = 'py',
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
$textAssetExtensions = @(
    '.bash', '.cjs', '.conf', '.css', '.csv', '.html', '.ini', '.js', '.json',
    '.jsonl', '.jsx', '.md', '.mjs', '.ps1', '.psd1', '.psm1', '.py', '.pyi',
    '.scss', '.sh', '.toml', '.ts', '.tsx', '.txt', '.xml', '.yaml', '.yml',
    '.zsh'
)

# Captain-owned release manifest. Each value hashes the sorted relative path and
# SHA-256 of every file in that skill directory. Known text assets normalize
# CRLF and standalone CR to LF before hashing; binary files remain byte-exact.
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

function Resolve-ExternalDirectoryPath {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$HermesHome
    )

    $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim())
    if ($expanded -eq '~' -or $expanded.StartsWith("~$([System.IO.Path]::DirectorySeparatorChar)")) {
        $profileRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
        $expanded = Join-Path $profileRoot $expanded.Substring(1).TrimStart('\', '/')
    }
    if (-not [System.IO.Path]::IsPathFullyQualified($expanded)) {
        $expanded = Join-Path $HermesHome $expanded
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Read-ExternalDirectoryEntries {
    param([Parameter(Mandatory)][string]$HermesHome)

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
        return @(
            $decoded | ForEach-Object {
                $value = [string]$_
                [pscustomobject]@{
                    Raw = $value
                    Resolved = Resolve-ExternalDirectoryPath -Value $value -HermesHome $HermesHome
                }
            }
        )
    }
    if ($raw.StartsWith('"') -and $decoded -is [string]) {
        if ($decoded.Trim().StartsWith('[')) {
            throw 'Hermes skills.external_dirs is a JSON array encoded as a string; refusing an unsafe merge.'
        }
        return @(
            [pscustomobject]@{
                Raw = $decoded
                Resolved = Resolve-ExternalDirectoryPath -Value $decoded -HermesHome $HermesHome
            }
        )
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
            $extension = [System.IO.Path]::GetExtension($_.Name).ToLowerInvariant()
            $isTextAsset = $extension -in $textAssetExtensions
            $fileBytes = [System.IO.File]::ReadAllBytes($_.FullName)
            if ($isTextAsset) {
                $canonicalBytes = [System.Collections.Generic.List[byte]]::new($fileBytes.Length)
                for ($index = 0; $index -lt $fileBytes.Length; $index++) {
                    if ($fileBytes[$index] -eq 13) {
                        if (($index + 1) -lt $fileBytes.Length -and $fileBytes[$index + 1] -eq 10) {
                            $index++
                        }
                        $canonicalBytes.Add(10)
                    }
                    else {
                        $canonicalBytes.Add($fileBytes[$index])
                    }
                }
                $fileBytes = $canonicalBytes.ToArray()
            }
            $fileHashBytes = [System.Security.Cryptography.SHA256]::HashData($fileBytes)
            $fileHash = [Convert]::ToHexString($fileHashBytes).ToLowerInvariant()
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
    param(
        [Parameter(Mandatory)][string[]]$Directories,
        [Parameter(Mandatory)][string]$HermesHome
    )

    Assert-JsonArrayConfigSupport
    $externalDirsJson = ConvertTo-Json -InputObject @($Directories) -Compress
    Invoke-Hermes -Arguments @('config', 'set', 'skills.external_dirs', $externalDirsJson) | Out-Null
    $roundTripRaw = (Invoke-Hermes -Arguments @('config', 'get', 'skills.external_dirs', '--json')).Trim()
    if (-not $roundTripRaw.StartsWith('[')) {
        throw 'Hermes did not persist skills.external_dirs as an array.'
    }
    $roundTrip = @(Read-ExternalDirectoryEntries -HermesHome $HermesHome)
    if ($roundTrip.Count -ne $Directories.Count) {
        throw 'Hermes did not preserve every skills.external_dirs entry.'
    }
    for ($index = 0; $index -lt $Directories.Count; $index++) {
        if (-not [string]::Equals($roundTrip[$index].Raw, $Directories[$index], [StringComparison]::Ordinal)) {
            throw 'Hermes changed the ordered skills.external_dirs merge.'
        }
    }
}

function Remove-HermesBundle {
    param([Parameter(Mandatory)][string]$Name)

    $output = & $HermesExecutable 'bundles' 'delete' $Name 2>&1
    $exitCode = $LASTEXITCODE
    $outputText = [string]::Join([Environment]::NewLine, @($output))
    if ($exitCode -eq 0) {
        return $true
    }
    if ($outputText -match '(?i)(bundle\s+not\s+found|no\s+bundle\s+at|does\s+not\s+exist|unknown\s+bundle)') {
        return $false
    }
    throw "Hermes command failed: hermes bundles delete $Name`n$outputText"
}

function Invoke-SafeYamlProjection {
    param(
        [Parameter(Mandatory)][ValidateSet('skill-name', 'bundle')][string]$Mode,
        [Parameter(Mandatory)][string]$YamlFile
    )

    if (-not (Get-Command $PythonExecutable -ErrorAction SilentlyContinue)) {
        throw "Python 3.11 executable was not found; cannot safely parse YAML metadata."
    }

    $pythonArguments = @()
    $pythonCommandName = [System.IO.Path]::GetFileNameWithoutExtension($PythonExecutable)
    if ([string]::Equals($pythonCommandName, 'py', [StringComparison]::OrdinalIgnoreCase)) {
        $pythonArguments += '-3.11'
    }
    $parserSource = @'
from pathlib import Path
import json
import sys

import yaml

result = {"ok": False}
try:
    mode = sys.argv[1]
    yaml_file = Path(sys.argv[2])
    if mode == "skill-name":
        lines = yaml_file.read_text(encoding="utf-8-sig").splitlines()
        metadata = {}
        if lines and lines[0].strip() == "---":
            closing_index = next(
                index for index, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
            metadata = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
            if not isinstance(metadata, dict):
                raise ValueError("frontmatter must be a mapping")
        name = metadata.get("name", yaml_file.parent.name)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("skill name must be a non-empty string")
        result = {"ok": True, "name": name.strip()}
    elif mode == "bundle":
        metadata = yaml.safe_load(yaml_file.read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError("bundle manifest must be a mapping")
        name = metadata.get("name")
        description = metadata.get("description")
        instruction = metadata.get("instruction")
        skills = metadata.get("skills")
        if not all(isinstance(value, str) for value in (name, description, instruction)):
            raise ValueError("bundle metadata must be strings")
        if not isinstance(skills, list) or not all(isinstance(skill, str) for skill in skills):
            raise ValueError("bundle skills must be a string list")
        result = {
            "ok": True,
            "name": name,
            "description": description,
            "instruction": instruction,
            "skills": skills,
        }
except Exception:
    pass
print(json.dumps(result, separators=(",", ":")))
'@

    $output = & $PythonExecutable @pythonArguments '-c' $parserSource $Mode $YamlFile 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Python 3.11 with PyYAML is required to safely parse YAML metadata: $YamlFile"
    }
    try {
        $parsed = ([string]::Join([Environment]::NewLine, @($output))) | ConvertFrom-Json
    }
    catch {
        throw "Could not safely parse YAML metadata: $YamlFile"
    }
    if (-not $parsed.ok) {
        throw "Could not safely parse YAML metadata: $YamlFile"
    }
    return $parsed
}

function Read-HermesSkillName {
    param([Parameter(Mandatory)][string]$SkillFile)

    $parsed = Invoke-SafeYamlProjection -Mode 'skill-name' -YamlFile $SkillFile
    if (-not ($parsed.name -is [string]) -or -not $parsed.name.Trim()) {
        throw "Could not safely parse skill frontmatter: $SkillFile"
    }
    return $parsed.name.Trim()
}

function Read-HermesBundleManifest {
    param([Parameter(Mandatory)][string]$BundleFile)

    return Invoke-SafeYamlProjection -Mode 'bundle' -YamlFile $BundleFile
}

function Get-HermesSkillCandidates {
    param([Parameter(Mandatory)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @()
    }

    $excludedDirectories = @(
        '.git', '.github', '.hub', '.archive', '.venv', 'venv', 'node_modules',
        'site-packages', '__pycache__', '.tox', '.nox', '.pytest_cache',
        '.mypy_cache', '.ruff_cache'
    )
    $supportDirectories = @('references', 'templates', 'assets', 'scripts')
    $candidates = @()
    foreach ($skillFile in @(Get-ChildItem -LiteralPath $Root -File -Recurse -Filter 'SKILL.md' | Sort-Object FullName)) {
        $relativePath = [System.IO.Path]::GetRelativePath($Root, $skillFile.FullName)
        $parts = @($relativePath -split '[\\/]')
        $skip = $false
        for ($index = 0; $index -lt ($parts.Count - 1); $index++) {
            if ($excludedDirectories -contains $parts[$index]) {
                $skip = $true
                break
            }
            if ($supportDirectories -contains $parts[$index]) {
                $ancestor = $Root
                for ($ancestorIndex = 0; $ancestorIndex -lt $index; $ancestorIndex++) {
                    $ancestor = Join-Path $ancestor $parts[$ancestorIndex]
                }
                if (Test-Path -LiteralPath (Join-Path $ancestor 'SKILL.md') -PathType Leaf) {
                    $skip = $true
                    break
                }
            }
        }
        if ($skip) {
            continue
        }

        $skillName = Read-HermesSkillName -SkillFile $skillFile.FullName
        $candidates += [pscustomobject]@{
            Name = $skillName
            Path = $skillFile.Directory.FullName
        }
    }
    return $candidates
}

$resolvedRepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$skillRoot = (Resolve-Path -LiteralPath (Join-Path $resolvedRepositoryRoot 'agenten\agent_factory\skills')).Path
$bundleManifestPath = Join-Path $skillRoot "$bundleName\bundle.yaml"

if (-not (Get-Command $HermesExecutable -ErrorAction SilentlyContinue)) {
    throw "Hermes executable was not found: $HermesExecutable"
}

$configuredHermesHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'Process')
if (-not $configuredHermesHome) {
    $configuredHermesHome = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)) '.hermes'
}
$configuredHermesHome = [System.IO.Path]::GetFullPath($configuredHermesHome)

$externalDirectoryEntries = @(Read-ExternalDirectoryEntries -HermesHome $configuredHermesHome)
$otherDirectoryEntries = @(
    $externalDirectoryEntries | Where-Object {
        -not [string]::Equals($_.Resolved, $skillRoot, [StringComparison]::OrdinalIgnoreCase)
    }
)
$otherDirectories = @($otherDirectoryEntries | ForEach-Object { $_.Raw })
$targetCount = @(
    $externalDirectoryEntries | Where-Object {
        [string]::Equals($_.Resolved, $skillRoot, [StringComparison]::OrdinalIgnoreCase)
    }
).Count

if ($Remove) {
    if ($targetCount -gt 0) {
        if ($otherDirectories.Count -eq 0) {
            Invoke-Hermes -Arguments @('config', 'unset', 'skills.external_dirs') | Out-Null
        }
        else {
            Set-ExternalDirectories -Directories $otherDirectories -HermesHome $configuredHermesHome
        }
    }
    $bundleRemoved = Remove-HermesBundle -Name $bundleName
    if ($targetCount -eq 0 -and -not $bundleRemoved) {
        Write-Output "Already removed $skillRoot and /$bundleName; unrelated external directories were preserved."
    }
    else {
        Write-Output "Removed $skillRoot and /$bundleName; unrelated external directories were preserved."
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $bundleManifestPath -PathType Leaf)) {
    throw "Repository bundle manifest is missing: $bundleManifestPath"
}
$bundleManifest = Read-HermesBundleManifest -BundleFile $bundleManifestPath
$manifestSkills = @($bundleManifest.skills | ForEach-Object { [string]$_ })
if ([string]::Join('|', $manifestSkills) -ne [string]::Join('|', $skillNames)) {
    throw 'Repository bundle manifest does not contain the exact released six-skill order.'
}
foreach ($requiredMetadata in @(
    @{ Name = 'name'; Expected = $bundleName },
    @{ Name = 'description'; Expected = $bundleDescription },
    @{ Name = 'instruction'; Expected = $bundleInstruction }
)) {
    if (-not [string]::Equals(
        [string]$bundleManifest.($requiredMetadata.Name),
        $requiredMetadata.Expected,
        [StringComparison]::Ordinal
    )) {
        throw "Repository bundle manifest has invalid $($requiredMetadata.Name) metadata."
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

$candidateShadowRoots = @(
    [pscustomobject]@{
        Source = 'local'
        Path = Join-Path $configuredHermesHome 'skills'
    }
)
$candidateShadowRoots += @(
    $otherDirectoryEntries | ForEach-Object {
        [pscustomobject]@{
            Source = 'external'
            Path = $_.Resolved
        }
    }
)
foreach ($shadowRoot in $candidateShadowRoots) {
    foreach ($candidate in @(Get-HermesSkillCandidates -Root $shadowRoot.Path)) {
        if ($skillNames -contains $candidate.Name) {
            throw "Released skill $($candidate.Name) is shadowed by a $($shadowRoot.Source) skill at $($candidate.Path)."
        }
    }
}

$configurationChanged = $targetCount -ne 1
if ($configurationChanged) {
    Set-ExternalDirectories -Directories @($otherDirectories + $skillRoot) -HermesHome $configuredHermesHome
}

$previousColumns = $env:COLUMNS
try {
    # Rich truncates long skill names to the current terminal width. Force a
    # stable machine-readable width so verification is independent of the host.
    $env:COLUMNS = '240'
    $skillsOutput = Invoke-Hermes -Arguments @('skills', 'list', '--enabled-only')
}
finally {
    if ($null -eq $previousColumns) {
        Remove-Item Env:COLUMNS -ErrorAction SilentlyContinue
    }
    else {
        $env:COLUMNS = $previousColumns
    }
}
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
