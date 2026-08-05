[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Rollback
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

$imageTag = [Environment]::GetEnvironmentVariable("CAPTAIN_PORTAL_IMAGE_TAG")
if ([string]::IsNullOrWhiteSpace($imageTag)) {
    $imageTag = "local"
}
if ($imageTag -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw "portal image tag is invalid"
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

$portalTlsFiles = @(
    [Environment]::GetEnvironmentVariable("CAPTAIN_PORTAL_TLS_CERT_PATH"),
    [Environment]::GetEnvironmentVariable("CAPTAIN_PORTAL_TLS_KEY_PATH")
)
if ($portalTlsFiles | Where-Object {
    [string]::IsNullOrWhiteSpace($_) -or -not (Test-Path -LiteralPath $_ -PathType Leaf)
}) {
    throw "required local portal TLS file is missing"
}

$mode = "deploy"
if ($Rollback) {
    $acceptedImage = [Environment]::GetEnvironmentVariable("CAPTAIN_PORTAL_ACCEPTED_IMAGE")
    if ([string]::IsNullOrWhiteSpace($acceptedImage) -or
        $acceptedImage -notmatch '^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$') {
        throw "rollback requires an immutable accepted portal image digest"
    }
    $env:CAPTAIN_PORTAL_IMAGE_REFERENCE = $acceptedImage
    $mode = "rollback"
} else {
    $env:CAPTAIN_PORTAL_IMAGE_REFERENCE = "captain-integration-portal:$imageTag"
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
    mode = $mode
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

$portalUpArguments = if ($Rollback) {
    @(
        "compose", "--project-name", "captain-mini-pc-portal",
        "-f", $portalCompose, "up", "-d", "--no-build", "--pull", "always",
        "--no-deps", "portal"
    )
} else {
    @(
        "compose", "--project-name", "captain-mini-pc-portal",
        "-f", $portalCompose, "up", "-d", "--build", "--no-deps", "portal"
    )
}
$linkUpArguments = @(
    "compose", "--project-name", "captain-mini-pc-portal-link",
    "-f", $linkCompose, "--profile", "mini-pc", "up", "-d", "--no-deps",
    "mini-pc-wireguard", "mini-pc-portal-link"
)
Invoke-CheckedCompose -Arguments $linkUpArguments
Invoke-CheckedCompose -Arguments $portalUpArguments
