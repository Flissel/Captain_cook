Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')

$script:ApprovedPackages = @{
    Git        = 'Git.Git'
    Python     = 'Python.Python.3.11'
    Node       = 'OpenJS.NodeJS.LTS'
    Docker     = 'Docker.DockerDesktop'
    PowerShell = 'Microsoft.PowerShell'
}

function Test-SetupPlatform {
    [CmdletBinding()]
    param(
        [int] $BuildNumber = [Environment]::OSVersion.Version.Build,
        [version] $PowerShellVersion = $PSVersionTable.PSVersion
    )

    if (-not $IsWindows -or $BuildNumber -lt 22000) {
        return New-SetupResult -Component 'Windows' -Status 'Invalid' -Message 'Windows 11 (Build 22000 oder neuer) wird benötigt.' -Remediation 'Manual'
    }
    if ($PowerShellVersion -lt [version]'7.0') {
        return New-SetupResult -Component 'PowerShell' -Status 'Missing' -Message 'PowerShell 7 wird benötigt.' -Remediation 'Install'
    }
    New-SetupResult -Component 'Windows' -Status 'Ready' -Message 'Windows 11 und PowerShell 7 sind bereit.' -Remediation 'None'
}

function Test-SetupExecutable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][version] $MinimumVersion,
        [version] $MaximumVersion,
        [scriptblock] $Resolver = { param($commandName) Get-Command $commandName -ErrorAction SilentlyContinue },
        [scriptblock] $VersionProvider = { param($command) [version]$command.Version }
    )

    $command = & $Resolver $Name
    if ($null -eq $command) {
        return New-SetupResult -Component $Name -Status 'Missing' -Message "$Name fehlt." -Remediation 'Install'
    }

    try { $version = [version](& $VersionProvider $command) }
    catch { return New-SetupResult -Component $Name -Status 'Invalid' -Message "Die Version von $Name konnte nicht bestimmt werden." -Remediation 'Manual' }

    if ($version -lt $MinimumVersion -or ($null -ne $MaximumVersion -and $version -ge $MaximumVersion)) {
        return New-SetupResult -Component $Name -Status 'Invalid' -Message "$Name $version erfüllt die Versionsanforderung nicht." -Remediation 'Install' -Data @{ Version = $version.ToString() }
    }
    New-SetupResult -Component $Name -Status 'Ready' -Message "$Name $version ist bereit." -Remediation 'None' -Data @{ Version = $version.ToString(); Path = $command.Source }
}

function Test-ExpectedSetupPortOwner {
    param(
        [Parameter(Mandatory)][string] $Root,
        [Parameter(Mandatory)][string] $ExpectedOwner,
        [Parameter(Mandatory)][int] $Port,
        [Parameter(Mandatory)][object] $Connection,
        [AllowNull()][uri] $Endpoint
    )

    if ($ExpectedOwner -in @('minibook-backend', 'minibook-frontend')) {
        $metadataPath = Join-Path $Root ".captain-cook/runtime/$ExpectedOwner.json"
        if (-not (Test-Path -LiteralPath $metadataPath)) { return $false }
        try {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
            if ([int]$metadata.Id -ne [int]$Connection.OwningProcess) { return $false }
            $process = Get-Process -Id ([int]$metadata.Id) -ErrorAction Stop
            if ($process.StartTime.ToUniversalTime().Ticks -ne [long]$metadata.StartTimeUtcTicks) { return $false }
            if ($null -ne $Endpoint) {
                $response = Invoke-WebRequest -Uri $Endpoint -Method Get -TimeoutSec 5 -UseBasicParsing
                if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 400) { return $false }
            }
            return $true
        }
        catch { return $false }
    }

    if ($ExpectedOwner -notin @('mailpit', 'mariadb', 'n8n')) { return $false }
    $arguments = @(
        'compose', '--project-directory', $Root, '--env-file', (Join-Path $Root '.env'),
        'ps', '--format', 'json', $ExpectedOwner
    )
    try {
        $commandResult = Common\Invoke-SetupCommand -FilePath 'docker' -ArgumentList $arguments
        if ($commandResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace([string]$commandResult.Output)) { return $false }
        $records = @($commandResult.Output | ConvertFrom-Json)
        foreach ($record in $records) {
            $publishedPorts = @($record.Publishers | Where-Object {
                [int]$_.PublishedPort -eq $Port -and [string]$_.Protocol -eq 'tcp'
            })
            if ([string]$record.Service -ceq $ExpectedOwner -and
                [string]$record.State -ceq 'running' -and
                [string]$record.Health -ceq 'healthy' -and
                $publishedPorts.Count -gt 0) {
                return $true
            }
        }
    }
    catch { return $false }
    $false
}

function Test-SetupPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateRange(1, 65535)][int] $Port,
        [string] $Root = (Get-Location).Path,
        [string] $ExpectedOwner,
        [AllowNull()][uri] $ExpectedEndpoint,
        [scriptblock] $ConnectionProvider = { param($candidatePort) Get-NetTCPConnection -State Listen -LocalPort $candidatePort -ErrorAction SilentlyContinue | Select-Object -First 1 },
        [scriptblock] $OwnershipProvider = {
            param($candidateRoot, $candidateOwner, $connection, $endpoint, $candidatePort)
            Test-ExpectedSetupPortOwner -Root $candidateRoot -ExpectedOwner $candidateOwner `
                -Port $candidatePort -Connection $connection -Endpoint $endpoint
        }
    )

    $connection = & $ConnectionProvider $Port
    if ($null -eq $connection) {
        return New-SetupResult -Component "Port $Port" -Status 'Ready' -Message "Port $Port ist frei." -Remediation 'None'
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedOwner)) {
        try {
            $isExpectedOwner = [bool](& $OwnershipProvider $Root $ExpectedOwner $connection $ExpectedEndpoint $Port)
        }
        catch { $isExpectedOwner = $false }
        if ($isExpectedOwner) {
            return New-SetupResult -Component "Port $Port" -Status 'Ready' `
                -Message "Port $Port wird vom erwarteten Captain-Dienst $ExpectedOwner verwendet." `
                -Remediation 'None' -Data @{ OwningProcess = $connection.OwningProcess; ExpectedOwner = $ExpectedOwner }
        }
    }
    New-SetupResult -Component "Port $Port" -Status 'Invalid' -Message "Port $Port wird bereits verwendet." -Remediation 'Manual' -Data @{ OwningProcess = $connection.OwningProcess }
}

function Test-SetupDiskSpace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Path,
        [long] $MinimumBytes = 4GB,
        [scriptblock] $DriveProvider = {
            param($candidatePath)
            $root = [IO.Path]::GetPathRoot((Resolve-Path $candidatePath).Path)
            Get-PSDrive -Name $root.TrimEnd('\').TrimEnd(':')
        }
    )

    $drive = & $DriveProvider $Path
    if ($null -eq $drive -or $drive.Free -lt $MinimumBytes) {
        $freeBytes = if ($null -eq $drive) { 0 } else { [long]$drive.Free }
        return New-SetupResult -Component 'Disk' -Status 'Invalid' -Message 'Mindestens 4 GB freier Speicher werden benötigt.' -Remediation 'Manual' -Data @{ FreeBytes = $freeBytes }
    }
    New-SetupResult -Component 'Disk' -Status 'Ready' -Message 'Ausreichend freier Speicher ist verfügbar.' -Remediation 'None' -Data @{ FreeBytes = [long]$drive.Free }
}

function Test-SetupNetwork {
    [CmdletBinding()]
    param(
        [uri] $Uri = 'https://pypi.org/',
        [scriptblock] $Probe = {
            param($candidateUri)
            try {
                Invoke-WebRequest -Uri $candidateUri -Method Head -TimeoutSec 10 -UseBasicParsing | Out-Null
                $true
            }
            catch { $false }
        }
    )

    if (-not (& $Probe $Uri)) {
        return New-SetupResult -Component 'Network' -Status 'Failed' -Message 'Die Paketquellen sind derzeit nicht erreichbar.' -Remediation 'Retry'
    }
    New-SetupResult -Component 'Network' -Status 'Ready' -Message 'Die Paketquellen sind erreichbar.' -Remediation 'None'
}

function Test-DockerRuntime {
    [CmdletBinding()]
    param(
        [scriptblock] $CommandRunner = { param($commandPath, $commandArguments) Common\Invoke-SetupCommand -FilePath $commandPath -ArgumentList $commandArguments }
    )

    $engine = & $CommandRunner 'docker' @('info')
    if ($engine.ExitCode -ne 0) {
        return New-SetupResult -Component 'Docker' -Status 'Failed' -Message 'Docker Desktop läuft nicht.' -Remediation 'Retry'
    }
    $compose = & $CommandRunner 'docker' @('compose', 'version')
    if ($compose.ExitCode -ne 0 -or $compose.Output -notmatch '(?i)(?:v|version\s+v?)(?<major>\d+)\.') {
        return New-SetupResult -Component 'Docker Compose' -Status 'Missing' -Message 'Docker Compose v2 fehlt.' -Remediation 'Install'
    }
    if ([int]$Matches.major -lt 2) {
        return New-SetupResult -Component 'Docker Compose' -Status 'Invalid' -Message 'Docker Compose v2 oder neuer wird benötigt.' -Remediation 'Install'
    }
    New-SetupResult -Component 'Docker' -Status 'Ready' -Message 'Docker Desktop und Compose v2 sind bereit.' -Remediation 'None'
}

function Install-SetupPrerequisite {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][scriptblock] $ConfirmInstall,
        [scriptblock] $CommandRunner = { param($commandPath, $commandArguments) Common\Invoke-SetupCommand -FilePath $commandPath -ArgumentList $commandArguments }
    )

    if (-not $script:ApprovedPackages.ContainsKey($Name)) {
        throw "Nicht unterstützte Voraussetzung: $Name"
    }
    if (-not (& $ConfirmInstall $Name)) {
        return New-SetupResult -Component $Name -Status 'Skipped' -Message 'Installation wurde übersprungen.' -Remediation 'Manual'
    }

    $arguments = @('install', '--id', $script:ApprovedPackages[$Name], '-e', '--accept-package-agreements', '--accept-source-agreements')
    $commandResult = & $CommandRunner 'winget' $arguments
    if ($commandResult.ExitCode -ne 0) {
        return New-SetupResult -Component $Name -Status 'Failed' -Message "Installation von $Name ist fehlgeschlagen." -Remediation 'Retry' -Data @{ ExitCode = $commandResult.ExitCode }
    }
    New-SetupResult -Component $Name -Status 'Ready' -Message "$Name wurde installiert." -Remediation 'None'
}

function Confirm-InstalledPrerequisite {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Name,
        [scriptblock] $Resolver = { param($commandName) Get-Command $commandName -ErrorAction SilentlyContinue }
    )

    try {
        $resolvedCommands = @(& $Resolver $Name)
    }
    catch {
        $resolvedCommands = @()
    }

    if ($resolvedCommands.Count -ne 1 -or $null -eq $resolvedCommands[0]) {
        return New-SetupResult -Component $Name -Status 'RestartRequired' `
            -Message "$Name wurde installiert, ist in dieser PowerShell-Sitzung aber noch nicht sichtbar." `
            -Remediation 'Restart'
    }

    $command = $resolvedCommands[0]
    $pathProperty = $command.PSObject.Properties['Source']
    if ($null -eq $pathProperty -or $pathProperty.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace($pathProperty.Value)) {
        return New-SetupResult -Component $Name -Status 'RestartRequired' `
            -Message "$Name wurde installiert, ist in dieser PowerShell-Sitzung aber noch nicht sichtbar." `
            -Remediation 'Restart'
    }

    New-SetupResult -Component $Name -Status 'Ready' `
        -Message "$Name ist nach der Installation verfügbar." -Remediation 'None' `
        -Data @{ Path = [string]$pathProperty.Value }
}

function Get-PreflightConfigurationValue {
    param(
        [Parameter(Mandatory)][hashtable] $Configuration,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $DefaultValue
    )

    if ($Configuration.ContainsKey($Name) -and
        -not [string]::IsNullOrWhiteSpace([string]$Configuration[$Name])) {
        return [string]$Configuration[$Name]
    }
    $DefaultValue
}

function Get-ConfiguredPreflightPorts {
    param([Parameter(Mandatory)][hashtable] $Configuration)

    $ports = [Collections.Generic.List[object]]::new()
    foreach ($definition in @(
        [pscustomobject]@{
            ConfigurationKey = 'MINIBOOK_BACKEND_URL'; DefaultValue = 'http://localhost:3456'
            ExpectedOwner = 'minibook-backend'; HealthPath = '/health'
        }
        [pscustomobject]@{
            ConfigurationKey = 'MINIBOOK_PUBLIC_URL'; DefaultValue = 'http://localhost:3457'
            ExpectedOwner = 'minibook-frontend'; HealthPath = '/api/v1/version'
        }
    )) {
        $value = Get-PreflightConfigurationValue -Configuration $Configuration `
            -Name $definition.ConfigurationKey -DefaultValue $definition.DefaultValue
        try { $uri = [uri]$value }
        catch { throw "Ungültige Preflight-Konfiguration für $($definition.ConfigurationKey)." }
        if (-not $uri.IsAbsoluteUri) { throw "Ungültige Preflight-Konfiguration für $($definition.ConfigurationKey)." }
        if (-not $uri.IsLoopback) { continue }
        $endpoint = [uri]($uri.AbsoluteUri.TrimEnd('/') + $definition.HealthPath)
        $ports.Add([pscustomobject]@{
            Port = $uri.Port; ExpectedOwner = $definition.ExpectedOwner; ExpectedEndpoint = $endpoint
        })
    }

    foreach ($definition in @(
        [pscustomobject]@{ ConfigurationKey = 'MAILPIT_WEB_PORT'; DefaultValue = '8025'; ExpectedOwner = 'mailpit'; HealthPath = '/api/v1/info' }
        [pscustomobject]@{ ConfigurationKey = 'MAILPIT_SMTP_PORT'; DefaultValue = '1025'; ExpectedOwner = 'mailpit'; HealthPath = $null }
        [pscustomobject]@{ ConfigurationKey = 'MARIADB_PORT'; DefaultValue = '3306'; ExpectedOwner = 'mariadb'; HealthPath = $null }
    )) {
        $value = Get-PreflightConfigurationValue -Configuration $Configuration `
            -Name $definition.ConfigurationKey -DefaultValue $definition.DefaultValue
        $port = 0
        if (-not [int]::TryParse($value, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
            throw "Ungültige Preflight-Konfiguration für $($definition.ConfigurationKey)."
        }
        $endpoint = if ($null -eq $definition.HealthPath) {
            $null
        }
        else {
            [uri]"http://localhost:$port$($definition.HealthPath)"
        }
        $ports.Add([pscustomobject]@{
            Port = $port; ExpectedOwner = $definition.ExpectedOwner; ExpectedEndpoint = $endpoint
        })
    }

    $n8nMode = Get-PreflightConfigurationValue -Configuration $Configuration `
        -Name 'N8N_MODE' -DefaultValue 'external'
    if ($n8nMode -ceq 'owned') {
        $ports.Add([pscustomobject]@{
            Port = 5678; ExpectedOwner = 'n8n'; ExpectedEndpoint = [uri]'http://localhost:5678/healthz'
        })
    }
    elseif ($n8nMode -cne 'external') {
        throw 'Ungültige Preflight-Konfiguration für N8N_MODE.'
    }
    $ports.ToArray()
}

function Get-PreflightResults {
    [CmdletBinding()]
    param(
        [string] $Root = (Get-Location).Path,
        [hashtable] $Configuration = @{},
        [scriptblock] $RequirementResultProvider = {
            param($candidateRoot)
            @(
                Test-SetupPlatform
                Test-SetupDiskSpace -Path $candidateRoot
                Test-SetupNetwork
                Test-SetupExecutable -Name 'git' -MinimumVersion '2.0'
                Test-SetupExecutable -Name 'python' -MinimumVersion '3.11' -MaximumVersion '3.14'
                Test-SetupExecutable -Name 'node' -MinimumVersion '20.0'
                Test-SetupExecutable -Name 'docker' -MinimumVersion '20.0'
                Test-DockerRuntime
            )
        },
        [scriptblock] $PortConnectionProvider = { param($candidatePort) Get-NetTCPConnection -State Listen -LocalPort $candidatePort -ErrorAction SilentlyContinue | Select-Object -First 1 },
        [scriptblock] $PortOwnershipProvider
    )

    $results = [Collections.Generic.List[object]]::new()
    foreach ($result in @(& $RequirementResultProvider $Root)) { $results.Add($result) }
    foreach ($portDefinition in @(Get-ConfiguredPreflightPorts -Configuration $Configuration)) {
        $parameters = @{
            Port = $portDefinition.Port
            Root = $Root
            ExpectedOwner = $portDefinition.ExpectedOwner
            ExpectedEndpoint = $portDefinition.ExpectedEndpoint
            ConnectionProvider = $PortConnectionProvider
        }
        if ($null -ne $PortOwnershipProvider) { $parameters.OwnershipProvider = $PortOwnershipProvider }
        $results.Add((Test-SetupPort @parameters))
    }
    $results.ToArray()
}

function Test-SetupPreflight {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [hashtable] $Configuration = @{},
        [scriptblock] $ResultProvider = {
            param($candidateRoot, $candidateConfiguration)
            Get-PreflightResults -Root $candidateRoot -Configuration $candidateConfiguration
        }
    )

    try {
        $results = @(& $ResultProvider $Root $Configuration)
    }
    catch {
        $results = @()
    }

    $allowedStatuses = @('Ready', 'Missing', 'Invalid', 'Failed', 'Skipped', 'RestartRequired')
    $allowedRemediations = @('None', 'Install', 'Configure', 'Retry', 'Restart', 'Manual')
    $requiredProperties = @('Component', 'Status', 'Message', 'Remediation', 'Data')
    $validResults = [Collections.Generic.List[object]]::new()
    foreach ($result in $results) {
        if ($null -eq $result) { continue }
        $propertyNames = @($result.PSObject.Properties.Name)
        $hasRequiredProperties = -not @($requiredProperties | Where-Object { $_ -notin $propertyNames }).Count
        if (-not $hasRequiredProperties -or
            $result.Component -isnot [string] -or [string]::IsNullOrWhiteSpace($result.Component) -or
            $result.Status -isnot [string] -or $result.Status -cnotin $allowedStatuses -or
            $result.Message -isnot [string] -or [string]::IsNullOrWhiteSpace($result.Message) -or
            $result.Remediation -isnot [string] -or $result.Remediation -cnotin $allowedRemediations -or
            $result.Data -isnot [Collections.IDictionary]) {
            continue
        }
        $validResults.Add($result)
    }

    if ($results.Count -eq 0 -or $validResults.Count -ne $results.Count) {
        return New-SetupResult -Component 'Preflight' -Status 'Failed' `
            -Message 'Die Preflight-Prüfung hat kein gültiges vollständiges Ergebnis geliefert.' `
            -Remediation 'Retry' -Data @{ Results = [object[]]@() }
    }

    $stableResults = [object[]]$validResults.ToArray()
    $nonReady = @($stableResults | Where-Object Status -cne 'Ready')
    if ($nonReady.Count -eq 0) {
        return New-SetupResult -Component 'Preflight' -Status 'Ready' `
            -Message 'Alle Voraussetzungen sind bereit.' -Remediation 'None' `
            -Data @{ Results = $stableResults }
    }

    $aggregateStatus = @('RestartRequired', 'Missing', 'Invalid', 'Failed', 'Skipped') |
        Where-Object { $_ -cin $nonReady.Status } |
        Select-Object -First 1
    $primaryResult = $nonReady | Where-Object Status -CEQ $aggregateStatus | Select-Object -First 1
    New-SetupResult -Component 'Preflight' -Status $aggregateStatus `
        -Message ($nonReady.Message -join ' ') -Remediation $primaryResult.Remediation `
        -Data @{ Results = $stableResults }
}

Export-ModuleMember -Function @(
    'Test-SetupPlatform',
    'Test-SetupExecutable',
    'Test-SetupPort',
    'Test-SetupDiskSpace',
    'Test-SetupNetwork',
    'Test-DockerRuntime',
    'Install-SetupPrerequisite',
    'Confirm-InstalledPrerequisite',
    'Get-PreflightResults',
    'Test-SetupPreflight'
)
