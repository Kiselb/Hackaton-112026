# CLIP-010 — отчёт реализации Tailscale public demo lifecycle

Дата реализации: 2026-11-25. Исходный commit:
`10e817c94f02acf329a968373d49048eeadbffe9`.

## Исходное состояние

- ветка `main`, рабочее дерево перед CLIP-010 чистое;
- Tailscale for Windows `1.104.7` найден по стандартному пути;
- действующий ручной Funnel:
  `https://kronwerk.tail9a1e57.ts.net/` → `http://127.0.0.1:8080`;
- FastAPI, Caddy и Funnel во время разработки не перезапускались и не
  отключались;
- Docker, Ollama, базы, индексы, документы, backup/restore и настоящий `.env`
  не изменялись.

Read-only проверка реального стенда показала:

- Tailscale `BackendState=Running`, node online;
- ровно одну публичную HTTPS mapping `/` на Caddy;
- FastAPI PID `92960`, `/healthz=200`, `/readyz=200`;
- Caddy PID `116508`, target `http://127.0.0.1:8080`;
- Caddy и публичный URL возвращают `401` без credentials;
- FastAPI/Caddy/Funnel корректно классифицированы как `managed=false`.

## Реализовано

- `Start-PublicDemo.ps1` поддерживает локальный запуск и явный `-StartFunnel`,
  безопасно переиспользует ожидаемые listener-процессы и не принимает текущий
  ручной Funnel под управление;
- добавлен общий PowerShell 5.1 модуль для executable discovery, JSON/status,
  exact mapping, HTTP checks и process ownership;
- добавлен read-only `Get-PublicDemoStatus.ps1`;
- добавлен fail-closed `Stop-PublicDemo.ps1`, который не использует `down` или
  `reset` и не завершает неподтверждённые процессы;
- ngrok удалён из рекомендуемого сценария; `-StartTunnel` выдаёт явную ошибку;
- `.env.example`, README, demo/architecture docs и исторический CLIP-008 обновлены;
- добавлены Windows PowerShell 5.1 mock/regression tests без реальных операций
  Tailscale/Docker и без записи в рабочий `STORAGE_ROOT`.

Изменённые файлы:

- `.env.example`;
- `README.md`, `DEMO.md`, `ARCHITECTURE.md`;
- `docs/CLIP008_PUBLIC_DEMO.md`, `docs/CLIP010_TAILSCALE_DEMO.md`;
- `docs/reports/CLIP010_TAILSCALE_DEMO.md`;
- `scripts/demo/PublicDemo.Common.ps1`;
- `scripts/demo/Start-PublicDemo.ps1`;
- `scripts/demo/Get-PublicDemoStatus.ps1`;
- `scripts/demo/Stop-PublicDemo.ps1`;
- `tests/test_demo_configuration.py`;
- `tests/test_tailscale_demo_configuration.py`.

`scripts/demo/Caddyfile`, настоящий `.env` и frontend не изменялись.

## Что проверено реально и что смоделировано

Реально, только read-only: версия/CLI help, Tailscale runtime status, Funnel JSON,
listener PID/paths/command lines, локальные health/readiness/401 и публичный 401.

Mock-тестами: отсутствие авторизации и Funnel, exact URL/target, Serve и
конфликтующие mappings, повторное использование компонентов, чужой process/state,
PowerShell 5.1 parsing и отсутствие запрещённых команд. Реальный
startup/stop lifecycle не выполнялся, поэтому эксплуатационная приёмка ещё не
завершена.

## Проверки

Итоговые команды и результаты:

- `pytest -q tests/test_tailscale_demo_configuration.py tests/test_demo_configuration.py`:
  `6 passed`;
- `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider ...`:
  `60 passed`, два существующих FastAPI `on_event` deprecation warnings;
- `.venv\Scripts\ruff.exe check backend tests scripts`: `All checks passed`;
- `.venv\Scripts\python.exe -m pip check`: `No broken requirements found`;
- parser всех пяти demo-скриптов в Windows PowerShell 5.1: `OK`;
- frontend production build: не требуется, frontend не изменялся;
- `git diff --check`: успешно, только предупреждения Git о будущей нормализации
  LF→CRLF;
- read-only `Get-PublicDemoStatus.ps1 -Json`: exit `0`, `healthy=true`;
- после тестов в рабочем `STORAGE_ROOT` не найдено новых `pytest`/CLIP-010
  временных артефактов.

В ограниченном контексте launcher `.venv` не смог создать базовый Python process,
поэтому промежуточный полный прогон был выполнен bundled Python 3.12 с
`site-packages` проекта (`60 passed`). Финальные pytest и pip check затем повторены
точным `.venv\Scripts\python.exe` в read-only расширенном контексте с теми же
результатами.

## Ограничения и следующий этап

- текущий ручной Funnel остаётся unmanaged и не может быть остановлен новым Stop;
- Tailscale CLI на текущей Windows требует административной PowerShell;
- `--bg` сохраняет Funnel после reboot, но FastAPI/Caddy и зависимости должны быть
  доступны отдельно; Tailscale до Windows login требует unattended mode;
- общая Basic Auth не заменяет production authentication/authorization;
- полный RAG test с credentials ранее подтверждён владельцем, но в CLIP-010 не
  повторялся, чтобы не затрагивать работающий стенд;
- для реального переключения и проверки startup/stop требуется отдельное
  разрешение владельца и допустимо краткое окно недоступности.

Процедура перехода и rollback: [CLIP010_TAILSCALE_DEMO.md](../CLIP010_TAILSCALE_DEMO.md).

## CLIP-010-FIX — Windows venv process identity

Windows `Win32_Process.ExecutablePath` для Python 3.12 может сообщать базовый
`Python312\python.exe`, хотя launcher использует `.venv\Scripts\python.exe`.
Допустимые пути теперь берутся только из `sys.executable` и
`sys._base_executable`, полученных запуском Python проектной `.venv`; дополнительно
проверяются Python 3.12, `sys.prefix`, `sys.base_prefix` и существование файлов.

Существующий FastAPI принимается только при совпадении одного из этих путей,
loopback listener, ожидаемых command tokens и health/readiness, но остаётся
unmanaged. Для вновь запущенного FastAPI ownership создаётся из фактического
`Win32_Process` snapshot владельца порта и сразу повторно проверяется по PID,
executable, времени старта, command line и listener PID. Stop использует тот же
allow-list и не может принять произвольный Python-процесс или подменённый state.

Проверки исправления: профильные тесты `7 passed`, полный pytest `61 passed`,
Ruff и Windows PowerShell 5.1 parser успешно, `pip check` без нарушений. Реальный
Start/Stop lifecycle не запускался. Read-only проверка PID `92960` дала
`IdentityValid=True`, `ReuseUnmanaged`, health/readiness `200/200`.

## CLIP-010-HOTFIX — Windows PowerShell 5.1 Python probe quoting

Windows PowerShell 5.1 удалял внутренние двойные кавычки из многострочного
аргумента `python -c`; Python получал повреждённый словарь и завершался с
`SyntaxError`. Python-литералы probe заменены на одинарные. Его вызов выделен в
отдельную функцию, которая локально собирает native stderr и exit code, не меняя
общий runner и поведение команд Tailscale. При ненулевом exit code диагностика
содержит и код, и фактический stderr.

Настоящий integration-тест запускает функцию через Windows PowerShell 5.1 и
проектный `.venv\Scripts\python.exe`, подтверждает Python 3.12, отличающиеся
`sys.prefix`/`sys.base_prefix` и оба разрешённых executable. Негативный probe с
exit code `7` подтверждает сохранение stderr. Результаты: профиль `8 passed`,
полный pytest `62 passed`, Ruff/parser/git diff check успешно, `pip check` без
нарушений. Выполнялась только read-only Python identity проверка; Start/Stop и
операции Tailscale не запускались.
