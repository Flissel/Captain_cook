#requires -Version 7.0
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$ContainerName,
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$ProjectId,
    [string]$CredentialEnvironment = '.env.n8n-credentials'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sourcePath = [System.IO.Path]::GetFullPath((Join-Path $root $CredentialEnvironment))
$expectedPath = [System.IO.Path]::GetFullPath((Join-Path $root '.env.n8n-credentials'))
if ($sourcePath -ne $expectedPath) {
    throw 'Refusing to read credentials outside the gitignored n8n credential environment file.'
}
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw 'The gitignored n8n credential environment file is missing.'
}

$values = [ordered]@{}
$lines = [System.Collections.Generic.List[string]]::new()
$lines.AddRange([string[]][System.IO.File]::ReadAllLines($sourcePath))
foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
        continue
    }
    $parts = $line.Split('=', 2)
    if ($parts.Count -ne 2 -or $values.Contains($parts[0])) {
        throw 'The n8n credential environment file contains an invalid or duplicate key.'
    }
    $values[$parts[0]] = $parts[1]
}

$required = @(
    'CAPTAIN_N8N_OAUTH2_CREDENTIAL_NAME',
    'CAPTAIN_N8N_OAUTH2_CLIENT_ID',
    'CAPTAIN_N8N_OAUTH2_CLIENT_SECRET',
    'CAPTAIN_N8N_OAUTH2_ACCESS_TOKEN_URL',
    'CAPTAIN_N8N_OAUTH2_GRANT_TYPE',
    'CAPTAIN_N8N_OAUTH2_SCOPE',
    'CAPTAIN_N8N_OAUTH2_AUTHENTICATION'
)
foreach ($name in $required) {
    if (-not $values.Contains($name) -or [string]::IsNullOrWhiteSpace($values[$name])) {
        throw "The n8n credential environment file is missing required key: $name"
    }
}
if ($values['CAPTAIN_N8N_OAUTH2_GRANT_TYPE'] -ne 'clientCredentials') {
    throw 'Only the controlled-provider client-credentials grant is supported.'
}

$credentialId = [string]$values['CAPTAIN_N8N_OAUTH2_CREDENTIAL_ID']
if ([string]::IsNullOrWhiteSpace($credentialId)) {
    $credentialId = [guid]::NewGuid().ToString('N').Substring(0, 24)
    $lines.Add("CAPTAIN_N8N_OAUTH2_CREDENTIAL_ID=$credentialId")
    $temporaryPath = "$sourcePath.incoming"
    try {
        [System.IO.File]::WriteAllLines(
            $temporaryPath,
            $lines,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryPath -Destination $sourcePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $sourcePath /inheritance:r /grant:r "${currentUser}:(R,W)" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not restrict permissions on the n8n credential environment file.'
    }
}
if ($credentialId -notmatch '^[A-Za-z0-9_-]{8,64}$') {
    throw 'The persisted n8n OAuth credential identity is invalid.'
}

if ([string]::IsNullOrWhiteSpace($ContainerName)) {
    $containers = @(
        docker ps `
            --filter 'label=com.docker.compose.project=captain-n8n-builder' `
            --filter 'label=com.docker.compose.service=n8n' `
            --format '{{.Names}}'
    )
    if ($LASTEXITCODE -ne 0 -or $containers.Count -ne 1) {
        throw 'Exactly one running Captain n8n container is required.'
    }
    $ContainerName = $containers[0]
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw 'A concrete n8n project identity is required.'
}

$item = @{
    id = $credentialId
    name = $values['CAPTAIN_N8N_OAUTH2_CREDENTIAL_NAME']
    type = 'oAuth2Api'
    data = @{
        grantType = $values['CAPTAIN_N8N_OAUTH2_GRANT_TYPE']
        authUrl = [string]$values['CAPTAIN_N8N_OAUTH2_AUTH_URL']
        accessTokenUrl = $values['CAPTAIN_N8N_OAUTH2_ACCESS_TOKEN_URL']
        clientId = $values['CAPTAIN_N8N_OAUTH2_CLIENT_ID']
        clientSecret=$values['CAPTAIN_N8N_OAUTH2_CLIENT_SECRET']
        scope = $values['CAPTAIN_N8N_OAUTH2_SCOPE']
        authQueryParameters = ''
        authentication = $values['CAPTAIN_N8N_OAUTH2_AUTHENTICATION']
        ignoreSSLIssues = $false
    }
}
$payload = $item | ConvertTo-Json -Depth 8 -Compress -AsArray
$output = $payload | docker exec -i $ContainerName n8n import:credentials --input=/dev/stdin --projectId=$ProjectId 2>&1
if ($LASTEXITCODE -ne 0) {
    throw 'The official n8n credential import failed; inspect n8n container logs locally.'
}

[ordered]@{
    schema = 'captain.n8n-credential-import.v1'
    status = 'imported'
    credential_id = $credentialId
    credential_type = 'oAuth2Api'
    project_id = $ProjectId
    secrets_emitted = $false
} | ConvertTo-Json -Compress
