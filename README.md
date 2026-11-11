# Syllabus AI — бэкенд

Бэкенд для прототипа `syllabus-ai-prototype` (Narxoz University). Он проверяет силлабусы по рубрике качества (9 принципов, 49 критериев) с помощью локальной LLM и ведёт академическое согласование: Draft → AI Checked → Head of Program Review → KOK Review → Approved. Кроме того, он поддерживает массовые сессии проверки, аналитику, приватную предпроверку и мастер создания силлабуса.

**Стек:** FastAPI · PostgreSQL (SQLAlchemy 2 + Alembic) · MongoDB (Beanie) · Celery + Redis · vLLM (OpenAI-совместимый API).

## Быстрый старт

```bash
cp .env.example .env              # LLM_PROVIDER=fake работает без GPU
docker compose up -d --build      # postgres, mongo, redis, migrate, api, worker, beat
docker compose exec api python -m app.cli seed   # данные прототипа + первые проверки
open http://localhost:8080/docs   # Swagger
uv run python scripts/demo_flow.py                # e2e: сессия → правки → v4 → Approved
```

Пока сервис аутентификации не подключён, работает заглушка: токеном служит `stub:<email>`, например `Authorization: Bearer stub:provost@demo.narxoz.kz`. При `ENV=prod` приложение с заглушкой не запустится.

Демо-пользователи:

| Пользователь | Роль |
|---|---|
| `superadmin@…` | Администратор системы (после `bootstrap` / `add-test-users`) |
| `provost@…` | Офис провоста |
| `kok@…` | КОК |
| `admin.digital@…` | Администратор School of Digital Technologies |
| `head.cs@…` | Руководитель программы Computer Science |
| `shalkar@…` | Ведущий курса AI |
| `lee@…` | Ведущий курса ML, преподаватель AI |
| `karimova@…` | Преподаватель |
| `sultanov@…` | Преподаватель |
| `abayeva@…` | Преподаватель |
| `sadykov@…` | Преподаватель |

У всех адресов домен `@demo.narxoz.kz`.

**Школы и пользователи в новой базе** (не миграции, команды можно запускать повторно):

```bash
docker compose exec api python -m app.cli setup-schools    # 4 школы (по программе на школу) и рубрика
docker compose exec api python -m app.cli add-test-users   # пользователи всех ролей, не в prod
```

Школы: Школа экономики и менеджмента, Гуманитарная школа, Школа права и государственного управления, Школа цифровых технологий. Пользователи: `provost@`, `kok@`, `admin.<школа>@` и `head.<школа>@` для `economics`, `humanities`, `law`, `digital`, а также `karimova@` (преподаватель). Адреса совпадают с переключателем пользователей на фронтенде.

**Права:** офис проректора и КОК видят все школы и все силлабусы, запускают проверки и сессии везде, видят всю аналитику. Администратор школы делает то же самое в рамках своей школы.

## Вход через Google

С `AUTH_PROVIDER=google` пользователи входят корпоративным Google-аккаунтом `@narxoz.kz` (`app/auth/google.py`). Google подтверждает только личность; что человек может делать, решают роли в нашей базе. Без записи в базе вход не пройдёт («нет доступа»).

1. В Google Cloud → APIs & Services → Credentials создайте OAuth client ID типа **Web application** и добавьте Authorized redirect URIs: `https://syllabus-agent.narxoz.kz/api/v1/auth/google/callback` (и для разработки `http://localhost:8080/api/v1/auth/google/callback`).
2. В `.env`: `AUTH_PROVIDER=google`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JWT_SECRET` (`openssl rand -hex 32`), для localhost ещё `COOKIE_SECURE=false`.
3. Заведите людей с ролями:

```bash
python -m app.cli add-user daniar.aitanov@narxoz.kz --role provost
python -m app.cli add-user kok.secretary@narxoz.kz --role kok
python -m app.cli add-user admin@narxoz.kz --role school_admin --school digital      # economics, humanities, law, digital
python -m app.cli add-user head@narxoz.kz --role program_head --school law
python -m app.cli add-user teacher@narxoz.kz --role professor
python -m app.cli list-users
python -m app.cli disable-user leaver@narxoz.kz      # вход и запросы отклоняются, история остаётся
```

**Сразу много людей на сервере** — `scripts/import_users.sh`: список прямо в файле (`email,role,school,program,name,title`, по строке на роль), правите и запускаете:

```bash
./scripts/import_users.sh              # вход через Google
./scripts/import_users.sh --password   # плюс общий пароль для всех (спросит)
# то же из файла: python -m app.cli import-users users.csv
```

Всё или ничего: если хоть одна строка с ошибкой, никто не добавится, а скрипт покажет номера строк. Повторный запуск добавляет только недостающие роли.

Повторный `add-user` добавляет ещё одну роль. Имя подтягивается из Google при первом входе. Сессия — cookie `HttpOnly` на `SESSION_TTL_HOURS` часов.

## Вход по email и паролю

- `AUTH_PROVIDER=password`: только email + пароль (Google не нужен).
- `AUTH_PROVIDER=google` + `PASSWORD_LOGIN=true`: на странице входа кнопка Google и форма с паролем.

Обоим нужен `JWT_SECRET` (`openssl rand -hex 32`). Пароли хешируются scrypt. После 10 неудачных попыток email блокируется на 15 минут. Смена пароля завершает остальные сессии пользователя.

```bash
python -m app.cli add-user ivanov@narxoz.kz --role professor --password   # спросит пароль
python -m app.cli set-password ivanov@narxoz.kz                           # сменить (спросит)
python -m app.cli set-password ivanov@narxoz.kz --remove                  # только Google
```

### Тестовые пользователи всех ролей на сервере

**Автоматически при запуске.** В `.env` на сервере:

```bash
SEED_TEST_USERS=true
TEST_USERS_PASSWORD=<общий пароль, от 8 символов>   # в ENV=prod обязателен
```

После этого каждый `docker compose up -d` (сервис `migrate` после миграций выполняет `python -m app.cli bootstrap --if-enabled`) создаёт 4 школы, рубрику и тестовых пользователей. Повторный запуск ничего не дублирует, только обновляет пароль.

**Вручную, одной командой:**

```bash
./scripts/bootstrap_server.sh                 # пароль из TEST_USERS_PASSWORD, иначе спросит
./scripts/bootstrap_server.sh --allow-prod    # при ENV=prod
# то же без скрипта: docker compose exec api python -m app.cli bootstrap --password
```

| Email (`@demo.narxoz.kz`) | Роль |
|---|---|
| `superadmin@` | Администратор системы: всё + экран «Admin» |
| `provost@` | Офис провоста |
| `kok@` | КОК |
| `admin.{economics,humanities,law,digital}@` | Администратор школы |
| `head.{economics,humanities,law,digital}@` | Руководитель программы школы |
| `karimova@` | Преподаватель |

У всех один пароль. Команда идемпотентна: повтор только обновит пароль. Убрать тестовых после демо: `disable-user <email>` или `set-password <email> --remove`.

## Админка (роль `admin`)

Роль `admin` видит и делает всё, что офис провоста (все школы, силлабусы, аналитика, Google Drive), и вдобавок управляет пользователями и справочником на экране **Admin** (API `/api/v1/admin/*`, `app/api/admin.py`):

- пользователи: поиск и фильтры, создание, имя и должность, включение и отключение, задать или убрать пароль;
- роли: добавить или снять роль с привязкой к школе (`school_admin`) или программе (`program_head`);
- школы, программы, курсы: правка и удаление (если на них ничего не ссылается, иначе 409), смена ведущего и преподавателей курса;
- сводка: пользователи по ролям, силлабусы по стадиям и статусу ИИ-проверки, прогоны, сессии, последние упавшие проверки.

Пользователи не удаляются, только отключаются: на них ссылается история. Отключить себя или снять с себя `admin` нельзя, поэтому хотя бы один активный админ всегда остаётся. В согласовании силлабусов админ не участвует.

```bash
python -m app.cli add-user it.admin@narxoz.kz --role admin --password   # настоящий админ
```

## Разработка без Docker для приложения

```bash
uv sync
docker compose up -d postgres mongo redis
uv run alembic upgrade head
uv run python -m app.cli seed
uv run uvicorn app.main:app --reload --port 8080
uv run celery -A app.workers.celery_app worker -Q ai,default -c 2   # или TASKS_EAGER=true в .env
uv run celery -A app.workers.celery_app beat
```

Проверки (тестам нужен запущенный Docker, testcontainers поднимает свои контейнеры):

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy app seed
```

## Как устроено

```
app/
  api/        роутеры (/api/v1), по одному на экран прототипа
  services/   бизнес-логика: checks, approval, proposals, precheck, sessions, analytics, wizard, notifications
  ai/         конвейер: extraction → technical → criteria (LLM) → evidence; proposals, apply, render_docx, fake
  models/     PostgreSQL     documents/  MongoDB     schemas/  Pydantic DTO
  auth/       клиент сервиса аутентификации, RBAC     workers/  Celery + dispatch     storage/  файлы, Drive, Redis
rubric/       рубрика в YAML (версионируется)     seed/  демо-данные     migrations/  Alembic
```

**Где хранятся данные:**
- **PostgreSQL** — справочники, пользователи и роли, силлабусы, версии, запуски проверок, статусы критериев (`criterion_outcomes`, для аналитики), события согласования, сессии, уведомления.
- **MongoDB** — извлечённая структура документа, полные результаты ИИ (цитата, объяснение, рекомендация), предложенные правки, черновики мастера, история ассистента, трейсы LLM.
- **Файлы** — на локальном диске: `FILES_ROOT/{syllabus_id}/v{n}/{файл}`. Версии неизменяемы. Копия каждой версии уходит в Google Drive, а потерянный локальный файл скачивается оттуда.

**Проверка силлабуса** выполняется в задаче Celery `check_run` в очереди `ai`:
1. Документ (.docx или .pdf) разбирается на разделы, LO, недели и задания с весами. Распознаются заголовки на EN, RU и KZ.
2. Детерминированные правила проверяют 3.2, 6.8 и 8.2 и составляют технический отчёт.
3. LLM вызывается один раз на принцип, ответ строго по JSON-схеме.
4. Критерии с внешним контекстом получают `Cannot verify`, если контекста нет. Это 3.4 (паспорт программы, поле `programs.passport_text`) и 7.2 (библиотечный каталог).
5. Цитату (`evidence`) сверяем с текстом документа. Если её там нет, статус понижается до `Cannot verify`: так отсекаются выдуманные цитаты.

**Права:**
- `provost` и `kok` видят весь университет, `school_admin` — свою школу, `program_head` — свою программу.
- Преподаватель видит курсы, которые ведёт или на которых преподаёт.
- Загрузку, улучшение и предпроверку может выполнять только ведущий курса.

**Согласование** описано в `app/services/approval.py`, таблица `TRANSITIONS`. При возврате на доработку комментарий обязателен. Новая версия возвращает силлабус в Draft, после проверки он переходит в AI Checked.

**Уведомления** создаются в приложении. Письма отправляет задача beat `send_pending_emails` по принципу outbox. При `EMAIL_BACKEND=console` письма пишутся в лог.

**Google Drive.** Загрузка для пользователя обычная: файл сначала сохраняется на диск, проверка работает с локальной копией, а задача beat `sync_drive_files` раз в минуту копирует новые версии в Drive. Поэтому сбой Drive не мешает загрузке, а неудачная отправка повторяется. Папка выбирается так: папка версии, затем папка школы на этот семестр, затем `DRIVE_DEFAULT_FOLDER_ID`. ID папок хранятся в `upload_folders.drive_folder_id` и задаются на экране «Schools & Drive». Предпроверка в Drive не попадает.

## Что подключить дальше

Обе интеграции сейчас работают на заглушках, и при `ENV=prod` приложение с ними не запустится. Остальной код менять не нужно: зависимости спрятаны за протоколами.

**Сервис аутентификации** ([app/auth/client.py](app/auth/client.py)):
1. Сверьте контракт с описанием в `HttpAuthClient` и при необходимости поправьте `verify_token`. Сейчас ожидается `GET {AUTH_SERVICE_URL}{AUTH_SERVICE_VERIFY_PATH}` с токеном пользователя и ответ `{"id", "email", "name"}`. Если сервис выдаёт JWT без такого эндпоинта, проверяйте подпись прямо в `verify_token`.
2. Реализуйте `HttpAuthClient.verify_account`: это проверка аккаунта перед сохранением каждой новой версии. Сейчас метод намеренно падает, чтобы непроверенный аккаунт не прошёл молча.
3. Задайте `AUTH_PROVIDER=http` и `AUTH_SERVICE_URL`.
4. На фронтенде замените `getToken()` в `lib/auth.ts`, добавьте переход на страницу входа и очистите `DEMO_USERS`.

Сервис отвечает только за личность. Роли остаются в нашей БД, поэтому пользователь должен быть заведён здесь заранее. Первый вход привязывает `users.external_id` по email, если привязки ещё нет. Без записи в БД пользователь получит 403.

**Google Drive** ([app/storage/drive.py](app/storage/drive.py)): пошаговая инструкция для сервисного аккаунта — [GOOGLE_DRIVE_SETUP.md](GOOGLE_DRIVE_SETUP.md). Проще всего подключать через интерфейс: **Schools & Drive → Подключение Google Drive** (офис провоста). Там загружается ключ, задаётся общая папка, проверяется подключение и связываются папки школ.
1. Положите JSON-ключ в `./secrets` и задайте `DRIVE_PROVIDER=google`, `GOOGLE_CREDENTIALS_FILE=/secrets/<файл>.json`. Подходит и OAuth-файл authorized user с refresh token.
2. Сервисный аккаунт загружает только в Shared Drive либо от имени почты школ (`GOOGLE_IMPERSONATE_USER`, domain-wide delegation).
3. Проверьте: `python -m app.cli drive-check --folder <id>` (с `--write` — ещё и запись).
4. Подключите общую папку (по подпапке на школу): `python -m app.cli drive-setup <id общей папки> --semester "Fall 2026"`. Команда найдёт или создаст папки школ и пропишет их ID на экране «Schools & Drive».

Версии, загруженные до подключения или при `DRIVE_READ_ONLY=true`, останутся ждать отправки и уйдут в Drive автоматически.

**Силлабусы из Drive** читаются на месте, без импорта: каталог папок школ (`app/services/drive_catalog.py`), подробно — в [GOOGLE_DRIVE_SETUP.md](GOOGLE_DRIVE_SETUP.md), шаг 8.

## Подключение vLLM

```bash
vllm serve Qwen/Qwen2.5-32B-Instruct --port 8000 --max-model-len 32768
```

В `.env` задайте:
```
LLM_PROVIDER=vllm
LLM_BASE_URL=http://<gpu-host>:8000/v1
LLM_MODEL=Qwen/Qwen2.5-32B-Instruct
LLM_MAX_CONCURRENCY=4
```

Нужна многоязычная модель, потому что силлабусы бывают на EN, RU и KZ. Параметр `-c` воркера (сейчас 2) и `LLM_MAX_CONCURRENCY` подберите под пропускную способность сервера. `LLM_MAX_CONCURRENCY` ограничивает запросы внутри одного процесса воркера, поэтому в сервер одновременно уходит до `-c × LLM_MAX_CONCURRENCY` запросов.

### llama.cpp и Ollama

Бэкенд работает с любым сервером с OpenAI-совместимым API. От значения `LLM_PROVIDER` зависит только то, как выключаются рассуждения модели (`LLM_THINKING=false`, по умолчанию):

| `LLM_PROVIDER` | Сервер | Что отправляется при `LLM_THINKING=false` |
|---|---|---|
| `llamacpp` | `llama-server` из llama.cpp | `chat_template_kwargs: {"enable_thinking": false}` |
| `ollama` | Ollama | `reasoning_effort: "none"` (`think` и `chat_template_kwargs` Ollama на `/v1` игнорирует) |
| `vllm` | vLLM | `chat_template_kwargs: {"enable_thinking": false}` |

У Qwen3 с включёнными рассуждениями каждый запрос проверки занимал больше 180 с, поэтому их лучше не включать.

```bash
llama-server -m qwen3-8b.gguf --jinja --host 0.0.0.0 --port 8080 -c 32768 --parallel 4
```
```
LLM_PROVIDER=llamacpp
LLM_BASE_URL=http://<gpu-host>:8080/v1
LLM_MODEL=qwen3-8b          # llama-server с одной моделью имя не проверяет, оно попадает в трейсы
LLM_MAX_CONCURRENCY=2       # 2 процесса воркера × 2 = 4 = --parallel
```

`--jinja` обязателен: без него llama-server не применяет шаблон чата модели и не видит `enable_thinking`. Контекст `-c` делится между слотами `--parallel`, так что на каждый запрос здесь приходится 8192 токена. Запрос проверки занимает около 3 тыс. токенов вместе с ответом.

### Скорость проверки с Ollama

С `LLM_PROVIDER=ollama` бэкенд ходит в нативный API Ollama (`/api/chat`, адрес берётся из `LLM_BASE_URL` без `/v1`) и сам задаёт контекст модели: `LLM_NUM_CTX=16384` по умолчанию. Без этого Ollama загружала `qwen3-8` с контекстом 262k: 33 ГБ, часть модели на CPU, ~15 ток/с и ~50 с на запрос. С 16k модель целиком на GPU: ~54 ток/с, 10–18 с на запрос.

| Что | Как настроить |
|---|---|
| Контекст модели | `LLM_NUM_CTX` (по умолчанию 16384). Силлабус длиннее `(LLM_NUM_CTX − 4000) × 2.5` символов обрезается, в логе будет предупреждение |
| Модель не выгружается между проверками | `LLM_KEEP_ALIVE=30m` |
| Параллельные запросы | На сервере Ollama `OLLAMA_NUM_PARALLEL=3` (переменная окружения сервера, не бэкенда), в `.env` — `LLM_MAX_CONCURRENCY=3`. Каждому слоту нужен свой `LLM_NUM_CTX` в памяти GPU |
| Одна проверка — весь сервер | Если на сервере один слот, запускайте воркер с `-c 1`: при `-c 2` две проверки делят слот и обе идут вдвое дольше |

Если сервер общий и другие клиенты используют ту же модель с другим контекстом, Ollama будет перезагружать её при каждой смене. Тогда лучше завести на сервере отдельную модель с нужным контекстом (Modelfile, шаг 1a в [TESTING_AI_CHECK.md](TESTING_AI_CHECK.md)) и указать её в `LLM_MODEL`.

Другие ускорения в коде: текст силлабуса идёт в начале промпта, поэтому сервер переиспользует его KV-кэш между девятью запросами. Модель пишет короткие объяснения и не пишет рекомендацию для критериев со статусом Compliant (текст «No change suggested» подставляет бэкенд).

**Прогресс и оставшееся время.** `GET /runs/{id}`, элементы сессии и `GET /precheck/{id}` возвращают поле `progress`: шаг, процент, принципы в работе, прошедшее время, `eta_s` и `eta_at`, а для проверки в очереди — `queue_position`. Оценка строится по среднему темпу последних проверок (Redis-ключ `eta:pace_s`, до первой проверки — `ETA_INITIAL_CALL_S`) и уточняется по темпу текущей проверки ([app/services/run_progress.py](app/services/run_progress.py)).

Как проверить ИИ-проверку с локальной моделью Ollama и посчитать токены: [TESTING_AI_CHECK.md](TESTING_AI_CHECK.md).

## Рубрика

Рубрика лежит в `rubric/2026.1.yaml`. Чтобы изменить её, создайте новый файл с новым `code` и выполните `python -m app.cli import-rubric rubric/<code>.yaml`. Новая версия станет активной. Каждый запуск проверки хранит ссылку на свою версию рубрики, поэтому старые результаты не меняются.

## Отличия от прототипа

- Синхронизация с Google Drive односторонняя: файлы загружаются через API и копируются в Drive, а файлы, появившиеся в Drive напрямую, приложение не видит. Экран «Schools & Drive» опирается на папки школ (`/schools/{id}/folders`), сессия может проверять одну папку.
- Детерминированными сделаны 3.2, 6.8 и 8.2. В прототипе это были 8.2 и 9.2, но проверка 9.2 требует понимания смысла текста.
- `LLM_PROVIDER=fake` — эвристический оценщик. Он воспроизводит пример проверки из прототипа (у AI v3 проблемы 1.2, 3.5, 4.3, 6.1, 6.4, 9.2) и нужен для тестов и разработки без GPU.

## Известные ограничения

- `POST /runs/{id}/proposals` и ассистент мастера синхронно ждут ответа LLM. С большой моделью запрос может занимать десятки секунд.
- `apply` для PDF не может править исходник, поэтому собирает новый .docx из извлечённого текста.
- Сравнение версий выдаёт построчный diff абзацев, без форматирования.
- Смена ID папки Drive действует только на новые файлы: уже скопированные версии остаются в старой папке.
- Локальные файлы после копирования в Drive не удаляются, диск растёт вместе с числом версий.


## TODO:
- [x] Убрать аутентификацию, вместо него добавить клиент для этого (есть микросервис, сделай так чтобы можно было подключить тот микросервис)
- [x] Сделать конфиг для гугл диск папок, для загрузки файлов сделать обычную загрузку файлов, а под капотом загружать через апи в гугл диск (сделать заглушку чтобы клиент аутентификатора проверял личность аккаунта)
- [x] подключить бэк к фронту
- [ ] Подключить настоящий сервис аутентификации (см. «Что подключить дальше»)
- [ ] Подключить Google Drive через сервисный аккаунт (см. [GOOGLE_DRIVE_SETUP.md](GOOGLE_DRIVE_SETUP.md))
