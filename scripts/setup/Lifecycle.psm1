Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')
Import-Module (Join-Path $PSScriptRoot 'Configuration.psm1')
Import-Module (Join-Path $PSScriptRoot 'Preflight.psm1')
Import-Module (Join-Path $PSScriptRoot 'Components.psm1')
Import-Module (Join-Path $PSScriptRoot 'Repository.psm1')
Import-Module (Join-Path $PSScriptRoot 'Services.psm1')
Import-Module (Join-Path $PSScriptRoot 'StageValidation.psm1')

function Get-SetupStages {
    @('Preflight', 'Configuration', 'Captain', 'Hermes', 'Minibook', 'Services', 'Verification')
}

function ConvertTo-CheckpointTable {
    param([object] $Checkpoint)

    $table = @{}
    if ($Checkpoint -is [Collections.IDictionary]) {
        foreach ($key in $Checkpoint.Keys) { $table[[string]$key] = [string]$Checkpoint[$key] }
    }
    elseif ($null -ne $Checkpoint) {
        foreach ($property in $Checkpoint.PSObject.Properties) { $table[$property.Name] = [string]$property.Value }
    }
    $table
}

function Get-InvalidatedCompletedSetupStages {
    param(
        [Parameter(Mandatory)][hashtable] $State,
        [Parameter(Mandatory)][string[]] $Stages,
        [Parameter(Mandatory)][scriptblock] $StageValidator,
        [Parameter(Mandatory)][hashtable] $Context
    )

    foreach ($stage in $Stages) {
        if (-not $State.ContainsKey($stage) -or $State[$stage] -ne 'Complete') { continue }
        if (-not [bool](& $StageValidator $stage $Context)) {
            return @(Get-InvalidatedSetupStages -Stages $Stages -FirstInvalidStage $stage)
        }
    }
    @()
}

function Remove-InvalidatedSetupStages {
    param(
        [Parameter(Mandatory)][hashtable] $State,
        [Parameter(Mandatory)][string[]] $InvalidatedStages
    )

    foreach ($stage in $InvalidatedStages) {
        [void]$State.Remove($stage)
    }
}

function New-InvalidSetupRunnerResult {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]] $InvalidatedStages
    )

    New-SetupResult -Component 'Setup' -Status 'Failed' `
        -Message 'Der Setup-Runner hat kein gültiges Ergebnis geliefert.' `
        -Remediation 'Retry' `
        -Data @{ InvalidatedStages = [string[]]@($InvalidatedStages) }
}

function ConvertTo-StableRepairResult {
    param(
        [AllowNull()][object] $SetupResult,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]] $InvalidatedStages
    )

    $requiredProperties = @('Component', 'Status', 'Message', 'Remediation', 'Data')
    $propertyNames = if ($null -eq $SetupResult) { @() } else { @($SetupResult.PSObject.Properties.Name) }
    foreach ($requiredProperty in $requiredProperties) {
        if ($requiredProperty -notin $propertyNames) {
            return New-InvalidSetupRunnerResult -InvalidatedStages $InvalidatedStages
        }
    }
    if ($SetupResult.Data -isnot [Collections.IDictionary]) {
        return New-InvalidSetupRunnerResult -InvalidatedStages $InvalidatedStages
    }

    $data = @{}
    foreach ($key in $SetupResult.Data.Keys) {
        $data[[string]$key] = $SetupResult.Data[$key]
    }
    $data.InvalidatedStages = [string[]]@($InvalidatedStages)

    try {
        New-SetupResult -Component ([string]$SetupResult.Component) `
            -Status ([string]$SetupResult.Status) `
            -Message ([string]$SetupResult.Message) `
            -Remediation ([string]$SetupResult.Remediation) `
            -Data $data
    }
    catch {
        New-InvalidSetupRunnerResult -InvalidatedStages $InvalidatedStages
    }
}

function Invoke-StableSetupStageAction {
    param(
        [AllowNull()][object] $Action,
        [Parameter(Mandatory)][string] $Root,
        [Parameter(Mandatory)][string] $FailureMessage
    )

    if ($Action -isnot [scriptblock]) {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }

    try {
        $actionOutput = @(& $Action $Root)
    }
    catch {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }

    if ($actionOutput.Count -ne 1) {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }
    $result = $actionOutput[0]
    if ($null -eq $result) {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }
    $statusProperty = $result.PSObject.Properties['Status']
    $messageProperty = $result.PSObject.Properties['Message']
    if ($null -eq $statusProperty -or $null -eq $messageProperty) {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }

    $status = $statusProperty.Value
    $message = $messageProperty.Value
    $allowedStatuses = @('Ready', 'Missing', 'Invalid', 'Failed', 'Skipped', 'RestartRequired')
    if ($status -isnot [string] -or [string]::IsNullOrWhiteSpace($status) -or
        $status -cnotin $allowedStatuses -or
        $message -isnot [string] -or [string]::IsNullOrWhiteSpace($message)) {
        return [pscustomobject]@{ Status = 'Failed'; Message = $FailureMessage }
    }
    $result
}

function Invoke-GuidedSetup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [object] $Checkpoint = @{},
        [scriptblock] $StageRunner,
        [scriptblock] $StageValidator
    )

    $state = ConvertTo-CheckpointTable $Checkpoint
    $stages = @(Get-SetupStages)
    $checkpointPath = Join-Path $Root '.captain-cook/checkpoint.json'
    $context = @{
        Root = $Root
        Checkpoint = $state
        SystemStatusProvider = { param($candidateRoot) Get-CaptainSystemStatus -Root $candidateRoot }
    }
    if ($null -eq $StageValidator) {
        $StageValidator = { param($stage, $stageContext) Test-SetupStage -Stage $stage -Context $stageContext }
    }
    if ($null -eq $StageRunner) {
        $StageRunner = { param($stage, $context) Invoke-DefaultSetupStage -Stage $stage -Context $context }
    }

    $invalidatedStages = @(Get-InvalidatedCompletedSetupStages -State $state -Stages $stages -StageValidator $StageValidator -Context $context)
    if ($invalidatedStages.Count -gt 0) {
        Remove-InvalidatedSetupStages -State $state -InvalidatedStages $invalidatedStages
        Save-SetupCheckpoint -Path $checkpointPath -Stages $state
    }

    foreach ($stage in $stages) {
        if ($state.ContainsKey($stage) -and $state[$stage] -eq 'Complete') { continue }
        $stageResult = & $StageRunner $stage $context
        if ($null -eq $stageResult -or $stageResult.Status -ne 'Complete') {
            $state[$stage] = if ($null -eq $stageResult) { 'Failed' } else { [string]$stageResult.Status }
            Save-SetupCheckpoint -Path $checkpointPath -Stages $state
            $message = if ($null -eq $stageResult) { "$stage hat kein Ergebnis geliefert." } else { [string]$stageResult.Message }
            return New-SetupResult -Component $stage -Status 'Failed' -Message $message -Remediation 'Retry'
        }
        $state[$stage] = 'Complete'
        Save-SetupCheckpoint -Path $checkpointPath -Stages $state
    }
    New-SetupResult -Component 'Setup' -Status 'Ready' -Message 'Captain Cook ist vollständig eingerichtet und verifiziert.' -Remediation 'None'
}

function Repair-CaptainSystem {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $StageValidator,
        [scriptblock] $SetupRunner
    )

    $checkpointPath = Join-Path $Root '.captain-cook/checkpoint.json'
    $state = ConvertTo-CheckpointTable (Get-SetupCheckpoint -Path $checkpointPath)
    $stages = @(Get-SetupStages)
    $context = @{
        Root = $Root
        Checkpoint = $state
        SystemStatusProvider = { param($candidateRoot) Get-CaptainSystemStatus -Root $candidateRoot }
    }
    if ($null -eq $StageValidator) {
        $StageValidator = { param($stage, $stageContext) Test-SetupStage -Stage $stage -Context $stageContext }
    }

    $invalidatedStages = @(Get-InvalidatedCompletedSetupStages -State $state -Stages $stages -StageValidator $StageValidator -Context $context)
    if ($invalidatedStages.Count -gt 0) {
        Remove-InvalidatedSetupStages -State $state -InvalidatedStages $invalidatedStages
        Save-SetupCheckpoint -Path $checkpointPath -Stages $state
    }

    if ($null -eq $SetupRunner) {
        $SetupRunner = {
            param($candidateRoot, $candidateCheckpoint)
            Invoke-GuidedSetup -Root $candidateRoot -Checkpoint $candidateCheckpoint -StageValidator { $true }
        }
    }
    $setupResult = & $SetupRunner $Root $state
    ConvertTo-StableRepairResult -SetupResult $setupResult -InvalidatedStages $invalidatedStages
}

function Initialize-SetupConfiguration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $SecretPathValidator = { param($path) Test-TrackedSecretPath -Path $path }
    )

    $path = Join-Path $Root '.env'
    if (-not (& $SecretPathValidator $path)) {
        return New-SetupResult -Component 'Configuration' -Status 'Failed' -Message 'Die lokale .env-Datei ist nicht sicher von Git ausgeschlossen.' -Remediation 'Manual'
    }
    $values = @{}
    foreach ($entry in (Read-DotEnv -Path (Join-Path $Root '.env.example')).GetEnumerator()) { $values[$entry.Key] = $entry.Value }
    foreach ($entry in (Read-DotEnv -Path $path).GetEnumerator()) { $values[$entry.Key] = $entry.Value }
    foreach ($key in @('MARIADB_PASSWORD', 'MARIADB_ROOT_PASSWORD')) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace([string]$values[$key])) { $values[$key] = New-SetupSecret }
    }
    foreach ($key in @('CAPTAIN_GATEWAY_TOKEN', 'WORKER_GATEWAY_TOKEN')) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace([string]$values[$key])) { $values[$key] = New-SetupSecret }
    }
    if (-not $values.ContainsKey('GATEWAY_PORT') -or [string]::IsNullOrWhiteSpace([string]$values.GATEWAY_PORT)) { $values.GATEWAY_PORT = '8090' }
    if (-not $values.ContainsKey('CAPTAIN_GATEWAY_URL') -or [string]::IsNullOrWhiteSpace([string]$values.CAPTAIN_GATEWAY_URL)) {
        $values.CAPTAIN_GATEWAY_URL = "http://127.0.0.1:$($values.GATEWAY_PORT)"
    }
    if (-not $values.ContainsKey('LEDGER_DSN') -or [string]::IsNullOrWhiteSpace([string]$values.LEDGER_DSN)) {
        $database = if ($values.ContainsKey('MARIADB_DATABASE') -and $values.MARIADB_DATABASE) { [string]$values.MARIADB_DATABASE } else { 'captain_ledger' }
        $user = if ($values.ContainsKey('MARIADB_USER') -and $values.MARIADB_USER) { [string]$values.MARIADB_USER } else { 'captain' }
        $port = if ($values.ContainsKey('MARIADB_PORT') -and $values.MARIADB_PORT) { [string]$values.MARIADB_PORT } else { '3306' }
        $password = [uri]::EscapeDataString([string]$values.MARIADB_PASSWORD)
        $values.LEDGER_DSN = "mariadb://$user`:$password@127.0.0.1:$port/$database"
    }
    if (-not $values.ContainsKey('N8N_MODE') -or $values.N8N_MODE -notin @('owned', 'external')) { $values.N8N_MODE = 'external' }
    Write-DotEnv -Path $path -Values $values
    New-SetupResult -Component 'Configuration' -Status 'Ready' -Message 'Die lokale Konfiguration ist vollständig und sicher gespeichert.' -Remediation 'None' -Data @{ Values = $values }
}

function Get-HermesHome {
    if ($env:HERMES_HOME) { return $env:HERMES_HOME }
    if ($env:LOCALAPPDATA) { return Join-Path $env:LOCALAPPDATA 'hermes' }
    Join-Path $HOME 'AppData/Local/hermes'
}

function Test-ManagedProcess {
    param([string] $MetadataPath)
    if (-not (Test-Path $MetadataPath)) { return $false }
    try {
        $metadata = Get-Content $MetadataPath -Raw | ConvertFrom-Json
        $process = Get-Process -Id $metadata.Id -ErrorAction Stop
        $process.StartTime.ToUniversalTime().Ticks -eq [long]$metadata.StartTimeUtcTicks
    }
    catch { $false }
}

function Start-ManagedProcess {
    param([string] $Name, [string] $FilePath, [string[]] $Arguments, [string] $WorkingDirectory, [string] $RuntimeDirectory, [hashtable] $Environment=@{})
    $metadataPath = Join-Path $RuntimeDirectory "$Name.json"
    if (Test-ManagedProcess $metadataPath) { return }
    New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null
    $parameters = @{
        FilePath = $FilePath; ArgumentList = $Arguments; WorkingDirectory = $WorkingDirectory
        WindowStyle = 'Hidden'; PassThru = $true
        RedirectStandardOutput = (Join-Path $RuntimeDirectory "$Name.out.log")
        RedirectStandardError = (Join-Path $RuntimeDirectory "$Name.err.log")
    }
    if ($Environment.Count -gt 0) { $parameters.Environment = $Environment }
    $process = Start-Process @parameters
    [ordered]@{ Id=$process.Id; StartTimeUtcTicks=$process.StartTime.ToUniversalTime().Ticks; Name=$Name } | ConvertTo-Json | Set-Content $metadataPath -Encoding utf8
}

function Start-MinibookProcesses {
    param([string] $Root, [hashtable] $Configuration)
    if ((Wait-SetupEndpoint -Uri "$($Configuration.MINIBOOK_BACKEND_URL)/health" -TimeoutSeconds 2) -and
        (Wait-SetupEndpoint -Uri "$($Configuration.MINIBOOK_PUBLIC_URL)/api/v1/version" -TimeoutSeconds 2)) {
        return
    }
    $runtime = Join-Path $Root '.captain-cook/runtime'
    $minibook = Join-Path $Root 'minibook'
    Start-ManagedProcess -Name 'minibook-backend' -FilePath (Join-Path $minibook '.venv/Scripts/python.exe') -Arguments @('run.py') -WorkingDirectory $minibook -RuntimeDirectory $runtime
    Start-ManagedProcess -Name 'minibook-frontend' -FilePath 'npm.cmd' -Arguments @('start') -WorkingDirectory (Join-Path $minibook 'frontend') -RuntimeDirectory $runtime -Environment @{
        PORT = '3457'; BACKEND_URL = [string]$Configuration.MINIBOOK_BACKEND_URL; NEXT_PUBLIC_BASE_URL = [string]$Configuration.MINIBOOK_PUBLIC_URL
    }
}

function Start-CaptainGateway {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Root, [Parameter(Mandatory)][hashtable] $Configuration)

    $python = Join-Path $Root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        return New-SetupResult -Component 'Captain Gateway' -Status 'Failed' -Message 'Der Captain-Python-Interpreter fehlt.' -Remediation 'Retry'
    }
    foreach ($key in @('LEDGER_DSN', 'CAPTAIN_GATEWAY_TOKEN', 'WORKER_GATEWAY_TOKEN', 'CAPTAIN_GATEWAY_URL', 'GATEWAY_PORT')) {
        if (-not $Configuration.ContainsKey($key) -or [string]::IsNullOrWhiteSpace([string]$Configuration[$key])) {
            return New-SetupResult -Component 'Captain Gateway' -Status 'Failed' -Message "Die lokale Gateway-Konfiguration $key fehlt." -Remediation 'Configure'
        }
    }
    $runtime = Join-Path $Root '.captain-cook/runtime'
    $environment = @{}
    foreach ($key in @('LEDGER_DSN', 'CAPTAIN_GATEWAY_TOKEN', 'WORKER_GATEWAY_TOKEN', 'GATEWAY_PORT', 'GATEWAY_APPROVAL_ENABLED', 'GATEWAY_CLAIM_TTL_SECONDS')) {
        if ($Configuration.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace([string]$Configuration[$key])) { $environment[$key] = [string]$Configuration[$key] }
    }
    Start-ManagedProcess -Name 'captain-gateway' -FilePath $python -Arguments @('-m', 'gateway.app') -WorkingDirectory $Root -RuntimeDirectory $runtime -Environment $environment
    if (-not (Wait-SetupEndpoint -Uri "$([string]$Configuration.CAPTAIN_GATEWAY_URL.TrimEnd('/'))/healthz" -TimeoutSeconds 30)) {
        return New-SetupResult -Component 'Captain Gateway' -Status 'Failed' -Message 'Captain Gateway wurde nicht gesund.' -Remediation 'Retry'
    }
    New-SetupResult -Component 'Captain Gateway' -Status 'Ready' -Message 'Captain Gateway ist gesund und lifecycle-authoritativ.' -Remediation 'None'
}

function Stop-CaptainGateway {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Root)

    $path = Join-Path $Root '.captain-cook/runtime/captain-gateway.json'
    if (Test-ManagedProcess $path) {
        $metadata = Get-Content $path -Raw | ConvertFrom-Json
        Stop-Process -Id $metadata.Id -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Invoke-CaptainGatewayRecovery {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Root, [Parameter(Mandatory)][hashtable] $Configuration)

    $python = Join-Path $Root '.venv/Scripts/python.exe'
    $previousToken = $env:CAPTAIN_GATEWAY_TOKEN
    try {
        $env:CAPTAIN_GATEWAY_TOKEN = [string]$Configuration.CAPTAIN_GATEWAY_TOKEN
        & $python (Join-Path $Root 'main.py') 'recover-gateway' '--gateway-url' ([string]$Configuration.CAPTAIN_GATEWAY_URL) *> $null
        if ($LASTEXITCODE -ne 0) {
            return New-SetupResult -Component 'Captain Recovery' -Status 'Failed' -Message 'Captain Gateway-Recovery konnte nicht sicher ausgeführt werden.' -Remediation 'Retry'
        }
    }
    finally {
        if ($null -eq $previousToken) { Remove-Item Env:CAPTAIN_GATEWAY_TOKEN -ErrorAction SilentlyContinue }
        else { $env:CAPTAIN_GATEWAY_TOKEN = $previousToken }
    }
    New-SetupResult -Component 'Captain Recovery' -Status 'Ready' -Message 'Captain-Recovery-Pass wurde fail-closed ausgeführt.' -Remediation 'None'
}

function Stop-MinibookProcesses {
    param([string] $Root)
    $runtime = Join-Path $Root '.captain-cook/runtime'
    foreach ($name in @('minibook-backend', 'minibook-frontend')) {
        $path = Join-Path $runtime "$name.json"
        if (Test-ManagedProcess $path) {
            $metadata = Get-Content $path -Raw | ConvertFrom-Json
            Stop-Process -Id $metadata.Id -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function Wait-SetupEndpoint {
    param([uri] $Uri, [int] $TimeoutSeconds=60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try { if ((Invoke-WebRequest -Uri $Uri -TimeoutSec 5 -UseBasicParsing).StatusCode -lt 400) { return $true } } catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    $false
}

function Initialize-MinibookIdentity {
    param([string] $Root, [hashtable] $Configuration)
    $baseUrl = [string]$Configuration.MINIBOOK_PUBLIC_URL
    $hermesHome = Get-HermesHome
    $hermesEnv = Read-DotEnv -Path (Join-Path $hermesHome '.env')
    $existingKey = if ($hermesEnv.ContainsKey('MINIBOOK_API_KEY')) { [string]$hermesEnv.MINIBOOK_API_KEY } else { '' }
    $headers = if ($existingKey) { @{ Authorization = "Bearer $existingKey" } } else { @{} }
    $isValid = try { -not [string]::IsNullOrWhiteSpace([string](Invoke-RestMethod -Uri "$baseUrl/api/v1/agents/me" -Headers $headers -TimeoutSec 10).name) } catch { $false }
    if (-not $isValid) {
        $agentName = Get-AvailableMinibookAgentName -BaseUrl $baseUrl
        try { $identity = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/agents" -ContentType 'application/json' -Body (@{name=$agentName} | ConvertTo-Json) -TimeoutSec 10 }
        catch { return New-SetupResult -Component 'Hermes Identity' -Status 'Failed' -Message "Hermes konnte nicht bei Minibook registriert werden: $($_.Exception.Message)" -Remediation 'Retry' }
        Save-HermesMinibookCredential -HermesHome $hermesHome -BaseUrl $baseUrl -ApiKey $identity.api_key | Out-Null
    }
    Install-MinibookSkill -Source (Join-Path $Root 'minibook/skills/minibook/SKILL.md') -DestinationDirectory (Join-Path $hermesHome 'skills/minibook')
}

function Get-SetupPreflightConfiguration {
    param(
        [Parameter(Mandatory)][string] $Root,
        [AllowNull()][object] $Configuration
    )

    $allowedKeys = @(
        'MINIBOOK_BACKEND_URL', 'MINIBOOK_PUBLIC_URL',
        'MAILPIT_WEB_PORT', 'MAILPIT_SMTP_PORT', 'MARIADB_PORT',
        'N8N_MODE', 'N8N_URL'
    )
    $selected = @{}
    $sources = if ($null -ne $Configuration) {
        @($Configuration)
    }
    else {
        @(
            Read-DotEnv -Path (Join-Path $Root '.env.example')
            Read-DotEnv -Path (Join-Path $Root '.env')
        )
    }
    foreach ($source in $sources) {
        if ($source -isnot [Collections.IDictionary]) {
            throw 'Die Preflight-Konfiguration hat kein gültiges Format.'
        }
        foreach ($key in $allowedKeys) {
            if ($source.Contains($key)) { $selected[$key] = [string]$source[$key] }
        }
    }
    $selected
}

function Invoke-DefaultSetupStage {
    param([string] $Stage, [hashtable] $Context)
    $root = [string]$Context.Root
    switch ($Stage) {
        'Preflight' {
            try {
                $configurationSource = if ($Context.ContainsKey('Configuration')) {
                    $Context.Configuration
                }
                elseif ($Context.ContainsKey('PreflightConfigurationProvider') -and
                    $Context.PreflightConfigurationProvider -is [scriptblock]) {
                    & $Context.PreflightConfigurationProvider $root
                }
                else {
                    $null
                }
                $configuration = Get-SetupPreflightConfiguration -Root $root -Configuration $configurationSource
            }
            catch {
                return [pscustomobject]@{
                    Status = 'Failed'
                    Message = 'Die nicht geheime Preflight-Konfiguration konnte nicht geladen werden.'
                }
            }
            $parameters = @{ Root = $root; Configuration = $configuration }
            if ($Context.ContainsKey('PreflightResultProvider')) {
                $parameters.ResultProvider = $Context.PreflightResultProvider
            }
            $result = Test-SetupPreflight @parameters
            if ($result.Status -cne 'Ready') {
                return [pscustomobject]@{ Status = 'Failed'; Message = $result.Message }
            }
        }
        'Configuration' {
            $result = Initialize-SetupConfiguration -Root $root
            if ($result.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} }
        }
        'Captain' { $result = Install-Captain -Root $root; if ($result.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} } }
        'Hermes' {
            $submoduleInitializer = if ($Context.ContainsKey('SubmoduleInitializer')) {
                $Context.SubmoduleInitializer
            }
            else {
                { param($candidateRoot) Initialize-SetupSubmodules -Root $candidateRoot }
            }
            $hermesInstaller = if ($Context.ContainsKey('HermesInstaller')) {
                $Context.HermesInstaller
            }
            else {
                { param($candidateRoot) Install-Hermes -Root $candidateRoot }
            }

            $result = Invoke-StableSetupStageAction -Action $submoduleInitializer -Root $root `
                -FailureMessage 'Die Repository-Initialisierung hat kein gültiges Ergebnis geliefert.'
            if ($result.Status -cne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} }
            $result = Invoke-StableSetupStageAction -Action $hermesInstaller -Root $root `
                -FailureMessage 'Die Hermes-Installation hat kein gültiges Ergebnis geliefert.'
            if ($result.Status -cne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} }
        }
        'Minibook' {
            $result = Install-Minibook -Root $root
            if ($result.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} }
            $config = Read-DotEnv -Path (Join-Path $root '.env')
            Initialize-MinibookConfiguration -Root $root -BackendUrl $config.MINIBOOK_BACKEND_URL -PublicUrl $config.MINIBOOK_PUBLIC_URL | Out-Null
            Start-MinibookProcesses -Root $root -Configuration $config
            if (-not (Wait-SetupEndpoint -Uri "$($config.MINIBOOK_BACKEND_URL)/health")) { return [pscustomobject]@{Status='Failed';Message='Minibook wurde gestartet, antwortet aber nicht.'} }
            $identity = Initialize-MinibookIdentity -Root $root -Configuration $config
            if ($identity.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$identity.Message} }
        }
        'Services' {
            $config = Read-DotEnv -Path (Join-Path $root '.env')
            $mode = if ($config.N8N_MODE -eq 'external') { 'External' } else { 'Owned' }
            $result = Start-CaptainServices -Root $root -N8nMode $mode
            if ($result.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$result.Message} }
        }
        'Verification' {
            $status = Get-CaptainSystemStatus -Root $root
            if ($status.Status -ne 'Ready') { return [pscustomobject]@{Status='Failed';Message=$status.Message} }
        }
    }
    [pscustomobject]@{ Status='Complete'; Message="$Stage ist vollständig." }
}

function Get-CaptainSystemStatus {
    [CmdletBinding()]
    param([string] $Root, [scriptblock[]] $HealthProbes)
    if ($null -eq $HealthProbes) {
        $config = Read-DotEnv -Path (Join-Path $Root '.env')
        $HealthProbes = @(
            { if (Test-Path (Join-Path $Root '.captain-cook/demo-run.json')) { New-SetupResult 'Captain' Ready 'Offline-Demo verifiziert.' None } else { New-SetupResult 'Captain' Failed 'Offline-Demo fehlt.' Retry } },
            { Test-HttpService -Name 'Captain Gateway' -Uri "$([string]$config.CAPTAIN_GATEWAY_URL.TrimEnd('/'))/healthz" },
            { Test-HttpService -Name 'Minibook' -Uri "$($config.MINIBOOK_BACKEND_URL)/health" },
            { Test-HttpService -Name 'Mailpit' -Uri "http://localhost:$($config.MAILPIT_WEB_PORT)/api/v1/info" },
            { Test-HttpService -Name 'n8n' -Uri "$([string]$config.N8N_URL.TrimEnd('/'))/healthz" }
        )
    }
    $results = @($HealthProbes | ForEach-Object { & $_ })
    $failed = @($results | Where-Object Status -ne 'Ready')
    if ($failed.Count) { return New-SetupResult -Component 'System' -Status 'Failed' -Message ($failed.Message -join ' ') -Remediation 'Retry' -Data @{Results=$results} }
    New-SetupResult -Component 'System' -Status 'Ready' -Message 'Alle geprüften Komponenten sind gesund.' -Remediation 'None' -Data @{Results=$results}
}

function Start-CaptainSystem {
    [CmdletBinding()]
    param([string] $Root)
    $config = Read-DotEnv -Path (Join-Path $Root '.env')
    $captain = Install-Captain -Root $Root
    if ($captain.Status -ne 'Ready') { return $captain }
    $services = Start-CaptainServices -Root $Root -N8nMode $(if ($config.N8N_MODE -eq 'external') {'External'} else {'Owned'})
    if ($services.Status -ne 'Ready') { return $services }
    $gateway = Start-CaptainGateway -Root $Root -Configuration $config
    if ($gateway.Status -ne 'Ready') { return $gateway }
    $recovery = Invoke-CaptainGatewayRecovery -Root $Root -Configuration $config
    if ($recovery.Status -ne 'Ready') { return $recovery }
    Start-MinibookProcesses -Root $Root -Configuration $config
    New-SetupResult -Component 'System' -Status 'Ready' -Message 'Captain, Gateway-Recovery und lokale Dienste sind gestartet.' -Remediation 'None'
}

function Stop-CaptainSystem {
    [CmdletBinding()]
    param([string] $Root)
    $config = Read-DotEnv -Path (Join-Path $Root '.env')
    Stop-MinibookProcesses -Root $Root
    Stop-CaptainGateway -Root $Root
    Stop-CaptainServices -Root $Root -N8nMode $(if ($config.N8N_MODE -eq 'external') {'External'} else {'Owned'})
}

Export-ModuleMember -Function @('Get-SetupStages','Invoke-GuidedSetup','Repair-CaptainSystem','Invoke-DefaultSetupStage','Initialize-SetupConfiguration','Get-CaptainSystemStatus','Start-CaptainSystem','Stop-CaptainSystem','Start-CaptainGateway','Stop-CaptainGateway','Invoke-CaptainGatewayRecovery')
