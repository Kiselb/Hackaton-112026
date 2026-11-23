# CLIP-001 — результаты baseline

Дата проверки: 2026-11-23. Базовый commit до изменений: `089f4f0`.

## Статус

Целевой baseline с `qwen3.5:9b` **не измерен**. Модель отсутствовала, а две попытки
`ollama pull qwen3.5:9b` завершились `TLS handshake timeout` при получении manifest.
Отдельная проверка показала: DNS разрешается, но HTTPS-соединение с
`registry.ollama.ai:443` завершается timeout. Настройки Windows/Docker не менялись.

Для проверки технического контура выполнен диагностический smoke с уже имевшейся
`tinyllama:latest`. Эти числа нельзя использовать как показатели целевой модели.

## Подтверждённые проверки

| Проверка | Результат |
|---|---|
| Python 3.12 venv / установка | passed |
| PostgreSQL из Windows | passed, `127.0.0.1:5432` |
| Qdrant из Windows | passed, HTTP 200 |
| OpenSearch из Windows | passed, HTTP 200 |
| Ollama API из Windows | passed, HTTP 200 |
| Таблицы при startup | `documents`, `chunks`, `search_traces` созданы |
| Отдельная Qdrant collection | `clip_agent_dense_v1`, создана при upload |
| Отдельный OpenSearch index | `clip_agent_sparse_v1`, создан при upload |
| React production build | passed, Vite 6.4.6 |
| npm production audit | 0 vulnerabilities |
| Unit tests | 3 passed |
| Ruff | passed |
| Frontend dev server / API proxy | HTTP 200 / HTTP 200 |
| Интерактивная browser-проверка | not measured: browser controller crashed on connect |

База `rag` не использовалась приложением. В PostgreSQL создана новая база
`clip_agent_2026`; старые Qdrant/OpenSearch имена не удалялись и не изменялись.

## Диагностический smoke (не Qwen baseline)

Файл измерений: локальный `smoke-results.json` (исключён из Git). Модель:
`tinyllama:latest`; `ollama ps` показал `100% GPU`.

### Индексация

| Файл | Время |
|---|---:|
| `agency_contract.txt` | 2.347 s |
| `order_101.txt` | 1.611 s |
| `order_102.txt` | 1.636 s |
| `product_alpha.txt` | 1.545 s |

Первичная загрузка BGE-M3 и весов не включена в эти timings: cold download занял
несколько минут и прерывался/возобновлялся после CDN timeout. Точное полное время
cold download: **not measured**.

### Запросы

| Фаза | Полное время | Backend latency | Input tokens | Output tokens | Найдено / отобрано |
|---|---:|---:|---:|---:|---:|
| Первый | 29.759 s | 29 750 ms | 1600 | 89 | 8 / 8 |
| Повторный | 33.604 s | 33 593 ms | 1600 | 20480* | 8 / 8 |
| Документ выключен | 2.701 s | 2674 ms | 1401 | 894 | 8 / 7 |
| Документ восстановлен | 1.477 s | 1447 ms | 1600 | 86 | 8 / 8 |

* Значение возвращено Ollama для диагностической модели до добавления ограничения
`num_predict=512`; оно аномально и не считается валидной метрикой Qwen.

### Ресурсы

| Метрика | До | После |
|---|---:|---:|
| GPU VRAM used | 5675 MiB | 6497 MiB |
| System RAM used | 47085 MiB | 48499 MiB |

Снимки системные и включают другие приложения. Peak во время генерации:
**not measured**. Процессный RAM: **not measured**.

### Функциональные результаты

- Оба приказа присутствовали среди найденных источников: passed.
- Деактивированный приказ исключался SQL-фильтром: passed.
- После восстановления приказ снова появлялся: passed.
- Четыре query traces записаны в PostgreSQL: passed.
- Ответ диагностической TinyLlama не содержал корректных `[N]` цитат: **failed**.
- Фактическая правильность ответа Qwen и поддержка утверждений источниками:
  **not measured**.

## Оставшийся блокер

1. Обеспечить исходящий HTTPS к `registry.ollama.ai:443`.
2. Выполнить `ollama pull qwen3.5:9b`.
3. Перезапустить backend с исходным `.env` и убедиться, что `/readyz` возвращает 200.
4. На свежих проектных ресурсах повторить smoke.
5. Вручную проверить, что ответ на вопрос о двух приказах называет ставки 0,10% и
   0,05%, не выдумывает приоритет и связывает каждое утверждение с реальным источником.
6. Заменить диагностические числа в этом файле на Qwen baseline; неизмеренное оставить
   `not measured`.
