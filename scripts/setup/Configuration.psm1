Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-DotEnv {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?:#|$)') { continue }
        if ($line -notmatch '^\s*(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') { continue }
        $key = $Matches.key
        $rawValue = $Matches.value.Trim()
        if ($rawValue.StartsWith('"') -and $rawValue.EndsWith('"')) {
            try { $values[$key] = $rawValue | ConvertFrom-Json }
            catch { throw "Ungültiger Wert für $key in $Path" }
        }
        else {
            $values[$key] = $rawValue
        }
    }
    $values
}

function Write-DotEnv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][hashtable] $Values
    )

    $directory = Split-Path $Path -Parent
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    $lines = foreach ($key in ($Values.Keys | Sort-Object)) {
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "Ungültiger Konfigurationsname: $key" }
        $encoded = ConvertTo-Json -InputObject ([string]$Values[$key]) -Compress
        "$key=$encoded"
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
}

function New-SetupSecret {
    [CmdletBinding()]
    param()

    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-OrCreateSecret {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string] $ExistingValue,
        [scriptblock] $PromptProvider,
        [switch] $Generate
    )

    if (-not [string]::IsNullOrWhiteSpace($ExistingValue)) { return $ExistingValue }
    if ($Generate) { return New-SetupSecret }
    if ($null -eq $PromptProvider) { return '' }
    & $PromptProvider
}

function Test-TrackedSecretPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [scriptblock] $GitRunner = {
            param($arguments)
            $output = & git @arguments 2>&1
            [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output -join [Environment]::NewLine }
        }
    )

    $tracked = & $GitRunner @('ls-files', '--error-unmatch', '--', $Path)
    if ($tracked.ExitCode -eq 0) { return $false }
    $ignored = & $GitRunner @('check-ignore', '-q', '--', $Path)
    $ignored.ExitCode -eq 0
}

function Resolve-N8nMode {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][uri] $Url,
        [scriptblock] $Probe = {
            param($candidateUrl)
            try {
                Invoke-WebRequest -Uri $candidateUrl -Method Get -TimeoutSec 5 -UseBasicParsing | Out-Null
                $true
            }
            catch { $false }
        },
        [scriptblock] $ConfirmAdoption = { $false }
    )

    if ((& $Probe $Url) -and (& $ConfirmAdoption $Url)) { return 'External' }
    'Owned'
}

Export-ModuleMember -Function @(
    'Read-DotEnv',
    'Write-DotEnv',
    'New-SetupSecret',
    'Get-OrCreateSecret',
    'Test-TrackedSecretPath',
    'Resolve-N8nMode'
)
