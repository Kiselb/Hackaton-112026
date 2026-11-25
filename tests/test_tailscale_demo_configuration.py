import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "demo"


def test_tailscale_common_logic_runs_in_windows_powershell_51(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is not available")

    fake_tailscale = tmp_path / "tailscale.exe"
    fake_tailscale.write_bytes(b"")
    common_path = str(DEMO / "PublicDemo.Common.ps1").replace("'", "''")
    executable_path = str(fake_tailscale).replace("'", "''")
    command = rf"""
. '{common_path}'

$resolved = Resolve-DemoExecutable 'tailscale.exe' '{executable_path}' @()
if ($resolved -cne [IO.Path]::GetFullPath('{executable_path}')) {{
    throw 'Explicit Tailscale executable was not resolved.'
}}

$runner = {{
    param($executable, $arguments)
    $key = $arguments -join ' '
    if ($key -eq 'status --json') {{
        return [pscustomobject]@{{
            ExitCode = 0
            Output = @('{{"BackendState":"Running","Self":{{"Online":true,"DNSName":"demo.example.ts.net."}}}}')
        }}
    }}
    throw "Unexpected mocked command: $key"
}}
$runtime = Get-TailscaleRuntimeStatus '{executable_path}' $runner
Assert-TailscaleReady $runtime
if ($runtime.DnsName -cne 'demo.example.ts.net') {{ throw 'DNS name normalization failed.' }}

$lifecycleCalls = New-Object System.Collections.ArrayList
$lifecycleRunner = {{
    param($executable, $arguments)
    [void]$lifecycleCalls.Add(($arguments -join ' '))
    return [pscustomobject]@{{ ExitCode = 0; Output = @('ok') }}
}}
Start-TailscaleDemoFunnel '{executable_path}' 'http://127.0.0.1:8080' $lifecycleRunner
Stop-TailscaleDemoFunnel '{executable_path}' 'http://127.0.0.1:8080' $lifecycleRunner
if ($lifecycleCalls[0] -cne 'funnel --bg --https=443 http://127.0.0.1:8080') {{
    throw 'Funnel start arguments are not exact.'
}}
if ($lifecycleCalls[1] -cne 'funnel --https=443 http://127.0.0.1:8080 off') {{
    throw 'Funnel stop arguments are not exact.'
}}

$loginRunner = {{
    param($executable, $arguments)
    return [pscustomobject]@{{
        ExitCode = 0
        Output = @('{{"BackendState":"NeedsLogin"}}')
    }}
}}
$unauthorized = Get-TailscaleRuntimeStatus '{executable_path}' $loginRunner
$failedClosed = $false
try {{ Assert-TailscaleReady $unauthorized }} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unauthorized Tailscale state did not fail closed.' }}

$empty = ConvertFrom-TailscaleFunnelStatusJson @('{{"TCP":{{}},"Web":{{}},"AllowFunnel":{{}}}}')
$emptyResolution = Resolve-TailscaleFunnel $empty 'http://127.0.0.1:8080' ''
if ($emptyResolution.State -cne 'Inactive') {{ throw 'Missing Funnel was not detected.' }}

$expectedJson = @'
{{
  "TCP": {{"443": {{"HTTPS": true}}}},
  "Web": {{
    "demo.example.ts.net:443": {{
      "Handlers": {{"/": {{"Proxy": "http://127.0.0.1:8080"}}}}
    }}
  }},
  "AllowFunnel": {{"demo.example.ts.net:443": true}}
}}
'@
$expected = ConvertFrom-TailscaleFunnelStatusJson @($expectedJson)
$resolution = Resolve-TailscaleFunnel `
    $expected `
    'http://127.0.0.1:8080' `
    'https://demo.example.ts.net/'
if ($resolution.State -cne 'ActiveExpected') {{ throw 'Expected Funnel was not accepted.' }}
if ($resolution.Mapping.PublicUrl -cne 'https://demo.example.ts.net/') {{
    throw 'Public Funnel URL was parsed incorrectly.'
}}

$failedClosed = $false
try {{
    Resolve-TailscaleFunnel $expected 'http://127.0.0.1:8080' 'https://other.example.ts.net/'
}} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unexpected Funnel URL did not fail closed.' }}

$conflictJson = $expectedJson.Replace(
    '"demo.example.ts.net:443": {{',
    '"other.example.ts.net:443": {{"Handlers": {{"/": {{"Proxy": "http://127.0.0.1:9000"}}}}}}, "demo.example.ts.net:443": {{'
).Replace(
    '"demo.example.ts.net:443": true',
    '"other.example.ts.net:443": true, "demo.example.ts.net:443": true'
)
$conflict = ConvertFrom-TailscaleFunnelStatusJson @($conflictJson)
$failedClosed = $false
try {{ Resolve-TailscaleFunnel $conflict 'http://127.0.0.1:8080' '' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Conflicting Funnel did not fail closed.' }}

$serveJson = $expectedJson.Replace('true}}', 'false}}')
$serve = ConvertFrom-TailscaleFunnelStatusJson @($serveJson)
$failedClosed = $false
try {{ Resolve-TailscaleFunnel $serve 'http://127.0.0.1:8080' '' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Private Serve mapping was accepted as Funnel.' }}

if ((Resolve-DemoComponentAction $false $false $false) -cne 'Start') {{ throw 'Start action failed.' }}
if ((Resolve-DemoComponentAction $true $true $false) -cne 'ReuseUnmanaged') {{ throw 'Unmanaged reuse failed.' }}
if ((Resolve-DemoComponentAction $true $true $true) -cne 'ReuseOwned') {{ throw 'Owned reuse failed.' }}
$failedClosed = $false
try {{ Resolve-DemoComponentAction $true $false $false }} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unexpected port owner did not fail closed.' }}

$record = [pscustomobject]@{{
    pid = 42
    executable = 'C:\Tools\expected.exe'
    started_utc = '2026-11-25T10:00:00Z'
    role = 'backend'
    command_tokens = @('expected-token')
}}
$actual = [pscustomobject]@{{
    ProcessId = 42
    ExecutablePath = 'C:\Tools\expected.exe'
    StartedUtc = '2026-11-25T10:00:00Z'
    CommandLine = 'expected.exe expected-token'
}}
if (-not (Test-DemoProcessIdentity $record $actual).Valid) {{ throw 'Owned process was rejected.' }}
$actual.ExecutablePath = 'C:\Tools\foreign.exe'
if ((Test-DemoProcessIdentity $record $actual).Valid) {{ throw 'Foreign process was accepted.' }}

Assert-DemoProcessRecordContract $record 'backend' 'C:\Tools\expected.exe' @('expected-token')
$failedClosed = $false
try {{ Assert-DemoProcessRecordContract $record 'caddy' 'C:\Tools\expected.exe' @('expected-token') }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unexpected lifecycle role did not fail closed.' }}
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_windows_powershell_51_python_process_identity(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is not available")

    project_root = tmp_path / "project"
    venv_root = project_root / ".venv"
    venv_python = venv_root / "Scripts" / "python.exe"
    base_root = tmp_path / "Python312"
    base_python = base_root / "python.exe"
    venv_python.parent.mkdir(parents=True)
    base_root.mkdir(parents=True)
    venv_python.write_bytes(b"")
    base_python.write_bytes(b"")

    common_path = str(DEMO / "PublicDemo.Common.ps1").replace("'", "''")
    venv_root_path = str(venv_root).replace("'", "''")
    venv_python_path = str(venv_python).replace("'", "''")
    base_root_path = str(base_root).replace("'", "''")
    base_python_path = str(base_python).replace("'", "''")
    command = rf"""
. '{common_path}'

$runtimeRunner = {{
    param($executable, $arguments)
    $json = [ordered]@{{
        version = '3.12'
        executable = '{venv_python_path}'
        base_executable = '{base_python_path}'
        prefix = '{venv_root_path}'
        base_prefix = '{base_root_path}'
    }} | ConvertTo-Json -Compress
    return [pscustomobject]@{{ ExitCode = 0; Output = @($json) }}
}}
$runtime = Get-DemoPythonRuntimeIdentity `
    '{venv_python_path}' '{venv_root_path}' $runtimeRunner
if ($runtime.AllowedExecutables.Count -ne 2) {{
    throw 'Project Python executable allow-list is incomplete.'
}}
if (-not (Test-DemoPathAllowed '{venv_python_path}' $runtime.AllowedExecutables)) {{
    throw 'Virtual environment executable was rejected.'
}}
if (-not (Test-DemoPathAllowed '{base_python_path}' $runtime.AllowedExecutables)) {{
    throw 'Project base executable was rejected.'
}}

$tokens = @('uvicorn', 'clip_agent.main:app', '--host', '127.0.0.1', '--port', '8000')
$commandLine = 'python.exe -m uvicorn clip_agent.main:app --host 127.0.0.1 --port 8000 --no-access-log'
$venvSnapshot = [pscustomobject]@{{
    ProcessId = 41
    ExecutablePath = '{venv_python_path}'
    StartedUtc = '2026-11-25T10:00:00Z'
    CommandLine = $commandLine
}}
$baseSnapshot = [pscustomobject]@{{
    ProcessId = 42
    ExecutablePath = '{base_python_path}'
    StartedUtc = '2026-11-25T10:00:01Z'
    CommandLine = $commandLine
}}
Assert-DemoExpectedProcessSnapshot $venvSnapshot $runtime.AllowedExecutables $tokens 'FastAPI'
Assert-DemoExpectedProcessSnapshot $baseSnapshot $runtime.AllowedExecutables $tokens 'FastAPI'
if ((Resolve-DemoComponentAction $true $true $false) -cne 'ReuseUnmanaged') {{
    throw 'Allowed unmanaged FastAPI was not planned for unmanaged reuse.'
}}

$foreign = [pscustomobject]@{{
    ProcessId = 43
    ExecutablePath = 'C:\Unknown\python.exe'
    StartedUtc = '2026-11-25T10:00:02Z'
    CommandLine = $commandLine
}}
$failedClosed = $false
try {{ Assert-DemoExpectedProcessSnapshot $foreign $runtime.AllowedExecutables $tokens 'FastAPI' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unknown Python executable was accepted.' }}

$wrongCommand = [pscustomobject]@{{
    ProcessId = 44
    ExecutablePath = '{base_python_path}'
    StartedUtc = '2026-11-25T10:00:03Z'
    CommandLine = 'python.exe -m http.server 8000'
}}
$failedClosed = $false
try {{ Assert-DemoExpectedProcessSnapshot $wrongCommand $runtime.AllowedExecutables $tokens 'FastAPI' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unexpected Python command line was accepted.' }}

# A launcher-owned record must use the inspected listener snapshot, including its actual base path.
$record = New-DemoProcessRecord $baseSnapshot 'backend' $tokens
if ([int]$record.pid -ne 42 -or
    -not (Test-DemoPathEqual ([string]$record.executable) '{base_python_path}')) {{
    throw 'New FastAPI ownership did not preserve the actual listener identity.'
}}
Assert-DemoProcessRecordContract $record 'backend' $runtime.AllowedExecutables $tokens
if (-not (Test-DemoProcessIdentity $record $baseSnapshot).Valid) {{
    throw 'New FastAPI ownership record did not validate.'
}}
Assert-DemoOwnedListenerIdentity `
    $record $baseSnapshot 42 'backend' $runtime.AllowedExecutables $tokens
$venvRecord = New-DemoProcessRecord $venvSnapshot 'backend' $tokens
Assert-DemoOwnedListenerIdentity `
    $venvRecord $venvSnapshot 41 'backend' $runtime.AllowedExecutables $tokens

$failedClosed = $false
try {{
    Assert-DemoOwnedListenerIdentity `
        $record $foreign 42 'backend' $runtime.AllowedExecutables $tokens
}} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Stop ownership accepted a foreign process.' }}

$failedClosed = $false
try {{
    Assert-DemoOwnedListenerIdentity `
        $record $baseSnapshot 99 'backend' $runtime.AllowedExecutables $tokens
}} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Stop ownership accepted the wrong listener PID.' }}

$foreignRecord = [pscustomobject]@{{
    pid = 43
    executable = 'C:\Unknown\python.exe'
    started_utc = '2026-11-25T10:00:02Z'
    role = 'backend'
    command_tokens = $tokens
}}
$failedClosed = $false
try {{
    Assert-DemoProcessRecordContract `
        $foreignRecord 'backend' $runtime.AllowedExecutables $tokens
}} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Foreign executable was accepted into lifecycle state.' }}

$wrongRuntimeRunner = {{
    param($executable, $arguments)
    $json = [ordered]@{{
        version = '3.12'
        executable = '{base_python_path}'
        base_executable = '{base_python_path}'
        prefix = '{base_root_path}'
        base_prefix = '{base_root_path}'
    }} | ConvertTo-Json -Compress
    return [pscustomobject]@{{ ExitCode = 0; Output = @($json) }}
}}
$failedClosed = $false
try {{
    Get-DemoPythonRuntimeIdentity `
        '{venv_python_path}' '{venv_root_path}' $wrongRuntimeRunner
}} catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Python runtime outside the project venv was accepted.' }}
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_tailscale_lifecycle_scripts_are_fail_closed_and_ngrok_free() -> None:
    start = (DEMO / "Start-PublicDemo.ps1").read_text(encoding="utf-8")
    status = (DEMO / "Get-PublicDemoStatus.ps1").read_text(encoding="utf-8")
    stop = (DEMO / "Stop-PublicDemo.ps1").read_text(encoding="utf-8")
    common = (DEMO / "PublicDemo.Common.ps1").read_text(encoding="utf-8")

    assert "[switch]$StartFunnel" in start
    assert "-StartTunnel was the CLIP-008 ngrok interface" in start
    assert 'Resolve-DemoExecutable "tailscale.exe"' in start
    assert "Start-TailscaleDemoFunnel" in start
    assert "Get-DemoPythonRuntimeIdentity" in start
    assert "New-DemoProcessRecord $backendSnapshot" in start
    assert "if (-not $backendOwned) { $state.backend = $null }" in start
    assert "Stop-PublicDemo.ps1 will not disable it" in start
    assert 'Require-Command "ngrok"' not in start
    assert "NGROK_DOMAIN" not in start

    assert "Start-Process" not in status
    assert "Stop-Process" not in status
    assert "Write-DemoState" not in status
    assert "Get-TailscaleFunnelStatus" in status

    assert "Test-DemoProcessIdentity" in common
    assert "Assert-DemoProcessRecordContract" in stop
    assert "Assert-DemoOwnedListenerIdentity" in stop
    assert "Get-DemoPythonRuntimeIdentity" in stop
    assert "Get-DemoProcessSnapshot" in stop
    assert "Stop-TailscaleDemoFunnel" in stop
    assert "No project-owned public demo state exists. No Funnel or process was stopped." in stop
    assert "tailscale down" not in stop.lower()
    assert "funnel reset" not in stop.lower()
    assert "serve reset" not in stop.lower()
    assert "Stop-Process -Id $caddyPlan.Pid" in stop
    assert "Stop-Process -Id $backendPlan.Pid" in stop

    assert "DEMO_AUTH_PASSWORD_HASH" not in common
    assert "POSTGRES_DSN" not in common
    assert "SetEnvironmentVariable" in common
    assert '"Process"' in common
    for script in (start, status, stop):
        assert "Set-Content -LiteralPath $envFile" not in script
        assert "docker compose" not in script.lower()


def test_all_public_demo_scripts_parse_in_windows_powershell_51() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is not available")

    paths = [
        DEMO / "PublicDemo.Common.ps1",
        DEMO / "Start-PublicDemo.ps1",
        DEMO / "Get-PublicDemoStatus.ps1",
        DEMO / "Stop-PublicDemo.ps1",
        DEMO / "Test-PublicDemo.ps1",
    ]
    escaped = [str(path).replace("'", "''") for path in paths]
    path_literals = ",".join(f"'{path}'" for path in escaped)
    command = rf"""
foreach ($path in @({path_literals})) {{
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $path, [ref]$tokens, [ref]$errors
    )
    if (@($errors).Count -ne 0) {{ throw ($errors | Out-String) }}
}}
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
