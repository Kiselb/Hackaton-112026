Set-StrictMode -Version 2.0

function ConvertTo-DemoCanonicalPath {
    param([string]$Path, [string]$BasePath)

    if ([string]::IsNullOrWhiteSpace($Path)) { throw "Path is empty." }
    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    }
    return [IO.Path]::GetFullPath((Join-Path $BasePath $Path)).TrimEnd('\', '/')
}

function Import-DemoDotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
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

function Resolve-DemoExecutable {
    param(
        [string]$Name,
        [string]$ExplicitPath,
        [string[]]$FallbackPaths = @()
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $resolved = [IO.Path]::GetFullPath($ExplicitPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Configured $Name executable does not exist: $resolved"
        }
        return $resolved
    }

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return [IO.Path]::GetFullPath($command.Source) }
    foreach ($candidate in $FallbackPaths) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Required command is not installed or was not found: $Name"
}

function Test-DemoPathEqual {
    param([string]$Left, [string]$Right)

    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left),
        [IO.Path]::GetFullPath($Right),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Test-DemoPathAllowed {
    param([string]$Path, [string[]]$AllowedPaths)

    foreach ($allowedPath in @($AllowedPaths)) {
        if (Test-DemoPathEqual $Path $allowedPath) { return $true }
    }
    return $false
}

function Invoke-DemoCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [scriptblock]$Runner = $null
    )

    if ($null -ne $Runner) {
        $result = & $Runner $Executable $Arguments
        if ($null -eq $result -or $null -eq $result.PSObject.Properties['ExitCode'] -or
            $null -eq $result.PSObject.Properties['Output']) {
            throw "Mock command runner returned an invalid result."
        }
        return [pscustomobject]@{
            ExitCode = [int]$result.ExitCode
            Output = @($result.Output | ForEach-Object { [string]$_ })
        }
    }

    $output = @(& $Executable @Arguments 2>&1)
    return [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = @($output | ForEach-Object { [string]$_ })
    }
}

function ConvertFrom-DemoJson {
    param([string[]]$Output, [string]$Description)

    $text = ($Output -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw "$Description returned empty JSON." }
    try {
        return ConvertFrom-Json -InputObject $text -ErrorAction Stop
    } catch {
        throw "$Description returned invalid JSON."
    }
}

function Get-DemoPythonRuntimeIdentity {
    param(
        [string]$PythonPath,
        [string]$VenvRoot,
        [scriptblock]$Runner = $null
    )

    $python = [IO.Path]::GetFullPath($PythonPath)
    $venv = [IO.Path]::GetFullPath($VenvRoot).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Project virtual environment Python does not exist: $python"
    }
    if (-not (Test-Path -LiteralPath $venv -PathType Container)) {
        throw "Project virtual environment does not exist: $venv"
    }

    $probe = @'
import json, sys
print(json.dumps({
    "version": f"{sys.version_info.major}.{sys.version_info.minor}",
    "executable": sys.executable,
    "base_executable": getattr(sys, "_base_executable", ""),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
}))
'@
    $result = Invoke-DemoCommand $python @("-c", $probe) $Runner
    if ($result.ExitCode -ne 0) {
        throw "Cannot inspect the project Python runtime: $(($result.Output -join ' ').Trim())"
    }
    $runtime = ConvertFrom-DemoJson $result.Output "Project Python runtime probe"
    foreach ($property in @("version", "executable", "base_executable", "prefix", "base_prefix")) {
        if ($null -eq $runtime.PSObject.Properties[$property] -or
            [string]::IsNullOrWhiteSpace([string]$runtime.$property)) {
            throw "Project Python runtime probe did not return '$property'."
        }
    }
    if ([string]$runtime.version -cne "3.12") {
        throw "The project virtual environment must use Python 3.12."
    }

    $venvExecutable = [IO.Path]::GetFullPath([string]$runtime.executable)
    $baseExecutable = [IO.Path]::GetFullPath([string]$runtime.base_executable)
    $runtimePrefix = [IO.Path]::GetFullPath([string]$runtime.prefix).TrimEnd('\', '/')
    $basePrefix = [IO.Path]::GetFullPath([string]$runtime.base_prefix).TrimEnd('\', '/')
    if (-not (Test-DemoPathEqual $venvExecutable $python) -or
        -not (Test-DemoPathEqual $runtimePrefix $venv)) {
        throw "Python runtime does not belong to the project virtual environment."
    }
    if (Test-DemoPathEqual $runtimePrefix $basePrefix) {
        throw "Project Python is not running from a virtual environment."
    }
    if (-not (Test-Path -LiteralPath $venvExecutable -PathType Leaf) -or
        -not (Test-Path -LiteralPath $baseExecutable -PathType Leaf)) {
        throw "Project Python runtime reported a missing executable."
    }
    if (-not (Test-DemoPathEqual (Split-Path -Parent $baseExecutable) $basePrefix)) {
        throw "Project Python base executable does not match sys.base_prefix."
    }

    return [pscustomobject]@{
        VenvExecutable = $venvExecutable
        BaseExecutable = $baseExecutable
        AllowedExecutables = @(@($venvExecutable, $baseExecutable) | Select-Object -Unique)
    }
}

function Get-TailscaleRuntimeStatus {
    param([string]$Executable, [scriptblock]$Runner = $null)

    $result = Invoke-DemoCommand $Executable @("status", "--json") $Runner
    if ($result.ExitCode -ne 0) {
        throw "Tailscale status failed: $(($result.Output -join ' ').Trim())"
    }
    $status = ConvertFrom-DemoJson $result.Output "Tailscale status"
    $backendState = "Unknown"
    $online = $false
    $dnsName = ""
    if ($null -ne $status.PSObject.Properties['BackendState']) {
        $backendState = [string]$status.BackendState
    }
    if ($null -ne $status.PSObject.Properties['Self'] -and $null -ne $status.Self) {
        if ($null -ne $status.Self.PSObject.Properties['Online']) {
            $online = [bool]$status.Self.Online
        }
        if ($null -ne $status.Self.PSObject.Properties['DNSName']) {
            $dnsName = ([string]$status.Self.DNSName).TrimEnd('.')
        }
    }
    return [pscustomobject]@{
        BackendState = $backendState
        Online = $online
        DnsName = $dnsName
    }
}

function Assert-TailscaleReady {
    param([object]$Status)

    if ($Status.BackendState -cne "Running" -or -not [bool]$Status.Online) {
        throw "Tailscale is not connected and authorized (state=$($Status.BackendState), online=$($Status.Online))."
    }
    if ([string]::IsNullOrWhiteSpace([string]$Status.DnsName)) {
        throw "Tailscale did not report this node's DNS name."
    }
}

function ConvertTo-DemoHttpsOrigin {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
    try { $uri = [Uri]$Value } catch { throw "TAILSCALE_FUNNEL_URL must be an HTTPS origin." }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne "https" -or -not $uri.IsDefaultPort -or
        $uri.HostNameType -eq [UriHostNameType]::Unknown -or $uri.AbsolutePath -ne "/" -or
        $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw "TAILSCALE_FUNNEL_URL must be an HTTPS origin without credentials, port, path, query or fragment."
    }
    return "https://$($uri.DnsSafeHost)/"
}

function ConvertFrom-TailscaleFunnelStatusJson {
    param([string[]]$Output)

    $config = ConvertFrom-DemoJson $Output "Tailscale Funnel status"
    $mappings = @()
    if ($null -eq $config.PSObject.Properties['Web'] -or $null -eq $config.Web) {
        return [pscustomobject]@{ Mappings = @() }
    }

    foreach ($endpointProperty in $config.Web.PSObject.Properties) {
        $endpoint = [string]$endpointProperty.Name
        $separator = $endpoint.LastIndexOf(':')
        if ($separator -lt 1) { throw "Tailscale Funnel status contains an invalid web endpoint." }
        $hostName = $endpoint.Substring(0, $separator)
        $portText = $endpoint.Substring($separator + 1)
        $httpsPort = 0
        if (-not [int]::TryParse($portText, [ref]$httpsPort) -or $httpsPort -lt 1 -or $httpsPort -gt 65535) {
            throw "Tailscale Funnel status contains an invalid HTTPS port."
        }
        $allowFunnel = $false
        if ($null -ne $config.PSObject.Properties['AllowFunnel'] -and $null -ne $config.AllowFunnel) {
            $allowProperty = $config.AllowFunnel.PSObject.Properties[$endpoint]
            if ($null -ne $allowProperty) { $allowFunnel = [bool]$allowProperty.Value }
        }
        $handlersProperty = $endpointProperty.Value.PSObject.Properties['Handlers']
        if ($null -eq $handlersProperty -or $null -eq $handlersProperty.Value) { continue }
        foreach ($handlerProperty in $handlersProperty.Value.PSObject.Properties) {
            $proxyProperty = $handlerProperty.Value.PSObject.Properties['Proxy']
            if ($null -eq $proxyProperty -or [string]::IsNullOrWhiteSpace([string]$proxyProperty.Value)) {
                continue
            }
            $origin = if ($httpsPort -eq 443) { "https://$hostName/" } else { "https://$hostName`:$httpsPort/" }
            $mappings += [pscustomobject]@{
                Endpoint = $endpoint
                PublicUrl = $origin
                Path = [string]$handlerProperty.Name
                Target = ([string]$proxyProperty.Value).TrimEnd('/')
                HttpsPort = $httpsPort
                IsFunnel = $allowFunnel
            }
        }
    }
    return [pscustomobject]@{ Mappings = @($mappings) }
}

function Get-TailscaleFunnelStatus {
    param([string]$Executable, [scriptblock]$Runner = $null)

    $result = Invoke-DemoCommand $Executable @("funnel", "status", "--json") $Runner
    if ($result.ExitCode -ne 0) {
        throw "Tailscale Funnel status failed: $(($result.Output -join ' ').Trim())"
    }
    return ConvertFrom-TailscaleFunnelStatusJson $result.Output
}

function Start-TailscaleDemoFunnel {
    param([string]$Executable, [string]$Target, [scriptblock]$Runner = $null)

    $result = Invoke-DemoCommand `
        $Executable @("funnel", "--bg", "--https=443", $Target) $Runner
    if ($result.ExitCode -ne 0) {
        throw "Tailscale Funnel start failed: $(($result.Output -join ' ').Trim())"
    }
}

function Stop-TailscaleDemoFunnel {
    param([string]$Executable, [string]$Target, [scriptblock]$Runner = $null)

    $result = Invoke-DemoCommand `
        $Executable @("funnel", "--https=443", $Target, "off") $Runner
    if ($result.ExitCode -ne 0) {
        throw "Project-owned Funnel could not be disabled: $(($result.Output -join ' ').Trim())"
    }
}

function Resolve-TailscaleFunnel {
    param(
        [object]$Status,
        [string]$ExpectedTarget,
        [string]$ExpectedUrl = ""
    )

    $normalizedTarget = $ExpectedTarget.TrimEnd('/')
    $normalizedUrl = ConvertTo-DemoHttpsOrigin $ExpectedUrl
    $allMappings = @($Status.Mappings)
    if ($allMappings.Count -eq 0) {
        return [pscustomobject]@{ State = "Inactive"; Mapping = $null }
    }
    if ($allMappings.Count -ne 1) {
        throw "Conflicting Tailscale Serve/Funnel configuration contains multiple web mappings."
    }

    $mapping = $allMappings[0]
    if (-not [bool]$mapping.IsFunnel) {
        throw "A private Tailscale Serve mapping already occupies the web endpoint; it will not be overwritten."
    }
    if ([string]$mapping.Path -cne "/" -or [string]$mapping.Target -cne $normalizedTarget) {
        throw "Active Funnel target conflicts with the project target '$normalizedTarget'."
    }
    if (-not [string]::IsNullOrWhiteSpace($normalizedUrl) -and
        [string]$mapping.PublicUrl -cne $normalizedUrl) {
        throw "Active Funnel URL '$($mapping.PublicUrl)' does not match TAILSCALE_FUNNEL_URL '$normalizedUrl'."
    }
    return [pscustomobject]@{ State = "ActiveExpected"; Mapping = $mapping }
}

function Get-DemoHttpStatus {
    param([string]$Uri, [int]$TimeoutSeconds = 10)

    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

function Wait-DemoHttpStatus {
    param([string]$Uri, [int]$ExpectedStatus, [int]$Seconds = 30)

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $status = Get-DemoHttpStatus $Uri 3
        if ($status -eq $ExpectedStatus) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Expected HTTP $ExpectedStatus from $Uri, last status: $status"
}

function Get-DemoStatePath {
    param([string]$RepoRoot)
    return Join-Path $RepoRoot "storage\demo-logs\public-demo-state.json"
}

function Read-DemoState {
    param([string]$Path, [string]$RepoRoot)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $state = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Public demo state file is invalid: $Path" }
    if ([int]$state.schema_version -ne 1) { throw "Unsupported public demo state schema." }
    $actualRoot = ConvertTo-DemoCanonicalPath ([string]$state.repo_root) $RepoRoot
    $expectedRoot = ConvertTo-DemoCanonicalPath $RepoRoot $RepoRoot
    if ($actualRoot -cne $expectedRoot) { throw "Public demo state belongs to another repository." }
    return $state
}

function Write-DemoState {
    param([string]$Path, [object]$State)

    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporaryPath = "$Path.tmp"
    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}

function Get-DemoListenerOwner {
    param([int]$Port)

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 1) { throw "Multiple listeners were found on port $Port." }
    if ($listeners.Count -eq 0) { return $null }
    $address = $null
    if (-not [Net.IPAddress]::TryParse([string]$listeners[0].LocalAddress, [ref]$address) -or
        -not [Net.IPAddress]::IsLoopback($address)) {
        throw "Port $Port is not bound exclusively to loopback."
    }
    return [int]$listeners[0].OwningProcess
}

function Get-DemoProcessSnapshot {
    param([int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $creation = $process.CreationDate
    if ($creation -isnot [datetime]) {
        $creation = [Management.ManagementDateTimeConverter]::ToDateTime([string]$creation)
    }
    return [pscustomobject]@{
        ProcessId = [int]$process.ProcessId
        ExecutablePath = [string]$process.ExecutablePath
        CommandLine = [string]$process.CommandLine
        StartedUtc = ([datetime]$creation).ToUniversalTime().ToString("o")
    }
}

function New-DemoProcessRecord {
    param([object]$Snapshot, [string]$Role, [string[]]$CommandTokens)

    if ($null -eq $Snapshot -or [int]$Snapshot.ProcessId -lt 1 -or
        [string]::IsNullOrWhiteSpace([string]$Snapshot.ExecutablePath) -or
        [string]::IsNullOrWhiteSpace([string]$Snapshot.StartedUtc) -or
        [string]::IsNullOrWhiteSpace([string]$Snapshot.CommandLine)) {
        throw "Cannot create lifecycle ownership from an incomplete process snapshot."
    }
    try { $startedUtc = [datetime]::Parse([string]$Snapshot.StartedUtc).ToUniversalTime().ToString("o") }
    catch { throw "Cannot create lifecycle ownership from an invalid process start time." }

    return [pscustomobject][ordered]@{
        pid = [int]$Snapshot.ProcessId
        executable = [IO.Path]::GetFullPath([string]$Snapshot.ExecutablePath)
        started_utc = $startedUtc
        role = $Role
        command_tokens = @($CommandTokens)
    }
}

function Test-DemoProcessSnapshot {
    param([object]$Actual, [string[]]$AllowedExecutables, [string[]]$CommandTokens)

    if ($null -eq $Actual) {
        return [pscustomobject]@{ Valid = $false; Reason = "process is not running" }
    }
    if (-not (Test-DemoPathAllowed ([string]$Actual.ExecutablePath) $AllowedExecutables)) {
        return [pscustomobject]@{ Valid = $false; Reason = "executable path mismatch" }
    }
    foreach ($token in @($CommandTokens)) {
        if ([string]$Actual.CommandLine -notlike "*$token*") {
            return [pscustomobject]@{ Valid = $false; Reason = "command line mismatch" }
        }
    }
    return [pscustomobject]@{ Valid = $true; Reason = "matched" }
}

function Assert-DemoExpectedProcessSnapshot {
    param(
        [object]$Snapshot,
        [string[]]$AllowedExecutables,
        [string[]]$CommandTokens,
        [string]$Role
    )

    $identity = Test-DemoProcessSnapshot $Snapshot $AllowedExecutables $CommandTokens
    if (-not [bool]$identity.Valid) {
        throw "$Role port is owned by an unexpected process ($($identity.Reason))."
    }
}

function Test-DemoProcessIdentity {
    param([object]$Record, [object]$Actual)

    if ($null -eq $Actual) { return [pscustomobject]@{ Valid = $false; Reason = "process is not running" } }
    if ([int]$Record.pid -ne [int]$Actual.ProcessId) {
        return [pscustomobject]@{ Valid = $false; Reason = "PID mismatch" }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Actual.ExecutablePath) -or
        -not (Test-DemoPathEqual ([string]$Actual.ExecutablePath) ([string]$Record.executable))) {
        return [pscustomobject]@{ Valid = $false; Reason = "executable path mismatch" }
    }
    try {
        $expectedStarted = [datetime]::Parse([string]$Record.started_utc).ToUniversalTime()
        $actualStarted = [datetime]::Parse([string]$Actual.StartedUtc).ToUniversalTime()
    } catch {
        return [pscustomobject]@{ Valid = $false; Reason = "invalid process start time" }
    }
    if ([Math]::Abs(($actualStarted - $expectedStarted).TotalSeconds) -gt 2) {
        return [pscustomobject]@{ Valid = $false; Reason = "process start time mismatch" }
    }
    foreach ($token in @($Record.command_tokens)) {
        if ([string]$Actual.CommandLine -notlike "*$token*") {
            return [pscustomobject]@{ Valid = $false; Reason = "command line mismatch" }
        }
    }
    return [pscustomobject]@{ Valid = $true; Reason = "matched" }
}

function Assert-DemoProcessRecordContract {
    param(
        [object]$Record,
        [string]$ExpectedRole,
        [string[]]$AllowedExecutables,
        [string[]]$ExpectedCommandTokens
    )

    if ($null -eq $Record) { return }
    if ($null -eq $Record.PSObject.Properties['role'] -or
        [string]$Record.role -cne $ExpectedRole) {
        throw "$ExpectedRole lifecycle record has an unexpected role."
    }
    if ($null -eq $Record.PSObject.Properties['executable'] -or
        [string]::IsNullOrWhiteSpace([string]$Record.executable) -or
        -not (Test-DemoPathAllowed ([string]$Record.executable) $AllowedExecutables)) {
        throw "$ExpectedRole lifecycle record has an unexpected executable."
    }
    $actualTokens = @($Record.command_tokens)
    if ($actualTokens.Count -ne $ExpectedCommandTokens.Count) {
        throw "$ExpectedRole lifecycle record has unexpected command tokens."
    }
    for ($index = 0; $index -lt $ExpectedCommandTokens.Count; $index++) {
        if ([string]$actualTokens[$index] -cne [string]$ExpectedCommandTokens[$index]) {
            throw "$ExpectedRole lifecycle record has unexpected command tokens."
        }
    }
}

function Assert-DemoOwnedListenerIdentity {
    param(
        [object]$Record,
        [object]$Actual,
        [int]$ListenerPid,
        [string]$ExpectedRole,
        [string[]]$AllowedExecutables,
        [string[]]$ExpectedCommandTokens
    )

    Assert-DemoProcessRecordContract `
        $Record $ExpectedRole $AllowedExecutables $ExpectedCommandTokens
    $identity = Test-DemoProcessIdentity $Record $Actual
    if (-not [bool]$identity.Valid) {
        throw "$ExpectedRole PID $($Record.pid) failed ownership validation ($($identity.Reason))."
    }
    if ([int]$Record.pid -ne $ListenerPid) {
        throw "$ExpectedRole PID $($Record.pid) no longer owns the expected listener."
    }
}

function Resolve-DemoComponentAction {
    param([bool]$ListenerExists, [bool]$ListenerMatches, [bool]$OwnedByState)

    if (-not $ListenerExists) { return "Start" }
    if (-not $ListenerMatches) { throw "Port is occupied by an unexpected process." }
    if ($OwnedByState) { return "ReuseOwned" }
    return "ReuseUnmanaged"
}
