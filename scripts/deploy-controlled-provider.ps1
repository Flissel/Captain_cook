[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$SshHost = 'offload-vm',
    [ValidatePattern('^/home/[A-Za-z0-9._-]+/captain-controlled-provider$')]
    [string]$RemoteRoot = '/home/debian/captain-controlled-provider',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$deploymentRoot = Join-Path $root 'deploy/portal-provider'
$compose = Join-Path $deploymentRoot 'compose.portal-provider.yml'
$dockerfile = Join-Path $deploymentRoot 'Dockerfile'
$requirements = Join-Path $deploymentRoot 'requirements.txt'
$package = Join-Path $root 'portal_provider'
$required = @($compose, $dockerfile, $requirements, (Join-Path $package 'app.py'), (Join-Path $package 'server.py'))
if ($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }) {
    throw 'required controlled provider deployment file is missing'
}

$plan = [ordered]@{
    schema = 'captain.controlled-provider-deploy.v1'
    apply = [bool]$Apply
    ssh_host = $SshHost
    remote_root = $RemoteRoot
    services = @('controlled-provider')
}
$plan | ConvertTo-Json -Compress
if (-not $Apply) {
    Write-Output 'No changes applied. Re-run with -Apply to deploy the bounded provider service.'
    exit 0
}

& ssh $SshHost "set -eu; mkdir -p '$RemoteRoot/portal_provider' /home/debian/.captain-secrets; chmod 700 /home/debian/.captain-secrets" *> $null
if ($LASTEXITCODE -ne 0) { throw 'controlled provider remote directory preparation failed' }
& scp $compose $dockerfile $requirements "${SshHost}:$RemoteRoot/" *> $null
if ($LASTEXITCODE -ne 0) { throw 'controlled provider deployment transfer failed' }
& scp (Join-Path $package '__init__.py') (Join-Path $package 'app.py') (Join-Path $package 'server.py') "${SshHost}:$RemoteRoot/portal_provider/" *> $null
if ($LASTEXITCODE -ne 0) { throw 'controlled provider package transfer failed' }

$remoteApply = @'
set -eu
secret_file=/home/debian/.captain-secrets/controlled-provider.env
if [ ! -f "$secret_file" ]; then
  umask 077
  bearer=$(openssl rand -hex 32)
  oauth_secret=$(openssl rand -hex 32)
  signing_secret=$(openssl rand -hex 32)
  audit=$(openssl rand -hex 32)
  temporary=$(mktemp /home/debian/.captain-secrets/.controlled-provider.env.XXXXXX)
  trap 'rm -f "$temporary"' EXIT
  {
    printf 'CAPTAIN_PROVIDER_DATABASE_PATH=/data/provider.sqlite3\n'
    printf 'CAPTAIN_PROVIDER_ISSUER=https://192.168.178.65:9443\n'
    printf 'CAPTAIN_PROVIDER_AUDIENCE=captain-n8n-verification\n'
    printf 'CAPTAIN_PROVIDER_BEARER_TOKEN=%s\n' "$bearer"
    printf 'CAPTAIN_PROVIDER_OAUTH_CLIENT_ID=captain-n8n\n'
    printf 'CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET=%s\n' "$oauth_secret"
    printf 'CAPTAIN_PROVIDER_OAUTH_SIGNING_SECRET=%s\n' "$signing_secret"
    printf 'CAPTAIN_PROVIDER_AUDIT_TOKEN=%s\n' "$audit"
  } > "$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$secret_file"
  trap - EXIT
fi
chmod 600 "$secret_file"
cd '__REMOTE_ROOT__'
docker compose --project-name captain-controlled-provider -f compose.portal-provider.yml config --quiet
docker compose --project-name captain-controlled-provider -f compose.portal-provider.yml up -d --build --wait controlled-provider
curl -fsS --max-time 5 http://127.0.0.1:9080/healthz >/dev/null
'@
$remoteApply = $remoteApply.Replace('__REMOTE_ROOT__', $RemoteRoot)
& ssh $SshHost $remoteApply *> $null
if ($LASTEXITCODE -ne 0) { throw 'controlled provider validation or apply failed' }

[ordered]@{
    schema = $plan.schema
    applied = $true
    service = 'controlled-provider'
    secrets_emitted = $false
} | ConvertTo-Json -Compress

