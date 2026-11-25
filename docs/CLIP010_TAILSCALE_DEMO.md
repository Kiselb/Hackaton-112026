# CLIP-010 — публичный стенд через Tailscale Funnel

## Граница решения

Штатный внешний транспорт публичного demo — **Tailscale Funnel**. Это публичный
доступ из интернета, в отличие от Tailscale Serve, доступного только участникам
tailnet. Funnel публикует только loopback Caddy:

```text
Internet --HTTPS--> Tailscale Funnel --HTTP/loopback--> Caddy:8080
         --Basic Auth--> React/FastAPI:8000 --> PostgreSQL/Qdrant/OpenSearch/Ollama
```

FastAPI, Ollama и базы напрямую не публикуются. Caddy сохраняет site-wide Basic
Auth, удаляет `Authorization` перед upstream, ограничивает размер upload и слушает
только loopback. Используются существующие `.env`, рабочие данные и индексы.

Tailscale — внешний транспортный провайдер и часть границы доверия. На стенде
допустимы только документы, для которых владелец разрешил такую передачу. Общая
Basic Auth учётная запись `jury` не является production ACL: у неё нет персональной
идентификации, MFA, ролей или отзыва отдельной сессии.

## Однократная настройка владельцем

Установить Tailscale для Windows и авторизовать устройство через приложение либо
из административной Windows PowerShell:

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' login
& 'C:\Program Files\Tailscale\tailscale.exe' status --json
```

Не помещать auth key, cookies или конфигурацию аккаунта в `.env` и Git. Команды
Serve/Funnel на Windows выполнять из административной консоли. При первом Funnel
CLI может вывести URL владельца для включения HTTPS/Funnel в tailnet; пройти этот
шаг в браузере под учётной записью владельца и не ослаблять access policy из
скрипта проекта.

Официальный синтаксис и ограничения:

- [Tailscale Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel);
- [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve);
- [Windows unattended mode](https://tailscale.com/docs/how-to/run-unattended).

В существующем `.env` должны быть параметры CLIP-008 и, при необходимости,
ожидаемый HTTPS origin:

```dotenv
PUBLIC_DEMO_ENABLED=true
DEMO_TUNNEL_PROVIDER=tailscale
TAILSCALE_FUNNEL_URL=https://expected-node.example.ts.net/
```

`TAILSCALE_FUNNEL_URL` можно оставить пустым: фактический URL читается из
`tailscale funnel status --json`, а не угадывается по имени ПК. Если URL задан,
скрипты требуют точного совпадения. Пароль остаётся только bcrypt-хешем в
`DEMO_AUTH_PASSWORD_HASH`.

## Стандартные команды

Из корня репозитория в административной Windows PowerShell:

```powershell
# Локальный защищённый стенд без внешнего доступа
.\scripts\demo\Start-PublicDemo.ps1

# Единый запуск с Funnel
.\scripts\demo\Start-PublicDemo.ps1 -StartFunnel

# При необходимости пересобрать production frontend
.\scripts\demo\Start-PublicDemo.ps1 -RebuildFrontend -StartFunnel

# Если tailscale.exe отсутствует в PATH и стандартном каталоге
.\scripts\demo\Start-PublicDemo.ps1 -StartFunnel `
  -TailscalePath 'C:\Program Files\Tailscale\tailscale.exe'
```

`-StartTunnel` — устаревший интерфейс ngrok CLIP-008; он завершается с понятной
ошибкой и никогда молча не включает Funnel.

Launcher проверяет Python 3.12, Caddy, health/readiness, занятые порты, состояние
авторизации Tailscale и точную Funnel mapping. Ожидаемые уже работающие процессы
можно безопасно переиспользовать, но они не становятся project-managed. Чужой
процесс, Serve mapping, иной URL/target или несколько web mappings приводят к
fail-closed ошибке.

Состояние ownership новых компонентов хранится в игнорируемом Git файле
`storage/demo-logs/public-demo-state.json`. Для процессов записываются PID, полный
путь executable, UTC-время старта, роль и сигнатура команды. Существующий ручной
Funnel автоматически не принимается под управление.

На Windows проектная Python 3.12 `.venv` может сообщать в `Win32_Process` как
собственный `sys.executable`, так и связанный `sys._base_executable`. Оба пути
получаются запуском Python именно из `.venv`; произвольный `python.exe` из PATH не
принимается. Для нового FastAPI ownership записывается из фактического snapshot
владельца loopback-порта после проверки PID, executable и command line, а не из
предполагаемого пути launcher-а.

## Статус и публичная проверка

```powershell
.\scripts\demo\Get-PublicDemoStatus.ps1
.\scripts\demo\Get-PublicDemoStatus.ps1 -Json

$credential = Get-Credential -UserName jury
.\scripts\demo\Test-PublicDemo.ps1 `
  -BaseUri https://actual-node.example.ts.net `
  -Credential $credential
```

Status-скрипт read-only. Успех требует FastAPI `/healthz=200`, `/readyz=200`,
Caddy `401` без credentials, Tailscale `Running/online`, ровно одну публичную
Funnel mapping на `http://127.0.0.1:<DEMO_PROXY_PORT>` и публичный `401`. Он не
показывает пароль, токены или DSN. Readiness приложения проверяет предусмотренную
приложением доступность PostgreSQL, Qdrant, OpenSearch и Ollama.

## Безопасная остановка

```powershell
.\scripts\demo\Stop-PublicDemo.ps1
```

Stop отключает только Funnel, созданный launcher-ом и подтверждённый state-файлом,
точной активной mapping и executable. Используется адресная команда вида:

```powershell
tailscale funnel --https=443 http://127.0.0.1:8080 off
```

`tailscale down`, `funnel reset` и остановка Tailscale service не выполняются.
FastAPI/Caddy завершаются только после проверки PID, executable, времени старта,
роли, сигнатуры команды и listener port. Docker, Ollama и рабочие данные не
затрагиваются. Если Funnel unmanaged либо ownership неоднозначен, скрипт ничего
не останавливает и выдаёт диагностику. При отсутствии state-файла повторный Stop
является безопасным no-op.

## После перезагрузки Windows

Funnel, запущенный с `--bg`, сохраняет конфигурацию и может возобновиться после
перезапуска Tailscale. Локальные FastAPI/Caddy при этом сами не запускаются.

1. Включить ПК и дождаться Docker Desktop, PostgreSQL, Qdrant и OpenSearch.
2. Убедиться, что Ollama и `qwen3.5:9b` доступны в Windows.
3. Войти в Windows; для работы до интерактивного входа заранее включить Tailscale
   unattended mode по инструкции владельца.
4. Выполнить `Get-PublicDemoStatus.ps1`; кратковременный публичный `502` до запуска
   Caddy ожидаем.
5. Выполнить `Start-PublicDemo.ps1 -StartFunnel`, затем снова status и
   `Test-PublicDemo.ps1`.

Если сохранились state и принадлежащая проекту Funnel mapping, launcher её
проверит и переиспользует. Если state утерян, активный Funnel считается unmanaged
и не получает права на остановку.

## Диагностика

| Симптом | Безопасная проверка | Действие |
|---|---|---|
| `tailscale.exe` не найден | `Get-Command tailscale.exe`; проверить стандартный путь | Передать `-TailscalePath`; не менять глобальный PATH из скрипта |
| `NeedsLogin`, offline или access denied | `tailscale status --json` в Administrator PowerShell | Авторизовать Tailscale/запустить консоль владельца; не сохранять auth key в проекте |
| Funnel отсутствует | `tailscale funnel status --json` | Проверить HTTPS/Funnel consent и затем повторить `-StartFunnel` |
| Обнаружен Serve | status JSON показывает mapping без `AllowFunnel=true` | Не перезаписывать; владелец должен отдельно решить конфликт |
| Неверный URL/target или несколько mappings | Сравнить status JSON с `127.0.0.1:8080` и `.env` | Не применять `reset`; согласовать точечное изменение |
| DNS/TLS ещё не готовы | Проверить точный URL из status, системное время и DNS | Подождать выдачу сертификата; не обходить HTTPS |
| Локальный `401`, публичный `0/502` | Проверить online/Funnel и доступность Caddy | Не ослаблять Basic Auth; проверить, что ПК не спит |
| Public `200` без credentials | Немедленно считать стенд небезопасным | Не публиковать данные; остановить только подтверждённый owned Funnel или обратиться к владельцу |
| Stop отказался | Прочитать ownership diagnostic и state | Не завершать PID вручную без повторной идентификации |

Логи Caddy/FastAPI находятся в `storage/demo-logs` и не коммитятся. Перед передачей
диагностики удалить из неё приватные документы, credentials, DSN и сырые запросы.

## Безопасный переход с текущего ручного Funnel

Пока действует ручной Funnel `https://kronwerk.tail9a1e57.ts.net/` →
`http://127.0.0.1:8080`, launcher/status показывают его как `managed=false`.
`Stop-PublicDemo.ps1` не имеет права его отключать.

Фактический переход выполнять только в согласованное окно:

1. Зафиксировать `Get-PublicDemoStatus.ps1 -Json` и успешный внешний test.
2. Получить отдельное разрешение владельца на кратковременное отключение.
3. Точечно отключить ручную mapping той же командой и flags, с которыми она была
   создана; не использовать `reset`.
4. Не перезапуская здоровые FastAPI/Caddy, выполнить
   `Start-PublicDemo.ps1 -StartFunnel`. Новый Funnel станет owned, локальные
   процессы останутся unmanaged.
5. Проверить status, публичные `401/200` и полный `Test-PublicDemo.ps1`.
6. После следующего reboot launcher сможет запустить отсутствующие FastAPI/Caddy
   и записать их ownership.

Если новый launcher неисправен, не удалять state и не применять reset. Проверить
локальный Caddy, затем владелец может восстановить прежнюю схему:

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --bg 8080
& 'C:\Program Files\Tailscale\tailscale.exe' funnel status
```

После восстановления снова проверить публичный `401` и authenticated test.
Ручное прекращение PID допускается только после отдельной проверки пути,
command line, времени запуска и listener port.
