# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Проект

Telegram-бот «Дневник гипертоника» — личный дневник измерений давления. Несмотря на расположение
папки в дереве Qt, проект на Python, Qt к нему отношения не имеет.
Репозиторий: https://github.com/StarLeG/my-bp-diary.git

Полное ТЗ — `doc/SPEC.md`. Оно же описывает процесс: сначала расширенный SPEC.md (схема БД,
сценарии, переменные окружения, структура файлов), затем вопросы автору, и только после
утверждения — код. Кода в репозитории пока нет.

## Стек

Python 3.11+, aiogram 3.x, SQLite на старте (подключается через SQLAlchemy 2.0 async),
Pydantic для валидации, python-dotenv для конфига. Код типизирован (type hints), с docstring'ами.

## Ключевые принципы UX

- Минимум трения: ввод давления — клавиатурой-калькулятором (цифры, «⌫», «C», «Готово») или числом в чат. Кнопки вместо свободного ввода везде, где это возможно.
- Редактировать существующее сообщение (`edit_message_text` / `edit_message_reply_markup`), а не отправлять новые — чтобы не плодить визуальный шум.
- Не использовать FSM там, где можно обойтись `callback_data`.
- Все тексты — на русском, дружелюбные, без медицинского жаргона и без медицинских советов. При давлении ≥180/110 — вежливо предложить обратиться к врачу, без диагнозов и критериев скорой.

## Жёсткие ограничения

- Реквизиты и любые платёжные данные — только в секции «Поддержать проект» (якорь
  `#support`) статического лендинга (`web/landing`), никогда в коде бота и в личном
  кабинете. В боте — лишь ссылка через `SUPPORT_URL` (можно прямо на секцию `#support`
  лендинга). Ссылки и контакты берутся только из переменных
  окружения (`SUPPORT_URL`, `AUTHOR_CONTACT`), не хардкодятся в хендлерах. Если `SUPPORT_URL`
  не задан или равен плейсхолдеру — кнопка «Поддержать» скрывается.
- Не показывать просьбу поддержать/рекламу, пока пользователь не сделал 3 записи давления.
- В терминах — «поддержать», не «донат».
- Схема БД: `users` (telegram_id, timezone, target_systolic, target_diastolic,
  med_remeasure_delay, last_support_shown_at, onboarded, followup_enabled,
  reminders_paused_until, ui_large), `measurements` (user_id, timestamp,
  systolic, diastolic, pulse, medication_taken, feeling, note), `reminders` (user_id, type,
  local_time, next_trigger_time, enabled, last_sent_date, measurement_id, last_message_id,
  mode: both|meds|pressure, followup_at), `medications`
  (name, dosage, slot; UNIQUE(user_id, name, slot)), `medication_logs` (medication_id,
  timestamp, status: taken|skipped|partial, slot), `measurement_medications`
  (measurement_id, medication_id; UNIQUE(measurement_id, medication_id) — отмеченные
  приёмы записи, перечень для «Истории записей» кабинета), `web_users` (user_id UNIQUE, email,
  password_hash — Argon2id), `web_sessions` (token PK, web_user_id FK, created_at,
  expires_at — сессии кабинета на 7 дней), `appointments` (user_id, kind: doctor|lab|exam,
  title, starts_on — локальная дата, starts_at — локальное HH:MM или NULL, note,
  status: planned|done|moved|missed|cancelled, sent_eve_at, sent_soon_at, sent_morning_at,
  sent_after_at, last_message_id, created_at — визиты и анализы, схема — поколение 8).
  Расписание ежедневных напоминаний живёт только в `reminders` (слоты morning/noon/evening +
  med_remeasure; полей `users.reminder_*` нет); уведомления о визитах — по строкам
  `appointments`, без job'ов на событие.
- Токен бота — только из переменных окружения / секрет-менеджера; в коде, конфигах и логах
  его быть не должно. Пароли кабинета — только хеш Argon2id.
- Веб-части: `web/landing` (статический лендинг) и `web/cabinet` (FastAPI, Argon2id, CSRF,
  HTTPS) — создаются после бота, архитектурно закладываются сразу. Платёжных данных в
  личном кабинете тоже нет.
- Планировщик напоминаний — APScheduler 3.x (AsyncIOScheduler + SQLAlchemyJobStore);
  планировщик строго в UTC (DST-ловушка ZoneInfo в APScheduler 3.x). Тик раз в минуту
  (`reminders:tick`): сверка job'ов слотов с БД — кабинет пишет только строки (0.5.7), и
  уведомления о визитах с захватом по флагам `sent_*_at` (0.5.8).
- Версионирование (SemVer): MAJOR — ломающие изменения, MINOR — новые функции, PATCH —
  багфиксы. Номер версии — только в `pyproject.toml`.
- Лицензия проекта — PolyForm-Noncommercial-1.0.0 (файл `LICENSE` в корне,
  `license = {text = "PolyForm-Noncommercial-1.0.0"}` в `pyproject.toml`). Строго следовать
  `doc/SPEC.md`: не добавлять функции, которых нет в
  спецификации; перед созданием файла — свериться с требованиями к нему в SPEC.
- Бот многопользовательский: данные строго изолированы по `telegram_id`, все запросы к
  записям — по `user_id` текущего пользователя, чужие id в callback_data проверяются на владельца.
- Весь код покрыт юнит-тестами (pytest + pytest-cov, порог ≥ 90% строк, бизнес-логика — 100%
  веток, обязательны тесты конкурентности). Код без тестов не считается готовым.
- Конкурентная работа (asyncio): сессия БД на операцию, короткие транзакции, SQLite WAL +
  busy_timeout, идемпотентность двойных нажатий, атомарный захват и перепланировка
  напоминаний. Подробности — `doc/SPEC.md`, раздел 7.
- Архивные записи (SPEC §6.10): момент измерения — только прошедший (будущее — ошибка
  валидации «Время измерения не может быть в будущем»), глубина не ограничена. Для архива
  повторный замер после лекарства не планируется, `medication_logs` не создаются, строка
  «обсудить с врачом» (≥180/110) не показывается. В ЛК добавление/правка/удаление записей
  и импорт CSV — только для пациента (врач — только чтение и комментарии).
- Удаление аккаунта: при удалении аккаунта пользователя сначала вызывай
  `scheduler.remove_all_jobs()` для его задач, и только потом делай `session.delete(user)`.
- Фильтр админа: в `bot/handlers/__init__.py` подключай `admin.py` только если
  `settings.ADMIN_TELEGRAM_IDS` не пуст. Используй `AdminFilter` для проверки ID.
- Бэкап: для шифрования бэкапов используй библиотеку `cryptography` (AES-GCM) или `pyrage`
  (обертка для `age`). Не используй простое копирование файлов SQLite.
- Приватность админки: при запросе статистики или поиска пользователя в админке,
  SELECT-запросы к таблице `measurements` должны возвращать только `COUNT(*)`, а не сами
  записи (никаких `systolic`, `note`).
- Размер БД: для подсчета размера SQLite используй `os.path.getsize(DB_PATH)` и
  `os.path.getsize(DB_PATH + '-wal')` (если WAL-файл существует). Не используй `VACUUM`
  для подсчета размера, так как это заблокирует БД.
- Безопасность рассылки: метод `broadcast_message()` должен использовать `asyncio.gather`
  с ограничением конкурентности (например, `asyncio.Semaphore(20)`), чтобы не заDDoSить
  Telegram API, и обязательно оборачивать каждый `send_message` в
  `try/except TelegramForbiddenError`.
- Логика личного кабинета: в хендлере `menu:cabinet` и команде `/cabinet` сначала проверяй
  наличие записи в `web_users` по `user_id`. Если записи нет — запускай FSM
  `CabinetRegistration`. Только после успешного создания записи (или если она уже есть)
  генерируй токен через `itsdangerous` и отправляй ссылку. Никогда не отправляй magic link
  пользователю, которого нет в `web_users`, так как это приведет к ошибке 401.
- Роли и доступ: в Web-кабинете реализуй два типа пользователей: patient (обычный
  пользователь) и doctor. Врач не должен иметь доступа к редактированию данных пациента,
  только к чтению (GET-запросы) и добавлению комментариев. Проверяй `status='active'` в
  таблице `doctor_patients` при каждом запросе данных пациента врачом.
- QA Bot и Telegram: для тестирования Telegram-бота используй библиотеку `Telethon`. Она
  позволяет отправлять команды от имени тестового аккаунта и читать ответы. Не используй
  `aiogram` для тестирования, так как это потребует создания второго бота.
- Скриншоты: в Playwright используй
  `page.screenshot(path=f"qa/reports/screenshots/{step_name}.png")` после каждого ключевого
  действия (логин, открытие дашборда, график). Убедись, что папка для скриншотов создана
  (`os.makedirs`).
- Лицензия: замени все упоминания MIT на `PolyForm-Noncommercial-1.0.0`. Создай файл
  `LICENSE` с полным текстом лицензии и `LICENSE-COMMERCIAL.md` с шаблоном для запросов. В
  `pyproject.toml` укажи `license = {text = "PolyForm-Noncommercial-1.0.0"}`. В `README.md`
  добавь раздел «Лицензия» с пояснением: бесплатно для некоммерческого использования, для
  коммерческого — свяжитесь с автором.
- Лендинг: создай `web/landing/index.html` с использованием Tailwind CSS (статический CSS). Реализуй
  Hero-секцию с кнопкой на https://t.me/my_bp_diary_bot?start=landing, блоки «Проблема/Решение»,
  «Как это работает», «Сигналы доверия» и FAQ. На первом экране размести 2–3 сигнала доверия.
  Стиль — чистый, медицинский, с мягкими синими и белыми тонами.
- Лендинг (Поддержка): в файле `web/landing/index.html` перед футером создай секцию
  `id="support"` с информацией об авторе (Олег Григорьевич, почта, телеграм) и реквизитами
  для донатов. Обязательно добавь блок «Для бизнеса», где указана лицензия PolyForm
  Noncommercial и контакт для запроса коммерческой лицензии. Используй Tailwind CSS для
  красивого и чистого оформления.

## Правила для Claude Code

### 1. Строгое следование спецификации
- Файл `SPEC.md` — единственный источник истины. Не добавляй функции, которых там нет.
- Если в ТЗ и SPEC.md есть противоречия, приоритет всегда у SPEC.md.

### 2. Безопасность и платежи
- НИКОГДА не хардкодь токены, пароли и реквизиты в коде. Только через `bot/config.py` и `.env`.
- В коде бота НЕ ДОЛЖНО быть реквизитов для донатов (ЮMoney, IBAN). Только ссылка `SUPPORT_URL`.
- Пароли веб-пользователей и врачей хешируются ТОЛЬКО через `argon2-cffi`.

### 3. Работа с базой данных
- При удалении пользователя (`delete_user_data`) СНАЧАЛА удаляй его джобы из APScheduler (`scheduler.remove_all_jobs()`), и только потом вызывай `session.delete(user)`. Иначе бот упадет с ошибкой `User not found` при попытке отправить напоминание.
- Все транзакции к SQLite должны быть короткими. Не держи сессию открытой во время отправки сообщений в Telegram.

### 4. Веб-кабинет и лицензия
- Лендинг должен быть сверстан на Tailwind CSS. Tailwind собирается в статический CSS скриптом
  `tools/landing/build_css.py` → `web/landing/css/tailwind.css` (в git); Play CDN
  (`cdn.tailwindcss.com`) не используется. После правки классов в разметке — пересобрать CSS
  (тест `tests/test_landing.py` падает, если класса нет в собранном CSS).
- В секции `#support` на лендинге размести таблицу с реквизитами (ЮMoney, IBAN, СберБанк) и ФИО автора.
- Лицензия проекта: `PolyForm-Noncommercial-1.0.0`. В `pyproject.toml` укажи именно её.

### 5. Локальный Docker-стенд (лендинг, кабинет, бот)
- Три компонента проекта: **бот** (`python -m bot`, aiogram, long polling, портов нет),
  **кабинет** (`web/cabinet`, FastAPI + uvicorn, локально `http://127.0.0.1:8000`),
  **лендинг** (`web/landing`, статический HTML/Tailwind; в compose не входит — и на сервере,
  и локально отдаётся отдельным контейнером nginx:
  `docker run -d --name bp-local-landing -p 127.0.0.1:8090:80 -v "<путь>/web/landing:/usr/share/nginx/html:ro" -v "<путь>/deploy/landing-nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine` →
  `http://127.0.0.1:8090` (порт 8081 на машине разработчика занят — используйте 8090).
- Локальный запуск на машине разработчика: `docker compose -f docker-compose.yml up -d --build`.
  **`-f` обязателен**: без него compose подхватывает `docker-compose.override.yml` с внешней
  сетью `nginx-proxy-manager_default` (NPM прод-сервера) и падает, потому что сети на локальной
  машине нет. Логи: `docker compose -f docker-compose.yml logs -f --tail=100`; остановка:
  `docker compose -f docker-compose.yml down`.
- На локальной машине в `.env` должен стоять токен **тестового** бота: два long-polling
  процесса с одним токеном конфликтуют и рвут соединения прод-бота.
- QA-стенд (бот + кабинет + контейнер E2E-тестов) — отдельный файл `qa/docker-compose.qa.yml`
  (запуск: `docker compose -f qa/docker-compose.qa.yml up -d --build bot cabinet`).
- Ссылки лендинга для тестового стенда: кнопки переключаются на тестового бота
  (`https://t.me/<тестовый_бот>?start=landing`) и локальный кабинет
  (`http://127.0.0.1:8000/login`). **Перед релизом вернуть боевые ссылки**
  (`https://t.me/my_bp_diary_bot?start=landing`, `https://cabinet.electroman.by/login`)!
