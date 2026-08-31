#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "status", "portal-start", "gateway-restart", "benchmark-start", "benchmark-restart", "health", "stop")]
    [string]$Action,
    [switch]$RecoverDemoCredentials,
    [string]$CredentialSourceEnv,
    [string]$MinibookCommand = (Join-Path $PSScriptRoot 'minibook-demo.ps1'),
    [scriptblock]$CaptainStartProbe,
    [scriptblock]$StatusProbe,
    [scriptblock]$RuntimeHealthProbe,
    [scriptblock]$RuntimeListenerProbe
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
if (-not (Get-Variable -Name MinibookCommand -Scope Script -ErrorAction SilentlyContinue)) {
    $script:MinibookCommand = Join-Path $PSScriptRoot 'minibook-demo.ps1'
}
if (-not (Get-Variable -Name CaptainStartProbe -Scope Script -ErrorAction SilentlyContinue)) {
    $script:CaptainStartProbe = $null
}
if (-not (Get-Variable -Name StatusProbe -Scope Script -ErrorAction SilentlyContinue)) {
    $script:StatusProbe = $null
}
if (-not (Get-Variable -Name RuntimeHealthProbe -Scope Script -ErrorAction SilentlyContinue)) {
    $script:RuntimeHealthProbe = $null
}
if (-not (Get-Variable -Name RuntimeListenerProbe -Scope Script -ErrorAction SilentlyContinue)) {
    $script:RuntimeListenerProbe = $null
}
$root = Split-Path -Parent $PSScriptRoot
$rootEnv = Join-Path $root '.env'
$n8nEnv = Join-Path $root '.env.captain-n8n'
$testCompose = Join-Path $root 'docker-compose.test.yml'
$benchmarkCompose = Join-Path $root 'docker-compose.benchmark.yml'
$stateDir = Join-Path $root '.captain-cook'
$gatewayPid = Join-Path $stateDir 'gateway-demo.pid'
$benchmarkGatewayPid = Join-Path $stateDir 'gateway-business-benchmark.pid'
$benchmarkRuntimeEnv = Join-Path $stateDir 'private/business-benchmarks/business-benchmark-runtime.env'
$runtimePid = Join-Path $stateDir 'runtime-demo.pid'
$evidence = Join-Path $stateDir 'evidence/live-demo-services.json'
$project = 'captain-cook-test'
$benchmarkProject = 'captain-cook-business-benchmark'
. (Join-Path $PSScriptRoot 'managed-process-identity.ps1')

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
function Initialize-LocalEnvironment {
    $allowed = @(
        'MARIADB_PASSWORD','MARIADB_ROOT_PASSWORD','MARIADB_TEST_PASSWORD','MARIADB_TEST_ROOT_PASSWORD',
        'CAPTAIN_GATEWAY_TOKEN','WORKER_GATEWAY_TOKEN','CAPTAIN_RUNTIME_TOKEN',
        'MARIADB_TEST_PORT','MARIADB_BENCHMARK_PORT','GATEWAY_PORT','CAPTAIN_BENCHMARK_GATEWAY_PORT','CAPTAIN_RUNTIME_PORT',
        'TEST_MARIADB_DSN','LEDGER_DSN','CAPTAIN_GATEWAY_URL','CAPTAIN_RUNTIME_URL',
        'CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256',
        'CAPTAIN_HERMES_PROVIDER','CAPTAIN_HERMES_MODEL','CAPTAIN_HERMES_BASE_URL',
        'N8N_API_KEY','N8N_MCP_TOKEN','CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_URL',
        'PORTAL_SUPABASE_ISSUER','PORTAL_SUPABASE_AUDIENCE','PORTAL_SUPABASE_JWKS_URL','PORTAL_ORGANIZATION_CLAIM',
        'PORTAL_PROVIDER_CONTROL_TOKEN','PORTAL_EVIDENCE_TOKEN','PORTAL_RESTART_CONTROL_TOKEN','SSL_CERT_FILE',
        'CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED','CAPTAIN_PORTAL_GITEA_ORIGIN','CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON'
    )
    $values = Read-Env $rootEnv $allowed
    Set-Missing $values 'MARIADB_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'CAPTAIN_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'WORKER_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'CAPTAIN_RUNTIME_TOKEN' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PORT' { '33306' }
    Set-Missing $values 'MARIADB_BENCHMARK_PORT' { '33316' }
    Set-Missing $values 'GATEWAY_PORT' { '8090' }
    Set-Missing $values 'CAPTAIN_BENCHMARK_GATEWAY_PORT' { '8092' }
    Set-Missing $values 'CAPTAIN_RUNTIME_PORT' { '8091' }
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
    $source = Read-Env $n8nEnv @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL')
    foreach ($name in @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL')) {
        if (-not $source.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$source[$name])) { throw "Captain n8n bootstrap did not provide $name." }
        $Values[$name] = $source[$name]
    }
    $Values['CAPTAIN_N8N_URL'] = "http://127.0.0.1:$($Values['CAPTAIN_N8N_PORT'])"
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n credentials inherited (values redacted)'
}
function Set-ProcessEnvironment($Values) {
    foreach ($item in $Values.GetEnumerator()) { [Environment]::SetEnvironmentVariable([string]$item.Key,[string]$item.Value,'Process') }
}
function Get-RuntimePythonExecutable {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for Runtime preflight.' }
    return $python
}
function Get-RuntimeAdapterManifestMetadata($Values) {
    $manifestName = 'CAPTAIN_RUNTIME_ADAPTER_MANIFEST'
    $digestName = 'CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256'
    $hasManifest = $Values.Contains($manifestName) -and -not [string]::IsNullOrWhiteSpace([string]$Values[$manifestName])
    $hasDigest = $Values.Contains($digestName) -and -not [string]::IsNullOrWhiteSpace([string]$Values[$digestName])
    if ($hasManifest -xor $hasDigest) { throw 'Runtime adapter manifest path and expected digest must be supplied together.' }
    $python = Get-RuntimePythonExecutable
    $generator = Join-Path $PSScriptRoot 'generate-runtime-adapter-manifest.py'
    if (-not (Test-Path $generator -PathType Leaf)) { throw 'Runtime adapter manifest generator is missing.' }
    $arguments = @($generator, '--repository-root', $root)
    if ($hasManifest) { $arguments += '--check' }
    $lines = @(& $python @arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Runtime adapter manifest generation or validation failed.' }
    if ($lines.Count -ne 3) { throw 'Runtime adapter manifest generator returned an invalid result.' }
    $metadata = [ordered]@{}
    foreach ($line in $lines) {
        if ($line -notmatch '^(manifest_path|manifest_sha256|module_sha256)=(.+)$') { throw 'Runtime adapter manifest generator returned an invalid result.' }
        if ($metadata.Contains($Matches[1])) { throw 'Runtime adapter manifest generator returned duplicate metadata.' }
        $metadata[$Matches[1]] = $Matches[2]
    }
    foreach ($name in @('manifest_path','manifest_sha256','module_sha256')) {
        if (-not $metadata.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$metadata[$name])) { throw 'Runtime adapter manifest generator returned incomplete metadata.' }
    }
    $runtimeAdapterRoot = [IO.Path]::GetFullPath((Join-Path $stateDir 'runtime-adapters'))
    $generatedManifest = [IO.Path]::GetFullPath([string]$metadata['manifest_path'])
    if (-not $generatedManifest.StartsWith($runtimeAdapterRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime adapter manifest generator returned a path outside the managed runtime directory.' }
    if ([string]$metadata['manifest_sha256'] -notmatch '^[0-9a-f]{64}$' -or [string]$metadata['module_sha256'] -notmatch '^[0-9a-f]{64}$') { throw 'Runtime adapter manifest generator returned invalid digests.' }
    if ($hasManifest) {
        if ([IO.Path]::GetFullPath([string]$Values[$manifestName]) -cne $generatedManifest -or [string]$Values[$digestName] -cne [string]$metadata['manifest_sha256']) { throw 'Runtime adapter manifest settings do not match the current committed adapter bytes.' }
    }
    $Values[$manifestName] = $generatedManifest
    $Values[$digestName] = [string]$metadata['manifest_sha256']
}
function Get-RuntimeConfigurationSha256($Values) {
    foreach ($name in @('CAPTAIN_RUNTIME_URL','CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256')) {
        if (-not $Values.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$Values[$name])) { throw "Runtime managed identity requires $name." }
    }
    $payload = @(
        'captain.runtime.configuration.v1',
        "CAPTAIN_RUNTIME_URL=$([string]$Values['CAPTAIN_RUNTIME_URL'])",
        "CAPTAIN_RUNTIME_ADAPTER_MANIFEST=$([string]$Values['CAPTAIN_RUNTIME_ADAPTER_MANIFEST'])",
        "CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256=$([string]$Values['CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256'])"
    ) -join "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($payload))).ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Test-AuthenticatedRuntimeHealth($Values) {
    $runtimeUrl = [string]$Values['CAPTAIN_RUNTIME_URL']
    $runtimeToken = [string]$Values['CAPTAIN_RUNTIME_TOKEN']
    if ($RuntimeHealthProbe) { return [bool](& $RuntimeHealthProbe $runtimeUrl $runtimeToken) }
    try {
        return (Invoke-WebRequest "$runtimeUrl/health" -Headers @{Authorization="Bearer $runtimeToken"} -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
    } catch { return $false }
}
function Get-RuntimeListeners([int]$Port) {
    if ($RuntimeListenerProbe) { return @(& $RuntimeListenerProbe $Port) }
    return @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
function Assert-ManagedRuntimeListener($Process, [int]$Port) {
    $listeners = @(Get-RuntimeListeners $Port)
    if ($listeners.Count -ne 1) { throw 'Runtime requires exactly one managed listener.' }
    if ([int]$listeners[0].OwningProcess -ne $Process.Id) { throw 'Runtime listener is not owned by the managed process.' }
}
function Resolve-ManagedRuntimeListenerProcess($Process, [int]$Port) {
    $listeners = @(Get-RuntimeListeners $Port)
    if ($listeners.Count -ne 1) { throw 'Runtime requires exactly one managed listener.' }
    $listenerProcess = Get-Process -Id $listeners[0].OwningProcess -ErrorAction Stop
    if ($listenerProcess.Id -ne $Process.Id) {
        $listenerMetadata = Get-CimInstance Win32_Process -Filter "ProcessId=$($listenerProcess.Id)"
        if ([int]$listenerMetadata.ParentProcessId -ne $Process.Id) {
            throw 'Runtime listener process is neither the managed launcher nor an exact child of it.'
        }
        if ([string]$listenerMetadata.CommandLine -notmatch '(?:^|\s)-m\s+agenten\.agent_runtime\.runtime_entrypoint(?:\s|$)') {
            throw 'Runtime listener child process does not run the expected runtime entrypoint module.'
        }
    }
    return $listenerProcess
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
    foreach ($attempt in 1..30) { try {
        $rest = Invoke-WebRequest "$baseUrl/api/v1/workflows?limit=1" -Headers @{'X-N8N-API-KEY'=[string]$Values['N8N_API_KEY']} -UseBasicParsing -TimeoutSec 5
        $body = '{"jsonrpc":"2.0","id":"credential-recovery","method":"tools/list","params":{}}'
        $mcp = Invoke-WebRequest "$baseUrl/mcp-server/http" -Method Post -Headers @{Authorization="Bearer $([string]$Values['N8N_MCP_TOKEN'])";Accept='application/json, text/event-stream'} -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 5
        if ($rest.StatusCode -eq 200 -and $mcp.StatusCode -eq 200) { $authenticated=$true; break }
    } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $authenticated) { throw 'Captain n8n recovery credentials failed REST or MCP authentication.' }
    $recovered = [ordered]@{
        CAPTAIN_N8N_PORT='5679'; CAPTAIN_N8N_API_KEY=[string]$Values['N8N_API_KEY']; CAPTAIN_N8N_MCP_TOKEN=[string]$Values['N8N_MCP_TOKEN']; CAPTAIN_N8N_MCP_BROKER_URL='http://127.0.0.1:5680'
    }
    Save-Env $recovered $n8nEnv
    foreach ($item in $recovered.GetEnumerator()) { $Values[$item.Key] = $item.Value }
    $Values['CAPTAIN_N8N_URL'] = $baseUrl
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n demo credentials recovered after REST/MCP verification (values redacted)'
}
function Get-GatewayConfigurationSha256($Values) {
    $configurationNames = @(
        'CAPTAIN_GATEWAY_URL','LEDGER_DSN','CAPTAIN_GATEWAY_TOKEN','WORKER_GATEWAY_TOKEN',
        'PORTAL_SUPABASE_ISSUER','PORTAL_SUPABASE_AUDIENCE','PORTAL_SUPABASE_JWKS_URL','PORTAL_ORGANIZATION_CLAIM',
        'PORTAL_PROVIDER_CONTROL_TOKEN','PORTAL_EVIDENCE_TOKEN','PORTAL_RESTART_CONTROL_TOKEN','SSL_CERT_FILE',
        'CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN',
        'CAPTAIN_PORTAL_GITEA_ORIGIN','CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON'
    )
    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add('captain.gateway.configuration.v2')
    foreach ($name in $configurationNames) {
        $value = if ($Values.Contains($name)) { [string]$Values[$name] } else { '' }
        $lines.Add("$name=$value")
    }
    $payload = $lines -join "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        return [Convert]::ToHexString($sha.ComputeHash($bytes)).ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}
function Test-CaptainN8nCredentials($Values) {
    foreach ($name in @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL')) {
        if (-not $Values.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$Values[$name])) {
            throw "Captain n8n stored credential verification requires $name."
        }
    }
    $baseUrl = "http://127.0.0.1:$($Values['CAPTAIN_N8N_PORT'])"
    $verified = $false
    foreach ($attempt in 1..10) {
        try {
            $rest = Invoke-WebRequest "$baseUrl/api/v1/workflows?limit=1" -Headers @{'X-N8N-API-KEY'=[string]$Values['CAPTAIN_N8N_API_KEY']} -UseBasicParsing -TimeoutSec 5
            $body = '{"jsonrpc":"2.0","id":"stored-credential-verification","method":"tools/list","params":{}}'
            $mcp = Invoke-WebRequest "$baseUrl/mcp-server/http" -Method Post -Headers @{Authorization="Bearer $([string]$Values['CAPTAIN_N8N_MCP_TOKEN'])";Accept='application/json, text/event-stream'} -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 5
            $brokerPort = ([Uri][string]$Values['CAPTAIN_N8N_MCP_BROKER_URL']).Port
            $brokerReady = Test-NetConnection -ComputerName '127.0.0.1' -Port $brokerPort -InformationLevel Quiet -WarningAction SilentlyContinue
            if ($rest.StatusCode -eq 200 -and $mcp.StatusCode -eq 200 -and $brokerReady) {
                $verified = $true
                break
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $verified) {
        throw 'Captain n8n stored REST/MCP credentials failed verification.'
    }
    Write-Host '[ready] Captain n8n stored REST/MCP credentials verified (values redacted)'
}
function Initialize-CaptainN8n($Values, [switch]$Recover, [string]$SourceEnv) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
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
        Sync-CaptainN8nEnvironment $Values
        Test-CaptainN8nCredentials $Values
        return
    } else {
        & $n8n init
        & $n8n start
        & $n8n bootstrap
    }
    Sync-CaptainN8nEnvironment $Values
}
function Start-CaptainN8nBroker($Values) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
    $gatewayUri = [Uri][string]$Values['CAPTAIN_GATEWAY_URL']
    $brokerValues = [ordered]@{}
    foreach ($item in $Values.GetEnumerator()) { $brokerValues[$item.Key] = $item.Value }
    $brokerValues['CAPTAIN_GATEWAY_URL'] = "http://host.docker.internal:$($gatewayUri.Port)"
    Set-ProcessEnvironment $brokerValues
    try {
        & $n8n broker-start *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Captain n8n MCP broker could not bind to the selected Gateway.'
        }
    }
    finally {
        Set-ProcessEnvironment $Values
    }
    Write-Host '[ready] Captain n8n MCP broker bound to the selected Gateway (values redacted)'
}
function Stop-CaptainN8nContainers {
    $containers = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Captain n8n containers.' }
    if ($containers.Count -gt 0) { & docker stop @containers *> $null; if ($LASTEXITCODE -ne 0) { throw 'Captain n8n containers could not be stopped.' } }
    Write-Host '[ready] labelled Captain n8n containers stopped; volumes preserved'
}
function Start-Gateway($Values, [string]$PidPath=$gatewayPid) {
    $gatewayPort = [Uri]$Values['CAPTAIN_GATEWAY_URL'] | Select-Object -ExpandProperty Port
    $configurationSha256 = Get-GatewayConfigurationSha256 $Values
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $gatewayPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $configurationReplacement = $false
        try {
            $managed = Get-ManagedListenerIdentity -Path $PidPath -Port $gatewayPort -ConfigurationSha256 $configurationSha256
        }
        catch {
            try {
                $managed = Get-ManagedListenerIdentityForConfigurationReplacement `
                    -Path $PidPath `
                    -Port $gatewayPort `
                    -ReplacementConfigurationSha256 $configurationSha256
                $configurationReplacement = $true
            }
            catch {
                throw 'Gateway port is occupied without the exact managed process and ledger identity; refusing reuse or termination.'
            }
        }
        if (-not $configurationReplacement) {
            try { if ((Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { Write-Host '[ready] Gateway already healthy with verified process and ledger identity'; return } } catch {}
        }
        & taskkill.exe /PID $managed.Id /T /F *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Verified stale local Gateway process could not be stopped.' }
        Remove-Item -LiteralPath $PidPath -Force
        if ($configurationReplacement) {
            Write-Host '[ready] verified managed Gateway stopped for configuration replacement'
        }
        else {
            Write-Host '[ready] verified stale local Gateway process stopped after failed health check'
        }
    } elseif (Test-Path -LiteralPath $PidPath -PathType Leaf) {
        $managed = $null
        try {
            $managed = Get-ManagedProcessIdentity -Path $PidPath -ConfigurationSha256 $configurationSha256 -AllowExited
        }
        catch {
            try { $legacyIdentity = Get-Content -LiteralPath $PidPath -Raw | ConvertFrom-Json }
            catch { throw 'Legacy Gateway identity is invalid; refusing cleanup.' }
            $propertyNames = @($legacyIdentity.PSObject.Properties.Name)
            if ($propertyNames -contains 'schema' -or $propertyNames -contains 'configuration_sha256' -or $propertyNames -notcontains 'pid') {
                throw
            }
            $legacyProcess = Get-Process -Id ([int]$legacyIdentity.pid) -ErrorAction SilentlyContinue
            if ($legacyProcess) {
                throw 'legacy Gateway identity still refers to a running process; refusing cleanup.'
            }
            Remove-Item -LiteralPath $PidPath -Force
            Write-Host '[ready] verified exited legacy Gateway identity removed'
        }
        if ($managed) {
            & taskkill.exe /PID $managed.Id /T /F *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Verified listenerless Gateway process could not be stopped.' }
        }
        if (Test-Path -LiteralPath $PidPath -PathType Leaf) {
            Remove-Item -LiteralPath $PidPath -Force
        }
    }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for the managed Gateway.' }
    Set-ProcessEnvironment $Values
    $process = Start-Process $python -ArgumentList '-m','gateway.app' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    foreach ($attempt in 1..60) {
        $healthy = $false
        try { $healthy = (Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
        if ($healthy) {
            try {
                $listeners = @(
                    Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $gatewayPort -State Listen -ErrorAction SilentlyContinue
                )
                if ($listeners.Count -ne 1) { throw 'Gateway requires exactly one listener.' }
                $listenerProcess = Get-Process -Id $listeners[0].OwningProcess -ErrorAction Stop
                $listenerMetadata = Get-CimInstance Win32_Process -Filter "ProcessId=$($listenerProcess.Id)"
                if (
                    [int]$listenerMetadata.ParentProcessId -ne $process.Id -or
                    [string]$listenerMetadata.CommandLine -notmatch '(?:^|\s)-m\s+gateway\.app(?:\s|$)'
                ) {
                    throw 'Gateway listener process is not an exact child of the managed launcher.'
                }
                Write-ManagedProcessIdentity -Process $listenerProcess -Path $PidPath -ConfigurationSha256 $configurationSha256
                $verified = Get-ManagedListenerIdentity -Path $PidPath -Port $gatewayPort -ConfigurationSha256 $configurationSha256
                if ($verified.Id -ne $listenerProcess.Id) { throw 'New Gateway process does not own the healthy listener.' }
            }
            catch {
                if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
                    & taskkill.exe /PID $process.Id /T /F *> $null
                }
                if (Test-Path -LiteralPath $PidPath -PathType Leaf) {
                    Remove-Item -LiteralPath $PidPath -Force
                }
                throw 'Healthy Gateway listener does not match the newly managed process and ledger identity.'
            }
            Write-Host '[ready] Gateway database=captain_test with verified process and ledger identity'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
        & taskkill.exe /PID $process.Id /T /F *> $null
    }
    if (Test-Path -LiteralPath $PidPath -PathType Leaf) {
        Remove-Item -LiteralPath $PidPath -Force
    }
    throw 'Gateway did not become healthy against captain_test.'
}
function Stop-ManagedGateway([string]$PidPath=$gatewayPid) {
    if (-not (Test-Path $PidPath)) { Write-Host '[ready] no managed Gateway process'; return }
    try { $identity = Get-Content $PidPath -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Gateway PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if ($process) {
        $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
        if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$identity.executable)) { throw 'PID no longer belongs to the managed Gateway process.' }
        & taskkill.exe /PID $process.Id /T /F *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Managed Gateway process tree could not be stopped.' }
    }
    Remove-Item $PidPath -Force
    Write-Host '[ready] managed Gateway stopped'
}
function Restart-Gateway($Values, [string]$PidPath=$gatewayPid) {
    $gatewayPort = [Uri]$Values['CAPTAIN_GATEWAY_URL'] | Select-Object -ExpandProperty Port
    $configurationSha256 = Get-GatewayConfigurationSha256 $Values
    try {
        $managed = Get-ManagedListenerIdentity `
            -Path $PidPath `
            -Port $gatewayPort `
            -ConfigurationSha256 $configurationSha256
    }
    catch {
        $managed = Get-ManagedListenerIdentityForConfigurationReplacement `
            -Path $PidPath `
            -Port $gatewayPort `
            -ReplacementConfigurationSha256 $configurationSha256
    }
    & taskkill.exe /PID $managed.Id /T /F *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Verified managed Gateway process tree could not be stopped for restart.'
    }
    Remove-Item -LiteralPath $PidPath -Force
    Start-Gateway $Values -PidPath $PidPath
    Write-Host '[ready] verified managed Gateway restarted without container or volume changes'
}
function Get-ManagedRuntimeProcess($Values) {
    if (-not (Test-Path $runtimePid)) { return $null }
    $configurationSha256 = Get-RuntimeConfigurationSha256 $Values
    try {
        $process = Get-ManagedProcessIdentity -Path $runtimePid -ConfigurationSha256 $configurationSha256 -AllowExited
        if (-not $process) { Remove-Item -LiteralPath $runtimePid -Force; return $null }
        return $process
    }
    catch { throw 'PID no longer belongs to the managed Runtime process.' }
}
function Start-Runtime($Values) {
    $runtimeUrl = [string]$Values['CAPTAIN_RUNTIME_URL']
    $runtimePort = ([Uri]$runtimeUrl).Port
    $configurationSha256 = Get-RuntimeConfigurationSha256 $Values
    $managed = Get-ManagedRuntimeProcess $Values
    if ($managed) {
        if (Test-AuthenticatedRuntimeHealth $Values) {
            Assert-ManagedRuntimeListener $managed $runtimePort
            Write-Host '[ready] Runtime already healthy with verified process identity'
            return
        }
        throw 'Managed Runtime process exists but is not healthy.'
    }
    if (@(Get-RuntimeListeners $runtimePort).Count -ne 0) { throw 'Runtime port is occupied by an unmanaged process; refusing to reuse or stop it.' }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Get-RuntimePythonExecutable
    Set-ProcessEnvironment $Values
    $stdoutLog = Join-Path $root 'runtime-stdout.log'
    $stderrLog = Join-Path $root 'runtime-stderr.log'
    $process = Start-Process $python `
        -ArgumentList '-u','-m','agenten.agent_runtime.runtime_entrypoint' `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $identityFailure = ''
    foreach ($attempt in 1..60) {
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) { break }
        try {
            if (Test-AuthenticatedRuntimeHealth $Values) {
                $listenerProcess = Resolve-ManagedRuntimeListenerProcess $process $runtimePort
                Write-ManagedProcessIdentity -Process $listenerProcess -Path $runtimePid -ConfigurationSha256 $configurationSha256
                Write-Host '[ready] authenticated Runtime boundary with verified process identity'
                return
            }
        } catch { $identityFailure = $_.Exception.Message }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
        & taskkill.exe /PID $process.Id /T /F *> $null
    }
    $detail = ''
    try {
        if (Test-Path $stderrLog) { $detail = (Get-Content $stderrLog -Tail 20 -ErrorAction Stop) -join "`n" }
        if ([string]::IsNullOrWhiteSpace($detail) -and (Test-Path $stdoutLog)) {
            $detail = (Get-Content $stdoutLog -Tail 20 -ErrorAction Stop) -join "`n"
        }
    } catch {
        $detail = "(runtime log could not be read: $($_.Exception.GetType().Name))"
    }
    if ($identityFailure) { $detail = "process identity: $identityFailure`n$detail" }
    $runtimeSecretValueKeys = @(
        'MARIADB_PASSWORD','MARIADB_ROOT_PASSWORD','MARIADB_TEST_PASSWORD','MARIADB_TEST_ROOT_PASSWORD',
        'CAPTAIN_GATEWAY_TOKEN','WORKER_GATEWAY_TOKEN','CAPTAIN_RUNTIME_TOKEN',
        'N8N_API_KEY','N8N_MCP_TOKEN','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN',
        'PORTAL_PROVIDER_CONTROL_TOKEN','PORTAL_EVIDENCE_TOKEN','PORTAL_RESTART_CONTROL_TOKEN',
        'TEST_MARIADB_DSN','LEDGER_DSN'
    )
    foreach ($secretKey in $runtimeSecretValueKeys) {
        if ($Values.Contains($secretKey)) {
            $secretValue = [string]$Values[$secretKey]
            if (-not [string]::IsNullOrWhiteSpace($secretValue)) { $detail = $detail.Replace($secretValue, '***') }
        }
    }
    throw "Runtime did not become healthy.`n--- runtime output ---`n$detail"
}
function Stop-ManagedRuntime($Values) {
    $process = Get-ManagedRuntimeProcess $Values
    if (-not $process) { Write-Host '[ready] no managed Runtime process'; return }
    & taskkill.exe /PID $process.Id /T /F *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Managed Runtime process tree could not be stopped.' }
    Remove-Item $runtimePid -Force
    Write-Host '[ready] managed Runtime stopped'
}
function Assert-RuntimeConfiguration($Values) {
    Get-RuntimeAdapterManifestMetadata $Values
    $python = Get-RuntimePythonExecutable
    Set-ProcessEnvironment $Values
    & $python -c 'from agenten.agent_runtime.runtime_entrypoint import preflight_runtime; preflight_runtime()'
    if ($LASTEXITCODE -ne 0) { throw 'Production Runtime configuration is unavailable; no services were started.' }
}
function Invoke-StartServices([switch]$RecoverDemoCredentials, [string]$SourceEnv) {
    if ($null -ne $CaptainStartProbe) {
        $values = [ordered]@{}
        & $CaptainStartProbe $values
    } else {
        $runtimeAdapterValues = Read-Env $rootEnv @('CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256','CAPTAIN_HERMES_RUNTIME_SKILL','CAPTAIN_HERMES_RUNTIME_SKILL_SHA256','CAPTAIN_RUNTIME_URL','CAPTAIN_RUNTIME_TOKEN','CAPTAIN_GATEWAY_URL','CAPTAIN_GATEWAY_TOKEN','CAPTAIN_HERMES_PROVIDER','CAPTAIN_HERMES_MODEL','CAPTAIN_HERMES_BASE_URL')
        Get-RuntimeAdapterManifestMetadata $runtimeAdapterValues
        $values = Initialize-LocalEnvironment
        foreach ($name in @('CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256','CAPTAIN_HERMES_RUNTIME_SKILL','CAPTAIN_HERMES_RUNTIME_SKILL_SHA256','CAPTAIN_RUNTIME_URL','CAPTAIN_RUNTIME_TOKEN','CAPTAIN_GATEWAY_URL','CAPTAIN_GATEWAY_TOKEN','CAPTAIN_HERMES_PROVIDER','CAPTAIN_HERMES_MODEL','CAPTAIN_HERMES_BASE_URL')) { $values[$name] = $runtimeAdapterValues[$name] }
        Set-ProcessEnvironment $values
        Assert-RuntimeConfiguration $values
        Initialize-CaptainN8n $values -Recover:$RecoverDemoCredentials -SourceEnv $SourceEnv
        docker compose --project-name $project --env-file $rootEnv --file $testCompose up -d --wait mariadb-test
        if ($LASTEXITCODE -ne 0) { throw 'Isolated captain_test MariaDB failed to start.' }
        Start-Gateway $values
        Start-CaptainN8nBroker $values
        Start-Runtime $values
        docker compose --env-file $rootEnv up -d --wait mailpit
        if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit failed to start.' }
    }
    $global:LASTEXITCODE = 0
    & $MinibookCommand start
    if ($LASTEXITCODE -ne 0) { throw 'Minibook start failed.' }
    if ($null -ne $StatusProbe) { & $StatusProbe } else { Invoke-Health }
}
function Invoke-PortalStart([switch]$RecoverDemoCredentials, [string]$SourceEnv) {
    $values = Initialize-LocalEnvironment
    Set-ProcessEnvironment $values
    Initialize-CaptainN8n $values -Recover:$RecoverDemoCredentials -SourceEnv $SourceEnv
    docker compose --project-name $project --env-file $rootEnv --file $testCompose up -d --wait mariadb-test
    if ($LASTEXITCODE -ne 0) { throw 'Isolated captain_test MariaDB failed to start.' }
    Start-Gateway $values
    Start-CaptainN8nBroker $values
    if ((Invoke-WebRequest "$($values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -ne 200) {
        throw 'Portal Gateway health check failed.'
    }
    $summary = [ordered]@{
        schema='captain.portal-services.v1'
        checked_at=(Get-Date).ToUniversalTime().ToString('o')
        status='ready'
        secrets='redacted'
        database='captain_test'
        services=@('gateway','captain-n8n-rest','captain-n8n-mcp')
        non_claims=@('agent-runtime','minibook')
    }
    $portalEvidence = Join-Path $stateDir 'evidence/portal-services.json'
    New-Item -ItemType Directory -Force (Split-Path $portalEvidence -Parent) | Out-Null
    $summary | ConvertTo-Json -Depth 4 | Set-Content $portalEvidence -Encoding utf8
    Write-Host '[ready] Portal control services started; Runtime and Minibook not claimed'
}
function Invoke-BenchmarkStart([switch]$RecoverDemoCredentials, [string]$SourceEnv) {
    $values = Initialize-LocalEnvironment
    Initialize-CaptainN8n $values -Recover:$RecoverDemoCredentials -SourceEnv $SourceEnv
    $benchmarkValues = [ordered]@{}
    foreach ($item in $values.GetEnumerator()) { $benchmarkValues[$item.Key] = $item.Value }
    $benchmarkValues['MARIADB_TEST_PORT'] = [string]$values['MARIADB_BENCHMARK_PORT']
    $benchmarkValues['GATEWAY_PORT'] = [string]$values['CAPTAIN_BENCHMARK_GATEWAY_PORT']
    $escapedPassword = [Uri]::EscapeDataString([string]$values['MARIADB_TEST_PASSWORD'])
    $benchmarkValues['TEST_MARIADB_DSN'] = "mariadb://captain_test:${escapedPassword}@127.0.0.1:$($benchmarkValues['MARIADB_TEST_PORT'])/captain_test"
    $benchmarkValues['LEDGER_DSN'] = $benchmarkValues['TEST_MARIADB_DSN']
    $benchmarkValues['CAPTAIN_GATEWAY_URL'] = "http://127.0.0.1:$($benchmarkValues['GATEWAY_PORT'])"
    Set-ProcessEnvironment $benchmarkValues
    docker compose --project-name $benchmarkProject --env-file $rootEnv --file $benchmarkCompose up -d --wait mariadb-benchmark
    if ($LASTEXITCODE -ne 0) { throw 'Dedicated persistent business benchmark MariaDB failed to start.' }
    Start-Gateway $benchmarkValues -PidPath $benchmarkGatewayPid
    Start-CaptainN8nBroker $benchmarkValues
    $runtimeValues = [ordered]@{
        TEST_MARIADB_DSN=[string]$benchmarkValues['TEST_MARIADB_DSN']
        MARIADB_BENCHMARK_PORT=[string]$benchmarkValues['MARIADB_TEST_PORT']
        CAPTAIN_BENCHMARK_GATEWAY_URL=[string]$benchmarkValues['CAPTAIN_GATEWAY_URL']
    }
    New-Item -ItemType Directory -Force (Split-Path $benchmarkRuntimeEnv -Parent) | Out-Null
    Save-Env $runtimeValues $benchmarkRuntimeEnv
    Write-Host '[ready] dedicated persistent business benchmark infrastructure database=captain_test (values redacted)'
}
function Invoke-Health {
    $values = Read-Env $rootEnv @('CAPTAIN_RUNTIME_URL','CAPTAIN_RUNTIME_TOKEN','CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256')
    if (-not $values.Contains('CAPTAIN_RUNTIME_URL')) { throw 'Runtime URL is not configured.' }
    if (-not $values.Contains('CAPTAIN_RUNTIME_TOKEN')) { throw 'Runtime health authentication is not configured.' }
    Get-RuntimeAdapterManifestMetadata $values
    $managed = Get-ManagedRuntimeProcess $values
    if (-not $managed) { throw 'Managed Runtime process is not running.' }
    if (-not (Test-AuthenticatedRuntimeHealth $values)) { throw 'Runtime health check failed.' }
    Assert-ManagedRuntimeListener $managed ([Uri]$values['CAPTAIN_RUNTIME_URL']).Port
    & $MinibookCommand status
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
        portal-start {
            Invoke-PortalStart -Recover:$RecoverDemoCredentials -SourceEnv $CredentialSourceEnv
        }
        gateway-restart {
            $values = Initialize-LocalEnvironment
            Restart-Gateway $values
        }
        benchmark-start {
            Invoke-BenchmarkStart -Recover:$RecoverDemoCredentials -SourceEnv $CredentialSourceEnv
        }
        benchmark-restart {
            Stop-ManagedGateway -PidPath $benchmarkGatewayPid
            Invoke-BenchmarkStart -Recover:$RecoverDemoCredentials -SourceEnv $CredentialSourceEnv
        }
        status { Invoke-Health }
        health { Invoke-Health }
        stop {
            $runtimeValues = Read-Env $rootEnv @('CAPTAIN_RUNTIME_URL','CAPTAIN_RUNTIME_ADAPTER_MANIFEST','CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256')
            if ($runtimeValues.Contains('CAPTAIN_RUNTIME_URL')) { Get-RuntimeAdapterManifestMetadata $runtimeValues }
            & (Join-Path $PSScriptRoot 'minibook-demo.ps1') stop
            if ($runtimeValues.Contains('CAPTAIN_RUNTIME_URL')) { Stop-ManagedRuntime $runtimeValues }
            Stop-ManagedGateway
            Stop-ManagedGateway -PidPath $benchmarkGatewayPid
            docker compose --env-file $rootEnv stop mailpit
            docker compose --project-name $project --env-file $rootEnv --file $testCompose stop mariadb-test
            if ($LASTEXITCODE -ne 0) { throw 'Captain demo container stop failed.' }
            docker compose --project-name $benchmarkProject --env-file $rootEnv --file $benchmarkCompose stop mariadb-benchmark
            if ($LASTEXITCODE -ne 0) { throw 'Captain benchmark container stop failed.' }
            Stop-CaptainN8nContainers
            Write-Host '[ready] only Captain-managed demo services stopped; no volumes removed'
        }
    }
} finally { Pop-Location }
