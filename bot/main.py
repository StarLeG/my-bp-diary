"""Сборка и запуск бота (SPEC.md, §16.3; разделы 7, 12).

`build_app` собирает компоненты без сетевых запросов (проверяется только формат
токена), `run` ведёт жизненный цикл: схема БД → планировщик → catch-up → поллинг;
остановка корректная (jobstore, сессия бота, engine). Ошибки конфигурации (нет
или битый `BOT_TOKEN`) — понятное сообщение в консоль и exit 1. Токен в логи и
сообщения не попадает (SPEC.md, «Безопасность токена»).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.utils.token import TokenValidationError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.config import Settings, get_settings
from bot.db import create_engine, create_session_factory, init_db
from bot.handlers import router
from bot.middlewares import DbSessionMiddleware, DebounceMiddleware, ThrottleMiddleware
from bot.services.reminders import RemindersService

logger = logging.getLogger(__name__)

#: Формат логов; секреты в коде и логах отсутствуют по построению.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@dataclass
class App:
    """Собранные компоненты приложения (без сетевых запросов)."""

    bot: Bot
    """Telegram-бот; токен проверяется по формату при создании."""

    dp: Dispatcher
    """Диспетчер: роутеры, middleware, workflow_data с сервисами."""

    engine: AsyncEngine
    """Async-engine SQLite (PRAGMA для конкурентной работы)."""

    session_factory: async_sessionmaker[AsyncSession]
    """Фабрика сессий: сессия на операцию (SPEC.md, раздел 7.2)."""

    scheduler: AsyncIOScheduler
    """Планировщик напоминаний: APScheduler, строго UTC, job store — SQLite."""

    reminders: RemindersService
    """Сервис напоминаний: строки `reminders` — источник истины, job'ы — доставка."""


def build_scheduler(db_path: str) -> AsyncIOScheduler:
    """Создать планировщик: job store — та же SQLite, всё строго в UTC.

    Job defaults: `coalesce=True`, `max_instances=1`, `misfire_grace_time=300`
    (SPEC.md, §6.7). Таймзоны в APScheduler не передаются (DST-ловушка
    ZoneInfo): локальное время конвертируется в UTC в `RemindersService`.
    """
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )


def build_app(settings: Settings, scheduler: AsyncIOScheduler | None = None) -> App:
    """Собрать бота, диспетчер, планировщик и сервисы (без сети).

    Middleware: сессия БД на апдейт, debounce (1.5 с) и throttle (1 с) на
    callback'и; сервис напоминаний доступен хендлерам через workflow_data.
    Sender адаптирует `bot.send_message` под контракт `Sender` (клавиатура —
    третий аргумент).

    Исключения:
        TokenValidationError: `BOT_TOKEN` неверного формата.
    """
    bot = Bot(token=settings.bot_token)  # валидация формата токена, без сети
    engine = create_engine(settings.db_path)
    session_factory = create_session_factory(engine)
    if scheduler is None:
        scheduler = build_scheduler(settings.db_path)

    async def send(telegram_id: int, text: str, markup: object | None) -> None:
        """Доставка напоминания (контракт `Sender`)."""
        await bot.send_message(telegram_id, text, reply_markup=markup)  # type: ignore[arg-type]

    reminders = RemindersService(session_factory, send, scheduler)

    dp = Dispatcher()
    dp.include_router(router)
    dp.update.outer_middleware.register(DbSessionMiddleware(session_factory))
    dp.callback_query.outer_middleware.register(DebounceMiddleware())
    dp.callback_query.outer_middleware.register(ThrottleMiddleware())
    dp.workflow_data["reminders"] = reminders
    return App(
        bot=bot,
        dp=dp,
        engine=engine,
        session_factory=session_factory,
        scheduler=scheduler,
        reminders=reminders,
    )


async def run(app: App) -> None:
    """Жизненный цикл: схема БД → планировщик → catch-up → поллинг → остановка.

    Catch-up при старте доставляет просроченное не чаще одного раза в день на
    тип (SPEC.md, §6.7). Остановка корректная: jobstore (`wait=False`), сессия
    бота и engine закрываются всегда.
    """
    await init_db(app.engine)
    app.scheduler.start()
    try:
        sent = await app.reminders.startup()
        logger.info("Планировщик запущен, catch-up отправок: %s", sent)
        await app.dp.start_polling(app.bot)
    finally:
        if app.scheduler.running:
            app.scheduler.shutdown(wait=False)
        await app.bot.session.close()
        await app.engine.dispose()


def main() -> int:
    """Точка входа: собрать приложение и запустить поллинг.

    Возвращает 0 при штатной остановке и 1 при ошибке конфигурации (нет или
    битый `BOT_TOKEN`, БД не создаётся) — SPEC.md, раздел 12. Значения
    секретов в сообщения не попадают.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = ", ".join(sorted({str(err["loc"][0]) for err in exc.errors()}))
        print(
            f"Ошибка конфигурации ({fields}): проверьте .env — пример в .env.example.",
            file=sys.stderr,
        )
        return 1
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    try:
        app = build_app(settings)
    except TokenValidationError:
        print(
            "Некорректный BOT_TOKEN: проверьте значение в .env (формат «123456:ABC…»).",
            file=sys.stderr,
        )
        return 1
    try:
        asyncio.run(run(app))
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    return 0
