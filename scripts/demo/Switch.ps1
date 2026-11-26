$ErrorActionPreference = 'Stop'

$ts = 'C:\Program Files\Tailscale\tailscale.exe'
$caddyDirectory = 'C:\Users\mvkiselev\AppData\Local\Microsoft\WinGet\Packages\CaddyServer.Caddy_Microsoft.Winget.Source_8wekyb3d8bbwe'
$env:Path = "$caddyDirectory;$env:Path"

$statePath = '.\storage\demo-logs\public-demo-state.json'
$manualFunnelDisabled = $false

try {
    # Проверки до отключения публичного доступа
    Get-Command caddy -ErrorAction Stop | Out-Null

    $before = .\scripts\demo\Get-PublicDemoStatus.ps1 -Json |
        ConvertFrom-Json

    if (-not $before.healthy -or
        $before.funnel.managed -or
        (Test-Path $statePath)) {
        throw 'Preflight failed: unexpected demo state.'
    }

    # Отключаем только ручной Funnel
    & $ts funnel --bg --https=443 off
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to disable manual Funnel.'
    }
    $manualFunnelDisabled = $true

    # Включаем Funnel через CLIP-010
    .\scripts\demo\Start-PublicDemo.ps1 `
        -StartFunnel `
        -TailscalePath $ts

    # Проверяем результат переключения
    $after = .\scripts\demo\Get-PublicDemoStatus.ps1 -Json |
        ConvertFrom-Json

    if (-not $after.healthy -or
        -not $after.funnel.managed -or
        $after.fastapi.managed -or
        $after.caddy.managed -or
        $after.fastapi.pid -ne $before.fastapi.pid -or
        $after.caddy.pid -ne $before.caddy.pid) {
        throw 'Post-switch verification failed.'
    }

    Write-Host 'SUCCESS: CLIP-010 Funnel is project-managed.'
    $after | ConvertTo-Json -Depth 6

} catch {
    Write-Warning "Switch failed: $($_.Exception.Message)"

    if ($manualFunnelDisabled) {
        $canRestore = $true

        # Если CLIP-010 успел создать owned Funnel,
        # сначала корректно освобождаем его.
        if (Test-Path $statePath) {
            try {
                .\scripts\demo\Stop-PublicDemo.ps1 `
                    -TailscalePath $ts

                if (Test-Path $statePath) {
                    throw 'Lifecycle state still exists.'
                }
            } catch {
                $canRestore = $false
                Write-Warning "Automatic cleanup failed: $($_.Exception.Message)"
            }
        }

        if ($canRestore) {
            Write-Host 'Restoring previous manual Funnel...'
            & $ts funnel --bg --https=443 8080

            if ($LASTEXITCODE -eq 0) {
                Write-Host 'Manual Funnel restoration command succeeded.'
            } else {
                Write-Warning 'Manual Funnel restoration failed.'
            }
        } else {
            Write-Warning 'Automatic restoration skipped to avoid conflicting ownership.'
        }
    }
}
