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

Если `ollama pull` завершается `TLS handshake timeout`, проверить без изменения сети:

```powershell
Resolve-DnsName registry.ollama.ai
Test-NetConnection registry.ollama.ai -Port 443
curl.exe -I --connect-timeout 15 https://registry.ollama.ai/v2/
```

Нужен исходящий HTTPS к `registry.ollama.ai:443`. После восстановления доступа повторить
`ollama pull qwen3.5:9b`. Настройки proxy/firewall самостоятельно не менять.

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
.\.venv\Scripts\python.exe scripts\smoke_test.py --output smoke-results.json
```

Smoke загружает все `examples/*.txt`, задаёт вопрос о противоречащих ставках,
деактивирует один приказ, повторяет поиск, восстанавливает документ и проверяет traces.
Повторный запуск создаёт новые ревизии и предназначен только для отдельной тестовой базы.
Результат содержит реальные timings, токены, RAM/VRAM snapshots и проверки цитат.
