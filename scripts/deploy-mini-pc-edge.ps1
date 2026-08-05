[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$SshHost = 'offload-vm',
    [ValidatePattern('^/home/[A-Za-z0-9._-]+/captain-mini-pc-edge$')]
    [string]$RemoteRoot = '/home/debian/captain-mini-pc-edge',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $root 'deploy/mini-pc-edge'
$compose = Join-Path $sourceRoot 'compose.mini-pc-edge.yml'
$config = Join-Path $sourceRoot 'nginx.conf'
$certificate = Join-Path $sourceRoot '.secrets/mini-pc-edge.crt'
$certificateKey = Join-Path $sourceRoot '.secrets/mini-pc-edge.key'
$caCertificate = Join-Path $sourceRoot '.secrets/mini-pc-edge-ca.crt'
$required = @($compose, $config, $certificate, $certificateKey, $caCertificate)
if ($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }) {
    throw 'required mini PC edge deployment file is missing'
}

$plan = [ordered]@{
    schema = 'captain.mini-pc-edge-deploy.v1'
    apply = [bool]$Apply
    ssh_host = $SshHost
    remote_root = $RemoteRoot
    services = @('mini-pc-edge')
}
$plan | ConvertTo-Json -Compress
if (-not $Apply) {
    Write-Output 'No changes applied. Re-run with -Apply to deploy the bounded edge service.'
    exit 0
}

& ssh $SshHost "set -eu; mkdir -p '$RemoteRoot/.secrets'; chmod 700 '$RemoteRoot/.secrets'" *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'mini PC edge remote directory preparation failed'
}
& scp $compose $config "${SshHost}:$RemoteRoot/" *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'mini PC edge public configuration transfer failed'
}
foreach ($transfer in @(
    @($certificate, 'mini-pc-edge.crt.incoming'),
    @($certificateKey, 'mini-pc-edge.key.incoming'),
    @($caCertificate, 'mini-pc-edge-ca.crt.incoming')
)) {
    & scp $transfer[0] "${SshHost}:$RemoteRoot/.secrets/$($transfer[1])" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'mini PC edge TLS transfer failed'
    }
}

$remoteApply = @"
set -eu
sudo -n install -o 101 -g 101 -m 400 '$RemoteRoot/.secrets/mini-pc-edge.crt.incoming' '$RemoteRoot/.secrets/mini-pc-edge.crt'
sudo -n install -o 101 -g 101 -m 400 '$RemoteRoot/.secrets/mini-pc-edge.key.incoming' '$RemoteRoot/.secrets/mini-pc-edge.key'
install -m 400 '$RemoteRoot/.secrets/mini-pc-edge-ca.crt.incoming' '$RemoteRoot/.secrets/mini-pc-edge-ca.crt'
rm -f '$RemoteRoot/.secrets/mini-pc-edge.crt.incoming' '$RemoteRoot/.secrets/mini-pc-edge.key.incoming' '$RemoteRoot/.secrets/mini-pc-edge-ca.crt.incoming'
cd '$RemoteRoot'
docker compose --project-name captain-mini-pc-edge -f compose.mini-pc-edge.yml config --quiet
docker compose --project-name captain-mini-pc-edge -f compose.mini-pc-edge.yml up -d --no-deps --force-recreate mini-pc-edge
"@
& ssh $SshHost $remoteApply *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'mini PC edge validation or apply failed'
}

[ordered]@{
    schema = $plan.schema
    applied = $true
    service = 'mini-pc-edge'
    secrets_emitted = $false
} | ConvertTo-Json -Compress
