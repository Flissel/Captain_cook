#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'),
    [switch]$NormalizeOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-SafeEnv([string]$Path, [string[]]$AllowedNames) {
    $values = [ordered]@{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            if ($line -match '^\s*#' -or [string]::IsNullOrWhiteSpace($line)) { continue }
            if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw 'Invalid .env line; expected NAME=value.' }
            $name = $Matches[1]
            if ($AllowedNames -notcontains $name) { continue }
            if ($values.Contains($name)) { throw "Duplicate .env key: $name" }
            $values[$name] = $Matches[2]
        }
    }
    return $values
}

function Set-SafeDefault($Values, [string]$Name, [string]$Value) {
    if (-not $Values.Contains($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) { $Values[$Name] = $Value }
}

function Save-SafeEnv($Values, [string]$Path) {
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { [void](New-Item -ItemType Directory -Path $parent) }
    $pending = [ordered]@{}; foreach ($entry in $Values.GetEnumerator()) { $pending[$entry.Key] = $entry.Value }
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=') {
            $name = $Matches[1]
            if ($pending.Contains($name)) { $lines.Add(('{0}={1}' -f $name,$pending[$name])); $pending.Remove($name); continue }
        }
        $lines.Add($line)
    }}
    foreach ($entry in $pending.GetEnumerator()) { $lines.Add(('{0}={1}' -f $entry.Key,$entry.Value)) }
    [IO.File]::WriteAllLines($Path, $lines, [Text.UTF8Encoding]::new($false))
}

function Test-Http([string]$Name, [string]$Uri, [hashtable]$Headers = @{}, [string]$Method = 'GET', [string]$Body = '') {
    $parameters = @{ Uri=$Uri; Method=$Method; Headers=$Headers; TimeoutSec=10; ErrorAction='Stop'; UseBasicParsing=$true }
    if ($Body) { $parameters.Body=$Body; $parameters.ContentType='application/json' }
    $response = Invoke-WebRequest @parameters
    if ([int]$response.StatusCode -ne 200) { throw "$Name returned HTTP $([int]$response.StatusCode)." }
    Write-Host "[ready] $Name"
}

function Assert-CaptainSandboxImage([string]$Reference) {
    if ($Reference -notmatch '^(?<name>(?:[a-z0-9.-]+(?::[0-9]+)?/)?captain-[a-z0-9._/-]+(?::[a-z0-9._-]+)?)@(?<digest>sha256:[0-9a-f]{64})$') {
        throw 'A Captain-owned digest-pinned capability sandbox image is required.'
    }

    $expectedDigest = $Matches['digest']
    $inspection = & docker image inspect $Reference 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'The pinned Captain capability sandbox image is unavailable locally; pulling is forbidden.'
    }
    $image = @($inspection | ConvertFrom-Json)[0]
    $repoDigests = @($image.RepoDigests)
    if ([string]$image.Id -ne $expectedDigest -and $repoDigests -notcontains $Reference) {
        throw 'The local Captain capability sandbox image does not match its immutable digest.'
    }
    if ([string]$image.Config.Labels.'org.opencontainers.image.title' -ne 'captain-capability-sandbox') {
        throw 'The pinned image is not the Captain capability sandbox.'
    }
    Write-Host '[ready] Captain capability sandbox image digest verified'
}

$allowedNames = @('MAILPIT_WEB_PORT','MAILPIT_URL','MAILPIT_SMTP_PORT','CAPTAIN_N8N_URL','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET','CAPTAIN_FACTORY_N8N_WORKFLOW_ID','CAPTAIN_GATEWAY_URL','MINIBOOK_BACKEND_URL','MINIBOOK_API_KEY','MINIBOOK_PROJECTION_API_KEY','CAPTAIN_CAPABILITY_SANDBOX_IMAGE','CAPTAIN_N8N_API_KEY','N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','N8N_MCP_TOKEN','TEST_MARIADB_DSN')
$config = Read-SafeEnv $EnvFile $allowedNames
# The running Captain Mailpit instance is intentionally published on 18025.
$config['MAILPIT_WEB_PORT'] = '18025'
$config['MAILPIT_URL'] = 'http://localhost:18025'
Set-SafeDefault $config 'MAILPIT_SMTP_PORT' '1025'
Set-SafeDefault $config 'CAPTAIN_N8N_URL' 'http://127.0.0.1:5679'
Set-SafeDefault $config 'CAPTAIN_N8N_MCP_BROKER_URL' 'http://127.0.0.1:5680'
Set-SafeDefault $config 'CAPTAIN_GATEWAY_URL' 'http://127.0.0.1:8090'
Set-SafeDefault $config 'MINIBOOK_BACKEND_URL' 'http://127.0.0.1:3456'
if (-not $config.Contains('CAPTAIN_N8N_API_KEY') -and $config.Contains('N8N_API_KEY')) { $config['CAPTAIN_N8N_API_KEY'] = $config['N8N_API_KEY'] }
if (-not $config.Contains('CAPTAIN_N8N_MCP_TOKEN') -and $config.Contains('N8N_MCP_TOKEN')) { $config['CAPTAIN_N8N_MCP_TOKEN'] = $config['N8N_MCP_TOKEN'] }
Save-SafeEnv $config $EnvFile
Write-Host '[ready] local .env normalized (values redacted)'
if ($NormalizeOnly) { exit 0 }

foreach ($required in @('CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET','CAPTAIN_FACTORY_N8N_WORKFLOW_ID','MINIBOOK_API_KEY','MINIBOOK_PROJECTION_API_KEY','CAPTAIN_CAPABILITY_SANDBOX_IMAGE','TEST_MARIADB_DSN')) {
    if (-not $config.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$config[$required])) { throw "Missing required local setting: $required" }
}
if ([string]$config['TEST_MARIADB_DSN'] -notmatch '/captain_test(?:\?|$)') { throw 'TEST_MARIADB_DSN must target the isolated captain_test database.' }
Assert-CaptainSandboxImage ([string]$config['CAPTAIN_CAPABILITY_SANDBOX_IMAGE'])

$captainN8nUrl = ([string]$config['CAPTAIN_N8N_URL']).TrimEnd('/')
$mailpitUrl = ([string]$config['MAILPIT_URL']).TrimEnd('/')
$minibookUrl = ([string]$config['MINIBOOK_BACKEND_URL']).TrimEnd('/')
$gatewayUrl = ([string]$config['CAPTAIN_GATEWAY_URL']).TrimEnd('/')
Test-Http 'Captain n8n REST' "$captainN8nUrl/api/v1/workflows?limit=1" @{ 'X-N8N-API-KEY'=[string]$config['CAPTAIN_N8N_API_KEY'] }
Test-Http 'Captain factory n8n workflow' "$captainN8nUrl/api/v1/workflows/$([Uri]::EscapeDataString([string]$config['CAPTAIN_FACTORY_N8N_WORKFLOW_ID']))" @{ 'X-N8N-API-KEY'=[string]$config['CAPTAIN_N8N_API_KEY'] }
$mcpBody = '{"jsonrpc":"2.0","id":"demo-preflight","method":"tools/list","params":{}}'
Test-Http 'Captain n8n MCP' "$captainN8nUrl/mcp-server/http" @{ Authorization="Bearer $([string]$config['CAPTAIN_N8N_MCP_TOKEN'])"; Accept='application/json, text/event-stream' } 'POST' $mcpBody
Test-Http 'Mailpit' "$mailpitUrl/api/v1/info"
Test-Http 'Minibook' "$minibookUrl/health"
Test-Http 'Minibook API identity' "$minibookUrl/api/v1/agents/me" @{ Authorization="Bearer $([string]$config['MINIBOOK_API_KEY'])" }
$creationCapabilities = Invoke-RestMethod "$minibookUrl/api/v1/creation-capabilities" -Headers @{ Authorization="Bearer $([string]$config['MINIBOOK_API_KEY'])" } -TimeoutSec 10
if ($creationCapabilities.schema -ne 'minibook.creation-capabilities.v1' -or $creationCapabilities.creation_jobs -ne $true) {
    throw 'Minibook creation jobs are not enabled.'
}
Write-Host '[ready] Minibook creation jobs'
Test-Http 'Gateway' "$gatewayUrl/healthz"

$broker = [Uri]([string]$config['CAPTAIN_N8N_MCP_BROKER_URL'])
$brokerTcp = [Net.Sockets.TcpClient]::new()
try { $brokerTcp.Connect($broker.Host, $(if ($broker.Port -gt 0) {$broker.Port} else {80})) } finally { $brokerTcp.Dispose() }
Write-Host '[ready] Captain n8n MCP broker'

$dsn = [Uri]([string]$config['TEST_MARIADB_DSN'])
$tcp = [Net.Sockets.TcpClient]::new()
try { $tcp.Connect($dsn.Host, $(if ($dsn.Port -gt 0) {$dsn.Port} else {3306})) } finally { $tcp.Dispose() }
Write-Host '[ready] MariaDB database=captain_test'
Write-Host '[ready] demo preflight complete'
