[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("init", "start", "bootstrap", "status", "stop")]
    [string]$Action
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $Root "docker-compose.captain-n8n.yml"
$EnvFile = Join-Path $Root ".env.captain-n8n"
$ProjectName = "captain-n8n-builder"
$ProjectLabel = "com.docker.compose.project=captain-n8n-builder"
$OwnerEmail = "captain@local.test"
$ApiKeyLabel = "Captain local builder"
$DefaultPort = 5679

$AllowedEnvironmentKeys = @(
    "CAPTAIN_N8N_PORT",
    "CAPTAIN_N8N_ENCRYPTION_KEY",
    "CAPTAIN_N8N_POSTGRES_PASSWORD",
    "CAPTAIN_N8N_POSTGRES_USER",
    "CAPTAIN_N8N_POSTGRES_DB",
    "CAPTAIN_N8N_OWNER_PASSWORD",
    "CAPTAIN_N8N_API_KEY",
    "CAPTAIN_N8N_MCP_TOKEN",
    "CAPTAIN_N8N_MCP_BROKER_URL",
    "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET"
)

function Assert-LocalContractFiles {
    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) {
        throw "Captain n8n Compose contract is missing: $ComposeFile"
    }

    $expectedEnvPath = [System.IO.Path]::GetFullPath((Join-Path $Root ".env.captain-n8n"))
    if ([System.IO.Path]::GetFullPath($EnvFile) -ne $expectedEnvPath) {
        throw "Refusing to use an environment file outside the Captain n8n contract."
    }
}

function Assert-DockerAvailable {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is required for the Captain n8n builder."
    }

    $versionOutput = & docker version --format "{{.Server.Version}}" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Engine is unavailable; start Docker Desktop and retry."
    }
    $null = $versionOutput
}

function Protect-EnvFile {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        return
    }

    $isWindowsPlatform = (
        $PSVersionTable.PSEdition -eq "Desktop" -or
        [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
    )
    if ($isWindowsPlatform) {
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $aclOutput = & icacls.exe $EnvFile /inheritance:r /grant:r "${currentUser}:(R,W)" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restrict permissions on .env.captain-n8n."
        }
        $null = $aclOutput
        return
    }

    $chmodOutput = & chmod 600 $EnvFile 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict permissions on .env.captain-n8n."
    }
    $null = $chmodOutput
}

function Get-EnvValues {
    $values = @{}
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        return $values
    }

    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            throw "Invalid line in .env.captain-n8n; expected NAME=value."
        }

        $name = $Matches[1]
        if ($values.ContainsKey($name)) {
            throw "Duplicate environment key in .env.captain-n8n: $name"
        }
        $values[$name] = $Matches[2]
    }
    return $values
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ($AllowedEnvironmentKeys -notcontains $Name) {
        throw "Refusing to write an unknown Captain n8n environment key."
    }
    if ($Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Refusing to write a multiline Captain n8n environment value."
    }

    $lines = if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        [System.Collections.Generic.List[string]]::new(
            [System.IO.File]::ReadAllLines($EnvFile)
        )
    }
    else {
        [System.Collections.Generic.List[string]]::new()
    }

    $updated = [System.Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Name))=") {
            if (-not $found) {
                $updated.Add("$Name=$Value")
                $found = $true
            }
            continue
        }
        $updated.Add($line)
    }
    if (-not $found) {
        $updated.Add("$Name=$Value")
    }

    [System.IO.File]::WriteAllLines(
        $EnvFile,
        [string[]]$updated,
        [System.Text.UTF8Encoding]::new($false)
    )
    Protect-EnvFile
}

function New-RandomSecret {
    param(
        [ValidateRange(16, 64)]
        [int]$ByteCount = 32
    )

    $bytes = [byte[]]::new($ByteCount)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }

    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Get-CaptainPort {
    $values = Get-EnvValues
    $configuredPort = if ($values.ContainsKey("CAPTAIN_N8N_PORT")) {
        $values["CAPTAIN_N8N_PORT"]
    }
    else {
        [string]$DefaultPort
    }

    $port = 0
    if (-not [int]::TryParse($configuredPort, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "CAPTAIN_N8N_PORT must be an integer between 1 and 65535."
    }
    return $port
}

function Assert-LoopbackPortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $Port
    )
    try {
        $listener.Start()
    }
    catch {
        throw "127.0.0.1:$Port is already in use; Captain n8n was not started."
    }
    finally {
        $listener.Stop()
    }
}

function Initialize-CaptainEnvironment {
    $values = Get-EnvValues
    $defaults = [ordered]@{
        CAPTAIN_N8N_PORT = [string]$DefaultPort
        CAPTAIN_N8N_POSTGRES_USER = "captain_n8n"
        CAPTAIN_N8N_POSTGRES_DB = "captain_n8n"
    }

    foreach ($entry in $defaults.GetEnumerator()) {
        if (-not $values.ContainsKey($entry.Key) -or [string]::IsNullOrWhiteSpace($values[$entry.Key])) {
            Set-EnvValue -Name $entry.Key -Value $entry.Value
        }
    }
    if (-not $values.ContainsKey("CAPTAIN_N8N_ENCRYPTION_KEY") -or [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_ENCRYPTION_KEY"])) {
        Set-EnvValue -Name "CAPTAIN_N8N_ENCRYPTION_KEY" -Value (New-RandomSecret -ByteCount 48)
    }
    if (-not $values.ContainsKey("CAPTAIN_N8N_POSTGRES_PASSWORD") -or [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_POSTGRES_PASSWORD"])) {
        Set-EnvValue -Name "CAPTAIN_N8N_POSTGRES_PASSWORD" -Value (New-RandomSecret -ByteCount 32)
    }
    if (-not $values.ContainsKey("CAPTAIN_N8N_OWNER_PASSWORD") -or [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_OWNER_PASSWORD"])) {
        Set-EnvValue -Name "CAPTAIN_N8N_OWNER_PASSWORD" -Value ("A1" + (New-RandomSecret -ByteCount 30))
    }
    if (-not $values.ContainsKey("CAPTAIN_N8N_MCP_BROKER_URL") -or [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_MCP_BROKER_URL"])) {
        Set-EnvValue -Name "CAPTAIN_N8N_MCP_BROKER_URL" -Value "http://127.0.0.1:5680"
    }
    if (-not $values.ContainsKey("CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET") -or [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET"])) {
        Set-EnvValue -Name "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET" -Value (New-RandomSecret -ByteCount 32)
    }
}

function Assert-EnvironmentReady {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw "Run scripts/captain-n8n.ps1 -Action init first."
    }

    $values = Get-EnvValues
    foreach ($required in @(
        "CAPTAIN_N8N_PORT",
        "CAPTAIN_N8N_ENCRYPTION_KEY",
        "CAPTAIN_N8N_POSTGRES_PASSWORD",
        "CAPTAIN_N8N_POSTGRES_USER",
        "CAPTAIN_N8N_POSTGRES_DB",
        "CAPTAIN_N8N_OWNER_PASSWORD"
    )) {
        if (-not $values.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($values[$required])) {
            throw "Missing $required in .env.captain-n8n; rerun init."
        }
    }
}

function Invoke-ComposeConfigValidation {
    $configOutput = & docker compose -p captain-n8n-builder --env-file $EnvFile -f $ComposeFile config --quiet 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Captain n8n Compose validation failed."
    }
    $null = $configOutput
}

function Get-ProjectContainerIds {
    $ids = @(& docker ps -aq --filter "label=$ProjectLabel" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the Captain n8n project inventory."
    }
    return @($ids | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Assert-CaptainInventory {
    $ids = @(Get-ProjectContainerIds)
    if ($ids.Count -ne 2) {
        throw "Captain n8n inventory must contain exactly two project-scoped containers."
    }

    $services = @()
    foreach ($id in $ids) {
        $service = & docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' $id 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Could not validate a Captain n8n project container."
        }
        $services += ([string]$service).Trim()
    }

    $expected = @("n8n", "postgres")
    $actualServiceList = (@($services | Sort-Object) -join ",")
    $expectedServiceList = (@($expected | Sort-Object) -join ",")
    if ($actualServiceList -ne $expectedServiceList) {
        throw "Captain n8n inventory contains an unexpected service."
    }
}

function Test-CaptainN8nRunning {
    $ids = @(& docker ps -q `
        --filter "label=$ProjectLabel" `
        --filter "label=com.docker.compose.service=n8n" `
        --filter "status=running" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect Captain n8n status."
    }
    return @($ids | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -eq 1
}

function Get-HttpErrorStatusCode {
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    if ($null -eq $ErrorRecord.Exception.Response) {
        return 0
    }
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    }
    catch {
        return 0
    }
}

function Invoke-N8nJsonRequest {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("GET", "POST", "PATCH")]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$EndpointIdentity,
        [Parameter(Mandatory = $true)]
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [AllowNull()]
        [object]$Body,
        [int[]]$AllowedStatusCodes = @(200)
    )

    $parameters = @{
        Uri = $Uri
        Method = $Method
        WebSession = $Session
        UseBasicParsing = $true
        TimeoutSec = 15
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $parameters["Body"] = $Body | ConvertTo-Json -Compress -Depth 8
        $parameters["ContentType"] = "application/json"
    }

    try {
        $response = Invoke-WebRequest @parameters
        $statusCode = [int]$response.StatusCode
    }
    catch {
        $statusCode = Get-HttpErrorStatusCode -ErrorRecord $_
        if ($AllowedStatusCodes -contains $statusCode) {
            return [pscustomobject]@{ StatusCode = $statusCode; Data = $null }
        }
        if ($statusCode -eq 0) {
            throw "n8n $EndpointIdentity is unavailable or timed out; the Captain stack remains running."
        }
        throw "n8n $EndpointIdentity returned HTTP $statusCode; the Captain stack remains running."
    }

    if ($AllowedStatusCodes -notcontains $statusCode) {
        throw "n8n $EndpointIdentity returned unexpected HTTP $statusCode; the Captain stack remains running."
    }

    try {
        $json = $response.Content | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "n8n $EndpointIdentity returned an unsupported JSON schema; the Captain stack remains running."
    }
    if ($null -eq $json -or $null -eq $json.PSObject.Properties["data"]) {
        throw "n8n $EndpointIdentity omitted the pinned data envelope; the Captain stack remains running."
    }
    return [pscustomobject]@{ StatusCode = $statusCode; Data = $json.data }
}

function Assert-OwnerResponse {
    param(
        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [object]$Data,
        [Parameter(Mandatory = $true)]
        [string]$EndpointIdentity
    )

    if ($null -eq $Data -or $null -eq $Data.PSObject.Properties["email"] -or $Data.email -ne $OwnerEmail) {
        throw "n8n $EndpointIdentity returned an unsupported owner schema; the Captain stack remains running."
    }
}

function ConvertFrom-SecureValue {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.SecureString]$SecureValue
    )

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function ConvertTo-SecureStringValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $secureValue = [System.Security.SecureString]::new()
    foreach ($character in $Value.ToCharArray()) {
        $secureValue.AppendChar($character)
    }
    $secureValue.MakeReadOnly()
    return $secureValue
}

function Wait-CaptainN8nHealth {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl
    )

    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "$BaseUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ([int]$response.StatusCode -eq 200) {
                return 200
            }
        }
        catch {
            # Readiness failures are intentionally retried without emitting response content.
        }
        Start-Sleep -Seconds 2
    }
    throw "Captain n8n healthz did not return HTTP 200; the Captain stack remains running."
}

function Test-CaptainApiKey {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl,
        [Parameter(Mandatory = $true)]
        [string]$ApiKey
    )

    try {
        $response = Invoke-WebRequest `
            -Uri "$BaseUrl/api/v1/workflows?limit=1" `
            -Method GET `
            -Headers @{ "X-N8N-API-KEY" = $ApiKey } `
            -UseBasicParsing `
            -TimeoutSec 15 `
            -ErrorAction Stop
        if ([int]$response.StatusCode -ne 200) {
            throw "Authenticated workflow verification returned an unexpected status."
        }
        return 200
    }
    catch {
        $statusCode = Get-HttpErrorStatusCode -ErrorRecord $_
        if ($statusCode -eq 0) {
            throw "n8n workflows endpoint is unavailable or timed out; the Captain stack remains running."
        }
        throw "n8n workflows endpoint returned HTTP $statusCode; the Captain stack remains running."
    }
}

function Enable-CaptainMcpAccess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl,
        [Parameter(Mandatory = $true)]
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $settings = Invoke-N8nJsonRequest `
        -Method PATCH `
        -Uri "$BaseUrl/rest/mcp/settings" `
        -EndpointIdentity "MCP access settings" `
        -Session $Session `
        -Body @{ mcpAccessEnabled = $true }
    if (
        $null -eq $settings.Data -or
        $null -eq $settings.Data.PSObject.Properties["mcpAccessEnabled"] -or
        $settings.Data.mcpAccessEnabled -ne $true
    ) {
        throw "n8n MCP access settings returned an unsupported schema; the Captain stack remains running."
    }
}

function Get-CaptainMcpAccessToken {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl,
        [Parameter(Mandatory = $true)]
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [Parameter(Mandatory = $true)]
        [hashtable]$Values,
        [switch]$Rotate
    )

    $storedToken = $Values["CAPTAIN_N8N_MCP_TOKEN"]
    if (-not $Rotate -and -not [string]::IsNullOrWhiteSpace([string]$storedToken)) {
        return [string]$storedToken
    }

    $tokenResult = Invoke-N8nJsonRequest `
        -Method GET `
        -Uri "$BaseUrl/rest/mcp/api-key" `
        -EndpointIdentity "MCP access token" `
        -Session $Session `
        -Body $null
    if (
        $null -eq $tokenResult.Data -or
        $null -eq $tokenResult.Data.PSObject.Properties["apiKey"] -or
        [string]::IsNullOrWhiteSpace($tokenResult.Data.apiKey)
    ) {
        throw "n8n MCP access token response omitted apiKey; the Captain stack remains running."
    }
    $token = [string]$tokenResult.Data.apiKey
    if ($Rotate -or $token -match "^\*+$") {
        $tokenResult = Invoke-N8nJsonRequest `
            -Method POST `
            -Uri "$BaseUrl/rest/mcp/api-key/rotate" `
            -EndpointIdentity "MCP access token recovery rotation" `
            -Session $Session `
            -Body $null
        if (
            $null -eq $tokenResult.Data -or
            $null -eq $tokenResult.Data.PSObject.Properties["apiKey"] -or
            [string]::IsNullOrWhiteSpace($tokenResult.Data.apiKey)
        ) {
            throw "n8n MCP access token rotation omitted apiKey; the Captain stack remains running."
        }
        $token = [string]$tokenResult.Data.apiKey
    }
    Set-EnvValue -Name "CAPTAIN_N8N_MCP_TOKEN" -Value $token
    return $token
}

function Test-CaptainMcpAccessToken {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl,
        [Parameter(Mandatory = $true)]
        [string]$McpToken
    )

    $initializeRequest = @{
        jsonrpc = "2.0"
        id = "captain-bootstrap"
        method = "initialize"
        params = @{
            protocolVersion = "2025-03-26"
            capabilities = @{}
            clientInfo = @{ name = "captain-bootstrap"; version = "1" }
        }
    } | ConvertTo-Json -Compress -Depth 8
    try {
        $response = Invoke-WebRequest `
            -Uri "$BaseUrl/mcp-server/http" `
            -Method POST `
            -Headers @{
                Authorization = "Bearer $McpToken"
                Accept = "application/json, text/event-stream"
            } `
            -ContentType "application/json" `
            -Body $initializeRequest `
            -UseBasicParsing `
            -TimeoutSec 15 `
            -ErrorAction Stop
        if ([int]$response.StatusCode -ne 200) {
            throw "MCP access verification returned an unexpected status."
        }
        return 200
    }
    catch {
        $statusCode = Get-HttpErrorStatusCode -ErrorRecord $_
        if ($statusCode -eq 0) {
            throw "n8n MCP endpoint is unavailable or timed out; the Captain stack remains running."
        }
        throw "n8n MCP endpoint returned HTTP $statusCode; the Captain stack remains running."
    }
}

function Invoke-CaptainMcpBootstrap {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl,
        [Parameter(Mandatory = $true)]
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [Parameter(Mandatory = $true)]
        [hashtable]$Values
    )

    Enable-CaptainMcpAccess -BaseUrl $BaseUrl -Session $Session
    $token = Get-CaptainMcpAccessToken -BaseUrl $BaseUrl -Session $Session -Values $Values
    try {
        return Test-CaptainMcpAccessToken -BaseUrl $BaseUrl -McpToken $token
    }
    catch {
        if ($_.Exception.Message -notmatch "HTTP 401") {
            throw
        }
        $token = Get-CaptainMcpAccessToken -BaseUrl $BaseUrl -Session $Session -Values $Values -Rotate
        return Test-CaptainMcpAccessToken -BaseUrl $BaseUrl -McpToken $token
    }
}

function Invoke-Init {
    Assert-LocalContractFiles
    Assert-DockerAvailable
    Assert-LoopbackPortAvailable -Port (Get-CaptainPort)
    Initialize-CaptainEnvironment
    Invoke-ComposeConfigValidation
    Write-Output "Captain n8n environment initialized and Compose contract validated."
}

function Invoke-Start {
    Assert-LocalContractFiles
    Assert-DockerAvailable
    Assert-EnvironmentReady
    Invoke-ComposeConfigValidation

    if (-not (Test-CaptainN8nRunning)) {
        Assert-LoopbackPortAvailable -Port (Get-CaptainPort)
    }
    & docker compose -p captain-n8n-builder --env-file $EnvFile -f $ComposeFile up -d --wait
    if ($LASTEXITCODE -ne 0) {
        throw "Captain n8n readiness failed; inspect only the captain-n8n-builder project."
    }
    Assert-CaptainInventory
    Write-Output "Captain n8n project services are ready with the expected inventory."
}

function Invoke-Bootstrap {
    Assert-LocalContractFiles
    Assert-DockerAvailable
    Assert-EnvironmentReady
    Assert-CaptainInventory

    $port = Get-CaptainPort
    $baseUrl = "http://127.0.0.1:$port"
    $healthStatus = Wait-CaptainN8nHealth -BaseUrl $baseUrl
    $values = Get-EnvValues

    if ($values.ContainsKey("CAPTAIN_N8N_API_KEY") -and -not [string]::IsNullOrWhiteSpace($values["CAPTAIN_N8N_API_KEY"])) {
        $workflowStatus = Test-CaptainApiKey -BaseUrl $baseUrl -ApiKey $values["CAPTAIN_N8N_API_KEY"]
        $securePassword = ConvertTo-SecureStringValue -Value $values["CAPTAIN_N8N_OWNER_PASSWORD"]
        $ownerPassword = ConvertFrom-SecureValue -SecureValue $securePassword
        $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        try {
            $login = Invoke-N8nJsonRequest `
                -Method POST `
                -Uri "$baseUrl/rest/login" `
                -EndpointIdentity "owner login for MCP bootstrap" `
                -Session $session `
                -Body @{ emailOrLdapLoginId = $OwnerEmail; password = $ownerPassword }
            Assert-OwnerResponse -Data $login.Data -EndpointIdentity "owner login for MCP bootstrap"
            $mcpStatus = Invoke-CaptainMcpBootstrap -BaseUrl $baseUrl -Session $session -Values $values
        }
        finally {
            $ownerPassword = $null
            $securePassword.Dispose()
        }
        Write-Output "endpoint=healthz status=$healthStatus"
        Write-Output "endpoint=workflows status=$workflowStatus"
        Write-Output "endpoint=mcp status=$mcpStatus"
        return
    }

    $securePassword = ConvertTo-SecureStringValue -Value $values["CAPTAIN_N8N_OWNER_PASSWORD"]
    $ownerPassword = ConvertFrom-SecureValue -SecureValue $securePassword
    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    try {
        $loginBody = @{
            emailOrLdapLoginId = $OwnerEmail
            password = $ownerPassword
        }
        $login = Invoke-N8nJsonRequest `
            -Method POST `
            -Uri "$baseUrl/rest/login" `
            -EndpointIdentity "owner login" `
            -Session $session `
            -Body $loginBody `
            -AllowedStatusCodes @(200, 400, 401)

        if ($login.StatusCode -eq 200) {
            Assert-OwnerResponse -Data $login.Data -EndpointIdentity "owner login"
        }
        else {
            $setupSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $setupBody = @{
                email = $OwnerEmail
                firstName = "Captain"
                lastName = "Builder"
                password = $ownerPassword
            }
            $setup = Invoke-N8nJsonRequest `
                -Method POST `
                -Uri "$baseUrl/rest/owner/setup" `
                -EndpointIdentity "owner setup" `
                -Session $setupSession `
                -Body $setupBody `
                -AllowedStatusCodes @(200, 400)
            if ($setup.StatusCode -ne 200) {
                throw "Owner setup was rejected. If the owner already exists, restore its matching password in .env.captain-n8n and retry; the Captain stack remains running."
            }
            Assert-OwnerResponse -Data $setup.Data -EndpointIdentity "owner setup"

            $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $login = Invoke-N8nJsonRequest `
                -Method POST `
                -Uri "$baseUrl/rest/login" `
                -EndpointIdentity "owner login after setup" `
                -Session $session `
                -Body $loginBody
            Assert-OwnerResponse -Data $login.Data -EndpointIdentity "owner login after setup"
        }

        $encodedLabel = [Uri]::EscapeDataString($ApiKeyLabel)
        $keyList = Invoke-N8nJsonRequest `
            -Method GET `
            -Uri "$baseUrl/rest/api-keys?ownership=mine&label=$encodedLabel" `
            -EndpointIdentity "API key list" `
            -Session $session `
            -Body $null
        if ($null -eq $keyList.Data -or $null -eq $keyList.Data.PSObject.Properties["items"]) {
            throw "n8n API key list returned an unsupported schema; the Captain stack remains running."
        }

        $matchingKeys = @()
        foreach ($listedKey in @($keyList.Data.items)) {
            if ($null -eq $listedKey -or $null -eq $listedKey.PSObject.Properties["label"]) {
                throw "n8n API key list returned an unsupported item schema; the Captain stack remains running."
            }
            if ($listedKey.label -eq $ApiKeyLabel) {
                $matchingKeys += $listedKey
            }
        }
        if ($matchingKeys.Count -gt 1) {
            throw "Multiple Captain-labelled API keys exist; reconcile them in the Captain n8n UI and retry."
        }

        if ($matchingKeys.Count -eq 1) {
            $keyId = [string]$matchingKeys[0].id
            if ([string]::IsNullOrWhiteSpace($keyId)) {
                throw "n8n API key list omitted the pinned key id; the Captain stack remains running."
            }
            $encodedKeyId = [Uri]::EscapeDataString($keyId)
            $keyResult = Invoke-N8nJsonRequest `
                -Method POST `
                -Uri "$baseUrl/rest/api-keys/$encodedKeyId/rotate" `
                -EndpointIdentity "API key recovery rotation" `
                -Session $session `
                -Body $null
        }
        else {
            $scopeResult = Invoke-N8nJsonRequest `
                -Method GET `
                -Uri "$baseUrl/rest/api-keys/scopes" `
                -EndpointIdentity "API key scopes" `
                -Session $session `
                -Body $null
            $scopes = @($scopeResult.Data)
            if ($scopes.Count -eq 0 -or @($scopes | Where-Object { $_ -isnot [string] -or [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
                throw "n8n API key scopes returned an unsupported schema; the Captain stack remains running."
            }
            $keyResult = Invoke-N8nJsonRequest `
                -Method POST `
                -Uri "$baseUrl/rest/api-keys" `
                -EndpointIdentity "API key creation" `
                -Session $session `
                -Body @{ label = $ApiKeyLabel; scopes = $scopes; expiresAt = $null }
        }

        if ($null -eq $keyResult.Data -or $null -eq $keyResult.Data.PSObject.Properties["rawApiKey"] -or [string]::IsNullOrWhiteSpace($keyResult.Data.rawApiKey)) {
            throw "n8n API key response omitted rawApiKey; the Captain stack remains running."
        }
        $apiKey = [string]$keyResult.Data.rawApiKey
        Set-EnvValue -Name "CAPTAIN_N8N_API_KEY" -Value $apiKey
        $workflowStatus = Test-CaptainApiKey -BaseUrl $baseUrl -ApiKey $apiKey
        $mcpStatus = Invoke-CaptainMcpBootstrap -BaseUrl $baseUrl -Session $session -Values $values

        Write-Output "endpoint=healthz status=$healthStatus"
        Write-Output "endpoint=workflows status=$workflowStatus"
        Write-Output "endpoint=mcp status=$mcpStatus"
    }
    finally {
        $ownerPassword = $null
        $securePassword.Dispose()
    }
}

function Invoke-Status {
    Assert-DockerAvailable
    $statusOutput = & docker ps -a `
        --filter "label=$ProjectLabel" `
        --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect Captain n8n status."
    }
    $statusOutput
}

function Invoke-Stop {
    Assert-LocalContractFiles
    Assert-DockerAvailable
    Assert-EnvironmentReady
    & docker compose -p captain-n8n-builder --env-file $EnvFile -f $ComposeFile stop
    if ($LASTEXITCODE -ne 0) {
        throw "Captain n8n stop failed."
    }
}

switch ($Action) {
    "init" { Invoke-Init }
    "start" { Invoke-Start }
    "bootstrap" { Invoke-Bootstrap }
    "status" { Invoke-Status }
    "stop" { Invoke-Stop }
}
