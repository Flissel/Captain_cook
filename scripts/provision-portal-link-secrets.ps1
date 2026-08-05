[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9.-]+$')]
    [string]$MiniPcEndpoint,
    [switch]$Apply,
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$linkRoot = Join-Path $root 'deploy/portal-link'
$secretRoot = Join-Path $linkRoot '.secrets'
$wireGuardImage = 'lscr.io/linuxserver/wireguard@sha256:ac43e1226878d2611315172d6ea357a95cb326ee73124b91108118efc8666889'
$expected = @(
    'captain/wireguard/captain.conf',
    'captain/captain-server.crt',
    'captain/captain-server.key',
    'captain/mini-pc-client-ca.crt',
    'mini-pc/wireguard/mini-pc.conf',
    'mini-pc/captain-server-ca.crt',
    'mini-pc/mini-pc-client.crt',
    'mini-pc/mini-pc-client.key'
)

$plan = [ordered]@{
    schema = 'captain.portal-link-secret-provision.v1'
    apply = [bool]$Apply
    rotate = [bool]$Rotate
    mini_pc_endpoint = $MiniPcEndpoint
    secret_file_count = $expected.Count
    wireguard_image = $wireGuardImage
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Compress
    Write-Output 'No secrets created. Re-run with -Apply after reviewing the endpoint.'
    exit 0
}

$existing = @(
    $expected | Where-Object { Test-Path -LiteralPath (Join-Path $secretRoot $_) }
)
if ($existing.Count -gt 0 -and -not $Rotate) {
    throw 'portal-link secrets already exist; use -Rotate for an explicit replacement'
}

$opensslCandidates = @(
    (Get-Command openssl -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    'C:\Program Files\Git\usr\bin\openssl.exe',
    'C:\Program Files\Git\mingw64\bin\openssl.exe'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$openssl = $opensslCandidates | Select-Object -First 1
if (-not $openssl) {
    throw 'a local OpenSSL executable is required'
}
& docker version --format '{{.Server.Version}}' *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker is required for pinned WireGuard key generation'
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('captain-portal-link-' + [Guid]::NewGuid())
$resolvedTempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
if (-not $resolvedTemp.StartsWith($resolvedTempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'temporary portal-link path escaped the system temp directory'
}
New-Item -ItemType Directory -Path $tempRoot | Out-Null

function Invoke-OpenSsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $openssl @Arguments *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'OpenSSL portal-link certificate generation failed'
    }
}

try {
    & docker pull $wireGuardImage *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'the pinned WireGuard image could not be resolved'
    }
    $captainPrivate = (& docker run --rm --entrypoint /usr/bin/wg $wireGuardImage genkey).Trim()
    $captainPublic = ($captainPrivate | & docker run --rm -i --entrypoint /usr/bin/wg $wireGuardImage pubkey).Trim()
    $miniPrivate = (& docker run --rm --entrypoint /usr/bin/wg $wireGuardImage genkey).Trim()
    $miniPublic = ($miniPrivate | & docker run --rm -i --entrypoint /usr/bin/wg $wireGuardImage pubkey).Trim()
    foreach ($value in @($captainPrivate, $captainPublic, $miniPrivate, $miniPublic)) {
        if ($value -notmatch '^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$') {
            throw 'WireGuard key generation returned an invalid key'
        }
    }

    $serverExt = Join-Path $tempRoot 'server-ext.cnf'
    $clientExt = Join-Path $tempRoot 'client-ext.cnf'
    Set-Content -LiteralPath $serverExt -Encoding ascii -Value @(
        'basicConstraints=CA:FALSE',
        'keyUsage=digitalSignature,keyEncipherment',
        'extendedKeyUsage=serverAuth',
        'subjectAltName=DNS:captain-portal-link.internal'
    )
    Set-Content -LiteralPath $clientExt -Encoding ascii -Value @(
        'basicConstraints=CA:FALSE',
        'keyUsage=digitalSignature,keyEncipherment',
        'extendedKeyUsage=clientAuth'
    )
    $serverCaKey = Join-Path $tempRoot 'server-ca.key'
    $serverCa = Join-Path $tempRoot 'server-ca.crt'
    $serverKey = Join-Path $tempRoot 'captain-server.key'
    $serverCsr = Join-Path $tempRoot 'captain-server.csr'
    $serverCert = Join-Path $tempRoot 'captain-server.crt'
    $clientCaKey = Join-Path $tempRoot 'client-ca.key'
    $clientCa = Join-Path $tempRoot 'client-ca.crt'
    $clientKey = Join-Path $tempRoot 'mini-pc-client.key'
    $clientCsr = Join-Path $tempRoot 'mini-pc-client.csr'
    $clientCert = Join-Path $tempRoot 'mini-pc-client.crt'

    Invoke-OpenSsl @('req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','825','-subj','/CN=Captain Portal Server CA','-keyout',$serverCaKey,'-out',$serverCa)
    Invoke-OpenSsl @('req','-newkey','rsa:3072','-nodes','-sha256','-subj','/CN=captain-portal-link.internal','-keyout',$serverKey,'-out',$serverCsr)
    Invoke-OpenSsl @('x509','-req','-sha256','-days','397','-in',$serverCsr,'-CA',$serverCa,'-CAkey',$serverCaKey,'-set_serial','1001','-extfile',$serverExt,'-out',$serverCert)
    Invoke-OpenSsl @('req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','825','-subj','/CN=Captain Portal Client CA','-keyout',$clientCaKey,'-out',$clientCa)
    Invoke-OpenSsl @('req','-newkey','rsa:3072','-nodes','-sha256','-subj','/CN=mini-pc-portal','-keyout',$clientKey,'-out',$clientCsr)
    Invoke-OpenSsl @('x509','-req','-sha256','-days','397','-in',$clientCsr,'-CA',$clientCa,'-CAkey',$clientCaKey,'-set_serial','2001','-extfile',$clientExt,'-out',$clientCert)

    foreach ($relative in $expected) {
        $parent = Split-Path -Parent (Join-Path $secretRoot $relative)
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $captainConfig = @"
[Interface]
Address = 10.77.0.1/30
PrivateKey = $captainPrivate

[Peer]
PublicKey = $miniPublic
Endpoint = ${MiniPcEndpoint}:51820
AllowedIPs = 10.77.0.2/32
PersistentKeepalive = 25
"@
    $miniConfig = @"
[Interface]
Address = 10.77.0.2/30
ListenPort = 51820
PrivateKey = $miniPrivate

[Peer]
PublicKey = $captainPublic
AllowedIPs = 10.77.0.1/32
"@
    Set-Content -LiteralPath (Join-Path $secretRoot 'captain/wireguard/captain.conf') -Encoding ascii -NoNewline -Value $captainConfig
    Set-Content -LiteralPath (Join-Path $secretRoot 'mini-pc/wireguard/mini-pc.conf') -Encoding ascii -NoNewline -Value $miniConfig
    Copy-Item -LiteralPath $serverCert -Destination (Join-Path $secretRoot 'captain/captain-server.crt') -Force
    Copy-Item -LiteralPath $serverKey -Destination (Join-Path $secretRoot 'captain/captain-server.key') -Force
    Copy-Item -LiteralPath $clientCa -Destination (Join-Path $secretRoot 'captain/mini-pc-client-ca.crt') -Force
    Copy-Item -LiteralPath $serverCa -Destination (Join-Path $secretRoot 'mini-pc/captain-server-ca.crt') -Force
    Copy-Item -LiteralPath $clientCert -Destination (Join-Path $secretRoot 'mini-pc/mini-pc-client.crt') -Force
    Copy-Item -LiteralPath $clientKey -Destination (Join-Path $secretRoot 'mini-pc/mini-pc-client.key') -Force

    foreach ($relative in $expected) {
        $path = Join-Path $secretRoot $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item $path).Length -eq 0) {
            throw 'portal-link secret provisioning did not produce the complete file set'
        }
        & icacls.exe $path /inheritance:r /grant:r "${env:USERNAME}:(F)" *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'portal-link secret file ACL hardening failed'
        }
    }

    [ordered]@{
        schema = $plan.schema
        applied = $true
        rotated = [bool]$Rotate
        secret_file_count = $expected.Count
        server_ca_sha256 = (Get-FileHash -Algorithm SHA256 (Join-Path $secretRoot 'mini-pc/captain-server-ca.crt')).Hash.ToLowerInvariant()
        client_ca_sha256 = (Get-FileHash -Algorithm SHA256 (Join-Path $secretRoot 'captain/mini-pc-client-ca.crt')).Hash.ToLowerInvariant()
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $resolvedTemp -PathType Container) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
