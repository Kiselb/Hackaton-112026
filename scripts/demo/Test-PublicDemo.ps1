[CmdletBinding()]
param(
    [string]$BaseUri = "http://127.0.0.1:8080",
    [Parameter(Mandatory = $true)]
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = "Stop"
if ($Credential.UserName -ne "jury") { throw "Credential username must be jury." }

function New-BasicHeader {
    param([System.Management.Automation.PSCredential]$Value)
    $plain = $Value.GetNetworkCredential().Password
    $bytes = [Text.Encoding]::UTF8.GetBytes("$($Value.UserName):$plain")
    return @{ Authorization = "Basic $([Convert]::ToBase64String($bytes))" }
}

function Get-Status {
    param([string]$Method, [string]$Uri, [hashtable]$Headers, [string]$Body = $null)
    try {
        $arguments = @{ Method = $Method; Uri = $Uri; Headers = $Headers; UseBasicParsing = $true; TimeoutSec = 20 }
        if ($Method -ne "GET" -and $null -ne $Body) {
            $arguments.Body = $Body
            $arguments.ContentType = "application/json"
        }
        return [int](Invoke-WebRequest @arguments).StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        throw
    }
}

$valid = New-BasicHeader $Credential
$wrongToken = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("jury:definitely-wrong"))
$wrong = @{ Authorization = "Basic $wrongToken" }
$randomId = "00000000-0000-0000-0000-000000000000"
$checks = @(
    @{ Name = "root without credentials"; Method = "GET"; Path = "/"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "root with wrong credentials"; Method = "GET"; Path = "/"; Headers = $wrong; Expected = 401; Body = $null },
    @{ Name = "root with valid credentials"; Method = "GET"; Path = "/"; Headers = $valid; Expected = 200; Body = $null },
    @{ Name = "Swagger UI without credentials"; Method = "GET"; Path = "/docs"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "Swagger UI with wrong credentials"; Method = "GET"; Path = "/docs"; Headers = $wrong; Expected = 401; Body = $null },
    @{ Name = "Swagger UI with valid credentials"; Method = "GET"; Path = "/docs"; Headers = $valid; Expected = 200; Body = $null },
    @{ Name = "ReDoc without credentials"; Method = "GET"; Path = "/redoc"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "ReDoc with wrong credentials"; Method = "GET"; Path = "/redoc"; Headers = $wrong; Expected = 401; Body = $null },
    @{ Name = "ReDoc with valid credentials"; Method = "GET"; Path = "/redoc"; Headers = $valid; Expected = 200; Body = $null },
    @{ Name = "OpenAPI without credentials"; Method = "GET"; Path = "/openapi.json"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "OpenAPI with wrong credentials"; Method = "GET"; Path = "/openapi.json"; Headers = $wrong; Expected = 401; Body = $null },
    @{ Name = "OpenAPI with valid credentials"; Method = "GET"; Path = "/openapi.json"; Headers = $valid; Expected = 200; Body = $null },
    @{ Name = "ask protected"; Method = "POST"; Path = "/api/v1/ask"; Headers = @{}; Expected = 401; Body = '{"question":"test"}' },
    @{ Name = "documents list protected"; Method = "GET"; Path = "/api/v1/documents"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "documents upload protected"; Method = "POST"; Path = "/api/v1/documents"; Headers = @{}; Expected = 401; Body = "" },
    @{ Name = "deactivate protected"; Method = "POST"; Path = "/api/v1/documents/$randomId/deactivate"; Headers = @{}; Expected = 401; Body = "" },
    @{ Name = "restore protected"; Method = "POST"; Path = "/api/v1/documents/$randomId/restore"; Headers = @{}; Expected = 401; Body = "" },
    @{ Name = "traces protected"; Method = "GET"; Path = "/api/v1/admin/traces"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "feedback protected"; Method = "POST"; Path = "/api/v1/traces/$randomId/feedback"; Headers = @{}; Expected = 401; Body = '{}' },
    @{ Name = "health protected"; Method = "GET"; Path = "/healthz"; Headers = @{}; Expected = 401; Body = $null },
    @{ Name = "documents with valid credentials"; Method = "GET"; Path = "/api/v1/documents"; Headers = $valid; Expected = 200; Body = $null },
    @{ Name = "traces with valid credentials"; Method = "GET"; Path = "/api/v1/admin/traces"; Headers = $valid; Expected = 200; Body = $null }
)

foreach ($check in $checks) {
    $status = Get-Status -Method $check.Method -Uri "$BaseUri$($check.Path)" `
        -Headers $check.Headers -Body $check.Body
    if ($status -ne $check.Expected) {
        throw "$($check.Name): expected HTTP $($check.Expected), got $status"
    }
    Write-Host "PASS $($check.Name): HTTP $status"
}

$swagger = Invoke-WebRequest -Uri "$BaseUri/docs" -Headers $valid -UseBasicParsing -TimeoutSec 20
if (-not $swagger.Content.Contains('<div id="swagger-ui">') -or
    -not $swagger.Content.Contains('SwaggerUIBundle') -or
    -not $swagger.Content.Contains("url: '/openapi.json'")) {
    throw "The authenticated /docs response is not the expected same-origin Swagger UI."
}
Write-Host "PASS Swagger UI content and same-origin OpenAPI URL"

$redoc = Invoke-WebRequest -Uri "$BaseUri/redoc" -Headers $valid -UseBasicParsing -TimeoutSec 20
if (-not $redoc.Content.Contains('<redoc spec-url="/openapi.json"></redoc>') -or
    -not $redoc.Content.Contains('redoc.standalone.js')) {
    throw "The authenticated /redoc response is not the expected ReDoc interface."
}
Write-Host "PASS ReDoc content and same-origin OpenAPI URL"

$openApiResponse = Invoke-WebRequest `
    -Uri "$BaseUri/openapi.json" -Headers $valid -UseBasicParsing -TimeoutSec 20
if ([string]$openApiResponse.Headers['Content-Type'] -notmatch '^application/json') {
    throw "The authenticated /openapi.json response is not JSON."
}
try {
    $openApi = ConvertFrom-Json -InputObject $openApiResponse.Content -ErrorAction Stop
} catch {
    throw "The authenticated /openapi.json response contains invalid JSON."
}
if ($null -eq $openApi.PSObject.Properties['openapi'] -or
    $null -eq $openApi.PSObject.Properties['info'] -or
    $null -eq $openApi.info.PSObject.Properties['title'] -or
    $null -eq $openApi.PSObject.Properties['paths'] -or
    @($openApi.paths.PSObject.Properties).Count -lt 1) {
    throw "The authenticated /openapi.json response is missing openapi, info, or paths."
}
Write-Host "PASS OpenAPI JSON structure"

$page = Invoke-WebRequest -Uri "$BaseUri/" -Headers $valid -UseBasicParsing -TimeoutSec 20
$assetMatch = [regex]::Match($page.Content, '(?:src|href)="(/assets/[^"]+)"')
if (-not $assetMatch.Success) { throw "No production asset link found in index.html." }
$assetStatus = Get-Status -Method "GET" -Uri "$BaseUri$($assetMatch.Groups[1].Value)" -Headers $valid
if ($assetStatus -ne 200) { throw "Production asset request returned HTTP $assetStatus." }
Write-Host "PASS production asset: HTTP 200"
Write-Host "Authentication and safe routing checks passed. No document or trace data was changed."
