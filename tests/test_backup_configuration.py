import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest


ROOT = Path(__file__).resolve().parents[1]


def test_backup_script_is_scoped_and_fail_closed() -> None:
    script = (ROOT / "scripts" / "backup" / "Backup-Clip.ps1").read_text(
        encoding="utf-8"
    )

    for resource in (
        "clip_agent_2026",
        "clip_agent_dense_v1",
        "clip_agent_sparse_v1",
    ):
        assert resource in script

    assert 'if ($Backup -and' in script
    assert "Active document indexing detected" in script
    assert "snapshot repository" in script
    assert 'snapshotResult.snapshot.state -ne "SUCCESS"' in script
    assert "include_global_state = $false" in script
    assert "pg_restore --list" in script
    assert "Get-FileHash" in script
    assert "manifest.json" in script
    assert "complete = $false" in script


def test_backup_script_has_no_destructive_store_operations() -> None:
    script = (ROOT / "scripts" / "backup" / "Backup-Clip.ps1").read_text(
        encoding="utf-8"
    ).lower()

    forbidden = (
        "drop database",
        "drop table",
        "truncate ",
        "docker compose down",
        "pg_restore --clean",
        "delete /_snapshot",
        "remove-item",
    )
    for text in forbidden:
        assert text not in script


def test_backup_outputs_are_outside_git_and_env_is_not_copied() -> None:
    script = (ROOT / "scripts" / "backup" / "Backup-Clip.ps1").read_text(
        encoding="utf-8"
    )

    assert "BackupBase must be outside the Git repository" in script
    assert 'PRIVATE_BACKUP_DO_NOT_PUBLISH.txt' in script
    assert 'Join-Path $Script:BackupRoot ".gitignore"' in script
    assert "Copy-Item -LiteralPath $EnvFile" not in script


def test_repository_config_does_not_collide_with_typed_parameter() -> None:
    script = (ROOT / "scripts" / "backup" / "Backup-Clip.ps1").read_text(
        encoding="utf-8"
    )

    assert "$repositoryConfig = $property.Value" in script
    assert "$repository = $property.Value" not in script
    assert "$repositoryConfig.type" in script


def test_pytest_basetemp_inside_working_storage_is_rejected() -> None:
    config = SimpleNamespace(
        getoption=lambda name: str(ROOT / "storage" / "pytest-forbidden")
        if name == "basetemp"
        else None
    )

    with pytest.raises(pytest.UsageError, match="outside the working STORAGE_ROOT"):
        conftest.pytest_configure(config)

    assert not (ROOT / "storage" / "pytest-forbidden").exists()


def test_docker_mounts_and_desktop_path_in_windows_powershell_51() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is not available")

    script_path = str(ROOT / "scripts" / "backup" / "Backup-Clip.ps1").replace(
        "'", "''"
    )
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{script_path}', [ref]$tokens, [ref]$errors
)
if (@($errors).Count -ne 0) {{ throw ($errors | Out-String) }}
foreach ($functionName in @(
    'ConvertFrom-DockerBindSource',
    'ConvertFrom-DockerMountJson',
    'Get-DockerBindMountForDestination'
)) {{
    $function = $ast.Find({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $functionName
    }}, $true)
    if ($null -eq $function) {{ throw "Function '$functionName' was not found." }}
    Invoke-Expression $function.Extent.Text
}}

$mountJson = @'
[
  {{"Type":"volume","Source":"/var/lib/docker/volumes/rag_opensearch_data/_data","Destination":"/usr/share/opensearch/data"}},
  {{"Type":"bind","Source":"/run/desktop/mnt/host/c/Users/owner/Documents/backups","Destination":"/mnt/clip-backups"}}
]
'@
$mounts = @(ConvertFrom-DockerMountJson $mountJson)
if ($mounts.Count -ne 2) {{
    throw "Expected two separate Docker mounts, found $($mounts.Count)."
}}
$repositoryMount = Get-DockerBindMountForDestination `
    -Mounts $mounts `
    -Destination '/mnt/clip-backups'
if ($repositoryMount.Type -cne 'bind') {{ throw 'Repository mount is not a bind.' }}
if ($repositoryMount.Destination -cne '/mnt/clip-backups') {{
    throw 'Repository mount destination mismatch.'
}}

$expected = 'C:\Users\owner\Documents\backups'
if ((ConvertFrom-DockerBindSource $repositoryMount.Source) -ne $expected) {{
    throw 'Docker Desktop path conversion failed.'
}}
if ((ConvertFrom-DockerBindSource $expected) -ne $expected) {{
    throw 'Absolute Windows path conversion failed.'
}}
$failedClosed = $false
try {{ Get-DockerBindMountForDestination -Mounts $mounts -Destination '/usr/share/opensearch/data' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Named volume was accepted as repository bind.' }}
$failedClosed = $false
try {{ Get-DockerBindMountForDestination -Mounts $mounts -Destination '/mnt' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Partial destination match did not fail closed.' }}
$failedClosed = $false
try {{ ConvertFrom-DockerBindSource '/var/lib/docker/volumes/example' }}
catch {{ $failedClosed = $true }}
if (-not $failedClosed) {{ throw 'Unsupported bind source did not fail closed.' }}
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
