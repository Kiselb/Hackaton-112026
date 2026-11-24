# CLIP-008 — защищённый публичный стенд

## Граница решения

Публичный стенд — это тот же AI-Agent Clip, тот же `.env`, PostgreSQL
`clip_agent_2026`, Qdrant `clip_agent_dense_v1`, OpenSearch
`clip_agent_sparse_v1` и текущий `STORAGE_ROOT`. Отдельного backend, корпуса или
demo-базы нет. Жюри работает с общими данными и может загружать, деактивировать и
восстанавливать документы.

```text
browser --HTTPS--> ngrok --HTTP/loopback--> Caddy:8080 --HTTP/loopback--> FastAPI:8000
                                         |                         |
                                         +-- React production     +-- existing stores/Ollama
```

TLS завершается в ngrok. Туннель шифрует передачу по сети, но провайдер является
частью границы доверия и технически обрабатывает проходящий трафик. На стенде должны
быть только разрешённые для такой передачи данные. Caddy слушает только `127.0.0.1`,
проверяет Basic Auth до любого маршрута и не передаёт заголовок `Authorization` в
FastAPI. Backend, Ollama, PostgreSQL, Qdrant и OpenSearch не публикуются.

Basic Auth — ограниченная защита демонстрационного стенда, а не production IAM:
пользователь один (`jury`), роли и ACL отсутствуют, история и документы общие.

## Требования и установка

Нужны существующая Python 3.12 `.venv`, Node.js/npm для сборки, Caddy 2.10 или новее
и ngrok. Скрипты не устанавливают ПО, не меняют PATH, Firewall, proxy или Docker.

```powershell
# Один из вариантов установки Caddy, выбранный владельцем:
choco install caddy
# либо
scoop install caddy

# ngrok из Microsoft Store через WinGet:
winget install ngrok -s msstore
```

Альтернативно загрузить подписанные бинарники с официальных страниц и добавить их в
PATH вручную. Источники: [Caddy installation](https://caddyserver.com/docs/install),
[ngrok for Windows](https://ngrok.com/download/windows).

Подключить ngrok к аккаунту владельца (токен не помещать в `.env` проекта):

```powershell
ngrok config add-authtoken "<TOKEN_FROM_OWNER_ACCOUNT>"
```

Команда сохраняет токен в пользовательской конфигурации ngrok, не в Git. Каждый
аккаунт получает стабильный dev domain; другой зарезервированный домен указывается
через `--url`. См. [ngrok domains](https://ngrok.com/docs/gateway/domains).

## Пароль и единый `.env`

Caddy принимает только bcrypt-хеш. Генерация интерактивна: пароль не оказывается в
аргументах процесса или истории PowerShell.

```powershell
caddy hash-password
```

Введите сильный общий пароль дважды и скопируйте результат в существующий `.env`.
Символы `$` сохранять буквально; PowerShell launcher читает значение как данные и
передаёт его Caddy через process-only environment variable.

```dotenv
PUBLIC_DEMO_ENABLED=true
DEMO_AUTH_USERNAME=jury
DEMO_AUTH_PASSWORD_HASH=$2a$14$REPLACE_WITH_REAL_BCRYPT_HASH
DEMO_PROXY_PORT=8080
DEMO_BACKEND_PORT=8000
DEMO_MAX_CONCURRENT_LLM=1
DEMO_MAX_CONCURRENT_INGEST=1
DEMO_MAX_UPLOAD_MB=20
AGENT_ENABLED=false
AGENT_DEMO_OVERRIDE_ENABLED=true
NGROK_DOMAIN=owner-domain.ngrok-free.dev
```

Не менять DSN, коллекцию, индекс или `STORAGE_ROOT`. `.env` уже исключён из Git.
`NGROK_DOMAIN` — только hostname или `https://` URL, фактически принадлежащий аккаунту
владельца; не придумывать его. Для обычной локальной разработки оставить
`PUBLIC_DEMO_ENABLED=false`.

Смена пароля: остановить Caddy/ngrok, повторить `caddy hash-password`, заменить только
`DEMO_AUTH_PASSWORD_HASH`, затем запустить стенд и снова выполнить проверки. Исходный
пароль нигде не сохраняется.

## Резервная копия перед публикацией

Остановить новые операции с документами на время согласованного backup. Следующие
команды создают страховочную копию, а не второе рабочее окружение. Они не выполняют
restore и ничего не удаляют.

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupBase = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'AI-Agent-Clip-Backups'
$backupRoot = Join-Path $backupBase "clip008-$stamp"
New-Item -ItemType Directory -Path $backupRoot | Out-Null

# PostgreSQL client 16/17 должен быть доступен в PATH.
$dsnLine = Get-Content .env | Where-Object { $_ -match '^POSTGRES_DSN=' } | Select-Object -First 1
$pgDsn = ($dsnLine -split '=', 2)[1] -replace '^postgresql\+psycopg://', 'postgresql://'
pg_dump --dbname=$pgDsn --format=custom --file=(Join-Path $backupRoot 'clip_agent_2026.dump')

# Исходные файлы и локальные метаданные storage.
Compress-Archive -Path 'storage\*' -DestinationPath (Join-Path $backupRoot 'storage.zip')
```

Если `STORAGE_ROOT` отличается, использовать его фактическое значение. Не печатать
`$pgDsn`: оно может содержать пароль.

Qdrant поддерживает snapshot коллекции; создать и скачать snapshot
`clip_agent_dense_v1` через API, не затрагивая другие коллекции:

```powershell
$qdrantUrl = 'http://127.0.0.1:6333'
$collection = 'clip_agent_dense_v1'
$snapshot = Invoke-RestMethod -Method Post "$qdrantUrl/collections/$collection/snapshots?wait=true"
$snapshotName = $snapshot.result.name
Invoke-WebRequest "$qdrantUrl/collections/$collection/snapshots/$snapshotName" `
  -OutFile (Join-Path $backupRoot $snapshotName)
```

Для OpenSearch до публикации создать snapshot только индекса
`clip_agent_sparse_v1` в заранее настроенный snapshot repository. Repository требует
отдельного volume/`path.repo` или совместимого object storage; его настройка зависит от
текущего Docker compose и не должна выполняться скриптом молча. Пример после того, как
владелец настроил repository `clip_backup`:

```powershell
$body = @{ indices = 'clip_agent_sparse_v1'; include_global_state = $false } | ConvertTo-Json
Invoke-RestMethod -Method Put -ContentType 'application/json' -Body $body `
  'http://127.0.0.1:9200/_snapshot/clip_backup/clip008_before_jury?wait_for_completion=true'
```

Проверить ненулевые файлы PostgreSQL/storage/Qdrant и состояние OpenSearch snapshot
`SUCCESS`. Автоматического batch-rebuild индексов в CLIP-008 нет, поэтому открывать
туннель без этих копий не следует. Restore — отдельная destructive операция: сначала
остановить стенд, проверить точные targets и получить явное подтверждение владельца.

## Сборка и запуск

Из корня репозитория:

```powershell
# Первая активация или после изменений frontend:
.\scripts\demo\Start-PublicDemo.ps1 -RebuildFrontend

# Последующие локальные запуски с существующим frontend/dist:
.\scripts\demo\Start-PublicDemo.ps1
```

Launcher:

1. читает существующий `.env` без интерполяции `$`;
2. требует `PUBLIC_DEMO_ENABLED=true`, username `jury` и валидный bcrypt-хеш;
3. проверяет Python 3.12, Caddy 2.10+, порты и upload limit;
4. при необходимости собирает `frontend/dist`;
5. валидирует Caddyfile;
6. запускает FastAPI и Caddy только на `127.0.0.1`;
7. проверяет прямой backend health, внешний 401 и полный readiness;
8. печатает PID и точную команду остановки.

Production frontend и API используют один origin. Vite dev server остаётся прежним и
не участвует в публичном стенде. Caddy даёт SPA fallback и проксирует `/api/*`,
`/healthz`, `/readyz` в FastAPI. Proxy body limit на 1 MiB больше лимита файла, чтобы
учесть multipart framing; backend независимо применяет точный `DEMO_MAX_UPLOAD_MB`.

После локальной проверки остановить локальный запуск по напечатанным PID и запустить
новый экземпляр со стабильным HTTPS endpoint:

```powershell
.\scripts\demo\Start-PublicDemo.ps1 -StartTunnel
```

Launcher вызывает эквивалент `ngrok http 8080 --url https://<NGROK_DOMAIN>` и не
публикует порт FastAPI. Не сообщать URL жюри до внешней проверки с другого подключения.

## Проверка защиты и маршрутов

Сначала получить credential интерактивно, затем выполнить безопасный тест. Скрипт не
делает успешных POST и не меняет документы/traces.

```powershell
$credential = Get-Credential -UserName jury -Message 'Пароль публичного стенда'
.\scripts\demo\Test-PublicDemo.ps1 -Credential $credential
```

Проверяются 401 без credentials для root, ask, documents, deactivate/restore, traces,
feedback и health; 401 с неверным паролем; 200 с верным паролем для root, production
asset, documents и traces. Ту же команду выполнить с `-BaseUri
https://<NGROK_DOMAIN>` с внешней сети.

Дополнительно вручную в DevTools проверить, что запросы идут на текущий HTTPS origin,
нет CORS/mixed-content ошибок, а reload допустимого frontend URL возвращает SPA.
Заголовок `Authorization` удаляется до FastAPI и из Caddy access log; Caddy также
редактирует credential headers по умолчанию.

## Полная функциональная проверка

Автоматический lifecycle smoke нельзя запускать на основных ресурсах. На временных
изолированных PostgreSQL schema/database, Qdrant collection, OpenSearch index и storage
выполнить существующий `scripts/smoke_test.py`, затем browser QA production UI:

1. login как `jury`, desktop и мобильная ширина;
2. загрузка TXT/MD/PDF/DOCX в пределах лимита и ожидание `ready`;
3. Baseline answer с источниками и parent context;
4. request-level Agent, trajectory и Dashboard;
5. Feedback;
6. deactivate, исключение из поиска, restore и возвращение;
7. второй параллельный ask/ingest получает 429 без бесконечного ожидания;
8. upload сверх лимита получает 413.

Проверку на изолированных ресурсах запускать отдельным процессом с временными
process-only настройками. Не менять основной `.env` на тестовые targets. После
проверки удалить только явно созданные временные ресурсы с отдельным подтверждением.

## Остановка и перезапуск Windows

Использовать напечатанные launcher-ом PID:

```powershell
Stop-Process -Id <backend_pid>,<caddy_pid>,<ngrok_pid>
```

После остановки убедиться, что `http://127.0.0.1:8080` недоступен и endpoint ngrok
offline. После перезапуска Windows запустить Docker/WSL stores, Ollama, проверить
`ollama list`, затем снова `Start-PublicDemo.ps1 -StartTunnel`. Скрипты не создают
Windows services и не стартуют автоматически.

## Ограничения и риски

- общий пароль и общий набор данных; действия участников неразличимы;
- нет ACL, malware scan, PDF sandbox, OCR и гарантированного разбора таблиц;
- Basic Auth защищён только при HTTPS на внешнем участке; локальный Caddy↔ngrok hop
  остаётся loopback HTTP;
- ngrok входит в границу доверия и может иметь account/traffic limits;
- процесс на одной рабочей станции недоступен при sleep, reboot или сетевом сбое;
- лимиты 429 защищают GPU/CPU от случайной конкуренции, но не являются полноценным
  rate limiter против подбора пароля;
- синхронная индексация не является транзакцией сразу для трёх хранилищ;
- основной корпус реально меняется действиями жюри; автоматического rollback нет.
