# CLIP-011 — защищённая API-документация для жюри

## Адреса и граница доступа

После согласованного применения нового Caddyfile документация будет доступна на
действующем публичном стенде по тем же HTTPS origin и Basic Auth, что и React UI:

- Swagger UI: `https://kronwerk.tail9a1e57.ts.net/docs`;
- ReDoc: `https://kronwerk.tail9a1e57.ts.net/redoc`;
- OpenAPI JSON: `https://kronwerk.tail9a1e57.ts.net/openapi.json`.

Имя пользователя — `jury`; пароль владелец передаёт отдельно. Не помещать пароль
или Basic header в URL, командную строку, Git либо журналы. Первый переход в
браузере показывает стандартный Basic Auth prompt. После успешного входа браузер
обычно повторно отправляет credentials всем запросам того же origin, поэтому
Swagger UI получает `/openapi.json` и вызывает `/api/*` без CORS-обходов. Для
смены пользователя может потребоваться закрыть все окна браузера либо использовать
отдельный приватный профиль.

Caddy применяет `basic_auth` ко всему сайту до выбора React/FastAPI handler,
удаляет `Authorization` перед FastAPI и из access log. FastAPI по-прежнему слушает
только loopback и напрямую в интернет не публикуется.

## Работа со Swagger UI

Кнопка **Try it out** вызывает существующие рабочие API и существующие ресурсы
`clip_agent_2026`, `clip_agent_dense_v1`, `clip_agent_sparse_v1` и рабочий
`STORAGE_ROOT`. Это не sandbox и не отдельная демонстрационная база.

Без отдельного разрешения владельца использовать только GET-запросы. POST upload,
ask/feedback, deactivate и restore могут соответственно загрузить документ,
создать trace/feedback или изменить видимость документа. Basic Auth является общей
защитой ingress, но не ролевой моделью: пользователь `jury` после входа технически
видит все опубликованные методы OpenAPI.

## Внешние assets документации

Текущая версия FastAPI формирует HTML со следующими внешними URL:

- `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js`;
- `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css`;
- `https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js`;
- `https://fastapi.tiangolo.com/img/favicon.png`.

На 2026-11-25 все четыре URL проверены с машины стенда и отвечали HTTP 200.
Версии Swagger/ReDoc закреплены только по major, поэтому остаются риски внешней
доступности и supply chain. Если интернет-доступ жюри к CDN ненадёжен, минимальный
fallback — сохранить точные версии `swagger-ui-dist` и ReDoc вместе с их LICENSE,
зафиксировать URL/версию/SHA-256, раздавать только нужные JS/CSS/favicon локально и
создать FastAPI docs routes через `get_swagger_ui_html`/`get_redoc_html` с
same-origin asset URL. Это отдельное изменение backend и должно пройти review;
текущая задача не добавляет крупные frontend-зависимости или непроверенные bundles.

## Безопасное применение к работающему стенду

Выполнять только после отдельного разрешения владельца, из корня репозитория в
консоли владельца работающего Caddy. Команды не перезапускают FastAPI, Caddy или
Tailscale Funnel: `caddy reload` атомарно загружает новую конфигурацию в текущий
процесс.

Сначала проверить исходное состояние и запросить credentials без вывода пароля:

```powershell
.\scripts\demo\Get-PublicDemoStatus.ps1
$credential = Get-Credential -UserName jury
```

Если status не подтверждает здоровые FastAPI/Caddy/Funnel и публичный `401`, не
продолжать. Затем подготовить ровно то process-only окружение, которое использует
launcher, и определить executable уже работающего Caddy:

```powershell
$repoRoot = (Resolve-Path -LiteralPath '.').Path
. .\scripts\demo\PublicDemo.Common.ps1
Import-DemoDotEnv (Join-Path $repoRoot '.env')

if ($env:PUBLIC_DEMO_ENABLED -cne 'true' -or $env:DEMO_AUTH_USERNAME -cne 'jury') {
    throw 'Public demo configuration is not enabled for jury.'
}
if ($env:DEMO_AUTH_PASSWORD_HASH -notmatch '^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$') {
    throw 'DEMO_AUTH_PASSWORD_HASH is not a bcrypt hash.'
}

$proxyPort = if ($env:DEMO_PROXY_PORT) { [int]$env:DEMO_PROXY_PORT } else { 8080 }
$backendPort = if ($env:DEMO_BACKEND_PORT) { [int]$env:DEMO_BACKEND_PORT } else { 8000 }
$maxUploadMb = if ($env:DEMO_MAX_UPLOAD_MB) { [int]$env:DEMO_MAX_UPLOAD_MB } else { 20 }
$logDir = Join-Path $repoRoot 'storage\demo-logs'
$env:DEMO_PROXY_PORT = [string]$proxyPort
$env:DEMO_BACKEND_PORT = [string]$backendPort
$env:DEMO_FRONTEND_ROOT = (Join-Path $repoRoot 'frontend\dist').Replace('\', '/')
$env:DEMO_ACCESS_LOG = (Join-Path $logDir 'caddy-access.json').Replace('\', '/')
$env:DEMO_MAX_REQUEST_BODY = '{0}MiB' -f ($maxUploadMb + 1)

$caddyPaths = @(
    Get-Process -Name caddy -ErrorAction Stop |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_.Path) } |
        Select-Object -ExpandProperty Path -Unique
)
if ($caddyPaths.Count -ne 1 -or -not (Test-Path -LiteralPath $caddyPaths[0] -PathType Leaf)) {
    throw 'Cannot identify exactly one running Caddy executable.'
}
$caddy = $caddyPaths[0]
$caddyFile = Join-Path $repoRoot 'scripts\demo\Caddyfile'
```

Не выводить значения environment variables. Сохранить точную активную JSON
конфигурацию через loopback-only admin API в пользовательский временный каталог,
а затем проверить backup и новый Caddyfile до reload:

```powershell
$rollbackConfig = Join-Path $env:TEMP (
    'clip011-caddy-active-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
)
Invoke-WebRequest -Uri 'http://127.0.0.1:2019/config/' `
    -OutFile $rollbackConfig -UseBasicParsing -TimeoutSec 10
if ((Get-Item -LiteralPath $rollbackConfig).Length -eq 0) {
    throw 'Caddy active configuration backup is empty.'
}

& $caddy validate --config $rollbackConfig
if ($LASTEXITCODE -ne 0) { throw 'Active Caddy configuration backup is invalid.' }
& $caddy validate --config $caddyFile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'New Caddyfile validation failed.' }
```

Активный JSON содержит bcrypt hash: не печатать его, не прикладывать к отчёту и
не переносить из пользовательского `%TEMP%`. Применение и проверка:

```powershell
try {
    & $caddy reload --config $caddyFile --adapter caddyfile
    if ($LASTEXITCODE -ne 0) { throw 'Caddy reload failed.' }

    .\scripts\demo\Get-PublicDemoStatus.ps1
    .\scripts\demo\Test-PublicDemo.ps1 `
        -BaseUri 'https://kronwerk.tail9a1e57.ts.net' `
        -Credential $credential
} catch {
    $failure = $_.Exception.Message
    & $caddy reload --config $rollbackConfig
    if ($LASTEXITCODE -ne 0) {
        throw "New configuration failed and automatic Caddy rollback also failed: $failure"
    }
    throw "New configuration failed; previous active Caddy configuration restored: $failure"
}
```

После успешной приёмки удалить чувствительный временный JSON:

```powershell
Remove-Item -LiteralPath $rollbackConfig
```

## Ручной откат после успешного reload

Пока `$rollbackConfig` сохранён, выполнить:

```powershell
& $caddy validate --config $rollbackConfig
if ($LASTEXITCODE -ne 0) { throw 'Rollback configuration is invalid.' }
& $caddy reload --config $rollbackConfig
if ($LASTEXITCODE -ne 0) { throw 'Caddy rollback failed.' }
.\scripts\demo\Get-PublicDemoStatus.ps1
```

Откат меняет только конфигурацию работающего Caddy. Он не отключает Funnel, не
останавливает FastAPI и не меняет базы, индексы, документы или `.env`.
