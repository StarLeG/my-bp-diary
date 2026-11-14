# SPEC.md — Telegram-бот «Дневник гипертоника»

**Статус:** черновик на утверждение · **Обновлено:** 2026-11-14
**Репозиторий:** [StarLeG/my-bp-diary](https://github.com/StarLeG/my-bp-diary.git)
**Исходное ТЗ:** `doc/TZ.md`. Настоящий документ — расширенная спецификация: схема БД, сценарии, список переменных окружения, структура файлов. Расхождения с ТЗ помечены ⚠ и собраны в разделе 17.

## 1. Назначение и принципы

Личный Telegram-бот для ведения дневника измерений артериального давления: быстрая запись показаний тонометра, просмотр за сегодня, текстовая статистика за неделю/месяц, экспорт CSV для врача, интерактивные напоминания.

**Принцип №1 — минимум трения.** Запись показания — за 2 нажатия. Кнопки вместо свободного ввода везде, где возможно. Редактирование текущего сообщения (`edit_message_text` / `edit_message_reply_markup`) вместо отправки новых.

**Принцип №2 — многопользовательность.** Бот обслуживает любое число пользователей одновременно. Данные строго изолированы по `telegram_id`: пользователь никогда не видит и не может изменить чужие записи.

**Принцип №3 — безопасность конкурентной работы.** Обработка апдейтов разных пользователей идёт параллельно (asyncio, при необходимости — thread pool). Код обязан быть защищён от гонок, двойных нажатий и повторных доставок от Telegram: см. §7.

### Не входит в объём (non-goals)

- Веб-страница поддержки с реквизитами (отдельный проект позже; в боте — только ссылка `SUPPORT_URL`). Лендинг и личный кабинет (§16) — в объёме проекта, но создаются после бота.
- Графики и диаграммы в боте — только текстовая сводка (в личном кабинете график есть, §16.2).
- Мультиязычность — только русский.
- Медицинские рекомендации и диагностика.
- Облачные аккаунты и синхронизация как таковые: данные привязаны к `telegram_id`; личный кабинет — другой взгляд на те же данные пользователя.

## 2. Стек

| Компонент | Выбор |
|---|---|
| Python | 3.11+ |
| Бот | aiogram 3.x (asyncio) |
| БД | SQLite (старт) через SQLAlchemy 2.0 async (aiosqlite) |
| Валидация | Pydantic |
| Конфиг | python-dotenv |
| Часовые пояса | `zoneinfo` (stdlib) |
| Планировщик | APScheduler 3.x (AsyncIOScheduler + SQLAlchemyJobStore в отдельном файле SQLite рядом с БД, §6.7) |
| Веб-части (§16) | лендинг — статика (HTML/CSS/JS); кабинет — FastAPI + Jinja2 + uvicorn, пароли — Argon2id (`argon2-cffi`), сессии — `itsdangerous` |
| PDF-отчёт (§10.1) | fpdf2; кириллица — встроенные шрифты DejaVuSans (обычный и жирный, `bot/services/fonts/`) |
| Менеджер пакетов | uv; зависимости и версия — в `pyproject.toml` |
| Тесты | pytest, pytest-asyncio, pytest-cov; моки aiogram — unittest.mock / pytest-mock |

Типизация везде (type hints), docstrings у всех функций, классов и модулей — на русском. Версия проекта — SemVer, в `pyproject.toml` (стартовая `0.1.0`).

## 3. Переменные окружения

Читаются из `.env` через python-dotenv, валидируются Pydantic-моделью `Settings` (`bot/config.py`). `.env.example` — в репозитории, `.env` — в `.gitignore`.

| Переменная | Обязательна | По умолчанию | Назначение |
|---|---|---|---|
| `BOT_TOKEN` | да | — | токен от @BotFather (см. «Безопасность токена») |
| `BOT_PROXY` | нет | пусто | Прокси для Telegram API (HTTP(S) или SOCKS: `http://user:pass@host:port`, `socks5://host:port`). Пусто = без прокси. Может содержать учётные данные — не логируется. |
| `SUPPORT_URL` | нет | пусто | ссылка на страницу поддержки. Пусто или плейсхолдер (`CHANGE_ME`, `https://example.com` и т.п.) → кнопка «Поддержать проект» скрывается |
| `AUTHOR_CONTACT` | нет | пусто | контакт автора (например `@username` или e-mail). Пусто → кнопка «Написать автору» скрывается |
| `GROUP_URL` | нет | пусто | ссылка на сообщество бота в Telegram (группа с темами). Пусто или плейсхолдер (как у `SUPPORT_URL`) → кнопка «Наше сообщество» на экране «О боте» скрывается |
| `DB_PATH` | нет | `data/bot.sqlite3` | путь к файлу SQLite |
| `DEFAULT_TIMEZONE` | нет | `Europe/Minsk` | часовой пояс (IANA), если пользователь пропустил выбор |
| `LOG_LEVEL` | нет | `INFO` | уровень логирования |
| `WEB_SECRET_KEY` | для кабинета | — | секрет для подписи сессий и ссылок входа личного кабинета (`itsdangerous`); задаётся при развёртывании кабинета |
| `CABINET_URL` | для кабинета | пусто | base URL личного кабинета (например `https://cabinet.example.org`); пусто = кабинет не развёрнут, `/cabinet` отвечает «Личный кабинет пока не развёрнут» |
| `SMTP_HOST` | нет | пусто | SMTP-сервер для писем врачу (§21). Пусто = письма не отправляются, бот работает как без почты (ссылка — пациенту для пересылки) |
| `SMTP_PORT` | нет | `465` | порт SMTP: `465` (SSL) или `587` (STARTTLS) |
| `SMTP_SECURITY` | нет | `ssl` | `ssl` или `starttls` |
| `SMTP_USER` | при `SMTP_HOST` | пусто | логин SMTP |
| `SMTP_PASSWORD` | при `SMTP_HOST` | пусто | пароль SMTP; не логируется |
| `SMTP_FROM` | при `SMTP_HOST` | пусто | отправитель, например `Дневник гипертоника <noreply@electroman.by>` |
| `PRIVACY_URL` | да (с умолчанием) | `https://bp-diary.electroman.by/privacy.html` | страница «Персональные данные» (§16.1); ссылки «Полный текст» в экранах согласия бота и кабинета (§6.11) ведут на её якоря `#consent`, `#policy`, `#rights`. Пусто или плейсхолдер (как у `SUPPORT_URL`) — ошибка конфигурации при старте бота и кабинета: ссылку нельзя скрыть, её требует ст. 5 п. 5 |
| `BACKUP_ENCRYPTION_KEY` | для бэкапов | пусто | ключ AES-GCM резервных копий (§20); пусто — бэкапы выключены; не логируется |
| `BACKUP_DIR` | нет | `data/backups` | каталог локальных зашифрованных копий (§20) |
| `DELETION_LOG_KEY` | при `BACKUP_ENCRYPTION_KEY` | пусто | ключ HMAC журнала удалений (§19, §20); не логируется. Пусто при заданном `BACKUP_ENCRYPTION_KEY` — ошибка конфигурации при старте |
| `DELETION_LOG_PATH` | нет | `data/deletions.log` | файл журнала удалений — **вне** файла БД и вне `BACKUP_DIR`: восстановление копии не должно его перезаписать |
| `ADMIN_TELEGRAM_IDS` | нет | пусто | ID администраторов через запятую (например `123456789,987654321`). Пусто = админ-панель отключена. |
| `TELEGRAM_API_ID` | для QA | — | API ID для Telethon (получить на my.telegram.org) |
| `TELEGRAM_API_HASH` | для QA | — | API Hash для Telethon |
| `TELEGRAM_TEST_PHONE` | для QA | — | Номер телефона тестового аккаунта для E2E |
| `LOAD_TEST` | для нагрузки | пусто | `1` — полный профиль нагрузочных тестов `qa/` (§22); пусто — дымовой |
| `LOAD_PASSWORD` | для нагрузки | — | пароль нагрузочных пациентов и врачей тестового стенда (сиды, Locust); в коде и логах не бывает |
| `LOAD_DB` | для нагрузки | `data/test-stand.sqlite3` | БД нагрузочного стенда; имя файла обязано содержать `test-stand` (§22) |

⚠ В ТЗ названы только `SUPPORT_URL` и `AUTHOR_CONTACT`; остальное — необходимая инфраструктура.

Реквизиты (карты, IBAN, кошельки, ФИО и пр.) не хранятся в коде и конфиге ни при каких условиях — только на внешней странице.

### Безопасность токена

Токен даёт полный доступ к боту; утечка = компрометация. Правила обязательны:

- **Токен хранится только в переменных окружения / секрет-менеджере.** В коде, конфигах и логах токена быть не должно. В репозитории — лишь `.env.example` без значений; `.env` — в `.gitignore`.
- Для продакшена — секрет-менеджер (Docker secrets, HashiCorp Vault, AWS Secrets Manager) или переменные окружения контейнера, а не файл на диске.
- Токен никогда не логируется — ни в обычных, ни в отладочных сообщениях.
- Ротация: при подозрении на утечку — немедленно отозвать токен через @BotFather (`/revoke`) и выпустить новый.
- Webhook (если позже будет использоваться вместо long polling): обязательно валидировать заголовок `X-Telegram-Bot-Api-Secret-Token`. Для long polling не требуется.
- Pre-commit hooks: `detect-private-key`, `gitleaks` — чтобы случайно не закоммитить `.env` или ключи.

## 4. Схема БД

SQLite, файл по `DB_PATH`. Все моменты времени — **UTC** (ISO-8601), отображение — в часовом поясе пользователя. Связи — `ON DELETE CASCADE` от `users`. При работе: `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `busy_timeout=5000` (см. §7).

### 4.1 users

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | внутренний ключ |
| telegram_id | BIGINT | NOT NULL, **UNIQUE** | id пользователя Telegram |
| timezone | TEXT | NOT NULL, `Europe/Minsk` | IANA-имя (`zoneinfo`) |
| target_systolic | INTEGER | NOT NULL, `130` | целевое систолическое |
| target_diastolic | INTEGER | NOT NULL, `80` | целевое диастолическое |
| med_remeasure_delay | INTEGER | NULL, по умолчанию `60` | через сколько минут напомнить повторить измерение после приёма лекарства (30/60/120); NULL = выключено пользователем |
| onboarded | BOOLEAN | NOT NULL, `0` | онбординг пройден |
| last_support_shown_at | DATETIME | NULL | UTC, когда последний раз показывали футер «Поддержать» (не чаще 1 раза в сутки) |
| deletion_requested_at | DATETIME | NULL | UTC: согласие отозвано в личном кабинете (§6.11, К5) — бот удаляет аккаунт при ближайшей ежечасной проверке (§19); с 0.5.0 |
| last_name | TEXT | NULL, 1–50 | фамилия (§6.12, с 0.5.1) |
| first_name | TEXT | NULL, 1–50 | имя |
| middle_name | TEXT | NULL, 1–50 | отчество — необязательно |
| birth_date | DATE | NULL | дата рождения; возраст на сегодня ≥ 16 лет (ст. 5 п. 9 Закона № 99-З) |
| sex | TEXT | NULL, CHECK IN (`m`, `f`) | пол |
| telegram_name | TEXT | NULL, ≤ 64 | «имя фамилия» из профиля Telegram — врач видит его, пока данные не заполнены (§6.12); обновляется при обращении к боту (чтение, запись — только если сменилось) |
| profile_prompted_at | DATETIME | NULL | UTC: предложение заполнить данные (§6.12, П7) уже показано — больше не показывается |
| meds_ask_shown_at | DATETIME | NULL | UTC: подсказка «Напоминать, какие лекарства принимать?» (§6.3, K2) уже показана — при повторных «💊 Лекарство» без лекарств показывается «Сначала добавьте лекарства в настройках»; 0.6.0 (M1) |
| followup_enabled | BOOLEAN | NOT NULL, `1` | «Повторять, если не ответил» (§6.7, 0.5.6) — общий для всех слотов |
| reminders_paused_until | DATE | NULL | пауза напоминаний: последний день паузы включительно, локальная дата по `timezone` (§6.7, 0.5.6); NULL — паузы нет |
| ui_large | BOOLEAN | NOT NULL, `0` | «Крупный режим» бота (§6.6, §11): кнопки по одной в строке, калькулятор 2 колонки, списки блоками с пагинацией по 5, значения жирным, гистограмма крупнее; `0` — обычный режим (0.5.10) |
| created_at | DATETIME | NOT NULL | UTC |

Данные пациента (`last_name`, `first_name`, `birth_date`, `sex`) заполнены все или ни одного; `middle_name` — по желанию (проверка — в приложении, `bot/schemas.py`). Схема БД — поколение 5 (0.5.1): колонки добавляются `ALTER TABLE users ADD COLUMN`. Поколение 7 (0.5.6): `followup_enabled`, `reminders_paused_until` — так же. Поколение 9 (0.5.10): `ui_large` — так же. Поколение 11 (0.6.0): `meds_ask_shown_at` — так же (вместе с `diary_events`, §4.18).

⚠ ТЗ указывало `users.reminder_time` — поле удалено: расписание живёт только в `reminders` (§4.3). Три слота (утро/обед/вечер) + одноразовый повтор замера после приёма лекарства — требование заказчика (2026-11-04). `onboarded` — для §6.1, `last_support_shown_at` — для §6.5.

### 4.2 measurements

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id | владелец записи |
| timestamp | DATETIME(timezone=True) | NOT NULL | UTC, момент измерения; границы «сегодня»/«недели» считаются в `users.timezone` |
| systolic | INTEGER | NOT NULL, CHECK 70…260 | систолическое |
| diastolic | INTEGER | NOT NULL, CHECK 40…160 | диастолическое |
| pulse | INTEGER | NULL, CHECK 30…220 | пульс; NULL = пропущено |
| medication_taken | BOOLEAN | NULL | TRUE = да, FALSE = нет, NULL = не отмечал |
| feeling | TEXT | NULL | `normal` / `headache` / `weakness` / `other` |
| note | TEXT | NULL, ≤ 500 | заметка; при «Другое» (самочувствие) — свободный текст сюда же |

Индексы: `(user_id, timestamp)`. Ограничения CHECK продублированы на уровне БД (защита от ошибок слоя выше).

⚠ В ТЗ схема без CHECK — добавлены как защита данных при конкурентном доступе.

### 4.3 reminders

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id | |
| type | TEXT | NOT NULL, CHECK IN (`morning`,`noon`,`evening`,`med_remeasure`) | тип слота; типа `snooze` НЕТ (см. §6.7) |
| local_time | TEXT | NULL | время слота `HH:MM` в таймзоне пользователя; NULL для `med_remeasure` |
| next_trigger_time | DATETIME | NOT NULL (UTC) | когда отправить |
| enabled | BOOLEAN | NOT NULL, `1` | слот включён; у `med_remeasure` — `0` после доставки (строка хранится, пока по ней работают кнопки сообщения, §6.7) |
| last_sent_date | DATE | NULL | дата последней отправки (правило «не чаще 1 раза в день на тип») |
| measurement_id | INTEGER | NULL, FK measurements.id | для `med_remeasure`: запись, к которой относится повтор |
| last_message_id | INTEGER | NULL | `message_id` последнего отправленного напоминания слота — у него снимаются кнопки при следующем (0.5.4, §6.7; схема — поколение 6); после повторного напоминания — его `message_id` |
| mode | TEXT | NOT NULL, `both`, CHECK IN (`both`, `meds`, `pressure`) | режим слота (§6.7, 0.5.6): «Давление и лекарства» / «Только лекарства» / «Только давление»; у `med_remeasure` не используется |
| followup_at | DATETIME | NULL | UTC: когда отправить повторное напоминание слота, если пользователь не ответил (§6.7, 0.5.6); NULL — не нужно |

Миграция поколения 6 (0.5.4) — при старте: `ALTER TABLE reminders ADD COLUMN last_message_id INTEGER` (у существующих строк — NULL: кнопки сообщений, отправленных до обновления, не снимаются, они отвечают по правилам §6.7).

Миграция поколения 7 (0.5.6) — при старте, без пересборки таблиц: `ALTER TABLE reminders ADD COLUMN mode VARCHAR(8) NOT NULL DEFAULT 'both' CHECK (mode IN ('both','meds','pressure'))`, `ALTER TABLE reminders ADD COLUMN followup_at DATETIME`, `ALTER TABLE users ADD COLUMN followup_enabled BOOLEAN NOT NULL DEFAULT 1`, `ALTER TABLE users ADD COLUMN reminders_paused_until DATE`. У существующих строк — режим «Давление и лекарства», повтор включён, паузы нет.

**UNIQUE (user_id, type)** для слотов `morning`/`noon`/`evening` — не более одной активной строки на слот. Строк с `type='snooze'` нет: «Через 30 мин» перезаписывает `next_trigger_time` существующей строки слота транзакционно (§6.7). Для `med_remeasure` — не более одного активного на `measurement_id` (идемпотентность при двойном нажатии «Да»).

### 4.4 medications

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id | владелец |
| name | TEXT | NOT NULL, ≤ 100 | название препарата |
| dosage | TEXT | NOT NULL, ≤ 50 | дозировка строкой (например, «10 мг»); пустая строка — «без дозировки» |
| slot | TEXT | NOT NULL, CHECK IN (`morning`,`noon`,`evening`,`crisis`) | слот приёма: утро/обед/вечер (как у измерений) или `crisis` — препарат на случай кризиса |
| created_at | DATETIME | NOT NULL | UTC |

**UNIQUE (user_id, name, slot)** — одно и то же лекарство не дублируется в одном слоте. Лекарство в двух слотах — две строки. Лекарство целиком — группа строк с одним `(user_id, name)`: изменение и удаление действуют по группе (§6.6). Настройка — за 3 шага (§6.6).

Слот `crisis` — препарат SOS (давление подскочило, нужно сбить): напоминаний по нему нет и в приверженность (§6.5, §6.7) он не входит. Схема versioned через `PRAGMA user_version` (`bot/db.py::SCHEMA_VERSION`); старые БД (дозировка обязательна, без `crisis`) пересобираются миграцией при старте.

### 4.5 medication_logs

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id | владелец |
| medication_id | INTEGER | NOT NULL, FK medications.id | препарат |
| timestamp | DATETIME | NOT NULL | UTC, когда отмечен приём |
| status | TEXT | CHECK IN (`taken`,`skipped`,`partial`) | принято / пропущено / частично |
| slot | TEXT | NOT NULL | слот (для агрегации приверженности) |

Отметка действия пользователя («Принял всё ✓» / «Частично…» → «Готово» / «Пропускаю») создаёт записи по каждому лекарству слота одной транзакцией (§6.7). Также записи создаются при выборе лекарств в «Приняли лекарство?» → «Да» при записи давления (§6.3): `status='taken'`, слот — из строки лекарства, идемпотентно за локальные сутки. Так же — при выборе лекарств в форме записи личного кабинета (§16.2), но с `timestamp` = моменту измерения (`measurements.timestamp`), а не моменту отметки. В MVP используются статусы `taken` и `skipped`; значение `partial` допустимо схемой. Приверженность в статистике (§6.5) считается по статусу `taken` относительно запланированных приёмов.

### 4.6 web_users

Учётные записи личного кабинета (§16.2). Отдельная таблица, чтобы не смешивать с Telegram-пользователями.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id, **UNIQUE** | привязка к пользователю бота |
| email | TEXT | NOT NULL, **UNIQUE** | e-mail для входа |
| password_hash | TEXT | NOT NULL | хеш пароля **Argon2id** (соль генерируется автоматически; пароль в открытом виде не хранится и не логируется) |
| created_at | DATETIME | NOT NULL | UTC |

### 4.7 web_sessions

Сессии личного кабинета (§16.2): сессионный токен хранится в SQLite, время жизни — 7 дней.
Просроченные строки удаляются при создании новых сессий.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| token | TEXT | PK | случайный сессионный токен (в HTTP-only cookie) |
| web_user_id | INTEGER | NOT NULL, FK web_users.id, CASCADE | владелец сессии |
| created_at | DATETIME | NOT NULL | UTC, когда создана |
| expires_at | DATETIME | NOT NULL | UTC, момент истечения (сейчас + 7 дней) |

### 4.8 doctors
Врачи, которые могут просматривать данные пациентов.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| email | TEXT | NULL, **UNIQUE** | e-mail врача (приглашение по e-mail); NULL — врач добавлен только через Telegram |
| telegram_id | INTEGER | NULL, **UNIQUE** | Telegram-аккаунт врача (приглашение через Telegram, `/iamdoctor`) |
| telegram_username | TEXT | NULL, ≤ 32 | username врача без «@», в нижнем регистре; обновляется при каждом сообщении врача боту; поиск по нему — только среди врачей (§21) |
| name | TEXT | NOT NULL, ≤ 100 | ФИО врача: из `/iamdoctor`; врач по ссылке-приглашению — имя и фамилия из профиля Telegram; врач по e-mail — часть адреса до «@» |
| clinic | TEXT | NULL, ≤ 100 | Название клиники (опционально) |
| password_hash | TEXT | NOT NULL | хеш пароля (Argon2id) |
| created_at | DATETIME | NOT NULL | UTC |
| consent_version | TEXT | NULL | версия согласия врача на обработку его данных (`DOCTOR_CONSENT_VERSION`, §6.11); NULL — согласия нет (с 0.5.0) |
| consent_at | DATETIME | NULL | UTC, когда врач дал согласие (§6.11) |
| consent_channel | TEXT | NULL, CHECK IN (`bot`, `cabinet`) | где дано согласие врача: Б8 (`bot`) или К2 (`cabinet`) — для доказательства (ст. 5 п. 7) |

Согласие врача хранится в `doctors`, а не в `consents` (§4.15): у врача, приглашённого по e-mail, записи `users` нет. Действующее согласие врача — `consent_at IS NOT NULL AND consent_version = DOCTOR_CONSENT_VERSION`.

**CHECK (email IS NOT NULL OR telegram_id IS NOT NULL)** — врач опознаётся хотя бы одним способом. Записи одного врача, добавленного разными путями (e-mail и Telegram), не объединяются: в кабинете врач видит пациентов той записи, через которую вошёл. Миграция живой БД (NOT NULL → NULL у `email`, новые поля) — пересборкой таблицы при старте, как `_migrate_medications` (`bot/db.py`).

### 4.9 doctor_patients
Связь между врачом и пациентом (право доступа к данным).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| doctor_id | INTEGER | FK doctors.id | врач |
| user_id | INTEGER | FK users.id | пациент |
| status | TEXT | CHECK IN (`pending`, `active`, `revoked`) | статус доступа |
| created_at | DATETIME | NOT NULL | UTC |

**UNIQUE (doctor_id, user_id)** — нельзя дважды связать одного врача с одним пациентом.

### 4.10 doctor_notes
Заметки и рекомендации от врача для конкретного пациента.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| doctor_id | INTEGER | FK doctors.id | врач, оставивший заметку |
| user_id | INTEGER | FK users.id | пациент |
| text | TEXT | NOT NULL, ≤ 1000 | текст рекомендации |
| created_at | DATETIME | NOT NULL | UTC |

### 4.11 measurement_medications

Связь «измерение ↔ отмеченные лекарства»: какие приёмы пользователь отметил в мультивыборе «Какие лекарства приняли?» конкретной записи (§6.3). Перечень показывается в «Истории записей» кабинета (§16.2).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| measurement_id | INTEGER | NOT NULL, FK measurements.id, CASCADE | запись измерения |
| medication_id | INTEGER | NOT NULL, FK medications.id, CASCADE | отмеченный приём |

**UNIQUE (measurement_id, medication_id)** — один приём отмечается у записи один раз (идемпотентность двойного «Готово», §7.2). Связи создаются при «Готово» мультивыбора вместе с записями `medication_logs` (§4.5), но живут независимо: журнал — учёт приёмов за локальные сутки (приверженность, экспорт, §6.5, §10), связи — контекст конкретного замера для истории. Удаление измерения или лекарства удаляет связи каскадом; правка значений измерения (§6.3) связи не трогает.

Кабинет (§16.2) пишет журнал приёмов записи с `timestamp = measurements.timestamp`: журнал записи — строки `medication_logs` с её моментом и id связанных с ней лекарств (пара `medication_id + timestamp`). По этой паре кабинет снимает журнал вместе со связью, переносит его при смене момента записи и удаляет при удалении записи; журналы бота и напоминаний (с моментом отметки) кабинет не трогает.

### 4.12 invite_log

Учёт приглашений врача — для лимитов (§21) и админки. Письма (e-mail) и приглашения по @username (Telegram) — в одной таблице.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id, CASCADE | пациент |
| channel | TEXT | CHECK IN (`email`, `telegram`) | канал |
| target | TEXT | NOT NULL, ≤ 255 | e-mail или username врача (в нижнем регистре) |
| kind | TEXT | CHECK IN (`invite`, `report`) | приглашение или «Отправить отчет» |
| ok | BOOLEAN | NOT NULL | доставлено (для e-mail — SMTP принял письмо) |
| created_at | DATETIME | NOT NULL | UTC |

Индексы: `(user_id, created_at)`, `(target, created_at)`. Удаление аккаунта удаляет строки каскадом (§19).

### 4.13 doctor_invites

Ссылки-приглашения врача в бота (`https://t.me/<бот>?start=doc_<code>`, §21).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| code | TEXT | PK, 22 символа `[A-Za-z0-9_-]` | `secrets.token_urlsafe(16)` |
| user_id | INTEGER | NOT NULL, FK users.id, CASCADE | пациент |
| created_at | DATETIME | NOT NULL | UTC |
| expires_at | DATETIME | NOT NULL | `created_at + 72 ч` |
| patient_name | TEXT | NOT NULL, ≤ 64 | имя пациента из Telegram на момент приглашения — для текста «Пациент {name} открыл вам доступ…» (врач открывает ссылку позже, имени пациента у бота в этот момент нет) |
| used_at | DATETIME | NULL | момент «Принять» / «Отклонить»; не NULL — код списан |

Новое приглашение пациента списывает его прежние неиспользованные коды (действует одно). Повторный заход на экран «Через Telegram» и поиск по @username выдают действующую ссылку, а не новую — уже пересланная врачу ссылка не сгорает. Приглашение самому себе (врач и пациент — один Telegram-аккаунт) недействительно.

### 4.14 used_tokens

Списанные одноразовые ссылки кабинета врача — переживают перезапуск кабинета.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| jti | TEXT | PK | идентификатор ссылки из подписанного токена |
| expires_at | DATETIME | NOT NULL | конец срока ссылки; строки старше чистятся при старте кабинета |

### 4.15 consents

Согласия пациентов на обработку персональных данных (§6.11; Закон РБ № 99-З, ст. 5, 8). Запись — доказательство согласия (ст. 5 п. 7). Схема БД — поколение 4 (`PRAGMA user_version`), миграция при старте: `CREATE TABLE consents`, `ALTER TABLE doctors ADD COLUMN consent_version`, `consent_at`, `consent_channel`, `ALTER TABLE users ADD COLUMN deletion_requested_at`.

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id, CASCADE | пациент |
| kind | TEXT | NOT NULL, CHECK IN (`processing`, `doctor`) | `processing` — основное согласие (ведение дневника); `doctor` — на предоставление данных врачу |
| doctor_id | INTEGER | NULL, FK doctors.id, CASCADE | врач для `kind='doctor'`: по e-mail и @username — сразу, по ссылке-приглашению — при «Принять» (до этого NULL); у `processing` всегда NULL |
| version | TEXT | NOT NULL | `CONSENT_VERSION` на момент согласия (дата редакции `ГГГГ-ММ-ДД`) |
| channel | TEXT | NOT NULL, CHECK IN (`bot`, `cabinet`) | где дано |
| given_at | DATETIME | NOT NULL | UTC |
| withdrawn_at | DATETIME | NULL | UTC: отзыв доступа врачу, отклонение приглашения врачом, «Отменить приглашение», замена записи новой версией |

- **CHECK** (`kind = 'processing'` → `doctor_id IS NULL`).
- Действующая запись — `withdrawn_at IS NULL`. Частичный уникальный индекс `UNIQUE (user_id, kind, COALESCE(doctor_id, 0)) WHERE withdrawn_at IS NULL` — двойное «Согласен» не создаёт вторую запись (§7.2). У приглашения по ссылке (`doctor_id` NULL до «Принять») открытая запись одна: повторное «Согласен, пригласить» (новый код списывает прежний, §4.13) использует её, а не вставляет вторую; связь `doctor_patients` при выдаче ссылки не создаётся (`doctor_id` обязателен, врач ещё неизвестен).
- **Действующее основное согласие** — запись `processing` с `withdrawn_at IS NULL` и `version = CONSENT_VERSION`. Новая версия (`CONSENT_VERSION` меняется только при существенных изменениях: новые цели или перечень данных) — прежняя запись закрывается `withdrawn_at`, новая вставляется той же транзакцией.
- Удаление аккаунта удаляет записи каскадом — осознанно: для удалённых данных доказывать согласие не нужно.
- Константы `CONSENT_VERSION`, `DOCTOR_CONSENT_VERSION` и `POLICY_DATE` — `bot/services/consent.py`. `CONSENT_VERSION` — дата редакции раздела `#consent`, `POLICY_DATE` — разделов `#policy` и `#rights` (тест сверяет); `DOCTOR_CONSENT_VERSION` меняется при изменении текстов Б8 / К2.

### 4.16 appointments

Визиты к врачу, анализы и обследования пациента (§6.13, 0.5.8; ответы — `doc/APPOINTMENTS-QUESTIONS.md`). Схема БД — поколение 8, миграция при старте: `CREATE TABLE appointments` и индексы (существующие таблицы не меняются).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id, CASCADE | пациент |
| kind | TEXT | NOT NULL, CHECK IN (`doctor`, `lab`, `exam`) | «🩺 Визит к врачу» / «🧪 Анализы» / «📋 Обследование» |
| title | TEXT | NOT NULL, 1–60 символов | «Кардиолог», «Общий анализ крови», «ЭКГ» или своё |
| starts_on | DATE | NOT NULL | день события — **локальная** дата по `users.timezone` |
| starts_at | TEXT | NULL | время `HH:MM` — локальное; NULL — «Без времени» |
| note | TEXT | NOT NULL, `''`, ≤ 200 символов | заметка пользователя (адрес, кабинет, «натощак») |
| status | TEXT | NOT NULL, `planned`, CHECK IN (`planned`, `done`, `moved`, `missed`, `cancelled`) | `done` — был(а) / сдал(а); `moved` — перенесено **после** даты (Н1); `missed` — не был(а); `cancelled` — отменено |
| sent_eve_at | DATETIME | NULL | UTC: уведомление «накануне» обработано (отправлено или пропущено, §6.13) |
| sent_soon_at | DATETIME | NULL | то же для «за 2 часа» |
| sent_morning_at | DATETIME | NULL | то же для «утром в день события» (только «Без времени») |
| sent_after_at | DATETIME | NULL | то же для «Как прошёл визит?» |
| last_message_id | INTEGER | NULL | `message_id` последнего уведомления события: у него снимаются кнопки при следующем (как у слотов, §6.7) |
| created_at | DATETIME | NOT NULL | UTC |

- Локальные дата и время (не UTC): моменты уведомлений считаются тиком из `users.timezone` в момент проверки — смена пояса пересчитывает их сама, без миграции строк.
- Индексы: `(user_id, starts_on)` — список и лимит; `(status, starts_on)` — выборка тика (§6.13).
- «Без отметки» — не статус: `planned` больше 3 дней после `starts_on` (`APPOINTMENT_STATUS_TIMEOUT`, Н2) — вычисляется при показе.
- Удаление аккаунта удаляет события каскадом (§19).

### 4.17 appointment_conclusions

Заключения врачей, результаты анализов и протоколы обследований: текст, который пользователь пишет сам (распознавания фото нет), и необязательное фото оригинала (§6.13, 0.5.11; ответы — `doc/APPOINTMENTS-POSTVISIT-QUESTIONS.md`, В27–В36, У1–У3). Схема БД — **поколение 10**, миграция при старте: `CREATE TABLE appointment_conclusions` и индекс (поколение 9 занято `users.ui_large`, 0.5.10; существующие таблицы не меняются).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| appointment_id | INTEGER | NOT NULL, FK appointments.id, CASCADE | событие |
| text | TEXT | NOT NULL, 1–4000 символов, CHECK (length(text) BETWEEN 1 AND 4000) | текст заключения, введённый пользователем; пустой текст (после `trim()`) отклоняют и сервис, и CHECK (канон §4.2/§9) |
| photo_file_id | TEXT | NULL | Telegram `file_id` фото оригинала; файл на сервере **не хранится**, в ЛК и PDF не попадает |
| created_at | DATETIME | NOT NULL | UTC |

- Индекс: `(appointment_id)`.
- Лимит: ≤ 5 записей на событие — общий для бота и ЛК (§6.13, §16.2); условная вставка (§7.2), гонка вкладок.
- Фото — одно на заключение, только `message.photo`; текст — обязателен, фото — нет (В28-Б, В36-А).
- Удаление события удаляет заключения каскадом (§6.13); удаление аккаунта — вместе с пользователем (§19).
- Бот текст не интерпретирует и не комментирует (§1, non-goals).

### 4.18 diary_events

События дневника (0.6.0): питание, стресс/самочувствие, сон, прогулка и самостоятельные
заметки — контекст вокруг измерений. Схема БД — **поколение 11**, миграция при старте:
`CREATE TABLE diary_events` и индексы (поколение 10 занято `appointment_conclusions`,
0.5.11; существующие таблицы не меняются). Давление — `measurements` (§4.2), приёмы
лекарств — `medication_logs` (§4.5): события их не дублируют и не заменяют (ответы —
`doc/MEASURE-REDESIGN-QUESTIONS.md`, R13 = Б, B4 = Б).

| Поле | Тип | Ограничения | Назначение |
|---|---|---|---|
| id | INTEGER | PK | |
| user_id | INTEGER | NOT NULL, FK users.id | владелец |
| type | TEXT | NOT NULL, CHECK IN (`food`, `stress`, `sleep`, `activity`, `note`) | тип события |
| moment | DATETIME(timezone=True) | NOT NULL | UTC, момент события («сейчас» или уточнённый, последние 24 часа, §9) |
| note | TEXT | NULL, ≤ 500 | заметка события (канон §4.2); у `stress` — свободный текст, у `note` — весь текст |
| payload | TEXT (JSON) | NULL | поля типа, дословно ниже |
| measurement_id | INTEGER | NULL, FK measurements.id, ON DELETE SET NULL | привязка к измерению («➕ Фактор», §6.3); NULL — самостоятельное событие; удаление измерения снимает привязку, событие остаётся |
| created_at | DATETIME | NOT NULL | UTC |

Поля `payload` по типам (значения enum — строки):

- `food` — `marks`: массив 1–3 из `salt` («Солёное») / `coffee` («Кофе») / `alcohol`
  («Алкоголь»); одно событие на тип с набором меток (B6).
- `stress` — `variant`: `stress` / `anxiety` / `headache` / `weakness` / `dizziness` /
  `other`; свободный текст — в `note`.
- `sleep` — `rating`: `little` («Мало спал») / `normal` («Нормально») / `much`
  («Много»); не более одной оценки на локальные сутки: повторный ввод тех же суток
  обновляет существующее событие (условная вставка, §7.2).
- `activity` — `duration_min`: 10 / 20 / 30 / 60 (минут).
- `note` — payload пуст; текст — в `note`.

Индексы: `(user_id, moment)` — лента дня; `(measurement_id)` — факторы измерения.
Удаление аккаунта удаляет события каскадом (§19). Правка и удаление события — по id
с проверкой владельца (§6.4). Момент события — «сейчас» или уточнение в пределах последних 24 часов; архив (задним числом глубже) для событий не предусмотрен (§6.10).

## 5. Структура файлов

```
my-bp-diary/
├── LICENSE                   # текст лицензии (PolyForm-Noncommercial-1.0.0)
├── CLAUDE.md
├── README.md                 # запуск, env, «Поддержать проект», «Лицензия»
├── pyproject.toml            # зависимости + версия проекта (SemVer)
├── .env.example
├── .gitignore                # .env, data/, __pycache__, .venv, .pytest_cache, htmlcov
├── .pre-commit-config.yaml   # detect-private-key, gitleaks
├── Dockerfile                # образ бота и кабинета (один, две команды)
├── .dockerignore
├── Makefile                  # make acceptance — приёмка перед релизом (§22)
├── docker-compose.yml        # сервисы bot, cabinet, caddy (профиль https)
├── deploy.sh                 # скрипт развёртывания (Linux/macOS)
├── deploy.ps1                # скрипт развёртывания (Windows PowerShell)
├── deploy/
│   ├── Caddyfile             # HTTPS (Let's Encrypt) для кабинета и лендинга
│   └── landing-nginx.conf    # nginx лендинга: кеш, gzip, 404 (§16.1, «SEO и скорость»)
├── tools/
│   └── landing/              # сборка статики лендинга (§16.1, «SEO и скорость»)
│       ├── tailwind.config.js    # content: web/landing/*.html, script.js
│       ├── tailwind.input.css    # @tailwind base/components/utilities + свои правила
│       ├── build_css.py          # Tailwind CLI (standalone, версия и SHA-256 закреплены) → css/tailwind.css
│       └── make_icons.py         # favicon.svg → favicon.ico, apple-touch-icon.png
├── doc/
│   ├── TZ.md                 # исходное ТЗ
│   └── SPEC.md               # этот файл
├── bot/
│   ├── __init__.py
│   ├── __main__.py           # точка входа: python -m bot
│   ├── main.py               # Bot, Dispatcher, запуск планировщика
│   ├── config.py             # Settings (Pydantic + dotenv)
│   ├── db.py                 # engine, session factory, PRAGMA, create_all
│   ├── models.py             # User, Measurement, Reminder, Medication, MedicationLog, WebUser, WebSession
│   ├── schemas.py            # Pydantic-схемы ввода (числа, время, заметка, лекарства)
│   ├── middlewares.py        # DbSessionMiddleware, DebounceMiddleware (1.5 с), throttle, ConsentMiddleware (§6.11)
│   ├── texts.py              # все тексты сообщений
│   ├── keyboards.py          # клавиатуры, схемы callback_data
│   ├── services/
│   │   ├── admin.py          # сервис метрик и рассылок
│   │   ├── backup.py         # создание, проверка и шифрование бэкапов
│   │   ├── consent.py        # согласия (§6.11): CONSENT_VERSION, DOCTOR_CONSENT_VERSION, проверки, запись и отзыв
│   │   ├── profile.py        # данные пациента (§6.12): сохранение, удаление, возраст, подписи для врача, имя файла
│   │   ├── deletion_log.py   # журнал удалений (HMAC telegram_id + дата, §19); `python -m bot.services.deletion_log --apply` после восстановления копии
│   │   ├── doctor.py         # логика связей врача и пациента, приглашения (doctor_invites)
│   │   ├── mailer.py         # письма врачу: SMTP (smtplib в to_thread), лимиты и invite_log (§21)
│   │   ├── measurements.py   # сохранение и выборка записей
│   │   ├── medications.py    # лекарства: CRUD, расписание по слотам, лог приёмов, приверженность
│   │   ├── appointments.py   # визиты и анализы (§6.13, 0.5.8): CRUD, статусы, лимиты, моменты уведомлений; заключения (§4.17, 0.5.11)
│   │   ├── stats.py          # сводка, тренд, средние по дням (§6.5)
│   │   ├── stats_chart.py    # гистограмма статистики: PNG (Pillow), САД вверх / ДАД вниз / пульс (§6.5)
│   │   ├── csv_export.py     # CSV для врача (измерения + приёмы лекарств)
│   │   ├── pdf_report.py     # PDF-отчёт: шапка, сводка, таблица «Истории записей» (§10.1)
│   │   ├── fonts/            # DejaVuSans.ttf, DejaVuSans-Bold.ttf — кириллица в PDF
│   │   └── reminders.py      # планировщик (APScheduler 3.x + job store); тик раз в минуту — сверка слотов (0.5.7) и уведомления о визитах (0.5.8)
│   └── handlers/
│       ├── __init__.py       # сборка роутеров
│       ├── admin.py          # команда /admin, защищенный роутер
│       ├── consent.py        # экраны согласия пациента (Б1–Б6), подтверждение доступа врачей (Д4), согласие врача (Б8)
│       ├── profile.py        # «Мои данные» (§6.12): экран, FSM ввода, удаление, предложение заполнить (П7)
│       ├── doctor.py         # команда /doctor, «Мой врач»: приглашение по e-mail и через Telegram, отзыв
│       ├── doctor_menu.py    # сторона врача в боте: /start doc_<code>, /iamdoctor, меню врача (§21)
│       ├── start.py          # /start, онбординг, главное меню
│       ├── measure.py        # запись давления и опциональные шаги
│       ├── diary.py          # дневник за сегодня
│       ├── appointments.py   # «📅 Визиты и анализы», /visits: список, добавление, карточка, кнопки уведомлений, отметка и заключения (§6.13)
│       ├── stats.py          # статистика
│       ├── settings.py       # настройки (в т.ч. «Мои лекарства»)
│       ├── support.py        # /support, /cabinet, «О боте», «Поддержать»
│       └── reminders.py      # кнопки «Записать давление» / «Принял всё ✓» / «Частично…» / «Через 30 мин» / «Пропускаю»
├── web/
│   ├── landing/              # статический лендинг (создаётся после бота, §16.1)
│   │   ├── index.html
│   │   ├── privacy.html      # «Персональные данные»: политика #policy, согласие #consent, права #rights (§16.1)
│   │   ├── style.css
│   │   ├── script.js
│   │   ├── robots.txt        # Allow: /, ссылка на sitemap.xml
│   │   ├── sitemap.xml       # / и /privacy.html
│   │   ├── favicon.svg       # иконка по логотипу (синий квадрат с сердцем)
│   │   ├── favicon.ico       # 32×32, из favicon.svg
│   │   ├── apple-touch-icon.png  # 180×180, из favicon.svg
│   │   ├── css/tailwind.css  # собранный Tailwind (tools/landing/build_css.py), в git
│   │   ├── img/og.png        # превью ссылки 1200×630 (qa/screenshots/make_og.py)
│   │   └── img/screens/      # скриншоты секции «Как это выглядит» (WebP, §16.1)
│   └── cabinet/              # личный кабинет (FastAPI, §16.2)
│       ├── app.py            # FastAPI-приложение (роуты, шаблоны)
│       ├── auth.py           # аутентификация: Argon2id, magic link, сессии, CSRF, rate limiting
│       ├── doctor_auth.py    # аутентификация врача
│       ├── csv_import.py     # парсинг и валидация CSV-импорта архива (§16.2)
│       ├── templates/
│       │   ├── login.html
│       │   ├── register.html
│       │   ├── dashboard.html
│       │   ├── consent.html  # согласие пациента в ЛК (§6.11, К3)
│       │   ├── doctor_consent.html # согласие врача (§6.11, К2)
│       │   ├── doctor_dashboard.html # интерфейс врача
│       │   └── partials/
│       │       ├── daily_averages.html # «Средние по дням» — ЛК и врач (§16.2, 0.5.3)
│       │       ├── event_card.html # карточка события «Визитов и анализов» (§16.2, 0.5.9)
│       │       ├── history_day.html # день «Истории записей» аккордеоном — ЛК и врач (§16.2, 0.5.9)
│       │       └── medication_form.html # форма добавления/правки лекарства (§16.2, 0.5.9)
│       └── static/
│           └── style.css
├── qa/                       # Автотесты (QA Bot)
│   ├── __init__.py
│   ├── README.md             # запуск уровней нагрузки, критерии, отчёты, флешмоб (§22)
│   ├── Dockerfile            # образ QA (uv sync --group qa --group load)
│   ├── docker-compose.qa.yml # QA-стенд: бот + кабинет + контейнер тестов
│   ├── conftest.py           # фикстуры E2E и нагрузки (шаблонная БД, мок-бот, часы)
│   ├── test_telegram_e2e.py  # тесты бота через Telethon / aiogram
│   ├── test_reminders_e2e.py # кнопки напоминаний через Telethon: «Отмена» → «Через 30 мин», два списка, повторная отметка (§6.7, 0.5.4)
│   ├── test_appointments_e2e.py # визиты через Telethon: добавить, уведомление, «Был(а)», «Записать следующий», отметка и заключение (§6.13)
│   ├── test_web_e2e.py       # тесты Web UI через Playwright
│   ├── screenshots/          # съёмка скриншотов лендинга (§16.1): shot_landing.py, демо-данные, make_og.py (превью ссылки), README
│   ├── seed/                 # сид тестового стенда (только БД с «test-stand» в имени)
│   │   ├── __init__.py
│   │   ├── seed_load_users.py        # 20 пациентов + web_users
│   │   ├── seed_5000_measurements.py # 5000 измерений (250 на пациента, 30 дней)
│   │   ├── seed_medications.py       # 100 лекарств, 500 medication_logs
│   │   ├── seed_reminders.py         # 100 активных напоминаний
│   │   ├── seed_doctors.py           # 20 врачей + doctor_patients (active)
│   │   ├── seed_admin.py             # строка users для ADMIN_TELEGRAM_IDS
│   │   └── seed_all.py               # оркестратор, --scale, --clean
│   ├── locustfile.py         # HTTP-нагрузка кабинета пациента (Locust, 20 VU)
│   ├── test_load_burst.py    # burst сервисного слоя
│   ├── test_load_bot.py      # burst бота через Dispatcher
│   ├── test_load_doctor.py   # burst кабинета врача
│   ├── test_load_reminders.py # 100 напоминаний, настоящий планировщик
│   ├── test_load_backup.py   # бэкап под нагрузкой
│   ├── test_load_concurrent.py # гонки §7.2 в масштабе
│   ├── test_load_admin.py    # админ-панель на полном объёме
│   ├── test_load_screenshots.py # 3 скриншота стенда (Playwright)
│   ├── test_load_regression.py # критерии приёмки по артефактам
│   ├── test_acceptance_volume.py # приёмка: полный объём ~50 000 записей (§22, ACCEPTANCE=1)
│   ├── test_acceptance_degradation.py # приёмка: деградация, 1000 операций подряд
│   ├── test_acceptance_soak.py # приёмка: часовая нагрузка, RSS раз в минуту
│   ├── test_acceptance_memory.py # приёмка: возврат памяти, 3000 циклов
│   ├── test_acceptance_growth.py # приёмка: рост БД и файлов за 3 года
│   ├── test_acceptance_shutdown.py # приёмка: штатная остановка под нагрузкой
│   ├── test_acceptance_regression.py # приёмка: сбор артефактов после модулей
│   ├── acceptance_common.py  # общее для приёмки: копия стенда, RSS, JSON-артефакты
│   └── reports/              # скриншоты, HTML-отчёты, load/ (JSON, Locust CSV) — вне git
└── tests/
    ├── conftest.py           # fixtures: in-memory БД, моки aiogram, фабрики данных
    ├── test_config.py
    ├── test_main.py          # main: job store отделён от основной БД и не попадает в бэкап
    ├── test_schemas.py
    ├── test_db.py
    ├── test_models.py
    ├── test_keyboards.py
    ├── test_texts.py
    ├── test_bot_ui.py          # внешний вид бота: шапки, кнопки, оба режима (0.5.10)
    ├── test_services_measurements.py
    ├── test_services_medications.py
    ├── test_services_stats.py
    ├── test_services_csv.py
    ├── test_services_pdf.py  # PDF-отчёт: сводка, подсветка, пустой период, лимит
    ├── test_services_reminders.py
    ├── test_services_appointments.py # визиты: CRUD, статусы, лимиты, моменты уведомлений, тик, catch-up, смена пояса, заключения (§4.17)
    ├── test_services_backup.py
    ├── test_services_consent.py # версии, действующее согласие, отзыв, двойное «Согласен», доступ врача (3 условия)
    ├── test_services_deletion_log.py # журнал удалений: HMAC, чистка > 15 дней, --apply после восстановления
    ├── test_services_profile.py # данные пациента: валидация, возраст, 16+, подписи, транслитерация имени файла
    ├── test_handlers_start.py
    ├── test_handlers_measure.py
    ├── test_handlers_diary.py
    ├── test_handlers_stats.py
    ├── test_services_stats_chart.py # гистограмма: размер, цвета зон, пропуски, пульс, подписи
    ├── test_handlers_settings.py
    ├── test_handlers_support.py
    ├── test_handlers_reminders.py
    ├── test_handlers_appointments.py # «Визиты и анализы»: шаги, FSM, карточка, уведомления, «Как прошёл визит?», отметка, заключения, чужие id
    ├── test_handlers_fallback.py # устаревшие кнопки: общий последний обработчик и места аудита (§8, 0.5.5)
    ├── test_handlers_admin.py
    ├── test_handlers_doctor.py # «Мой врач»: e-mail и Telegram, лимиты, «Отправить отчет»
    ├── test_handlers_doctor_menu.py # сторона врача: приглашение, «Принять»/«Отклонить», /iamdoctor, меню
    ├── test_services_mailer.py # письмо, SMTP-ошибки, лимиты, invite_log
    ├── test_handlers_pdf.py  # «Экспорт PDF» в боте (pdf:7 / pdf:30 / pdf:all)
    ├── test_handlers_consent.py # Б1–Б8, Д4: экран до согласия, «Не согласен», отзыв, врачи до 0.5.0
    ├── test_handlers_profile.py # «Мои данные»: ввод, ошибки, 16+, удаление, перед приглашением врача, П7
    ├── test_web_auth.py      # веб-аутентификация: Argon2id, сессии, magic link, rate limiting
    ├── test_web_measurements.py # записи в ЛК: добавление, правка, удаление, импорт CSV
    ├── test_web_pdf.py       # PDF в ЛК (/export/report.pdf) и у врача (/doctor/patient/{id}/export.pdf)
    ├── test_web_consent.py   # /consent пациента и /doctor/consent: перенаправления, CSRF, скрытие пациентов без согласия
    ├── test_web_profile.py   # данные пациента в ЛК и у врача: форма, список, карточка, PDF, имя CSV
    ├── test_web_daily_averages.py # «Средние по дням» в ЛК и у врача: пустые дни, подсветка, итог, лимит 92 дня (§16.2)
    ├── test_web_appointments.py # «Визиты и анализы» в ЛК: CRUD, статусы, лимиты, изоляция, врач не видит (§16.2)
    ├── test_concurrency.py   # гонки: двойные нажатия, параллельные записи, двойной тик планировщика
    └── test_qa_seed.py       # сиды qa/seed: идемпотентность, --clean, защита БД стенда
```

## 6. Сценарии

### 6.1 /start и онбординг

Пользователь создаётся в `users` при первом /start (с дефолтами, `onboarded=0`). Повторный /start при `onboarded=1` → сразу главное меню, без опроса.

**Шаг 0 — согласие на обработку данных** (§6.11): первый экран для нового пользователя — до приветствия. Без «Согласен» онбординг не начинается; «Не согласен» удаляет только что созданную запись `users`.

Четыре экрана. У шагов 2–4 — кнопка «Пропустить» (принимаются значения по умолчанию); у шага 1 — «Начать»:

1. **Приветствие** (2–3 коротких предложения, без простыней): «Привет! Я помогу вести дневник давления: запись — пара нажатий, статистика — понятная сводка для врача.» Кнопка «Начать».
2. **Как вам удобнее смотреть?** (0.5.10, UI7): выбор режима отображения — «Обычный» (`onb:ui:0`) / «Крупный» (`onb:ui:1`) / «Пропустить» (`onb:ui:skip`, = обычный). «Крупный» перерисовывает этот же экран в крупном режиме (демонстрация), кнопка «Крупный» помечается «✓». Шаги 3–4 отрисовываются в выбранном режиме; переключатель остаётся в «Настройках» (`set:ui:0` / `set:ui:1`, §6.6). Существующим пользователям (`onboarded=1`) шаг не показывается.
3. **Часовой пояс**: кнопки с популярными городами (Москва, Санкт-Петербург, Екатеринбург, Новосибирск, Казань, Минск) + «Другой» → свободный ввод IANA-имени с проверкой через `zoneinfo`. Показывается смещение от UTC.
4. **Цели и напоминания**: целевые значения — «130/80 (по умолчанию)», «120/80», «Другое» (по одному числу); напоминания — мультивыбор слотов «Утро», «Обед», «Вечер» (отмечаются ✓, кнопка «Готово») либо «Без напоминаний», затем время для каждого выбранного слота кнопками (07:00/08:00/09:00, 12:00/13:00/14:00, 20:00/21:00/22:00 + «Другое» HH:MM). Повтор замера после лекарства включён по умолчанию (60 минут, меняется в настройках).

Финал: короткое «Готово» + главное меню, `onboarded=1`. Если пользователь прервал онбординг — при следующем `/start` он начинается заново с шага 1 (зависших FSM-состояний нет); `onboarded` не меняется, пока не нажата финальная кнопка «Готово».

### 6.2 Главное меню

Inline-клавиатура, редактирует текущее сообщение, по одной кнопке в строке (в обоих режимах — обычном и крупном; канон §11). Сообщение меню — без заголовка-«шапки» (канон §11, UI1): текст — только приветственная строка, если есть.
«Записать давление» · «➕ Добавить запись» (0.6.0, §6.14) · «Дневник за сегодня» · «📅 Визиты и анализы» (0.5.8, §6.13) · «Статистика» · «Настройки» · «Личный кабинет» · «Отправить отчет» (§21) · «О боте / Поддержать». У врача с дневником — ещё «🩺 Кабинет врача» внизу (§21). «Записать давление» остаётся прямым входом в калькулятор (R1 = Б): выбор типа записи — только у «➕ Добавить запись».
Пункт «Личный кабинет» проверяет наличие аккаунта в `web_users`. Если аккаунта нет — запускает FSM-диалог создания ЛК (запрос e-mail). Если аккаунт есть — генерирует одноразовую magic link и отправляет её. Кнопка видна, только если развернут веб-кабинет (`CABINET_URL` не пуст).

### 6.3 Запись давления

Точки входа: «Записать давление» (главное меню, пустой «Дневник за сегодня», напоминание
слота `rem:write:{slot}:{д}`, повтор замера `rem:now`) и «Давление и пульс» из «➕ Добавить
запись» (`add:bp`, §6.14) — везде сразу калькулятор.

**Калькулятор (0.6.0).** Один экран с шапкой «🩺 Давление и пульс» (V3) и тремя полями — систолическое, диастолическое, пульс
(пульс необязателен) — и итогом `135/85 · пульс 72` (без пульса — `135/85`). Активное
поле одно (`m_field`), набор — в буфере `m_buf` (не более 3 цифр, ведущие нули
схлопываются); тап по числу в итоге возвращает фокус в поле (`m:field:{sys|dia|pulse}`).
Кнопки: цифры 0–9, «C» и «⌫» действуют на **активное** поле (очистить / стереть
последнюю цифру), «Готово» (`m:key:ok`) — сохранить, «Изменить» (`m:reset`) — сброс всех
трёх полей с фокусом в систолическое, «Отмена» (`m:cancel`) — в главное меню. Кнопки
«Пропустить» нет: пустое поле пульса — `pulse = NULL`; при правке пустой пульс — полная
перезапись (прежнее значение очищается). «Готово» и переключение поля сначала коммитят
буфер в активное поле, затем валидируют (§9): «Готово» с пустыми
систолическим/диастолическим — «Введите число», вне диапазона или с нарушением пары —
ошибка валидации, экран не меняется. Экран редактируется на каждое нажатие; в крупном
режиме (`users.ui_large=1`, §6.6) калькулятор — 2 колонки: пары цифр (1–2 … 9–0),
«C» / «⌫» рядом, «Готово», «Изменить» и «Отмена» — каждая своей строкой; данные и
валидация — без изменений (канон §11).

**Быстрая строка.** Любое сообщение на экране калькулятора — ввод одной строкой:
`120/80 72`, `120/80/72` или `120/80` (позиции 1/2/3 — систолическое, диастолическое,
пульс; сепараторы — `/` и пробел; точка и запятая — ошибка «Например: 120/80 72»).
Строка заполняет все три поля (буфер сбрасывается, фокус — на пульс); сохранение —
только по «Готово». Одиночное число — в активное поле; если активного поля нет —
«Например: 120/80 72». Не распознано — короткая ошибка с примером и повтор ввода.

**Сохранение.** INSERT текущим моментом; дубль за ту же минуту — «Это давление уже
записано» (§7.2). Сводка — одна строка `✅ 135/85, пульс 72` (без пульса — `✅ 135/85`);
при правке — `✅ Обновлено: 135/85, пульс 72`; архивная — `✅ 135/85, пульс 72 · за
12.03.2025, 08:00` (§6.10). Если systolic ≥ 180 или diastolic ≥ 110 (кроме архива) —
второй строкой вежливое: «Эти значения стоит обсудить с врачом на ближайшем приёме.»
Без диагнозов, без критериев скорой, без паники. Иных «медицинских» подсказок бот не
даёт нигде.

Кнопки сводки (по одной в строке в обоих режимах): «✏️ Изменить» · «➕ Фактор» ·
«💊 Лекарство» · «Самочувствие» · «📝 Заметка» · «Готово» (в меню). У архивной сводки —
свои кнопки (`m:arch:*`, §6.10).

Опциональные шаги — только по нажатию, привязаны к id сохранённой записи (`m_id`), а не
к «последней записи пользователя» (§7.2):
- **Заметка** → свободный ввод ≤ 500 символов → «Заметка сохранена».
- **Самочувствие** → «Норма» / «Головная боль» / «Слабость» / «Другое»; «Другое» →
  короткий свободный текст в `note`.
- **💊 Лекарство** (0.6.0) → если к записи уже привязан приём — «💊 Принято: …» (только
  чтение) + «➕ Добавить ещё» (`m:med:more`, открывает мультивыбор); иначе — мультивыбор
  «Какие лекарства приняли?»: по кнопке на каждый приём («✓ Лозартан 50 мг · утро», §4.4),
  выбор открывается пустым; у приёмов, принятых за текущие локальные сутки
  (`medication_logs` со статусом `taken`; «Пропускаю» и «Частично» не считаются), —
  пометка в подписи без галочки: «Лозартан 50 мг · утро · сегодня ✓»; нажатие отмечает
  такой приём для этой записи как обычно. С записью связывается только то, что отмечено
  в этом выборе. Кнопки «Готово» / «Отмена»; «Готово» с пустым выбором — тост «Отметьте, что приняли», экран не меняется. «Готово» создаёт по одной записи
  `medication_logs` (`status='taken'`, слот — из строки лекарства) на каждый отмеченный
  приём и связывает отмеченные приёмы с записью измерения (`measurement_medications`,
  §4.11) — перечень показывается в «Истории записей» кабинета (§16.2); повторная отметка
  того же приёма за те же локальные сутки новые записи не создаёт (§7.2), связи дублей
  не создают. Ответ — «💊 Принято: …» + кнопки сводки; «Отмена» — назад к сводке без
  записей. «Готово» с непустым выбором планирует повторный замер (§6.7): дедуп по `m_id` —
  один повтор на измерение, сколько бы приёмов ни отмечено. Если у пользователя нет ни одного
  лекарства — вместо мультивыбора показывается «Напоминать, какие лекарства принимать?» +
  «Добавить» (`med:add`) / «Не сейчас» (`m:medask:dismiss`; один раз при первой попытке —
  маркер `users.meds_ask_shown_at` (§4.1, M1): при первом показе ставится `now`;
  при повторных — «Сначала добавьте лекарства в настройках» + «Добавить») (K2).
  «Не сейчас» — «Хорошо. Добавить лекарства можно позже в настройках.» и возврат к
  сводке записи; «Добавить» — добавление лекарства (§6.6), после сохранения — возврат
  к сводке записи (M3).
- **➕ Фактор** (0.6.0) → мультивыбор меток «Солёное» / «Кофе» / «Алкоголь» / «Стресс» /
  «Недосып»; «Готово» создаёт события `diary_events` с привязкой `measurement_id` (§4.18):
  соль/кофе/алкоголь — одно событие `food` с набором выбранных меток, «Стресс» — событие
  `stress` (`variant=stress`), «Недосып» — событие `sleep` (`rating=little`); разные
  группы — по событию на тип, всё одной транзакцией. Ответ — «✅ Записано» + кнопки
  сводки; повторное «Готово» дописывает недостающие события (идемпотентно, §7.2).

⚠ Поле `measurements.medication_taken` — быстрый флаг для конкретного замера; детальный
трекинг приёмов — `medication_logs` (§4.5). Записи `medication_logs` при выборе лекарств
попадают в приверженность (§6.5) и экспорт приёмов (§10), связи `measurement_medications`
(§4.11) — в «Историю записей» кабинета (§16.2). «Самочувствие» записи и факторы — разные
сущности: `feeling` остаётся 4-значным полем записи, факторы и «Стресс/самочувствие»
меню — события `diary_events` (R19 = Б).

**Редактирование последней записи.** Кнопка «✏️ Изменить» показывается в двух местах: в
сводке (клавиатура опциональных шагов) и в «Дневнике за сегодня» — «✏️ Изменить
последнюю». «Последняя запись» — самая новая по времени, без ограничений по давности;
определяется на сервере в момент нажатия (по `user_id` + `timestamp`, при равном времени —
по `id`), id в `callback_data` не передаётся. Редактируются три значения — тем же
калькулятором и той же валидацией (§9); пустой пульс — полная перезапись («Пропустить»
нет). Заметка, самочувствие и лекарства правятся своими существующими шагами и не
дублируются. Сохранение правки: UPDATE трёх значений, время записи не меняется,
дубль-проверка за минуту не применяется (правка — UPDATE существующей записи, §7.2);
сводка `✅ Обновлено: …` + те же кнопки опциональных шагов; при новых значениях ≥ 180/110 —
та же вежливая строка о враче. Повторное «✏️ Изменить» правит ту же запись, пока она
остаётся последней. «Отмена» (и «Записать давление» / «Записать сейчас») посреди правки
отменяет её без сохранения. Если записи уже нет — мягко «Такой записи нет».

### 6.4 Дневник за сегодня

Лента событий дня (0.6.0): измерения (`measurements`), отметки приёмов (`medication_logs`) и события (`diary_events`) — за текущие локальные сутки, по времени вперемешку (R23 = А). Строки: измерение — «08:15 — 135/85, пульс 72 · лекарство: да · норма» (как прежде); приём — «08:20 — 💊 Лозартан 50 мг · утро ✓» (слот — подписью, время — момент отметки, I2; статус: `taken` — «✓», `skipped` — «✗», `partial` — «~»); событие — «13:20 — 🥗 Питание: солёное, кофе» / «🚶 Прогулка: 20 минут» / «😐 Стресс» (`stress`), «😐 Стресс · тревога» (`anxiety`), «😐 Стресс · головная боль» (`headache`), «😐 Стресс · слабость» (`weakness`), «😐 Стресс · головокружение» (`dizziness`), «😐 Стресс · другое» (`other`; текст `note` у `stress` в ленте не показывается — в карточке события) / «😴 Сон: мало спал» / «📝 Заметка: …» (первые 60 символов текста, полный — в карточке события, M7). Фильтры-чипы: «Давление» / «Лекарства» / «Факторы» (`food`/`stress`/`sleep`) / «Другое» (`activity`/`note`) (`d:filt:{all|bp|med|fact|other}`); «Все» — отсутствие выделенного чипа (стартовое состояние), тап по активному чипу сбрасывает фильтр; выборки по отдельной метке внутри «Факторов» нет (известное ограничение 0.6.0). В конце списка — «Всего записей: N» (по текущему фильтру, M1); если в выбранном фильтре записей нет — «В этой категории записей нет», кнопки фильтров остаются. Пусто → «Сегодня записей ещё нет» + кнопки «Записать давление» и «➕ Добавить запись». Кнопки: «➕ Добавить запись» (§6.14); при непустом списке — «✏️ Изменить последнюю» (правка последней записи, §6.3); «💊 Отметить приём» (как прежде); «Назад».

**Действия со строками.** Тап по строке события — карточка (`ev:open:{id}`): текст — по типу: «😐 Стресс: {вариант} · {время}», «🥗 Питание: {метки} · {время}», «🚶 Прогулка: {N} минут · {время}», «😴 Сон: {оценка}», «📝 {текст} · {время}»; `note` (если есть) — отдельной строкой; кнопки — «✏️ Изменить» (перезапуск флоу типа с текущими значениями, сохранение по id; шаг времени в правке — «🕓 Оставить {момент}» / «🕓 Другое» (ввод `ЧЧ:ММ`), «Сейчас» не используется (V9); у `sleep` момент не показывается и не меняется) и «🗑 Удалить» (с подтверждением) + «Назад» (C3 = А). Тап по строке приёма — тост «Только просмотр» (M4): `medication_logs` в ленте не правятся и не удаляются — управление приёмами через «Приём лекарства» (§6.7) и напоминания слота. Строки измерений правятся «✏️ Изменить последнюю» / «✏️ Изменить» сводки (§6.3); прочие измерения дня — в личном кабинете (§16.2). Тап по строке измерения ничего не делает (строка не интерактивна, M6). Устаревшие кнопки — «Это сообщение устарело» (§8, U8).

В крупном режиме (`users.ui_large=1`) каждая запись — блок (время отдельной строкой, значения — следующей строкой в `<b>`, статусы — третьей); пагинации нет (дневник обычно короткий); канон §11.

**Лекарства на сегодня (0.5.5, U6; ответы — `doc/REMINDERS-UX-QUESTIONS.md`, X11–X12).**
- **Строка.** Под списком (и под «Сегодня записей ещё нет») — «💊 Лекарства: утро ✓ · обед — частично · вечер — ещё не отмечено». Слоты — `morning` / `noon` / `evening`, у которых есть лекарства (включено ли напоминание слота — не важно), в порядке дня; «Кризис» не показывается; лекарств в этих слотах нет — строки нет. Статус слота — по `medication_logs` за сегодня (локальные сутки, §7.2): записей нет — «ещё не отмечено»; все лекарства слота `taken` — «✓»; все отмечены и ни одного `taken` — «пропущено»; иначе — «частично». Будущие сегодняшние слоты — тоже «ещё не отмечено».
- **Кнопка «💊 Отметить приём».** Есть, если есть неотмеченный слот (не все лекарства `taken` — «частично», «пропущено» и «ещё не отмечено» можно отметить заново). Один такой слот — сразу его список; несколько — экран «Какой приём отметить?» с кнопками слотов («Утро» / «Обед» / «Вечер», только неотмеченные) и «Назад» (к дневнику).
- **Список** — тот же механизм без FSM, что «Отметить принятые» в напоминании (§6.7: отметки — в клавиатуре сообщения, дата — сегодня `ГГГГММДД`), но со своими callback'ами (§8), чтобы вернуться в дневник: «Готово» — экран «Дневник за сегодня», первой строкой итог по правилам «Тексты итога» §6.7; «Отмена» — дневник без изменений. Повтор замера ставится так же, как после «Готово» в напоминании (если что-то принято и включён «Повтор после лекарства», §6.7). Мусор, удалённое лекарство, дата не сегодня — уведомление «Это сообщение устарело».
- **Повторная отметка.** Список открывается со всеми лекарствами слота отмеченными. Лекарство, записанное сегодня как не принятое (`skipped`), при отметке «✓» переводится в `taken` — строка журнала обновляется (момент — сейчас), второй строки нет (дедупликация §7.2); уже `taken` не меняется (снятая галочка его не «отменяет»). В напоминаниях повторная отметка по-прежнему ничего не меняет — «Эти лекарства уже отмечены сегодня ✓».

**Ближайшее событие (0.5.8, §6.13, В15).** Если есть событие со статусом `planned` сегодня или завтра — строка под строкой лекарств (лекарств нет — сразу после списка измерений или «Сегодня записей ещё нет»): «📅 Сегодня: Кардиолог, 10:30» / «📅 Завтра: Общий анализ крови» (без времени — без «, ЧЧ:ММ»). Показывается ближайший день: есть сегодняшние — только они, иначе завтрашние. Несколько в один день — через запятую по времени («📅 Сегодня: Анализы 08:00, Кардиолог 15:00», «Без времени» — первыми, без времени в строке); строка длиннее 80 символов — первое событие и «…». Событие сегодняшнего дня показывается до полуночи, пока не отмечено («Был(а)» убирает его из строки).

### 6.5 Статистика

Период кнопками: «7 дней» / «30 дней» / «📆 Свой период» (с 0.5.3, см. «Свой период» ниже) → гистограмма с текстовой сводкой в подписи (см. «Гистограмма» ниже):

```
📊 За 7 дней (29.10–04.11)
Измерений: 12
Систолическое: в среднем 128 (115…145)
Диастолическое: в среднем 82 (72…92)
Пульс: в среднем 68
Тренд: стабильно
Цель 130/80 — среднее в пределах цели
Лекарства: принято 18 из 21 приёма
```

- **Тренд**: период делится пополам; среднее систолическое второй половины минус первой: > +2 мм рт. ст. → «растёт», < −2 → «падает», иначе «стабильно». Меньше 4 измерений за период → «мало данных для тренда».
- Строка «Цель»: «в пределах цели» / «выше цели» / «ниже цели» по среднему относительно `target_*`.
- **Приверженность** (строка «Лекарства» показывается, только если настроены лекарства): «Лекарства: принято X из Y приёмов за период» — по `medication_logs` со статусом `taken` относительно общего числа запланированных приёмов за период (§4.5). Считаются только расписание слоты (утро/обед/вечер): кризисные препараты (слот `crisis`, §4.4) не планируются и в расчёт не входят.
- Кнопки «📅 По дням» (с 0.5.3, см. «Средние по дням» ниже), «Экспорт CSV» (§10), «Экспорт PDF» (§10.1) и «Назад». Экспорт по-прежнему спрашивает свой период (7 / 30 / всё время) и для своего периода сводки не меняется.

**Гистограмма** (`bot/services/stats_chart.py`, ответы — `doc/STATS-HISTOGRAM-QUESTIONS.md`):
- **Показ.** Сводка за период — одно сообщение-фото: картинка + подпись (текст сводки выше, вместе с футером «Поддержать», ≤ 1024 символов) + кнопки «📅 По дням» / «Экспорт CSV» / «Экспорт PDF» / «Назад». Текстовое сообщение выбора периода (и календаря своего периода) в фото не превращается (ограничение Telegram): оно удаляется и отправляется фото. Кнопки под фото («📅 По дням», «Назад», «Экспорт CSV», «Экспорт PDF») удаляют фото-сообщение и показывают свой экран новым текстовым сообщением. Нет записей за период — прежний текст «нет записей», без картинки.
- **Столбик** — один день периода (по столбику на каждый день: 7, 30 или длина своего периода — от 1 до 31, по локальным суткам пользователя, слева направо по датам). Значения дня — средние за день (округление до целого): САД, ДАД, пульс — те же числа, что в «Средних по дням» (одна функция расчёта `stats.daily_averages`). День без измерений — пустое место (даты на месте).
- **Геометрия.** Горизонтальная средняя линия делит картинку: САД — столбик вверх от линии, ДАД — вниз. Масштаб от базового уровня, а не от 0: САД растёт вверх от 90 мм рт. ст., ДАД — вниз от 50 мм рт. ст.; значение ниже базы — столбик нулевой высоты (на картинке — тонкая черта). Верх шкалы — max(180, наибольшее САД периода), низ — max(110, наибольшее ДАД периода), с округлением вверх до 10.
- **Цвета** — из `web/cabinet/chart_zones.py` (те же пороги, что графики и «История записей» ЛК, §16.2), насыщенные: базовый RGB зоны без прозрачности (бледная заливка, как у строк таблиц, на столбиках не читается); каждая половина столбика целиком — цветом своей зоны. САД: зелёная < 130, жёлтая 130–139, красная ≥ 140; ДАД: зелёная < 85, жёлтая 85–89, красная ≥ 90. Отдельного цвета для ≥ 180/110 нет.
- **Пульс** — число на средней линии под столбиком, серое; дня без пульса — ничего.
- **Подписи:** даты под столбиками (`ДД` и у первого дня `ДД.ММ`; при периоде больше 7 дней — каждая 5-я дата и первая); значения САД / ДАД на концах столбиков — только при периоде не больше 7 дней; шкала слева (мм рт. ст.) с пунктирными линиями порогов 130 и 140 (вверх) и 85 и 90 (вниз); легенда снизу: «< 130 / < 85», «130–139 / 85–89», «≥ 140 / ≥ 90» с цветными квадратами и «72 — пульс». Заголовка и линии цели на картинке нет — они в тексте подписи.
- **Оформление:** PNG 1080 × 720, белый фон, шрифт DejaVuSans (`bot/services/fonts/`). Кегль — под показ в чате: Telegram ужимает фото примерно до 300 px по ширине, поэтому значения 34 px, пульс 30 px (при периоде больше 7 дней — 20 px), даты и шкала 28 px, легенда 26 px; поля с запасом — Telegram Web подрезает края фото. Крупный режим (`users.ui_large=1`, UI11): `stats_chart.render` получает параметр `large` и рисует кегли +30% с округлением вверх до чётного — значения 44 px, пульс 40 px (при периоде больше 7 дней — 26 px), даты и шкала 38 px, легенда 36 px; если подписи не влезают в поля — 42 / 38 / 36 / 34 px (значение зафиксировано в `test_services_stats_chart.py`); геометрия и цвета — без изменений. Рисуется Pillow (уже в образе через fpdf2) в `asyncio.to_thread` (§7.2 п.7); в поток передаются значения по дням, не объекты ORM.
- Футер «Бот бесплатный. Поддержать → /support»: показывается, только если у пользователя ≥ 3 записей и `now - last_support_shown_at > 24h`; при показе `last_support_shown_at` обновляется.

**Свой период** (0.5.3, ответы — `doc/DAILY-AVG-QUESTIONS.md`). Инлайн-календарь, без FSM: всё состояние — в `callback_data` (§8), одно редактируемое сообщение; своя клавиатура `keyboards.stats_calendar_kb`, без новых библиотек.
- **Шаг 1.** «📆 Свой период» → текст «Выберите первый день периода:» и календарь текущего периода (по `users.timezone`). Два вида календаря (UI12, 0.5.10): обычный режим — сетка месяца; крупный режим — список недели. Раскладка сетки: строка «Ноябрь 2026» (название месяца в именительном падеже, кнопка-заглушка `stats:noop`); строка «Пн Вт Ср Чт Пт Сб Вс» (заглушки); недели месяца по 7 кнопок — числа дней, клетки вне месяца — пустые заглушки; строка «◀» / «▶» (соседний месяц; «▶» в текущем месяце нет, назад — без ограничения); строка «Назад» (`stats:periods`). Раскладка списка недели (крупный режим): строка «◀ Неделя ДД.ММ–ДД.ММ ▶» (кнопки-стрелки — `stats:cal:w:{понедельник±7дн}`, «▶» отсутствует, если вперёд — дальше недели с сегодняшним днём; «◀» — без ограничений, как листание месяца назад); дни недели строками «Пн 3.11» — `stats:day:{ГГГГММДД}`, только прошедшие и сегодняшний (будущее недоступно, как в сетке), дни без записей показываются (в них можно ставить границы); строка «Назад» (`stats:periods`). Callback'и сетки: `stats:cal:m:{ГГГГММ}` (листание месяца — `stats:cal:m:{соседний}`); вида календаря (сетка/неделя) переключение не предусмотрено — вид определяется режимом пользователя на момент показа.
- **Шаг 2.** Нажат день → текст «Период с ДД.ММ. Выберите последний день:», календарь того же периода, выбранный день отмечен «[3]» (сетка) / «✓ 3.11» (список недели). Листание и «Назад» — как на шаге 1; первый день сохраняется в `callback_data`: `stats:cal:m:{ГГГГММ}:{с}`, `stats:cal:w:{пн}:{с}` (и `stats:day:{с}:{ГГГГММДД}` — тот же формат, что на шаге 1: день уже выбран → завершить период).
- **Совместимость:** прежние callback'и календаря `stats:cal:{ГГГГММ}`, `stats:cal:{ГГГГММ}:{с}` (до 0.5.10) принимаются как `:m:`-вариант (канон `rem:write` без слота, §8) — кнопки живых сообщений продолжают работать; `stats:day:` без изменений.
- **Проверки** (на сервере, по разобранной `callback_data`, а не по раскладке; во всплывающем уведомлении `callback.answer(text)`, календарь остаётся): день позже сегодняшнего (на любом шаге) — «Период не может заканчиваться в будущем»; длина больше 31 дня — «Период — не больше 31 дня». Последний день раньше первого — даты меняются местами (как в ЛК). Один и тот же день дважды — период в один день. Битая `callback_data` — экран выбора периода.
- **Результат** — сводка за период, как для 7 / 30 дней (фото-гистограмма, подпись, кнопки, футер, тренд и приверженность — за выбранные сутки). Заголовок: «📊 За период 01.11–15.11 (15 дней)» — с числом дней в правильной форме («1 день», «2 дня», «5 дней»); для 7 / 30 дней — прежний «📊 За 7 дней (29.10–04.11)». Нет записей — «За выбранный период записей нет» и экран выбора периода.

**Средние по дням** (0.5.3). Один расчёт `bot/services/stats.daily_averages` для бота, ЛК (§16.2) и врача (§21); гистограмма берёт значения дней из него же.
- **День** — локальные сутки по часовому поясу пациента (у врача — тоже по поясу пациента). Значения дня: САД и ДАД — среднее всех измерений дня, пульс — среднее только измерений с пульсом (нет — «—»), «Измерений» — число записей дня; округление до целого (`round`, как в сводке). Учитываются все записи, в том числе архивные (§6.10) и добавленные или импортированные в ЛК.
- **Итог «Среднее за день»** — среднее из **неокруглённых** средних дней с измерениями (каждый день весит одинаково, дни без измерений не учитываются), округление в конце; пульс — по дням, где он есть. Прежние «в среднем» сводки (по всем измерениям) не меняются — это отдельное число, может отличаться на 1–3 мм.
- **Зона дня** — худшая из зон округлённых средних САД и ДАД дня (пороги `web/cabinet/chart_zones.py`, как «История записей» ЛК; персональная цель не влияет).
- **Бот.** Кнопка «📅 По дням» под фото сводки → фото удаляется, новым текстовым сообщением — список и кнопка «Назад» (к фото сводки этого же периода; текст в фото не превращается — сообщение удаляется и отправляется фото, футер «Поддержать» — по общему правилу). Строки — только дни с измерениями, по возрастанию даты; кружок — зона дня (🟢 / 🟡 / 🔴); без пульса — без «, пульс …». В боте число измерений дня не показывается (в отличие от таблицы ЛК и врача) — только дата, значения и пульс. Свой период ≤ 31 дня — список всегда в одно сообщение. В крупном режиме (`users.ui_large=1`) — пагинация по 5 дней: кнопка «Показать ещё {min(5, N − limit)}» (`stats:days:{7|30}:{limit}` / `stats:days:{с}:{по}:{limit}`, §8; UI13; «7 дней» → «Показать ещё 2» → все). Записей за период уже нет — «За выбранный период записей нет» и экран выбора периода.

```
📅 Средние по дням · 04.11–10.11
🟡 04.11 — 134/86, пульс 71
🟢 05.11 — 128/81
🔴 07.11 — 146/92, пульс 75
Среднее за день: 131/84, пульс 70
```

Пульса нет ни в одном дне — итог «Среднее за день: 131/84».

### 6.6 Настройки

Inline-меню, каждая опция редактирует сообщение:
- **👁 Крупный режим** (0.5.10, UI6–UI8) — первая строка-состояние «👁 Крупный режим: вкл/выкл»; нажатие переключает `users.ui_large` (`set:ui:0` / `set:ui:1`) и перерисовывает экран в новом режиме. Режим включает: кнопки по одной в строке (включая пары «Да»/«Нет»), калькулятор 2 колонки, списки блоками с пагинацией по 5 («Показать ещё …»), значения жирным, гистограмму крупнее (§6.5). Данные, валидация, callback'и — без изменений; канон — §11. Шаг онбординга «Как вам удобнее смотреть?» (§6.1) задаёт режим сразу; существующие пользователи переключают его здесь.
- **Целевые значения** — «120/80», «130/80», «140/90», «Другое».
- **Напоминания** — вкл/выкл слотов «Утро»/«Обед»/«Вечер», время каждого (кнопки + «Другое» HH:MM), «Повтор после лекарства» (выкл / 30 / 60 / 120 минут). С 0.5.6 (§6.7 «Удобство напоминаний, часть 2»): первая строка — пауза, у включённого слота — режим, после «Повтор после лекарства» — «Повторять, если не ответил»:

```
Напоминания
⏸ Пауза: нет                       ← пауза (на паузе: «⏸ На паузе до 13.11»)
✅ Утро
⏰ 08:00
💊🩺 Давление и лекарства           ← режим слота
✅ Вечер
⏰ 21:00
💊 Только лекарства
☐ Обед
Повтор после лекарства: 60 минут
🔁 Повторять, если не ответил: вкл
Назад
```
- **Мои лекарства** — список сохранённых препаратов: строка на каждый слот с дозировкой («• Лозартан 50 мг — Утро», §4.4). Внизу — три действия: «Добавить», «Изменить», «Удалить» и «Назад»; если лекарств нет — только «Добавить» и «Назад». В крупном режиме — пагинация по 5 препаратов, кнопка «Показать ещё {min(5, N − limit)}» (`med:list:{limit}`, §8; UI13). Лекарство — группа строк (user_id, name) во всех слотах (§4.4): «Изменить» и «Удалить» сначала показывают выбор лекарства из списка и действуют по всей группе.
  **Добавление** — FSM-цепочка из трёх коротких шагов:
  1. **Название**: свободный ввод ≤ 100 символов; бот подсказывает: «Например: Лозартан, Амлодипин».
  2. **Дозировка**: кнопки популярных вариантов (5 мг, 10 мг, 20 мг, 50 мг, 100 мг), «Без дозировки» (пустая строка, §4.4) и «Другое» (число + единица: мг/мл/таб). Хранится строкой (§4.4).
  3. **Когда принимать**: мультивыбор слотов «Утро» / «Обед» / «Вечер» / «Кризис» (слоты давления + препарат на случай кризиса, §4.4). Кнопка «Готово».
  После сохранения: «Лекарство добавлено. Оно будет появляться в напоминаниях в выбранные слоты»; если выбран только «Кризис» — «Лекарство добавлено. Напоминаний не будет — выбран только приём «Кризис»». Если добавление начато из контекста «💊 Лекарство» (§6.3, K2/M3), после сохранения — возврат к сводке записи, а не в список лекарств.
  **Изменение** — сначала выбор лекарства из списка («Какое лекарство изменить?»), затем повторный проход тех же трёх шагов; текущие значения показываются в подсказках шагов («Сейчас: …»), на шагах названия и дозировки — кнопка «Оставить как есть». Шаг «Когда принимать» открывается с текущими слотами. «Готово» пересобирает группу одной транзакцией: переживающие слоты обновляются на месте (id строк и история `medication_logs` сохраняются, §4.5), снятые слоты удаляются (их логи каскадом), новые — добавляются. Название, занятое другим лекарством пользователя, не принимается: «Лекарство с таким названием уже есть — введите другое.» После сохранения: «Лекарство обновлено. Оно будет появляться в напоминаниях в выбранные слоты»; если выбран только «Кризис» — «Лекарство обновлено. Напоминаний не будет — выбран только приём «Кризис»».
  **Удаление** — сначала выбор лекарства из списка («Какое лекарство удалить?»), затем удаление сразу, без подтверждения: снимаются все слоты лекарства, логи приёмов удаляются каскадом. Ответ — тост «Лекарство удалено» и обновлённый список.
- **Часовой пояс** — как в онбординге.
- **Архивные записи** — перенос прошлых измерений (например, из блокнота): экран с двумя действиями — «Добавить запись» (по одной, кнопками) и «Быстрый ввод» (одной строкой), §6.10.
- **Экспорт CSV** — период (7 дней / 30 дней / всё время) → файл.
- **Экспорт PDF** — период (7 дней / 30 дней / всё время) → PDF-отчёт (§10.1).
- **👤 Мои данные** — ФИО, дата рождения и пол для врача (§6.12).
- **Согласие на обработку данных** — дата согласия, ссылка на полный текст и «Отозвать согласие» (= удаление аккаунта, §6.11, §19).
- **Назад** в меню.

### 6.7 Напоминания

Планировщик — **APScheduler 3.x** (`AsyncIOScheduler` + `SQLAlchemyJobStore`, SQLite): job'ы переживают рестарт бота; поллинг БД раз в N секунд не используется. Job store — **отдельный файл** рядом с основной БД (`data/bot.sqlite3` → `data/bot.jobs.sqlite3`, `bot/main.py::jobs_db_path`): `SQLAlchemyJobStore` пишет синхронно, в потоке event loop, и в общей с данными БД такая запись ждала бы чужую незакоммиченную запись aiosqlite — бот замирал бы до `busy_timeout` с `database is locked` (найдено нагрузкой, §22). Источник истины — строки `reminders` основной БД: при старте job'ы пересоздаются из них, поэтому файл job'ов — кэш планировщика и в бэкап (§20) не входит. Планировщик строго `timezone="UTC"` (в APScheduler 3.x — известная ловушка DST с `ZoneInfo`); локальное время `HH:MM` пользователя конвертируется в UTC через `zoneinfo` при создании/обновлении job'а. Слот — цепочка одноразовых `DateTrigger`-job'ов: сработал → отправил → пересчитал следующий `HH:MM` → новый job. Job defaults: `coalesce=True`, `max_instances=1`, `misfire_grace_time=300`. При старте бота — catch-up по `reminders.next_trigger_time`: если время прошло и напоминание не отправлено — отправить одно (с кнопками) и пересчитать; не более одного раза в день на тип (`last_sent_date`). **С 0.5.4:** окно catch-up — `CATCHUP_WINDOW = 6 ч`: слот, просроченный больше чем на 6 часов, не отправляется — молча переносится на следующее вхождение (утреннее напоминание в три часа дня бессмысленно); повтор замера, просроченный больше чем на 6 часов или не доставленный catch-up'ом, **удаляется** — включённой строки без job'а не остаётся (иначе она блокировала бы новые повторы, `ensure_remeasure`). Отложенное «Через 30 мин» сбрасывает `last_sent_date`, поэтому catch-up его доставляет (в пределах окна).

Напоминания — только текстовые сообщения, без медиа (кнопки у прежнего сообщения снимаются `edit_message_reply_markup`, см. «Кнопки напоминаний (0.5.4)»).

**Сообщение слота (утро/обед/вечер)** — одно, с перечнем лекарств этого слота:

> Пора измерить давление ☀️
> 💊 Лекарства: Лозартан 50 мг, Амлодипин 5 мг

Лекарства со слотом `crisis` в напоминаниях не участвуют (§4.4).

Кнопки:
- **«Записать давление»** → сценарий 6.3 (сообщение редактируется);
- **«Принял всё ✓»** → все лекарства слота помечаются как `taken` одним действием (одна транзакция, по одной записи `medication_logs` на лекарство); если включён «Повтор после лекарства» — планируется повторный замер (как при «💊 Лекарство» → «Готово» с непустым выбором, §6.3), идемпотентно;
- **«Отметить принятые»** (до 0.5.5 — «Частично…») → список лекарств слота с чекбоксами (inline-кнопки «✓ Лозартан», «✓ Амлодипин»), пользователь отмечает принятые, затем «Готово»: отмеченные — `taken`, непринятые — `skipped`;
- **«Через 30 мин»** — не создаёт новую строку: `next_trigger_time` существующей строки слота (`morning`/`noon`/`evening`/`med_remeasure`) перезаписывается на `now + 30 мин` транзакционно, job пересоздаётся; регулярное расписание слота сохраняется (после срабатывания — следующий день);
- **«Пропускаю сегодня»** (до 0.5.5 — «Пропускаю») → все лекарства слота помечаются как `skipped`, слот перепланируется на завтра, без уведомлений.

Если в слоте нет лекарств — кнопки «Принял всё ✓» и «Отметить принятые» не показываются; «Пропускаю сегодня» только перепланирует слот.

**Крупный режим (0.5.10, UI10).** Все сообщения напоминаний — слот-сообщения, повтор «Не забыли…», повтор замера, «Не забыли про лекарства?» — рендерятся по `users.ui_large`: в крупном режиме кнопки — по одной в строке (пары «Записать давление» / «Принял всё ✓», «Отметить принятые» / «Через 30 мин», «Принял всё ✓» / «Отметить принятые» (режим «Только лекарства»), «Записать сейчас» / «Через 30 мин» разъезжаются по строкам), значения — `<b>`; тексты, callback'и и сценарии — без изменений (канон §11).

**После отметки (0.5.5, U1; ответы — `doc/REMINDERS-QUESTIONS.md`, `doc/REMINDERS-UX-QUESTIONS.md`).** Итог «Принял всё ✓» и «Готово» списка — две строки: слот с эмодзи (`SLOT_EMOJI`, §6.7) и итог по правилам «Тексты итога» ниже, например «☀️ Утро» / «Отметил: принято: Лозартан 50 мг». Кнопки — «Записать давление» (`rem:write`, §6.3) и «В меню»: врач советует мерить давление до таблетки, а после — сравнить. Итог «Пропускаю сегодня» — прежний («Хорошо, напоминание перенёс на завтра.» + «В меню»).

Правило «не дёргаем зря»: слоты `morning`/`noon`/`evening` не отправляются, если за последние 3 часа у пользователя уже есть запись. На `med_remeasure` это правило НЕ распространяется (иначе оно заблокирует повторный замер через 60 минут после приёма лекарства), и на перенесённые «Через 30 мин» — тоже.

**Повтор после лекарства (`med_remeasure`).** Когда пользователь отметил приём лекарства со статусом `taken` («💊 Лекарство» → «Готово» с непустым выбором в сводке записи, «Приём лекарства» из «➕ Добавить запись», или «Принял всё ✓» в слоте), через `med_remeasure_delay` минут отправляется одноразовое напоминание: «Прошло время после лекарства — пора повторить измерение 🌡» + кнопки «Записать сейчас» / «Через 30 мин» / «Больше не напоминать». После доставки строка не удаляется, а помечается доставленной (`enabled=0`): кнопки сообщения действуют по её id. «Через 30 мин» снова включает её с `next_trigger_time = now + 30 мин`; «Больше не напоминать» закрывает этот повтор — строка удаляется, job снимается, настройка «Повтор после лекарства» не меняется; «Записать сейчас» — запись давления. Доставленная строка не мешает новому повтору (при постановке и при следующей доставке прежние доставленные строки пользователя удаляются). Кнопки ищут строку по внутреннему `users.id` владельца, а не по Telegram id. Дедуп повтора: из сводки записи — по `measurement_id` (один повтор на измерение, сколько бы приёмов ни отмечено); из «Приёма лекарства» без измерения — один активный на пользователя (`measurement_id = NULL`; `ensure_remeasure` принимает `Optional[int]` — проверено по `bot/services/reminders.py`).

**Кнопки напоминаний (0.5.4, ответы — `doc/REMINDERS-QUESTIONS.md`).** Найдено проверкой на стенде: список «Частично…» (с 0.5.5 — «Отметить принятые») держал отметки в FSM (в памяти процесса) — после перезапуска бота кнопки молчали, при двух открытых списках «Готово» сохраняло чужой слот; «Через 30 мин» после «Отмена» не работало (id строки `0`); вчерашнее напоминание записывало приём на сегодня.
- **Без FSM.** Всё состояние — в `callback_data` и в клавиатуре сообщения (§8). Кнопки слот-сообщения несут слот и дату напоминания `ГГГГММДД` (локальный день пользователя на момент доставки): `rem:all:{slot}:{д}`, `rem:partial:{slot}:{д}`, `rem:snooze:{slot}:{д}`, `rem:skip:{slot}:{д}`. Строка слота находится на сервере по `(users.id, type)` — id строки в callback'е слота нет. Список «Частично…»: `rem:pt:{slot}:{д}:{id лекарства}` — переключить отметку, `rem:pd:{slot}:{д}` — «Готово», `rem:pc:{slot}:{д}` — «Отмена» (возвращает слот-сообщение с теми же кнопками). Отметки читаются из текущей клавиатуры сообщения (кнопки с префиксом «✓ »); по «Готово» — отмеченные `taken`, остальные лекарства слота `skipped`. Любое число открытых списков и перезапуск бота на них не влияют; запись давления (FSM §6.3) — тоже. Повтор замера: `rem:snooze:{id}` / `rem:off:{id}` — по id строки, как прежде.
- **Дата приёма.** «День напоминания», «тот же день» и «сегодня» — локальные даты по `users.timezone`; `callback.message.date` (UTC) переводится в пояс пользователя. Приём пишется за день напоминания: нажатие в тот же день — момент нажатия; в другой день — момент доставки сообщения (`callback.message.date`, он внутри дня напоминания). Дедупликация «один раз за сутки» (§7.2) — по этому дню. Напоминание старше 2 суток (по дате в callback'е), «Через 30 мин» на напоминании не сегодняшнего дня, лекарство из списка, которого уже нет в слоте, мусорные данные — уведомление `callback.answer("Это напоминание устарело")`, данные не меняются. Повтор замера, доставленный больше 24 часов назад, — то же уведомление, строка удаляется. Callback'и 0.5.3 без даты (сообщения, отправленные до обновления): дата — день `callback.message.date`.
- **Уборка старых кнопок.** При отправке напоминания слота `message_id` сохраняется в `reminders.last_message_id` (§4.3); при отправке следующего напоминания того же слота у прежнего сообщения снимаются кнопки (`edit_message_reply_markup(reply_markup=None)`; сообщение удалено, старше 48 ч или уже без кнопок — не ошибка). Так в чате нет «живых» вчерашних кнопок. Для `med_remeasure` уборки нет — следующего напоминания того же типа у него нет; устаревший повтор (> 24 ч) обрабатывается уведомлением при нажатии.
- **Тексты итога.** Всё отмеченное уже было записано за этот день — «Эти лекарства уже отмечены сегодня ✓: {перечень}»; часть новая — «Отметил: {итог}» и строкой ниже «Уже отмечено сегодня: {перечень}». Прежний ответ «Отметил: нет данных» не показывается. Те же тексты — после «Готово» списка в «Дневнике за сегодня» (§6.4). В слоте без лекарств экрана итога нет (нет кнопок «Принял всё ✓» и «Отметить принятые»).
- **«Через 30 мин»** у слота сбрасывает `last_sent_date` строки (отложенное ещё не доставлено, см. catch-up выше).

**Удобство напоминаний, часть 2 (0.5.6; ответы — `doc/REMINDERS-UX-QUESTIONS.md`, X1–X10, X13–X16).**

*Режим слота (U5).* `reminders.mode` у слотов `morning` / `noon` / `evening`; у `med_remeasure` не используется (повтор замера — всегда про давление).
- **Выбор.** На экране «Напоминания» у включённого слота — кнопка режима: «💊🩺 Давление и лекарства» / «💊 Только лекарства» / «🩺 Только давление» (`set:rem:mode:{slot}`). Она открывает экран «О чём напоминать: {Утро}?» с тремя кнопками (у текущего — «✓ »; `set:rem:mode:{slot}:{both|meds|pressure}`) и «Назад» (к «Напоминаниям»). Нажатие сохраняет режим и перерисовывает этот же экран.
- **Несогласованные сочетания.** В слоте нет лекарств — кнопки «Только лекарства» нет, вместо неё строка текста «Чтобы напоминать только о лекарствах, сначала добавьте лекарства в этот слот.»; подделанный callback — уведомление «Сначала добавьте лекарства в этот слот». Выбран «Только давление», а в слоте есть лекарства — второй строкой экрана: «Лекарства этого слота не будут напоминаться. Отметить приём можно в «Дневнике за сегодня».» Удалили или перенесли последнее лекарство слота с режимом «Только лекарства» (бот, §6.6, и ЛК, §16.2) — режим слота сбрасывается в «Давление и лекарства» той же транзакцией; при доставке «Только лекарства» без лекарств тоже ведёт себя как «Давление и лекарства».
- **Тексты и кнопки:**

| Режим | Текст | Кнопки |
|---|---|---|
| `both` | «Пора измерить давление ☀️» + «💊 Лекарства: …» (как раньше) | «Записать давление» / «Принял всё ✓» / «Отметить принятые» / «Через 30 мин» / «Пропускаю сегодня» |
| `meds` | «Пора принять лекарства 💊» + строка перечня «Лозартан 50 мг, Амлодипин 5 мг» | пара в ряд «Принял всё ✓» / «Отметить принятые», «Через 30 мин», «Пропускаю сегодня» — по одной |
| `pressure` | «Пора измерить давление ☀️» (без перечня) | «Записать давление» / «Через 30 мин» / «Пропускаю сегодня» |

  В `pressure` «Пропускаю сегодня» только перепланирует слот — лекарства не напоминались, `skipped` не пишется.
- **«Не дёргаем зря»** (запись давления за последние 3 часа) — только для `both` и `pressure`; `meds` отправляется всегда: запись давления не значит, что таблетка принята.
- **Онбординг (§6.1) не меняется:** у всех слотов — `both`.

*Повтор, если не ответил (U4).* Одно повторное напоминание на слот в день.
- **Когда ставится.** При доставке слот-сообщения по расписанию или catch-up'ом (не отложенного «Через 30 мин»), если `users.followup_enabled` и нет паузы, — `reminders.followup_at = момент отправки + FOLLOWUP_DELAY` (`FOLLOWUP_DELAY = 60 мин`) и отдельный job `reminder:{id}:followup` (`DateTrigger`, UTC, те же job defaults).
- **Что считается ответом** (повтор не отправляется, `followup_at = NULL`, job снимается): нажатие любой кнопки этого напоминания — «Записать давление», «Принял всё ✓», «Отметить принятые», «Через 30 мин», «Пропускаю сегодня»; либо к моменту повтора есть отметка `taken` лекарства этого слота (`medication_logs.slot`) или — в режимах `both` / `pressure` — запись давления, сделанные не раньше чем за `FOLLOWUP_ANSWER_WINDOW` (1 ч) до отправки исходного напоминания (так засчитывается и ответ другим путём: «Записать давление» из меню, «💊 Лекарство» в сводке записи, «💊 Отметить приём» в дневнике). Чтобы «Записать давление» напоминания тоже было ответом, кнопка слот-сообщения несёт слот и дату: `rem:write:{slot}:{д}` (сценарий §6.3 тот же).
- **Сообщение.** Новое, **без звука** (`disable_notification=True`), по режиму слота: `meds` — «Не забыли про лекарства? 💊» + строка перечня; `pressure` — «Не забыли измерить давление? 🩺»; `both` — обе строки («Не забыли измерить давление? 🩺» и «Не забыли про лекарства? 💊 {перечень}»; без лекарств в слоте — только первая). Кнопки — как у слот-сообщения этого режима, с той же датой. У исходного напоминания кнопки снимаются (механизм 0.5.4), `last_message_id` — на повтор (следующее напоминание слота снимет кнопки уже с него). «Через 30 мин» на повторе работает как обычно и повтора не порождает.
- **Гонка «ответ и повтор».** Отправка повтора захватывает строку атомарно: `UPDATE reminders SET followup_at = NULL WHERE id = :id AND followup_at = :expected`; ответ обнуляет `followup_at` тем же UPDATE. Кто первым обнулил — тот и прав: ответ, пришедший раньше, отменяет повтор; повтор, ушедший раньше, остаётся (кнопки его работают).
- **Catch-up при старте.** `followup_at` в прошлом: просрочка ≤ `CATCHUP_WINDOW` (6 ч) — доставить (с проверкой ответа), больше — обнулить.
- **Выключение** «Повторять, если не ответил» снимает все отложенные повторы пользователя (`followup_at = NULL`, job'ы `…:followup` сняты). Без согласия (§6.11) и на паузе повтор не отправляется и обнуляется.
- У `med_remeasure` своего повтора нет.

*Пауза напоминаний (U7).* `users.reminders_paused_until` — последний день паузы (включительно, локальная дата).
- **Экран.** Первая строка «Напоминаний»: «⏸ Пауза: нет» или «⏸ На паузе до 13.11» (`set:rem:pause`) → экран «Поставить напоминания на паузу?» (на паузе — «Напоминания на паузе до 13.11.»): «1 день» / «3 дня» / «7 дней» (`set:rem:pause:1|3|7`), на паузе — ещё «▶️ Снять паузу» (`set:rem:pause:off`), и «Назад». «1 день» — до конца сегодняшнего дня, «3 дня» — сегодня и ещё 2, «7 дней» — сегодня и ещё 6; повторный выбор отсчитывается от сегодня. После выбора — экран «Напоминания» с обновлённой первой строкой.
- **На паузе:** слоты не отправляются — job срабатывает и молча переносит слот на следующее вхождение (как правило «3 часа»); повтор замера (`med_remeasure`) не отправляется и удаляется; повтор «если не ответил» не ставится. Кнопки старых сообщений работают (данные не теряются), кроме «Через 30 мин» — уведомление «Напоминания на паузе до 13.11».
- **Конец паузы.** Первое слот-сообщение после паузы начинается строкой «Напоминания снова включены ▶️», тогда же `reminders_paused_until = NULL`. Все слоты выключены — отдельного сообщения нет (на экране «Напоминания» — «⏸ Пауза: нет»). «▶️ Снять паузу» — `NULL` сразу, экран «Напоминания» с первой строкой текста «Пауза снята ▶️».

*Константы* — в `bot/services/reminders.py`: `FOLLOWUP_DELAY = 60 мин`, `FOLLOWUP_ANSWER_WINDOW = 1 ч` (рядом с `CATCHUP_WINDOW = 6 ч`).

**Сверка слотов с базой (0.5.7; ответы — `doc/APPOINTMENTS-QUESTIONS.md`, Н8).** Личный кабинет — отдельный процесс: форма напоминаний (§16.2) меняет строки `reminders`, а job'ы живут только в процессе бота. До 0.5.7 изменение времени или включение слота в ЛК не доходило до бота до его перезапуска (job на старое время срабатывал, видел `next_trigger_time > now` и выходил без нового job'а — слот молчал; более раннее время срабатывало в старое). Исправление — **тик**: один периодический job бота `reminders:tick` (`IntervalTrigger` 60 с, UTC; `coalesce=True`, `max_instances=1`; точка входа — module-level `fire_tick`, как у `fire_reminder`), ставится при старте. За один проход:
- читает включённые слоты (`morning` / `noon` / `evening`, `enabled=1`) и все job'ы планировщика одним вызовом (`scheduler.get_jobs()`, без запроса на каждый слот);
- слот, у которого нет job'а `reminder:{id}` или job стоит не на `next_trigger_time`, — ставит job на `next_trigger_time` (тот же `id`, `replace_existing`); `next_trigger_time` в прошлом — как при старте (§6.7, catch-up): в окне `CATCHUP_WINDOW` — доставка `catchup`, за окном — молча на следующее вхождение;
- job выключенного слота снимает;
- повторы замера (`med_remeasure`) и повторы «если не ответил» не трогает: их создаёт только бот.

Правка в ЛК применяется не позже чем через минуту. Фраза выше «поллинг БД раз в N секунд не используется» остаётся верной для доставки слотов: тик только сверяет job'ы. С 0.5.8 тот же тик доставляет уведомления о визитах (§6.13) — один тик на всё.

**Визиты и пауза (0.5.8, В8).** Пауза (`users.reminders_paused_until`) и повтор «если не ответил» действуют только на слоты и `med_remeasure`; уведомления о визитах (§6.13) приходят и на паузе, повтора у них нет.

**Приём лекарства отдельной записью (0.6.0).** Пункт «Приём лекарства» меню «➕ Добавить запись» (`add:med`, §6.14) — экран-фасад над `medication_logs`: выбор **приёма** (строка «Лозартан 50 мг · утро», §4.4 — приём, а не препарат, B3) → статус «Принял» / «Пропустил» / «Частично» (`taken` / `skipped` / `partial`) → время: «Сейчас» / «Другое» (`ЧЧ:ММ`, последние 24 часа) — это `timestamp` записи журнала; слот — из строки лекарства, не меняется (C4). Дедуп за локальные сутки — §7.2 (повторная отметка того же приёма новые записи не создаёт). При статусе `taken` и включённом «Повторе после лекарства» — повторный замер (дедуп: один активный на пользователя, `measurement_id = NULL`, C2/C18). Пустой список лекарств — «Сначала добавьте лекарства в настройках» + «Добавить» (§6.6, C10).

### 6.8 «О боте / Поддержать»

Кнопка в главном меню и команда `/support`. Доступно всегда (это справка, не реклама). ⚠ ТЗ-ограничение «не показывать поддержку до 3 записей» трактуем как просьбы/футеры (§6.5, §6.1), а не как скрытие справочной информации по явной кнопке.

**Экран «О боте»** (канонические тексты в `texts.py`, автор может отредактировать):

> Бота пишет один разработчик в свободное время. Он бесплатный и без рекламы: все функции доступны всегда.
>
> Я сделал его, потому что мне самому нужен был простой дневник давления — записать показание за пару секунд и показать врачу понятную сводку, без лишних экранов.
>
> Бот только записывает цифры и показывает статистику. Он не ставит диагнозов и не даёт медицинских советов.

Кнопки: «Поддержать проект» (URL `SUPPORT_URL`), «Написать автору» (`AUTHOR_CONTACT`), «Наше сообщество» (URL `GROUP_URL` — группа бота в Telegram), «Назад». Кнопка скрывается, если соответствующая переменная пустая или плейсхолдер.

**Экран «Поддержать»** (текст перед ссылкой):

> Поддержка добровольная, функции за оплатой не закрыты.
> Деньги идут на хостинг, развитие бота и тестирование на реальных тонометрах с разными протоколами.

Кнопка-ссылка «Перейти на страницу поддержки». Реквизиты в боте не хранятся и не показываются.

### 6.9 Команды

| Команда | Поведение |
|---|---|
| `/start` | онбординг (если не пройден) или главное меню |
| `/support` | экран «О боте / Поддержать» |
| `/iamdoctor` | Регистрация врача в боте: ФИО → врача можно найти по @username (§21). Повторный вызов — смена ФИО. |
| `/visits` | Экран «📅 Визиты и анализы» (§6.13, 0.5.8). |
| `/cabinet` | Если пользователя нет в `web_users` — запускает FSM создания ЛК (запрос e-mail). Если есть — генерирует одноразовую ссылку (15 минут) и отправляет. Доступно, когда развернут веб-кабинет. |

Прочее — через меню. «Режим ввода» — FSM-состояния (числа, заметка, время, таймзона, шаги добавления лекарства); команды (`/start`, `/support`, `/cabinet`, `/doctor`, `/iamdoctor`, `/visits`) обрабатываются всегда и сбрасывают FSM, чтобы пользователь не застрял в вводе. Свободный ввод вне режима ввода → мягкая подсказка «Выберите пункт меню» + кнопка «В меню».

### 6.10 Архивные записи (перенос прошлых измерений)

Назначение: перенести в дневник прошлые измерения (например, велось в блокноте). Доступ: «Настройки» → «Архивные записи». Момент измерения — только прошедший (строго ≤ текущего момента); глубина не ограничена. Сохраняется обычная запись `measurements` с указанным `timestamp` — статистика, графики, «Дневник за сегодня» и CSV учитывают её наравне с остальными. Дубль за ту же минуту не создаётся (§7.2) — «Это давление уже записано».

**Способ 1 — по одной записи (кнопками).** FSM-цепочка:

1. **«Когда?»** — кнопки «Сегодня», «Вчера», «3 дня назад», «Неделю назад», «Другая дата»; «Другая дата» — свободный ввод `ДД.ММ.ГГГГ` или `ДД.ММ` (год — текущий). Даты считаются в таймзоне пользователя.
2. **«Во сколько?»** — кнопки «08:00», «13:00», «21:00», «Другое» (ввод `ЧЧ:ММ`, формат как в напоминаниях). Момент измерения (дата + время в таймзоне пользователя) проверяется на будущее время здесь же: «Время измерения не может быть в будущем» + повтор ввода.
3. Калькулятор §6.3 — один экран «САД | ДАД | Пульс» (0.6.0; архив использует тот же калькулятор, C14), та же валидация (§9).

Сохранение → «✅ Записано за 12.03.2025, 08:00» + сводка введённых данных (формат — по строке на поле, как §6.3; в крупном режиме значения — `<b>`, UI4) + кнопки: «✏️ Изменить» · «Добавить заметку» · «Приняли лекарство?» · «Самочувствие» · «➕ Ещё запись» · «В меню». «Ещё запись» — новый проход с шага «Когда?» (удобно переносить блокнот подряд). «✏️ Изменить» в этой сводке правит только что созданную запись (по id из контекста сохранения, §7.2), а не «последнюю» по `timestamp`, — архив можно вносить не в хронологическом порядке.

**Способ 2 — быстрый ввод (одной строкой).** Свободный ввод сообщением формата `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]`: например `12.03 08:30 135/85 72` (год — текущий, если не указан) или `12.03.2024 08:30 135/85` (без пульса). Валидация полей — та же (§9), включая запрет будущего времени. Не распознано — «Не понял формат. Пример: 12.03 08:30 135/85 72» + кнопка «Отмена». После сохранения — та же сводка с кнопками опциональных шагов.

**Опциональные шаги архивной записи.** Заметка и самочувствие — как в §6.3. «Приняли лекарство?» — только исторический флаг `medication_taken` («Да» / «Нет»): повторный замер не планируется, выбор конкретных лекарств и `medication_logs` не предлагаются (момент приёма — в прошлом). Архивная запись из бота не создаёт `medication_logs` и не предлагает выбор лекарств. Для детального исторического ввода с привязкой к лекарствам используйте личный кабинет (§16.2).

⚠ Строка «Эти значения стоит обсудить с врачом…» (§6.3) для архивных записей не показывается — данные исторические, а не срочные.

### 6.11 Согласие на обработку персональных данных (0.5.0)

Основание — Закон РБ № 99-З: данные о здоровье — специальные (ст. 8), обработка только с согласия; согласие в электронной форме (ст. 5 п. 3); до согласия — сведения ст. 5 п. 5 (`privacy.html#consent`); отзыв — в той же форме (ст. 10). Решения — `doc/PATIENT-PROFILE-QUESTIONS.md`, тексты — `doc/PERSONAL-DATA-TEXTS.md`. Оператор — физическое лицо Старинский Олег Григорьевич (адрес — в `privacy.html`). Сервис — для лиц 16+ (в 0.5.0 подтверждается фразой согласия, §17).

**Два согласия пациента** (`consents`, §4.15): основное (`processing`) — на ведение дневника; на врача (`doctor`) — отдельно при каждом приглашении. **Согласие врача** — `doctors.consent_*` (§4.8).

**Пациент без действующего основного согласия** (`ConsentMiddleware`):
- middleware блокирует апдейт, **только если** у отправителя есть строка `users` с `onboarded=1` и нет действующего основного согласия (§4.15). Нет строки `users` или `onboarded=0` — пропускает: новый пользователь и врач без дневника обрабатываются хендлером `/start` (§6.1, §21) — новому `/start` показывает Б1 первым шагом онбординга, врачу без дневника — меню врача; «Вести свой дневник давления» тоже начинается с Б1;
- существующий пользователь (`onboarded=1`) — Б1 с первой строкой Б2 при любом следующем сообщении или нажатии;
- до ответа работают только кнопки экрана согласия, «Удалить аккаунт» (`set:delete`, `set:delete:ok`, §19), `/start` (снова Б1); остальное — Б4: на нажатие кнопки — `callback.answer("Сначала нужно согласие на обработку данных")` (всплывающая подсказка без `show_alert`) и Б1 новым сообщением; на сообщение — сразу Б1;
- напоминания и повторы замера не отправляются (проверка при срабатывании; расписание не меняется);
- проверка не касается стороны врача и администратора: `doc:*`, `/iamdoctor`, `/start doc_<code>`, `/admin` и `admin:*` — у врача своё согласие (Б8).

**Подтверждение врачей (Д4).** Связь `doctor_patients` `pending` / `active` без действующей записи `consents(kind='doctor', doctor_id)` (созданные до 0.5.0, принятые по ссылке до 0.5.0) — после «Согласен» на Б1 или при следующем обращении пациента, по одному экрану на связь. До ответа доступ врача приостановлен бессрочно; напоминаний нет.

**Доступ врача к пациенту** — три условия сразу: `doctor_patients.status='active'`; действующая `consents(kind='doctor', doctor_id, withdrawn_at IS NULL)`; действующее согласие врача (§4.8). Иначе пациент **не отображается** врачу вовсе (кабинет: список, карточка, экспорт, заметки — как отсутствующий; бот: «Мои пациенты», «Открыть в кабинете», уведомление об отчёте не отправляется). «Отправить отчет» пациента при приостановленном доступе — экран Д4 для этой связи.

**Тексты бота (дословно; `{privacy_url}` — `PRIVACY_URL`):**

- Б1 (новый пользователь):
  ```
  Перед началом — о ваших данных.

  Бот хранит ваши измерения давления и пульса, лекарства и заметки. По закону это данные о здоровье, и нужно ваше согласие.

  • Записи видите только вы и врач, которого вы пригласите сами.
  • Данные хранятся на сервере в Германии, сообщения идут через Telegram.
  • Отозвать согласие и удалить всё можно в любой момент.

  Бот — для тех, кому исполнилось 16 лет.
  Полный текст: {privacy_url}#consent
  ```
  Кнопки: «✅ Согласен» (`cons:yes`) · «Не согласен» (`cons:no`) · «📄 Прочитать полностью» (URL `{privacy_url}#consent`); существующему — ещё «Удалить аккаунт» (`set:delete`).
- Б2 (существующий пользователь) — первая строка Б1 заменяется на «Теперь для работы бота нужно ваше согласие на обработку данных.»
- Б3 «Не согласен», новый: «Без согласия бот не может хранить ваши записи. Мы ничего о вас не сохранили.\nЕсли передумаете — нажмите /start.» (запись `users` удаляется). Существующий: «Без согласия бот не может хранить ваши записи. Удалить аккаунт и все данные?» — кнопки «Удалить аккаунт» (`set:delete`, дальше §19) · «Назад к согласию» (`cons:back`).
- Б4 действие до согласия: на кнопку — всплывающая подсказка «Сначала нужно согласие на обработку данных» (`callback.answer`) + Б1 новым сообщением; на сообщение — Б1.
- Б5 согласие дано: «Спасибо! Согласие сохранено.» → онбординг (новый) / Д4 (если есть связи без согласия) / главное меню.
- Б6 «Настройки» → «Согласие на обработку данных» (`set:consent`): «Согласие дано {ДД.ММ.ГГГГ}.\nПолный текст: {privacy_url}#consent\n\nЕсли отозвать согласие, аккаунт и все записи будут удалены.» — кнопки «Отозвать согласие» (`set:consent:withdraw`) · «Назад». Подтверждение: «Отозвать согласие? Аккаунт и все записи будут удалены без возможности восстановления.» — «Да, отозвать и удалить» (`set:consent:withdraw:ok`) · «Отмена» → удаление аккаунта §19, последнее сообщение «Согласие отозвано, ваш аккаунт и все данные удалены.»
- Б7 согласие на врача — перед экраном выбора способа §21: «Врач увидит ваши измерения, лекарства, заметки, графики и отчёты и сможет оставлять комментарии. Менять записи он не сможет.\n\nЗакрыть доступ можно в любой момент: «Мой врач» → «Отозвать доступ».» — кнопки «✅ Согласен, пригласить» (`set:doctor:agree`) · «Назад». Запись `consents(kind='doctor')` — в момент нажатия; `doctor_id` — по e-mail / @username сразу, по ссылке — при «Принять» (открытая запись без `doctor_id` повторно используется новым кодом, §4.15); `withdrawn_at` — при «Отозвать доступ» и «Отклонить» у врача.
- Б8 согласие врача (`/iamdoctor`, «Принять» приглашение, любое действие врача в боте без действующего согласия врача): «Для работы кабинета врача мы храним ваши ФИО, e-mail или имя пользователя в Telegram и ваши заметки к записям пациентов.\nПодробнее: {privacy_url}#policy» — кнопки «✅ Согласен» (`doc:consent:yes`; при приглашении — `doc:consent:yes:{code}`, затем «Принять» выполняется) · «Отмена» (`doc:consent:no`).
- Д4, связь `active`: «Ваш врач {врач} видит ваши измерения, лекарства, заметки, графики и отчёты.\nОставить ему доступ?» — «✅ Оставить доступ» (`cons:doc:keep:{doctor_id}`) · «Закрыть доступ» (`cons:doc:close:{doctor_id}`: `revoked`, врачу в Telegram — уведомление §21).
- Д4, связь `pending`: «Вы пригласили врача {врач}. После принятия приглашения он увидит ваши измерения, лекарства, заметки, графики и отчёты.\nОставить приглашение?» — «✅ Оставить» (`cons:doc:keep:{doctor_id}`) · «Отменить приглашение» (`cons:doc:close:{doctor_id}`: `revoked` + списание неиспользованного кода `doctor_invites`; врач в старом приглашении видит «Приглашение устарело или уже использовано. Попросите пациента прислать новое.»).
- «Оставить» → `consents(kind='doctor', doctor_id, version=CONSENT_VERSION, channel='bot')`. `{врач}` — как в списке «Мой врач» (ФИО, @username или e-mail).

**Кабинет:**
- К1 «Личная карточка»: «Согласие на обработку данных: дано {ДД.ММ.ГГГГ}» + кнопка «Отозвать согласие» (К5).
- К5 отзыв в кабинете (ст. 10 п. 1 — в той же форме, в какой дано): «Отозвать согласие» → страница подтверждения «Отозвать согласие? Аккаунт и все записи будут удалены в течение часа без возможности восстановления.» — «Да, отозвать и удалить» (POST `/consent/withdraw`, CSRF) · «Отмена». Одной транзакцией: основное согласие закрывается `withdrawn_at`, `users.deletion_requested_at = now`, сессии `web_sessions` пользователя удаляются; ответ — страница «Согласие отозвано. Аккаунт и все данные будут удалены в течение часа.» без входа. С этого момента пациент не проходит проверку согласия (бот, кабинет, врач, напоминания). Сам аккаунт удаляет бот (§19): кабинет не имеет доступа к планировщику бота, а джобы удаляются строго до `users`.
- К3 пациент без действующего согласия (вход по magic link или паролю): все страницы ЛК, экспорт и импорт → `303 /consent`. Страница — заголовок «Нужно ваше согласие» и текст Б1 без строки «Полный текст», ссылка «Полный текст согласия» (`{privacy_url}#consent`); кнопки «Согласен» (POST `/consent`, CSRF → `consents`, `channel='cabinet'`) · «Не согласен» → «Без согласия дневник работать не может. Удалить аккаунт и все данные можно в боте: «Настройки» → «Удалить аккаунт».» + «Выйти». Связи врачей без согласия (Д4) подтверждаются только в боте.
- К2 врач без действующего согласия (любой путь входа: ссылка доступа, ссылка входа, e-mail + пароль): все страницы врача → `303 /doctor/consent`. Ссылка доступа к этому моменту уже списана и связь активирована; после согласия — в карточку пациента. Страница: «Кабинет врача» / «Для работы кабинета мы храним ваши ФИО, e-mail или имя пользователя в Telegram и ваши заметки к записям пациентов.» / чекбокс «Согласен на обработку моих данных (ФИО, e-mail и/или Telegram) — политика» (`{privacy_url}#policy`) / кнопка «Продолжить» (POST, CSRF; без отметки — неактивна, сервер проверяет) / «Выйти».

**Новая редакция согласия (0.5.8, В20-Б).** Визиты к врачам, анализы и обследования — данные о здоровье (ст. 8), в перечне согласия 0.5.0 их нет. С 0.5.8 `CONSENT_VERSION` и `POLICY_DATE` — дата выпуска 0.5.8: существующие пользователи при следующем обращении видят Б1 с первой строкой Б2 (механизм выше), новые — Б1. Тексты (дословно, `doc/PERSONAL-DATA-TEXTS.md` и `privacy.html` правятся в коде 0.5.8; **до выпуска — показать юристу**; если юрист сочтёт, что формулировка «заметки» покрывает визиты, — без новой версии, только `privacy.html`):
- Б1, второй абзац: «Бот хранит ваши измерения давления и пульса, лекарства, заметки, визиты к врачу и анализы. По закону это данные о здоровье, и нужно ваше согласие.»
- `#consent`, п. 2 «Данные», после «лекарства и отметки об их приёме;»: «визиты к врачам, анализы и обследования — вид, название, дата, время, заметка;».
- `#policy`, таблица — новая строка после «Измерения давления и пульса…»: «Визиты к врачам, анализы и обследования — вид, название, дата, время, заметка» | «Напоминать о них и показывать историю визитов».
- `#policy`, «Кто видит ваши данные», пункт о враче — в конце: «Визиты и анализы врач не видит.»

**Новая редакция согласия (0.5.11, В35-Б).** Заключения врачей — данные о здоровье (ст. 8), в перечне согласия 0.5.8 (выше) их нет. С 0.5.11 `CONSENT_VERSION` и `POLICY_DATE` — дата выпуска 0.5.11 в формате `ГГГГ-ММ-ДД` (как `"2026-11-11"` сейчас; проставляется в `bot/services/consent.py` при релизе, сверка — `test_services_consent.py`): существующие пользователи при следующем обращении видят Б1 с первой строкой Б2 (механизм выше), новые — Б1. Тексты (дословно, `doc/PERSONAL-DATA-TEXTS.md`, `bot/texts.py` (экран Б1) и `privacy.html` правятся в коде 0.5.11; **до выпуска — показать юристу**; формулировки уточнены по ревью 13.11.2026 — политика не утверждает хранение фото, которого у нас нет):
- Б1, второй абзац: «Бот хранит ваши измерения давления и пульса, лекарства, заметки, визиты к врачу, анализы и заключения врачей. По закону это данные о здоровье, и нужно ваше согласие.»
- `#consent`, п. 2 «Данные», после «визиты к врачам, анализы и обследования — вид, название, дата, время, заметка;»: «заключения врачей, результаты анализов и обследований — текст; фото документов хранится в Telegram, у бота — только идентификатор;».
- `#policy`, таблица — новая строка после «Визиты к врачам, анализы и обследования…»: «Заключения врачей — текст; фото документов хранится в Telegram, у нас — только идентификатор» | «Показывать их вместе с записью о событии и включать в отчёт для врача».
- `#policy`, «Кто видит ваши данные», пункт о враче — заменить «Визиты и анализы врач не видит.» на «Визиты, анализы и заключения врач не видит.».
- Кабинет (К3) — тот же текст `#consent`.

### 6.12 Данные пациента для врача (0.5.1)

ФИО, дата рождения и пол — чтобы приглашённый врач узнал пациента и правильно понял показатели. Решения — `doc/PATIENT-PROFILE-QUESTIONS.md` (P1–P9). Данные входят в согласие 0.5.0 («если вы их укажете»), повторного согласия нет.

- **Что собирается** (P1, P4): фамилия, имя, отчество (необязательно) — отдельные поля по 1–50 символов: буквы, пробел, дефис, апостроф; лишние пробелы убираются, регистр сохраняется. Дата рождения — `ДД.ММ.ГГГГ`, от 01.01.1900 до сегодня. Пол — кнопками.
- **16+** (P5): возраст на сегодня (в часовом поясе пациента) меньше 16 — данные не сохраняются, текст П5.
- **Когда нужны** (P2): только для приглашения врача. «Мой врач» без врача: Б7 → (данные не заполнены) ввод данных → выбор способа. Дневник без врача работает без данных.
- **Где вводятся** (P3): бот — «Настройки» → «👤 Мои данные» и перед приглашением врача; ЛК — «Личная карточка» (§16.2). Поля одни и те же. Данные можно удалить — врач снова видит имя из Telegram.
- **Предложение заполнить** (P8, П7): пациенту с действующим согласием, врачом `pending` / `active` и незаполненными данными — один раз (`users.profile_prompted_at`): после «Согласен» (Б5) и Д4 или при первом обращении после выпуска 0.5.1 (`ConsentMiddleware`, как Д4). «Позже» больше не спрашивает.
- **Имя из Telegram** (P7): `users.telegram_name` — «имя фамилия» профиля Telegram, обновляется при обращении к боту. Пока данных нет, врач видит его с пометкой «данные не заполнены»; имени тоже нет (пациент давно не писал боту) — «Пациент {telegram_id}».
- **Приглашения и письма** (P9) — без изменений: имя из Telegram. ФИО и дата рождения — только в кабинете врача и боте врача после принятия приглашения.

**Подписи пациента у врача:**
- полная — «Иванов Иван Иванович» (отчества нет — «Иванов Иван»);
- короткая — «Иванов И. И.»;
- возраст — «67 лет» / «41 год» / «22 года» (по правилам склонения);
- пол — «мужской» / «женский», в списке — «м» / «ж»;
- без данных — `telegram_name` или «Пациент {telegram_id}».

**Тексты бота (дословно):**
- Кнопка в настройках: «👤 Мои данные».
- П1 данные не заполнены: «Мои данные не заполнены.\n\nФИО, дату рождения и пол увидит только врач, которого вы пригласите, — так он узнает вас и правильно поймёт показатели.» — кнопки «Заполнить» (`prof:edit`), «Назад» (`set:back`).
- П2 данные заполнены: «Мои данные\n\nФИО: {ФИО}\nДата рождения: {ДД.ММ.ГГГГ} ({возраст})\nПол: {пол}\n\nЭти данные видит только врач, которого вы пригласили.» — кнопки «Изменить» (`prof:edit`), «Удалить данные» (`prof:del`), «Назад» (`set:back`).
- П3 ввод (FSM `PatientProfile`, у каждого шага «Отмена» → П1 / П2): «Введите фамилию:» → «Введите имя:» → «Введите отчество:» + кнопка «Нет отчества» (`prof:nomiddle`) → «Дата рождения в формате ДД.ММ.ГГГГ, например 12.03.1958:» → «Пол:» — кнопки «Мужской» (`prof:sex:m`), «Женский» (`prof:sex:f`). При изменении в подсказке шага — «Сейчас: {значение}».
- П4 ошибки: имя — «От 1 до 50 букв; можно пробел, дефис и апостроф. Попробуйте ещё раз:»; дата — «Нужна дата в формате ДД.ММ.ГГГГ, например 12.03.1958:».
- П5 младше 16: «Сервис доступен с 16 лет. Если вы законный представитель — заполните данные и согласие от своего имени.» — данные не сохраняются, кнопка «Назад» (`set:back`).
- П6 сохранено: «Данные сохранены.» + экран П2 (или выбор способа приглашения, если ввод начался из «Мой врач»). Удаление: «Удалить ФИО, дату рождения и пол? Врач снова будет видеть только ваше имя из Telegram.» — «Да, удалить» (`prof:del:ok`), «Отмена» (`prof:show`) → «Данные удалены.» + П1.
- Перед приглашением врача (данные не заполнены): «Чтобы врач узнал вас, укажите ФИО, дату рождения и пол.» — затем шаги П3; после сохранения — выбор способа приглашения.
- П7 предложение заполнить: «Заполните ФИО и дату рождения — врач увидит их в кабинете.» — кнопки «Заполнить» (`prof:edit`), «Позже» (`prof:later` → главное меню).

**Кабинет:** §16.2 (форма пациента), §21 (врач). **PDF:** §10.1. **Админка:** только счётчик (§18.2).

### 6.13 Визиты и анализы (0.5.8)

Пользователь заносит предстоящий визит к врачу, анализы или обследование — бот заранее напоминает, после события спрашивает, как прошло, и предлагает записать следующее. Бот не записывает к врачу и не связан с поликлиниками; подготовку к анализам не подсказывает — показывает только заметку пользователя (§1, non-goals). Решения — `doc/APPOINTMENTS-QUESTIONS.md` (В1–В24, Н1–Н8); отметка из карточки и заключения (0.5.11) — `doc/APPOINTMENTS-POSTVISIT-QUESTIONS.md` (В25–В38, У1–У3). Таблица — §4.16, таблица заключений — §4.17, сервис — `bot/services/appointments.py`, хендлеры — `bot/handlers/appointments.py`.

**Типы и подсказки (В1, В2):**

| `kind` | Кнопка шага 1 | Иконка | Вопрос шага 2 | Подсказки шага 2 |
|---|---|---|---|---|
| `doctor` | «🩺 Визит к врачу» | 🩺 | «К какому врачу?» | Терапевт, Кардиолог, Эндокринолог, Невролог, Окулист, Нефролог |
| `lab` | «🧪 Анализы» | 🧪 | «Какие анализы?» | Общий анализ крови, Биохимия, Липидный профиль, Анализ мочи |
| `exam` | «📋 Обследование» | 📋 | «Какое обследование?» | ЭКГ, УЗИ сердца, СМАД, УЗИ почек |

**Формат события в текстах:** «{иконка} {title} — {дд} {ДД.ММ}, {ЧЧ:ММ}», где `{дд}` — «пн»…«вс»; без времени — «{иконка} {title} — {дд} {ДД.ММ}, без времени». В карточке и «Прошедших» дата — «ДД.ММ.ГГГГ». Заметка (если есть) — следующей строкой с отступом в три пробела.

**Список** (`ap:list`, кнопка меню «📅 Визиты и анализы», `/visits`):
```
📅 Визиты и анализы

Скоро:
🩺 Кардиолог — пт 14.11, 10:30
   поликлиника №5, каб. 212
🧪 Общий анализ крови — пн 17.11, без времени
```
- «Скоро» — `status='planned'` и `starts_on >= сегодня` (локально), по дате, внутри дня — «без времени» первыми, затем по времени. Пусто — «Пока ничего не запланировано.\nДобавьте визит к врачу, анализы или обследование — бот напомнит заранее.»
- Кнопки: по одной на событие «Скоро» — «{иконка} {title} · {ДД.ММ}» (`ap:open:{id}`); «➕ Добавить» (`ap:add`); «🗂 Прошедшие» (`ap:past`, если есть прошедшие); «В меню» (`menu:back`). В крупном режиме (`users.ui_large=1`) — пагинация по 5 событий, кнопка «Показать ещё {min(5, N − limit)}» (`ap:list:{limit}`, §8; UI13); обычный режим — до 30 событий одним списком, как сейчас.

**Прошедшие** (`ap:past`) — `status != 'planned'` или `starts_on < сегодня`, последние 20 по дате (новые сверху): «{иконка} {title} — {ДД.ММ.ГГГГ} · {статус}». Статус: `done` — «был(а) ✓» (у `lab` — «сдано ✓»); `moved` — «перенесено»; `missed` — «не был(а)» (у `lab` — «не сдано»); `cancelled` — «отменено»; `planned` ≤ 3 дней после даты — «ждёт отметки»; `planned` > 3 дней — «без отметки» (Н2). Больше 20 — последней строкой «Более ранние — в личном кабинете.» Кнопки: по одной на событие (`ap:open:{id}`), «Назад» (`ap:list`). В крупном режиме — пагинация по 5: «Показать ещё {min(5, N − limit)}» (`ap:past:{limit}`, `limit` — 5/10/15/20; UI13); обычный режим — как сейчас, до 20.

**Добавление (`ap:add`)** — одно редактируемое сообщение; черновик — FSM-data (`ap_kind`, `ap_title`, `ap_day`, `ap_time`, `ap_mode`, `ap_id`), FSM-состояние — только на шагах свободного ввода (допущение 4). На каждом шаге «Назад» — к предыдущему шагу, с шага 1 — к списку.
1. «Что добавить?» — «🩺 Визит к врачу» / «🧪 Анализы» / «📋 Обследование» (`ap:kind:{kind}`), «Назад». Лимит (В21): 30 событий «Скоро» — вместо шага 1 всплывающее «Запланировано уже 30 событий. Удалите ненужные, чтобы добавить новое.»
2. Вопрос шага 2 (таблица выше) — подсказки по две в строке (`ap:title:{kind}:{n}`, `n` — номер подсказки с 0), «Другой…» (`ap:title:other`), «Назад». «Другой…» — FSM `AppointmentInput.title`: «Напишите название — до 60 символов.»; длиннее — «Слишком длинно — до 60 символов. Напишите короче.»; пустое — повтор приглашения.
3. «Когда? Выберите день:» — календарь, как «Свой период» (§6.5), своя клавиатура; вид — по режиму пользователя (UI12, 0.5.10): обычный — сетка месяца, крупный — список недели. Сетка: строка «Ноябрь 2026» (заглушка), «Пн … Вс» (заглушки), недели месяца — дни до сегодня и после сегодня + 2 года — заглушки «·», остальные — числа (`ap:day:{ГГГГММДД}`); «◀» / «▶» (`ap:cal:m:{ГГГГММ}`; «◀» нет в текущем месяце, «▶» нет в месяце сегодня + 2 года; прежний `ap:cal:{ГГГГММ}` принимается как `:m:`-вариант); «Назад». Список недели (крупный режим): строка «◀ Неделя ДД.ММ–ДД.ММ ▶» (`ap:cal:w:{пн}`, `{пн}` — понедельник недели; «◀» нет, если вся неделя раньше сегодня, «▶» нет, если неделя дальше сегодня + 2 года), дни строками «Пн 3.11» (`ap:day:{ГГГГММДД}`, только дни в доступном диапазоне), «Назад». Проверка на сервере по `callback_data` (всплывающее, календарь остаётся): раньше сегодня — «Эта дата уже прошла»; позже сегодня + 2 года — «Не дальше чем на 2 года вперёд».
4. «Во сколько?» — 07:00…18:00 сеткой по 4 (`ap:time:{ЧЧММ}`), «Другое время» (`ap:time:other`), «Без времени» (`ap:time:none`), «Назад». Сегодня и время уже прошло — всплывающее «Это время уже прошло». «Другое время» — FSM `AppointmentInput.time`: «Напишите время, например 10:30.»; не разобрано — «Не понял время — напишите, например, 10:30.»; прошло (сегодня) — «Это время уже прошло — напишите другое.»
5. «Заметка (необязательно): адрес, кабинет, что взять с собой. Напишите её одним сообщением или нажмите «Пропустить».» — FSM `AppointmentInput.note`, кнопка «Пропустить» (`ap:note:skip`); длиннее 200 — «Слишком длинно — до 200 символов. Напишите короче.»
6. Сохранение (заметка или «Пропустить») — одной транзакцией, лимит 30 проверяется условной вставкой (`INSERT … SELECT … WHERE (SELECT COUNT(*) …) < 30`, гонка двух вкладок — §7.2); черновик очищается. Ответ:
```
Готово ✓
🩺 Кардиолог — пт 14.11, 10:30
   поликлиника №5, каб. 212
Напомню накануне в 20:00 и за 2 часа.
```
Кнопки «📅 К списку» (`ap:list`), «В меню». Двойное «Пропустить» — одна строка (черновик уже очищен — «Это сообщение устарело»).

**Строка расписания** (после сохранения, переноса, в карточке) — по уведомлениям, которые ещё впереди: накануне + за 2 часа — «Напомню накануне в 20:00 и за 2 часа.»; накануне + утром — «Напомню накануне в 20:00 и в день события в 07:00.»; только накануне — «Напомню накануне в 20:00.»; только за 2 часа — «Напомню за 2 часа.»; только утром — «Напомню в день события в 07:00.»; ни одного — «Событие скоро — напоминать не буду.» (В7).

**Уведомления (В5, В6, Н3, Н7).** Моменты — по локальной дате и времени события и `users.timezone`:

| Уведомление | Флаг | Когда (локально) | Есть, если |
|---|---|---|---|
| накануне | `sent_eve_at` | `starts_on − 1` в 20:00 | всегда |
| за 2 часа | `sent_soon_at` | `starts_at − 2 ч` | есть `starts_at` и момент не раньше 06:00 (Н3) |
| утром | `sent_morning_at` | `starts_on` в 07:00 | нет `starts_at` |
| «Как прошёл визит?» | `sent_after_at` | `starts_on` в 20:00; `starts_at` позже 18:00 — `starts_on + 1` в 10:00 | всегда |

- **Сохранение, перенос, правка даты/времени** — флаги пересчитываются: уведомления, чей момент уже прошёл, получают флаг = сейчас (обработано, не отправляется — В7); будущие — NULL.
- **Тик** (`reminders:tick`, §6.7, раз в минуту): выбирает `planned`-события с `starts_on` от вчера − 1 до завтра + 1 (UTC-дата, по индексу `(status, starts_on)`) и хотя бы одним NULL-флагом; для каждого считает моменты в поясе пользователя. Уведомление с моментом ≤ сейчас и NULL-флагом: момент старше `CATCHUP_WINDOW` (6 ч) — флаг = сейчас без отправки; «за 2 часа», если событие уже началось, — так же; иначе атомарный захват `UPDATE appointments SET sent_*_at = :now WHERE id = :id AND sent_*_at IS NULL AND status = 'planned'` и отправка только при `rowcount = 1` (двойной тик, два процесса — одна отправка). Сессия закрывается до отправки (§7.2).
- **Без действующего согласия** (§6.11) — флаг = сейчас без отправки. **Пауза** не действует (В8).
- Новое уведомление события снимает кнопки у прежнего (`last_message_id`, ошибки Telegram игнорируются — как у слотов).

**Тексты уведомлений:**

| | `doctor` | `lab` | `exam` |
|---|---|---|---|
| накануне | «Завтра визит 🩺» | «Завтра анализы 🧪» | «Завтра обследование 📋» |
| за 2 часа | «Через 2 часа визит 🩺» | «Через 2 часа анализы 🧪» | «Через 2 часа обследование 📋» |
| утром | «Сегодня визит 🩺» | «Сегодня анализы 🧪» | «Сегодня обследование 📋» |

Следующей строкой — событие в формате выше без иконки («Кардиолог — пт 14.11, 10:30»), затем заметка. Кнопки (В9; раскладка — 0.5.10, UI3): «✓ Помню» (`ap:seen:{id}` — снять кнопки, ничего не менять) / «Перенести» (`ap:move:{id}` — шаги 3–4, затем строка расписания) — пара в ряд; «Отменить визит» (у `lab` и `exam` — «Отменить»; `ap:cancel:{id}`) — у `lab` и `exam` своей строкой, у `doctor` — парой в ряд с «📄 Отчёт для врача» (`ap:report:{id}`); если есть врач с действующим доступом (§6.11, три условия) — ещё «Отправить врачу» (`ap:send:{id}`, сценарий «Отправить отчет», §21) своей строкой. В крупном режиме (`users.ui_large=1`) все кнопки — по одной в строке (UI10).

- **Отчёт для врача (В12)** — PDF за последние 30 дней (генератор §10.1, без выбора периода) отдельным документом, подпись «Отчёт по давлению за ДД.ММ–ДД.ММ»; уведомление не меняется. Это **пациентский** PDF (§10.1): включает раздел «Заключения врачей» — как и «Экспорт PDF», несмотря на название кнопки. Нет записей — всплывающее «За этот период записей нет». Двойное нажатие — одна выгрузка.
- **Отмена** — «Отменить: Кардиолог — пт 14.11, 10:30?» — «Да, отменить» (`ap:cancel:{id}:ok`) / «Нет» (карточка). «Да» — `status='cancelled'` (`UPDATE … WHERE status='planned'`), ответ «Отменено: Кардиолог — 14.11.» и «📅 К списку» / «В меню».

**«Как прошёл визит?» (В10):**

| `doctor` | `lab` | `exam` |
|---|---|---|
| «Как прошёл визит? 🩺» | «Сдали анализы? 🧪» | «Как прошло обследование? 📋» |
| «✓ Был(а)» / «Перенёс(ла)» / «Не был(а)» | «✓ Сдал(а)» / «Перенёс(ла)» / «Не сдал(а)» | «✓ Был(а)» / «Перенёс(ла)» / «Не был(а)» |

Второй строкой — событие («Кардиолог — пт 14.11, 10:30»). Callback'и — `ap:after:{id}:{done|moved|missed}`. Ответ принимается, пока `status='planned'` и не прошло 3 дня после `starts_on`; иначе — «Это сообщение устарело». Окно 3 дня — **только** для этих кнопок уведомления; карточка с дня события отмечает без ограничения окна (`ap:mark:{id}:{…}`, 0.5.11, В26-А, У1-А). Двойное нажатие — `UPDATE … WHERE status='planned'`, второе — без изменений.
- **Был(а) / Сдал(а)** → `done`; ответ «Отметил ✓\nЗаписать следующий?» — «Через месяц» / «Через 3 мес» / «Через полгода» / «Через год» (`ap:next:{id}:{1|3|6|12}`) / «Не нужно» (`ap:next:{id}:0`); последней строкой экрана — «{Подпись заключения}» (`ap:conc:{id}:add`, 0.5.11, Р8): **сразу FSM ввода** нового заключения, не список (в карточке кнопка ведёт в список — `ap:conc:{id}`). Следующее (В11): новая строка `planned` — те же `kind`, `title`, `note`, `starts_at`; дата — `starts_on` + N месяцев (несуществующее число — последний день месяца: 31.01 + 1 мес → 28.02 / 29.02), выходные не сдвигаются; ответ — как после добавления. Лимит 30 — как при добавлении. «Не нужно» — «Хорошо.» и «📅 К списку» / «В меню». Двойное или повторное `ap:next` — одно новое событие: вставка условная — нет `planned`-события пользователя с тем же `kind`, `title` и вычисленной датой; иначе «Это сообщение устарело». `ap:next` принимается только у события со `status='done'`.
- **Перенёс(ла)** (Н1) — шаги 3–4 (`ap_mode=moved`); при сохранении одной транзакцией: прежняя строка → `moved` (`WHERE status='planned'`), новая `planned` (те же `kind`, `title`, `note`; дата и время — выбранные). Шаги отменены («Назад» с шага 3) — прежняя строка не меняется, вопрос можно ответить снова.
- **Не был(а) / Не сдал(а)** → `missed`; ответ «Понятно, отметил.» и «📅 К списку». Кнопка заключения на этом экране не показывается (в т.ч. при уже добавленных заключениях) — они остаются доступны в карточке события.

**Карточка** (`ap:open:{id}`, чужой или удалённый id — «Это сообщение устарело», §7.1):
```
🩺 Кардиолог
пт 14.11.2026, 10:30
поликлиника №5, каб. 212
Напомню накануне в 20:00 и за 2 часа.
```
Последняя строка: `planned` и дата не прошла — строка расписания; иначе — «Статус: {статус из «Прошедших»}». В тексте карточки — строка заключения (0.5.11): «📝 Заключение: «первые 60 символов…»» (одно) или «📝 Заключений: N» (несколько) — короткое превью **без типа**; кнопка — с типом (В27). Без заключений строки нет. Кнопки (0.5.11, В25-Б, В26-А, У1-А):
- `planned`, **дата не наступила** (бывш. `upcoming` до дня события) — как прежде: у `doctor` «📄 Отчёт для врача» (`ap:report:{id}`), «✏️ Изменить» (`ap:edit:{id}`), «Отменить визит» / «Отменить» (`ap:cancel:{id}`), «🗑 Удалить» (`ap:del:{id}`), «Назад». Кнопки заключения нет: до события заключать нечего (Р3).
- `planned` **с дня события включительно** (бывш. `upcoming` в день события, `waiting` и `unmarked` — одна ветка, Р2) — сверху три статусные «✓ Был(а)» / «Перенёс(ла)» / «Не был(а)» (`ap:mark:{id}:{done|moved|missed}`); подписи по типам — как у «Как прошёл визит?»: `doctor`, `exam` — «✓ Был(а)» / «Перенёс(ла)» / «Не был(а)»; `lab` — «✓ Сдал(а)» / «Перенёс(ла)» / «Не сдал(а)» (все три); раскладка — как `ap_after_kb` (UI3, Р1): «✓ Был(а)» отдельно, пара «Перенёс(ла)»/«Не был(а)»; крупный режим — по одной. Окно — без ограничения, включая «без отметки» (позже 3 дней). В день события ниже — прежние кнопки («📄 Отчёт для врача», «✏️ Изменить», «Отменить визит» / «Отменить», «🗑 Удалить», «Назад») и «{Подпись заключения}» (`ap:conc:{id}` — список заключений); «📄 Отчёт для врача» и «{Подпись заключения}» — **по строкам**, не парой (Р4); весь расклад карточки — по канону §11 (крупный режим — пары разъезжаются, по одной). После прошедшей даты (та же ветка «с дня события», бывш. `waiting` / `unmarked`) — «{Подпись заключения}», «🗑 Удалить», «Назад».
- прочее (не `planned`) — `done` и `moved`: «{Подпись заключения}», «🗑 Удалить», «Назад»; `missed` и `cancelled`: «🗑 Удалить», «Назад» — кнопки заключения нет (событие не состоялось; решение по ревью 13.11.2026, п. 5 — вариант Б), **но если заключения уже есть — кнопка показывается**, чтобы их прочитать и исправить.
Статусные кнопки — тот же поток, что «Как прошёл визит?» (В10, Н1, В11), всегда **редактированием карточки**: `done` → карточка становится «Отметил ✓» + «Записать следующий?»; `moved` → карточка редактируется в шаги 3–4 (выбор даты/времени; прежняя строка → `moved`, новая `planned`); `missed` → карточка становится «Понятно, отметил.». Новое сообщение не отправляется ни в одной ветке. Ранняя отметка гасит «Как прошёл визит?»: тик шлёт только `planned` (§6.13, уведомления). Нажатие статусной кнопки **редактирует карточку** (`edit_message_text`, §1: «редактировать, а не отправлять новые»), новое сообщение не отправляется; повторное нажатие после отметки (`rowcount = 0`) — «Это сообщение устарело» (U8). Сообщения с `ap:after` (уведомления и `waiting`-карточки, отправленные до 0.5.11) продолжают работать по прежним правилам — окно 3 дня. «Назад» — к списку, в котором событие сейчас: «Скоро» (`ap:list`) или «Прошедшие» (`ap:past`), по правилам выше.
- **Изменить (Н4)** — «Что изменить?»: «📅 Дата и время» (`ap:edit:{id}:when` — шаги 3–4, та же строка, флаги пересчитываются), «✏️ Название» (`ap:edit:{id}:title` — шаг 2 этого типа), «📝 Заметка» (`ap:edit:{id}:note` — шаг 5; у заполненной — ещё «Убрать заметку», `ap:edit:{id}:note:clear`), «Назад». Тип не меняется. Название и заметка уведомления не пересчитывают.
- **Удалить** — «Удалить «Кардиолог — 14.11»? Это нельзя отменить.» — «Да, удалить» (`ap:del:{id}:ok`) / «Нет»; ответ «Удалено.» и список. Удаление уведомлений не требует: тик работает по строкам.

**Заключения (0.5.11, В27–В31, В36, У3-Б; таблица — §4.17).** Текст заключения пользователь пишет сам — распознавания фото нет; бот текст не интерпретирует и не комментирует (§1, non-goals). Подписи (В27): `doctor` — «📝 Заключение врача», `lab` — «📝 Результаты анализов», `exam` — «📝 Протокол обследования». Кнопка и экран доступны **с дня события** — в боте и в ЛК (Р3): до события заключения добавить нельзя.

**Экран заключений** (`ap:conc:{id}`, одно редактируемое сообщение, канон §11):
```
📝 Заключение врача — Кардиолог 14.11

1. «Рекомендовано продолжить…»
2. «Контроль АД дважды в день…»

[➕ Добавить]  [Назад]
```
Список — превью каждой записи «N. «первые 60 символов…»» (все до 5 сразу, без пагинации); пусто — «Пока пусто. Напишите текст заключения одним сообщением — до 4000 символов.». Кнопки записи: «✏️ Текст» (`ap:conc:{id}:{cid}:text`), «📎 Фото» (`ap:conc:{id}:{cid}:photo`), «🗑 Удалить» (`ap:conc:{id}:{cid}:del`); внизу — «➕ Добавить» (`ap:conc:{id}:add`), «Назад» (`ap:open:{id}`). Раскладка — UI-канон (пары §11, крупный режим — по одной, UI10).

- **Добавление** (`ap:conc:{id}:add`; callback несёт `id` события — не «последнее событие») — FSM `AppointmentConclusion.text` (В29-А, без подтверждения): «Напишите текст заключения одним сообщением (до 4000 символов).»; пустое — только пробелы и переводы строк (после `trim()`) — повтор приглашения; длиннее 4000 — «Слишком длинно — до 4000 символов. Напишите короче.». Сохранение — «Сохранил ✓» + текст + экран заключений. Лимит 5 — общий для бота и ЛК (§4.17, §16.2) — условной вставкой (§7.2); переполнение — всплывающее «Заключений уже 5. Удалите ненужные, чтобы добавить новое.».
- **Правка текста** (`ap:conc:{id}:{cid}:text`) — тот же FSM-ввод, замена текста; ответ — экран заключений.
- **Фото** (В28-Б) — кнопка всегда «📎 Фото» (`ap:conc:{id}:{cid}:photo`, **один callback с ветвлением** по `photo_file_id`). Экран заключений живёт в одном текстовом сообщении (редактируется); фото-сообщение — временное и **удаляется** после любого действия:
  - фото нет — то же сообщение редактируется в приглашение, FSM `AppointmentConclusion.photo` (черновики `ap_conc_id`, `ap_conc_cid`; кнопка «Отмена» — `ap:conc:{id}`, назад к списку, как все FSM-состояния, §8): «Пришлите фото заключения одним сообщением.»; принимается только `message.photo` — файл-документ или текст: «Пришлите фото одним сообщением.»; приём → `photo_file_id` (одно фото на заключение), ответ — список заключений (редактированием того же сообщения);
  - фото есть — показ **новым сообщением**: фото (`file_id`) + подпись «{Подпись заключения} — {title} {ДД.ММ}» и кнопки «🔄 Заменить» (`…:photo:put` — тот же FSM; при приёме прежний `photo_file_id` затирается, фото-сообщение удаляется, ответ — список заключений), «Убрать фото» (`…:photo:rm` — `photo_file_id = NULL`, фото-сообщение удаляется, ответ — список заключений), «Назад» (фото-сообщение удаляется, список под ним не меняется).
  Фото — только `file_id`: в ЛК и PDF не попадает (§10.1, §16.2).
- **Удаление** (`ap:conc:{id}:{cid}:del`) — как все удаления в боте (канон бота): «Удалить заключение «первые 60 символов…»? Это нельзя отменить.» — «Да, удалить» (`…:del:ok`) / «Нет»; ответ — экран заключений. Двойное нажатие — идемпотентно.
- На экране «Отметил ✓» («Записать следующий?») — кнопка «{Подпись заключения}» (`ap:conc:{id}:add`) **последней строкой, после «Не нужно»** (Р8): сразу FSM ввода нового заключения, не список (как в блоке «Был(а) / Сдал(а)»; из карточки кнопка ведёт в список — `ap:conc:{id}`).
- Изоляция (§7): `id` события и `id` заключения из `callback_data` — проверка на владельца; чужой или удалённый — «Это сообщение устарело» (U8).

**Ограничения (В21):** 30 событий «Скоро» на пользователя; `title` 1–60, `note` ≤ 200 символов; дата — от сегодня до сегодня + 2 года (локально). Те же проверки — в ЛК (§16.2).

**Изоляция и конкурентность (§7):** каждый `id` из `callback_data` проверяется на владельца (`user_id` текущего пользователя); статусы меняются условным `UPDATE … WHERE status='planned'`; захват уведомления — по NULL-флагу; лимит — условной вставкой. Врач визиты не видит (В19, §21); админка — только `COUNT(*)` (§18.2).

### 6.14 «➕ Добавить запись» — типы записей дневника (0.6.0)

Кнопка главного меню «➕ Добавить запись» (`add:menu`) — экран выбора типа (кнопки по одной в строке в обоих режимах, канон §11; выход — «В меню», M5):
«Давление и пульс» (`add:bp`) · «Приём лекарства» (`add:med`) · «Прогулка / активность» (`add:walk`) · «Питание (соль / кофе / алкоголь)» (`add:food`) · «Стресс / самочувствие» (`add:stress`) · «Сон» (`add:sleep`) · «Заметка / другое» (`add:note`).

- **Давление и пульс** — сразу калькулятор §6.3.
- **Приём лекарства** — фасад над `medication_logs` (§6.7).
- **Прогулка / активность** — два шага: длительность «10 минут» / «20 минут» / «30 минут» / «60 минут» → время «Сейчас» / «Другое» → событие `activity` (R17 = Б).
- **Питание** — **один экран** (R18 = А, K1): чекбоксы «Солёное» / «Кофе» / «Алкоголь» (1–3 метки) + ряд времени (radio «🕓 Сейчас» / «🕓 Другое время», по умолчанию «Сейчас») + «Готово» / «Отмена»; «Готово» без единой метки — тост «Отметьте, что выбрали», экран не меняется. Тап «Другое время» — FSM-ввод `ЧЧ:ММ` без сохранения; «Готово» сохраняет: «Сейчас» — текущим моментом, «Другое время» — введённым; «Готово» при «Другое время» без введённого времени — тост «Введите время». «Отмена» на вводе (`add:food:time:other`) — возврат на экран питания с выбранными метками и radio «Сейчас». Одно событие `food` с набором меток. В режиме правки radio времени — «🕓 Оставить {момент}» / «🕓 Другое время» (V9, «Сейчас» не используется).
- **Стресс / самочувствие** — вариант «Стресс» / «Тревога» / «Головная боль» / «Слабость» / «Головокружение» / «Другое» → короткий текст (текст — в `note`; для вариантов кроме «Другое» — необязательный, кнопка «Пропустить», M6; для «Другое» — обязателен, кнопки «Пропустить» нет; пустой → «Напишите, что чувствуете» и повтор ввода) → время «Сейчас» / «Другое» → событие `stress`.
- **Сон** — оценка «Мало спал» / «Нормально» / «Много» → событие `sleep` без времени; не более одной оценки на локальные сутки (повторный ввод тех же суток обновляет существующее событие, C23).
- **Заметка / другое** — свободный текст ≤ 500 (пустой — «Заметка не может быть пустой», §9, и повтор ввода) → время «Сейчас» / «Другое» → событие `note`.

Шаг времени — «Сейчас» / «Другое»: `ЧЧ:ММ` в пределах последних 24 часов (C13), будущее — «Время измерения не может быть в будущем» (§9); шаг есть у `activity` / `food` / `stress` / `note` и нет у `sleep` (C22); «Отмена» на вводе времени — возврат на экран типа с уже выбранными значениями (M2). На каждом шаге флоу типа есть «Отмена»: с первого шага — в «➕ Добавить запись», с последующих — на предыдущий шаг; из FSM-ввода — на экран, откуда ввод вызван. «Отмена» с шага времени — на предыдущий шаг с сохранением введённых значений (текст, длительность, метки остаются в FSM-данных). Сохранение события — «✅ Записано» + кнопки «✏️ Изменить» · «🗑 Удалить» (с подтверждением) · «➕ Добавить ещё» (новый проход того же типа) · «В меню» (C3 = А); правка — перезапуск флоу типа с текущими значениями, сохранение по id. Момент события — «сейчас» или уточнение в пределах последних 24 часов; архив (задним числом глубже) для событий не предусмотрен (§6.10). В кабинет, CSV и PDF события не попадают (известные ограничения 0.6.0, §15).

## 7. Многопользовательность и конкурентность

### 7.1 Изоляция пользователей

- Каждый пользователь — ровно одна строка `users` (ключ `telegram_id`, UNIQUE; создание идемпотентно — `INSERT ... ON CONFLICT DO NOTHING` / get-or-create в транзакции).
- **Все** запросы к `measurements`, `reminders`, `medications`, `medication_logs` строго по `user_id` текущего пользователя. Никаких запросов «без фильтра».
- `callback_data` не содержит чужих идентификаторов; если в ней есть id записи — хендлер обязан проверить владельца (`WHERE id=? AND user_id=?`) и на чужую запись ответить мягко, не раскрывая её существования.
- Сессия БД создаётся middleware на каждый апдейт и передаётся только в контекст этого апдейта.

### 7.2 Защита от гонок (максимальная)

Архитектура: один процесс, asyncio; «многопоточность» означает конкурентную обработку апдейтов многих пользователей (и возможный thread pool для блокирующих операций). Правила:

1. **Сессия на операцию.** SQLAlchemy-сессия живёт в рамках одного апдейта (или одной задачи планировщика). Сессии не кэшируются в глобальных переменных и не передаются между задачами/потоками. Один поток — одна сессия.
2. **Короткие транзакции.** Запись данных — в одной транзакции (BEGIN…COMMIT), чтение — без удержания открытой транзакции дольше нужного. Ничего «человеческого» (отправка сообщений в Telegram) внутри транзакции не делается: сначала фиксируем состояние, потом отвечаем пользователю.
   Asyncio-лок (дедуп сохранений, отметки приёмов, повторы замера) **не ждут, держа незакоммиченную запись SQLite**: держатель лока ждёт запись SQLite, держатель записи ждёт лок — взаимоблокировка до `busy_timeout` и `database is locked` у соседних пользователей. Операция под локом берёт его один раз, а запись, сделанная до лока, сначала коммитится.
3. **SQLite настроен на конкурентный доступ.** `journal_mode=WAL` (читатели не блокируют писателя), `busy_timeout=5000`, `foreign_keys=ON`. На `OperationalError: database is locked` — повтор с экспоненциальной задержкой (до 3 попыток), затем понятная ошибка пользователю.
4. **Идемпотентность и двойные нажатия.** Telegram может доставить callback повторно, пользователь может жать быстро. Меры:
   - DebounceMiddleware: 1.5 с на callback-кнопки флоу записи давления; повторный тап той же кнопки игнорируется, ответ «Уже записываю…» (плюс общий throttle — не более одного действия в секунду на остальные callback'и). Правка буфера калькулятора (цифры, «⌫», «C») и чекбоксы мультивыбора лекарств из-под дебаунса выведены — экран редактируется на каждое нажатие (§6.3), двойное нажатие здесь ничего не дублирует;
   - сценарий записи давления защищён от параллельного запуска дважды (один «черновик» измерения на пользователя; повторное «Записать давление» перезаписывает черновик, а не плодит второй);
   - сохранение измерения идемпотентно на уровне сервиса: дедуп по `user_id` + `timestamp` (округление до минуты) → «Это давление уже записано»; повторный вызов с тем же черновиком дубль не создаёт;
   - опциональные шаги (заметка/лекарство/самочувствие) привязаны к id только что сохранённой записи, а не к «последней записи пользователя» (защита от гонки «два измерения подряд»);
   - `med_remeasure` создаётся, только если для этой `measurement_id` ещё нет активного повтора (двойное нажатие «Да» или «Принял всё ✓» не плодит job'ы); отметка приёма лекарств идемпотентна — повторное действие не создаёт второго набора `medication_logs` за тот же слот/момент;
   - правка значений записи идемпотентна: повторное и конкурентное сохранение правки — UPDATE одной и той же строки (`timestamp` не меняется). Систолическое, диастолическое и пульс пишутся **одним явным `UPDATE` всех трёх полей**, а не через атрибуты прочитанного ORM-объекта: flush пишет только поля, отличающиеся от прочитанных, и две параллельные правки дали бы смесь наборов (найдено нагрузкой, §22). Так же — перенастройка существующего слота напоминания (`local_time`, `next_trigger_time`, `enabled`; `reminders.enable_slot_row`, и в боте, и в ЛК).
   - **Известные ограничения** (0.4.4, ревизия нагрузки — протокол V4/V5): прочие многопольные правки одной строки идут через атрибуты ORM-объекта, и две одновременные правки одной строки могут смешать поля — правка записи в ЛК (`medication_taken`, `feeling`, `note`), профиль в ЛК (цели, часовой пояс), лекарство в ЛК (`name`, `dosage`, `slot`) и переименование в боте (`name`, `dosage`), финал онбординга; «Самочувствие → Другое» дописывает `note` чтением-записью (из двух одновременных текстов останется один). Сценарий требует двух одновременных правок одной строки одним пользователем; на поведение бота не влияет.
5. **Напоминания — атомарная доставка.** Перепланировка слота — одной транзакцией (`UPDATE reminders SET next_trigger_time=… WHERE id=? AND next_trigger_time=?` с проверкой затронутых строк — optimistic locking); в APScheduler — `max_instances=1`, `coalesce=True`. Двойной тик или два конкурентных обработчика не отправят одно напоминание дважды. UNIQUE(user_id, type) страхует на уровне БД.
6. **Никакого разделяемого мутабельного состояния.** Конфиг неизменяем после загрузки; черновик измерения — только в FSM-storage с ключом по пользователю (не в БД, не в глобальных dict); статистика считается на лету из БД, без кэшей (MVP). `logging` из stdlib — потокобезопасен.
7. **Блокирующий код — в thread pool.** Если операция блокирует цикл (генерация большого CSV, PDF-отчёт врача, хеширование и проверка пароля Argon2id, тяжёлый I/O) — `asyncio.to_thread`; сессии и объекты ORM через потоки не таскаются, в поток передаются данные (id/значения).
8. **Crash-safety.** Данные фиксируются в SQLite до ответа пользователю; падение процесса не теряет подтверждённые записи. Запуск второго экземпляра бота с тем же `DB_PATH` допустим (WAL), но по умолчанию работает один экземпляр long polling.

## 8. FSM и callback_data

- Всё, что выбирается кнопками, — `callback_data` без FSM. Формат: `<область>:<действие>[:<значение>]`, напр. `m:key:1`, `menu:stats`, `stats:7`, `set:tz:Asia/Yekaterinburg`. Укладываться в лимит 64 байт. Клавиатура-калькулятор: `m:key:0`…`m:key:9` (цифра), `m:key:bksp` («⌫»), `m:key:clear` («C»), `m:key:ok` («Готово»), `m:cancel` («Отмена»); в 0.6.0 кнопки «Пропустить» нет (R7 = А), `m:key:skip` — только legacy (см. ниже).
- FSM (Aiogram FSM) — только для одношагового свободного ввода и коротких цепочек: экран калькулятора (одно состояние `MeasureInput.active` + данные `m_field`/`m_buf`, §6.3, K3; подтверждённые значения полей — в существующих ключах `m_sys`/`m_dia`/`m_pulse`, они не заменяются; состояния `sys_input`/`dia_input`/`pulse_input` удалены в 0.6.0 — V1), заметка, «Другое» (самочувствие), HH:MM, IANA-таймзона, добавление лекарства (название → дозировка «Другое» → слоты). Каждое состояние — с кнопкой «Отмена», сбрасывающей FSM; команды `/start`, `/support` и `/cabinet` тоже сбрасывают FSM. Ключ FSM — по пользователю (изолировано между пользователями). **Черновик измерения хранится в FSM-storage, не в БД**: живёт минуты, персистентность не нужна, при рестарте очищается автоматически.
- `CabinetRegistration.email` (ожидание ввода e-mail для создания личного кабинета).
- Врач (§21): FSM `DoctorAccess.email` (e-mail врача), `DoctorAccess.username` (@username врача), `DoctorRegistration.name` (ФИО в `/iamdoctor`). Callback'и пациента: `set:doctor` («Мой врач»), `set:doctor:email`, `set:doctor:tg`, `set:doctor:tg:find`, `set:doctor:cancel`, `set:doctor:revoke:{doctor_id}`. Callback'и врача: `doc:accept:{code}` / `doc:decline:{code}` (22 символа кода; проверка: код не списан, не просрочен), `doc:cabinet`, `doc:patients`, `doc:patients:{limit}` («Показать ещё …», крупный режим), `doc:open:{user_id}` (только пациенты с `status='active'` этого врача), `doc:diary` («Вести свой дневник давления»), `doc:name` (смена ФИО), `doc:menu` (меню врача: кнопка «🩺 Кабинет врача» главного меню и «Назад» в меню врача; не врачу — главное меню). Приглашение в бот — параметр `/start doc_<code>`.
- `m:edit` — вход в редактирование последней записи (без id в `callback_data`: «последняя» определяется на сервере, §6.3). Числовой флоу правки — те же шаги калькулятора (`m:key:*`) и те же FSM-ключи черновика `m_sys`/`m_dia`/`m_pulse` (плюс буфер `m_buf`); режим правки определяется ключом `m_edit_id` (id редактируемой записи), финальное сохранение по нему делает UPDATE вместо INSERT. Выбор лекарств в «Приняли лекарство?»: `m:med:pick:{id}` (переключение «✓», id строки лекарства, проверка владельца), `m:med:done` («Готово»), `m:med:cancel` («Отмена» — назад к сводке).
- `med:list:{limit}` — «Мои лекарства» с пагинацией («Показать ещё …», крупный режим; без `:{limit}` — первый экран, как сейчас). `med:edit:{id}` / `med:del:{id}` — выбор лекарства из списка в изменении/удалении (§6.6), `med:add` («Добавить» лекарство, §6.6; используется также в подсказке «Напоминать, какие лекарства принимать?», §6.3, §6.7); `id` — любой строки лекарства (лекарство — группа строк по названию, §4.4). Шаги правки — те же callback'и `med:dose:`/`med:slot:` и те же FSM-ключи черновика `med_name`/`med_dosage`/`med_slots`; режим правки определяется ключом `med_edit_name` (исходное название группы; канон `m_edit_id`), финальное сохранение по нему делает UPDATE переживающих строк вместо пересоздания (§6.6).
- Архивный ввод (§6.10): `set:archive` (экран «Архивные записи»), `set:archive:add` (по одной записи), `set:archive:quick` (быстрый ввод), `set:archive:date:{today|yesterday|3d|week|other}`, `set:archive:time:{preset|other}`, `set:archive:more` («➕ Ещё запись»). FSM-состояния: `ArchiveInput.date` (`ДД.ММ[.ГГГГ]`), `ArchiveInput.time` (`ЧЧ:ММ`), `ArchiveInput.quick` (строка быстрого ввода). Числовой поток — те же шаги калькулятора (`m:key:*`) и те же FSM-ключи `m_sys`/`m_dia`/`m_pulse`/`m_buf`; режим архива определяется ключом `m_arch_ts` (момент измерения в UTC, ISO 8601; канон `m_edit_id`) — финальное сохранение делает INSERT с этим `timestamp`. Опциональные шаги сводки архива — `m:arch:note`, `m:arch:med:{yes|no}`, `m:arch:feel`, `m:arch:edit`, `m:arch:more`, `m:arch:menu` (те же экраны, что в §6.3, но «Приняли лекарство?» не планирует повторный замер).
- Согласие (§6.11): `cons:yes`, `cons:no`, `cons:back`, `cons:doc:keep:{doctor_id}`, `cons:doc:close:{doctor_id}` (проверка: связь этого пациента), `set:consent`, `set:consent:withdraw`, `set:consent:withdraw:ok`, `set:doctor:agree`; кабинет: POST `/consent`, `/consent/withdraw`, `/doctor/consent`; врач — `doc:consent:yes`, `doc:consent:yes:{code}`, `doc:consent:no`. Без FSM.
- Данные пациента (§6.12): `set:profile` (экран «Мои данные»), `prof:edit`, `prof:show`, `prof:nomiddle`, `prof:sex:{m|f}`, `prof:del`, `prof:del:ok`, `prof:later`. FSM `PatientProfile.last_name` / `first_name` / `middle_name` / `birth_date`, черновик — ключи `pf_last`, `pf_first`, `pf_middle`, `pf_birth`; ключ `pf_then_invite` — после сохранения вернуться к приглашению врача.
- Статистика, свой период и средние по дням (§6.5, 0.5.3; календарь и пагинация — 0.5.10, UI12–UI13), без FSM; даты — `ГГГГММДД`, месяц — `ГГГГММ`: `stats:cal` (кнопка «📆 Свой период»), `stats:cal:m:{ГГГГММ}` (сетка месяца, шаг 1), `stats:cal:w:{пн}` (список недели, шаг 1; `{пн}` — понедельник недели), `stats:cal:m:{ГГГГММ}:{с}` и `stats:cal:w:{пн}:{с}` (шаг 2, первый день выбран), `stats:day:{ГГГГММДД}` (выбор первого дня), `stats:day:{с}:{ГГГГММДД}` (выбор последнего дня → сводка), `stats:c:{с}:{по}` (сводка своего периода — «Назад» из «По дням»), `stats:days:{7|30}` и `stats:days:{с}:{по}` («📅 По дням»), `stats:days:{7|30}:{limit}` и `stats:days:{с}:{по}:{limit}` («Показать ещё …», крупный режим), `stats:noop` (заглушки календаря — только `callback.answer()`). Самая длинная — `stats:days:20261101:20261115:10` (31 байт). Совместимость: прежние `stats:cal:{ГГГГММ}` и `stats:cal:{ГГГГММ}:{с}` (до 0.5.10) принимаются как `:m:`-вариант (канон `rem:write` без слота). Даты из `callback_data` проверяются заново (формат, будущее, длина ≤ 31 дня); данные — только текущего пользователя (§7.1).
- Напоминания (§6.7, 0.5.4), без FSM; `{д}` — дата напоминания `ГГГГММДД`: `rem:write:{slot}:{д}` («Записать давление» — калькулятор §6.3, запись — текущим моментом; слот и дата — чтобы нажатие считалось ответом для повтора, 0.5.6; прежний `rem:write` без слота принимается), `rem:all:{slot}:{д}`, `rem:partial:{slot}:{д}`, `rem:snooze:{slot}:{д}`, `rem:skip:{slot}:{д}`; список «Частично…» — `rem:pt:{slot}:{д}:{id}`, `rem:pd:{slot}:{д}`, `rem:pc:{slot}:{д}`; повтор замера — `rem:now`, `rem:snooze:{id}`, `rem:off:{id}`. Самая длинная — `rem:pt:morning:20261110:123456` (30 байт). Callback'и 0.5.3 без `{д}` принимаются (дата — день сообщения); `rem:med:*`, `rem:partial:done` / `rem:partial:cancel` старых списков — «Это напоминание устарело». Чужой id лекарства (не из слота пользователя) — то же уведомление (§7.1).
- Лекарства на сегодня в дневнике (§6.4, 0.5.5), без FSM: `d:mt` («💊 Отметить приём» — выбор слота или сразу список), `d:mt:{slot}` (список слота), `d:pt:{slot}:{д}:{id}`, `d:pd:{slot}:{д}`, `d:pc:{slot}:{д}` — как `rem:pt/pd/pc`, но «Готово» и «Отмена» возвращают «Дневник за сегодня».
- **Устаревшие кнопки (0.5.5, U8).** Последний обработчик цепочки (`fallback_router` в `bot/handlers/__init__.py`) принимает любой callback, который не принял никто другой, и отвечает уведомлением `callback.answer("Это сообщение устарело")` — без нового сообщения; «часики» на кнопке не висят. Аудит «тихих» ответов в обработчиках (`await callback.answer()` без текста на неверных данных):
  - **→ «Это сообщение устарело»:** `settings.cb_slot_toggle` и `settings.cb_slot_time` (неизвестный слот), `settings.cb_med_slot` (неизвестный слот лекарства), `measure.cb_archive_date` (неизвестный пресет даты);
  - **остаются как есть** — у них уже есть свой экран или текст: `settings.cb_med_del_pick` (лекарство уже удалено — обновлённый список), калькулятор и мультивыбор записи без черновика (возврат в меню, §6.3), приглашения врача («Приглашение устарело…», §21), чужая запись («NOT_YOUR_RECORD»), статистика без записей, повторные «Готово» (идемпотентны, §7.2), кнопки напоминаний («Это напоминание устарело», §6.7). Места аудита проверяются в `tests/test_handlers_fallback.py`;
  - **остаются тихими** — это не устаревшие данные, а обычный ход сценария: переход к свободному вводу («Другое» в архиве, времени, онбординге), ветки админки (§18).
- Удобство напоминаний, часть 2 (§6.7, 0.5.6), без FSM: `set:rem:mode:{slot}` (экран режима), `set:rem:mode:{slot}:{both|meds|pressure}` (выбор), `set:rem:pause` (экран паузы), `set:rem:pause:{1|3|7}`, `set:rem:pause:off`, `set:rem:followup:{on|off}` («Повторять, если не ответил»). Неизвестный слот или значение — «Это сообщение устарело» (U8).
- Режим отображения (§6.1 шаг 2, §6.6; 0.5.10, UI6–UI8), без FSM: `onb:ui:{0|1|skip}` (шаг онбординга «Как вам удобнее смотреть?»; `skip` = обычный), `set:ui:{0|1}` (переключатель «👁 Крупный режим» в «Настройках»). Нажатие переключает `users.ui_large` и перерисовывает текущий экран; данные, валидация и все callback'и не зависят от режима (крупный режим — только рендер, канон §11).
- Визиты и анализы (§6.13, 0.5.8; календарь и пагинация — 0.5.10, UI12–UI13): `ap:list`, `ap:list:{limit}`, `ap:past`, `ap:past:{limit}` (`limit` — 5/10/15/20, «Показать ещё …», крупный режим), `ap:add`, `ap:kind:{doctor|lab|exam}`, `ap:title:{kind}:{n}`, `ap:title:other`, `ap:cal:m:{ГГГГММ}` / `ap:cal:w:{пн}` (сетка месяца / список недели; совместимость: прежний `ap:cal:{ГГГГММ}` принимается как `:m:`-вариант), `ap:day:{ГГГГММДД}`, `ap:time:{ЧЧММ}`, `ap:time:none`, `ap:time:other`, `ap:note:skip`, `ap:open:{id}`, `ap:edit:{id}`, `ap:edit:{id}:{when|title|note}`, `ap:edit:{id}:note:clear`, `ap:del:{id}`, `ap:del:{id}:ok`, `ap:seen:{id}`, `ap:move:{id}`, `ap:cancel:{id}`, `ap:cancel:{id}:ok`, `ap:after:{id}:{done|moved|missed}`, `ap:next:{id}:{0|1|3|6|12}`, `ap:report:{id}`, `ap:send:{id}` («Отправить врачу» — сценарий «Отправить отчет» §21 новым сообщением, уведомление остаётся), `ap:back` («Назад» по шагам черновика; из свободного ввода — к кнопкам того же шага). FSM `AppointmentInput.title` / `time` / `note`; черновик — ключи `ap_kind`, `ap_title`, `ap_day`, `ap_time`, `ap_mode` (`add` / `edit` / `move` / `moved`), `ap_id`. Самая длинная — `ap:edit:123456:note:clear` (25 байт). Чужой или удалённый `id`, неизвестный тип, номер подсказки, дата, время — «Это сообщение устарело» (§8, U8).
- Отметка и заключения (§6.13, 0.5.11, У1-А): `ap:mark:{id}:{done|moved|missed}` — статусные кнопки карточки (окно: `planned`, дата наступила, без ограничения сверху), поток — как `ap:after`; `ap:conc:{id}` (экран заключений), `ap:conc:{id}:add`, `ap:conc:{id}:{cid}:text`, `ap:conc:{id}:{cid}:photo`, `ap:conc:{id}:{cid}:photo:put`, `ap:conc:{id}:{cid}:photo:rm`, `ap:conc:{id}:{cid}:del`, `ap:conc:{id}:{cid}:del:ok`. FSM `AppointmentConclusion.text` (текст заключения) и `AppointmentConclusion.photo` (ожидание фото); черновик — ключи `ap_conc_id` (id события), `ap_conc_cid` (id записи), `ap_conc_text`, `ap_conc_msg` (id текстового сообщения экрана — чтобы править его после действий с фото-сообщением, реализация 0.5.11). Самая длинная — `ap:conc:123456789:123456789:photo:rm` (37 байт). Чужой или удалённый `id` — «Это сообщение устарело» (U8).
- Записи дневника и новый калькулятор (0.6.0, §6.3, §6.14): `add:menu` (экран выбора типа), `add:bp`, `add:med` (фасад, §6.7), `add:walk`, `add:food`, `add:stress`, `add:sleep`, `add:note`; шаги типов — `add:walk:{10|20|30|60}`, `add:food:pick:{salt|coffee|alcohol}`, `add:food:done`, `add:food:time:{now|other}` (radio времени: `now` — «Сейчас», в правке — «Оставить {момент}»; `other` — ввод `ЧЧ:ММ`), `add:stress:{stress|anxiety|headache|weakness|dizziness|other}`, `add:sleep:{little|normal|much}`, `add:time:{now|other}`; FSM — `EventInput.text` (текст события) и `EventInput.time` (`ЧЧ:ММ`). Калькулятор: `m:field:{sys|dia|pulse}` (фокус поля), `m:reset` («Изменить» — сброс полей); `m:key:*` — как прежде (`m:key:skip` — legacy: старые сообщения без черновика → главное меню, C14). «💊 Лекарство» сводки: `m:med:more` («➕ Добавить ещё»), `m:medask:dismiss` («Не сейчас» в подсказке о лекарствах, ответ — «Хорошо. Добавить лекарства можно позже в настройках.»); «➕ Фактор»: `m:fact`, `m:fact:pick:{salt|coffee|alcohol|stress|sleep}`, `m:fact:done`, `m:fact:cancel`. Лента: `d:filt:{all|bp|med|fact|other}`; события: `ev:open:{id}`, `ev:edit:{id}`, `ev:save:{id}`, `ev:del:{id}`, `ev:del:{id}:ok`, `ev:back`; `ev:edit:{id}` — перезапуск флоу типа в режиме правки (флаг режима — в FSM-данных); финальные callback'и в правке — `ev:save:{id}` для `walk`/`stress`/`note` (вместо `add:time:now`) и `food` (вместо `add:food:done`); у `sleep` кнопки `add:sleep:{little|normal|much}` остаются, обработчик по флагу правки делает UPDATE вместо INSERT (V10). Debounce 1.5 с — только callback'и, пишущие в БД (`m:key:ok`, `m:fact:done`, `m:med:done`, `add:time:now`, `add:sleep:*`, `add:food:done`, `ev:save:*`, `ev:del:ok`; `add:time:other` и `add:food:time:other` — только переход к вводу времени, под общий throttle 1 с); правка буфера (`m:key:0…9`, `m:key:bksp`, `m:key:clear`), чекбоксы (`m:fact:pick:*`, `m:med:pick:*`, `add:food:pick:*`) и чтение (`ev:open`, `ev:back`, `ev:del` — экран подтверждения, `d:filt:*`) — без дебаунса, каждое нажатие (V2/V4/V5); остальное — общий throttle 1 с. Самая длинная — `add:stress:headache` (18 байт), `ev:del:123456789:ok` (21 байт) — в лимит 64 байт.
- Никаких банковских/платёжных данных в callback_data и FSM.

## 9. Валидация ввода

Pydantic-схемы (`bot/schemas.py`); сообщения об ошибках — короткие и человеческие («Нужно число от 70 до 260»), с повторной попыткой. Дублируются CHECK-ограничениями БД (§4.2).

| Поле | Правило |
|---|---|
| систолическое | целое 70…260 (калькулятор: 1–3 цифры) |
| диастолическое | целое 40…160 (калькулятор: 1–3 цифры); строго ниже систолического |
| пульс | целое 30…220 (калькулятор: 1–3 цифры) или «Пропустить» |
| заметка | строка 1…500; пустая (после `trim()`) — «Заметка не может быть пустой» (канон §4.2) |
| название лекарства | строка 1…100 |
| дозировка | строка 1…50 (кнопки 5/10/20/50/100 мг + «Другое»: мг/мл/таб) |
| время HH:MM | `datetime.time`, строго `ЧЧ:ММ` |
| таймзона | валидный IANA через `zoneinfo.ZoneInfo` |
| дата `ДД.ММ[.ГГГГ]` | календарная дата в таймзоне пользователя, не позже сегодняшней |
| момент измерения (дата + время) | строго ≤ текущего момента; будущее — «Время измерения не может быть в будущем» |
| быстрый ввод архива | `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]`, целые числа в диапазонах выше |
| быстрая строка давления (0.6.0) | `САД/ДАД [пульс]` или `САД/ДАД/пульс` (позиции 1/2/3 = систолическое/диастолическое/пульс); сепараторы — `/` и пробел (лишние пробелы и ведущие нули допустимы: «120 / 80», «083» = 83 — либеральный парсер, как `parse_archive_quick`, R4 = Б), точка и запятая — ошибка «Например: 120/80 72»; одиночное число — в активное поле |
| момент события (0.6.0) | «сейчас» или `ЧЧ:ММ` в пределах последних 24 часов (скользящее окно, C13); будущее — «Время измерения не может быть в будущем» |

Пара давлений проверяется при подтверждении диастолического (и при правке): систолическое строго выше диастолического — иначе «Диастолическое (нижнее) должно быть ниже систолического (верхнего) — проверьте цифры.» и повтор ввода. Минимальная разница (пульсовое давление) не ограничивается. Текстовый ввод числа — строго целое (1–3 цифры): «12.5», «120/80», «сто двадцать» — ошибка «Нужно целое число от … до …». Диапазоны покрывают всё, что измеряет бытовой тонометр (20…299 мм рт. ст. давление, 30…190 уд/мин пульс), и отсекают опечатки; значения совпадают с CHECK-ограничениями БД (§4.2).

## 10. Экспорт CSV

- Файл `bp_diary_<дата1>-<дата2>.csv`, отправляется как документ.
- Кодировка UTF-8 с BOM (кириллица в Excel), разделитель `;`.
- Колонки: `timestamp_local, systolic, diastolic, pulse, medication_taken, feeling, note` (`timestamp_local` — `ДД.ММ.ГГГГ ЧЧ:ММ` в таймзоне пользователя).
- Второй файл — приёмы лекарств: `timestamp_local, medication, dosage, status` (статус: `taken` / `skipped` / `partial`).
- Период: 7 дней / 30 дней / всё время. Для «всё время» экспорт разрешён, только если записей ≤ 5000; иначе — «Слишком много записей, выберите 7 или 30 дней». Генерация — через `asyncio.to_thread` (§7.2 п.7).
- Формат совместим с импортом CSV в личном кабинете (§16.2): экспорт → правка (например, в Excel) → импорт обратно без преобразований.

### 10.1 Экспорт PDF

Один генератор `bot/services/pdf_report.py` для трёх мест: бот (§6.5, §6.6), личный кабинет (§16.2) и кабинет врача (§21). Файл одинаковый, отличается только подпись пациента в шапке. Отчёт повторяет таблицу «История записей» ЛК (§16.2); графиков и дополнительных разделов (комментарии врача, журнал приёмов, список лекарств) в нём нет. Исключение с 0.5.11 (В32-Г) — раздел «Заключения врачей» **у пациента** (см. ниже; в кабинете врача его нет, У2-А).

- **Страница**: A4 альбомная, поля 10 мм; шрифт DejaVuSans, заголовки и шапка таблицы — DejaVuSans-Bold (`bot/services/fonts/`, свободная лицензия).
- **Шапка**: «Отчёт по давлению» (жирный); «Период: ДД.ММ.ГГГГ — ДД.ММ.ГГГГ»; подпись пациента и время формирования: у врача — «Пациент {telegram_id} · сформирован ДД.ММ.ГГГГ ЧЧ:ММ», у пациента (бот и ЛК) — «Дневник давления · сформирован ДД.ММ.ГГГГ ЧЧ:ММ». Время формирования — в часовом поясе пациента (как строки таблицы). В файле пациента telegram_id нет: файл могут переслать. **С 0.5.1** (§6.12, P6-В): данные пациента заполнены — и у врача, и у пациента подпись «Иванов Иван Иванович, 12.03.1958 (67 лет) · сформирован …»; не заполнены — у врача имя из Telegram или «Пациент {telegram_id}», у пациента — прежняя «Дневник давления».
- **Сводка** — те же метрики и та же функция расчёта, что в блоке «Сводка» ЛК (`services/stats.summary`): «Записей: N»; «Систолическое: в среднем X (мин–макс)»; «Диастолическое: в среднем X (мин–макс)»; «Пульс: в среднем X» (если есть); «Тренд: …»; «Цель: …» (если есть); «Лекарства: принято X из Y приёмов» (если настроены лекарства, §6.5).
- **Легенда** над таблицей — две плашки, как в ЛК: жёлтая «САД 130–139 · ДАД 85–89» и красная «САД ≥ 140 · ДАД ≥ 90».
- **Таблица**: колонки Дата (`ДД.ММ.ГГГГ`), Время (`ЧЧ:ММ`), Сист., Диаст., Пульс, Лекарство, Самочувствие, Заметка — по локальному времени пациента, сортировка по времени по возрастанию. Значения ячеек — как в ЛК: «Лекарство» — перечень отмеченных приёмов («Принято: Лозартан 50 мг, Амлодипин 5 мг», §4.11), без них — «Принято» / «Пропущено» / «—»; самочувствие — по-русски, как в ЛК («головная боль»); пульс без значения — «—»; заметка — полностью. Длинный текст переносится по словам, высота строки — по самой высокой ячейке; строка, не помещающаяся на странице, целиком переносится на следующую.
- **Подсветка строк** — правило ЛК (§16.2): вся строка заливается цветом худшей из зон САД/ДАД, только жёлтая и красная; пороги и цвета — из `web/cabinet/chart_zones.py` (полупрозрачная заливка переводится в непрозрачный цвет на белом фоне). Без текстовых меток для ч/б печати.
- **Колонтитулы**: на каждой странице после первой повторяется шапка таблицы; внизу каждой страницы — «Стр. N из M».
- **Пустой период** — файл не формируется: в боте — «За этот период записей нет»; в ЛК и у врача — сообщение на странице «За этот период записей нет.».
- **Лимит** — как у CSV: больше 5000 записей за период — файл не формируется: в боте — «Слишком много записей, выберите 7 или 30 дней»; в ЛК и у врача — «Слишком много записей, выберите период поменьше.».
- **Имя файла** — `bp_diary_<дата1>-<дата2>.pdf` (та же функция, что у CSV, §10). У врача при заполненных данных пациента (0.5.1, P6-Д) — `<Фамилия>_<И>_<О>_<дата1>-<дата2>.pdf` латиницей (транслитерация по ГОСТ 7.79-2000, схема Б, без апострофов), CSV — так же.
- **Генерация** — в `asyncio.to_thread` (§7.2 п.7); в поток передаются готовые строки отчёта (значения, не объекты ORM). Сессия БД закрывается до генерации и отправки файла.
- **«Заключения врачей»** (0.5.11, В32-Г, У2-А) — только в PDF пациента (бот и ЛК): генератор получает признак «показывать заключения», в кабинете врача раздела нет (§21). Раздел идёт **после таблицы, в конце документа** (Р6), и только если за период отчёта есть заключения — фильтр по дате **события** (`starts_on`), не по `created_at` заключения. Группировка по событию (Р7): заголовок события («{title} — {ДД.ММ.ГГГГ}» — без иконки: DejaVuSans не содержит эмодзи, реализация 0.5.11), под ним заключения события — одним — текст, несколькими — список «1.», «2.»; события — по дате по возрастанию. Фото (`photo_file_id`) и заметка события не включаются.
- **Бот**: кнопка «Экспорт PDF» под сводкой статистики и в «Настройках» → вопрос «За какой период выгрузить?» с кнопками «7 дней» / «30 дней» / «всё время» и «Назад» (`pdf:7` / `pdf:30` / `pdf:all`). На время генерации сообщение с вопросом редактируется на «Готовлю отчёт…»; затем — документ с подписью «Отчёт по давлению за ДД.ММ–ДД.ММ» и возврат к меню, как после CSV. «Всё время» — период от даты первой записи до сегодня. Двойное нажатие — одна выгрузка (§7.2). «Отправить отчет» (§21) PDF не присылает — только ссылку для врача.

## 11. Тексты и тон

- Всё по-русски, дружелюбно, коротко. Без канцелярита и маркетинга. Слово «поддержать», не «донат».
- «Измерение» (не «замер»), «давление» (не «АД»).
- `texts.py` — единственный источник текстов; в хендлерах строки-литералы не разбрасываются.
- Подписи кнопок веб-кабинета — по единому словарю (§16.2, «Единый UI-канон кнопок», 0.5.9); он же канон для тестов (`test_web_*`).

**Канон внешнего вида бота (0.5.10; ответы — `doc/BOT-UI-UX-QUESTIONS.md`, UI1–UI17).** Канон для экранов бота — тот же дух, что «Единый UI-канон кнопок» §16.2, но для Telegram. Крупный режим (`users.ui_large`) — только рендер: данные, валидация, callback'и и обработчики не зависят от режима.

- **Шаблон экрана:** первая строка — шапка «[эмодзи] Заголовок»; тело (данные, статусы, подсказки); кнопки — сначала действия, потом навигация «Назад» (уровень выше) и «В меню» (главное меню). Исключения (UI1): главное меню — **без заголовка**; шаги ввода («Систолическое (верхнее)?») — без шапки, вопрос сам по себе; сообщения напоминаний и уведомлений — по своим шаблонам §6.7 / §6.13, но теми же правилами кнопок.
- **Словарь шапок** (канонические, для `test_texts.py`): «📊 Статистика», «⚙️ Настройки», «☀️ Напоминания», «📖 Дневник за сегодня», «📅 Визиты и анализы», «👤 Мои данные», «📅 Средние по дням», «📆 Свой период», «📄 Отчёт для врача», «💊 Мои лекарства», «🗂 Архивные записи», «📣 О боте», «📣 Поддержать», «👥 Мои пациенты» (врач), «🩺 Меню врача», «📊 Статистика БД» (админ), «💾 Бэкапы», «🩺 Здоровье системы», «📢 Рассылка», «👤 Поиск пользователя», «🩺 Давление и пульс» (экран калькулятора, 0.6.0).
- **Эмодзи** (UI2, умеренно): в заголовках экранов, статусах (✅, 💊, 📅, 🟢/🟡/🔴), в кнопках точечно (✏️, 📅, 💊, 📊, ➕); не в каждой строке данных.
- **Кнопки.** По одной в строке — базовое правило (и в главном меню, и в админке). Пары в ряд — только короткие альтернативы (UI3): «Да» / «Нет», «Готово» / «Пропустить», «Мужской» / «Женский», «Принять» / «Отклонить», «7 дней» / «30 дней»; слот-сообщение — «Записать давление» / «Принял всё ✓» и «Отметить принятые» / «Через 30 мин» («Пропускаю сегодня» — отдельно); режим «Только лекарства» — «Принял всё ✓» / «Отметить принятые»; повтор замера — «Записать сейчас» / «Через 30 мин» + «Больше не напоминать» (2+1); уведомление о визите — «✓ Помню» / «Перенести» и «Отменить визит» / «📄 Отчёт для врача» (пара для отмены визита у `doctor`). Всё остальное — по одной в строке; в крупном режиме пары разъезжаются по строкам. **Исключение** — клавиатура-калькулятор (§6.3), календарь (§6.5, §6.13) и сетки времени/целевых значений (§6.1, §6.6, §6.13): их раскладка задана в соответствующих разделах (3 колонки в обычном режиме, 2 — в крупном; сетка времени — 3–4 кнопки в ряд) и правилу «по одной в строке» не подчиняется.
- **Parse mode** (UI5): HTML точечно. Шапки экранов — `<b>{эмодзи} Заголовок</b>` всегда (оба режима); значения в `<b>` — в крупном режиме везде, где они есть (сводки, дневник, списки, уведомления о визитах и слотах); обычный режим — plain text. Пользовательский текст (заметки, названия лекарств, ФИО, текст рассылки) в HTML-сообщениях экранируется (`html.escape`).
- **Сводки** (UI4) — по строке на поле («Систолическое: 135») в обоих режимах; в крупном режиме значения — `<b>`.
- **Экраны записей дневника (0.6.0, §6.3, §6.14).** Шапка калькулятора — «🩺 Давление и пульс» (V3 = А: экран-форма, а не «шаг с вопросом»). У измерения — «Как самочувствие?», у события «Стресс / самочувствие» — «Что чувствуете?» (I7); «Головная боль» есть и там и там — это норма (C19). «Приём лекарства» — запись о приёме, а не о препарате (C20). Опциональный текст события — кнопка «Пропустить» (M6); тап по строке приёма в ленте — тост «Только просмотр» (M4).
- **Крупный режим** (UI6, всё пакетом, один переключатель «👁 Крупный режим»): 1) кнопки — по одной в строке везде (включая пары); 2) калькулятор — 2 колонки (§6.3); 3) списки — блоками и с пагинацией по 5 («Показать ещё …», §8); 4) сводки — значения `<b>`; 5) гистограмма — крупнее (§6.5, параметр `large` у `stats_chart.render`).
- **Админка** (UI14): тот же шаблон (шапка, тело секциями, кнопки «Назад» / «В меню»); крупный режим на админку не распространяется — она одинакова для всех.

## 12. Ошибки и логирование

- Ошибки БД и неожиданные исключения в хендлерах — логируются (`logging`, `LOG_LEVEL`), пользователю — «Что-то пошло не так, попробуйте ещё раз» без технических деталей.
- Падение на старте (нет `BOT_TOKEN`, БД не создаётся) — понятное сообщение в консоль, exit code 1.
- Токен и пароли не попадают в логи ни при каких условиях (см. «Безопасность токена»).

## 13. Тесты и покрытие

**Требование: весь код покрыт юнит-тестами.** Это критерий готовности, а не пожелание.

- Инструменты: pytest, pytest-asyncio, pytest-cov, pytest-mock. БД в тестах — in-memory SQLite / временный файл с теми же PRAGMA, что и в проде.
- **Покрываются все модули**: `config`, `db`, `models`, `schemas`, `keyboards`, `texts` (состав/целостность текстов), `services/*`, `handlers/*` (через моки `Bot`, `Message`, `CallbackQuery` — хендлеры вызываются напрямую с фейковыми объектами aiogram), веб-аутентификация (`test_web_auth.py`: хеширование/проверка Argon2id, сессионные cookie, magic link — срок действия и одноразовость, rate limiting входа).
- **Покрываются все таблицы**: в том числе `medications` (CRUD, UNIQUE(user_id, name, slot)), `medication_logs` (статусы, идемпотентность отметок), `web_users`.
- **Порог покрытия**: ≥ 90% строк по проекту (`pytest --cov=bot --cov-fail-under=90`); бизнес-логика (`services/stats`, `services/medications`, `schemas`, `services/csv_export`, `services/pdf_report`, `services/reminders`) — 100% веток. Отчёт: `--cov-report=term-missing`.
- **Тесты конкурентности** (`test_concurrency.py`) обязательны: параллельное сохранение измерений (`asyncio.gather`), двойное нажатие на кнопку (throttle/идемпотентность), двойной тик планировщика (одно напоминание — одна отправка), get-or-create пользователя из двух параллельных /start. Правило §7.2 «не ждать asyncio-лок, держа незакоммиченную запись SQLite» проверяется детерминированно, порядком очереди лока: «Готово» мультивыбора лекарств берёт лок приёмов один раз (журнал и связи — одной транзакцией); `get_or_create_user` не оставляет открытой запись после создания пользователя. Две правки одной записи, прочитавшие строку до коммита друг друга, оставляют один набор значений целиком, без смеси (чередование сессий, а не гонка). Две одновременные перенастройки слота напоминания оставляют согласованные `local_time` и `next_trigger_time` (обёртка фабрики сессий держит обе после чтения строки). Job store отделён от основной БД и не попадает в бэкап (`test_main.py`). Тяжёлый синхронный код кабинета (PDF-отчёт врача и ЛК, Argon2id при входе, регистрации и смене пароля) идёт вне event loop: пока заглушка такой функции ждёт сигнала, параллельный лёгкий запрос отвечает (`LoopGate` в `tests/conftest.py`, проверка по событиям, а не по таймингам).
- **PDF-отчёт** (§10.1): `test_services_pdf.py` — шапка (врач / пациент, у пациента без telegram_id), сводка совпадает с `stats.summary`, подсветка строк по худшей зоне, перенос длинного текста и повтор шапки таблицы, пустой период и лимит 5000 (файл не формируется); `test_web_pdf.py` — ЛК и врач (только свои данные; чужой пациент и `status≠'active'` — отказ; пустой период и лимит — сообщение на странице); `test_handlers_pdf.py` — бот (`pdf:7/30/all`, «Готовлю отчёт…», подпись и имя файла, пустой период, лимит, двойное нажатие). Текст PDF проверяется извлечением (`pypdf`, только в тестовых зависимостях). Нагрузка: `LOAD_TEST=1 pytest qa/test_load_doctor.py` — p95 карточки врача < 2 с с новым генератором.
- **Приглашение врача по e-mail** (§21): `test_services_mailer.py` — текст письма, SMTP по SSL и STARTTLS, SMTP недоступен / адрес отвергнут (ошибка в лог без адреса и токена), пустой `SMTP_HOST`, лимиты 3/сутки и 1/час, атомарность лимита при параллельных отправках, `invite_log`; `test_handlers_doctor.py` — письмо при добавлении врача (ушло / не ушло / лимит, ссылка в письме и для пересылки — один токен), повторное добавление после отзыва, «Отправить отчет» (письмо, ошибка, лимит, без почты); `test_web_doctor.py` — ссылка на 72 часа, страница подтверждения (GET не списывает), списание по кнопке и его переживание перезапуска (`used_tokens`), параллельное «Открыть дневник», чистка просроченных списаний при старте. Письма в тестах не уходят: SMTP подменяется.
- **Приглашение врача через Telegram** (§21): `test_handlers_doctor.py` — экран выбора способа, ссылка-приглашение, @username найден / не найден / не врач — одинаковый ответ, лимиты приглашений, «Отправить отчет» врачу в Telegram; `test_handlers_doctor_menu.py` — `/start doc_<code>` (действующий, просроченный, списанный, чужой код), «Принять» / «Отклонить» и уведомления пациента, двойное «Принять», `/iamdoctor`, меню врача, «Мои пациенты» (только `active`) и пагинация списка (`doc:patients:{limit}`, 5/10/…), ссылка входа врача (15 минут, страница подтверждения), уведомления врачу об отчёте и отзыве; миграция `doctors` на копии старой схемы (`test_db.py`).
- **Гистограмма статистики** (§6.5): `test_services_stats_chart.py` — PNG 1080 × 720, цвета половин по зонам (пиксели в столбике САД / ДАД), пустой день, нулевая высота ниже базы, расширение шкалы при значениях > 180 / > 110, пульс есть / нет, подписи значений только за 7 дней, средние по локальным суткам; `test_handlers_stats.py` — фото с подписью и кнопками вместо текста, «Назад» / «Экспорт CSV» / «Экспорт PDF» под фото, период без записей — текст, генерация в потоке.
- **Кнопки напоминаний** (§6.7, 0.5.4): `test_handlers_reminders.py` — «Частично…» без FSM: переключение по клавиатуре сообщения, «Готово» и «Отмена», **два открытых списка** («Готово» в каждом отмечает свой слот), список после «перезапуска» (пустой FSM), запись давления посередине не мешает; «Отмена» → «Через 30 мин» работает; дата приёма (тот же день — момент нажатия, вчерашнее — день напоминания, старше 2 суток — «устарело», «Через 30 мин» вчерашнего — «устарело»); callback'и 0.5.3 без даты; чужое и удалённое лекарство; тексты «уже отмечены сегодня» и смешанный итог; `test_services_reminders.py` — `last_message_id` и снятие кнопок прежнего сообщения (ошибка Telegram — не сбой), сброс `last_sent_date` при «Через 30 мин», catch-up: отложенное доставляется, просрочка > 6 ч — перенос слота, повтор замера просрочен или не доставлен — удалён, новый повтор после этого ставится; миграция поколения 6 (`test_db.py`); Telethon-сценарий — `qa/test_reminders_e2e.py` (слоты ставятся через «Настройки» на «через минуту»: «Отмена» → «Через 30 мин», два открытых списка, повторное «Принял всё ✓»; перезапуск бота QA-контейнер не делает — его закрывают юнит-тесты обработчиков без FSM).
- **Удобство напоминаний, часть 1** (0.5.5; §6.4, §6.7, §8): `test_handlers_reminders.py` — подписи «Отметить принятые» / «Пропускаю сегодня», итог после «Принял всё ✓» и «Готово» со слотом и кнопками «Записать давление» / «В меню»; `test_handlers_diary.py` — строка лекарств (статусы «✓» / «частично» / «пропущено» / «ещё не отмечено», только слоты с лекарствами, без «Кризиса», без лекарств — строки нет, границы локальных суток), «💊 Отметить приём» (нет неотмеченных — кнопки нет; один слот — сразу список; несколько — выбор), список `d:*` (переключение, «Готово» → дневник с итогом, «Отмена», дата не сегодня и чужое лекарство — «устарело», повтор замера); `test_handlers_fallback.py` — непринятый callback → «Это сообщение устарело», места аудита §8; `tests/test_landing.py` — руководство с новыми подписями.
- **Удобство напоминаний, часть 2** (0.5.6; §4.1, §4.3, §6.7, §16.2): `test_services_reminders.py` — режимы (тексты и кнопки, «3 часа» не для `meds`, `meds` без лекарств как `both`), повтор (ставится при доставке по расписанию и catch-up, не при «Через 30 мин»; не ставится без `followup_enabled` и на паузе; ответ кнопкой, отметкой `taken` слота, записью давления в окне 1 ч — повтора нет; запись давления не считается в `meds`; сообщение без звука, кнопки исходного сняты, `last_message_id` — на повтор; catch-up в окне 6 ч и обнуление за окном; выключение снимает job'ы), пауза (слоты переносятся молча, `med_remeasure` удаляется, первая строка «Напоминания снова включены ▶️» и сброс паузы, «1 день» = до конца сегодня); `test_handlers_reminders.py` — ответ кнопками (`rem:write:{slot}:{д}`, «Принял всё ✓», «Отметить принятые», «Через 30 мин», «Пропускаю сегодня») обнуляет `followup_at`, «Через 30 мин» на паузе — уведомление, `pressure` + «Пропускаю сегодня» без `skipped`; `test_handlers_settings.py` — экран «Напоминания» с паузой, режимом и переключателем, экран режима (нет лекарств — нет «Только лекарства», предупреждение для `pressure`), пауза 1/3/7 и «Снять паузу», сброс `meds` → `both` при удалении последнего лекарства слота; `test_concurrency.py` — гонка «ответ и повтор» (детерминированное чередование: кто первым обнулил `followup_at`); `test_web_reminders.py` — ЛК: режим, пауза, переключатель, те же проверки и сбросы, CSRF; миграция поколения 7 на копии старой схемы (`test_db.py`); `qa/test_reminders_e2e.py` — пауза и повтор.
- **Средние по дням и свой период** (§6.5, 0.5.3): `test_services_stats.py` — `daily_averages`: дни без измерений, дни без пульса, округление дня и итога из неокруглённых средних, итог без дней с пульсом, границы локальных суток и переход DST, зона дня по худшей из САД/ДАД, совпадение значений с гистограммой; `test_keyboards.py` — календарь (первый день недели — понедельник, пустые клетки, нет «▶» в текущем месяце, отметка «[N]», все `callback_data` ≤ 64 байт); `test_handlers_stats.py` — «📆 Свой период» → шаг 1 → шаг 2 → фото с заголовком «За период … (N дней)», листание месяцев, будущий день и > 31 дня (уведомление, календарь на месте), обратный порядок дат, один день, битая и подделанная `callback_data`, «📅 По дням» и «Назад» к фото (7 / 30 / свой период), нет записей, склонение «день / дня / дней», двойное нажатие; `test_services_stats_chart.py` — 1 и 31 столбик, подписи значений только при ≤ 7 днях; `test_web_daily_averages.py` — ЛК и врач: блок в сводке, все дни периода и «0» у пустых, подсветка и `tfoot`, 92 дня — таблица, 93 — текст, нет записей — блока нет, сутки по поясу пациента, чужой пациент и `status≠'active'` — отказ.
- **Согласие** (§6.11): `test_services_consent.py` — действующее согласие (версия, отзыв, замена версией), частичный уникальный индекс и двойное «Согласен» (`asyncio.gather`), доступ врача по трём условиям; `test_handlers_consent.py` — Б1 новому и существующему, блокировка действий и напоминаний до согласия, «Не согласен» (новый — запись удалена; существующий — §19), отзыв = удаление, Б7 (запись в момент согласия, `doctor_id` при «Принять», `withdrawn_at` при отзыве и отклонении), Б8, Д4 для `active` и `pending` (отмена списывает код), скрытие пациента у врача до ответа; `test_web_consent.py` — `/consent` и `/doctor/consent` (перенаправления, CSRF, чекбокс), пациент без согласия не виден врачу (список, карточка, экспорт, заметки); `test_services_deletion_log.py` — HMAC без telegram_id в файле, чистка > 15 дней, `--apply` удаляет восстановленные аккаунты; `test_services_backup.py` — хранение 7 + 1 и удаление копий старше 14 дней; `tests/test_landing.py` — три раздела `privacy.html` с якорями; дата редакции `#consent` = `CONSENT_VERSION`, `#policy` и `#rights` = `POLICY_DATE`; К5 — отзыв в кабинете (CSRF, сессии удалены, ежечасное удаление ботом с отменой джобов); миграция поколения 4 на копии старой схемы (`test_db.py`).
- **Данные пациента** (§6.12): `test_services_profile.py` — валидация полей, «все или ни одного», граница 16 лет по часовому поясу, склонение возраста, подписи (полная, короткая, без данных), транслитерация имени файла; `test_handlers_profile.py` — экран «Мои данные», FSM с ошибками и «Нет отчества», младше 16, удаление, ввод перед приглашением врача, П7 один раз, обновление `telegram_name`; `test_web_profile.py` — форма ЛК (CSRF, ошибки, удаление), список врача (сортировка по фамилии, «данные не заполнены»), карточка, шапка PDF врача и пациента, имена файлов CSV/PDF; админка — только счётчик; миграция поколения 5 (`test_db.py`).
- **Сверка слотов с базой** (0.5.7, §6.7): `test_services_reminders.py` — тик ставит job слота, время которого изменено «в обход» бота (как форма ЛК): позже и раньше прежнего; включённый в обход слот получает job; job на верное время не пересоздаётся; job выключенного слота снимается; `next_trigger_time` в прошлом — catch-up в окне и перенос за окном; повторы замера и «если не ответил» не трогаются; job тика ставится при старте; `test_web_reminders.py` — после POST `/settings` и тика job стоит на новое время (общая БД, без перезапуска); живая проверка — сценарий воспроизведения Н8 на локальном стенде (`qa/reports/e2e057/`): напоминание приходит в новое время.
- **Визиты и анализы** (0.5.8, §4.16, §6.13): `test_services_appointments.py` — CRUD, статусы и условные `UPDATE`, лимит 30 (в т.ч. две параллельные вставки), 60/200 символов, +2 года, моменты уведомлений (все строки таблицы §6.13, Н3 до 06:00, событие после 18:00, «без времени»), пересчёт флагов при сохранении и переносе (В7), тик (захват, двойной тик, окно 6 ч, началось — без «за 2 часа», без согласия, пауза не действует, смена пояса), «без отметки» через 3 дня, `moved` после даты и правка до даты, +N месяцев с концом месяца; `test_handlers_appointments.py` — список (пусто, «Скоро», «Прошедшие», > 20), шаги 1–5 и «Назад», FSM ввода (ошибки), календарь (границы, листание), время (прошло сегодня), сохранение и строка расписания, карточка по статусам, «Изменить» по полям, «Удалить» и «Отменить» с подтверждением, уведомления и кнопки (`ap:seen`, «Перенести», «📄 Отчёт для врача», «Отправить врачу» только при доступе), «Как прошёл визит?» по типам, «Записать следующий», устаревшие и чужие id, двойные нажатия; `test_handlers_diary.py` — строка «📅 Сегодня / Завтра»; `test_web_appointments.py` — раздел ЛК, добавление, правка, статусы, удаление, ошибки формы, лимиты, CSRF, изоляция, врач не видит; `test_db.py` — миграция поколения 8; `test_concurrency.py` — тик против тика и ответ против тика, лимит 30 под гонкой; `test_services_consent.py` — новая `CONSENT_VERSION`; `qa/test_appointments_e2e.py` — Telethon: добавить, уведомление (момент сдвигается в БД стенда), «Был(а)», «Записать следующий».
- **Отметка о посещении и заключения** (0.5.11, §4.17, §6.13, §8, §10.1, §16.2): `test_services_appointments.py` — заключения CRUD (лимит 5 условной вставкой, 1–4000 символов, пустой текст после `trim()` отклоняется, `photo_file_id` set/clear, каскадное удаление с событием), отметка из карточки без окна (в т.ч. «без отметки» позже 3 дней), `moved`/`missed` из карточки; `test_handlers_appointments.py` — статусные кнопки карточки с дня события (`ap:mark:*`), оба пути отметки идемпотентны (Р10): `ap:mark` действует без окна (через две недели, из «без отметки»), `ap:after` через 3 дня — «Это сообщение устарело», повторное нажатие после отметки — «Это сообщение устарело» (U8), экран заключений (добавление, правка, фото — прислать/заменить/убрать, удаление с подтверждением, пустое и >4000, лимит 5), кнопка «{Подпись заключения}» на «Отметил ✓», чужие id, двойные нажатия; `test_services_pdf.py` — раздел «Заключения врачей» у пациента и его отсутствие у врача; `test_web_appointments.py` — заключения в ЛК (просмотр, добавление, правка, удаление, CSRF), врач не видит; `test_db.py` — миграция поколения 10; `test_concurrency.py` — двойное сохранение заключения, лимит 5 под гонкой, отметка против тика; `test_services_consent.py` — новая `CONSENT_VERSION` (0.5.11); `qa/test_appointments_e2e.py` — Telethon: отметить из карточки, добавить заключение.
- **UX таблиц и кнопок кабинета** (0.5.9, §16.2, §21): `test_web_tables_ux.py` — карточки «Скоро» (заметка целиком, `white-space: pre-wrap`, «⋯», подпись отмены по типу — В7а — и только у `planned`, форма правки в карточке позволяет сменить тип (Вид) — в отличие от бота, Н4, §6.13), «Прошедшие» аккордеоном по месяцам (текущий развёрнут, «Ждёт отметки» сверху, вне групп, правильная форма «событие / события / событий»; `planned` > 3 дней — в группе своего месяца с подписью «без отметки», не в «Ждёт отметки»), история аккордеоном по дням (заголовок: число записей, средние, точка зоны; развёрнуты дни за последние 7 дней; период ≤ 7 дней — без свёртки и без кнопки; «Свернуть все / Развернуть все»; > 92 дней — «Показать ещё 30 дней» и `?shown=`, кнопка исчезает на полном периоде, ≤ 92 — кнопки нет), правка лекарств формой `?edit_med={id}` (в строке нет инпутов и нет пары «Сохранить»/«Удалить»; не `?edit={id}` — тот занят правкой измерения, id таблиц пересекаются), единые роли кнопок и словарь подписей (все подтверждения удаления — блоком «Удалить «…»? Это действие нельзя отменить.», подтверждение отмены события — по типу), врач — тот же `history_day.html` без действий.
- **Внешний вид бота и крупный режим** (0.5.10, §4.1, §6.1–§6.7, §6.10, §6.13, §8, §11, §18): `test_bot_ui.py` (новый) — рендер обоих режимов: кнопки по одной / пары (в т.ч. режим «Только лекарства» — 3 строки), калькулятор 3 → 2 колонки, сводки по строке на поле и `<b>` в крупном, дневник блоками, пагинация по 5 с «Показать ещё …», HTML-шапки и экранирование пользовательского текста; `test_texts.py` — словарь шапок §11; `test_keyboards.py` — пары из словаря §11, раскладка калькулятора и сеток (исключение §11: не схлопываются в один столбец), подпись «Показать ещё {min(5, N − limit)}», все `callback_data` ≤ 64 байт (самая длинная `stats:days:20261101:20261115:10`); `test_handlers_start.py` — шаг 2 онбординга (`onb:ui:0|1|skip`, перерисовка «Крупный» с «✓», шаги 3–4 в выбранном режиме, `onboarded=1` — шага нет); `test_handlers_settings.py` — «👁 Крупный режим» (`set:ui:0|1`, перерисовка экрана); `test_handlers_stats.py` — календарь `stats:cal:m:/w:` (сетка/неделя, «◀»/«▶» по доступности, шаг 2 с `:{с}`), совместимость `stats:cal:{…}`, пагинация `stats:days:…[:{limit}]`; `test_handlers_appointments.py` — `ap:cal:m:/w:` и совместимость `ap:cal:{…}`, `ap:list:{limit}` / `ap:past:{limit}` (5/10/15/20), пары кнопок уведомлений и «по одной» в крупном; `test_handlers_diary.py` / `test_handlers_measure.py` / `test_handlers_reminders.py` — блоки дневника и сводок, кнопки по одной в крупном; `test_services_stats_chart.py` — `large`: 44/40/26/38/36 и fallback 42/38/36/34 px; `test_handlers_admin.py` — секции «Статистики БД», «Показатель: значение», «Обновлено: ДД.ММ ЧЧ:ММ», прогресс рассылки (полоса 10 символов, throttle 1 с, ≤ 20 получателей без перерисовок); миграция поколения 9 на копии старой схемы (`test_db.py`).
- Запуск всех тестов: `uv run pytest`; одного теста: `uv run pytest tests/test_services_stats.py::test_trend_up`; покрытие: `uv run pytest --cov=bot --cov-report=term-missing`.
- Опционально: в pre-commit/CI — проверка наличия файла `LICENSE`.
- Код без тестов не считается готовым.

## 14. Версионирование

SemVer (`MAJOR.MINOR.PATCH`). В ветке 0.x — конвенция `semver-0x` (0.6.0, M2): MINOR — несовместимые изменения (ломающие привычный сценарий, новые сущности), PATCH — совместимые дополнения и багфиксы; полная семантика MAJOR/MINOR/PATCH — с 1.0.0. Номер — в единственном месте: `pyproject.toml → [project] version`. Стартовая версия `0.1.0`.

## 15. Принятые решения

1. **Напоминания** — три слота (утро/обед/вечер) + одноразовый повтор замера после приёма лекарства (§4.1, §6.7).
2. **Часовой пояс** — кнопки с городами + «Другой» (IANA-ввод с проверкой); `DEFAULT_TIMEZONE = Europe/Minsk`.
3. **Язык докстрингов и комментариев** — русский.
4. **Тулинг сборки** — uv + pyproject.toml; тесты обязательны (§13).
5. **Лекарства** — настройка за 3 шага (название → дозировка → слоты), отметка приёма одним нажатием («Принял всё ✓» / «Частично…» / «Пропускаю»); слот лекарства = слот давления или `crisis` (SOS-препарат без напоминаний и без приверженности), дозировка опциональна (§4.4, §4.5, §6.6, §6.7). Правка — повторный проход тех же шагов с текущими значениями в подсказках (переживающие слоты обновляются на месте, история приёмов сохраняется); удаление — сразу по выбору из списка, без подтверждения (§6.6).
6. **Веб-компоненты** — лендинг (статический сайт) и личный кабинет (FastAPI + Argon2id): вход magic link (`/cabinet`, 15 минут) или e-mail + пароль; сессии — HTTP-only cookie (§16).
7. **Токен бота** — только переменные окружения / секрет-менеджер; в коде, конфигах и логах не бывает (§3, «Безопасность токена»).
8. **Лицензия** — PolyForm-Noncommercial-1.0.0 (бесплатно для некоммерческого использования; коммерческое — только с письменного разрешения автора). Файл `LICENSE` в корне репозитория; упоминание в `README.md` и `pyproject.toml` (`license = {text = "PolyForm-Noncommercial-1.0.0"}`).
9. **Коммерческое лицензирование:** Для использования в клиниках, коммерческих медицинских центрах и других организациях требуется отдельная платная лицензия. Контакт для запросов: `AUTHOR_CONTACT`.

10. **Архивные записи и ввод в кабинете** — перенос прошлых измерений из «Настроек» двумя способами (кнопками и одной строкой); в личном кабинете — добавление, правка и удаление записей и импорт CSV (формат совместим с экспортом). Момент измерения — только прошедший; для архива повторный замер после лекарства не планируется (§6.10, §16.2).

11. **PDF-отчёт** — один формат «как История записей ЛК» для бота, ЛК и кабинета врача: A4 альбомная, сводка как в ЛК, таблица с подсветкой зон, без графиков (§10.1; ответы — `doc/PDF-REPORT-QUESTIONS.md`).

12. **Приглашение врача** — письмо на e-mail через SMTP своего домена (plain text, ссылка на 72 часа) и приглашение через Telegram (ссылка `?start=doc_<code>` и поиск по @username только среди врачей, явное «Принять»), меню врача в боте. Релизы: e-mail — 0.4.11, исправление соли ссылок — 0.4.12, Telegram — 0.4.14 (0.4.13 — исправление кнопок повтора замера) (§21; ответы — `doc/DOCTOR-INVITE-QUESTIONS.md`).

13. **Гистограмма в статистике бота** — фото со сводкой в подписи: столбик = день, САД вверх от 90, ДАД вниз от 50, цвета зон `chart_zones.py`, пульс числом на средней линии; Pillow, 1080 × 720 (§6.5; ответы — `doc/STATS-HISTOGRAM-QUESTIONS.md`). Релиз 0.4.15.

14. **Согласие на обработку персональных данных** (0.5.0) — основное согласие пациента и отдельное на каждого врача (`consents`), согласие врача (`doctors.consent_*`); бот и кабинет без действующего согласия показывают только экран согласия; отзыв = удаление аккаунта; страница «Персональные данные» из трёх разделов; резервные копии ≤ 14 дней и журнал удалений. ФИО, дата рождения и пол пациента в кабинете врача — 0.5.1 (§6.11; ответы — `doc/PATIENT-PROFILE-QUESTIONS.md`, тексты — `doc/PERSONAL-DATA-TEXTS.md`).

15. **Данные пациента для врача** (0.5.1) — ФИО (три поля), дата рождения (16+) и пол в боте и ЛК; нужны только для приглашения врача; у врача — в списке, карточке, PDF и боте; без данных — имя из Telegram (§6.12; ответы — `doc/PATIENT-PROFILE-QUESTIONS.md`).

16. **Сообщество в Telegram** (0.5.2) — публичная группа с темами `https://t.me/my_bp_diary` (новости, подсказки, вопросы, идеи, статус бота; публикация — вручную, бот в группе не админ). В боте — кнопка «Наше сообщество» на экране «О боте» (`GROUP_URL`, пустая или плейсхолдер — кнопки нет); на лендинге — футер, карточка автора в `#support` и `sameAs` в JSON-LD `SoftwareApplication` (§3, §6.8, §16.1; ответы — `doc/TG-CHANNEL-QUESTIONS.md`, материалы — `doc/tg-group/CONTENT.md`).

17. **Средние по дням и свой период** (0.5.3) — таблица средних САД / ДАД / пульса по локальным суткам и итог «Среднее за день» (среднее из средних дней) в ЛК и у врача (блок в «Сводке», ≤ 92 дней, пустые дни видны) и в боте (кнопка «📅 По дням», только дни с измерениями, кружок зоны); в боте — свой период инлайн-календарём без FSM, ≤ 31 дня; CSV и PDF не меняются (§6.5, §16.2, §21; ответы — `doc/DAILY-AVG-QUESTIONS.md`).

18. **Кнопки напоминаний без FSM** (0.5.4) — состояние списка «Частично…» в `callback_data` и клавиатуре сообщения (переживает перезапуск, не путает слоты), дата напоминания в callback'ах (приём — за день напоминания, старше 2 суток — «устарело»), кнопки прежнего напоминания слота снимаются (`reminders.last_message_id`, схема — поколение 6), окно catch-up 6 ч и удаление недоставленного повтора, понятный итог «уже отмечены сегодня» (§4.3, §6.7, §8; ответы — `doc/REMINDERS-QUESTIONS.md`). Улучшения U1, U2, U4–U8 оттуда же — отдельным релизом после правки §6.4, §6.6, §6.7 (U3 отклонено; U8 в 0.5.4 — только для напоминаний).

19. **Удобство напоминаний, часть 1** (0.5.5) — после «Принял всё ✓» и «Готово» — слот в итоге и «Записать давление» / «В меню»; кнопки «Отметить принятые» и «Пропускаю сегодня»; в «Дневнике за сегодня» — строка лекарств по слотам и «💊 Отметить приём»; любая устаревшая кнопка отвечает «Это сообщение устарело». Тексты руководства на лендинге — в релизе; кадры бота с напоминанием и «Отметить принятые» — сразу после релиза, отдельным коммитом. Часть 2 (0.5.6) — повтор, если не ответил, режим слота, пауза — после отдельной правки SPEC (§4.1, §4.3, §6.6, §6.7, §16.2, схема — поколение 7) и `CLAUDE.md` («Схема БД») (ответы — `doc/REMINDERS-UX-QUESTIONS.md`).

20. **Удобство напоминаний, часть 2** (0.5.6) — режим слота «Давление и лекарства» / «Только лекарства» / «Только давление»; одно тихое повторное напоминание через 60 минут, если человек не ответил (ответ — кнопка, отметка лекарств слота или запись давления), с выключателем; пауза напоминаний на 1 / 3 / 7 дней; всё — и в ЛК. Схема — поколение 7 (`reminders.mode`, `reminders.followup_at`, `users.followup_enabled`, `users.reminders_paused_until`) (§4.1, §4.3, §6.6, §6.7, §8, §16.2; ответы — `doc/REMINDERS-UX-QUESTIONS.md`).

21. **Сверка слотов с базой** (0.5.7) — тик бота раз в минуту ставит job'ы слотов, изменённых или включённых в ЛК (раньше — только после перезапуска бота; найдено при подготовке визитов, Н8) (§6.7, §16.2; ответы — `doc/APPOINTMENTS-QUESTIONS.md`).

22. **Визиты и анализы** (0.5.8) — визит к врачу, анализы и обследование с подсказками; уведомления накануне в 20:00 и за 2 часа (без времени — утром в 07:00), без паузы и повтора; «📄 Отчёт для врача» (PDF за 30 дней) в уведомлении о визите; «Как прошёл визит?» и «Записать следующий?»; кнопка меню, строка в дневнике, `/visits`, раздел ЛК; врач не видит; уведомления — тиком из 0.5.7 с захватом по флагам; новая редакция согласия (до выпуска — юрист). Схема — поколение 8 (`appointments`) (§4.16, §6.2, §6.4, §6.7, §6.11, §6.13, §8, §16.2; ответы — `doc/APPOINTMENTS-QUESTIONS.md`).

23. **UX таблиц и кнопок кабинета** (0.5.9) — «Визиты и анализы» карточками (заметка целиком, «⋯», форма правки в карточке), «Прошедшие» — аккордеон по месяцам, «Ждёт отметки» сверху; «История записей» — аккордеон по дням (заголовок со средними и зоной, развёрнуты дни за последние 7 дней, «Показать ещё 30 дней» > 92), правка лекарств формой, единый UI-канон кнопок и словарь подписей; врач — тот же partial истории без действий (§5, §11, §13, §16.2, §17, §21; ответы — `doc/CABINET-TABLES-UX-QUESTIONS.md`). Релиз 0.5.9 (PATCH, по конвенции 0.x).

24. **Внешний вид бота и «Крупный режим»** (0.5.10) — единый канон экранов бота (шаблон «[эмодзи] Заголовок», словарь шапок, умеренные эмодзи, пары кнопок только для коротких альтернатив, HTML точечно); «Крупный режим» для людей с плохим зрением: кнопки по одной в строке, калькулятор 2 колонки, списки блоками с пагинацией по 5, значения жирным, гистограмма крупнее, календарь списком недели — всё пакетом, переключатель «👁 Крупный режим» в «Настройках» и шаг 2 онбординга; настройка — только бот, рендер — только вид, данные и callback'и не меняются. Админка — по тому же шаблону, крупный режим на неё не распространяется. Схема — поколение 9 (`users.ui_large`) (§4.1, §6.1–§6.7, §6.10, §6.13, §8, §11, §13, §18; ответы — `doc/BOT-UI-UX-QUESTIONS.md`). Релиз 0.5.10 (PATCH, по конвенции 0.x).

25. **Отметка о посещении и заключения врачей** (0.5.11) — статусные кнопки «✓ Был(а)» / «Перенёс(ла)» / «Не был(а)» («✓ Сдал(а)» / «Не сдал(а)» у анализов) в карточке события с дня события и без ограничения окна (`ap:mark:{id}:{…}`; окно 3 дня — только у кнопок уведомления `ap:after`, У1-А); заключения к событиям всех типов: текст 1–4000 пишет пользователь (распознавания фото нет), необязательное фото — только Telegram `file_id` (на сервере не хранится, в ЛК и PDF не попадает), до 5 на событие; экран в боте, в ЛК — текст (просмотр, добавление, правка, удаление), раздел «Заключения врачей» в PDF пациента (у врача — нет, У2-А); врач не видит (В34); новая редакция согласия (до выпуска — юрист). Схема — поколение 10 (`appointment_conclusions`) (§4.17, §6.11, §6.13, §8, §10.1, §16.2, §21; ответы — `doc/APPOINTMENTS-POSTVISIT-QUESTIONS.md`). Релиз 0.5.11 (PATCH, по конвенции 0.x).

26. **Записи дневника и «➕ Добавить запись»** (0.6.0) — «Записать давление» остаётся прямым входом (R1 = Б), рядом «➕ Добавить запись» с семью типами записей (давление, приём лекарства, прогулка, питание, стресс/самочувствие, сон, заметка); калькулятор — поля «САД | ДАД | Пульс» с быстрой строкой `120/80 72` и сводкой `✅ 135/85, пульс 72`; «➕ Фактор» (соль/кофе/алкоголь/стресс/недосып) — события с привязкой к измерению; лента «Дневника за сегодня» — все виды записей с фильтрами и правкой/удалением событий; повтор замера после лекарства — из «💊 Лекарство» и фасада «Приём лекарства». Схема — поколение 11 (`diary_events`) (§4.18, §6.2–§6.4, §6.7, §6.14, §8, §9, §11; ответы — `doc/MEASURE-REDESIGN-QUESTIONS.md`, план — `doc/MEASURE-REDESIGN-PLAN.md`).

**Известные ограничения 0.6.0** (C21, подраздел §15 по M3): события не попадают в личный кабинет, CSV и PDF (экспорт и отчёт врача — следующими версиями, R24 = В); фильтра по отдельной метке внутри чипа «Факторы» нет; строки `medication_logs` в ленте не правятся и не удаляются (управление — «Приём лекарства» и напоминания слота).

Открытый вопрос: строка о группе в «Персональных данных» (`privacy.html`, О3) — правка текста, одобренного юристом, показать юристу (`doc/tg-group/CONTENT.md`, §5). Тексты 0.5.0 одобрены юристом (2026-11-09), редакция от 09.11.2026.

## 16. Веб-компоненты (лендинг и личный кабинет)

Создаются после бота, но архитектурно закладываются сразу: структура `web/` (§5), таблица `web_users` (§4.6), переменная `WEB_SECRET_KEY` (§3).

### 16.1 Лендинг (`web/landing/`)

Статический сайт (HTML + CSS + минимальный JS): быстрее, безопаснее, не требует БД. Вёрстка — Tailwind CSS, собранный заранее в статический `web/landing/css/tailwind.css` (без Play CDN в браузере).

- **Hero-секция**: заголовок «Дневник давления, который не мешает жить»; подзаголовок с ключевой выгодой (быстрая запись давления, напоминания о лекарствах, сводка для врача); большая кнопка «Открыть в Telegram» — `https://t.me/my_bp_diary_bot?start=landing`.
- **Вход в личный кабинет**: ссылка «Войти в личный кабинет» на `/login` веб-кабинета (вход e-mail + пароль) в трёх местах: шапка (компактная кнопка рядом с «Открыть в Telegram»), финальный CTA и раздел «Личный кабинет» руководства. Адаптивность шапки: <640px — компактные кнопки и только иконка логотипа; 640–1023px — название логотипа и кнопки, навигация скрыта; 1024–1279px — иконка логотипа, навигация и кнопки; ≥1280px — полный вид. Переполнения шапки и горизонтального скролла нет ни на одной ширине.
- **Проблема / решение**: 3 коротких блока — «Забываете мерить?», «Не помните, приняли ли таблетку?», «Врач просит дневник, а вести некогда?».
- **Как это работает**: 3 шага с иконками (запись → напоминания → статистика).
- **Как это выглядит** (якорь `#screens`, сразу после «Как это работает»): вкладки «Telegram-бот» / «Личный кабинет» / «Кабинет врача» со скриншотами и подписями. Бот (9 кадров, Telegram Web, рамка телефона): главное меню, калькулятор с вводом, сводка после сохранения, напоминание с перечнем лекарств, мультивыбор лекарств («· сегодня ✓»), «Дневник за сегодня» со строкой лекарств (0.5.5), статистика за 7 дней, «По дням» и календарь своего периода (0.5.3). Личный кабинет (6, рамка браузера): три графика с зонами, «Средние по дням» (0.5.3), история с подсветкой зон, форма «Добавить измерение» с выбором лекарств, управление лекарствами, импорт CSV с отчётом. Кабинет врача (3): список пациентов, карточка пациента, первая страница PDF-отчёта. Плюс мобильные кадры (390 px): история ЛК и карточка пациента у врача. Увеличение по клику — минимальный JS (`script.js`). Картинки — `web/landing/img/screens/*.webp` (`loading="lazy"`, заданные `width`/`height`, `alt`). Подписи — утверждённые дословно (`doc/LANDING-SCREENS-QUESTIONS.md`). Снимаются скриптом `qa/screenshots/shot_landing.py` на локальном стенде с вымышленными данными (отдельная демо-БД с «test-stand» в имени, тестовый бот); в кадр бота — только область диалога (без списка чатов и профиля аккаунта).
- **Сигналы доверия**: «Бесплатно, без рекламы», «Записи видите только вы и врач, которого вы пригласили», «Сделано одним разработчиком» (до 0.5.0 — «Данные хранятся локально на нашем сервере, не передаются третьим лицам»: неверно — сервер в Германии, Telegram, врач).
- **Секция «Поддержать проект»** (якорь `#support`):
  - **Информация об авторе**: Старинский Олег Григорьевич, `info@electroman.by`, `@ElectroManBY`; сообщество бота в Telegram — строка «Сообщество: `@my_bp_diary` — новости, подсказки, вопросы и идеи» (ссылка `https://t.me/my_bp_diary`).
  - **Реквизиты для донатов**: ЮMoney (кошелёк 4100119514684342), WebMoney, Пополнение по IBAN (BY18 BPSB 3014 F000 0000 1164 7609), СберБанк РФ (+375 29 714-28-82). Все способы оформлены списком с иконками и прямыми ссылками (если применимо).
  - **Пояснение**: поддержка добровольная, функции за оплатой не закрыты, деньги идут на хостинг и развитие.
- **Секция «Для бизнеса»**: пояснение о лицензии PolyForm Noncommercial 1.0.0 (бесплатно для личного использования, для клиник и коммерческих организаций требуется лицензия). Контакт `info@electroman.by` для запроса счёта.
- **FAQ**: 3–4 вопроса (нужна ли регистрация, можно ли экспортировать данные, как удалить аккаунт). С 0.5.8 — ещё «Напомнит ли бот о приёме у врача?»: «Да. В разделе «📅 Визиты и анализы» добавьте визит к врачу, анализы или обследование — бот напомнит накануне в 20:00 и за 2 часа, а после спросит, как всё прошло. В напоминании о визите к врачу есть кнопка «📄 Отчёт для врача» — PDF с давлением за 30 дней, чтобы показать на приёме.» (видимый FAQ и `FAQPage` — дословно).
- **Документация** (якорь `#docs`): подробное руководство пользователя простым языком — начало работы, запись давления, напоминания, лекарства, дневник и статистика, визиты и анализы (с 0.5.8; кадры — отдельным коммитом после релиза, В24), личный кабинет, отчёт для врача, данные и настройки. Оформлено аккордеонами (`<details>`) с оглавлением; формулировки кнопок и меню — по текстам бота (`bot/texts.py`), без медицинских советов. Содержание руководства синхронизируется с фактическими функциями бота (правка и удаление лекарств, изменение записи и т.п.) и не описывает несуществующих возможностей.
- **Футер**: ссылки «Персональные данные» (`privacy.html`), руководство (`#docs`), «Сообщество в Telegram» (`https://t.me/my_bp_diary`) и контакт автора.
- **SEO и скорость** (решения — `doc/LANDING-SEO-QUESTIONS.md`). Канонический адрес — `https://bp-diary.electroman.by/`.
  - **`<head>`**:
    - title «Дневник артериального давления в Telegram — Дневник гипертоника»;
    - description «Бесплатный дневник давления в Telegram: запись давления и пульса кнопками, напоминания о лекарствах, графики и отчёт для врача в PDF. Без рекламы.»;
    - `<link rel="canonical">`.
  - **Open Graph и Twitter Card**:
    - `og:title` «Дневник гипертоника — дневник давления в Telegram»;
    - `og:description` «Записывайте давление и пульс за пару секунд, получайте напоминания о лекарствах и показывайте врачу сводку. Бесплатно, без рекламы.»;
    - `og:image` — `img/og.png` (1200×630): коллаж из кадров бота и кабинета с названием и слоганом, собирается `qa/screenshots/make_og.py` из `img/screens/`;
    - `og:url` — канонический адрес; `og:type` website; `og:locale` ru_RU; `og:site_name` «Дневник гипертоника»;
    - `twitter:card` summary_large_image.
  - **Структурированные данные (JSON-LD)**:
    - `FAQPage` — вопросы и ответы дословно из видимого FAQ;
    - `SoftwareApplication` — `applicationCategory` HealthApplication, `operatingSystem` «Telegram, Web», `offers.price` 0, ссылка на бота, `sameAs` на сообщество `https://t.me/my_bp_diary`;
    - `Person` — автор, контакты как в `#support`.
  - **Подтверждение вебмастеров**: `<meta name="yandex-verification">` и `<meta name="google-site-verification">`. Коды публичные и хранятся в репозитории; для Яндекса дополнительно — файл подтверждения `yandex_<код>.html` в корне лендинга. Счётчиков аналитики (Яндекс.Метрика и т. п.) нет: это противоречило бы обещанию «не передаются третьим лицам».
  - **Иконки**: `favicon.svg` (по логотипу шапки), `favicon.ico`, `apple-touch-icon.png`, `<meta name="theme-color" content="#2563eb">`.
  - **Индексация**:
    - `robots.txt` (`Allow: /`, `Sitemap:`) и `sitemap.xml` (`/`, `/privacy.html`, `lastmod`);
    - кабинет закрыт от индексации: `GET /robots.txt` кабинета отдаёт `Disallow: /` (§16.2).
  - **Скорость**:
    - Tailwind — статический CSS, пересборка `tools/landing/build_css.py` после правки разметки. Тест проверяет, что все классы разметки есть в собранном CSS;
    - шрифт Inter — Google Fonts с `preload`;
    - кеширование — `deploy/landing-nginx.conf` (картинки, CSS, JS, иконки — `Cache-Control: public, max-age=2592000`; HTML, `robots.txt`, `sitemap.xml` — `no-cache`; gzip для текста). Подключается вторым томом контейнера лендинга. Те же правила — в блоке лендинга `deploy/Caddyfile`.
  - **Тексты под поисковые запросы**: правки S6 — «артериальное давление», «пульс», «журнал измерений», «приложение», «таблица» — в Hero, «Как это работает» и FAQ, утверждены дословно.
  - **Доступность**:
    - подсказка под скриншотами — контраст не ниже 4,5:1;
    - заголовок лайтбокса — не `<h3>`;
    - `src` картинки лайтбокса задаётся только при открытии.
  - **`privacy.html`**: description, canonical и ссылка «← На главную».
- **`privacy.html` — «Персональные данные»** (0.5.0, §6.11): шапка с оглавлением и три видимых раздела со своими H2 — «Политика обработки персональных данных» (`#policy`), «Согласие на обработку персональных данных» (`#consent`), «Ваши права» (`#rights`); у каждого строка «Редакция от ДД.ММ.ГГГГ» (`#consent` — `CONSENT_VERSION`, `#policy` и `#rights` — `POLICY_DATE`). Тексты — дословно утверждённая редакция `doc/PERSONAL-DATA-TEXTS.md` (Т1–Т4); разделы не смешиваются. Страница выкладывается раньше бота, чтобы ссылки из экранов согласия работали с первой минуты (сведения ст. 5 п. 5 должны быть доступны до согласия).

### 16.2 Личный кабинет (`web/cabinet/`)

FastAPI + Jinja2 + uvicorn; зависимости: `fastapi`, `uvicorn`, `jinja2`, `argon2-cffi`, `itsdangerous` (сессии). Предназначен для просмотра и управления данными с компьютера.

Кабинет закрыт от поисковиков: `GET /robots.txt` отдаёт `User-agent: *` / `Disallow: /` (без авторизации, `text/plain`).

**Логика входа и регистрации:**

1. **Пользователь без аккаунта:** При нажатии «Личный кабинет» или `/cabinet` бот запускает FSM `CabinetRegistration.email`. Просит ввести e-mail (с валидацией формата). После ввода создает запись в `web_users` (пароль генерируется случайно). Затем генерирует magic link и отправляет: «Аккаунт создан. Ваша ссылка для входа: [ссылка]».
2. **Пользователь с аккаунтом:** Бот просто генерирует magic link и отправляет её.
3. **Валидация e-mail:** Если e-mail уже занят (UNIQUE constraint), бот сообщает об ошибке и просит ввести другой.

**Аутентификация:**

- MVP — magic link: команда `/cabinet` (или кнопка «Личный кабинет») в боте генерирует одноразовую ссылку с токеном (действует 15 минут) → вход без пароля. Токен для входа по magic link генерируется через `itsdangerous.URLSafeTimedSerializer` с использованием `WEB_SECRET_KEY`. Дополнительная таблица в БД не нужна: токен проверяется по подписи и времени жизни (15 минут).
- Полноценный вход — форма регистрации в кабинете: e-mail + пароль (таблица `web_users`, §4.6). Пароль хешируется **Argon2id** (победитель Password Hashing Competition, рекомендация OWASP; библиотека `argon2-cffi`): в БД хранится только хеш, соль генерируется автоматически, проверка — `PasswordHasher().verify(hash, password)`.
- Сессии: HTTP-only cookie с сессионным токеном (хранится в SQLite, таблица `web_sessions`, §4.7), время жизни — 7 дней. Cookie ставятся с флагом Secure при HTTPS-доступе (по схеме запроса или `CABINET_URL`); для локального запуска по HTTP флаг снимается — иначе клиент не возвращает Secure-куки обратно и формы падают на CSRF-проверке.
- **Восстановление пароля**: в MVP отсутствует (SMTP используется только для приглашений врача, §21). Если пользователь забыл пароль, он запрашивает новую magic link через бота (`/cabinet`) — она даёт доступ к кабинету, где пароль можно сменить в настройках профиля.

**Содержимое (максимально информативно):**

- **Личная карточка**: имя, e-mail, целевые значения, часовой пояс, список лекарств. **С 0.5.1** (§6.12): строки «ФИО», «Дата рождения», «Пол» (не заполнены — «не заполнены») и форма «Мои данные» (POST `/profile/personal`, CSRF): «Фамилия», «Имя», «Отчество», «Дата рождения» (`type=date`), «Пол» (список «мужской» / «женский»), кнопки «Сохранить» и «Удалить данные» (POST `/profile/personal/delete`). Проверки — те же, что в боте (`bot/schemas.py`); ошибки — тексты П4 / П5 над формой; сохранено — «Данные сохранены.», удалено — «Данные удалены.». Список лекарств в карточке выводится отдельным блоком на всю ширину карточки, сгруппированным по слотам приёма («Утро», «Обед», «Вечер», «Кризис»), по одному лекарству на строку; пустые группы не показываются.
- **Согласие** (§6.11): строка К1 в «Личной карточке»; без действующего согласия — только страница `/consent` (К3).
- **История записей**: таблица с фильтрами по дате (7 / 30 дней / произвольный период), сортировка по времени. Колонки: дата, время, систолическое, диастолическое, пульс, лекарство, самочувствие, заметка. В колонке «лекарство» — перечень отмеченных при этой записи приёмов («Принято: Лозартан 50 мг, Амлодипин 5 мг», `measurement_medications`, §4.11); если отмеченных приёмов нет — статус флага «Принято» / «Пропущено» / «—» (`measurements.medication_taken`). Строки подсвечиваются по зонам давления графиков сводки (те же пороги и цвета — `web/cabinet/chart_zones.py`, персональная цель не влияет): вся строка — цветом **худшей** из двух зон (САД и ДАД; красная > жёлтая > зелёная), например 154/84 — красная строка. Подсвечиваются только жёлтая (САД 130–139 / ДАД 85–89) и красная (САД ≥ 140 / ДАД ≥ 90) зоны; строки в норме — стандартный фон таблицы. На узких экранах (строки-карточки) — тот же фон карточки. Над таблицей — легенда из двух плашек: жёлтая «САД 130–139 · ДАД 85–89» и красная «САД ≥ 140 · ДАД ≥ 90». У подсвеченной строки — `title` с зонами показателей («САД: ≥ 140»). Только информационная подсветка, без текстов-диагнозов.
  - **Группировка по дням** (0.5.9). История — аккордеон по дням (`<details>/<summary>`, без JS). Заголовок дня — «Пт, 12.11 · 3 записи · в среднем 132/84 · 🟡» (день недели, ДД.ММ, число записей в правильной форме, средние САД/ДАД дня, точка зоны дня — по правилу «Средних по дням»: худшая из зон округлённых средних, только жёлтая и красная, 🟢 — норма). Только дни с записями; внутри дня — текущие колонки таблицы, на узких экранах — строки-карточки (как сейчас). По умолчанию развёрнуты дни за последние 7 дней от сегодняшнего дня (пояс пользователя); при периоде ≤ 7 дней свёртки нет и кнопка не показывается. Над таблицей — кнопка «Свернуть все / Развернуть все» (короткий скрипт, как у предотметки лекарств); состояние свёртки — на время просмотра (не в БД и не в куках). Врачу — тот же partial (`partials/history_day.html`) без колонки действий (§21).
  - **Длинные периоды** (0.5.9). Период длиннее 92 дней — история показывает последние 92 дня периода и кнопку «Показать ещё 30 дней»; каждое нажатие добавляет 30 более ранних дней, кнопка исчезает, когда показан весь период. Состояние в URL: `?shown=N` — число показанных дней вглубь от конца периода (старт — min(92, длина периода)), ссылку можно переслать. Экспорт CSV/PDF — всегда за весь период фильтра. При периоде ≤ 92 дней кнопки нет.
- **Сводка**: те же метрики, что в боте (среднее, мин/макс, тренд), но с возможностью скачать CSV и PDF-отчёт и посмотреть графики (Chart.js — лёгкая библиотека). График разбит на три панели: «Систолическое», «Диастолическое», «Пульс» — тремя отдельными графиками (три canvas). У панелей САД и ДАД задний фон подсвечен полупрозрачными цветными зонами с горизонтальными линиями порогов (ESC/ESH + ВОЗ, только информационная подсветка, без диагностики): САД — зелёная < 130, жёлтая 130–139, красная ≥ 140 мм рт. ст.; ДАД — зелёная < 85, жёлтая 85–89, красная ≥ 90 мм рт. ст. Панель «Пульс» — без зон. Заливка зон — до границ графика. Рядом с кнопками «Скачать CSV измерений» / «Скачать CSV лекарств» — «Скачать PDF» (`GET /export/report.pdf?days=…&date_from=…&date_to=…`; период — текущий фильтр страницы, только свои данные; формат — §10.1). Пороги зон — константы приложения и не связаны с персональной «целью» пользователя. Страница адаптивная (брейкпоинты 480/768/1024/1280): на узких экранах панели графиков идут вертикально.
  - **Средние по дням** (0.5.3, расчёт — §6.5 «Средние по дням»). Блок внутри карточки «Сводка за период» — под метриками, над графиками; период — тот же фильтр страницы (7 / 30 дней / свой период). Заголовок «Средние по дням»; над таблицей — та же легенда из двух плашек, что у «Истории записей» (`partials/history_zone_legend.html`). Таблица: Дата (`ДД.ММ.ГГГГ`) · Сист. · Диаст. · Пульс · Измерений — **каждый** день периода по возрастанию даты; день без измерений — «—» в трёх колонках и «0», без подсветки (видны пропуски). Подсветка строк и `title` — правило «Истории записей» (§16.2) по зоне дня: только жёлтая и красная. Последняя строка (`<tfoot>`, жирным, без подсветки) — «Среднее за день» с итогами САД / ДАД / пульса, в колонке «Измерений» — пусто. На узких экранах — строки-карточки, как у «Истории записей». Записей за период нет — блока нет (сводка уже показывает «нет записей»). Период длиннее 92 дней — вместо таблицы текст «Выберите период до 92 дней, чтобы увидеть средние по дням» (сводка, графики, история и экспорт работают как прежде; сам «Свой период» не ограничивается). Шаблон — общий partial `partials/daily_averages.html` (ЛК и врач). В CSV и PDF (§10, §10.1) средних по дням нет.
- **Управление лекарствами**: CRUD-интерфейс (добавить, отредактировать, удалить). С 0.5.9 — строки таблицы только для чтения (Название · Дозировка · Слот), без инлайн-инпутов; правка — «✏️» открывает форму «Изменить лекарство» (тот же partial `partials/medication_form.html`, что «Добавить лекарство», предзаполненная, `?edit_med={id}` — коллизия с `?edit={id}` правки измерения: id таблиц пересекаются); удаление — «🗑» (красная иконка без заливки, `title="Удалить"`), подтверждение — блоком («Удалить «…»? Это действие нельзя отменить.», залитая красная «Удалить» + «Отмена»), не `js confirm()`.
- **Настройки напоминаний**: те же слоты, что в боте. С 0.5.6 (§6.7 «Удобство напоминаний, часть 2») — та же форма (POST, CSRF): у каждого слота — список «О чём напоминать» («Давление и лекарства» / «Только лекарства» / «Только давление»; «Только лекарства» недоступен, если в слоте нет лекарств — подпись «Сначала добавьте лекарства в этот слот»; при «Только давление» со лекарствами — подпись-предупреждение, как в боте); чекбокс «Повторять, если не ответил»; блок «Пауза»: «Нет» / «1 день» / «3 дня» / «7 дней» (на паузе — строка «На паузе до ДД.ММ» и кнопка «Снять паузу»). Проверки и последствия — как в боте: подделанный `meds` без лекарств — ошибка формы, выключение повтора снимает отложенные повторы, удаление последнего лекарства слота в «Управлении лекарствами» сбрасывает режим `meds` в `both`. С 0.5.7 правка применяется ботом не позже чем через минуту (тик, §6.7); под кнопкой «Сохранить напоминания» — подпись «Бот применит изменения в течение минуты.»
- **Крупный режим** (0.5.10, UI9): в ЛК **не подключён** — отдельной задачей; поле `users.ui_large` общее, веб-рендер может его использовать позже. Веб-часть идёт по своему «Единому UI-канону кнопок» (§16.2, 0.5.9).
- **Визиты и анализы** (0.5.8; с 0.5.9 — карточки вместо таблиц, §6.13, `#appointments`, только пациент). Два списка: «Скоро» и «Прошедшие» (статусы — как в боте, «ждёт отметки» / «без отметки» — вычисляются).
  - **«Скоро» — карточки событий** (0.5.9): `<article class="event-card">`; первая строка — вид (иконка и тип) + дата и время («—» без времени), вторая — название, третья — заметка (если есть, целиком, `white-space: pre-wrap`). Действия — «✏️ Изменить» (призрачная) и `<details>` «⋯» (без JS): «Отменить визит» / «Отменить сдачу» / «Отменить обследование» — по типу (В7а, см. «Отмена события»; только у `planned`) и «🗑 Удалить…» (у всех; иконка «🗑» внутри пункта — по роли «Опасное в строке», В5; отдельной «🗑»-кнопки в карточке нет). «✏️ Изменить» раскрывает под карточкой ту же форму, что «➕ Добавить» (предзаполненную; не модалка — минимум JS): «Вид» (список из трёх типов; тип в ЛК изменить можно — в отличие от бота, где тип не меняется, Н4, §6.13), «Название» (`<input>` с `<datalist>` подсказок всех типов, ≤ 60), «Дата» (`type=date`, `min` — сегодня, `max` — сегодня + 2 года), «Время» (`type=time`, необязательно), «Заметка» (≤ 200); кнопки «Сохранить» / «Отмена» / «🗑 Удалить». Правка — POST `/appointments/{id}` (смена даты/времени пересчитывает флаги, §6.13); удаление — POST `/appointments/{id}/delete`; форма «➕ Добавить» (POST `/appointments`, CSRF) — те же поля, кнопка «Добавить».
  - **«Прошедшие»** (0.5.9) — карточки со статусом, аккордеон по месяцам («Ноябрь 2026 · 5 событий», правильная форма «событие / события / событий»); группы-месяцы и события внутри — новые сверху; `<details open>` — текущий месяц, остальные — свёрнуты. «Ждёт отметки» — сверху списка, вне групп: `planned` с прошедшей датой (событие сегодняшнего дня остаётся в «Скоро» до полуночи) и прошло ≤ 3 дней. `planned` старше 3 дней — «без отметки» (Н2): в группе своего месяца с подписью статуса «без отметки», в «Ждёт отметки» не попадает. У «ждёт отметки» — «✓ Был(а)» / «Не был(а)» (`/appointments/{id}/status`, `done` / `missed`; у анализов — «Сдал(а)» / «Не сдал(а)») и «Перенесли на…» (дата и время, POST `/appointments/{id}/moved` — `moved` + новая строка, Н1).
  - **Отмена события** (0.5.9) — подпись по типу: `doctor` → «Отменить визит», `lab` → «Отменить сдачу», `exam` → «Отменить обследование»; подтверждение — «{Подпись} «{Название} — {ДД.ММ}»? Событие останется в «Прошедших» со статусом «отменено».» (подпись статуса — как в боте); POST `/appointments/{id}/cancel`.
  - **Заключения** (0.5.11, §6.13, §4.17, У3-Б): доступны с дня события, как в боте (Р3). В карточке события — строка-превью «📝 Заключение: «первые 60 символов…»» / «📝 Заключений: N» и раскрывающийся блок `<details>` «📝 {Подпись заключения}» (подписи по типу — как в боте) со списком записей: текст каждой целиком, «✏️ Изменить» (та же форма ввода, ≤ 4000) и «🗑 Удалить…» (подтверждение блоком «Удалить «…»? Это действие нельзя отменить.»); внизу блока — «➕ Добавить». Формы — `?add_conc={id}` (добавить к событию) и `?edit_conc={id}` (править заключение); общий `?edit={id}` занят правкой измерения (Р9, канон 0.5.9). Добавление — POST `/appointments/{id}/conclusions`, правка — POST `/conclusions/{id}` (CSRF). Форма: `<textarea>` ≤ 4000, кнопки «Сохранить» / «Отмена» (канон 0.5.9); длиннее — «Слишком длинно — до 4000 символов.», пустое (после `trim()`) — «Напишите текст заключения.». Лимит 5 — общий с ботом (§4.17, §6.13): та же условная вставка (§7.2), гонка вкладок; переполнение — «Заключений уже 5. Удалите ненужные, чтобы добавить новое.» (как в боте). Фото в ЛК не показывается и не загружается (`photo_file_id` — Telegram, вебу недоступен). Врач заключений не видит (§21).
  - Проверки — как в боте; ошибки — `/?error=appointment#appointments` («Проверьте название, дату и время.»), `/?error=appointment_limit#appointments` («Запланировано уже 30 событий. Удалите ненужные, чтобы добавить новое.»); чужой id — 404. Уведомления ставит бот (тик раз в минуту, §6.7): кабинет пишет только строки. Визиты врачу не видны (§21).
- **Добавление, правка и удаление записей**: в разделе «История записей» — кнопка «➕ Добавить измерение» и, у каждой строки, «✏️» (правка) и «🗑» (удаление). Форма (`POST /measurements`, правка — `POST /measurements/{id}`): дата, время, систолическое, диастолическое, пульс (опционально), «Принял лекарство» (чекбокс → `medication_taken`) с выбором лекарств под ним (см. ниже), самочувствие («Норма» / «Головная боль» / «Слабость» / «Другое» → короткий текст в заметку), заметка (≤ 500). Дефолт даты и времени — текущие; момент измерения ≤ текущего (§9). Валидация — Pydantic-схемы (§9); дубль за минуту — «Это давление уже записано» (§7.2); чужая запись выглядит как отсутствующая (§7.1). `timestamp` при правке меняется (в отличие от правки в боте, §6.3) — с той же проверкой дубля и запретом будущего времени. Добавление и правка из ЛК повторный замер не планируют. Выбор лекарств в ЛК работает так же, как в боте: создаёт связи `measurement_medications` и записи `medication_logs` (со статусом `taken` и соответствующим временем), влияя на приверженность (§6.5) — в том числе для прошлых дат, в отличие от архива в боте (§6.10). Запись данных (добавление/правка/удаление/импорт) — только для пациента: врач имеет доступ к чтению и комментариям (§21).
  - **Блок выбора лекарств.** Виден только при отмеченной галочке «Принял лекарство» (CSS `:has()`, без JS); без галочки сервер выбор не учитывает. Лекарства пользователя — чекбоксами, сгруппированными по слотам «Утро» / «Обед» / «Вечер» / «Кризис», как в личной карточке; пустые группы не показываются. Выбор необязателен: галочка без лекарств — только флаг. Лекарств нет — «У вас пока нет добавленных лекарств» + inline-ссылка «Перейти в Управление лекарствами». Чужой или несуществующий id лекарства (`WHERE id=? AND user_id=?`) — ошибка формы «Лекарство не найдено», ничего не сохраняется (§7.1).
  - **Предотметка (форма добавления).** Среди слотов «Утро» / «Обед» / «Вечер», чьё время в пределах ±2 ч от времени измерения, выбирается ближайший — его лекарства отмечены; ни одного в окне — ничего не отмечено. Время слота — `reminders.local_time` включённого напоминания; если напоминание выключено или записи нет — умолчание 08:00 / 13:00 / 21:00 (`DEFAULT_SLOT_TIMES`). «Кризис» не предотмечается никогда (препарат не по расписанию). Небольшой inline-скрипт пересчитывает предотметку при смене времени в форме, пока пользователь сам не менял галочки лекарств; без JS предотметка считается только при открытии формы.
  - **Журнал.** Кабинет пишет `medication_logs` с `timestamp = measurements.timestamp` (не «сейчас»), `status='taken'`, слот — из строки лекарства (§4.11). Повтор того же приёма за те же локальные сутки новой строки журнала не создаёт (§7.2), связь с записью создаётся всё равно. Журнал записи — строки с её моментом и id связанных лекарств; журналы бота и напоминаний кабинет не трогает.
  - **Правка.** Форма показывает текущие связи записи отмеченными (предотметки нет). Сохранение считает разницу: добавленные галочки — новые связи и журнал на момент записи; снятые — удаляются связь и журнал записи этого лекарства; снятая галочка «Принял лекарство» снимает все связи и журнал записи. Если момент записи изменён — журнал записи переносится на новый момент (`UPDATE medication_logs SET timestamp = :new_ts WHERE user_id = :uid AND timestamp = :old_ts AND medication_id IN (:linked_ids)`); если в новых локальных сутках этот приём уже отмечен, перенесённая строка удаляется (дедуп §7.2).
  - **Удаление** (`POST /measurements/{id}/delete`) — только с подтверждением на форме. Сначала удаляется журнал записи (по паре `medication_id + timestamp` для её связей), затем сама запись — связи `measurement_medications` удаляются каскадом.
  - Запись измерения, связи и журнал — одной транзакцией; лок приёмов берётся до первой записи в БД (правило §7.2: не ждать asyncio-лок, держа незакоммиченную запись SQLite).
- **Импорт CSV** (`POST /import`) — перенос архива из блокнота одним файлом. Формат — как у CSV-экспорта (§10): UTF-8 (BOM допустим), разделитель `;`, первая строка — заголовок `timestamp_local;systolic;diastolic;pulse;medication_taken;feeling;note`; обязательны `timestamp_local`, `systolic`, `diastolic`, остальные колонки опциональны. `timestamp_local` — `ДД.ММ.ГГГГ ЧЧ:ММ` в таймзоне пользователя. До 5000 строк на файл; больше — «Слишком много строк, разделите файл». Парсинг — в `asyncio.to_thread` (§7.2 п.7), разбор и валидация строк — `web/cabinet/csv_import.py`. Строки с ошибками валидации (включая будущее время) пропускаются и перечисляются в отчёте (до 10, «Строка 5: время в будущем»); дубли (за минуту с существующими записями и внутри файла) пропускаются. Вставка валидных строк — одной транзакцией; двойная отправка формы не создаёт дублей — все строки второго прогона попадают в «Пропущено дублей» (§7.2 п.4). Отчёт: «Импортировано: N · Пропущено дублей: M · Ошибок: K».

**Единый UI-канон кнопок** (0.5.9; ответы — `doc/CABINET-TABLES-UX-QUESTIONS.md`). Действует в ЛК и кабинете врача; канон для тестов (`test_web_*`).

| Роль | Вид | Где |
|---|---|---|
| Основное действие | залитая (`btn`) | «Записать», «Сохранить», «Добавить», «Импортировать», «Сохранить заметку», «Сохранить напоминания», «Войти» |
| Вторичное | призрачная (`btn-ghost`) | «Отмена», «Показать», «Скачать CSV/PDF», «Снять паузу», «✏️» в строке |
| Опасное в строке | иконка «🗑», красная, **без заливки** | строки истории, карточки визитов, строки лекарств |
| Опасное вне строки | призрачная, красный текст/рамка | «Удалить данные» (личная карточка), «🗑 Удалить» в форме правки визита (триггер подтверждения, не финал) |
| Опасное финал | залитая красная (`btn-danger`) | **только** «Удалить» в подтверждении |

Служебные правила: действия строки не прячутся до наведения (клавиатура и тач); подтверждения удаления — блоком на странице (не `js confirm()`); правка — единый паттерн «посмотрел → «✏️» → форма правки (те же поля, что форма добавления, предзаполнена)».

Словарь подписей (канон; тесты `test_web_*` проверяют единые тексты):

| Действие | Подпись |
|---|---|
| Сохранение формы | «Сохранить» («Сохранить изменения» и «✏️ Сохранить» не используются; заголовок формы — «Изменить запись») |
| Создание | «Записать» (измерение) / «Добавить» (лекарство, визит) |
| Отмена формы | «Отмена» |
| Удаление в строке | «🗑», `title="Удалить"` |
| Подтверждение удаления | «Удалить «…»? Это действие нельзя отменить.» + «Удалить» (залитая красная) + «Отмена»; запись измерения — «Удалить запись от ДД.ММ.ГГГГ ЧЧ:ММ? Это действие нельзя отменить.» |
| Отмена события | по типу: «Отменить визит» / «Отменить сдачу» / «Отменить обследование» |
| Отметка после события | «✓ Сдал(а)» / «Не сдал(а)», «✓ Был(а)» / «Не был(а)» |
| Перенос | «Перенести» (кнопка), форма «Перенесли на…» |

**Безопасность веб-части:**

- Все формы — с CSRF-токенами.
- Пароли — только Argon2id, никогда не логируются.
- Rate limiting на попытки входа (5 попыток в минуту).
- HTTPS обязателен (Let's Encrypt).
- Реквизиты карт и платёжные данные в веб-приложении не хранятся — только на внешней странице поддержки (§3).

### 16.3 Развёртывание бота и кабинета

Бот (`python -m bot`) и личный кабинет (`uvicorn web.cabinet.app:app`) — два независимых процесса (или два сервиса в Docker Compose), работающих с одной БД SQLite в режиме WAL. Каждый HTTP-запрос в FastAPI создаёт свою сессию БД (через `Depends`). Модели БД импортируются из `bot/models.py`.

Развёртывание в Docker: `deploy.sh` (Linux/macOS) или `deploy.ps1` (Windows) → `docker compose up -d --build`; оба сервиса собираются из одного `Dockerfile`, SQLite живёт на общем томе. Кабинет слушает только `127.0.0.1:8000`; наружу — через Caddy (профиль `https`, автоматический Let's Encrypt по `deploy/Caddyfile`). Секреты (`BOT_TOKEN`, `WEB_SECRET_KEY`) — только из `.env`, в образ и git не попадают.

## 17. Отличия от ТЗ

| Где | ТЗ | Здесь | Почему |
|---|---|---|---|
| §4.1, §4.3 | `reminder_time` одно поле | расписание — только в `reminders`: три слота (утро/обед/вечер) + `med_remeasure`; в `users` полей `reminder_*` нет | требование заказчика: 3 замера в день + повтор после лекарства |
| §4.2 | без CHECK | CHECK-ограничения диапазонов | защита данных при конкурентном доступе |
| §6.3 | кнопки значений с шагом 5 + «Другое» | клавиатура-калькулятор и ввод числа сообщением; сводка введённых данных | точный ввод любого значения в диапазоне без лишних экранов |
| §6.5 | «не чаще одного раза за сессию» | не чаще 1 раза в сутки | «сессия» в Telegram не определена; сутки — измеримо |
| §6.5, §16.2, §21 | статистика за неделю/месяц | + свой период в боте (календарь, ≤ 31 дня) и средние по дням в боте, ЛК и у врача | решение заказчика (2026-11-10, `doc/DAILY-AVG-QUESTIONS.md`) |
| §4.2 | `feeling` | enum + «Другое» → в `note` | «Другое» без уточнения бесполезно |
| §3 | SUPPORT_URL, AUTHOR_CONTACT | + BOT_TOKEN, DB_PATH, DEFAULT_TIMEZONE, LOG_LEVEL, WEB_SECRET_KEY | необходимая инфраструктура |
| — | многопользовательность и многопоточность не упомянуты | §7 целиком | требование заказчика: изоляция пользователей + максимальная защита от гонок |
| §13 | тесты не упомянуты | весь код покрыт юнит-тестами, порог ≥ 90% | требование заказчика |
| — | лекарства упомянуты пунктиром | §4.4, §4.5, §6.6, §6.7: настройка за 3 шага, слот = слот давления или `crisis`, дозировка опциональна, отметка одним нажатием, приверженность | требование заказчика: «важно, чтобы было напоминание о приёме лекарств и каких» |
| §6.10, §16.2 | добавление из ЛК не создаёт medication_logs | выбор лекарств в форме записи ЛК — связи и журнал с моментом измерения, включая прошлые даты; архив в боте — только флаг `medication_taken` | бот — минимум трения при переносе блокнота; кабинет — рабочее место для детального ввода (решение заказчика, 2026-11-08) |
| — | веб-страница поддержки «позже» | лендинг и личный кабинет — §16 (после бота) | требование заказчика (2026-11-04) |
| §21 | «Скачать PDF» у врача без описания формата | общий отчёт §10.1 для врача, ЛК и бота; экспорт PDF в ЛК и в боте | решение заказчика (2026-11-08, `doc/PDF-REPORT-QUESTIONS.md`) |
| §21 | MVP: ссылка для врача — пациенту для пересылки | письмо врачу через SMTP (если настроен) + та же ссылка пациенту; приглашение через Telegram и меню врача в боте | решение заказчика (2026-11-09, `doc/DOCTOR-INVITE-QUESTIONS.md`) |
| §6.11 | согласие на обработку данных не упомянуто | основное согласие + согласие на каждого врача + согласие врача; без согласия бот и кабинет не работают | Закон РБ № 99-З (ст. 5, 8, 9): данные о здоровье — только с согласия (решение заказчика, 2026-11-09, `doc/PATIENT-PROFILE-QUESTIONS.md`) |
| §6.11 | — | возраст 16+ в 0.5.0 только декларируется фразой согласия; проверка по дате рождения — 0.5.1 | риск (ст. 5 п. 9): несовершеннолетний может дать согласие до 0.5.1; вопрос юристу Л2 |
| §20 | 7 ежедневных + 4 еженедельных копии | 7 + 1, копии старше 14 дней удаляются; журнал удалений | удаление данных — не позже 15 дней (ст. 10, 13) |
| §22 (TEST TZ, ограничение 5) | лёгкие burst-тесты на in-memory SQLite | дымовой профиль на временном файле SQLite с PRAGMA прода | в in-memory (StaticPool) нет реальных блокировок, гонки не воспроизводятся |
| §22 (TEST TZ, §1) | 100 активных напоминаний на 20 пациентов | 60 слотовых + 40 `med_remeasure` | слотов у пользователя не больше трёх (`uq_reminders_user_slot`) |
| §22 (TEST TZ, §1) | 20 пациентов «с историей 30 дней» и отдельно 5000 измерений | история и есть 5000 (250 на пациента) | одна выборка вместо двух пересекающихся |
| §22 (TEST TZ, §4) | «ровно 20 × (10 + правки) измерений» | 240 (200 обычных + 40 архивных); правки — UPDATE той же строки | правка не создаёт строку (§6.3); архив добавлен в флоу бота |
| §22 (TEST TZ, §5) | magic link через `/doctor/login` | `GET /doctor?token=…`; `/doctor/login` — вход по паролю | так устроен кабинет врача (§21) |
| §22 (TEST TZ, §8) | «последняя правка победила» | итог — один из наборов целиком, без смеси | порядок конкурентных правок не определён |
| §22 (TEST TZ, критерии) | 0 ответов 5xx и 429; p95 — дашборд и CSV измерений | 0 ответов 5xx, 403 и 429; p95 — `/`, `/?days=30` и оба CSV | 403 — признак поломки CSRF/сессии; пороги — для всех экранов пользователя |
| §22 (TEST TZ, §9) | напоминание через `m:rem:snooze` | слот на «сейчас + 2 мин»; БД флешмоба — `data/flashmob.sqlite3` | такого callback нет (`rem:snooze:{id}` — через 30 мин); живые и нагрузочные данные не смешиваются |
| §22 (TEST TZ, зависимости) | `[project.optional-dependencies].load` | `[dependency-groups].load` и `.qa`; `qa/requirements.txt` убран | группы uv не публикуются как extra; одна точка правды |
| §22 (TEST TZ, структура) | — | + `test_load_admin.py`, `test_load_screenshots.py`, `tests/test_qa_seed.py` | админка заявлена в цели без сценария; скриншоты без файла; код сидов покрывается тестами (§13) |
| §16.2, §21 | — | унификация UX кабинета: карточки визитов, аккордеон истории по дням, единые роли кнопок и словарь подписей | решение заказчика (2026-11-11, `doc/CABINET-TABLES-UX-QUESTIONS.md`) |
| §11, §18 | — | канон внешнего вида бота и «Крупный режим» (0.5.10): шаблон экранов, словарь шапок, пары кнопок, HTML точечно, `users.ui_large`, оформление админки по шаблону | решение заказчика (2026-11-12, `doc/BOT-UI-UX-QUESTIONS.md`) |
| §4.17, §6.13, §10.1 | — | заключения врачей ручным вводом (текст 1–4000, фото — только Telegram `file_id`) и отметка о посещении из карточки (`ap:mark`); раздел «Заключения врачей» в PDF пациента | решение заказчика (2026-11-13, `doc/APPOINTMENTS-POSTVISIT-QUESTIONS.md`) |

## 18. Административная панель (Admin Panel)

**Безопасность:** Доступ к админ-панели имеют только Telegram ID, указанные в переменной `ADMIN_TELEGRAM_IDS`. Создается `AdminFilter`, который блокирует любые запросы к `/admin` от обычных пользователей (ответ: «Доступ запрещен»). Все действия администратора логируются.

**Приватность:** Администратор **не должен** видеть личные медицинские данные пользователей (цифры давления, заметки, список лекарств). Админ-панель показывает только агрегированные метрики и техническую информацию.

**Оформление (0.5.10, UI14).** Экраны админки — по канону §11: шапка `[эмодзи] Заголовок` (HTML `<b>`), тело секциями, кнопки действий → «Назад» / «В меню». Крупный режим (`users.ui_large`) на админку **не распространяется** — она одинакова для всех.

### 18.1. Главное меню админки
Команда `/admin` открывает inline-клавиатуру (по одной кнопке в строке, UI17):
`📊 Статистика БД` · `💾 Бэкапы` · `🩺 Здоровье системы` · `📢 Рассылка` · `👤 Поиск пользователя` · `В меню`

### 18.2. Статистика БД (Database Stats)
Показывает сводку по таблицам (без раскрытия содержимого):
- **Общие метрики:** Всего пользователей, активных за 24ч (DAU), за 7 дней (WAU). Всего записей давления. Всего активных лекарств.
- **Размер БД:** Размер основного файла `.sqlite3`, размер WAL-файла. (Реализуется через `PRAGMA page_count` * `PRAGMA page_size` или `os.path.getsize`).
- **Популярность слотов:** Сколько пользователей включили утренние, дневные и вечерние напоминания.
- **Согласия:** «Дали согласие: N» (действующее основное согласие); с 0.5.1 — «Заполнили данные: N» (§6.12). ФИО, даты рождения и прочие персональные данные пациентов и врачей не выводятся.
- **Визиты** (0.5.8): «Визиты и анализы: запланировано N» — `COUNT(*)` строк `appointments` со `status='planned'`; названия, даты и заметки не выводятся.
- **Кнопка «Обновить»** — пересчитывает метрики (кэширование на 5 минут, чтобы не нагружать БД).

**Формат (0.5.10, UI15).** Сводка — секциями с заголовками-эмодзи, строки — «Показатель: значение» (одна строка, без выравнивания колонок), числа — с разделителем разрядов («1 240»). Секции и порядок: «👥 Пользователи» (всего, DAU, WAU), «📈 Записи» (измерения, активные лекарства), «💾 База данных» (основной файл, WAL), «⏰ Напоминания» («Утро: 540 · Обед: 210 · Вечер: 610»), «🩺 Согласия», «📅 Визиты». Первая строка — «Обновлено: ДД.ММ ЧЧ:ММ» в поясе админа, если его `telegram_id` зарегистрирован в `users`, иначе UTC. Кнопки: «🔄 Обновить» (кэш 5 минут) / «Назад» / «В меню».

### 18.3. Управление бэкапами (Backup Management)
Инструмент для работы с резервными копиями:
- **Статус последнего бэкапа:** Дата, время, размер, статус верификации (`integrity_check: OK`).
- **«Создать бэкап сейчас»:** Запускает `services/backup.py` в фоне. По завершении присылает отчет.
- **Список локальных копий:** Вывод последних 7 файлов (имя, размер).
- **«Скачать последний бэкап»:** Отправляет зашифрованный файл (например, `.zst.age`) прямо в чат админу как документ (если размер < 50 МБ, иначе выдает ошибку и ссылку на S3).
- **Статус off-site:** Успешно ли последняя копия отправлена в S3-хранилище.

### 18.4. Здоровье системы (System Health)
Технический дашборд:
- **Uptime:** Время с момента запуска бота (из `time.monotonic()` или `psutil`).
- **Версия:** Текущая версия приложения (из `pyproject.toml`) и версия Python.
- **Планировщик (APScheduler):** Количество активных задач (например, «В очереди: 1 452 напоминания»). Статус: Running/Stopped.
- **Ошибки:** Количество ошибок в логах за последние 24 часа (парсинг `logging` или счетчик в памяти).

### 18.5. Массовая рассылка (Broadcast)
- Админ вводит текст (с поддержкой Markdown).
- Бот показывает превью сообщения и кнопку «Подтвердить отправку». Превью показывает текст как есть, без применения Markdown (текст рассылки в HTML-сообщении экранируется — UI5); разметка Markdown применяется при доставке.
- При подтверждении: отправка идет с задержкой `await asyncio.sleep(0.05)`, чтобы не превысить лимиты Telegram (30 сообщений/сек).
- В процессе админ видит обновляемое сообщение прогресса (0.5.10, UI16): «📢 Рассылка\nОтправлено {N} из {M} ▰▰▰▱▱ {P}%» — полоса из 10 символов (`filled = round(N/M*10)`, `{P} = round(N/M*100)`). `edit_message_text` — не чаще 1 раза в секунду (throttle по `time.monotonic()`); ошибка редактирования — в лог, рассылку не прерывает. Рассылка на ≤ 20 получателей — без промежуточных перерисовок, только финальный отчёт.
- Ошибки (пользователь заблокировал бота) логируются, но не прерывают рассылку. В конце — отчет: «Доставлено: X, Заблокировали: Y».

### 18.6. Поиск пользователя (Анонимизированный)
- Ввод `telegram_id` или `@username` (если есть).
- Вывод: ID, дата регистрации, часовой пояс, количество записей давления, статус онбординга, включены ли напоминания.
- Плюс: «Согласие: дано ДД.ММ.ГГГГ» / «Согласия нет».
- **Строго запрещено:** вывод цифр давления, заметок, названий лекарств, ФИО, даты рождения, пола (0.5.1) и e-mail.
- Кнопка «Отправить тестовое сообщение» (проверить, доходят ли уведомления до пользователя).

## 19. Удаление аккаунта (Right to be forgotten)

В разделе «Настройки» появляется кнопка «Удалить аккаунт». Бот предупреждает: «Это действие необратимо...». При подтверждении:
1. **Важно:** Сначала отменяются все джобы из APScheduler для этого пользователя (чтобы планировщик не упал с ошибкой `User not found` при попытке отправить сообщение).
2. Удаляется запись `users` (срабатывает `ON DELETE CASCADE` для `measurements`, `reminders`, `medications`, `medication_logs`, `web_users`, `web_sessions`, `doctor_patients`, `invite_log`, `doctor_invites`, `consents`, `appointments` — с 0.5.8, `appointment_conclusions` — с 0.5.11 (каскадом через `appointments`); job'ов у событий нет, уведомления — тиком по строкам, §6.13). Запись `doctors` не удаляется: врач — не пациент, его аккаунт живёт отдельно.
3. Бот отправляет последнее сообщение: «Ваш аккаунт и все данные удалены».
4. **Журнал удалений** (0.5.0, `bot/services/deletion_log.py`): если включены бэкапы (`BACKUP_ENCRYPTION_KEY`), в файл `DELETION_LOG_PATH` дописывается строка `HMAC-SHA256(DELETION_LOG_KEY, telegram_id)` + дата UTC — без других данных. Файл — вне БД: восстановление копии его не перезаписывает. Строки старше 15 дней удаляются (копий старше 14 дней нет, §20). После ручного восстановления копии: `python -m bot.services.deletion_log --apply` — удаляет аккаунты из журнала (с отменой джобов, как в п. 1).

Тот же порядок — при отзыве основного согласия (§6.11). Отзыв в кабинете (К5): бот раз в час (джоб планировщика, UTC) удаляет аккаунты с `deletion_requested_at IS NOT NULL` по пп. 1–2 и 4, без последнего сообщения в Telegram. «Не согласен» нового пользователя удаляет только что созданную запись `users` без журнала (его данных ещё нет в копиях).

## 20. Резервное копирование (Backup)

Реализуется в `bot/services/backup.py`. Запускается по расписанию (в 03:00 UTC) и вручную через админ-панель.
- **Создание:** только `VACUUM INTO` или Online Backup API (без `cp`).
- **Проверка:** `PRAGMA integrity_check` на самом файле копии.
- **Шифрование:** `age` или AES-GCM (ключ `BACKUP_ENCRYPTION_KEY`).
- **Отправка:** S3-совместимое хранилище (off-site).
- **Retention:** 7 ежедневных + 1 еженедельная (GFS); дополнительно удаляется любая копия старше 14 дней — удалённые аккаунты не живут в копиях дольше 15 дней (Закон № 99-З, ст. 10, 13; до 0.5.0 — 7 + 4). Повторное удаление после восстановления — журнал удалений (§19).
- **Совместимость:** при восстановлении проверять `PRAGMA user_version` (бэкап не должен быть новее текущей схемы).

## 21. Кабинет врача (Doctor Portal)

**Визиты, анализы и заключения пациента (§6.13, §4.17, 0.5.8/0.5.11, В19, В34) врачу не показываются** — ни в списке, ни в карточке, ни в PDF и CSV (в PDF врача нет раздела «Заключения врачей», У2-А). Показ — только отдельной задачей с правкой §6.11 и `privacy.html`.

**Логика для пациента:**
1. В разделе «Настройки» есть кнопка «Мой врач» (и команда `/doctor`). У пациента один врач: пока есть связь `pending` / `active`, второго добавить нельзя (сначала «Отозвать доступ»).
2. Врача нет → сначала согласие на врача (Б7, §6.11), затем экран выбора способа: «📧 По e-mail» / «✈️ Через Telegram» / «Назад». Врач есть → список (`• {ФИО или @username или e-mail} — {статус}`) и «Отозвать доступ».
3. **По e-mail.** Пациент вводит e-mail → создаётся (или находится) врач и связь `doctor_patients` (`pending`) → врачу уходит письмо со ссылкой (если настроен SMTP, §3), пациенту — та же ссылка для пересылки. Открытие ссылки врачом — `status='active'`.
4. **Через Telegram.** Бот выдаёт ссылку-приглашение `https://t.me/<бот>?start=doc_<code>` (§4.13, 72 часа, одноразовая) для пересылки врачу в Telegram и кнопку «🔎 Найти по @username». Поиск — только среди врачей (`doctors.telegram_username`), ответ «не найден» одинаков для «нет в боте» и «не врач»: по боту нельзя проверить, пользуется ли им человек. Найден — бот сам присылает врачу приглашение.
5. Приглашение у врача: «Принять» → врач (по `telegram_id`; новый — с именем «имя фамилия» из профиля Telegram, ≤ 100) и связь `active`, код списан, пациенту — уведомление; «Отклонить» → код списан, связь не создаётся, пациенту — уведомление.
6. Кнопка «Отправить отчет» в главном меню: врач с e-mail — письмо со ссылкой + та же ссылка пациенту; врач в Telegram — бот присылает врачу уведомление с кнопкой «Открыть в кабинете», пациенту — подтверждение и ссылка для пересылки. Без SMTP — как прежде, ссылка пациенту для пересылки.
7. «Отозвать доступ» → `status='revoked'`; врачу в Telegram — уведомление.

**Ссылки кабинета врача — два вида с разными сроками:**
- **Ссылка доступа к пациенту** (payload: врач + пациент) — **72 часа**, одноразовая (константа `DOCTOR_ACCESS_MAX_AGE`, прежняя `DOCTOR_TOKEN_MAX_AGE` = 15 минут для неё отменяется). Выдаётся при добавлении врача по e-mail, повторном добавлении и «Отправить отчет». Ссылка в письме и ссылка для пересылки пациенту — **один и тот же токен**: кто бы ни открыл первым, второй раз он не сработает. Открытие активирует связь (`status='active'`).
- **Ссылка входа врача** (payload: только врач) — **15 минут**, одноразовая (константа `DOCTOR_LOGIN_MAX_AGE`). Выдаётся только из меню врача в боте («🩺 Открыть кабинет врача», «Открыть в кабинете»), связей не меняет. Payload — врач, `jti` и необязательный пациент: «Открыть в кабинете» ведёт сразу в карточку пациента (доступ всё равно проверяется по `status='active'`). Открывается через ту же страницу подтверждения: заголовок «Кабинет врача», текст «Вход в кабинет врача.», кнопка «Войти» (POST `/doctor/signin`).
- Соль подписи ссылки доступа — `doctor-access` (с 0.4.12): ссылки, подписанные прежней солью `doctor-login` (до 0.4.11 — 15 минут, списание в памяти процесса), не принимаются, иначе продление срока «оживило» бы уже использованные. У ссылки входа врача — своя соль `doctor-signin`.
- Обе списываются в `used_tokens` (§4.14) — списание переживает перезапуск кабинета. Magic link личного кабинета пациента (`/cabinet`, 15 минут, §16.2) не меняется.
- **Страница подтверждения.** `GET /doctor?token=…` ссылку не списывает: показывает «Кабинет врача» / «Пациент открыл вам доступ к дневнику давления.» и кнопку «Открыть дневник» (POST `/doctor/open` с CSRF). Списание, активация связи и сессия врача — только по кнопке; из двух одновременных нажатий проходит одно (`INSERT … ON CONFLICT DO NOTHING` по `jti`). Почтовые сканеры (Outlook Safe Links, антивирусы), открывающие ссылки из писем заранее, одноразовую ссылку не сжигают. Просроченная, списанная или битая ссылка — `/doctor?error=link_expired` («…новая ссылка действует 72 часа.»).

**Письма (SMTP):**
- Отправитель — `SMTP_FROM` (`Дневник гипертоника <noreply@electroman.by>`); у домена — записи SPF, DKIM и DMARC.
- Только текст (plain text, UTF-8). `smtplib` в `asyncio.to_thread` (§7.2 п.7), без повторов; ошибка — в лог без адреса и токена, пациенту — ссылка для пересылки.
- Когда: добавление врача по e-mail (в т.ч. повторное после отзыва), «Отправить отчет».
- Лимиты (§4.12): не больше 3 писем на пациента в сутки и 1 письма на один адрес в час; сверх лимита — письмо не отправляется, пациенту — ссылка для пересылки.

**Врач в боте:**
- Регистрация: по ссылке-приглашению («Принять») или командой `/iamdoctor` (ФИО ≤ 100 → врача можно найти по @username). Онбординг пациента врач не проходит.
- `telegram_username` обновляется при каждом сообщении врача боту (`DoctorUsernameMiddleware`: сначала чтение, запись — только если username сменился). Схема БД — поколение 3 (`PRAGMA user_version`).
- `/start` без параметра: прошёл онбординг пациента — меню пациента + кнопка «🩺 Кабинет врача» (если он врач); не проходил, но врач — меню врача.
- Меню врача: «🩺 Открыть кабинет врача» (ссылка входа, 15 минут) · «👥 Мои пациенты» (только `active`; у каждого — «Открыть в кабинете») · «Вести свой дневник давления» (онбординг пациента; только если он не пройден).
- Уведомления врачу: пациент нажал «Отправить отчет»; пациент отозвал доступ.
- Лимит приглашений по @username: не больше 3 на пациента в сутки и 1 в час одному врачу (§4.12, `channel='telegram'`).

**Тексты (дословно):**

Пациент:
- Врача нет: «Врач пока не добавлен.\nКак пригласить врача?» — кнопки «📧 По e-mail», «✈️ Через Telegram», «Назад».
- Ввод e-mail, SMTP настроен: «Отправьте e-mail врача сообщением — отправлю ему приглашение.»; SMTP не настроен — «Отправьте e-mail врача сообщением — подготовлю ссылку для него.»
- Письмо ушло: «Приглашение отправлено на {email}.\nЕсли письмо не дойдёт, перешлите врачу сообщение ниже:\n\nПациент {name} предоставил вам доступ.\n{url}»
- Письмо не ушло: «Не удалось отправить письмо. Перешлите врачу сообщение ниже:\n\nПациент {name} предоставил вам доступ.\n{url}»
- Лимит писем: «Письмо уже отправлено недавно. Перешлите врачу сообщение ниже:\n\nПациент {name} предоставил вам доступ.\n{url}»
- SMTP не настроен: прежний текст «Готово! Перешлите врачу сообщение ниже — по ссылке он получит доступ к вашим измерениям:\n\nПациент {name} предоставил вам доступ.\n{url}»
- Через Telegram: «Перешлите врачу в Telegram ссылку-приглашение — он нажмёт «Старт» и подтвердит доступ:\n{invite_url}\n\nСсылка действует 72 часа. Если врач уже пользуется ботом, можно найти его по имени пользователя.» — кнопки «🔎 Найти по @username», «Назад».
- Ввод @username: «Напишите имя пользователя врача в Telegram, например @ivanova_doc:»
- Неверный @username: «Имя пользователя — от 5 до 32 латинских букв, цифр и «_». Попробуйте ещё раз:»
- Найден: «Приглашение отправлено врачу в Telegram. Сообщу, когда он ответит.»
- Не найден: «Врач с таким именем не найден. Перешлите ему ссылку-приглашение:\n{invite_url}»
- Лимит приглашений: «Приглашение уже отправлено недавно. Перешлите врачу ссылку-приглашение:\n{invite_url}»
- Врач принял: «Врач {doctor} принял приглашение — доступ открыт.»
- Врач отклонил: «Врач не принял приглашение. Можно пригласить другого врача: «Настройки» → «Мой врач».»
- «Отправить отчет», письмо ушло: «Ссылка отправлена врачу на {email}. Можно также переслать её: {url}»
- «Отправить отчет», письмо не ушло или лимит: «Не удалось отправить письмо. Перешлите ссылку врачу сами: {url}»
- «Отправить отчет», врач в Telegram: «Отчёт отправлен врачу в Telegram. Можно также переслать ссылку: {url}»
- «Отправить отчет» без SMTP (врач по e-mail): прежний текст «Ссылка для вашего врача сформирована. Перешлите её ему: {url}»

Врач:
- Приглашение: «Пациент {name} открыл вам доступ к дневнику давления: измерения, графики, отчёт.\nПринять приглашение?» — кнопки «✅ Принять», «Отклонить».
- Код недействителен: «Приглашение устарело или уже использовано. Попросите пациента прислать новое.»
- Принял: «Доступ открыт. Пациент появится в «Мои пациенты».» + меню врача.
- Отклонил: «Приглашение отклонено.»
- Меню врача: «Меню врача» — кнопки «🩺 Открыть кабинет врача», «👥 Мои пациенты», «Вести свой дневник давления».
- Ссылка входа: «Ссылка для входа в кабинет врача (действует 15 минут): {url}»
- Мои пациенты: «Мои пациенты:» — кнопки «Пациент {telegram_id}» (как в кабинете); пусто — «Пока нет пациентов с открытым доступом.» С 0.5.1 (§6.12) — кнопки «Иванов И. И., 67 лет»; без данных — имя из Telegram или «Пациент {telegram_id}», сортировка — как в кабинете. В крупном режиме (`users.ui_large=1`) — пагинация по 5, кнопка «Показать ещё {min(5, N − limit)}» (`doc:patients:{limit}`, §8; UI13).
- Отчёт: «Пациент {name} отправил вам отчёт.» — кнопка «Открыть в кабинете». С 0.5.1 `{name}` — короткая подпись «Иванов И. И.», если данные заполнены (иначе имя из Telegram).
- Отзыв: «Пациент {name} закрыл вам доступ к своему дневнику.» — `{name}` так же.
- `/iamdoctor`, не зарегистрирован: «Регистрация врача. Напишите ваше ФИО — его увидят пациенты:»
- `/iamdoctor`, зарегистрирован: «Вы зарегистрированы как врач: {name}. Изменить ФИО?» — кнопки «Изменить», «Назад».
- Неверное ФИО: «ФИО — от 2 до 100 символов. Попробуйте ещё раз:»
- ФИО сохранено, есть username: «Готово! Пациенты могут найти вас по @{username}.»
- ФИО сохранено, username нет: «Готово! Чтобы пациенты могли найти вас по имени пользователя, задайте его в настройках Telegram. Пригласить вас можно и ссылкой.»

Письмо (`{name}` — имя пациента из Telegram, `{срок}` — «72 часа»):

```
Тема: Пациент открыл вам доступ к дневнику давления

Здравствуйте!

Пациент {name} открыл вам доступ к своему дневнику давления
в «Дневнике гипертоника».

Посмотреть измерения, графики и отчёт:
{url}

Ссылка действует {срок} и открывает доступ один раз.
Если вы не знаете этого пациента — просто не открывайте ссылку,
доступ без вашего входа не появится.

—
Дневник гипертоника · https://bp-diary.electroman.by
```

**Логика для врача (Web UI):**
1. Врач переходит по ссылке-приглашению (или заходит на `/doctor`).
2. Видит панель «Doctor Dashboard»:
   - Список пациентов (только тех, кто дал доступ).
   - При выборе пациента — графики давления (Chart.js, три панели: «Систолическое», «Диастолическое», «Пульс»; у панелей САД и ДАД — те же цветные зоны и пороги, что в личном кабинете, §16.2), таблица измерений, статистика.
   - История записей пациента — тем же компонентом, что в личном кабинете (общий partial `partials/history_day.html`, 0.5.9, §16.2): аккордеон по дням с заголовком дня (число записей, средние, точка зоны), свёртка по умолчанию и «Показать ещё 30 дней» как в ЛК, та же подсветка строк по зонам давления и легенда; без колонки действий (только чтение).
   - **Средние по дням** (0.5.3) — тот же блок и в том же месте сводки карточки пациента, что в ЛК (общий partial `partials/daily_averages.html`, §16.2), за текущий фильтр карточки; сутки — по часовому поясу пациента; только чтение. Доступ — те же условия, что у остальной карточки (`status='active'` и действующие согласия, ниже «Безопасность»).
   - Кнопка «Скачать CSV» / «Скачать PDF» (`GET /doctor/patient/{id}/export.pdf`; период — текущий фильтр карточки; формат — общий отчёт §10.1, в шапке «Пациент {telegram_id}»; с 0.5.1 — подпись и имя файла по §10.1).
   - **Данные пациента (0.5.1, §6.12):** список пациентов — «Иванов Иван Иванович · 12.03.1958 (67 лет) · м», сортировка по фамилии, пациенты без данных — в конце: «{имя из Telegram или «Пациент {telegram_id}»} — данные не заполнены». Заголовок карточки — «Иванов Иван Иванович, 67 лет — сводка за период» и строка «Дата рождения: 12.03.1958 · Пол: мужской»; без данных — «{имя из Telegram или «Пациент {telegram_id}»} — сводка за период» и строка «Данные не заполнены».
   - Поле для заметок врача (сохраняется в БД и видно пациенту в его ЛК).
3. Врач не может редактировать записи пациента, только просматривать и комментировать.

**Безопасность:**
- Врач получает доступ только к тем пациентам, кто явно дал согласие: `status='active'` **и** действующая `consents(kind='doctor')` **и** действующее согласие самого врача (§6.11). Иначе пациент врачу не отображается.
- Пациент может отозвать доступ в настройках (`status='revoked'`).

## 22. Автоматическое тестирование (QA Bot)

**Цель:** Скрипт на тестовом сервере проверяет весь функционал бота и Web UI, делает скриншоты и отправляет отчет администратору.

**Инструменты:**
- **Telegram E2E:** `Telethon` (как юзербот) или `aiogram` (как второй бот) для прохождения онбординга, записи давления и проверки ответов.
- **Web E2E:** `Playwright` (Python) для открытия браузера, логина в ЛК, проверки графиков и создания скриншотов.
- **Runner:** `pytest`.

**Сценарии проверки:**
1. **Онбординг:** Регистрация, ввод часового пояса, выбор целевых значений.
2. **Запись давления:** Эмуляция нажатия кнопок, проверка сохранения в БД.
3. **Напоминания:** Ускоренная проверка (задать время напоминания на через 1 минуту) — проверка отправки сообщения.
4. **Web UI:** Логин по magic link, проверка отображения графиков (три панели), экспорт CSV.
5. **Кабинет врача:** Отправка отчета, вход по ссылке, проверка видимости данных.

**Оркестрация:**
- Запуск в Docker (`qa/` содержит свой `Dockerfile` и `docker-compose.qa.yml`).
- Результат: папка `qa/reports/screenshots/` с PNG-файлами + HTML-отчет `pytest --html=report.html`.
- В конце — отправка краткого отчета админу в Telegram: «✅ Все тесты пройдены. Скриншоты: [ссылка]».

### Нагрузочное тестирование (20 одновременных пользователей)

Цель — доказать, что система стабильно работает при **20 одновременных активных пользователях** и базе на **5000 измерений** (плюс лекарства, логи, напоминания, врачи) без потери корректности данных, без ошибок 5xx/403/429 и без дублей. Исходная постановка — `doc/TEST TZ.md`, протокол принятых решений — `doc/LOAD-TESTS-QUESTIONS.md`; при расхождении приоритет у этого раздела (отличия — §17).

**Ограничения.**
- **Только тестовый стенд.** Нагрузочный код (сиды и тесты) работает только с БД, в имени файла которой есть `test-stand`; иначе — отказ без возможности обхода. Стенд — `data/test-stand.sqlite3` (переменная `LOAD_DB`), во временных БД тестов файл тоже называется `test-stand.sqlite3`. Живой флешмоб — на отдельной БД `data/flashmob.sqlite3`, нагрузочный код в неё не пишет.
- **Без реального Telegram API**, кроме ручного флешмоба. Бот в нагрузке — `Dispatcher.feed_update` с подменённой сетевой сессией `Bot` (ответы Telegram — фейковые), Telethon для нагрузки не используется.
- **Прод-код не меняется.** Всё, что нужно тестам (фейковые часы middleware, сдвиг `measurements.utcnow`, мок-сессия бота), делается в фикстурах `qa/conftest.py`.
- **Пароли** нагрузочных пациентов и врачей — только из `LOAD_PASSWORD`. Если переменная не задана, pytest генерирует случайный пароль на сессию (`secrets.token_urlsafe`), держит его только в памяти и нигде не пишет; сиды и Locust без `LOAD_PASSWORD` не запускаются.
- **Два профиля.** Без флага — дымовой: 3 пациента × 5 операций на временном **файле** SQLite с PRAGMA продакшена (WAL, `busy_timeout=5000`, `foreign_keys=ON`; in-memory не используется — в ней нет реальных блокировок). С `LOAD_TEST=1` — полный профиль. `qa/` в CI не входит (`testpaths = ["tests"]`).
- **Изоляция.** Один раз за сессию pytest собирается шаблонная БД через `seed_all` во временной папке (масштаб по профилю); каждый тест получает её копию через Online Backup API SQLite (`sqlite3.Connection.backup`).
- **Без `time.sleep`** — только `asyncio.sleep`, `asyncio.gather`, `asyncio.Event`.

**Сид данных (`qa/seed/`).** Масштаб — `--scale=full` (по умолчанию) или `--scale=smoke` (3 пациента, по 25 измерений, 1–2 лекарства и 1–2 напоминания на пациента, 3 врача). Полный масштаб:
- **20 пациентов** (`load{n}@load.test`, TG ID 990 001…990 020), у каждого `web_users` (Argon2id-хеш пароля из `LOAD_PASSWORD`).
- **5000 измерений** — 250 на пациента, по 30 последним дням, времена в таймзоне пользователя, значения: САД 100–180, ДАД 60–110, пульс 50–110. У трети записей заполнены `feeling`, `note`, `medication_taken`. Расписание детерминированное: «сидированная запись» распознаётся по паре «нагрузочный пользователь + момент из расписания».
- **100 лекарств** — 5 названий на пациента, у каждого 1–2 слота (строк `medications` — 100–200); ровно одно лекарство `crisis` на пациента.
- **500 `medication_logs`** — 25 на пациента, статусы `taken`, `skipped`, `partial`; без дублей по `(user_id, slot, локальная дата, medication_id)`, кроме `crisis`.
- **100 активных `reminders`** — 60 слотовых (3 слота × 20) и 40 `med_remeasure` (по 2 на пациента, к разным записям), `next_trigger_time` — в ближайший час.
- **20 врачей** `doctor{n}@load.test` с паролем из `LOAD_PASSWORD` и связями `doctor_patients` 1:1 со статусом `active`.
- **Администратор** — строка `users` для ID из `ADMIN_TELEGRAM_IDS` (если задан) и подсказка, какую строку добавить в `.env` стенда.

`seed_all.py` — оркестратор: users → measurements → medications → reminders → doctors → admin. По умолчанию — идемпотентное дозаполнение (повторный запуск ничего не дублирует); `--clean` сначала удаляет только нагрузочных пользователей (диапазон TG ID, остальное — каскадом) и врачей `*@load.test` (связи и заметки — каскадом). Код сидов покрыт юнит-тестом `tests/test_qa_seed.py` (идемпотентность, `--clean`, защита БД, объёмы) — входит в обычный прогон и CI.

**Уровни и сценарии.**
1. **HTTP-кабинет пациента — Locust (`qa/locustfile.py`).** 20 VU, вход 2/с, 5 минут, против кабинета без Docker: `DB_PATH=data/test-stand.sqlite3 uv run uvicorn web.cabinet.app:app --port 8000`. Веса задач: вход → дашборд → выход — 40; история за 30 дней (`/?days=30`) — 20; CSV измерений за 30 дней — 15; добавление измерения — 10 (время — `now − 1 ч − случайные минуты в пределах суток`, чтобы не попадать в дедуп); профиль — 10; добавление лекарства — 5; настройки напоминаний — 3; CSV лекарств за 30 дней — 2. После выхода следующая задача входит заново. «Графики» проверяются по наличию трёх `canvas` (`bp-chart`, `bp-chart-dia`, `bp-chart-pulse`) и их данных в HTML.
2. **Сервисный слой — `qa/test_load_burst.py`.** 20 пользователей × 50 конкурентных операций (`asyncio.gather`): массовая запись (1000 записей), двойные нажатия «Записать давление», конкурентные правки одной записи, одновременные «Принял всё ✓» на одном слоте, параллельные `/start`. Критерии: нет `database is locked` (с учётом повтора `run_with_retry`), нет дублей за минуту, все операции завершены, время < 60 с.
3. **Telegram-бот — `qa/test_load_bot.py`.** 20 виртуальных пользователей через `Dispatcher` (весь стек middleware, debounce и throttle — на фейковых часах); `measurements.utcnow` сдвигается фикстурой — каждая следующая запись на минуту позже. Флоу каждого: `/start` → 10 записей калькулятором → правка последней × 3 → «Приняли лекарство?» → «Да» → мультивыбор → «Готово» → 2 архивные записи (по кнопкам и быстрым вводом) → «Принял всё ✓» на напоминании слота → «Дневник за сегодня» → «Статистика» 7 и 30 дней → экспорт CSV. Критерии: все 20 проходят без исключений; ровно **240** измерений (200 обычных + 40 архивных, архивные — по своему моменту); правки — по значениям той же строки; `measurement_medications` заполнена; `medication_logs` «Принял всё ✓» — ровно один набор на слот.
4. **Кабинет врача — `qa/test_load_doctor.py`.** 20 врачей одновременно входят по ссылке доступа (`GET /doctor?token=…` — страница подтверждения, `POST /doctor/open` — вход; токен генерирует тест тем же `DoctorMagicLink`), открывают карточку своего пациента, делают 5 выгрузок (CSV и PDF вперемешку) и оставляют 3 комментария — ровно 220 запросов через `httpx.ASGITransport`. Критерии: все 220 успешны; p95 карточки < 2 с; комментарии привязаны к своим `doctor_id`/`user_id`. Перед пачкой — калибровка без порога: последовательные GET одного врача и CPU-микробенчмарк; она пишется в JSON и в сообщение о падении, чтобы троттлинг машины отличался от медленного кода. Приёмочный прогон — на машине без троттлинга или на сервере.
5. **Напоминания — `qa/test_load_reminders.py`.** 100 активных напоминаний с `next_trigger_time = now + 1 с`; настоящий `AsyncIOScheduler` с `SQLAlchemyJobStore` на временном файле, ожидание доставок через `asyncio.Event`; отправки ловит мок `Bot.send_message`. Критерии: ровно 100 сообщений, нет дублей по `(user_id, type)` (для `med_remeasure` — по `measurement_id`), `last_sent_date` обновлён у всех, `next_trigger_time` слотов пересчитан на завтра (`med_remeasure` после доставки снимается), двойной тик ничего не отправляет повторно.
6. **Бэкап под нагрузкой — `qa/test_load_backup.py`.** `BackupService.create_backup()` во время burst (20 × 50 записей), ключ шифрования — тестовый, S3 выключен. Критерии: копия создана и расшифровывается; `PRAGMA integrity_check` = `ok`; число строк ключевых таблиц не меньше снимка на старте бэкапа; проба строго по расписанию раз в 100 мс на всё окно нагрузки, каждая запись — отдельной задачей, не дожидаясь предыдущей (медленная проба не вытесняет быстрые из выборки) — задержка event loop < 5 с и p95 записи мимо лока дедупа < 1 с (max не ограничивается: редкие выбросы busy-обработчика SQLite проходят повтором `run_with_retry`; очередь сохранений за локом дедупа меряет burst-тест). Калибровка машины (CPU-микробенчмарк, множитель к эталону) — в JSON и в сообщении о падении, без порога.
7. **Конкурентные гонки — `qa/test_load_concurrent.py`.** Гонки §7.2 в масштабе (20 пользователей × 5–10 конкурентных вызовов): `save_measurement` с одинаковыми данными → одна запись; `med_remeasure` на одном `measurement_id` → один job; «Принял всё ✓» на слот → ровно N логов, не 2N; правки одной записи → итог — один из наборов целиком, без смеси, `timestamp` не меняется; параллельные `/start` → одна строка `users`.
8. **Админ-панель — `qa/test_load_admin.py`.** «Статистика БД» на полном объёме (только `COUNT(*)` по `measurements`), поиск пользователя, рассылка на 20 получателей с `Semaphore(20)` и частью `TelegramForbiddenError` (отчёт «Доставлено / Заблокировали»).
9. **Скриншоты — `qa/test_load_screenshots.py`** (`LOAD_TEST=1`, Playwright против запущенного кабинета стенда): дашборд пациента с тремя графиками, дашборд врача (вход по magic link, `WEB_SECRET_KEY` — из `.env` стенда), история за 30 дней → `qa/reports/screenshots/`.
10. **Живой флешмоб** — ручной чек-лист в `qa/README.md`: бот флешмоба — `DB_PATH=data/flashmob.sqlite3 uv run python -m bot` с токеном **тестового** бота; 20 участников регистрируются, проходят онбординг (`Europe/Minsk`, цель 130/80), делают 5 записей, добавляют 2 лекарства, ставят любой слот напоминания на «сейчас + 2 мин», дожидаются его и отвечают «Записать давление» или «Принял всё ✓» («Через 30 мин» — только нажатие с проверкой ответа), экспортируют CSV; проверка — SQL на `flashmob.sqlite3`: 100 измерений, 40 лекарств (`COUNT(DISTINCT user_id, name)`); длительность ≤ 10 минут.

**Критерии приёмки — `qa/test_load_regression.py`** (только под `LOAD_TEST=1`, в дымовом прогоне — SKIP). Каждый нагрузочный тест сам проверяет свои критерии и пишет итог в `qa/reports/load/<тест>.json`; в начале полного прогона (сессия `LOAD_TEST=1`, в которой есть хотя бы один `test_load_*`, кроме регрессии) старые `*.json` удаляются (Locust CSV не трогается). Регрессия собирает JSON (нет файла — FAIL «не запускался»), Locust CSV (нет — проверки Locust SKIP) и `test-stand.sqlite3`; при нарушении — понятное сообщение с id нарушителя.

| Метрика | Порог | Источник |
|---|---|---|
| HTTP 5xx, 403, 429 | 0 | Locust CSV |
| p95 `/` и `/?days=30` | < 2000 мс | Locust CSV |
| p95 CSV измерений и лекарств за 30 дней | < 3000 мс | Locust CSV |
| Дубли измерений за минуту | 0 | SQL (стенд) и JSON тестов |
| Дубли `medication_logs` по `(user_id, slot, локальная дата, medication_id)`, кроме `crisis` | 0 | SQL и JSON |
| Дубли отправленных напоминаний | 0 | JSON `test_load_reminders` |
| `integrity_check` после бэкапа | `ok` | JSON `test_load_backup` |
| Нагрузочные пациенты в `users` | 20 | SQL (стенд) |
| Сидированные измерения на месте | 5000 (новые от Locust допускаются) | SQL (стенд) |

**Приёмка перед релизом — `qa/test_acceptance_*.py`** (новое ТЗ 2026-11-14,
ответы — `doc/LOAD-CRITERIA-QUESTIONS.md`). Только под `ACCEPTANCE=1`, на своей
машине: перед выпуском версии и после любой правки, касающейся хранения данных;
в обычный цикл разработки и в каждую сдачу не входят (длинные — часовая soak).
Модули не входят в `testpaths` (`tests`) и в CI никогда не запускаются: только
явной командой `make acceptance` или `ACCEPTANCE=1 pytest qa/test_acceptance_*.py`.
Каждый модуль независим (своя копия стенда через `sqlite3.Connection.backup`) и
пишет JSON в `qa/reports/load/acceptance_<тест>.json`; регрессия
`test_acceptance_regression.py` запускается **отдельной командой после** модулей
и собирает артефакты (нет файла — FAIL «не запускался»). Запуск — `make
acceptance` (модули, затем регрессия); порядок команд и ожидаемое время —
`qa/README.md`, раздел «Перед релизом». Данные генератора объёма —
**вымышленные** (не выгрузка заказчика).

| Критерий | Проверка | Порог (FAIL) |
|---|---|---|
| Полный объём | генератор ~50 000 записей за 3 года (20 пациентов × ~2500 измерений; плюс лекарства, логи приёма, визиты и заключения — 3 года назад + 2 года вперёд, §6.13; `reminders`, `doctor_patients`, `web_sessions`, `invite_log` не масштабируются); затем обычные операции | время отклика — «число из Т3» (пороги `doc/TEST TZ.md`): p95 дашборда < 2000 мс, p95 CSV < 3000 мс |
| Окно конкуренции (связка контуров) | 100 одновременных обычных операций на полной базе (`asyncio.gather`: дашборд, история, CSV) против **реального** сервера кабинета — `uvicorn --workers 2`, прод-конфигурация `docker-compose.yml` (один процесс сериализует CPU-рендер) | 0 ошибок; p95 окна — те же пороги «числа из Т3» (2000/3000 мс) |
| Деградация | 1000 операций подряд на копии стенда; медиана первых 10 **после прогрева** (первая операция — прогрев, не в счёт) против медианы последних 10; операции: «сохранение измерения» со сдвигом времени на минуту (дедуп §7.2), «дашборд» ×1000, «CSV за 30 дней» × 200–300; каждая операция — отдельная проверка | медиана последних 10 > 1,5 × медианы первых 10 (по каждой операции) |
| Длительная работа | 60 минут нагрузки на копии стенда; RSS бота и кабинета раз в минуту в `acceptance_memory.jsonl`; первые 10 минут из регрессии исключаются; память Locust-клиента не меряется | наклон линейной регрессии RSS **каждого** процесса > 0,5 МБ/мин; `tracemalloc` раз в 5 минут — только warning > 1 МБ/мин (Python-heap) |
| Возврат памяти | прогрев 100 циклов, замер RSS после `gc.collect()`, 3000 циклов «сохранение с шагом минута → чтение истории»; после 60 с покоя — `gc.collect()`, затем замер | RSS не вернулся: отклонение > ±10% или > ±20 МБ (по большему); `tracemalloc` (снимки 1 и 3000): прирост Python-heap > 5 МБ |
| Рост файлов и базы | прирост БД + WAL + jobstore и файлов (бэкапы, deletion log) за симулированные сутки × 1095 (3 года); перед замером — `PRAGMA wal_checkpoint(TRUNCATE)` и `scheduler.shutdown(wait=True)` (иначе WAL и jobstore дадут «хвост» активной нагрузки); прогноз — в отчёт информационно, без FAIL | БД + WAL + jobstore > 200 МБ; файлы на диске (бэкапы + deletion log) > 500 МБ |
| Остановка | штатное завершение (SIGTERM / `shutdown()`) под нагрузкой; в `docker-compose.yml` — `stop_grace_period: 30s` для `bot` и `cabinet` (иначе SIGKILL через 10 с) | завершение > 30 с; остались потоки с именами `asyncio*` / `sqlalchemy*` / `aiosqlite*` после `scheduler.shutdown(wait=True)` (бот) и `engine.dispose()`; `-wal` остался без владельца после остановки **обоих** процессов (бот и кабинет) |

**Логи и отчёты.** Логгер `qa.load`: JSON-строки (`user_id`, `op`, `duration_ms`, …) в `qa/reports/load/qa-load.jsonl` (перезаписывается в начале полного прогона) и краткая сводка в консоль. Артефакты (`qa/reports/`) в git не попадают; выдержки и раздел «Найденные узкие места» — в `qa/README.md`.

**Зависимости.** `[dependency-groups].load` — `locust`, `pytest-html`, `playwright`; `[dependency-groups].qa` — `telethon` и `{include-group = "dev"}`. `qa/requirements.txt` не используется: `qa/Dockerfile` ставит `uv sync --frozen --group qa --group load`. `httpx` — в `dev`.

**Запуск:**

```bash
# 1. Сид стенда (полный масштаб; --clean — пересоздать нагрузочные данные)
LOAD_PASSWORD='<пароль стенда>' \
    uv run python qa/seed/seed_all.py --db data/test-stand.sqlite3

# 2. Дымовой прогон (без флага, секунды)
uv run pytest qa/ -v

# 3. Полный набор нагрузочных тестов
LOAD_TEST=1 uv run pytest qa/ -v -s --ignore=qa/locustfile.py

# 4. HTTP-нагрузка кабинета (кабинет запущен на стенде)
DB_PATH=data/test-stand.sqlite3 uv run uvicorn web.cabinet.app:app --port 8000
LOAD_PASSWORD='<пароль стенда>' \
    uv run locust -f qa/locustfile.py --host=http://127.0.0.1:8000 \
    -u 20 -r 2 -t 5m --headless --csv=qa/reports/load/locust

# 5. Регрессия критериев приёмки
LOAD_TEST=1 uv run pytest qa/test_load_regression.py -v
```
