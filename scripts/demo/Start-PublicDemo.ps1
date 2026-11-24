[CmdletBinding()]
param(
    [switch]$RebuildFrontend,
    [switch]$StartTunnel
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $repoRoot ".env"
$caddyFile = Join-Path $PSScriptRoot "Caddyfile"
$logDir = Join-Path $repoRoot "storage\demo-logs"

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing .env: $Path"
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Require-Command {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) { throw "Required command is not installed or not in PATH: $Name" }
    return $command.Source
}

function Wait-HttpStatus {
    param([string]$Uri, [int]$ExpectedStatus, [int]$Seconds = 30)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
            $status = [int]$response.StatusCode
        } catch {
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } else { $status = 0 }
        }
        if ($status -eq $ExpectedStatus) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Expected HTTP $ExpectedStatus from $Uri, last status: $status"
}

Import-DotEnv $envFile
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

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Project .venv is missing." }
$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne "3.12") { throw "The project .venv must use Python 3.12." }

$caddy = Require-Command "caddy"
$caddyVersionText = & $caddy version
if ($caddyVersionText -notmatch 'v?(\d+)\.(\d+)\.(\d+)') { throw "Cannot parse Caddy version." }
$caddyVersion = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
if ($caddyVersion -lt [version]"2.10.0") { throw "Caddy 2.10+ is required for request_body limits." }
if ($StartTunnel) {
    $ngrok = Require-Command "ngrok"
    if (-not $env:NGROK_DOMAIN) { throw "Set NGROK_DOMAIN to the stable domain from the owner's ngrok account." }
    $publicUrl = if ($env:NGROK_DOMAIN -match '^https://') { $env:NGROK_DOMAIN } else { "https://$($env:NGROK_DOMAIN)" }
    $publicUri = [Uri]$publicUrl
    if ($publicUri.Scheme -ne "https" -or -not $publicUri.IsDefaultPort -or
        $publicUri.HostNameType -ne "Dns" -or
        $publicUri.AbsolutePath -ne "/" -or $publicUri.Query -or $publicUri.Fragment) {
        throw "NGROK_DOMAIN must be a bare DNS hostname or HTTPS origin without path/query/fragment."
    }
}

$frontendRoot = Join-Path $repoRoot "frontend\dist"
if ($RebuildFrontend -or -not (Test-Path -LiteralPath (Join-Path $frontendRoot "index.html"))) {
    $npm = Require-Command "npm"
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

$backendOut = Join-Path $logDir "backend.stdout.log"
$backendErr = Join-Path $logDir "backend.stderr.log"
$caddyOut = Join-Path $logDir "caddy.stdout.log"
$caddyErr = Join-Path $logDir "caddy.stderr.log"
$backend = Start-Process -FilePath $python -ArgumentList @(
    "-m", "uvicorn", "clip_agent.main:app", "--host", "127.0.0.1", "--port", [string]$backendPort,
    "--no-access-log"
) -WorkingDirectory $repoRoot -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr `
    -WindowStyle Hidden -PassThru
try {
    Wait-HttpStatus -Uri "http://127.0.0.1:$backendPort/healthz" -ExpectedStatus 200
    $caddyProcess = Start-Process -FilePath $caddy -ArgumentList @(
        "run", "--config", "scripts/demo/Caddyfile", "--adapter", "caddyfile"
    ) -WorkingDirectory $repoRoot -RedirectStandardOutput $caddyOut -RedirectStandardError $caddyErr `
        -WindowStyle Hidden -PassThru
    try {
        Wait-HttpStatus -Uri "http://127.0.0.1:$proxyPort/healthz" -ExpectedStatus 401
        $ready = Invoke-WebRequest -Uri "http://127.0.0.1:$backendPort/readyz" -UseBasicParsing -TimeoutSec 15
        if ([int]$ready.StatusCode -ne 200) { throw "Backend readiness check failed." }
        Write-Host "Protected local stand: http://127.0.0.1:$proxyPort (unauthenticated check returned 401)."
        Write-Host "Backend PID: $($backend.Id); Caddy PID: $($caddyProcess.Id)"
        Write-Host "Stop with: Stop-Process -Id $($backend.Id),$($caddyProcess.Id)"
        if ($StartTunnel) {
            $ngrokProcess = Start-Process -FilePath $ngrok -ArgumentList @(
                "http", [string]$proxyPort, "--url", $publicUrl
            ) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
            Write-Host "ngrok PID: $($ngrokProcess.Id); requested URL: $publicUrl"
            Write-Host "Stop all: Stop-Process -Id $($backend.Id),$($caddyProcess.Id),$($ngrokProcess.Id)"
        }
    } catch {
        Stop-Process -Id $caddyProcess.Id -ErrorAction SilentlyContinue
        throw
    }
} catch {
    Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue
    throw
}
