from pathlib import Path


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
