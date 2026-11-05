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
from aiogram.utils.token import TokenValidationError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError

from bot.config import Settings
from bot.handlers import router
from bot.main import App, build_app, build_scheduler, main, run
from bot.middlewares import DbSessionMiddleware, DebounceMiddleware, ThrottleMiddleware
from bot.services.reminders import RemindersService

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
    """Планировщик: job store — та же SQLite, дефолты job'ов и UTC."""

    def test_jobstore_is_sqlite(self, tmp_path: Path) -> None:
        scheduler = build_scheduler(str(tmp_path / "bot.sqlite3"))
        jobstore = scheduler._jobstores["default"]
        assert isinstance(jobstore, SQLAlchemyJobStore)
        assert str(jobstore.engine.url).endswith("bot.sqlite3")

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

    def test_reminders_in_workflow_data(self, settings: Settings) -> None:
        app = build_app(settings)
        assert app.dp.workflow_data["reminders"] is app.reminders

    def test_router_registered(self, settings: Settings) -> None:
        app = build_app(settings)
        assert router in app.dp.sub_routers

    def test_middlewares_registered(self, settings: Settings) -> None:
        app = build_app(settings)
        update_types = [type(m) for m in app.dp.update.outer_middleware]
        assert DbSessionMiddleware in update_types
        callback_types = [type(m) for m in app.dp.callback_query.outer_middleware]
        assert callback_types == [DebounceMiddleware, ThrottleMiddleware]

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
            42, "Пора измерить давление", reply_markup="клавиатура"
        )

    def test_invalid_token_raises(self, tmp_path: Path) -> None:
        bad = Settings(bot_token="не-токен", db_path=str(tmp_path / "bot.sqlite3"))
        with pytest.raises(TokenValidationError):
            build_app(bad)


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
