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

- Минимум трения: запись давления — за 2 нажатия. Кнопки вместо свободного ввода везде, где это возможно.
- Редактировать существующее сообщение (`edit_message_text` / `edit_message_reply_markup`), а не отправлять новые — чтобы не плодить визуальный шум.
- Не использовать FSM там, где можно обойтись `callback_data`.
- Все тексты — на русском, дружелюбные, без медицинского жаргона и без медицинских советов. При давлении ≥180/110 — вежливо предложить обратиться к врачу, без диагнозов и критериев скорой.

## Жёсткие ограничения

- Реквизиты и любые платёжные данные — только на внешней веб-странице, никогда в коде бота.
  В боте — лишь ссылка через `SUPPORT_URL`. Ссылки и контакты берутся только из переменных
  окружения (`SUPPORT_URL`, `AUTHOR_CONTACT`), не хардкодятся в хендлерах. Если `SUPPORT_URL`
  не задан или равен плейсхолдеру — кнопка «Поддержать» скрывается.
- Не показывать просьбу поддержать/рекламу, пока пользователь не сделал 3 записи давления.
- В терминах — «поддержать», не «донат».
- Схема БД: `users` (telegram_id, timezone, target_systolic, target_diastolic,
  med_remeasure_delay, last_support_shown_at, onboarded), `measurements` (user_id, timestamp,
  systolic, diastolic, pulse, medication_taken, feeling, note), `reminders` (user_id, type,
  local_time, next_trigger_time, enabled, last_sent_date, measurement_id), `medications`
  (name, dosage, slot; UNIQUE(user_id, name, slot)), `medication_logs` (medication_id,
  timestamp, status: taken|skipped|partial, slot), `web_users` (user_id UNIQUE, email,
  password_hash — Argon2id), `web_sessions` (token PK, web_user_id FK, created_at,
  expires_at — сессии кабинета на 7 дней). Расписание напоминаний живёт только в `reminders` (слоты
  morning/noon/evening + med_remeasure; полей `users.reminder_*` нет).
- Токен бота — только из переменных окружения / секрет-менеджера; в коде, конфигах и логах
  его быть не должно. Пароли кабинета — только хеш Argon2id.
- Веб-части: `web/landing` (статический лендинг) и `web/cabinet` (FastAPI, Argon2id, CSRF,
  HTTPS) — создаются после бота, архитектурно закладываются сразу. Платёжных данных в
  веб-приложении тоже нет.
- Планировщик напоминаний — APScheduler 3.x (AsyncIOScheduler + SQLAlchemyJobStore);
  планировщик строго в UTC (DST-ловушка ZoneInfo в APScheduler 3.x).
- Версионирование (SemVer): MAJOR — ломающие изменения, MINOR — новые функции, PATCH —
  багфиксы. Номер версии — только в `pyproject.toml`.
- Лицензия проекта — MIT (файл `LICENSE` в корне, `license = {text = "MIT"}` в
  `pyproject.toml`). Строго следовать `doc/SPEC.md`: не добавлять функции, которых нет в
  спецификации; перед созданием файла — свериться с требованиями к нему в SPEC.
- Бот многопользовательский: данные строго изолированы по `telegram_id`, все запросы к
  записям — по `user_id` текущего пользователя, чужие id в callback_data проверяются на владельца.
- Весь код покрыт юнит-тестами (pytest + pytest-cov, порог ≥ 90% строк, бизнес-логика — 100%
  веток, обязательны тесты конкурентности). Код без тестов не считается готовым.
- Конкурентная работа (asyncio): сессия БД на операцию, короткие транзакции, SQLite WAL +
  busy_timeout, идемпотентность двойных нажатий, атомарный захват и перепланировка
  напоминаний. Подробности — `doc/SPEC.md`, раздел 7.
