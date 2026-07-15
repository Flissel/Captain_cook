Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')
Import-Module (Join-Path $PSScriptRoot 'Configuration.psm1')

function Invoke-ComponentCommand {
    param([scriptblock] $CommandRunner, [string] $FilePath, [string[]] $ArgumentList, [string] $WorkingDirectory)

    $result = & $CommandRunner $FilePath $ArgumentList $WorkingDirectory
    if ($null -eq $result) { return [pscustomobject]@{ ExitCode = 0; Output = '' } }
    $result
}

function Install-Captain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $HealthCheck = { param($candidateRoot) Test-Path (Join-Path $candidateRoot 'artifacts/demo-run.json') },
        [scriptblock] $CommandRunner = { param($filePath, $argumentList, $workingDirectory) Invoke-SetupCommand -FilePath $filePath -ArgumentList $argumentList -WorkingDirectory $workingDirectory }
    )

    if (& $HealthCheck $Root) { return New-SetupResult -Component 'Captain Cook' -Status 'Ready' -Message 'Captain Cook ist bereits verifiziert.' -Remediation 'None' }
    $venvPython = Join-Path $Root '.venv/Scripts/python.exe'
    $commands = @(
        @('python', @('-m', 'venv', (Join-Path $Root '.venv'))),
        @($venvPython, @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements.txt'))),
        @($venvPython, @((Join-Path $Root 'main.py'), 'demo', '--output', (Join-Path $Root 'artifacts/demo-run.json')))
    )
    foreach ($command in $commands) {
        $result = Invoke-ComponentCommand $CommandRunner $command[0] $command[1] $Root
        if ($result.ExitCode -ne 0) { return New-SetupResult -Component 'Captain Cook' -Status 'Failed' -Message 'Captain Cook konnte nicht installiert oder verifiziert werden.' -Remediation 'Retry' -Data @{ Output = $result.Output } }
    }
    New-SetupResult -Component 'Captain Cook' -Status 'Ready' -Message 'Captain Cook und die Offline-Demo sind bereit.' -Remediation 'None'
}

function Install-Hermes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $HealthCheck = { param($candidateRoot) Test-Path (Join-Path $candidateRoot '.captain-cook/hermes/Scripts/hermes.exe') },
        [scriptblock] $CommandRunner = { param($filePath, $argumentList, $workingDirectory) Invoke-SetupCommand -FilePath $filePath -ArgumentList $argumentList -WorkingDirectory $workingDirectory }
    )

    if (& $HealthCheck $Root) { return New-SetupResult -Component 'Hermes' -Status 'Ready' -Message 'Hermes ist bereits installiert.' -Remediation 'None' }
    $source = Join-Path $Root 'hermes-agent'
    if (-not (Test-Path (Join-Path $source 'pyproject.toml'))) { return New-SetupResult -Component 'Hermes' -Status 'Missing' -Message 'Das Hermes-Submodul fehlt. Führe git submodule update --init aus.' -Remediation 'Manual' }
    $venv = Join-Path $Root '.captain-cook/hermes'
    $python = Join-Path $venv 'Scripts/python.exe'
    $hermes = Join-Path $venv 'Scripts/hermes.exe'
    $commands = @(
        @('python', @('-m', 'venv', $venv)),
        @($python, @('-m', 'pip', 'install', '--editable', $source)),
        @($hermes, @('--help'))
    )
    foreach ($command in $commands) {
        $result = Invoke-ComponentCommand $CommandRunner $command[0] $command[1] $Root
        if ($result.ExitCode -ne 0) { return New-SetupResult -Component 'Hermes' -Status 'Failed' -Message 'Hermes konnte nicht aus dem lokalen Quellcode installiert werden.' -Remediation 'Retry' -Data @{ Output = $result.Output } }
    }
    New-SetupResult -Component 'Hermes' -Status 'Ready' -Message 'Hermes wurde aus dem lokalen Quellcode installiert und geprüft.' -Remediation 'None' -Data @{ Executable = $hermes }
}

function Install-Minibook {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $HealthCheck = { $false },
        [scriptblock] $CommandRunner = { param($filePath, $argumentList, $workingDirectory) Invoke-SetupCommand -FilePath $filePath -ArgumentList $argumentList -WorkingDirectory $workingDirectory }
    )

    if (& $HealthCheck $Root) { return New-SetupResult -Component 'Minibook' -Status 'Ready' -Message 'Minibook ist bereits verifiziert.' -Remediation 'None' }
    $source = Join-Path $Root 'minibook'
    $venv = Join-Path $source '.venv'
    $python = Join-Path $venv 'Scripts/python.exe'
    $frontend = Join-Path $source 'frontend'
    $npmVerb = if (Test-Path (Join-Path $frontend 'package-lock.json')) { 'ci' } else { 'install' }
    $commands = @(
        @('python', @('-m', 'venv', $venv), $source),
        @($python, @('-m', 'pip', 'install', '-r', (Join-Path $source 'requirements.txt')), $source),
        @('npm', @($npmVerb), $frontend),
        @('npm', @('run', 'build'), $frontend)
    )
    foreach ($command in $commands) {
        $result = Invoke-ComponentCommand $CommandRunner $command[0] $command[1] $command[2]
        if ($result.ExitCode -ne 0) { return New-SetupResult -Component 'Minibook' -Status 'Failed' -Message 'Minibook konnte nicht installiert werden.' -Remediation 'Retry' -Data @{ Output = $result.Output } }
    }
    New-SetupResult -Component 'Minibook' -Status 'Ready' -Message 'Minibook-Backend und Frontend sind installiert.' -Remediation 'None'
}

function Install-MinibookSkill {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Source,
        [Parameter(Mandatory)][string] $DestinationDirectory
    )

    if (-not (Test-Path -LiteralPath $Source)) { return New-SetupResult -Component 'Minibook Skill' -Status 'Missing' -Message 'Die Minibook-Skill-Datei fehlt.' -Remediation 'Manual' }
    New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
    Copy-Item -LiteralPath $Source -Destination (Join-Path $DestinationDirectory 'SKILL.md') -Force
    New-SetupResult -Component 'Minibook Skill' -Status 'Ready' -Message 'Der Minibook Skill ist im Hermes-Profil installiert.' -Remediation 'None'
}

function Register-HermesIdentity {
    [CmdletBinding()]
    param(
        [scriptblock] $CurrentIdentityProbe,
        [scriptblock] $RegistrationRequest
    )

    if (& $CurrentIdentityProbe) { return New-SetupResult -Component 'Hermes Identity' -Status 'Ready' -Message 'Die vorhandene Hermes-Identität ist gültig.' -Remediation 'None' }
    try { $identity = & $RegistrationRequest }
    catch { return New-SetupResult -Component 'Hermes Identity' -Status 'Failed' -Message 'Hermes konnte nicht bei Minibook registriert werden.' -Remediation 'Retry' }
    if ($null -eq $identity -or [string]::IsNullOrWhiteSpace([string]$identity.api_key)) { return New-SetupResult -Component 'Hermes Identity' -Status 'Failed' -Message 'Minibook hat keinen API-Key geliefert.' -Remediation 'Retry' }
    New-SetupResult -Component 'Hermes Identity' -Status 'Ready' -Message 'Hermes wurde bei Minibook registriert.' -Remediation 'None' -Data @{ ApiKey = $identity.api_key }
}

function Save-HermesMinibookCredential {
    [CmdletBinding()]
    param([string] $HermesHome, [string] $BaseUrl, [string] $ApiKey)

    $path = Join-Path $HermesHome '.env'
    $values = Read-DotEnv -Path $path
    $values.MINIBOOK_BASE_URL = $BaseUrl
    $values.MINIBOOK_API_KEY = $ApiKey
    Write-DotEnv -Path $path -Values $values
    New-SetupResult -Component 'Hermes Credential' -Status 'Ready' -Message 'Die Minibook-Zugangsdaten wurden im lokalen Hermes-Profil gespeichert.' -Remediation 'None' -Data @{ Path = $path }
}

Export-ModuleMember -Function @(
    'Install-Captain',
    'Install-Hermes',
    'Install-Minibook',
    'Install-MinibookSkill',
    'Register-HermesIdentity',
    'Save-HermesMinibookCredential'
)
