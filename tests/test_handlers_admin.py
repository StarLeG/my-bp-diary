"""Тесты хендлеров админ-панели (bot/handlers/admin.py, SPEC.md, §18).

Проверяются: `AdminFilter` и отказ «Доступ запрещен», экраны статистики /
здоровья / бэкапов, создание и скачивание копии (лимит 50 МБ), рассылка
(превью, подтверждение, отчёт) и поиск пользователя (без мед-данных).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from bot import keyboards, texts
from bot.handlers import admin as admin_handlers
from bot.handlers.admin import AdminFilter, AdminStates
from bot.services.admin import BroadcastResult, DbStats, SystemHealth, UserCard
from bot.services.backup import BackupError, BackupFileInfo, BackupResult

TG_ID = 111_111


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, message: Message, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message,
    )


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры."""
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


def make_stats() -> DbStats:
    return DbStats(
        users=10,
        dau=3,
        wau=7,
        measurements=42,
        medications=5,
        db_size=1024,
        wal_size=256,
        slots_enabled={"morning": 4, "noon": 2, "evening": 1},
    )


def make_health() -> SystemHealth:
    return SystemHealth(
        uptime_seconds=3661,
        app_version="0.1.1",
        python_version="3.11.0",
        scheduler_running=True,
        jobs_count=5,
        errors_24h=0,
    )


def make_card() -> UserCard:
    return UserCard(
        telegram_id=TG_ID,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone="Europe/Minsk",
        measurements_count=5,
        onboarded=True,
        reminder_slots=("morning",),
    )


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer_document(mocker):
    return mocker.patch.object(
        Message, "answer_document", new_callable=mocker.AsyncMock
    )


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def state() -> FSMContext:
    return make_state()


@pytest.fixture
def as_admin(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(TG_ID))


@pytest.fixture
def admin(mocker):
    return mocker.Mock(
        get_stats=mocker.AsyncMock(return_value=make_stats()),
        get_health=mocker.Mock(return_value=make_health()),
        all_user_ids=mocker.AsyncMock(return_value=[1, 2, 3]),
        find_user=mocker.AsyncMock(return_value=make_card()),
    )


@pytest.fixture
def backup(mocker, tmp_path: Path):
    return mocker.Mock(
        backup_dir=tmp_path,
        last_status=mocker.Mock(return_value=None),
        list_backups=mocker.Mock(return_value=[]),
        remote_url=mocker.Mock(return_value=None),
        create_backup=mocker.AsyncMock(
            return_value=BackupResult(
                name="backup-20261105-030000.sqlite3.aes",
                created_at=datetime.now(timezone.utc),
                size_bytes=2048,
                integrity_ok=True,
            )
        ),
    )


class TestAccess:
    async def test_filter_passes_configured_admin(self, monkeypatch, as_admin) -> None:
        assert await AdminFilter()(make_message(user_id=TG_ID)) is True

    async def test_filter_rejects_stranger(self, monkeypatch, as_admin) -> None:
        assert await AdminFilter()(make_message(user_id=999_999)) is False

    async def test_filter_rejects_everyone_without_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        assert await AdminFilter()(make_message(user_id=TG_ID)) is False

    async def test_filter_rejects_event_without_user(self, as_admin) -> None:
        assert await AdminFilter()(object()) is False

    async def test_cmd_admin_shows_menu(
        self, state, answer, as_admin
    ) -> None:
        await call(admin_handlers.cmd_admin, message=make_message(), state=state)
        answer.assert_awaited_once()
        assert answer.call_args.args[0] == texts.ADMIN_MENU
        assert answer.call_args.kwargs["reply_markup"] == keyboards.admin_menu_kb()

    async def test_cmd_admin_denied_canonical(self, answer, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        await call(admin_handlers.cmd_admin_denied, message=make_message())
        answer.assert_awaited_once_with(texts.ADMIN_FORBIDDEN)

    async def test_callback_denied_alert(self, cb_answer, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        callback = make_callback("adm:stats", make_message())
        await call(admin_handlers.cb_admin_denied, callback=callback)
        cb_answer.assert_awaited_once_with(texts.ADMIN_FORBIDDEN, show_alert=True)

    async def test_back_returns_to_menu(
        self, state, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:back", make_message())
        await call(admin_handlers.cb_admin_back, callback=callback, state=state)
        assert edit.call_args.args[0] == texts.ADMIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.admin_menu_kb()
        cb_answer.assert_awaited_once()


class TestStatsAndHealth:
    async def test_stats_uses_cache(
        self, admin, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:stats", make_message())
        await call(admin_handlers.cb_admin_stats, callback=callback, admin=admin)
        admin.get_stats.assert_awaited_once_with(force=False)
        text = edit.call_args.args[0]
        assert text.startswith(texts.ADMIN_STATS_HEADER)
        assert texts.ADMIN_STATS_USERS.format(count=10) in text
        assert texts.ADMIN_STATS_DAU.format(count=3) in text
        assert texts.ADMIN_STATS_WAU.format(count=7) in text
        assert texts.ADMIN_STATS_MEASUREMENTS.format(count=42) in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.admin_stats_kb()

    async def test_refresh_forces_recount(
        self, admin, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:refresh", make_message())
        await call(admin_handlers.cb_admin_stats, callback=callback, admin=admin)
        admin.get_stats.assert_awaited_once_with(force=True)

    async def test_health_screen(
        self, admin, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:health", make_message())
        await call(admin_handlers.cb_admin_health, callback=callback, admin=admin)
        text = edit.call_args.args[0]
        assert text.startswith(texts.ADMIN_HEALTH_HEADER)
        assert texts.ADMIN_HEALTH_SCHEDULER.format(
            status=texts.ADMIN_HEALTH_SCHEDULER_RUNNING, jobs=5
        ) in text
        assert texts.ADMIN_HEALTH_ERRORS.format(count=0) in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.admin_health_kb()


class TestBackups:
    async def test_backups_screen_without_status(
        self, backup, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:backups", make_message())
        await call(admin_handlers.cb_admin_backups, callback=callback, backup=backup)
        text = edit.call_args.args[0]
        assert text.startswith(texts.ADMIN_BACKUP_HEADER)
        assert texts.ADMIN_BACKUP_NONE in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.admin_backups_kb()

    async def test_backups_screen_with_last_status(
        self, backup, edit, cb_answer, as_admin
    ) -> None:
        backup.last_status.return_value = BackupResult(
            name="backup-20261104-030000.sqlite3.aes",
            created_at=datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc),
            size_bytes=2048,
            integrity_ok=True,
            uploaded_url="s3://bucket/backups/x.aes",
        )
        backup.list_backups.return_value = [
            BackupFileInfo(
                name="backup-20261104-030000.sqlite3.aes",
                size_bytes=2048,
                created_at=datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc),
            )
        ]
        callback = make_callback("adm:backups", make_message())
        await call(admin_handlers.cb_admin_backups, callback=callback, backup=backup)
        text = edit.call_args.args[0]
        assert texts.ADMIN_BACKUP_LAST.format(
            name="backup-20261104-030000.sqlite3.aes",
            created="04.11.2026 03:00",
            size=texts.fmt_size(2048),
            integrity=texts.fmt_backup_status(True),
        ) in text
        assert texts.ADMIN_BACKUP_OFFSITE_OK.format(url="s3://bucket/backups/x.aes") in text

    async def test_create_backup_success(
        self, backup, answer, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:backup:create", make_message())
        await call(
            admin_handlers.cb_admin_backup_create, callback=callback, backup=backup
        )
        cb_answer.assert_awaited_once_with(texts.ADMIN_BACKUP_CREATING)
        report = answer.call_args.args[0]
        assert texts.ADMIN_BACKUP_DONE.format(
            name="backup-20261105-030000.sqlite3.aes", size=texts.fmt_size(2048)
        ) in report
        assert texts.fmt_backup_status(True) in report

    async def test_create_backup_error(
        self, backup, answer, cb_answer, as_admin
    ) -> None:
        backup.create_backup.side_effect = BackupError("нет ключа")
        callback = make_callback("adm:backup:create", make_message())
        await call(
            admin_handlers.cb_admin_backup_create, callback=callback, backup=backup
        )
        answer.assert_awaited_once()
        assert answer.call_args.args[0] == texts.ADMIN_BACKUP_ERROR.format(
            error="нет ключа"
        )

    async def test_download_without_files_alerts(
        self, backup, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:backup:download", make_message())
        await call(
            admin_handlers.cb_admin_backup_download, callback=callback, backup=backup
        )
        cb_answer.assert_awaited_once_with(texts.ADMIN_BACKUP_NONE, show_alert=True)

    async def test_download_small_file_sends_document(
        self, backup, answer_document, cb_answer, as_admin
    ) -> None:
        path = backup.backup_dir / "backup-20261105-030000.sqlite3.aes"
        path.write_bytes(b"encrypted")
        backup.list_backups.return_value = [
            BackupFileInfo(
                name=path.name,
                size_bytes=9,
                created_at=datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc),
            )
        ]
        callback = make_callback("adm:backup:download", make_message())
        await call(
            admin_handlers.cb_admin_backup_download, callback=callback, backup=backup
        )
        answer_document.assert_awaited_once()
        assert answer_document.call_args.kwargs["caption"] == path.name
        cb_answer.assert_awaited_once_with()

    async def test_download_big_file_with_s3_gives_link(
        self, backup, cb_answer, as_admin
    ) -> None:
        name = "backup-20261105-030000.sqlite3.aes"
        backup.list_backups.return_value = [
            BackupFileInfo(
                name=name,
                size_bytes=50 * 1024 * 1024,
                created_at=datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc),
            )
        ]
        backup.remote_url.return_value = f"s3://bucket/backups/{name}"
        callback = make_callback("adm:backup:download", make_message())
        await call(
            admin_handlers.cb_admin_backup_download, callback=callback, backup=backup
        )
        cb_answer.assert_awaited_once_with(
            texts.ADMIN_BACKUP_TOO_BIG.format(
                size=texts.fmt_size(50 * 1024 * 1024), url=f"s3://bucket/backups/{name}"
            ),
            show_alert=True,
        )

    async def test_download_big_file_without_s3_alerts(
        self, backup, cb_answer, as_admin
    ) -> None:
        backup.list_backups.return_value = [
            BackupFileInfo(
                name="backup-20261105-030000.sqlite3.aes",
                size_bytes=50 * 1024 * 1024,
                created_at=datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc),
            )
        ]
        backup.remote_url.return_value = None
        callback = make_callback("adm:backup:download", make_message())
        await call(
            admin_handlers.cb_admin_backup_download, callback=callback, backup=backup
        )
        cb_answer.assert_awaited_once_with(
            texts.ADMIN_BACKUP_NO_S3.format(size=texts.fmt_size(50 * 1024 * 1024)),
            show_alert=True,
        )


class TestBroadcast:
    async def test_bcast_prompts_and_sets_state(
        self, state, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:bcast", make_message())
        await call(admin_handlers.cb_admin_bcast, callback=callback, state=state)
        assert await state.get_state() == AdminStates.broadcast.state
        assert edit.call_args.args[0] == texts.ADMIN_BCAST_PROMPT
        assert (
            edit.call_args.kwargs["reply_markup"]
            == keyboards.input_cancel_kb("adm:bcast:cancel")
        )

    async def test_bcast_empty_text_reprompts(
        self, state, answer, as_admin
    ) -> None:
        await call(
            admin_handlers.msg_admin_bcast,
            message=make_message(text="   "),
            state=state,
        )
        answer.assert_awaited_once()
        assert answer.call_args.args[0] == texts.ADMIN_BCAST_EMPTY

    async def test_bcast_preview_saves_draft(
        self, state, answer, as_admin
    ) -> None:
        await call(
            admin_handlers.msg_admin_bcast,
            message=make_message(text="Плановые работы"),
            state=state,
        )
        data = await state.get_data()
        assert data[admin_handlers.B_TEXT] == "Плановые работы"
        assert answer.call_args.args[0] == texts.ADMIN_BCAST_PREVIEW.format(
            text="Плановые работы"
        )
        assert (
            answer.call_args.kwargs["reply_markup"]
            == keyboards.admin_broadcast_preview_kb()
        )

    async def test_bcast_cancel_returns_menu(
        self, state, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:bcast:cancel", make_message())
        await call(admin_handlers.cb_admin_bcast_cancel, callback=callback, state=state)
        cb_answer.assert_awaited_once_with(texts.ADMIN_BCAST_CANCELLED)
        assert edit.call_args.args[0] == texts.ADMIN_MENU

    async def test_confirm_without_draft_returns_menu(
        self, state, edit, cb_answer, admin, as_admin
    ) -> None:
        callback = make_callback("adm:bcast:confirm", make_message())
        await call(
            admin_handlers.cb_admin_bcast_confirm,
            callback=callback,
            state=state,
            bot=object(),
            admin=admin,
        )
        assert edit.call_args.args[0] == texts.ADMIN_MENU
        admin.all_user_ids.assert_not_awaited()

    async def test_confirm_without_users(
        self, state, edit, cb_answer, admin, as_admin
    ) -> None:
        await state.update_data(**{admin_handlers.B_TEXT: "Текст"})
        admin.all_user_ids.return_value = []
        callback = make_callback("adm:bcast:confirm", make_message())
        await call(
            admin_handlers.cb_admin_bcast_confirm,
            callback=callback,
            state=state,
            bot=object(),
            admin=admin,
        )
        assert edit.call_args.args[0] == texts.ADMIN_BCAST_NO_USERS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.admin_menu_kb()

    async def test_confirm_sends_and_reports(
        self, state, edit, cb_answer, admin, mocker, as_admin
    ) -> None:
        broadcast = mocker.patch(
            "bot.handlers.admin.broadcast_message",
            new_callable=mocker.AsyncMock,
            return_value=BroadcastResult(delivered=2, blocked=1, failed=0),
        )
        await state.update_data(**{admin_handlers.B_TEXT: "Текст рассылки"})
        callback = make_callback("adm:bcast:confirm", make_message())
        bot = mocker.Mock()
        await call(
            admin_handlers.cb_admin_bcast_confirm,
            callback=callback,
            state=state,
            bot=bot,
            admin=admin,
        )
        broadcast.assert_awaited_once()
        args = broadcast.call_args.args
        assert args[0] is bot
        assert args[1] == [1, 2, 3]
        assert args[2] == "Текст рассылки"
        # Первый edit — прогресс «Отправлено 0 из 3...», последний — отчёт.
        assert (
            edit.call_args_list[0].args[0]
            == texts.ADMIN_BCAST_PROGRESS.format(done=0, total=3)
        )
        assert (
            edit.call_args_list[-1].args[0]
            == texts.ADMIN_BCAST_REPORT.format(delivered=2, blocked=1)
        )
        assert (
            edit.call_args_list[-1].kwargs["reply_markup"]
            == keyboards.admin_menu_kb()
        )
        assert await state.get_state() is None


class TestSearch:
    async def test_search_prompts(
        self, state, edit, cb_answer, as_admin
    ) -> None:
        callback = make_callback("adm:search", make_message())
        await call(admin_handlers.cb_admin_search, callback=callback, state=state)
        assert await state.get_state() == AdminStates.search.state
        assert edit.call_args.args[0] == texts.ADMIN_SEARCH_PROMPT
        # «Отмена» ведёт в меню админки: старый вариант («adm:search») переоткрывал
        # тот же экран и получал от Telegram «message is not modified».
        assert edit.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb("adm:back")

    async def test_search_username_unsupported(
        self, state, answer, admin, as_admin
    ) -> None:
        await call(
            admin_handlers.msg_admin_search,
            message=make_message(text="@someone"),
            state=state,
            admin=admin,
        )
        assert answer.call_args.args[0] == texts.ADMIN_SEARCH_USERNAME_UNSUPPORTED
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb("adm:back")
        admin.find_user.assert_not_awaited()

    async def test_search_bad_input(
        self, state, answer, admin, as_admin
    ) -> None:
        await call(
            admin_handlers.msg_admin_search,
            message=make_message(text="не цифры"),
            state=state,
            admin=admin,
        )
        assert answer.call_args.args[0] == texts.ADMIN_SEARCH_BAD_INPUT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb("adm:back")

    async def test_search_not_found(
        self, state, answer, admin, as_admin
    ) -> None:
        admin.find_user.return_value = None
        await call(
            admin_handlers.msg_admin_search,
            message=make_message(text="999999"),
            state=state,
            admin=admin,
        )
        admin.find_user.assert_awaited_once_with(999_999)
        assert answer.call_args.args[0] == texts.ADMIN_SEARCH_NOT_FOUND
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb("adm:back")

    async def test_search_card_shows_aggregates_only(
        self, state, answer, admin, as_admin
    ) -> None:
        await call(
            admin_handlers.msg_admin_search,
            message=make_message(text=str(TG_ID)),
            state=state,
            admin=admin,
        )
        text = answer.call_args.args[0]
        # Только агрегаты: id, дата, таймзона, счётчик, статус онбординга.
        assert str(TG_ID) in text
        assert "01.01.2026" in text
        assert "Europe/Minsk" in text
        assert texts.ADMIN_SEARCH_ONBOARDED in text
        assert texts.SLOT_LABELS["morning"] in text
        assert answer.call_args.kwargs["reply_markup"] == keyboards.admin_search_card_kb(
            TG_ID
        )

    async def test_search_card_without_reminders(
        self, state, answer, admin, as_admin
    ) -> None:
        admin.find_user.return_value = UserCard(
            telegram_id=TG_ID,
            created_at=None,
            timezone="Europe/Minsk",
            measurements_count=0,
            onboarded=False,
            reminder_slots=(),
        )
        await call(
            admin_handlers.msg_admin_search,
            message=make_message(text=str(TG_ID)),
            state=state,
            admin=admin,
        )
        text = answer.call_args.args[0]
        assert texts.ADMIN_SEARCH_REMINDERS_OFF in text
        assert texts.ADMIN_SEARCH_NOT_ONBOARDED in text


class TestTestMessage:
    async def test_sent(self, cb_answer, mocker, as_admin) -> None:
        bot = mocker.Mock()
        bot.send_message = mocker.AsyncMock()
        callback = make_callback(f"adm:test:{TG_ID}", make_message())
        await call(admin_handlers.cb_admin_test_message, callback=callback, bot=bot)
        bot.send_message.assert_awaited_once_with(
            TG_ID, texts.ADMIN_SEARCH_TEST_MESSAGE
        )
        cb_answer.assert_awaited_once_with(texts.ADMIN_SEARCH_TEST_SENT)

    async def test_failed(self, cb_answer, mocker, as_admin) -> None:
        bot = mocker.Mock()
        bot.send_message = mocker.AsyncMock(
            side_effect=TelegramAPIError(method="sendMessage", message="boom")
        )
        callback = make_callback(f"adm:test:{TG_ID}", make_message())
        await call(admin_handlers.cb_admin_test_message, callback=callback, bot=bot)
        cb_answer.assert_awaited_once_with(
            texts.ADMIN_SEARCH_TEST_FAILED, show_alert=True
        )

    async def test_non_digit_value_is_ignored(self, cb_answer, mocker, as_admin) -> None:
        bot = mocker.Mock()
        bot.send_message = mocker.AsyncMock()
        callback = make_callback("adm:test:abc", make_message())
        await call(admin_handlers.cb_admin_test_message, callback=callback, bot=bot)
        bot.send_message.assert_not_awaited()
        cb_answer.assert_awaited_once_with()


class TestConsentLines:
    """Согласия в админке (§18.2, §18.6): только счётчик и дата, без данных."""

    def test_stats_line(self) -> None:
        from bot.handlers.admin import _fmt_stats

        stats = make_stats()
        stats.consented = 7
        assert "Дали согласие: 7" in _fmt_stats(stats)

    def test_card_consent(self) -> None:
        from dataclasses import replace

        from bot.handlers.admin import _fmt_user_card as _fmt_card

        given = replace(make_card(), consent_at=datetime(2026, 11, 10, tzinfo=timezone.utc))
        assert "Согласие: дано 10.11.2026" in _fmt_card(given)
        assert "Согласие: согласия нет" in _fmt_card(make_card())
