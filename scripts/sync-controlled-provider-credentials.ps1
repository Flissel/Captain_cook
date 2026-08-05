[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$SshHost = 'offload-vm',
    [ValidatePattern('^/home/[A-Za-z0-9._/-]+\.env$')]
    [string]$RemoteEnvFile = '/home/debian/.captain-secrets/controlled-provider.env',
    [ValidatePattern('^https://[^/]+(?::[0-9]+)?$')]
    [string]$ProviderOrigin = 'https://192.168.178.65:9443',
    [string]$Destination = '.env.n8n-credentials'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$destinationPath = [System.IO.Path]::GetFullPath((Join-Path $root $Destination))
$expectedPath = [System.IO.Path]::GetFullPath((Join-Path $root '.env.n8n-credentials'))
if ($destinationPath -ne $expectedPath) {
    throw 'Refusing to write outside the gitignored n8n credential environment file.'
}

$remoteCommand = 'set -eu; test -f ''{0}''; test "$(stat -c %a ''{0}'')" = 600; cat ''{0}''' -f $RemoteEnvFile
$remoteText = (& ssh $SshHost $remoteCommand 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteText)) {
    throw 'Could not read the protected controlled-provider credential source.'
}

$remote = @{}
foreach ($line in $remoteText -split "`r?`n") {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
        continue
    }
    if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        throw 'Remote controlled-provider environment contains an invalid line.'
    }
    $remote[$Matches[1]] = $Matches[2]
}

$requiredRemote = @(
    'CAPTAIN_PROVIDER_BEARER_TOKEN',
    'CAPTAIN_PROVIDER_OAUTH_CLIENT_ID',
    'CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET'
)
foreach ($name in $requiredRemote) {
    if (-not $remote.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($remote[$name])) {
        throw "Remote controlled-provider credential source is missing required key: $name"
    }
}

$updates = [ordered]@{
    CAPTAIN_N8N_BEARER_TOKEN = $remote['CAPTAIN_PROVIDER_BEARER_TOKEN']
    CAPTAIN_N8N_OAUTH2_CLIENT_ID = $remote['CAPTAIN_PROVIDER_OAUTH_CLIENT_ID']
    CAPTAIN_N8N_OAUTH2_CLIENT_SECRET = $remote['CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET']
    CAPTAIN_N8N_OAUTH2_AUTH_URL = ''
    CAPTAIN_N8N_OAUTH2_ACCESS_TOKEN_URL = "$ProviderOrigin/oauth/token"
    CAPTAIN_N8N_OAUTH2_GRANT_TYPE = 'clientCredentials'
    CAPTAIN_N8N_OAUTH2_SCOPE = 'probe:read'
    CAPTAIN_N8N_OAUTH2_AUTHENTICATION = 'header'
}

$lines = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
    $lines.AddRange([string[]][System.IO.File]::ReadAllLines($destinationPath))
}

foreach ($entry in $updates.GetEnumerator()) {
    $prefix = "$($entry.Key)="
    $matches = @()
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index].StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            $matches += $index
        }
    }
    if ($matches.Count -gt 1) {
        throw "Duplicate local credential key: $($entry.Key)"
    }
    $replacement = "$prefix$($entry.Value)"
    if ($matches.Count -eq 1) {
        $lines[$matches[0]] = $replacement
    } else {
        $lines.Add($replacement)
    }
}

$temporaryPath = "$destinationPath.incoming"
try {
    [System.IO.File]::WriteAllLines($temporaryPath, $lines, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $destinationPath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

if ($PSVersionTable.PSEdition -eq 'Desktop' -or [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $destinationPath /inheritance:r /grant:r "${currentUser}:(R,W)" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not restrict permissions on the local n8n credential environment file.'
    }
}

[ordered]@{
    schema = 'captain.controlled-provider-credential-sync.v1'
    synchronized = $true
    destination = '.env.n8n-credentials'
    credential_kinds = @('bearer', 'oauth2_client_credentials')
    secrets_emitted = $false
} | ConvertTo-Json -Compress
