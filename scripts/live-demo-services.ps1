#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "health", "stop")]
    [string]$Action,
    [switch]$RecoverDemoCredentials,
    [string]$CredentialSourceEnv
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$rootEnv = Join-Path $root '.env'
$n8nEnv = Join-Path $root '.env.captain-n8n'
$testCompose = Join-Path $root 'docker-compose.test.yml'
$stateDir = Join-Path $root '.captain-cook'
$gatewayPid = Join-Path $stateDir 'gateway-demo.pid'
$runtimePid = Join-Path $stateDir 'runtime-demo.pid'
$evidence = Join-Path $stateDir 'evidence/live-demo-services.json'
$project = 'captain-cook-live-demo'

function Read-Env([string]$Path, [string[]]$AllowedNames) {
    $values = [ordered]@{}
    if (Test-Path $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw "Invalid local environment line in $([IO.Path]::GetFileName($Path))." }
        $name = $Matches[1]
        if ($AllowedNames -notcontains $name) { continue }
        if ($values.Contains($name)) { throw "Duplicate local environment key: $name" }
        $values[$name] = $Matches[2]
    }}
    $values
}
function Save-Env($Values, [string]$Path) {
    $pending = [ordered]@{}; foreach ($item in $Values.GetEnumerator()) { $pending[$item.Key] = $item.Value }
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=') {
            $name = $Matches[1]
            if ($pending.Contains($name)) { $lines.Add(('{0}={1}' -f $name,$pending[$name])); $pending.Remove($name); continue }
        }
        $lines.Add($line)
    }}
    foreach ($item in $pending.GetEnumerator()) { $lines.Add(('{0}={1}' -f $item.Key,$item.Value)) }
    [IO.File]::WriteAllLines($Path, $lines, [Text.UTF8Encoding]::new($false))
}
function New-Secret([int]$Bytes=32) {
    $buffer = [byte[]]::new($Bytes)
    [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+','-').Replace('/','_')
}
function Set-Missing($Values, [string]$Name, [scriptblock]$Factory) {
    if (-not $Values.Contains($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) { $Values[$Name] = & $Factory }
}
function Set-ProductionAdapterManifests($Values) {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $generator = Join-Path $root 'scripts/generate-capability-adapter-manifest.py'
    $output = Join-Path $root '.captain-cook/adapters'
    if (-not (Test-Path $python -PathType Leaf) -or -not (Test-Path $generator -PathType Leaf)) {
        throw 'Adapter manifest generation requires the project Python runtime and generator.'
    }
    New-Item -ItemType Directory -Force $output | Out-Null
    $specifications = @(
        @{ Module='agenten/agent_factory/production_adapter_bundle.py'; Symbol='build_capability_factory_entrypoint'; Target='capability-entrypoint.manifest.json'; Kind='entrypoint'; Manifest='CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST'; Digest='CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256' },
        @{ Module='agenten/agent_factory/factory_live_paid_ports.py'; Symbol='build_factory_live_runtime'; Target='factory-live-runtime.manifest.json'; Kind='factory_live_runtime'; Manifest='FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST'; Digest='FACTORY_LIVE_RUNTIME_ADAPTER_SHA256' }
    )
    foreach ($specification in $specifications) {
        $target = Join-Path $output $specification.Target
        $raw = & $python $generator --workspace-root $root --module (Join-Path $root $specification.Module) --factory-symbol $specification.Symbol --target $target --kind $specification.Kind
        if ($LASTEXITCODE -ne 0) { throw 'Production adapter manifest generation failed.' }
        try { $report = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'Production adapter manifest generator returned invalid evidence.' }
        if ([string]::IsNullOrWhiteSpace([string]$report.manifest_sha256)) { throw 'Production adapter manifest digest is missing.' }
        $Values[$specification.Manifest] = [IO.Path]::GetFullPath([string]$report.manifest_path)
        $Values[$specification.Digest] = [string]$report.manifest_sha256
    }
    Write-Host '[ready] production adapter manifests regenerated (digests redacted)'
}
function Initialize-LocalEnvironment {
    $allowed = @('MARIADB_PASSWORD','MARIADB_ROOT_PASSWORD','MARIADB_TEST_PASSWORD','MARIADB_TEST_ROOT_PASSWORD','CAPTAIN_GATEWAY_TOKEN','WORKER_GATEWAY_TOKEN','CAPTAIN_RUNTIME_TOKEN','CAPTAIN_RUNTIME_EVIDENCE_MODE','MARIADB_TEST_PORT','GATEWAY_PORT','CAPTAIN_RUNTIME_PORT','TEST_MARIADB_DSN','LEDGER_DSN','CAPTAIN_GATEWAY_URL','CAPTAIN_RUNTIME_URL','MINIBOOK_API_KEY','MINIBOOK_PROJECTION_API_KEY','CAPTAIN_DEMO_MINIBOOK_API_KEY','MINIBOOK_CREATION_DB','MINIBOOK_CREATION_ARTIFACTS','CAPTAIN_CAPABILITY_SANDBOX_IMAGE','N8N_MODE','N8N_API_KEY','N8N_MCP_TOKEN','CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET','CAPTAIN_N8N_URL','CAPTAIN_N8N_BATCH_ID','CAPTAIN_N8N_PROJECT_ID','CAPTAIN_N8N_WORKSPACE_REF','CAPTAIN_FACTORY_N8N_WORKFLOW_ID','OPENAI_API_KEY','OPENAI_MODEL','LLM_PROVIDER','CONTEXT7_API_KEY','HERMES_EXECUTABLE','CODEX_EXECUTABLE','CAPTAIN_RUNTIME_ARTIFACT_ROOT','CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST','CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256','FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST','FACTORY_LIVE_RUNTIME_ADAPTER_SHA256','CAPTAIN_FACTORY_JOB_ID','CAPTAIN_FACTORY_SKILL_ROOT','CAPTAIN_FACTORY_WORKSPACE_REF','CAPTAIN_FACTORY_PROVIDER','CAPTAIN_FACTORY_HERMES_PROVIDER','CAPTAIN_FACTORY_HERMES_MODEL','CAPTAIN_FACTORY_MODEL','CAPTAIN_FACTORY_MAX_COST_USD','CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD','CAPTAIN_FACTORY_RUNTIME_SECONDS','CAPTAIN_FACTORY_PRICING_VERSION','CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT','CAPTAIN_FACTORY_PRICING_INPUT_COST_PER_MILLION_USD','CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD','CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD')
    $values = Read-Env $rootEnv $allowed
    Set-Missing $values 'MARIADB_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'CAPTAIN_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'WORKER_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'CAPTAIN_RUNTIME_TOKEN' { New-Secret }
    Set-Missing $values 'MINIBOOK_PROJECTION_API_KEY' { New-Secret }
    if (-not $values.Contains('MINIBOOK_API_KEY') -and $values.Contains('CAPTAIN_DEMO_MINIBOOK_API_KEY')) {
        $values['MINIBOOK_API_KEY'] = $values['CAPTAIN_DEMO_MINIBOOK_API_KEY']
    }
    if ($values.Contains('MINIBOOK_API_KEY') -and $values.Contains('CAPTAIN_DEMO_MINIBOOK_API_KEY') -and $values['MINIBOOK_API_KEY'] -ne $values['CAPTAIN_DEMO_MINIBOOK_API_KEY']) {
        throw 'Configured Minibook API key aliases do not match; refusing to choose one.'
    }
    Set-Missing $values 'MARIADB_TEST_PORT' { '33306' }
    Set-Missing $values 'GATEWAY_PORT' { '8090' }
    Set-Missing $values 'CAPTAIN_RUNTIME_PORT' { '8091' }
    Set-Missing $values 'CAPTAIN_RUNTIME_EVIDENCE_MODE' { 'production-v3' }
    Set-Missing $values 'N8N_MODE' { 'captain-builder' }
    Set-Missing $values 'HERMES_EXECUTABLE' { 'hermes' }
    Set-Missing $values 'CODEX_EXECUTABLE' { 'codex' }
    Set-Missing $values 'LLM_PROVIDER' { 'openai' }
    Set-Missing $values 'OPENAI_MODEL' { 'gpt-4o-mini' }
    Set-Missing $values 'CAPTAIN_FACTORY_SKILL_ROOT' { [IO.Path]::GetFullPath((Join-Path $root 'agenten/agent_factory/skills')) }
    Set-Missing $values 'CAPTAIN_FACTORY_WORKSPACE_REF' { 'workspace://captain-cook/live-demo' }
    Set-Missing $values 'CAPTAIN_FACTORY_PROVIDER' { 'openai' }
    Set-Missing $values 'CAPTAIN_FACTORY_HERMES_PROVIDER' { 'openai-api' }
    Set-Missing $values 'CAPTAIN_FACTORY_HERMES_MODEL' { 'gpt-5.6-terra' }
    Set-Missing $values 'CAPTAIN_FACTORY_MODEL' { 'gpt-4o-mini' }
    Set-Missing $values 'CAPTAIN_FACTORY_MAX_COST_USD' { '1.00' }
    Set-Missing $values 'CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD' { '0.25' }
    Set-Missing $values 'CAPTAIN_FACTORY_RUNTIME_SECONDS' { '600' }
    Set-Missing $values 'CAPTAIN_FACTORY_PRICING_VERSION' { 'openai-gpt-4o-mini-2026-07-21' }
    Set-Missing $values 'CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT' { '2026-07-21T00:00:00Z' }
    Set-Missing $values 'CAPTAIN_FACTORY_PRICING_INPUT_COST_PER_MILLION_USD' { '0.15' }
    Set-Missing $values 'CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD' { '0.60' }
    Set-Missing $values 'CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD' { '0.000001' }
    Set-Missing $values 'CAPTAIN_N8N_BATCH_ID' { 'factory-live-demo-n8n' }
    Set-Missing $values 'CAPTAIN_N8N_PROJECT_ID' { 'captain-cook-live-demo' }
    Set-Missing $values 'CAPTAIN_N8N_WORKSPACE_REF' { 'workspace://captain-cook/live-demo/n8n' }
    Set-Missing $values 'CAPTAIN_FACTORY_N8N_WORKFLOW_ID' { 'uROkVuVjYGnw8Dfm' }
    Set-Missing $values 'CAPTAIN_RUNTIME_ARTIFACT_ROOT' { [IO.Path]::GetFullPath((Join-Path $root 'artifacts/capability-factory')) }
    Set-Missing $values 'MINIBOOK_CREATION_ARTIFACTS' { [string]$values['CAPTAIN_RUNTIME_ARTIFACT_ROOT'] }
    Set-Missing $values 'MINIBOOK_CREATION_DB' { [IO.Path]::GetFullPath((Join-Path $root '.captain-cook/minibook-creation.sqlite3')) }
    $runtimeArtifactValue = [string]$values['CAPTAIN_RUNTIME_ARTIFACT_ROOT']
    $runtimeArtifactRoot = if ([IO.Path]::IsPathFullyQualified($runtimeArtifactValue)) {
        [IO.Path]::GetFullPath($runtimeArtifactValue)
    } else { [IO.Path]::GetFullPath((Join-Path $root $runtimeArtifactValue)) }
    $minibookArtifactValue = [string]$values['MINIBOOK_CREATION_ARTIFACTS']
    $minibookArtifactRoot = if ([IO.Path]::IsPathFullyQualified($minibookArtifactValue)) {
        [IO.Path]::GetFullPath($minibookArtifactValue)
    } else { [IO.Path]::GetFullPath((Join-Path $root $minibookArtifactValue)) }
    if ($runtimeArtifactRoot -ne $minibookArtifactRoot) {
        throw 'Captain Runtime and Minibook capability artifact roots differ.'
    }
    Set-ProductionAdapterManifests $values
    $escapedPassword = [Uri]::EscapeDataString([string]$values['MARIADB_TEST_PASSWORD'])
    $values['TEST_MARIADB_DSN'] = "mariadb://captain_test:${escapedPassword}@127.0.0.1:$($values['MARIADB_TEST_PORT'])/captain_test"
    $values['LEDGER_DSN'] = $values['TEST_MARIADB_DSN']
    $values['CAPTAIN_GATEWAY_URL'] = "http://127.0.0.1:$($values['GATEWAY_PORT'])"
    $values['CAPTAIN_RUNTIME_URL'] = "http://127.0.0.1:$($values['CAPTAIN_RUNTIME_PORT'])"
    Save-Env $values $rootEnv
    Write-Host '[ready] local Gateway/captain_test settings initialized (values redacted)'
    $values
}
function Sync-CaptainN8nEnvironment($Values) {
    if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n credential file is missing after bootstrap.' }
    $source = Read-Env $n8nEnv @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET')
    foreach ($name in @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET')) {
        if (-not $source.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$source[$name])) { throw "Captain n8n bootstrap did not provide $name." }
        $Values[$name] = $source[$name]
    }
    $Values['CAPTAIN_N8N_URL'] = "http://127.0.0.1:$($Values['CAPTAIN_N8N_PORT'])"
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n credentials inherited (values redacted)'
}
function Start-CaptainN8nBroker($Values) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
    $previousGatewayUrl = [Environment]::GetEnvironmentVariable('CAPTAIN_GATEWAY_URL','Process')
    try {
        [Environment]::SetEnvironmentVariable('CAPTAIN_GATEWAY_URL',"http://host.docker.internal:$($Values['GATEWAY_PORT'])",'Process')
        [Environment]::SetEnvironmentVariable('CAPTAIN_GATEWAY_TOKEN',[string]$Values['CAPTAIN_GATEWAY_TOKEN'],'Process')
        & $n8n broker-start
        if ($LASTEXITCODE -ne 0) { throw 'Captain n8n MCP broker failed to start.' }
    } finally {
        [Environment]::SetEnvironmentVariable('CAPTAIN_GATEWAY_URL',$previousGatewayUrl,'Process')
    }
    Write-Host '[ready] Captain n8n MCP broker bound to Gateway authority'
}
function Set-ProcessEnvironment($Values) {
    foreach ($item in $Values.GetEnumerator()) { [Environment]::SetEnvironmentVariable([string]$item.Key,[string]$item.Value,'Process') }
}
function Repair-CaptainN8nPersistenceForRecovery {
    if (-not (Test-Path $n8nEnv -PathType Leaf)) { return }
    $volumeName = 'captain-n8n-builder_captain_n8n_data'
    & docker volume inspect $volumeName *> $null
    if ($LASTEXITCODE -ne 0) { return }
    $rawConfig = & docker run --rm --entrypoint cat -v "${volumeName}:/data:ro" n8nio/n8n:2.29.10 /data/config 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$rawConfig)) { throw 'Existing Captain n8n config could not be inspected for safe recovery.' }
    try { $persistedKey = [string](($rawConfig | ConvertFrom-Json).encryptionKey) } catch { throw 'Existing Captain n8n config is invalid.' }
    if ([string]::IsNullOrWhiteSpace($persistedKey)) { throw 'Existing Captain n8n config has no encryption key.' }
    $builderKeys = @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_ENCRYPTION_KEY','CAPTAIN_N8N_POSTGRES_PASSWORD','CAPTAIN_N8N_POSTGRES_USER','CAPTAIN_N8N_POSTGRES_DB','CAPTAIN_N8N_OWNER_PASSWORD','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET')
    $builderValues = Read-Env $n8nEnv $builderKeys
    if ($builderValues.Contains('CAPTAIN_N8N_ENCRYPTION_KEY') -and [string]$builderValues['CAPTAIN_N8N_ENCRYPTION_KEY'] -eq $persistedKey) { return }
    $builderValues['CAPTAIN_N8N_ENCRYPTION_KEY'] = $persistedKey
    foreach ($required in @('CAPTAIN_N8N_POSTGRES_PASSWORD','CAPTAIN_N8N_POSTGRES_USER','CAPTAIN_N8N_POSTGRES_DB')) {
        if (-not $builderValues.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$builderValues[$required])) { throw "Safe Captain n8n recovery requires $required." }
    }
    $databaseUser = [string]$builderValues['CAPTAIN_N8N_POSTGRES_USER']
    $databaseName = [string]$builderValues['CAPTAIN_N8N_POSTGRES_DB']
    if ($databaseUser -notmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or $databaseName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Captain n8n database identifiers are unsafe.' }
    $postgresContainer = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=postgres' --format '{{.ID}}') | Select-Object -First 1
    if (-not $postgresContainer) { throw 'Captain n8n Postgres must be running for credential recovery.' }
    $escapedPassword = ([string]$builderValues['CAPTAIN_N8N_POSTGRES_PASSWORD']).Replace("'", "''")
    "ALTER ROLE `"$databaseUser`" WITH PASSWORD '$escapedPassword';" | & docker exec -i $postgresContainer psql -v ON_ERROR_STOP=1 -U $databaseUser -d $databaseName *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Captain n8n Postgres credential synchronization failed.' }
    Save-Env $builderValues $n8nEnv
    & docker compose -p captain-n8n-builder --env-file $n8nEnv -f (Join-Path $root 'docker-compose.captain-n8n.yml') up -d --force-recreate n8n *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Captain n8n could not be recreated with its persisted encryption key.' }
    Write-Host '[ready] Captain n8n persisted encryption and database credentials recovered (values redacted)'
}
function Recover-CaptainN8nEnvironment($Values, [string]$SourceEnv) {
    if ($SourceEnv -and [IO.Path]::GetFullPath($SourceEnv) -ne [IO.Path]::GetFullPath($rootEnv)) {
        if (-not (Test-Path $SourceEnv -PathType Leaf)) { throw 'The explicit credential source .env does not exist.' }
        $sourceAliases = Read-Env $SourceEnv @('N8N_API_KEY','N8N_MCP_TOKEN')
        foreach ($name in @('N8N_API_KEY','N8N_MCP_TOKEN')) {
            if ($sourceAliases.Contains($name)) { $Values[$name] = $sourceAliases[$name] }
        }
    }
    foreach ($name in @('N8N_API_KEY','N8N_MCP_TOKEN')) {
        if (-not $Values.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$Values[$name])) { throw "Recovery requires a non-empty $name in the root .env." }
    }
    $baseUrl = 'http://127.0.0.1:5679'
    $authenticated = $false
    foreach ($attempt in 1..60) { try {
        $rest = Invoke-WebRequest "$baseUrl/api/v1/workflows?limit=1" -Headers @{'X-N8N-API-KEY'=[string]$Values['N8N_API_KEY']} -UseBasicParsing -TimeoutSec 5
        $body = '{"jsonrpc":"2.0","id":"credential-recovery","method":"tools/list","params":{}}'
        $mcp = Invoke-WebRequest "$baseUrl/mcp-server/http" -Method Post -Headers @{Authorization="Bearer $([string]$Values['N8N_MCP_TOKEN'])";Accept='application/json, text/event-stream'} -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 5
        if ($rest.StatusCode -eq 200 -and $mcp.StatusCode -eq 200) { $authenticated=$true; break }
    } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $authenticated) { throw 'Captain n8n recovery credentials failed REST or MCP authentication.' }
    $builderKeys = @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_ENCRYPTION_KEY','CAPTAIN_N8N_POSTGRES_PASSWORD','CAPTAIN_N8N_POSTGRES_USER','CAPTAIN_N8N_POSTGRES_DB','CAPTAIN_N8N_OWNER_PASSWORD','CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET')
    $builderValues = Read-Env $n8nEnv $builderKeys
    $signingSecret = if ($builderValues.Contains('CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET') -and -not [string]::IsNullOrWhiteSpace([string]$builderValues['CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET'])) {
        [string]$builderValues['CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET']
    } else { New-Secret }
    $recovered = [ordered]@{
        CAPTAIN_N8N_PORT='5679'; CAPTAIN_N8N_API_KEY=[string]$Values['N8N_API_KEY']; CAPTAIN_N8N_MCP_TOKEN=[string]$Values['N8N_MCP_TOKEN']; CAPTAIN_N8N_MCP_BROKER_URL='http://127.0.0.1:5680'; CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET=$signingSecret
    }
    foreach ($name in @('CAPTAIN_N8N_ENCRYPTION_KEY','CAPTAIN_N8N_POSTGRES_PASSWORD','CAPTAIN_N8N_POSTGRES_USER','CAPTAIN_N8N_POSTGRES_DB','CAPTAIN_N8N_OWNER_PASSWORD')) {
        if ($builderValues.Contains($name) -and -not [string]::IsNullOrWhiteSpace([string]$builderValues[$name])) { $recovered[$name] = [string]$builderValues[$name] }
    }
    Save-Env $recovered $n8nEnv
    foreach ($item in $recovered.GetEnumerator()) { $Values[$item.Key] = $item.Value }
    $Values['CAPTAIN_N8N_URL'] = $baseUrl
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n demo credentials recovered after REST/MCP verification (values redacted)'
}
function Initialize-CaptainN8n($Values, [switch]$Recover, [string]$SourceEnv) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
    if ($Recover) { Repair-CaptainN8nPersistenceForRecovery }
    $running = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=n8n' --format '{{.ID}}')
    $existing = @(& docker ps -a --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=n8n' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Captain n8n project.' }
    if ($Recover -and $existing.Count -eq 0) { throw 'No existing Captain n8n builder is available for credential recovery.' }
    if ($Recover -and $running.Count -eq 0) {
        foreach ($service in @('postgres','n8n','mcp-broker')) {
            $containers = @(& docker ps -a --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter "label=com.docker.compose.service=$service" --format '{{.ID}}')
            if ($containers.Count -gt 0) { & docker start @containers *> $null; if ($LASTEXITCODE -ne 0) { throw "Could not restart Captain n8n service $service." } }
        }
        Recover-CaptainN8nEnvironment $Values $SourceEnv
        return
    }
    if ($running.Count -gt 0) {
        if ($Recover) { Recover-CaptainN8nEnvironment $Values $SourceEnv; return }
        if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n is running, but .env.captain-n8n is missing. Use -RecoverDemoCredentials only with already validated local demo aliases.' }
        & $n8n bootstrap
    } else {
        & $n8n init
        & $n8n start
        & $n8n bootstrap
    }
    Sync-CaptainN8nEnvironment $Values
}
function Stop-CaptainN8nContainers {
    $containers = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Captain n8n containers.' }
    if ($containers.Count -gt 0) { & docker stop @containers *> $null; if ($LASTEXITCODE -ne 0) { throw 'Captain n8n containers could not be stopped.' } }
    Write-Host '[ready] labelled Captain n8n containers stopped; volumes preserved'
}
function Start-Gateway($Values) {
    $gatewayHealthy = $false
    try { $gatewayHealthy = (Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch {}
    if ($gatewayHealthy) {
        if (-not (Test-Path $gatewayPid -PathType Leaf)) { throw 'Healthy Gateway endpoint is not the managed demo process.' }
        Stop-ManagedGateway
        Write-Host '[ready] managed Gateway restarted for current configuration'
    }
    $gatewayPort = [Uri]$Values['CAPTAIN_GATEWAY_URL'] | Select-Object -ExpandProperty Port
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $gatewayPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $conflict = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if ($conflict -and $conflict.CommandLine -match '(?i)(^|\s)-m\s+gateway\.app(\s|$)') {
            & taskkill.exe /PID $listener.OwningProcess /T /F *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Stale local Gateway process could not be stopped.' }
            Write-Host '[ready] stale local Gateway process stopped after failed health check'
        } else {
            throw 'Gateway port is occupied by a non-demo process; refusing to stop it.'
        }
    }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for the managed Gateway.' }
    Set-ProcessEnvironment $Values
    $process = Start-Process $python -ArgumentList '-m','gateway.app' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    @{pid=$process.Id;started_at=$process.StartTime.ToUniversalTime().ToString('o');executable=$python} | ConvertTo-Json -Compress | Set-Content $gatewayPid -Encoding utf8
    foreach ($attempt in 1..60) { try { if ((Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { Write-Host '[ready] Gateway database=captain_test'; return } } catch {}; Start-Sleep -Milliseconds 500 }
    throw 'Gateway did not become healthy against captain_test.'
}
function Stop-ManagedGateway {
    if (-not (Test-Path $gatewayPid)) { Write-Host '[ready] no managed Gateway process'; return }
    try { $identity = Get-Content $gatewayPid -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Gateway PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if ($process) {
        $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
        if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$identity.executable)) { throw 'PID no longer belongs to the managed Gateway process.' }
        & taskkill.exe /PID $process.Id /T /F *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Managed Gateway process tree could not be stopped.' }
    }
    Remove-Item $gatewayPid -Force
    Write-Host '[ready] managed Gateway stopped'
}
function Get-ManagedRuntimeProcess {
    if (-not (Test-Path $runtimePid)) { return $null }
    try { $identity = Get-Content $runtimePid -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Runtime PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item $runtimePid -Force
        return $null
    }
    $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
    if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$identity.executable)) {
        throw 'PID no longer belongs to the managed Runtime process.'
    }
    return $process
}
function Start-Runtime($Values) {
    $runtimeUrl = [string]$Values['CAPTAIN_RUNTIME_URL']
    $managed = Get-ManagedRuntimeProcess
    if ($managed) {
        Stop-ManagedRuntime
        Write-Host '[ready] managed Runtime restarted for current configuration'
    }
    $runtimePort = ([Uri]$runtimeUrl).Port
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $runtimePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { throw 'Runtime port is occupied by an unmanaged process; refusing to reuse or stop it.' }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for the managed Runtime.' }
    Set-ProcessEnvironment $Values
    $process = Start-Process $python -ArgumentList '-m','agenten.agent_runtime.runtime_entrypoint' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    @{pid=$process.Id;started_at=$process.StartTime.ToUniversalTime().ToString('o');executable=$python} | ConvertTo-Json -Compress | Set-Content $runtimePid -Encoding utf8
    foreach ($attempt in 1..60) {
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) { break }
        try { if ((Invoke-WebRequest "$runtimeUrl/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { Write-Host '[ready] authenticated Runtime boundary'; return } } catch {}
        Start-Sleep -Milliseconds 500
    }
    Stop-ManagedRuntime
    throw 'Runtime did not become healthy.'
}
function Stop-ManagedRuntime {
    $process = Get-ManagedRuntimeProcess
    if (-not $process) { Write-Host '[ready] no managed Runtime process'; return }
    & taskkill.exe /PID $process.Id /T /F *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Managed Runtime process tree could not be stopped.' }
    Remove-Item $runtimePid -Force
    Write-Host '[ready] managed Runtime stopped'
}
function Assert-RuntimeConfiguration($Values) {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for Runtime preflight.' }
    Set-ProcessEnvironment $Values
    & $python -c 'from agenten.agent_runtime.runtime_entrypoint import preflight_runtime; preflight_runtime()' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Production Runtime configuration is unavailable; no services were started.' }
}
function Invoke-StartServices([switch]$RecoverDemoCredentials, [string]$SourceEnv) {
    $values = Initialize-LocalEnvironment
    Set-ProcessEnvironment $values
    docker compose --project-name $project --env-file $rootEnv --file $testCompose up -d --wait mariadb-test
    if ($LASTEXITCODE -ne 0) { throw 'Isolated captain_test MariaDB failed to start.' }
    try {
        Assert-RuntimeConfiguration $values
    } catch {
        docker compose --project-name $project --env-file $rootEnv --file $testCompose stop mariadb-test *> $null
        throw
    }
    Initialize-CaptainN8n $values -Recover:$RecoverDemoCredentials -SourceEnv $SourceEnv
    Start-Gateway $values
    Start-CaptainN8nBroker $values
    Start-Runtime $values
    docker compose --env-file $rootEnv up -d --wait mailpit
    if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit failed to start.' }
    & (Join-Path $PSScriptRoot 'minibook-demo.ps1') bootstrap -RecoverDemoCredentials:$RecoverDemoCredentials
    Invoke-Health
}
function Invoke-Health {
    $values = Read-Env $rootEnv @('CAPTAIN_RUNTIME_URL')
    if (-not $values.Contains('CAPTAIN_RUNTIME_URL')) { throw 'Runtime URL is not configured.' }
    if (-not (Get-ManagedRuntimeProcess)) { throw 'Managed Runtime process is not running.' }
    if ((Invoke-WebRequest "$($values['CAPTAIN_RUNTIME_URL'])/health" -UseBasicParsing -TimeoutSec 3).StatusCode -ne 200) { throw 'Runtime health check failed.' }
    & (Join-Path $PSScriptRoot 'minibook-demo.ps1') status
    & (Join-Path $PSScriptRoot 'demo-preflight.ps1') -EnvFile $rootEnv
    $summary = [ordered]@{schema='captain.live-demo-services.v1';checked_at=(Get-Date).ToUniversalTime().ToString('o');status='ready';secrets='redacted';database='captain_test';services=@('gateway','runtime','minibook','captain-n8n-rest','captain-n8n-mcp','mailpit')}
    New-Item -ItemType Directory -Force (Split-Path $evidence -Parent) | Out-Null
    $summary | ConvertTo-Json -Depth 4 | Set-Content $evidence -Encoding utf8
    Write-Host '[ready] redacted .captain-cook/evidence/live-demo-services.json'
}
Push-Location $root
try {
    switch ($Action) {
        start {
            Invoke-StartServices -Recover:$RecoverDemoCredentials -SourceEnv $CredentialSourceEnv
        }
        health { Invoke-Health }
        stop {
            & (Join-Path $PSScriptRoot 'minibook-demo.ps1') stop
            Stop-ManagedRuntime
            Stop-ManagedGateway
            docker compose --env-file $rootEnv stop mailpit
            docker compose --project-name $project --env-file $rootEnv --file $testCompose stop mariadb-test
            if ($LASTEXITCODE -ne 0) { throw 'Captain demo container stop failed.' }
            Stop-CaptainN8nContainers
            Write-Host '[ready] only Captain-managed demo services stopped; no volumes removed'
        }
    }
} finally { Pop-Location }
