# CLIP-002 — Qwen baseline

Дата проверки: 2026-11-23. Базовый commit до изменений: `6c0d89a`.

## Итог

Целевая модель `qwen3.5:9b` установлена из официального Ollama registry и
проверена на NVIDIA RTX 5070 Ti. Полный сценарий upload → dense/sparse index → Qwen
answer → citations → trace → deactivate → search → restore → search выполнен на
отдельных ресурсах. Содержательный валидатор прошёл все проверки.

## Восстановление official pull

| Проверка | Результат |
|---|---|
| Прямой HTTPS к `registry.ollama.ai:443` | timeout |
| Отдельный Ollama с `GODEBUG=tlsmlkem=0` | тот же `TLS handshake timeout` |
| Windows Internet proxy | включён, `127.0.0.1:10808` |
| WinHTTP proxy | direct |
| `curl --proxy http://127.0.0.1:10808` | registry доступен |
| Отдельный Ollama с process-only `HTTPS_PROXY` | official pull successful |
| Изменение firewall/proxy/TLS verification | не выполнялось |

Причиной оказался не ML-KEM, а то, что Ollama не использовал пользовательский Windows
proxy при прямом соединении. Временный процесс получил только локальные переменные
`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` и слушал `127.0.0.1:11435`.
Загрузка заняла 10 минут 20 секунд. Оба процесса использовали общий официальный model
store, поэтому после остановки диагностического процесса основной Ollama на
`127.0.0.1:11434` сразу увидел модель.

Модель:

- name: `qwen3.5:9b`;
- digest: `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`;
- on-disk size: 6 550 825 550 bytes (Ollama list: 6.6 GB);
- parameters: 9.0B;
- quantization: Q4_K_M;
- family: qwen35;
- capabilities: completion, vision, tools, thinking.

## Прямая проверка Ollama API

Запрос передавал `think: false` на верхнем уровне, а в `options`:
`temperature: 0`, `num_predict: 512`.

Первый вызов сразу после установки:

| Поле | Значение |
|---|---:|
| Content | `Готов.` |
| Thinking | `null` |
| Done reason | `stop` |
| Prompt / completion tokens | 18 / 4 |
| Wall time | 41.586 s |
| Ollama total duration | 41.478 s |
| Load duration | 18.007 s |

Повторная проверка через стандартный `127.0.0.1:11434` после остановки временного
процесса также вернула `Готов.`, `thinking=null`, `done_reason=stop`,
`100% GPU`. Wall time: 2.870 s; load duration: 2.638 s. Файлы модели к этому моменту
были в системном cache, поэтому это не first-ever cold start.

## Изолированные ресурсы CLIP-002

- PostgreSQL: `clip_agent_2026_clip002`;
- Qdrant: `clip_agent_dense_v1_clip002`, 4 points, vectors=1024, Cosine;
- OpenSearch: `clip_agent_sparse_v1_clip002`, 4 documents;
- storage: `storage/clip002`;
- Ollama endpoint during measurement: `http://127.0.0.1:11435`.

CLIP-001 database/collection/index не удалялись и не использовались. База `rag` и
индексы прежнего проекта не изменялись.

## Qwen smoke baseline

Локальный сырой отчёт: `smoke-results-qwen.json` (исключён из Git). Перед cold
RAG-вопросом smoke отправил `keep_alive: 0` и подтвердил пустой `/api/ps`.

### Индексация

| Файл | Время |
|---|---:|
| `agency_contract.txt` | 3.646 s |
| `order_101.txt` | 2.000 s |
| `order_102.txt` | 1.986 s |
| `product_alpha.txt` | 1.928 s |

BGE-M3 уже находился в локальном cache. Время его первоначального скачивания в этот
baseline не входит.

### Запросы

| Фаза | Wall | Backend | Ollama total | Load | Prompt eval | Generation | Tokens in/out | Found/selected |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Cold after unload | 8.587 s | 8576 ms | 7011.803 ms | 4976.943 ms | 735.422 ms | 1293.040 ms | 567 / 147 | 4 / 4 |
| Warm | 2.938 s | 2929 ms | 1437.225 ms | 4.001 ms | 33.079 ms | 1394.886 ms | 567 / 147 | 4 / 4 |
| Order 102 inactive | 3.979 s | 3946 ms | 2557.586 ms | 2.999 ms | 105.327 ms | 2410.746 ms | 429 / 254 | 4 / 3 |
| Order 102 restored | 2.967 s | 2933 ms | 1516.729 ms | 3.001 ms | 34.253 ms | 1422.495 ms | 567 / 147 | 4 / 4 |

Все четыре ответа: `done_reason=stop`, `empty_answer=false`. Пустых ответов: 0.
Exact reason and Ollama durations записаны в PostgreSQL `search_traces.details`.

### RAM / VRAM

| Snapshot | GPU VRAM used | GPU utilization | System RAM used |
|---|---:|---:|---:|
| Before cold, model unloaded | 5716 MiB | 1% | 49705 MiB |
| Immediately after cold | 12186 MiB | 84% | 50495 MiB |
| Immediately after warm | 12190 MiB | 85% | 50609 MiB |

Observed VRAM delta после cold: 6470 MiB. Ollama `/api/ps` сообщил model
`size_vram=5 606 337 412` bytes (около 5347 MiB) и `100% GPU`.
Снимки `nvidia-smi` системные и включают другие приложения. Истинный peak между
снимками и process-only RAM: **not measured**.

### Проверка содержания

Фактический ответ:

- назвал 0,05% для приказа № 102 и сослался на `order_102.txt` через `[1]`;
- назвал 0,10% для приказа № 101 и сослался на `order_101.txt` через `[2]`;
- объяснил различие областями применения документов;
- явно указал, что документы не содержат прямой отмены или определения приоритета;
- не объявил более поздний приказ автоматически главенствующим.

Автоматические проверки:

| Проверка | Результат |
|---|---|
| Непустой ответ | passed |
| Обе ставки | passed |
| 0,10% → order 101 citation | passed |
| 0,05% → order 102 citation | passed |
| Все citation IDs существуют | passed |
| Приоритет не выдуман | passed |
| Деактивация исключает order 102 | passed |
| Restore возвращает order 102 | passed |
| Четыре traces записаны | passed |

## TinyLlama diagnostic — отдельно от Qwen

CLIP-001 запускался с `tinyllama:latest` только для диагностики контура. Его данные не
смешиваются с Qwen baseline:

| Фаза | Wall | Input / output tokens | Результат цитат |
|---|---:|---:|---|
| Первый | 29.759 s | 1600 / 89 | failed |
| Повторный | 33.604 s | 1600 / 20480* | failed |
| Документ выключен | 2.701 s | 1401 / 894 | not accepted |
| Документ восстановлен | 1.477 s | 1600 / 86 | not accepted |

* Аномальное значение старого диагностического запуска до ограничения
`num_predict=512`. TinyLlama baseline считается невалидным по качеству и не заменяет
Qwen baseline.

## Проверки проекта

- Python dependencies: no broken requirements;
- pytest: `6 passed in 5.47s`;
- Ruff: `All checks passed!`;
- `pip check`: `No broken requirements found`;
- frontend build: успешно, Vite 6.4.6, 29 modules, 533 ms;
- `npm audit --omit=dev`: 0 vulnerabilities.

## Оставшиеся ограничения

- Системные snapshots не являются точным profiler peak.
- Содержательная проверка основана на синтетических примерах и явных правилах; это не
  экспертная оценка реального корпоративного корпуса.
- Browser UI не измерялся повторно в CLIP-002; backend/frontend код не менялся по
  архитектуре.
