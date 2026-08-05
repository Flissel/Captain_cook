[CmdletBinding()]
param(
    [ValidateRange(1, 30)]
    [int]$TimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"

function Get-RequiredEndpoint {
    param([Parameter(Mandatory)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "required portal preflight endpoint is missing"
    }
    $uri = [Uri]$value
    if (($uri.Scheme -ne "http" -and $uri.Scheme -ne "https") -or
        -not [string]::IsNullOrEmpty($uri.UserInfo)) {
        throw "portal preflight endpoint is unsafe"
    }
    return $uri
}

function Join-EndpointPath {
    param(
        [Parameter(Mandatory)][Uri]$Base,
        [Parameter(Mandatory)][string]$Path
    )
    return [Uri]::new(($Base.GetLeftPart([UriPartial]::Authority) + $Path))
}

function Test-Endpoint {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][Uri]$Uri,
        [Parameter(Mandatory)][int[]]$ReadyStatuses,
        [switch]$ReadVersion
    )

    $status = 0
    $version = $null
    try {
        $response = Invoke-WebRequest -Uri $Uri -Method Get -UseBasicParsing `
            -TimeoutSec $TimeoutSeconds -SkipHttpErrorCheck
        $status = [int]$response.StatusCode
        if ($ReadVersion -and $status -ge 200 -and $status -lt 300) {
            try {
                $payload = $response.Content | ConvertFrom-Json
                if ($payload.version -is [string] -and $payload.version.Length -le 64) {
                    $version = $payload.version
                }
            } catch {
                $version = $null
            }
        }
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $status = [int]$_.Exception.Response.StatusCode
        }
    }

    return [ordered]@{
        name = $Name
        host = $Uri.DnsSafeHost
        status = $status
        version = $version
        readiness = ($ReadyStatuses -contains $status)
    }
}

$portalBase = Get-RequiredEndpoint -Name "CAPTAIN_PORTAL_URL"
$supabaseBase = Get-RequiredEndpoint -Name "CAPTAIN_PORTAL_SUPABASE_URL"
$giteaBase = Get-RequiredEndpoint -Name "CAPTAIN_PORTAL_GITEA_URL"

$checks = @()
$checks += Test-Endpoint -Name "portal" `
    -Uri (Join-EndpointPath -Base $portalBase -Path "/healthz") `
    -ReadyStatuses @(200)
$checks += Test-Endpoint -Name "portal_link" `
    -Uri ([Uri]"http://127.0.0.1:8443/v1/portal/preflight") `
    -ReadyStatuses @(200, 401, 503)
$checks += Test-Endpoint -Name "supabase_auth" `
    -Uri (Join-EndpointPath -Base $supabaseBase -Path "/auth/v1/health") `
    -ReadyStatuses @(200)
$checks += Test-Endpoint -Name "gitea" `
    -Uri (Join-EndpointPath -Base $giteaBase -Path "/api/v1/version") `
    -ReadyStatuses @(200) -ReadVersion

[ordered]@{
    schema = "captain.portal-preflight.v1"
    readiness = -not ($checks.readiness -contains $false)
    checks = $checks
} | ConvertTo-Json -Depth 4 -Compress
