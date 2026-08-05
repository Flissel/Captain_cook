[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$MiniPcAddress,
    [ValidatePattern('^[A-Za-z0-9.-]+$')]
    [string]$DnsName = 'mini-pc.local',
    [switch]$Apply,
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
$parsedAddress = $null
if (-not [Net.IPAddress]::TryParse($MiniPcAddress, [ref]$parsedAddress)) {
    throw 'mini PC address must be an IP address'
}

$root = Split-Path -Parent $PSScriptRoot
$secretRoot = Join-Path $root 'deploy/mini-pc-edge/.secrets'
$expected = @(
    'mini-pc-edge-ca.crt',
    'mini-pc-edge-ca.key',
    'mini-pc-edge.crt',
    'mini-pc-edge.key'
)
$plan = [ordered]@{
    schema = 'captain.mini-pc-edge-tls.v1'
    apply = [bool]$Apply
    rotate = [bool]$Rotate
    mini_pc_address = $MiniPcAddress
    dns_name = $DnsName
    secret_file_count = $expected.Count
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Compress
    Write-Output 'No certificate created. Re-run with -Apply after reviewing the endpoint.'
    exit 0
}

$existing = @($expected | Where-Object {
    Test-Path -LiteralPath (Join-Path $secretRoot $_) -PathType Leaf
})
if ($existing.Count -gt 0 -and -not $Rotate) {
    throw 'mini PC edge TLS files already exist; use -Rotate for explicit replacement'
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

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('captain-mini-pc-edge-' + [Guid]::NewGuid())
$resolvedTempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
if (-not $resolvedTemp.StartsWith($resolvedTempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'temporary certificate path escaped the system temp directory'
}
New-Item -ItemType Directory -Path $tempRoot | Out-Null

function Invoke-OpenSsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $openssl @Arguments *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'OpenSSL mini PC edge certificate generation failed'
    }
}

try {
    $extensions = Join-Path $tempRoot 'server-ext.cnf'
    Set-Content -LiteralPath $extensions -Encoding ascii -Value @(
        'basicConstraints=CA:FALSE',
        'keyUsage=digitalSignature,keyEncipherment',
        'extendedKeyUsage=serverAuth',
        'subjectAltName=@alt_names',
        '[alt_names]',
        'IP.1 = $MiniPcAddress',
        'DNS.1 = $DnsName'
    )
    # Expand only the two validated SAN values after writing the static template.
    (Get-Content -Raw -LiteralPath $extensions).
        Replace('$MiniPcAddress', $MiniPcAddress).
        Replace('$DnsName', $DnsName) |
        Set-Content -LiteralPath $extensions -Encoding ascii -NoNewline

    $caKey = Join-Path $tempRoot 'mini-pc-edge-ca.key'
    $caCert = Join-Path $tempRoot 'mini-pc-edge-ca.crt'
    $serverKey = Join-Path $tempRoot 'mini-pc-edge.key'
    $serverCsr = Join-Path $tempRoot 'mini-pc-edge.csr'
    $serverCert = Join-Path $tempRoot 'mini-pc-edge.crt'
    Invoke-OpenSsl @('req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','825','-subj','/CN=Captain Mini PC Edge CA','-keyout',$caKey,'-out',$caCert)
    Invoke-OpenSsl @('req','-newkey','rsa:3072','-nodes','-sha256','-subj',"/CN=$DnsName",'-keyout',$serverKey,'-out',$serverCsr)
    Invoke-OpenSsl @('x509','-req','-sha256','-days','397','-in',$serverCsr,'-CA',$caCert,'-CAkey',$caKey,'-set_serial','3001','-extfile',$extensions,'-out',$serverCert)

    New-Item -ItemType Directory -Force -Path $secretRoot | Out-Null
    foreach ($name in $expected) {
        Copy-Item -LiteralPath (Join-Path $tempRoot $name) -Destination (Join-Path $secretRoot $name) -Force
        & icacls.exe (Join-Path $secretRoot $name) /inheritance:r /grant:r "${env:USERNAME}:(F)" *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'mini PC edge TLS file ACL hardening failed'
        }
    }
    [ordered]@{
        schema = $plan.schema
        applied = $true
        rotated = [bool]$Rotate
        server_ca_sha256 = (Get-FileHash -Algorithm SHA256 (Join-Path $secretRoot 'mini-pc-edge-ca.crt')).Hash.ToLowerInvariant()
        secrets_emitted = $false
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $resolvedTemp -PathType Container) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
