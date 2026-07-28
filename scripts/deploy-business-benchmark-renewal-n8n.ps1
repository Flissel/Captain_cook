[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Validate", "Deploy")]
    [string]$Action,

    [string]$WorkflowPath,

    [string]$EvidenceDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$defaultWorkflowPath = Join-Path $root "examples/business_benchmark_candidates/customer_renewal_orchestration_team/workflows/renewal_context_read.json"
$selectedWorkflowPath = if ([string]::IsNullOrWhiteSpace($WorkflowPath)) {
    $defaultWorkflowPath
}
else {
    [System.IO.Path]::GetFullPath($WorkflowPath)
}
$environmentFile = Join-Path $root ".env.captain-n8n"
$defaultEvidenceDirectory = Join-Path $root ".captain-cook/business-benchmark"
$selectedEvidenceDirectory = if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
    $defaultEvidenceDirectory
}
else {
    [System.IO.Path]::GetFullPath($EvidenceDirectory)
}
$ownershipEvidencePath = Join-Path $selectedEvidenceDirectory "renewal-context-n8n-ownership.v1.json"
$deploymentReceiptDirectory = Join-Path $selectedEvidenceDirectory "renewal-context-n8n-deployments"
$activationReceiptDirectory = Join-Path $selectedEvidenceDirectory "renewal-context-n8n-activations"
$smokeReceiptDirectory = Join-Path $selectedEvidenceDirectory "renewal-context-n8n-smoke-receipts"
$captainBaseUrl = "http://127.0.0.1:5679"
$workflowName = "Captain Renewal Context Read v1"
$allowedPublishFields = @("name", "nodes", "connections", "settings")

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $hash = [System.Security.Cryptography.SHA256]::HashData($Bytes)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}

function ConvertTo-SortedValue {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $ordered = [ordered]@{}
        foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $ordered[$key] = ConvertTo-SortedValue -Value $Value[$key]
        }
        return $ordered
    }
    if ($Value -is [pscustomobject]) {
        $ordered = [ordered]@{}
        $propertyNames = @($Value.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object)
        foreach ($property in $propertyNames) {
            $ordered[$property] = ConvertTo-SortedValue -Value $Value.PSObject.Properties[$property].Value
        }
        return $ordered
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-SortedValue -Value $_ })
    }
    return $Value
}

function ConvertTo-CanonicalJson {
    param([AllowNull()][object]$Value)

    return (ConvertTo-SortedValue -Value $Value) | ConvertTo-Json -Compress -Depth 32
}

function Get-ObjectSha256 {
    param([Parameter(Mandatory = $true)][object]$Value)

    $json = ConvertTo-CanonicalJson -Value $Value
    return Get-Sha256Hex -Bytes ([System.Text.Encoding]::UTF8.GetBytes($json))
}

function Assert-NoDuplicateJsonProperties {
    param([Parameter(Mandatory = $true)][System.Text.Json.JsonElement]$Element)

    if ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
        $names = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
        foreach ($property in $Element.EnumerateObject()) {
            if (-not $names.Add($property.Name)) {
                throw "Workflow JSON contains a duplicate property."
            }
            Assert-NoDuplicateJsonProperties -Element $property.Value
        }
    }
    elseif ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
        foreach ($item in $Element.EnumerateArray()) {
            Assert-NoDuplicateJsonProperties -Element $item
        }
    }
}

function Test-ForbiddenProperty {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) {
        return $false
    }
    if ($Value -is [pscustomobject]) {
        foreach ($property in $Value.PSObject.Properties) {
            if ($property.Name -match "(?i)credential|secret|token|authorization|api[_-]?key") {
                return $true
            }
            if (Test-ForbiddenProperty -Value $property.Value) {
                return $true
            }
        }
    }
    elseif ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($item in $Value) {
            if (Test-ForbiddenProperty -Value $item) {
                return $true
            }
        }
    }
    return $false
}

function Read-AndValidateWorkflow {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Canonical renewal workflow is missing."
    }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -eq 0 -or $bytes.Length -gt 262144) {
        throw "Canonical renewal workflow size is invalid."
    }
    try {
        $jsonText = [System.Text.Encoding]::UTF8.GetString($bytes)
        $document = [System.Text.Json.JsonDocument]::Parse($jsonText)
        try {
            Assert-NoDuplicateJsonProperties -Element $document.RootElement
        }
        finally {
            $document.Dispose()
        }
        $workflow = $jsonText | ConvertFrom-Json -Depth 32
    }
    catch {
        throw "Canonical renewal workflow JSON is invalid."
    }

    $expectedTopLevel = @("active", "connections", "contract", "name", "nodes", "settings")
    $actualTopLevel = @($workflow.PSObject.Properties.Name | Sort-Object)
    if (($actualTopLevel -join "|") -cne ($expectedTopLevel -join "|")) {
        throw "Canonical renewal workflow top-level contract is invalid."
    }
    if ($workflow.name -cne $workflowName -or $workflow.active -ne $false) {
        throw "Canonical renewal workflow identity is invalid."
    }
    $contractFields = @($workflow.contract.PSObject.Properties.Name | Sort-Object)
    $expectedContractFields = @(
        "allowed_partitions",
        "effect",
        "idempotency",
        "intent",
        "mutation_operations",
        "schema"
    )
    if (($contractFields -join "|") -cne ($expectedContractFields -join "|")) {
        throw "Canonical renewal workflow execution contract is invalid."
    }
    $partitions = @($workflow.contract.allowed_partitions)
    if (
        $workflow.contract.schema -cne "captain.n8n-read-only-workflow.v1" -or
        $workflow.contract.intent -cne "n8n" -or
        $workflow.contract.effect -cne "read_only" -or
        $workflow.contract.idempotency -cne "required" -or
        ($partitions -join "|") -cne "ordinary|boundary" -or
        @($workflow.contract.mutation_operations).Count -ne 0
    ) {
        throw "Canonical renewal workflow read-only contract is invalid."
    }
    if (@($workflow.nodes).Count -ne 2) {
        throw "Canonical renewal workflow node inventory is invalid."
    }
    $nodeTypes = @($workflow.nodes | ForEach-Object { $_.type } | Sort-Object)
    $expectedNodeTypes = @("n8n-nodes-base.code", "n8n-nodes-base.executeWorkflowTrigger")
    if (($nodeTypes -join "|") -cne ($expectedNodeTypes -join "|")) {
        throw "Canonical renewal workflow contains an unauthorized node type."
    }
    if (Test-ForbiddenProperty -Value $workflow) {
        throw "Canonical renewal workflow contains a forbidden sensitive field."
    }
    $settingNames = @($workflow.settings.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object)
    if (
        ($settingNames -join "|") -cne "availableInMCP|executionOrder" -or
        $workflow.settings.availableInMCP -ne $true -or
        $workflow.settings.executionOrder -cne "v1"
    ) {
        throw "Canonical renewal workflow MCP availability settings are invalid."
    }

    $publishPayload = [ordered]@{}
    foreach ($field in $allowedPublishFields) {
        $publishPayload[$field] = $workflow.$field
    }
    return [pscustomobject]@{
        Workflow = $workflow
        PublishPayload = $publishPayload
        CanonicalSha256 = Get-Sha256Hex -Bytes $bytes
        PublishedSha256 = Get-ObjectSha256 -Value $publishPayload
    }
}

function Get-EnvironmentValues {
    $values = @{}
    if (Test-Path -LiteralPath $environmentFile -PathType Leaf) {
        foreach ($line in [System.IO.File]::ReadAllLines($environmentFile)) {
            if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
                continue
            }
            if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
                throw "Captain n8n environment file is invalid."
            }
            if (-not $values.ContainsKey($Matches[1])) {
                $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }
    foreach ($name in @("CAPTAIN_N8N_API_KEY", "CAPTAIN_N8N_MCP_TOKEN", "CAPTAIN_N8N_PORT")) {
        $processValue = [System.Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($processValue)) {
            $values[$name] = $processValue.Trim()
        }
    }
    return $values
}

function Invoke-CaptainRest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST", "PUT")][string]$Verb,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [AllowNull()][object]$Body
    )

    if (-not $Path.StartsWith("/api/v1/", [System.StringComparison]::Ordinal)) {
        throw "Captain n8n REST path is unauthorized."
    }
    $parameters = @{
        Uri = "$captainBaseUrl$Path"
        Method = $Verb
        Headers = @{ "X-N8N-API-KEY" = $ApiKey }
        UseBasicParsing = $true
        TimeoutSec = 20
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Compress -Depth 32
        $parameters.ContentType = "application/json"
    }
    try {
        $response = Invoke-WebRequest @parameters
        if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) {
            throw "unexpected status"
        }
        return $response.Content | ConvertFrom-Json -Depth 32
    }
    catch {
        throw "Captain n8n REST request failed closed."
    }
}

function Get-AllWorkflows {
    param([Parameter(Mandatory = $true)][string]$ApiKey)

    $results = [System.Collections.Generic.List[object]]::new()
    $cursor = $null
    do {
        $path = "/api/v1/workflows?limit=250"
        if (-not [string]::IsNullOrWhiteSpace([string]$cursor)) {
            $path += "&cursor=$([uri]::EscapeDataString([string]$cursor))"
        }
        $page = Invoke-CaptainRest -Verb GET -Path $path -ApiKey $ApiKey -Body $null
        $dataProperty = $page.PSObject.Properties["data"]
        if ($null -eq $dataProperty) {
            throw "Captain n8n workflow inventory schema is invalid."
        }
        if ($null -ne $dataProperty.Value) {
            foreach ($workflow in @($dataProperty.Value)) {
                if ($null -eq $workflow -or $workflow -isnot [pscustomobject]) {
                    throw "Captain n8n workflow inventory item is invalid."
                }
                $results.Add($workflow)
            }
        }
        $cursor = if ($null -ne $page.PSObject.Properties["nextCursor"]) { $page.nextCursor } else { $null }
    } while (-not [string]::IsNullOrWhiteSpace([string]$cursor))
    return @($results)
}

function Get-WorkflowId {
    param([Parameter(Mandatory = $true)][object]$Value)

    foreach ($field in @("id", "workflowId", "workflow_id")) {
        $property = $Value.PSObject.Properties[$field]
        if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            return ([string]$property.Value).Trim()
        }
    }
    if ($null -ne $Value.PSObject.Properties["data"] -and $null -ne $Value.data) {
        return Get-WorkflowId -Value $Value.data
    }
    throw "Captain n8n workflow response omitted its identity."
}

function Select-TemplateShape {
    param(
        [AllowNull()][object]$Remote,
        [AllowNull()][object]$Template
    )

    if ($Template -is [pscustomobject] -or $Template -is [System.Collections.IDictionary]) {
        if ($Remote -isnot [pscustomobject] -and $Remote -isnot [System.Collections.IDictionary]) {
            throw "Captain n8n workflow changed a published object shape."
        }
        $result = [ordered]@{}
        $names = if ($Template -is [System.Collections.IDictionary]) {
            @($Template.Keys | ForEach-Object { [string]$_ })
        }
        else {
            @($Template.PSObject.Properties.Name)
        }
        foreach ($name in $names) {
            $remoteProperty = if ($Remote -is [System.Collections.IDictionary]) {
                if ($Remote.Contains($name)) { $Remote[$name] } else { $null }
            }
            else {
                $property = $Remote.PSObject.Properties[$name]
                if ($null -ne $property) { $property.Value } else { $null }
            }
            $hasRemoteProperty = if ($Remote -is [System.Collections.IDictionary]) {
                $Remote.Contains($name)
            }
            else {
                $null -ne $Remote.PSObject.Properties[$name]
            }
            if (-not $hasRemoteProperty) {
                throw "Captain n8n workflow omitted published state."
            }
            $templateProperty = if ($Template -is [System.Collections.IDictionary]) {
                $Template[$name]
            }
            else {
                $Template.$name
            }
            $result[$name] = Select-TemplateShape -Remote $remoteProperty -Template $templateProperty
        }
        return $result
    }
    if ($Template -is [System.Collections.IEnumerable] -and $Template -isnot [string]) {
        if ($Remote -isnot [System.Collections.IEnumerable] -or $Remote -is [string]) {
            throw "Captain n8n workflow changed a published array shape."
        }
        $remoteItems = @($Remote)
        $templateItems = @($Template)
        if ($remoteItems.Count -ne $templateItems.Count) {
            throw "Captain n8n workflow changed a published array length."
        }
        $result = @()
        for ($index = 0; $index -lt $templateItems.Count; $index++) {
            $result += ,(Select-TemplateShape -Remote $remoteItems[$index] -Template $templateItems[$index])
        }
        return $result
    }
    return $Remote
}

function Get-ComparableRemotePayload {
    param(
        [Parameter(Mandatory = $true)][object]$Remote,
        [Parameter(Mandatory = $true)][object]$PublishedTemplate
    )

    if (Test-ForbiddenProperty -Value $Remote.nodes) {
        throw "Captain n8n workflow contains unauthorized sensitive node state."
    }
    foreach ($field in $allowedPublishFields) {
        if ($null -eq $Remote.PSObject.Properties[$field]) {
            throw "Captain n8n workflow omitted required publish state."
        }
    }
    $settings = [ordered]@{}
    foreach ($name in @($PublishedTemplate.settings.PSObject.Properties | ForEach-Object { $_.Name })) {
        if ($null -eq $Remote.settings.PSObject.Properties[$name]) {
            throw "Captain n8n workflow omitted a published setting."
        }
        $settings[$name] = $Remote.settings.PSObject.Properties[$name].Value
    }
    return [ordered]@{
        name = $Remote.name
        nodes = $Remote.nodes
        connections = $Remote.connections
        settings = $settings
    }
}

function Read-JsonEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json -Depth 32
    }
    catch {
        throw "Captain n8n local evidence is invalid."
    }
}

function Write-ImmutableJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    $json = ($Value | ConvertTo-Json -Depth 32) + "`n"
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($json)
    $directory = Split-Path -Parent $Path
    $null = New-Item -ItemType Directory -Path $directory -Force
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $existing = [System.IO.File]::ReadAllBytes($Path)
        if (-not [System.Linq.Enumerable]::SequenceEqual[byte]($existing, $bytes)) {
            throw "Captain n8n immutable evidence conflicts with existing state."
        }
        return
    }
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length) }
        finally { $stream.Dispose() }
    }
    catch [System.IO.IOException] {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw }
        $existing = [System.IO.File]::ReadAllBytes($Path)
        if (-not [System.Linq.Enumerable]::SequenceEqual[byte]($existing, $bytes)) {
            throw "Captain n8n immutable evidence conflicts with concurrent state."
        }
    }
}

function Get-OwnershipBindingSha256 {
    param([Parameter(Mandatory = $true)][string]$WorkflowId)

    return Get-Sha256Hex -Bytes ([System.Text.Encoding]::UTF8.GetBytes(
        "captain.business-benchmark-renewal-n8n.v1|$workflowName|$WorkflowId"
    ))
}

function Write-OwnershipEvidence {
    param([Parameter(Mandatory = $true)][string]$WorkflowId)

    $evidence = [ordered]@{
        schema = "captain.business-benchmark-renewal-n8n-ownership.v1"
        ownership = "captain"
        endpoint_id = "captain-n8n-local-5679"
        workflow_name = $workflowName
        workflow_id = $WorkflowId
        ownership_binding_sha256 = Get-OwnershipBindingSha256 -WorkflowId $WorkflowId
    }
    Write-ImmutableJson -Path $ownershipEvidencePath -Value $evidence
    return $evidence
}

function Assert-OwnershipBinding {
    param(
        [Parameter(Mandatory = $true)][object]$Evidence,
        [Parameter(Mandatory = $true)][string]$WorkflowId
    )

    if (
        $Evidence.schema -cne "captain.business-benchmark-renewal-n8n-ownership.v1" -or
        $Evidence.ownership -cne "captain" -or
        $Evidence.endpoint_id -cne "captain-n8n-local-5679" -or
        $Evidence.workflow_name -cne $workflowName -or
        ([string]$Evidence.workflow_id) -cne $WorkflowId -or
        $Evidence.ownership_binding_sha256 -cne (Get-OwnershipBindingSha256 -WorkflowId $WorkflowId)
    ) {
        throw "Captain n8n ownership binding did not match; refusing workflow mutation."
    }
}

function Write-DeploymentReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][object]$Validated
    )

    $receipt = [ordered]@{
        schema = "captain.business-benchmark-renewal-n8n-deployment-receipt.v1"
        ownership_binding_sha256 = Get-OwnershipBindingSha256 -WorkflowId $WorkflowId
        workflow_id = $WorkflowId
        workflow_name = $workflowName
        canonical_sha256 = $Validated.CanonicalSha256
        published_sha256 = $Validated.PublishedSha256
        published_payload = $Validated.PublishPayload
        verification = "provider_read_back_matched"
    }
    $path = Join-Path $deploymentReceiptDirectory "$($Validated.PublishedSha256).json"
    Write-ImmutableJson -Path $path -Value $receipt
    return $receipt
}

function Find-MatchingDeploymentReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][object]$Remote
    )

    if (-not (Test-Path -LiteralPath $deploymentReceiptDirectory -PathType Container)) {
        throw "Captain n8n remote workflow has no matching immutable deployment receipt."
    }
    $matches = [System.Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-ChildItem -LiteralPath $deploymentReceiptDirectory -Filter "*.json" -File)) {
        $receipt = Read-JsonEvidence -Path $file.FullName
        if (
            $null -eq $receipt -or
            $receipt.schema -cne "captain.business-benchmark-renewal-n8n-deployment-receipt.v1" -or
            $receipt.workflow_id -cne $WorkflowId -or
            $receipt.workflow_name -cne $workflowName -or
            $receipt.ownership_binding_sha256 -cne (Get-OwnershipBindingSha256 -WorkflowId $WorkflowId) -or
            $receipt.verification -cne "provider_read_back_matched" -or
            $null -eq $receipt.PSObject.Properties["published_payload"] -or
            $file.BaseName -cne $receipt.published_sha256
        ) {
            continue
        }
        try {
            $projected = Get-ComparableRemotePayload -Remote $Remote -PublishedTemplate $receipt.published_payload
            if ((Get-ObjectSha256 -Value $projected) -ceq $receipt.published_sha256) {
                $matches.Add($receipt)
            }
        }
        catch {
            continue
        }
    }
    if ($matches.Count -ne 1) {
        throw "Captain n8n remote workflow has no unambiguous immutable deployment receipt."
    }
    return $matches[0]
}

function Write-ActivationReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][string]$PublishedSha256
    )

    $receipt = [ordered]@{
        schema = "captain.business-benchmark-renewal-n8n-activation-receipt.v1"
        ownership_binding_sha256 = Get-OwnershipBindingSha256 -WorkflowId $WorkflowId
        workflow_id = $WorkflowId
        workflow_name = $workflowName
        published_sha256 = $PublishedSha256
        status = "active"
    }
    $path = Join-Path $activationReceiptDirectory "$PublishedSha256.json"
    Write-ImmutableJson -Path $path -Value $receipt
    return $receipt
}

function Ensure-WorkflowActivation {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][object]$Validated,
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [Parameter(Mandatory = $true)][object]$Remote
    )

    $receiptPath = Join-Path $activationReceiptDirectory "$($Validated.PublishedSha256).json"
    $existing = Read-JsonEvidence -Path $receiptPath
    if ($null -ne $existing) {
        if (
            $existing.schema -cne "captain.business-benchmark-renewal-n8n-activation-receipt.v1" -or
            $existing.workflow_id -cne $WorkflowId -or
            $existing.workflow_name -cne $workflowName -or
            $existing.published_sha256 -cne $Validated.PublishedSha256 -or
            $existing.ownership_binding_sha256 -cne (Get-OwnershipBindingSha256 -WorkflowId $WorkflowId) -or
            $existing.status -cne "active" -or
            $null -eq $Remote.PSObject.Properties["active"] -or
            $Remote.active -ne $true
        ) {
            throw "Captain n8n activation receipt conflicts with remote state."
        }
        return $existing
    }

    # Activation is effectful and its response may be lost. If the provider is
    # already active, recover only after independently revalidating the exact
    # Captain ownership and published payload; never repeat the POST blindly.
    if ($null -ne $Remote.PSObject.Properties["active"] -and $Remote.active -eq $true) {
        $ownership = Read-JsonEvidence -Path $ownershipEvidencePath
        if ($null -eq $ownership) {
            throw "Captain n8n active workflow has no ownership evidence."
        }
        Assert-OwnershipBinding -Evidence $ownership -WorkflowId $WorkflowId
        $remoteDigest = Get-ObjectSha256 -Value (Get-ComparableRemotePayload -Remote $Remote -PublishedTemplate $Validated.PublishPayload)
        if ($remoteDigest -cne $Validated.PublishedSha256) {
            throw "Captain n8n active workflow digest did not match during recovery."
        }
        return Write-ActivationReceipt -WorkflowId $WorkflowId -PublishedSha256 $Validated.PublishedSha256
    }

    $activated = Invoke-CaptainRest -Verb POST -Path "/api/v1/workflows/$([uri]::EscapeDataString($WorkflowId))/activate" -ApiKey $ApiKey -Body ([ordered]@{})
    if (
        (Get-WorkflowId -Value $activated) -cne $WorkflowId -or
        $null -eq $activated.PSObject.Properties["active"] -or
        $activated.active -ne $true
    ) {
        throw "Captain n8n workflow activation was not confirmed."
    }
    $confirmed = Invoke-CaptainRest -Verb GET -Path "/api/v1/workflows/$([uri]::EscapeDataString($WorkflowId))" -ApiKey $ApiKey -Body $null
    if (
        $null -eq $confirmed.PSObject.Properties["active"] -or
        $confirmed.active -ne $true -or
        (Get-ObjectSha256 -Value (Get-ComparableRemotePayload -Remote $confirmed -PublishedTemplate $Validated.PublishPayload)) -cne $Validated.PublishedSha256
    ) {
        throw "Captain n8n active workflow readback did not match."
    }
    return Write-ActivationReceipt -WorkflowId $WorkflowId -PublishedSha256 $Validated.PublishedSha256
}

function ConvertFrom-McpContent {
    param([Parameter(Mandatory = $true)][string]$Content)

    try {
        return $Content | ConvertFrom-Json -Depth 64
    }
    catch {
        $dataLines = @(
            $Content.Replace("`r`n", "`n").Split("`n") |
                Where-Object { $_.StartsWith("data:") } |
                ForEach-Object { $_.Substring(5).TrimStart() }
        )
        foreach ($line in $dataLines) {
            try {
                return $line | ConvertFrom-Json -Depth 64
            }
            catch {
                continue
            }
        }
        throw "Captain n8n MCP response schema is invalid."
    }
}

function ConvertFrom-NestedJson {
    param([AllowNull()][object]$Value)

    $current = $Value
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        if ($current -isnot [string]) {
            break
        }
        try {
            $current = $current | ConvertFrom-Json -Depth 64
        }
        catch {
            break
        }
    }
    return $current
}

function Test-McpStructuredError {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return $false }
    if ($Value -is [pscustomobject]) {
        $status = $Value.PSObject.Properties["status"]
        if ($null -ne $status -and ([string]$status.Value).Trim().ToLowerInvariant() -in @("error", "failed", "failure")) {
            return $true
        }
        $errorProperty = $Value.PSObject.Properties["error"]
        if ($null -ne $errorProperty -and $null -ne $errorProperty.Value) {
            if ($errorProperty.Value -isnot [string] -or -not [string]::IsNullOrWhiteSpace($errorProperty.Value)) {
                return $true
            }
        }
        foreach ($property in $Value.PSObject.Properties) {
            if (Test-McpStructuredError -Value $property.Value) { return $true }
        }
    }
    elseif ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($item in $Value) {
            if (Test-McpStructuredError -Value $item) { return $true }
        }
    }
    return $false
}

function Invoke-McpTool {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][object]$Arguments,
        [Parameter(Mandatory = $true)][string]$McpToken,
        [Parameter(Mandatory = $true)][string]$CallId
    )

    $request = [ordered]@{
        jsonrpc = "2.0"
        id = $CallId
        method = "tools/call"
        params = [ordered]@{ name = $Name; arguments = $Arguments }
    }
    try {
        $response = Invoke-WebRequest -Uri "$captainBaseUrl/mcp-server/http" -Method POST -Headers @{
            Authorization = "Bearer $McpToken"
            Accept = "application/json, text/event-stream"
        } -Body ($request | ConvertTo-Json -Compress -Depth 32) -ContentType "application/json" -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
    }
    catch {
        throw "Captain n8n MCP request failed closed."
    }
    $envelope = ConvertFrom-McpContent -Content $response.Content
    if ($envelope.jsonrpc -cne "2.0" -or ([string]$envelope.id) -cne $CallId -or $null -ne $envelope.PSObject.Properties["error"]) {
        throw "Captain n8n MCP response binding is invalid."
    }
    $result = $envelope.result
    $isErrorProperty = if ($null -ne $result) { $result.PSObject.Properties["isError"] } else { $null }
    if ($null -eq $result -or ($null -ne $isErrorProperty -and $isErrorProperty.Value -eq $true)) {
        throw "Captain n8n MCP tool failed."
    }
    if ($null -ne $result.PSObject.Properties["structuredContent"] -and $null -ne $result.structuredContent) {
        $structured = ConvertFrom-NestedJson -Value $result.structuredContent
        if (Test-McpStructuredError -Value $structured) {
            throw "Captain n8n MCP tool reported an error."
        }
        return $structured
    }
    $texts = @(
        @($result.content) |
            Where-Object { $_.type -eq "text" -and $_.text -is [string] } |
            ForEach-Object { $_.text }
    )
    if ($texts.Count -eq 0) {
        throw "Captain n8n MCP tool returned no structured output."
    }
    $structured = ConvertFrom-NestedJson -Value ($texts -join "`n")
    if (Test-McpStructuredError -Value $structured) {
        throw "Captain n8n MCP tool reported an error."
    }
    return $structured
}

function Find-UniqueValues {
    param(
        [AllowNull()][object]$Value,
        [Parameter(Mandatory = $true)][string[]]$Names
    )

    $found = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    function Visit([AllowNull()][object]$Current) {
        if ($null -eq $Current) { return }
        if ($Current -is [pscustomobject]) {
            foreach ($property in $Current.PSObject.Properties) {
                if ($Names -contains $property.Name -and $null -ne $property.Value) {
                    $text = ([string]$property.Value).Trim()
                    if ($text) { $null = $found.Add($text) }
                }
                else { Visit -Current $property.Value }
            }
        }
        elseif ($Current -is [System.Collections.IEnumerable] -and $Current -isnot [string]) {
            foreach ($item in $Current) { Visit -Current $item }
        }
    }
    Visit -Current $Value
    return @($found)
}

function Test-SmokeOutput {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$IdempotencyKey
    )

    if ($Value -is [pscustomobject]) {
        $operation = $Value.PSObject.Properties["operation"]
        $idempotency = $Value.PSObject.Properties["idempotency_key"]
        $status = $Value.PSObject.Properties["status"]
        $facts = $Value.PSObject.Properties["facts"]
        if (
            $null -ne $operation -and $operation.Value -ceq "read_renewal_context" -and
            $null -ne $idempotency -and $idempotency.Value -ceq $IdempotencyKey -and
            $null -ne $status -and $status.Value -ceq "read" -and
            $null -ne $facts -and @($facts.Value).Count -eq 4
        ) { return $true }
        foreach ($property in $Value.PSObject.Properties) {
            if (Test-SmokeOutput -Value $property.Value -IdempotencyKey $IdempotencyKey) { return $true }
        }
    }
    elseif ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($item in $Value) {
            if (Test-SmokeOutput -Value $item -IdempotencyKey $IdempotencyKey) { return $true }
        }
    }
    return $false
}

function Invoke-ReadOnlySmoke {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][string]$McpToken,
        [Parameter(Mandatory = $true)][string]$CanonicalSha256
    )

    $idempotencyKey = "captain-renewal-smoke-$($CanonicalSha256.Substring(0, 32))"
    $inputBody = [ordered]@{
        operation = "read_renewal_context"
        idempotency_key = $idempotencyKey
        evidence_partition = "ordinary"
        synthetic_subject_id = "subject-smoke01"
        commercial_snapshot = [ordered]@{
            renewal_window = "synthetic-90d"
            engagement_band = "synthetic-medium"
            commercial_evidence_state = "synthetic-complete"
            consent_state = "synthetic-consented"
        }
    }
    $executeId = "captain-renewal-deploy-execute-$($CanonicalSha256.Substring(0, 24))"
    $execute = Invoke-McpTool -Name "execute_workflow" -Arguments ([ordered]@{
        workflowId = $WorkflowId
        executionMode = "production"
        inputs = [ordered]@{
            type = "webhook"
            webhookData = [ordered]@{
                method = "POST"
                body = $inputBody
                headers = @{}
                query = @{}
            }
        }
    }) -McpToken $McpToken -CallId $executeId
    $executionIds = @(Find-UniqueValues -Value $execute -Names @("executionId", "execution_id", "id"))
    if ($executionIds.Count -ne 1) {
        throw "Captain n8n MCP execute result omitted an unambiguous execution identity."
    }
    $executionId = $executionIds[0]
    $terminal = $null
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        $terminal = Invoke-McpTool -Name "get_execution" -Arguments ([ordered]@{
            workflowId = $WorkflowId
            executionId = $executionId
            includeData = $true
        }) -McpToken $McpToken -CallId "captain-renewal-deploy-evidence-$attempt-$($CanonicalSha256.Substring(0, 16))"
        $observedExecutionIds = @(Find-UniqueValues -Value $terminal -Names @("executionId", "execution_id", "id"))
        $observedWorkflowIds = @(Find-UniqueValues -Value $terminal -Names @("workflowId", "workflow_id"))
        if ($observedExecutionIds -notcontains $executionId -or $observedWorkflowIds -notcontains $WorkflowId) {
            throw "Captain n8n MCP execution evidence identity did not match."
        }
        if (Test-SmokeOutput -Value $terminal -IdempotencyKey $idempotencyKey) {
            return [pscustomobject]@{
                ExecutionId = $executionId
                InputSha256 = Get-ObjectSha256 -Value $inputBody
                OutputSha256 = Get-ObjectSha256 -Value $terminal
            }
        }
        if ($attempt -lt 15) { Start-Sleep -Milliseconds 500 }
    }
    throw "Captain n8n read-only smoke execution did not produce the typed output."
}

function Write-SmokeReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][object]$Validated,
        [Parameter(Mandatory = $true)][object]$Smoke
    )

    $receiptId = Get-Sha256Hex -Bytes ([System.Text.Encoding]::UTF8.GetBytes(
        "captain.business-benchmark-renewal-n8n-smoke.v1|$WorkflowId|$($Smoke.ExecutionId)|$($Smoke.InputSha256)|$($Smoke.OutputSha256)"
    ))
    $receipt = [ordered]@{
        schema = "captain.business-benchmark-renewal-n8n-smoke-receipt.v1"
        receipt_sha256 = $receiptId
        ownership_binding_sha256 = Get-OwnershipBindingSha256 -WorkflowId $WorkflowId
        workflow_name = $workflowName
        workflow_id = $WorkflowId
        canonical_sha256 = $Validated.CanonicalSha256
        published_sha256 = $Validated.PublishedSha256
        status = "succeeded"
        effect = "read_only"
        execution_id = $Smoke.ExecutionId
        input_sha256 = $Smoke.InputSha256
        output_sha256 = $Smoke.OutputSha256
        tools = @("execute_workflow", "get_execution")
    }
    $path = Join-Path $smokeReceiptDirectory "$receiptId.json"
    Write-ImmutableJson -Path $path -Value $receipt
    return $receipt
}

$validated = Read-AndValidateWorkflow -Path $selectedWorkflowPath
if ($Action -eq "Validate") {
    [ordered]@{
        schema = "captain.business-benchmark-renewal-n8n-validation.v1"
        status = "validated"
        workflow_name = $workflowName
        canonical_sha256 = $validated.CanonicalSha256
    } | ConvertTo-Json -Compress
    exit 0
}

if ([System.IO.Path]::GetFullPath($selectedWorkflowPath) -cne [System.IO.Path]::GetFullPath($defaultWorkflowPath)) {
    throw "Deployment accepts only the repository canonical renewal workflow."
}
$environment = Get-EnvironmentValues
if ($environment["CAPTAIN_N8N_PORT"] -and $environment["CAPTAIN_N8N_PORT"] -cne "5679") {
    throw "Captain n8n deployment requires the isolated local port 5679."
}
$apiKey = [string]$environment["CAPTAIN_N8N_API_KEY"]
$mcpToken = [string]$environment["CAPTAIN_N8N_MCP_TOKEN"]
if ([string]::IsNullOrWhiteSpace($apiKey) -or [string]::IsNullOrWhiteSpace($mcpToken)) {
    throw "Captain n8n deployment prerequisites are incomplete."
}
try {
    $health = Invoke-WebRequest -Uri "$captainBaseUrl/healthz" -Method GET -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    if ([int]$health.StatusCode -ne 200) { throw "not healthy" }
}
catch {
    throw "Captain n8n health preflight failed closed."
}

$inventory = @(Get-AllWorkflows -ApiKey $apiKey)
$matches = @($inventory | Where-Object { $_.name -ceq $workflowName })
if ($matches.Count -gt 1) {
    throw "Captain n8n contains duplicate managed workflow names; refusing mutation."
}
$ownershipEvidence = Read-JsonEvidence -Path $ownershipEvidencePath
$deploymentStatus = "unchanged"
if ($matches.Count -eq 0) {
    if ($null -ne $ownershipEvidence) {
        throw "Captain n8n ownership evidence points to a missing workflow; refusing replacement."
    }
    $stored = Invoke-CaptainRest -Verb POST -Path "/api/v1/workflows" -ApiKey $apiKey -Body $validated.PublishPayload
    $workflowId = Get-WorkflowId -Value $stored
    # The successful provider identity is the ownership boundary. Persist it
    # before any readback, activation, or MCP call so transport loss can resume.
    $ownershipEvidence = Write-OwnershipEvidence -WorkflowId $workflowId
    $deploymentStatus = "created"
}
else {
    $workflowId = Get-WorkflowId -Value $matches[0]
    if ($null -eq $ownershipEvidence) {
        throw "Existing workflow with the managed name has no Captain ownership binding; refusing mutation."
    }
    $remote = Invoke-CaptainRest -Verb GET -Path "/api/v1/workflows/$([uri]::EscapeDataString($workflowId))" -ApiKey $apiKey -Body $null
    Assert-OwnershipBinding -Evidence $ownershipEvidence -WorkflowId $workflowId
    $receiptFiles = @(
        if (Test-Path -LiteralPath $deploymentReceiptDirectory -PathType Container) {
            Get-ChildItem -LiteralPath $deploymentReceiptDirectory -Filter "*.json" -File
        }
    )
    if ($receiptFiles.Count -eq 0) {
        # Recovery is allowed only for the exact POST-owned workflow and the
        # currently canonical payload. No mutation is performed on this path.
        $recoveredPayload = Get-ComparableRemotePayload -Remote $remote -PublishedTemplate $validated.PublishPayload
        $remotePublishedSha256 = Get-ObjectSha256 -Value $recoveredPayload
        if ($remotePublishedSha256 -cne $validated.PublishedSha256) {
            throw "Captain n8n POST recovery readback did not match the canonical workflow."
        }
        $deploymentStatus = "recovered"
    }
    else {
        $remoteReceipt = Find-MatchingDeploymentReceipt -WorkflowId $workflowId -Remote $remote
        $remotePublishedSha256 = $remoteReceipt.published_sha256
    }
    if ($remotePublishedSha256 -cne $validated.PublishedSha256) {
        $stored = Invoke-CaptainRest -Verb PUT -Path "/api/v1/workflows/$([uri]::EscapeDataString($workflowId))" -ApiKey $apiKey -Body $validated.PublishPayload
        if ((Get-WorkflowId -Value $stored) -cne $workflowId) {
            throw "Captain n8n update changed the workflow identity."
        }
        $deploymentStatus = "updated"
    }
}

$deployed = Invoke-CaptainRest -Verb GET -Path "/api/v1/workflows/$([uri]::EscapeDataString($workflowId))" -ApiKey $apiKey -Body $null
if ((Get-WorkflowId -Value $deployed) -cne $workflowId -or $deployed.name -cne $workflowName) {
    throw "Captain n8n deployed workflow identity did not match."
}
$deployedDigest = Get-ObjectSha256 -Value (Get-ComparableRemotePayload -Remote $deployed -PublishedTemplate $validated.PublishPayload)
if ($deployedDigest -cne $validated.PublishedSha256) {
    throw "Captain n8n deployed workflow digest did not match."
}
$ownershipEvidence = Write-OwnershipEvidence -WorkflowId $workflowId
$deploymentReceipt = Write-DeploymentReceipt -WorkflowId $workflowId -Validated $validated
$activationReceipt = Ensure-WorkflowActivation -WorkflowId $workflowId -Validated $validated -ApiKey $apiKey -Remote $deployed
$smoke = Invoke-ReadOnlySmoke -WorkflowId $workflowId -McpToken $mcpToken -CanonicalSha256 $validated.CanonicalSha256
$smokeReceipt = Write-SmokeReceipt -WorkflowId $workflowId -Validated $validated -Smoke $smoke
[ordered]@{
    schema = "captain.business-benchmark-renewal-n8n-deploy-result.v1"
    status = "ready"
    deployment_status = $deploymentStatus
    workflow_name = $workflowName
    workflow_id = $workflowId
    canonical_sha256 = $validated.CanonicalSha256
    published_sha256 = $validated.PublishedSha256
    smoke_status = "succeeded"
    smoke_execution_id = $smoke.ExecutionId
    ownership_evidence = "renewal-context-n8n-ownership.v1.json"
    deployment_receipt = "renewal-context-n8n-deployments/$($validated.PublishedSha256).json"
    activation_receipt = "renewal-context-n8n-activations/$($validated.PublishedSha256).json"
    smoke_receipt = "renewal-context-n8n-smoke-receipts/$($smokeReceipt.receipt_sha256).json"
} | ConvertTo-Json -Compress
