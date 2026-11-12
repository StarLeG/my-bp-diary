"""Тесты сборки и запуска (SPEC.md, §16.3; план — шаг 7).

Сборка — без сетевых запросов (aiogram проверяет только формат токена);
отсутствие или порча `BOT_TOKEN` — понятное сообщение и код выхода 1.
"""
from __future__ import annotations

import importlib
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.token import TokenValidationError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError

from bot.config import Settings
from bot.handlers import router
from bot.main import App, build_app, build_scheduler, jobs_db_path, main, run
from bot.middlewares import (
    ConsentMiddleware,
    DbSessionMiddleware,
    DebounceMiddleware,
    TelegramNameMiddleware,
    ThrottleMiddleware,
)
from bot.services.admin import AdminService, ErrorCounter
from bot.services.backup import BACKUP_JOB_ID, BackupService, fire_backup
from bot.services.reminders import RemindersService, fire_reminder

#: Фейковый токен валидного формата: проверяется только формат, сеть не нужна.
FAKE_TOKEN = f"123456:{'A' * 35}"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Настройки с фейковым токеном и БД во временном каталоге."""
    return Settings(bot_token=FAKE_TOKEN, db_path=str(tmp_path / "bot.sqlite3"))


@pytest.fixture(autouse=True)
def detach_root_router() -> Iterator[None]:
    """Модульный router включается в один Dispatcher — снимаем привязку после теста.

    В aiogram нет публичного способа открепить router (только приватный
    `_parent_router`), поэтому `build_app` в тестах можно вызывать многократно.
    """
    yield
    router._parent_router = None


def make_validation_error() -> ValidationError:
    """Реальная Pydantic-ошибка конфига (пустой `BOT_TOKEN`)."""
    try:
        Settings(bot_token="")
    except ValidationError as exc:
        return exc
    raise AssertionError("ожидалась ошибка валидации Settings")


def make_app(mocker, order: list[str]) -> App:
    """App из моков: каждый вызов жизненного цикла фиксируется в `order`."""
    app = mocker.Mock(spec=App)
    app.engine = mocker.Mock()
    app.engine.dispose = mocker.AsyncMock(side_effect=lambda: order.append("engine_dispose"))
    app.bot = mocker.Mock()
    app.bot.session.close = mocker.AsyncMock(side_effect=lambda: order.append("bot_close"))
    app.scheduler = mocker.Mock(running=True)
    app.scheduler.start = mocker.Mock(side_effect=lambda: order.append("scheduler_start"))
    app.scheduler.shutdown = mocker.Mock(
        side_effect=lambda wait: order.append("scheduler_shutdown")
    )
    app.reminders = mocker.Mock()
    app.reminders.startup = mocker.AsyncMock(side_effect=lambda: order.append("startup"))
    app.dp = mocker.Mock()
    app.dp.start_polling = mocker.AsyncMock(side_effect=lambda bot: order.append("polling"))
    return app


class TestBuildScheduler:
    """Планировщик: job store — отдельный файл SQLite, дефолты job'ов и UTC."""

    def test_jobstore_is_separate_sqlite(self, tmp_path: Path) -> None:
        """Job store — свой файл рядом с основной БД, не она сама (§6.7, U1)."""
        scheduler = build_scheduler(str(tmp_path / "bot.sqlite3"))
        jobstore = scheduler._jobstores["default"]
        assert isinstance(jobstore, SQLAlchemyJobStore)
        assert jobstore.engine.url.database == str(tmp_path / "bot.jobs.sqlite3")

    @pytest.mark.parametrize(
        ("db_path", "expected"),
        [
            ("data/bot.sqlite3", "bot.jobs.sqlite3"),
            ("data/test-stand.sqlite3", "test-stand.jobs.sqlite3"),
            ("data/bot", "bot.jobs.sqlite3"),
        ],
    )
    def test_jobs_db_path(self, db_path: str, expected: str) -> None:
        assert jobs_db_path(db_path) == str(Path(db_path).with_name(expected))

    def test_jobs_db_path_memory(self) -> None:
        assert jobs_db_path(":memory:") == ":memory:"

    async def test_jobs_not_in_backup(self, tmp_path: Path) -> None:
        """Job'ы живут в своём файле: бэкап основной БД (§20) их не содержит."""
        import sqlite3
        from datetime import datetime, timezone

        from bot.db import create_engine, init_db
        from bot.services.backup import decrypt_file, derive_key

        db_path = tmp_path / "bot.sqlite3"
        engine = create_engine(str(db_path))
        await init_db(engine)
        await engine.dispose()
        scheduler = build_scheduler(str(db_path))
        scheduler.start()
        try:
            scheduler.add_job(
                fire_reminder,
                "date",
                run_date=datetime(2099, 1, 1, tzinfo=timezone.utc),
                id="reminder:1",
                args=[1, "slot"],
            )
        finally:
            scheduler.shutdown(wait=False)
        jobs = sqlite3.connect(jobs_db_path(str(db_path)))
        try:
            assert jobs.execute("SELECT id FROM apscheduler_jobs").fetchall() == [("reminder:1",)]
        finally:
            jobs.close()

        backup = BackupService(str(db_path), tmp_path / "backups", "test-key")
        result = await backup.create_backup()
        restored = tmp_path / "restored.sqlite3"
        decrypt_file(tmp_path / "backups" / result.name, restored, derive_key("test-key"))
        copy = sqlite3.connect(restored)
        try:
            tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            copy.close()
        assert "measurements" in tables
        assert "apscheduler_jobs" not in tables

    def test_defaults_and_timezone(self, tmp_path: Path) -> None:
        scheduler = build_scheduler(str(tmp_path / "bot.sqlite3"))
        assert isinstance(scheduler, AsyncIOScheduler)
        assert str(scheduler.timezone) == "UTC"
        assert scheduler._job_defaults["coalesce"] is True
        assert scheduler._job_defaults["max_instances"] == 1
        assert scheduler._job_defaults["misfire_grace_time"] == 300


class TestBuildApp:
    """Сборка приложения с фейковым токеном — без сетевых запросов."""

    def test_components_created(self, settings: Settings) -> None:
        app = build_app(settings)
        assert isinstance(app.bot, Bot)
        assert isinstance(app.dp, Dispatcher)
        assert isinstance(app.scheduler, AsyncIOScheduler)
        assert isinstance(app.reminders, RemindersService)

    def test_bot_proxy_sets_session_proxy(self, tmp_path: Path) -> None:
        """`BOT_PROXY` подхватывается: сессия Telegram API идёт через прокси (§3)."""
        settings = Settings(
            bot_token=FAKE_TOKEN,
            db_path=str(tmp_path / "bot.sqlite3"),
            bot_proxy="http://127.0.0.1:8080",
        )
        app = build_app(settings)
        assert isinstance(app.bot.session, AiohttpSession)
        assert app.bot.session._proxy == "http://127.0.0.1:8080"

    def test_bot_without_proxy_uses_default_session(self, settings: Settings) -> None:
        """Пустой `BOT_PROXY` — обычная сессия без прокси."""
        app = build_app(settings)
        assert app.bot.session._proxy is None

    def test_reminders_in_workflow_data(self, settings: Settings) -> None:
        app = build_app(settings)
        assert app.dp.workflow_data["reminders"] is app.reminders

    async def test_reminders_service_bound_to_job_entry(
        self, settings: Settings, mocker
    ) -> None:
        """Job-точка входа `fire_reminder` делегирует сервису из `build_app`."""
        app = build_app(settings)
        fire = mocker.patch.object(app.reminders, "_fire", new=mocker.AsyncMock())
        await fire_reminder(5, "slot")
        fire.assert_awaited_once_with(5, "slot")

    def test_router_registered(self, settings: Settings) -> None:
        app = build_app(settings)
        assert router in app.dp.sub_routers

    def test_middlewares_registered(self, settings: Settings) -> None:
        app = build_app(settings)
        update_types = [type(m) for m in app.dp.update.outer_middleware]
        assert DbSessionMiddleware in update_types
        callback_types = [type(m) for m in app.dp.callback_query.outer_middleware]
        assert callback_types == [
            DebounceMiddleware,
            ThrottleMiddleware,
            TelegramNameMiddleware,
            ConsentMiddleware,
        ]
        message_types = [type(m) for m in app.dp.message.outer_middleware]
        assert ConsentMiddleware in message_types
        assert app.dp.workflow_data["deletion_log"] is app.deletion_log

    def test_external_scheduler_used(self, settings: Settings) -> None:
        scheduler = build_scheduler(":memory:")
        app = build_app(settings, scheduler=scheduler)
        assert app.scheduler is scheduler
        assert app.reminders._scheduler is scheduler

    async def test_sender_delivers_with_reply_markup(self, settings: Settings, mocker) -> None:
        """Sender-адаптер передаёт клавиатуру как `reply_markup` (контракт Sender)."""
        app = build_app(settings)
        send_message = mocker.patch.object(app.bot, "send_message", new_callable=mocker.AsyncMock)
        await app.reminders._sender(42, "Пора измерить давление", "клавиатура")
        send_message.assert_awaited_once_with(
            42,
            "Пора измерить давление",
            reply_markup="клавиатура",
            disable_notification=False,
            parse_mode=None,
        )

    async def test_sender_html_sets_parse_mode(self, settings: Settings, mocker) -> None:
        """`html=True` — parse_mode="HTML" для экранированных текстов (§11)."""
        app = build_app(settings)
        send_message = mocker.patch.object(app.bot, "send_message", new_callable=mocker.AsyncMock)
        await app.reminders._sender(42, "<b>Пора</b>", None, html=True)
        send_message.assert_awaited_once_with(
            42, "<b>Пора</b>", reply_markup=None, disable_notification=False, parse_mode="HTML"
        )

    async def test_sender_silent_and_clear_markup(self, settings: Settings, mocker) -> None:
        """Повтор «если не ответил» — без звука; кнопки прежнего сообщения снимаются (§6.7)."""
        app = build_app(settings)
        send_message = mocker.patch.object(app.bot, "send_message", new_callable=mocker.AsyncMock)
        send_message.return_value = mocker.Mock(message_id=77)
        assert await app.reminders._sender(42, "Повтор", None, silent=True) == 77
        assert send_message.call_args.kwargs["disable_notification"] is True
        edit = mocker.patch.object(app.bot, "edit_message_reply_markup", new_callable=mocker.AsyncMock)
        await app.reminders._clear_markup(42, 5)
        edit.assert_awaited_once_with(chat_id=42, message_id=5, reply_markup=None)

    def test_invalid_token_raises(self, tmp_path: Path) -> None:
        bad = Settings(bot_token="не-токен", db_path=str(tmp_path / "bot.sqlite3"))
        with pytest.raises(TokenValidationError):
            build_app(bad)


class TestBuildAppBackupAdmin:
    """Компоненты §18–§20: бэкапы, админ-сервис, счётчик ошибок."""

    def test_backup_admin_error_counter_created(self, settings: Settings) -> None:
        app = build_app(settings)
        assert isinstance(app.backup, BackupService)
        assert isinstance(app.admin, AdminService)
        assert isinstance(app.error_counter, ErrorCounter)

    def test_backup_and_admin_in_workflow_data(self, settings: Settings) -> None:
        app = build_app(settings)
        assert app.dp.workflow_data["backup"] is app.backup
        assert app.dp.workflow_data["admin"] is app.admin

    def test_daily_backup_job_scheduled(self, settings: Settings) -> None:
        """Ежедневный job бэкапа привязан к планировщику приложения (§20)."""
        app = build_app(settings)
        job = app.scheduler.get_job(BACKUP_JOB_ID)
        assert job is not None
        assert job.func is fire_backup

    async def test_daily_backup_job_survives_jobstore(self, settings: Settings) -> None:
        """Job сериализуется в SQLAlchemyJobStore и восстанавливается как `fire_backup`."""
        app = build_app(settings)
        app.scheduler.start()
        try:
            job = app.scheduler.get_job(BACKUP_JOB_ID)
            assert job is not None
            assert job.func is fire_backup
        finally:
            app.scheduler.shutdown(wait=False)

    async def test_fire_backup_delegates_to_service(
        self, settings: Settings, mocker
    ) -> None:
        """`fire_backup` (точка входа job'а) делегирует сервису из `build_app`."""
        app = build_app(settings)
        create = mocker.patch.object(
            app.backup, "create_backup", new_callable=mocker.AsyncMock
        )
        await fire_backup()
        create.assert_awaited_once()


class TestRun:
    """Жизненный цикл: порядок шагов и корректная остановка."""

    async def test_lifecycle_order(self, mocker) -> None:
        order: list[str] = []
        app = make_app(mocker, order)
        mocker.patch(
            "bot.main.init_db",
            new_callable=mocker.AsyncMock,
            side_effect=lambda engine: order.append("init_db"),
        )
        await run(app)
        assert order == [
            "init_db",
            "scheduler_start",
            "startup",
            "polling",
            "scheduler_shutdown",
            "bot_close",
            "engine_dispose",
        ]

    async def test_shutdown_after_polling_error(self, mocker) -> None:
        order: list[str] = []
        app = make_app(mocker, order)
        mocker.patch("bot.main.init_db", new_callable=mocker.AsyncMock)
        app.dp.start_polling = mocker.AsyncMock(side_effect=RuntimeError("поллинг упал"))
        with pytest.raises(RuntimeError):
            await run(app)
        app.scheduler.shutdown.assert_called_once_with(wait=False)
        app.bot.session.close.assert_awaited_once()
        app.engine.dispose.assert_awaited_once()

    async def test_no_shutdown_when_scheduler_not_running(self, mocker) -> None:
        order: list[str] = []
        app = make_app(mocker, order)
        mocker.patch("bot.main.init_db", new_callable=mocker.AsyncMock)
        app.scheduler.running = False
        await run(app)
        app.scheduler.shutdown.assert_not_called()
        app.bot.session.close.assert_awaited_once()


class TestMainExitCodes:
    """`main()`: коды выхода и понятные сообщения об ошибках конфига."""

    def test_missing_token_returns_1(self, mocker, capsys) -> None:
        mocker.patch("bot.main.get_settings", side_effect=make_validation_error())
        assert main() == 1
        assert "bot_token" in capsys.readouterr().err

    def test_invalid_token_returns_1(self, mocker, capsys) -> None:
        settings = Settings(bot_token="не-токен", db_path=":memory:")
        mocker.patch("bot.main.get_settings", return_value=settings)
        assert main() == 1
        assert "BOT_TOKEN" in capsys.readouterr().err

    def test_success_runs_app(self, mocker) -> None:
        settings = Settings(bot_token=FAKE_TOKEN, db_path=":memory:")
        mocker.patch("bot.main.get_settings", return_value=settings)
        run_mock = mocker.patch("bot.main.run", new_callable=mocker.AsyncMock)
        assert main() == 0
        run_mock.assert_awaited_once()

    def test_keyboard_interrupt_is_graceful(self, mocker) -> None:
        settings = Settings(bot_token=FAKE_TOKEN, db_path=":memory:")
        mocker.patch("bot.main.get_settings", return_value=settings)
        mocker.patch(
            "bot.main.run", new_callable=mocker.AsyncMock, side_effect=KeyboardInterrupt
        )
        assert main() == 0


class TestModuleEntrypoint:
    """`python -m bot` — обёртка над `main()` с её кодом выхода."""

    def test_import_does_not_run_main(self, mocker) -> None:
        """Импорт модуля (`__name__ != "__main__"`) не запускает бота."""
        main_mock = mocker.patch("bot.main.main")
        importlib.import_module("bot.__main__")
        main_mock.assert_not_called()
        sys.modules.pop("bot.__main__", None)  # чтобы runpy-тесты шли без предупреждений

    def test_exits_with_zero(self, mocker) -> None:
        mocker.patch("bot.main.main", return_value=0)
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bot", run_name="__main__")
        assert excinfo.value.code == 0

    def test_exits_with_one(self, mocker) -> None:
        mocker.patch("bot.main.main", return_value=1)
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bot", run_name="__main__")
        assert excinfo.value.code == 1
