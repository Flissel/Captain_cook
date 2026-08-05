Set-StrictMode -Version Latest

function Write-ManagedProcessIdentity {
    param(
        [Parameter(Mandatory)][Diagnostics.Process]$Process,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ConfigurationSha256
    )
    $parent = Split-Path $Path -Parent
    if ($parent) { New-Item -ItemType Directory -Force $parent | Out-Null }
    [ordered]@{
        schema = 'captain.managed-process-identity.v1'
        pid = $Process.Id
        started_at = $Process.StartTime.ToUniversalTime().ToString('o')
        executable = [IO.Path]::GetFullPath($Process.Path)
        configuration_sha256 = $ConfigurationSha256
    } | ConvertTo-Json -Compress | Set-Content $Path -Encoding utf8
}

function Get-ManagedProcessIdentity {
    param(
        [Parameter(Mandatory)][string]$Path,
        [int]$ListenerPid = 0,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ConfigurationSha256,
        [switch]$AllowExited
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Managed process identity file is missing.'
    }
    try { $identity = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { throw 'Managed process identity file is invalid.' }
    if (
        [string]$identity.schema -cne 'captain.managed-process-identity.v1' -or
        [string]$identity.configuration_sha256 -cne $ConfigurationSha256
    ) {
        throw 'Managed process configuration identity does not match.'
    }
    $recordedPid = [int]$identity.pid
    if ($ListenerPid -ne 0 -and $recordedPid -ne $ListenerPid) {
        throw 'Managed process identity does not own the expected listener.'
    }
    $process = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
    if (-not $process) {
        if ($AllowExited) { return $null }
        throw 'Managed process identity no longer has a running process.'
    }
    $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
    if (
        $process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or
        [IO.Path]::GetFullPath($process.Path) -cne [IO.Path]::GetFullPath([string]$identity.executable)
    ) {
        throw 'Managed process PID no longer belongs to the recorded executable.'
    }
    return $process
}

function Get-ManagedListenerIdentity {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateRange(1, 65535)][int]$Port,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ConfigurationSha256
    )
    $listeners = @(
        Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -ne 1) {
        throw 'Managed process requires exactly one expected TCP listener.'
    }
    return Get-ManagedProcessIdentity `
        -Path $Path `
        -ListenerPid $listeners[0].OwningProcess `
        -ConfigurationSha256 $ConfigurationSha256
}

function Get-ManagedListenerIdentityForConfigurationReplacement {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateRange(1, 65535)][int]$Port,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ReplacementConfigurationSha256
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Managed process identity file is missing.'
    }
    try { $identity = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { throw 'Managed process identity file is invalid.' }
    $recordedConfiguration = [string]$identity.configuration_sha256
    if (
        [string]$identity.schema -cne 'captain.managed-process-identity.v1' -or
        $recordedConfiguration -notmatch '^[0-9a-f]{64}$' -or
        $recordedConfiguration -ceq $ReplacementConfigurationSha256
    ) {
        throw 'Managed process identity is not an eligible configuration replacement.'
    }
    $listeners = @(
        Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -ne 1) {
        throw 'Managed process requires exactly one expected TCP listener.'
    }
    return Get-ManagedProcessIdentity `
        -Path $Path `
        -ListenerPid $listeners[0].OwningProcess `
        -ConfigurationSha256 $recordedConfiguration
}
