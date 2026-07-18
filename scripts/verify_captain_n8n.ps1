[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$EnvFile = Join-Path $Root ".env.captain-n8n"
$ProjectLabel = "com.docker.compose.project=captain-n8n-builder"

function Get-EnvValues {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw "Missing .env.captain-n8n; run the Captain n8n init and bootstrap actions first."
    }

    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            throw "Invalid line in .env.captain-n8n; expected NAME=value."
        }
        if ($values.ContainsKey($Matches[1])) {
            throw "Duplicate environment key in .env.captain-n8n."
        }
        $values[$Matches[1]] = $Matches[2]
    }
    return $values
}

function Get-HttpErrorStatusCode {
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    if ($null -eq $ErrorRecord.Exception.Response) {
        return 0
    }
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    }
    catch {
        return 0
    }
}

function Invoke-StatusRequest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$EndpointIdentity,
        [hashtable]$Headers = @{}
    )

    try {
        $response = Invoke-WebRequest `
            -Uri $Uri `
            -Method GET `
            -Headers $Headers `
            -UseBasicParsing `
            -TimeoutSec 15 `
            -ErrorAction Stop
        $statusCode = [int]$response.StatusCode
    }
    catch {
        $statusCode = Get-HttpErrorStatusCode -ErrorRecord $_
        if ($statusCode -eq 0) {
            throw "$EndpointIdentity is unavailable or timed out."
        }
        throw "$EndpointIdentity returned HTTP $statusCode."
    }
    if ($statusCode -ne 200) {
        throw "$EndpointIdentity returned unexpected HTTP $statusCode."
    }
    return $statusCode
}

if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is required to verify the Captain n8n builder."
}

$values = Get-EnvValues
foreach ($required in @("CAPTAIN_N8N_PORT", "CAPTAIN_N8N_API_KEY")) {
    if (-not $values.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($values[$required])) {
        throw "Missing $required in .env.captain-n8n."
    }
}

$port = 0
if (-not [int]::TryParse($values["CAPTAIN_N8N_PORT"], [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    throw "CAPTAIN_N8N_PORT must be an integer between 1 and 65535."
}
$apiKey = [string]$values["CAPTAIN_N8N_API_KEY"]

$ids = @(& docker ps -aq --filter "label=$ProjectLabel" 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the Captain n8n project inventory."
}
$ids = @($ids | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($ids.Count -ne 2) {
    throw "Captain n8n inventory must contain exactly two project-scoped containers."
}

$services = @()
foreach ($id in $ids) {
    $service = & docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' $id 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not validate a Captain n8n project container."
    }
    $services += ([string]$service).Trim()
}
$actualServiceList = (@($services | Sort-Object) -join ",")
$expectedServiceList = (@(@("n8n", "postgres") | Sort-Object) -join ",")
if ($actualServiceList -ne $expectedServiceList) {
    throw "Captain n8n inventory contains an unexpected service."
}

$baseUrl = "http://127.0.0.1:$port"
$healthStatus = Invoke-StatusRequest -Uri "$baseUrl/healthz" -EndpointIdentity "healthz"
$workflowStatus = Invoke-StatusRequest `
    -Uri "$baseUrl/api/v1/workflows?limit=1" `
    -EndpointIdentity "workflows" `
    -Headers @{ "X-N8N-API-KEY" = $apiKey }

Write-Output "endpoint=project:captain-n8n-builder status=ok"
Write-Output "endpoint=healthz status=$healthStatus"
Write-Output "endpoint=workflows status=$workflowStatus"
