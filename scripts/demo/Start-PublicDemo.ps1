[CmdletBinding()]
param(
    [switch]$RebuildFrontend,
    [switch]$StartFunnel,
    [switch]$StartTunnel,
    [string]$TailscalePath = ""
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PublicDemo.Common.ps1")

if ($StartTunnel) {
    throw "-StartTunnel was the CLIP-008 ngrok interface and no longer starts a provider. Use -StartFunnel explicitly."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $repoRoot ".env"
$caddyFile = Join-Path $PSScriptRoot "Caddyfile"
$logDir = Join-Path $repoRoot "storage\demo-logs"
$statePath = Get-DemoStatePath $repoRoot

function Assert-ExpectedProcessSnapshot {
    param([object]$Snapshot, [string]$Executable, [string[]]$CommandTokens, [string]$Role)

    if ($null -eq $Snapshot) { throw "$Role listener process could not be inspected." }
    $expectedPath = [IO.Path]::GetFullPath($Executable)
    if ([string]::IsNullOrWhiteSpace([string]$Snapshot.ExecutablePath) -or
        [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath) -cne $expectedPath) {
        throw "$Role port is owned by an unexpected executable."
    }
    foreach ($token in $CommandTokens) {
        if ([string]$Snapshot.CommandLine -notlike "*$token*") {
            throw "$Role port is owned by a process with an unexpected command line."
        }
    }
}

function Test-StateOwnsProcess {
    param(
        [object]$Record,
        [object]$Snapshot,
        [int]$ListenerPid,
        [string]$Role,
        [string]$Executable,
        [string[]]$CommandTokens
    )

    if ($null -eq $Record -or [int]$Record.pid -ne $ListenerPid) { return $false }
    Assert-DemoProcessRecordContract $Record $Role $Executable $CommandTokens
    $identity = Test-DemoProcessIdentity $Record $Snapshot
    return [bool]$identity.Valid
}

Import-DemoDotEnv $envFile
if ($env:PUBLIC_DEMO_ENABLED -ne "true") { throw "Set PUBLIC_DEMO_ENABLED=true in the existing .env." }
if ($env:DEMO_AUTH_USERNAME -ne "jury") { throw "DEMO_AUTH_USERNAME must be jury." }
if ($env:DEMO_AUTH_PASSWORD_HASH -notmatch '^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$') {
    throw "DEMO_AUTH_PASSWORD_HASH must contain a bcrypt hash; plaintext is not accepted."
}
$bcryptCost = [int](($env:DEMO_AUTH_PASSWORD_HASH -split '\$')[2])
if ($bcryptCost -lt 12 -or $bcryptCost -gt 16) {
    throw "DEMO_AUTH_PASSWORD_HASH must use a bcrypt cost from 12 through 16."
}

$proxyPort = if ($env:DEMO_PROXY_PORT) { [int]$env:DEMO_PROXY_PORT } else { 8080 }
$backendPort = if ($env:DEMO_BACKEND_PORT) { [int]$env:DEMO_BACKEND_PORT } else { 8000 }
$maxUploadMb = if ($env:DEMO_MAX_UPLOAD_MB) { [int]$env:DEMO_MAX_UPLOAD_MB } else { 20 }
$maxLlm = if ($env:DEMO_MAX_CONCURRENT_LLM) { [int]$env:DEMO_MAX_CONCURRENT_LLM } else { 1 }
$maxIngest = if ($env:DEMO_MAX_CONCURRENT_INGEST) { [int]$env:DEMO_MAX_CONCURRENT_INGEST } else { 1 }
if ($proxyPort -lt 1024 -or $proxyPort -gt 65535) { throw "DEMO_PROXY_PORT must be 1024..65535." }
if ($backendPort -lt 1024 -or $backendPort -gt 65535) { throw "DEMO_BACKEND_PORT must be 1024..65535." }
if ($proxyPort -eq $backendPort) { throw "Proxy and backend ports must differ." }
if ($maxUploadMb -lt 1 -or $maxUploadMb -gt 100) { throw "DEMO_MAX_UPLOAD_MB must be 1..100." }
if ($maxLlm -lt 1 -or $maxLlm -gt 16) { throw "DEMO_MAX_CONCURRENT_LLM must be 1..16." }
if ($maxIngest -lt 1 -or $maxIngest -gt 8) { throw "DEMO_MAX_CONCURRENT_INGEST must be 1..8." }
if ($StartFunnel -and $env:DEMO_TUNNEL_PROVIDER -and $env:DEMO_TUNNEL_PROVIDER -cne "tailscale") {
    throw "DEMO_TUNNEL_PROVIDER must be 'tailscale' when -StartFunnel is used."
}
$expectedFunnelUrl = ConvertTo-DemoHttpsOrigin ([string]$env:TAILSCALE_FUNNEL_URL)

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Project .venv is missing." }
$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne "3.12") { throw "The project .venv must use Python 3.12." }
$pythonProcessExecutable = (& $python -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonProcessExecutable -PathType Leaf)) {
    throw "Cannot resolve the Python executable used by the project .venv."
}
$pythonProcessExecutable = [IO.Path]::GetFullPath($pythonProcessExecutable)

$caddy = Resolve-DemoExecutable "caddy" "" @()
$caddyVersionText = (& $caddy version) -join " "
if ($LASTEXITCODE -ne 0 -or $caddyVersionText -notmatch 'v?(\d+)\.(\d+)\.(\d+)') {
    throw "Cannot parse Caddy version."
}
$caddyVersion = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
if ($caddyVersion -lt [version]"2.10.0") { throw "Caddy 2.10+ is required for request_body limits." }

$frontendRoot = Join-Path $repoRoot "frontend\dist"
if ($RebuildFrontend -or -not (Test-Path -LiteralPath (Join-Path $frontendRoot "index.html") -PathType Leaf)) {
    $npm = Resolve-DemoExecutable "npm" "" @()
    Push-Location (Join-Path $repoRoot "frontend")
    try {
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:DEMO_PROXY_PORT = [string]$proxyPort
$env:DEMO_BACKEND_PORT = [string]$backendPort
$env:DEMO_MAX_UPLOAD_MB = [string]$maxUploadMb
$env:DEMO_MAX_CONCURRENT_LLM = [string]$maxLlm
$env:DEMO_MAX_CONCURRENT_INGEST = [string]$maxIngest
$env:DEMO_FRONTEND_ROOT = $frontendRoot.Replace('\', '/')
$env:DEMO_ACCESS_LOG = (Join-Path $logDir "caddy-access.json").Replace('\', '/')
$env:DEMO_MAX_REQUEST_BODY = "{0}MiB" -f ($maxUploadMb + 1)

& $caddy validate --config $caddyFile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw "Caddy configuration validation failed." }

$existingState = Read-DemoState $statePath $repoRoot
$state = [ordered]@{
    schema_version = 1
    repo_root = $repoRoot
    created_utc = (Get-Date).ToUniversalTime().ToString("o")
    backend = $null
    caddy = $null
    funnel = $null
}
if ($null -ne $existingState) {
    $state.created_utc = [string]$existingState.created_utc
    if ($null -ne $existingState.PSObject.Properties['backend']) { $state.backend = $existingState.backend }
    if ($null -ne $existingState.PSObject.Properties['caddy']) { $state.caddy = $existingState.caddy }
    if ($null -ne $existingState.PSObject.Properties['funnel']) { $state.funnel = $existingState.funnel }
}

$backendTokens = @("uvicorn", "clip_agent.main:app", "--host", "127.0.0.1", "--port", [string]$backendPort)
$caddyTokens = @("run", "scripts/demo/Caddyfile", "caddyfile")
$startedBackend = $null
$startedCaddy = $null
$createdFunnel = $false
$tailscale = $null
$expectedTarget = "http://127.0.0.1:$proxyPort"

try {
    $backendListenerPid = Get-DemoListenerOwner $backendPort
    if ($null -ne $backendListenerPid) {
        $backendSnapshot = Get-DemoProcessSnapshot $backendListenerPid
        Assert-ExpectedProcessSnapshot $backendSnapshot $pythonProcessExecutable $backendTokens "FastAPI"
        $backendOwned = Test-StateOwnsProcess `
            $state.backend $backendSnapshot $backendListenerPid "backend" $pythonProcessExecutable $backendTokens
        $backendAction = Resolve-DemoComponentAction $true $true $backendOwned
        if (-not $backendOwned) { $state.backend = $null }
        Wait-DemoHttpStatus "http://127.0.0.1:$backendPort/healthz" 200
        Write-Host "FastAPI already healthy ($backendAction); PID $backendListenerPid."
    } else {
        $backendOut = Join-Path $logDir "backend.stdout.log"
        $backendErr = Join-Path $logDir "backend.stderr.log"
        $startedBackend = Start-Process -FilePath $python -ArgumentList @(
            "-m", "uvicorn", "clip_agent.main:app", "--host", "127.0.0.1", "--port", [string]$backendPort,
            "--no-access-log"
        ) -WorkingDirectory $repoRoot -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr `
            -WindowStyle Hidden -PassThru
        Wait-DemoHttpStatus "http://127.0.0.1:$backendPort/healthz" 200
        $state.backend = New-DemoProcessRecord $startedBackend $pythonProcessExecutable "backend" $backendTokens
        Write-Host "FastAPI started; PID $($startedBackend.Id)."
    }

    $caddyListenerPid = Get-DemoListenerOwner $proxyPort
    if ($null -ne $caddyListenerPid) {
        $caddySnapshot = Get-DemoProcessSnapshot $caddyListenerPid
        Assert-ExpectedProcessSnapshot $caddySnapshot $caddy $caddyTokens "Caddy"
        $caddyOwned = Test-StateOwnsProcess `
            $state.caddy $caddySnapshot $caddyListenerPid "caddy" $caddy $caddyTokens
        $caddyAction = Resolve-DemoComponentAction $true $true $caddyOwned
        if (-not $caddyOwned) { $state.caddy = $null }
        Wait-DemoHttpStatus "http://127.0.0.1:$proxyPort/healthz" 401
        Write-Host "Caddy already protected ($caddyAction); PID $caddyListenerPid."
    } else {
        $caddyOut = Join-Path $logDir "caddy.stdout.log"
        $caddyErr = Join-Path $logDir "caddy.stderr.log"
        $startedCaddy = Start-Process -FilePath $caddy -ArgumentList @(
            "run", "--config", "scripts/demo/Caddyfile", "--adapter", "caddyfile"
        ) -WorkingDirectory $repoRoot -RedirectStandardOutput $caddyOut -RedirectStandardError $caddyErr `
            -WindowStyle Hidden -PassThru
        Wait-DemoHttpStatus "http://127.0.0.1:$proxyPort/healthz" 401
        $state.caddy = New-DemoProcessRecord $startedCaddy $caddy "caddy" $caddyTokens
        Write-Host "Caddy started; PID $($startedCaddy.Id)."
    }

    $readyStatus = Get-DemoHttpStatus "http://127.0.0.1:$backendPort/readyz" 15
    if ($readyStatus -ne 200) { throw "Backend readiness check failed with HTTP $readyStatus." }
    Write-Host "Protected local stand: http://127.0.0.1:$proxyPort (unauthenticated HTTP 401)."

    if ($StartFunnel) {
        $tailscale = Resolve-DemoExecutable "tailscale.exe" $TailscalePath @(
            "C:\Program Files\Tailscale\tailscale.exe"
        )
        $tailscaleStatus = Get-TailscaleRuntimeStatus $tailscale
        Assert-TailscaleReady $tailscaleStatus
        $funnelStatus = Get-TailscaleFunnelStatus $tailscale
        $funnelResolution = Resolve-TailscaleFunnel $funnelStatus $expectedTarget $expectedFunnelUrl

        if ($funnelResolution.State -eq "Inactive") {
            Start-TailscaleDemoFunnel $tailscale $expectedTarget
            $createdFunnel = $true
            $funnelStatus = Get-TailscaleFunnelStatus $tailscale
            $funnelResolution = Resolve-TailscaleFunnel $funnelStatus $expectedTarget $expectedFunnelUrl
            if ($funnelResolution.State -ne "ActiveExpected") { throw "Funnel did not become active." }
            $state.funnel = [ordered]@{
                managed = $true
                executable = [IO.Path]::GetFullPath($tailscale)
                target = $expectedTarget
                public_url = [string]$funnelResolution.Mapping.PublicUrl
                https_port = 443
                started_utc = (Get-Date).ToUniversalTime().ToString("o")
                off_arguments = @("funnel", "--https=443", $expectedTarget, "off")
            }
            Write-Host "Tailscale Funnel started and is project-managed."
        } else {
            $ownedFunnel = $false
            if ($null -ne $state.funnel -and [bool]$state.funnel.managed) {
                $ownedFunnel = (
                    [IO.Path]::GetFullPath([string]$state.funnel.executable) -ceq [IO.Path]::GetFullPath($tailscale) -and
                    [string]$state.funnel.target -ceq $expectedTarget -and
                    [string]$state.funnel.public_url -ceq [string]$funnelResolution.Mapping.PublicUrl
                )
            }
            if (-not $ownedFunnel) {
                $state.funnel = $null
                Write-Warning "Expected Funnel is already active but is not project-managed; Stop-PublicDemo.ps1 will not disable it."
            } else {
                Write-Host "Expected project-managed Funnel is already active."
            }
        }

        $publicUrl = [string]$funnelResolution.Mapping.PublicUrl
        Wait-DemoHttpStatus $publicUrl 401 60
        Write-Host "Public HTTPS: $publicUrl (unauthenticated HTTP 401)."
    }

    if ($null -ne $state.backend -or $null -ne $state.caddy -or $null -ne $state.funnel) {
        Write-DemoState $statePath $state
        Write-Host "Project-owned lifecycle state saved."
    } else {
        Write-Warning "All detected components are unmanaged; no lifecycle ownership was recorded."
    }
} catch {
    $failure = $_
    if ($createdFunnel -and $null -ne $tailscale) {
        try { Stop-TailscaleDemoFunnel $tailscale $expectedTarget }
        catch { Write-Warning "Rollback could not disable the newly created Funnel." }
    }
    if ($null -ne $startedCaddy) { Stop-Process -Id $startedCaddy.Id -ErrorAction SilentlyContinue }
    if ($null -ne $startedBackend) { Stop-Process -Id $startedBackend.Id -ErrorAction SilentlyContinue }
    throw $failure
}
