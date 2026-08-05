[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$portalCompose = Join-Path $repositoryRoot "deploy/portal/compose.portal.yml"
$linkCompose = Join-Path $repositoryRoot "deploy/portal-link/compose.portal-link.yml"
$linkRoot = Join-Path $repositoryRoot "deploy/portal-link"

$requiredPublicEnvironment = @(
    "CAPTAIN_PORTAL_URL",
    "CAPTAIN_PORTAL_SUPABASE_URL",
    "CAPTAIN_PORTAL_SUPABASE_ANON_KEY",
    "CAPTAIN_PORTAL_GITEA_URL"
)
$missingEnvironment = @(
    $requiredPublicEnvironment | Where-Object {
        [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_))
    }
)
if ($missingEnvironment.Count -gt 0) {
    throw "required portal environment is missing"
}

$requiredSecretFiles = @(
    ".secrets/mini-pc/wireguard/mini-pc.conf",
    ".secrets/mini-pc/captain-server-ca.crt",
    ".secrets/mini-pc/mini-pc-client.crt",
    ".secrets/mini-pc/mini-pc-client.key"
)
$missingSecretFiles = @(
    $requiredSecretFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $linkRoot $_) -PathType Leaf)
    }
)
if ($missingSecretFiles.Count -gt 0) {
    throw "required local portal-link secret file is missing"
}

function Invoke-CheckedCompose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "portal compose validation or apply failed"
    }
}

$portalConfigArguments = @(
    "compose", "--project-name", "captain-mini-pc-portal",
    "-f", $portalCompose, "config"
)
$linkConfigArguments = @(
    "compose", "--project-name", "captain-mini-pc-portal-link",
    "-f", $linkCompose, "--profile", "mini-pc", "config"
)
Invoke-CheckedCompose -Arguments $portalConfigArguments
Invoke-CheckedCompose -Arguments $linkConfigArguments

$servicePlan = [ordered]@{
    apply = [bool]$Apply
    portal_project = "captain-mini-pc-portal"
    portal_services = @("portal")
    link_project = "captain-mini-pc-portal-link"
    link_services = @("mini-pc-wireguard", "mini-pc-portal-link")
}
$servicePlan | ConvertTo-Json -Depth 3

if (-not $Apply) {
    Write-Output "No changes applied. Re-run with -Apply to deploy the bounded service set."
    exit 0
}

$portalUpArguments = @(
    "compose", "--project-name", "captain-mini-pc-portal",
    "-f", $portalCompose, "up", "-d", "--build", "--no-deps", "portal"
)
$linkUpArguments = @(
    "compose", "--project-name", "captain-mini-pc-portal-link",
    "-f", $linkCompose, "--profile", "mini-pc", "up", "-d", "--no-deps",
    "mini-pc-wireguard", "mini-pc-portal-link"
)
Invoke-CheckedCompose -Arguments $linkUpArguments
Invoke-CheckedCompose -Arguments $portalUpArguments
