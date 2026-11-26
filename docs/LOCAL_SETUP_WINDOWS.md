# Локальный запуск в Windows

Проверенный сценарий: backend и frontend работают непосредственно в Windows, Ollama также
установлен в Windows, а PostgreSQL, Qdrant и OpenSearch опубликованы из Docker/WSL на
loopback-порты. Менять сетевой режим Docker или переносить Ollama в WSL не требуется.

## Проверенная среда

Снимок от 2026-11-23:

| Компонент | Версия / конфигурация |
|---|---|
| Windows Python | 3.12; запускать через `py -3.12` |
| Системный Python | 3.14.2; для проекта не использовать |
| Node.js / npm | 22.23.4 / 10.9.9 |
| Docker CLI | 28.5.1 |
| PostgreSQL | 17, Windows port 5432 |
| Qdrant | 1.15.5, Windows ports 6333–6334 |
| OpenSearch | 3.8.0, Windows port 9200 |
| Ollama | 0.40.14, `http://127.0.0.1:11434` |
| GPU | NVIDIA GeForce RTX 5070 Ti, 16 303 MiB, driver 581.29 |
| Dense embeddings | `BAAI/bge-m3`, 1024 dimensions, CPU |
| Целевая LLM | `qwen3.5:9b` в Ollama |

## 1. Проверка портов

```powershell
Test-NetConnection 127.0.0.1 -Port 5432
Invoke-RestMethod http://127.0.0.1:6333/collections
Invoke-RestMethod http://127.0.0.1:9200
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

Если Docker-сервис не доступен, сначала проверить `docker ps` и публикацию порта вида
`0.0.0.0:PORT->PORT/tcp`. Минимальное решение — добавить публикацию соответствующего
порта в существующий compose-файл и перезапустить только этот сервис. Не менять firewall,
WSL networking или Docker network без отдельного согласования.

## 2. Отдельные ресурсы проекта

Приложение использует только:

- PostgreSQL database `clip_agent_2026`;
- Qdrant collection `clip_agent_dense_v1`;
- OpenSearch index `clip_agent_sparse_v1`.

Пример создания пользователя и базы в существующем PostgreSQL-контейнере:

```powershell
docker exec <postgres-container> psql -U <admin-user> -d postgres -c "CREATE ROLE clip_agent LOGIN PASSWORD '<LOCAL_PASSWORD>'"
docker exec <postgres-container> createdb -U <admin-user> -O clip_agent clip_agent_2026
```

Команды выполняются один раз. Они не меняют схемы базы `rag`. Коллекция и индекс
создаются приложением автоматически при первой успешной загрузке документа.

## 3. Конфигурация и установка

```powershell
Copy-Item .env.example .env
# В .env заменить CHANGE_ME; файл исключён из Git.

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Set-Location frontend
npm install
Set-Location ..
```

Первый запуск скачивает BGE-M3 (примерно 2,3 GB; cache Hugging Face может потребовать
больше места на Windows без symlink). Для Ollama:

```powershell
ollama pull qwen3.5:9b
ollama list
```

Параметры Qwen по умолчанию уже находятся в `.env.example`:

```dotenv
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_THINK=false
OLLAMA_TEMPERATURE=0
OLLAMA_NUM_PREDICT=512
RERANK_ENABLED=false
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_MODEL_REVISION=
RERANK_DEVICE=cpu
RERANK_CANDIDATES=24
RERANK_TOP_K=8
AGENT_ENABLED=true
AGENT_MAX_SEARCH_CALLS=3
AGENT_MAX_RETRIES=1
AGENT_MAX_LLM_CALLS=4
AGENT_DEMO_OVERRIDE_ENABLED=false
```

`OLLAMA_THINK` передаётся верхнеуровневым полем `/api/chat`, а остальные два значения —
через `options`. Пустой ответ считается ошибкой, а `done_reason`, token counts и Ollama
durations сохраняются в trace.

Reranker загружается лениво только при RERANK_ENABLED=true. Для воспроизводимого
эксперимента CLIP-005 использована revision
953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e; без явной revision Hugging Face может
разрешить более новую версию модели. Reranker работает на CPU и не меняет размещение
Ollama на GPU.

Bounded AI-Агент включён по умолчанию для новых конфигураций. Он использует тот же
локальный Qwen и существующий hybrid retrieval; лимиты search/retry/LLM применяются при
каждом исполнении. При runtime error автоматический fallback в Базовый RAG не выполняется.
Отдельные внешние API и shared memory не используются. Существующий `.env` не
перезаписывается: владелец меняет `AGENT_ENABLED` вручную только в согласованное окно.

Для воспроизводимой локальной демонстрации trajectory можно оставить
`AGENT_ENABLED=false`, установить `AGENT_DEMO_OVERRIDE_ENABLED=true` и перезапустить
backend. В браузере появится выбор режима для конкретного запроса. Флаг нельзя включать
на публично доступном интерфейсе: в MVP нет аутентификации, а override предназначен
только для loopback-демо и не меняет глобальную настройку сервера.

### Диагностика TLS timeout

Если `ollama pull` завершается `TLS handshake timeout`, сначала выполнить read-only
проверки без изменения сети:

```powershell
Resolve-DnsName registry.ollama.ai
Test-NetConnection registry.ollama.ai -Port 443
curl.exe -I --connect-timeout 15 https://registry.ollama.ai/v2/
netsh winhttp show proxy
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' |
  Select-Object ProxyEnable, ProxyServer
```

На проверенной машине прямой IPv4 HTTPS зависал, а локальный Windows proxy был настроен
на `127.0.0.1:10808`. Гипотеза ML-KEM была проверена отдельным процессом Ollama:
`GODEBUG=tlsmlkem=0` в одиночку не устранил timeout. Успешной оказалась передача уже
существующего proxy только диагностическому процессу; глобальные переменные, firewall,
Docker networking и проверка TLS-сертификатов не менялись.

Подставить адрес из собственной настройки `ProxyServer`, затем запустить отдельный
Ollama в терминале 1:

```powershell
$env:GODEBUG='tlsmlkem=0'
$env:OLLAMA_HOST='127.0.0.1:11435'
$env:HTTP_PROXY='http://127.0.0.1:10808'
$env:HTTPS_PROXY='http://127.0.0.1:10808'
$env:NO_PROXY='127.0.0.1,localhost'
ollama serve
```

В терминале 2:

```powershell
$env:OLLAMA_HOST='http://127.0.0.1:11435'
ollama pull qwen3.5:9b
ollama show qwen3.5:9b
```

После загрузки остановить диагностический процесс. Обычный Ollama на `11434` увидит
модель в общем локальном хранилище. `GODEBUG=tlsmlkem=0` здесь оставлен только для
воспроизводимости проверки гипотезы и не считается исправлением.

Если официальный registry недоступен даже через разрешённый proxy, допустим явный
локальный GGUF-импорт. Скачать совместимый GGUF из доверенного источника, проверить
checksum и создать модель под отличающимся именем:

```text
# Modelfile
FROM C:\models\qwen3.5-9b-q4_k_m.gguf
PARAMETER num_ctx 4096
```

```powershell
ollama create qwen3.5:9b-local-gguf -f .\Modelfile
```

Затем явно указать `OLLAMA_MODEL=qwen3.5:9b-local-gguf` и отметить альтернативу в
результатах. Нельзя молча выдавать импорт за официальный `qwen3.5:9b`; для vision также
потребуются совместимые projector-файлы. Текущий baseline использует официальный registry.

## 4. Запуск

Терминал 1:

```powershell
.\.venv\Scripts\python.exe -m uvicorn clip_agent.main:app --host 127.0.0.1 --port 8000
```

Терминал 2:

```powershell
Set-Location frontend
npm run dev
```

Открыть `http://127.0.0.1:5173`. Проверки:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/readyz
ollama ps
```

`/readyz` проверяет PostgreSQL, Qdrant, OpenSearch, Ollama и наличие настроенной модели.
`ollama ps` должен показывать `100% GPU` для активной модели.

## 5. Тесты и smoke

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check backend tests scripts
Set-Location frontend
npm run build
npm audit --omit=dev
Set-Location ..
```

На свежих ресурсах выполнить:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py --model qwen3.5:9b --output smoke-results-qwen.json
```

Smoke загружает все `examples/*.txt`, задаёт вопрос о противоречащих ставках,
деактивирует один приказ, повторяет поиск, восстанавливает документ и проверяет traces.
Повторный запуск создаёт новые ревизии и предназначен только для отдельной тестовой базы.
Результат содержит реальные timings, токены, RAM/VRAM snapshots и проверки цитат.
