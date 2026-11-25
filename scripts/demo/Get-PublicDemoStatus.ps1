[CmdletBinding()]
param(
    [string]$TailscalePath = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PublicDemo.Common.ps1")

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Import-DemoDotEnv (Join-Path $repoRoot ".env")

$proxyPort = if ($env:DEMO_PROXY_PORT) { [int]$env:DEMO_PROXY_PORT } else { 8080 }
$backendPort = if ($env:DEMO_BACKEND_PORT) { [int]$env:DEMO_BACKEND_PORT } else { 8000 }
$expectedTarget = "http://127.0.0.1:$proxyPort"
$expectedUrl = ConvertTo-DemoHttpsOrigin ([string]$env:TAILSCALE_FUNNEL_URL)
$statePath = Get-DemoStatePath $repoRoot
$state = Read-DemoState $statePath $repoRoot

$backendPid = Get-DemoListenerOwner $backendPort
$caddyPid = Get-DemoListenerOwner $proxyPort
$backendHealth = Get-DemoHttpStatus "http://127.0.0.1:$backendPort/healthz" 5
$backendReadiness = Get-DemoHttpStatus "http://127.0.0.1:$backendPort/readyz" 15
$caddyProtection = Get-DemoHttpStatus "http://127.0.0.1:$proxyPort/healthz" 5

$backendManaged = $false
$caddyManaged = $false
if ($null -ne $state -and $null -ne $backendPid -and $null -ne $state.backend) {
    $backendIdentity = Test-DemoProcessIdentity $state.backend (Get-DemoProcessSnapshot $backendPid)
    $backendManaged = ([int]$state.backend.pid -eq $backendPid -and [bool]$backendIdentity.Valid)
}
if ($null -ne $state -and $null -ne $caddyPid -and $null -ne $state.caddy) {
    $caddyIdentity = Test-DemoProcessIdentity $state.caddy (Get-DemoProcessSnapshot $caddyPid)
    $caddyManaged = ([int]$state.caddy.pid -eq $caddyPid -and [bool]$caddyIdentity.Valid)
}

$tailscaleState = "Unavailable"
$tailscaleOnline = $false
$funnelState = "Unknown"
$publicUrl = ""
$publicProtection = 0
$funnelManaged = $false
$statusError = ""

try {
    $tailscale = Resolve-DemoExecutable "tailscale.exe" $TailscalePath @(
        "C:\Program Files\Tailscale\tailscale.exe"
    )
    $tailscaleStatus = Get-TailscaleRuntimeStatus $tailscale
    $tailscaleState = [string]$tailscaleStatus.BackendState
    $tailscaleOnline = [bool]$tailscaleStatus.Online
    Assert-TailscaleReady $tailscaleStatus
    $funnelStatus = Get-TailscaleFunnelStatus $tailscale
    $resolution = Resolve-TailscaleFunnel $funnelStatus $expectedTarget $expectedUrl
    $funnelState = [string]$resolution.State
    if ($resolution.State -eq "ActiveExpected") {
        $publicUrl = [string]$resolution.Mapping.PublicUrl
        $publicProtection = Get-DemoHttpStatus $publicUrl 15
        if ($null -ne $state -and $null -ne $state.funnel -and [bool]$state.funnel.managed) {
            $funnelManaged = (
                [string]$state.funnel.target -ceq $expectedTarget -and
                [string]$state.funnel.public_url -ceq $publicUrl -and
                [IO.Path]::GetFullPath([string]$state.funnel.executable) -ceq [IO.Path]::GetFullPath($tailscale)
            )
        }
    }
} catch {
    $statusError = $_.Exception.Message
    if ($funnelState -eq "Unknown") { $funnelState = "Error" }
}

$healthy = (
    $null -ne $backendPid -and $backendHealth -eq 200 -and $backendReadiness -eq 200 -and
    $null -ne $caddyPid -and $caddyProtection -eq 401 -and
    $tailscaleState -ceq "Running" -and $tailscaleOnline -and
    $funnelState -ceq "ActiveExpected" -and $publicProtection -eq 401
)

$result = [ordered]@{
    healthy = $healthy
    fastapi = [ordered]@{
        pid = $backendPid
        managed = $backendManaged
        health_http = $backendHealth
        readiness_http = $backendReadiness
    }
    caddy = [ordered]@{
        pid = $caddyPid
        managed = $caddyManaged
        unauthenticated_http = $caddyProtection
        target = $expectedTarget
    }
    tailscale = [ordered]@{
        state = $tailscaleState
        online = $tailscaleOnline
    }
    funnel = [ordered]@{
        state = $funnelState
        managed = $funnelManaged
        public_url = $publicUrl
        target = $expectedTarget
        unauthenticated_http = $publicProtection
    }
    error = $statusError
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6
} else {
    Write-Host "FastAPI: PID=$backendPid managed=$backendManaged health=$backendHealth readiness=$backendReadiness"
    Write-Host "Caddy: PID=$caddyPid managed=$caddyManaged unauthenticated=$caddyProtection target=$expectedTarget"
    Write-Host "Tailscale: state=$tailscaleState online=$tailscaleOnline"
    Write-Host "Funnel: state=$funnelState managed=$funnelManaged URL=$publicUrl unauthenticated=$publicProtection"
    if ($statusError) { Write-Host "Diagnostic: $statusError" }
    Write-Host "Overall healthy: $healthy"
}

if (-not $healthy) { exit 1 }
