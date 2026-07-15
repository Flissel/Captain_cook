Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')

function Get-ComposeArguments {
    param([string] $Root, [ValidateSet('Owned', 'External')][string] $N8nMode)

    $arguments = @('compose', '--project-directory', $Root, '--env-file', (Join-Path $Root '.env'))
    if ($N8nMode -eq 'Owned') { $arguments += @('--profile', 'owned-n8n') }
    $arguments
}

function Start-CaptainServices {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [Parameter(Mandatory)][ValidateSet('Owned', 'External')][string] $N8nMode,
        [scriptblock] $CommandRunner = { param($commandPath, $commandArguments) Common\Invoke-SetupCommand -FilePath $commandPath -ArgumentList $commandArguments }
    )

    $arguments = @(Get-ComposeArguments -Root $Root -N8nMode $N8nMode) + @('up', '-d', '--wait')
    $commandResult = & $CommandRunner 'docker' $arguments
    if ($commandResult.ExitCode -ne 0) {
        return New-SetupResult -Component 'Services' -Status 'Failed' -Message 'Die lokalen Dienste konnten nicht gestartet werden.' -Remediation 'Retry' -Data @{ ExitCode = $commandResult.ExitCode }
    }
    New-SetupResult -Component 'Services' -Status 'Ready' -Message 'Die lokalen Dienste sind gestartet.' -Remediation 'None'
}

function Stop-CaptainServices {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [Parameter(Mandatory)][ValidateSet('Owned', 'External')][string] $N8nMode,
        [scriptblock] $CommandRunner = { param($commandPath, $commandArguments) Common\Invoke-SetupCommand -FilePath $commandPath -ArgumentList $commandArguments }
    )

    $arguments = @(Get-ComposeArguments -Root $Root -N8nMode $N8nMode) + @('stop')
    $commandResult = & $CommandRunner 'docker' $arguments
    if ($commandResult.ExitCode -ne 0) {
        return New-SetupResult -Component 'Services' -Status 'Failed' -Message 'Die lokalen Dienste konnten nicht gestoppt werden.' -Remediation 'Retry' -Data @{ ExitCode = $commandResult.ExitCode }
    }
    New-SetupResult -Component 'Services' -Status 'Ready' -Message 'Die lokalen Dienste sind gestoppt; alle Daten bleiben erhalten.' -Remediation 'None'
}

function Test-HttpService {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][uri] $Uri,
        [ValidateRange(1, 20)][int] $AttemptCount = 5,
        [ValidateRange(0, 10000)][int] $DelayMilliseconds = 500,
        [scriptblock] $Probe = {
            param($candidateUri)
            try {
                $response = Invoke-WebRequest -Uri $candidateUri -Method Get -TimeoutSec 10 -UseBasicParsing
                $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
            }
            catch { $false }
        }
    )

    foreach ($attempt in 1..$AttemptCount) {
        if (& $Probe $Uri) {
            return New-SetupResult -Component $Name -Status 'Ready' -Message "$Name ist erreichbar." -Remediation 'None' -Data @{ Uri = $Uri.AbsoluteUri }
        }
        if ($attempt -lt $AttemptCount -and $DelayMilliseconds -gt 0) { Start-Sleep -Milliseconds $DelayMilliseconds }
    }
    New-SetupResult -Component $Name -Status 'Failed' -Message "$Name antwortet nicht unter $Uri." -Remediation 'Retry'
}

function Test-TcpService {
    [CmdletBinding()]
    param([string] $Name, [string] $ComputerName, [int] $Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($ComputerName, $Port)
        if (-not $task.Wait(5000) -or -not $client.Connected) {
            return New-SetupResult -Component $Name -Status 'Failed' -Message "$Name antwortet nicht auf Port $Port." -Remediation 'Retry'
        }
        New-SetupResult -Component $Name -Status 'Ready' -Message "$Name ist auf Port $Port erreichbar." -Remediation 'None'
    }
    catch { New-SetupResult -Component $Name -Status 'Failed' -Message "$Name antwortet nicht auf Port $Port." -Remediation 'Retry' }
    finally { $client.Dispose() }
}

function Test-MariaDbService {
    [CmdletBinding()]
    param(
        [string] $Root,
        [string] $User,
        [string] $Password,
        [scriptblock] $CommandRunner = { param($commandPath, $commandArguments) Common\Invoke-SetupCommand -FilePath $commandPath -ArgumentList $commandArguments }
    )

    $arguments = @('compose', '--project-directory', $Root, '--env-file', (Join-Path $Root '.env'), 'exec', '-T', '-e', 'MYSQL_PWD', 'mariadb', 'mariadb', '-u', $User, '-e', 'SELECT 1')
    $previousPassword = $env:MYSQL_PWD
    try {
        $env:MYSQL_PWD = $Password
        $result = & $CommandRunner 'docker' $arguments
    }
    finally {
        if ($null -eq $previousPassword) { Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue }
        else { $env:MYSQL_PWD = $previousPassword }
    }
    if ($result.ExitCode -ne 0) { return New-SetupResult -Component 'MariaDB' -Status 'Failed' -Message 'MariaDB lehnt die lokale Anmeldung ab.' -Remediation 'Configure' }
    New-SetupResult -Component 'MariaDB' -Status 'Ready' -Message 'MariaDB akzeptiert authentifizierte Abfragen.' -Remediation 'None'
}

function Get-ServiceHealth {
    [CmdletBinding()]
    param([hashtable] $Configuration)

    @(
        Test-HttpService -Name 'Mailpit' -Uri "http://localhost:$($Configuration.MAILPIT_WEB_PORT)/api/v1/info"
        Test-TcpService -Name 'Mailpit SMTP' -ComputerName 'localhost' -Port ([int]$Configuration.MAILPIT_SMTP_PORT)
        Test-HttpService -Name 'n8n' -Uri "$([string]$Configuration.N8N_URL.TrimEnd('/'))/healthz"
    )
}

Export-ModuleMember -Function @(
    'Start-CaptainServices',
    'Stop-CaptainServices',
    'Test-HttpService',
    'Test-TcpService',
    'Test-MariaDbService',
    'Get-ServiceHealth'
)
