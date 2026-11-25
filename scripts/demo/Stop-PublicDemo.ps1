[CmdletBinding()]
param([string]$TailscalePath = "")

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PublicDemo.Common.ps1")

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Import-DemoDotEnv (Join-Path $repoRoot ".env")
$proxyPort = if ($env:DEMO_PROXY_PORT) { [int]$env:DEMO_PROXY_PORT } else { 8080 }
$backendPort = if ($env:DEMO_BACKEND_PORT) { [int]$env:DEMO_BACKEND_PORT } else { 8000 }
$expectedTarget = "http://127.0.0.1:$proxyPort"
$statePath = Get-DemoStatePath $repoRoot
$state = Read-DemoState $statePath $repoRoot

if ($null -eq $state) {
    Write-Host "No project-owned public demo state exists. No Funnel or process was stopped."
    exit 0
}

$backendTokens = @("uvicorn", "clip_agent.main:app", "--host", "127.0.0.1", "--port", [string]$backendPort)
$caddyTokens = @("run", "scripts/demo/Caddyfile", "caddyfile")
$pythonAllowedExecutables = @()
$caddy = ""
if ($null -ne $state.backend) {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Project .venv is missing; nothing was stopped."
    }
    $pythonIdentity = Get-DemoPythonRuntimeIdentity $python (Join-Path $repoRoot ".venv")
    $pythonAllowedExecutables = @($pythonIdentity.AllowedExecutables)
}
if ($null -ne $state.caddy) {
    $caddy = Resolve-DemoExecutable "caddy" "" @()
}

function Get-OwnedStopPlan {
    param(
        [object]$Record,
        [int]$Port,
        [string]$Role,
        [string[]]$AllowedExecutables,
        [string[]]$CommandTokens
    )

    if ($null -eq $Record) { return [pscustomobject]@{ Action = "None"; Pid = $null } }
    Assert-DemoProcessRecordContract $Record $Role $AllowedExecutables $CommandTokens
    $recordPid = [int]$Record.pid
    $listenerPid = Get-DemoListenerOwner $Port
    $actual = Get-DemoProcessSnapshot $recordPid
    if ($null -eq $actual) {
        if ($null -ne $listenerPid) {
            throw "$Role project PID is gone but port $Port is occupied; no process was stopped."
        }
        return [pscustomobject]@{ Action = "AlreadyStopped"; Pid = $recordPid }
    }
    if ($null -eq $listenerPid) {
        throw "$Role PID $recordPid no longer owns expected port $Port; no process was stopped."
    }
    Assert-DemoOwnedListenerIdentity `
        $Record $actual $listenerPid $Role $AllowedExecutables $CommandTokens
    return [pscustomobject]@{ Action = "Stop"; Pid = $recordPid }
}

# Validate every process before changing Funnel or stopping anything.
$caddyPlan = Get-OwnedStopPlan $state.caddy $proxyPort "caddy" @($caddy) $caddyTokens
$backendPlan = Get-OwnedStopPlan `
    $state.backend $backendPort "backend" $pythonAllowedExecutables $backendTokens

$tailscale = $null
$funnelResolution = $null
if ($null -ne $state.funnel -and [bool]$state.funnel.managed) {
    if ([string]$state.funnel.target -cne $expectedTarget -or [int]$state.funnel.https_port -ne 443) {
        throw "Project Funnel state does not match the configured Caddy target; nothing was stopped."
    }
    $tailscale = Resolve-DemoExecutable "tailscale.exe" $TailscalePath @(
        "C:\Program Files\Tailscale\tailscale.exe"
    )
    if ([IO.Path]::GetFullPath([string]$state.funnel.executable) -cne [IO.Path]::GetFullPath($tailscale)) {
        throw "Tailscale executable does not match the project-owned state; nothing was stopped."
    }
    $tailscaleStatus = Get-TailscaleRuntimeStatus $tailscale
    Assert-TailscaleReady $tailscaleStatus
    $funnelStatus = Get-TailscaleFunnelStatus $tailscale
    $funnelResolution = Resolve-TailscaleFunnel $funnelStatus $expectedTarget ([string]$state.funnel.public_url)
} elseif ($caddyPlan.Action -eq "Stop" -or $backendPlan.Action -eq "Stop") {
    # Project-owned local processes must not be stopped underneath an unmanaged public Funnel.
    $tailscale = Resolve-DemoExecutable "tailscale.exe" $TailscalePath @(
        "C:\Program Files\Tailscale\tailscale.exe"
    )
    $tailscaleStatus = Get-TailscaleRuntimeStatus $tailscale
    Assert-TailscaleReady $tailscaleStatus
    $funnelStatus = Get-TailscaleFunnelStatus $tailscale
    $funnelResolution = Resolve-TailscaleFunnel $funnelStatus $expectedTarget ([string]$env:TAILSCALE_FUNNEL_URL)
    if ($funnelResolution.State -eq "ActiveExpected") {
        throw "An unmanaged Funnel still targets Caddy. It was not disabled and local processes were not stopped."
    }
}

if ($null -ne $state.funnel -and [bool]$state.funnel.managed -and
    $funnelResolution.State -eq "ActiveExpected") {
    Stop-TailscaleDemoFunnel $tailscale $expectedTarget
    $afterStatus = Get-TailscaleFunnelStatus $tailscale
    $afterResolution = Resolve-TailscaleFunnel $afterStatus $expectedTarget ""
    if ($afterResolution.State -ne "Inactive") {
        throw "Project-owned Funnel still appears active; local processes were not stopped."
    }
    Write-Host "Project-owned Funnel disabled."
} elseif ($null -ne $state.funnel -and [bool]$state.funnel.managed) {
    Write-Host "Project-owned Funnel was already inactive."
}

if ($caddyPlan.Action -eq "Stop") {
    Stop-Process -Id $caddyPlan.Pid -ErrorAction Stop
    Wait-Process -Id $caddyPlan.Pid -Timeout 15 -ErrorAction SilentlyContinue
    Write-Host "Project-owned Caddy stopped; PID $($caddyPlan.Pid)."
} elseif ($caddyPlan.Action -eq "AlreadyStopped") {
    Write-Host "Project-owned Caddy was already stopped."
}

if ($backendPlan.Action -eq "Stop") {
    Stop-Process -Id $backendPlan.Pid -ErrorAction Stop
    Wait-Process -Id $backendPlan.Pid -Timeout 15 -ErrorAction SilentlyContinue
    Write-Host "Project-owned FastAPI stopped; PID $($backendPlan.Pid)."
} elseif ($backendPlan.Action -eq "AlreadyStopped") {
    Write-Host "Project-owned FastAPI was already stopped."
}

if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    Remove-Item -LiteralPath $statePath -Force
}
Write-Host "Project-owned public demo lifecycle is stopped. Docker, Ollama and Tailscale itself were not stopped."
