"""Тесты хендлеров статистики и экспорта CSV (bot/handlers/stats.py, SPEC.md, §6.5, §10)."""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import stats
from bot.models import (
    Measurement,
    Medication,
    MedicationLog,
    User as UserModel,
)
from bot.services import csv_export

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def photo(mocker):
    return mocker.patch.object(Message, "answer_photo", new_callable=mocker.AsyncMock)


@pytest.fixture
def delete(mocker):
    return mocker.patch.object(Message, "delete", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def doc(mocker):
    return mocker.patch.object(Message, "answer_document", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


async def add_row(
    session: AsyncSession,
    user: UserModel,
    systolic: int = 135,
    diastolic: int = 85,
    pulse: int | None = 72,
    timestamp: datetime | None = None,
) -> Measurement:
    row = Measurement(
        user_id=user.id,
        timestamp=timestamp or utcnow(),
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
    )
    session.add(row)
    await session.commit()
    return row


def today_of(user: UserModel):
    return datetime.now(ZoneInfo(user.timezone)).date()


class TestPeriodsScreen:
    async def test_menu_shows_periods(self, session, edit, cb_answer) -> None:
        await stats.cb_stats_periods(make_callback("menu:stats", make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_periods_returns(self, session, edit, cb_answer) -> None:
        await stats.cb_stats_periods(make_callback("stats:periods", make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()


class TestSummary:
    async def test_empty_shows_prompt(self, session, user, edit, cb_answer) -> None:
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_shows_summary(self, session, user, photo, delete, cb_answer) -> None:
        today = today_of(user)
        await add_row(session, user, systolic=125, diastolic=75)
        await add_row(session, user, systolic=135, diastolic=85)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = photo.call_args.kwargs["caption"]
        assert texts.STATS_HEADER.format(
            days=7,
            date_from=(today - timedelta(days=6)).strftime("%d.%m"),
            date_to=today.strftime("%d.%m"),
        ) in text
        assert texts.STATS_COUNT.format(count=2) in text
        assert texts.STATS_SYSTOLIC.format(avg=130, min=125, max=135) in text
        assert texts.STATS_DIASTOLIC.format(avg=80, min=75, max=85) in text
        assert texts.STATS_GOAL.format(
            target=f"{user.target_systolic}/{user.target_diastolic}",
            verdict=texts.STATS_GOAL_OK,
        ) in text
        assert texts.STATS_FOOTER_SUPPORT not in text  # записей < 3
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb("7")

    async def test_foreign_measurements_hidden(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        other_user = UserModel(telegram_id=222_222)
        session.add(other_user)
        await session.commit()
        await add_row(session, user)
        await add_row(session, other_user, systolic=200)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = photo.call_args.kwargs["caption"]
        assert texts.STATS_COUNT.format(count=1) in text

    async def test_adherence_line_with_medications(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        med = Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning")
        session.add(med)
        await session.commit()
        session.add(
            MedicationLog(
                user_id=user.id,
                medication_id=med.id,
                timestamp=utcnow(),
                status="taken",
                slot="morning",
            )
        )
        await session.commit()
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = photo.call_args.kwargs["caption"]
        # 1 лекарство × 7 дней = 7 запланированных приёмов
        assert texts.STATS_ADHERENCE.format(taken=1, planned=7) in text

    async def test_no_adherence_line_without_medications(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert "Лекарства: принято" not in photo.call_args.kwargs["caption"]


class TestSupportFooter:
    async def test_shown_when_enough_and_never_shown(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        for _ in range(3):
            await add_row(session, user)
        assert user.last_support_shown_at is None
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in photo.call_args.kwargs["caption"]
        await session.refresh(user)
        assert user.last_support_shown_at is not None

    async def test_hidden_below_threshold(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        await add_row(session, user)
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in photo.call_args.kwargs["caption"]
        await session.refresh(user)
        assert user.last_support_shown_at is None

    async def test_hidden_within_a_day(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        shown = utcnow() - timedelta(hours=1)
        user.last_support_shown_at = shown
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in photo.call_args.kwargs["caption"]
        await session.refresh(user)
        assert user.last_support_shown_at == shown

    async def test_shown_again_after_a_day(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        user.last_support_shown_at = utcnow() - timedelta(hours=25)
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in photo.call_args.kwargs["caption"]
        await session.refresh(user)
        assert user.last_support_shown_at > utcnow() - timedelta(minutes=1)

    async def test_naive_timestamp_is_normalized(
        self, session, user, photo, delete, cb_answer
    ) -> None:
        """SQLite может вернуть naive datetime — поле трактуется как UTC."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        user.last_support_shown_at = naive - timedelta(hours=1)
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in photo.call_args.kwargs["caption"]
        await session.refresh(user)
        user.last_support_shown_at = naive - timedelta(hours=25)
        await session.commit()
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in photo.call_args.kwargs["caption"]


class TestCsv:
    async def test_csv_menu(self, session, edit, cb_answer) -> None:
        await stats.cb_csv_menu(make_callback("stats:csv", make_message()))
        assert edit.call_args.args[0] == texts.CSV_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.csv_period_kb()

    async def test_export_7_sends_two_documents(
        self, session, user, doc, cb_answer
    ) -> None:
        await add_row(session, user)
        today = today_of(user)
        await stats.cb_csv_export(make_callback("csv:7", make_message()), session)
        assert doc.await_count == 2
        start = today - timedelta(days=6)
        first, second = doc.await_args_list
        assert first.args[0].filename == (
            f"bp_diary_{start:%Y%m%d}-{today:%Y%m%d}.csv"
        )
        assert second.args[0].filename == (
            f"bp_meds_{start:%Y%m%d}-{today:%Y%m%d}.csv"
        )
        for call in (first, second):
            assert call.kwargs["caption"] == texts.CSV_FILE_CAPTION

    async def test_export_all_time_uses_actual_range(
        self, session, user, doc, cb_answer
    ) -> None:
        stamp = utcnow().replace(second=0, microsecond=0)
        await add_row(session, user, timestamp=stamp)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert doc.await_count == 2
        day = stamp.astimezone(ZoneInfo(user.timezone)).date()
        assert doc.await_args_list[0].args[0].filename == (
            f"bp_diary_{day:%Y%m%d}-{day:%Y%m%d}.csv"
        )

    async def test_export_all_time_empty(
        self, session, user, doc, cb_answer
    ) -> None:
        today = today_of(user)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert doc.await_count == 2
        assert doc.await_args_list[0].args[0].filename == (
            f"bp_diary_{today:%Y%m%d}-{today:%Y%m%d}.csv"
        )

    async def test_export_too_many(
        self, session, user, edit, doc, cb_answer, mocker
    ) -> None:
        mocker.patch.object(csv_export, "ALL_TIME_LIMIT", 1)
        await add_row(session, user)
        await add_row(session, user)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert edit.call_args.args[0] == texts.CSV_TOO_MANY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.csv_period_kb()
        doc.assert_not_awaited()

    async def test_export_hides_foreign_data(
        self, session, user, doc, cb_answer
    ) -> None:
        other_user = UserModel(telegram_id=222_222)
        session.add(other_user)
        await session.commit()
        await add_row(session, user)
        await add_row(session, other_user, systolic=200)
        await stats.cb_csv_export(make_callback("csv:7", make_message()), session)
        content = doc.await_args_list[0].args[0].data
        assert b"135" in content
        assert b"200" not in content


# ─────────────────── Гистограмма: фото вместо текста (§6.5, 0.4.15) ───────────────────

import io  # noqa: E402

from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import DeleteMessage  # noqa: E402
from aiogram.types import PhotoSize  # noqa: E402
from PIL import Image  # noqa: E402

from bot.services import stats_chart  # noqa: E402


def photo_message() -> Message:
    """Сообщение-фото (гистограмма) с кнопками."""
    return Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=Chat(id=TG_ID, type="private"),
        from_user=User(id=TG_ID, is_bot=True, first_name="Бот"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1080, height=720)],
        caption="📊 …",
    )


class TestHistogram:
    async def test_summary_is_photo(self, session, user, photo, delete, edit, cb_answer) -> None:
        """Текст выбора периода удаляется, приходит фото: PNG 1080×720, подпись, кнопки."""
        await add_row(session, user, systolic=150, diastolic=95, pulse=70)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        delete.assert_awaited_once()
        edit.assert_not_called()
        document = photo.call_args.args[0]
        today = today_of(user)
        start = today - timedelta(days=6)
        assert document.filename == f"bp_stats_{start:%Y%m%d}-{today:%Y%m%d}.png"
        image = Image.open(io.BytesIO(document.data))
        assert image.size == (1080, 720) and image.format == "PNG"
        caption = photo.call_args.kwargs["caption"]
        assert caption.startswith("📊 За 7 дней") and len(caption) <= 1024
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb("7")

    async def test_render_in_thread_with_day_values(self, session, user, photo, delete, cb_answer, mocker) -> None:
        """В поток уходят значения по дням (не ORM), транзакция закрыта (§7.2 п.7)."""
        import threading

        seen = {}

        def fake_render(days, *, large=False):
            seen["thread"] = threading.current_thread() is not threading.main_thread()
            seen["days"] = days
            seen["in_tx"] = session.in_transaction()
            seen["large"] = large
            return b"PNG"

        mocker.patch.object(stats_chart, "render", side_effect=fake_render)
        await add_row(session, user, systolic=150, diastolic=95)
        await stats.cb_stats_show(make_callback("stats:30", make_message()), session)
        assert seen["thread"] is True and seen["in_tx"] is False
        assert len(seen["days"]) == 30
        assert all(isinstance(d, stats_chart.DayValues) for d in seen["days"])
        assert seen["days"][-1].systolic == 150
        assert seen["large"] is False  # обычный режим

    async def test_empty_period_stays_text(self, session, user, photo, delete, edit, cb_answer) -> None:
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        photo.assert_not_awaited()
        delete.assert_not_awaited()
        assert edit.call_args.args[0] == texts.STATS_EMPTY

    async def test_old_message_cannot_be_deleted(self, session, user, photo, delete, cb_answer) -> None:
        """Сообщение старше 48 ч не удаляется — фото всё равно приходит."""
        delete.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=1, message_id=1), message="message can't be deleted"
        )
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        photo.assert_awaited_once()

    @pytest.mark.parametrize(
        ("handler", "data", "text", "markup"),
        [
            ("cb_stats_periods", "stats:periods", texts.BTN_STATS, keyboards.stats_period_kb()),
            ("cb_csv_menu", "stats:csv", texts.CSV_ASK, keyboards.csv_period_kb()),
            ("cb_pdf_menu", "stats:pdf", texts.CSV_ASK, keyboards.pdf_period_kb()),
        ],
    )
    async def test_buttons_under_photo(self, delete, answer, edit, cb_answer, session, handler, data, text, markup) -> None:
        """«Назад» / «Экспорт CSV» / «Экспорт PDF» под фото: фото удаляется, экран — новым сообщением."""
        target = getattr(stats, handler)
        kwargs = {"session": session} if "session" in inspect.signature(target).parameters else {}
        await target(make_callback(data, photo_message()), **kwargs)
        delete.assert_awaited_once()
        edit.assert_not_called()
        assert answer.call_args.args[0] == text
        assert answer.call_args.kwargs["reply_markup"] == markup


# ─────────────────────── Свой период и «По дням» (§6.5, 0.5.3) ───────────────────────

from aiogram.exceptions import TelegramBadRequest  # noqa: E402

from bot.services import stats_chart  # noqa: E402


def local_noon(user: UserModel, day) -> datetime:
    """Полдень локального дня пользователя в UTC."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(user.timezone)).astimezone(timezone.utc)


def ymd(day) -> str:
    return day.strftime("%Y%m%d")


class TestCalendar:
    async def test_opens_current_month(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        await stats.cb_stats_calendar(make_callback("stats:cal", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_CAL_FIRST == "Выберите первый день периода:"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_calendar_kb(
            today.year, today.month, today
        )
        cb_answer.assert_awaited_once_with()

    async def test_month_navigation(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        await stats.cb_stats_calendar(make_callback("stats:cal:202501", make_message()), session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_calendar_kb(2025, 1, today)

    async def test_month_navigation_step2(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        await stats.cb_stats_calendar(
            make_callback("stats:cal:202501:20250115", make_message()), session
        )
        assert edit.call_args.args[0] == "Период с 15.01. Выберите последний день:"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_calendar_kb(
            2025, 1, today, today.replace(year=2025, month=1, day=15)
        )

    @pytest.mark.parametrize(
        "data",
        [
            "stats:cal:2025",
            "stats:cal:202513",
            "stats:cal:000001",
            "stats:cal:abcdef",
            "stats:cal:209912",  # будущий месяц
            "stats:cal:202501:2025011",
            "stats:cal:202501:20990101",  # будущий первый день
            "stats:cal:202501:20250101:x",
        ],
    )
    async def test_broken_returns_to_periods(self, session, user, edit, cb_answer, data) -> None:
        await stats.cb_stats_calendar(make_callback(data, make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_from_photo_is_new_message(self, session, user, delete, answer, edit, cb_answer) -> None:
        await stats.cb_stats_calendar(make_callback("stats:cal", photo_message()), session)
        delete.assert_awaited_once()
        edit.assert_not_called()
        assert answer.call_args.args[0] == texts.STATS_CAL_FIRST

    async def test_noop(self, cb_answer) -> None:
        await stats.cb_stats_noop(make_callback("stats:noop", make_message()))
        cb_answer.assert_awaited_once_with()

    async def test_double_tap_same_screen(self, session, user, mocker, cb_answer) -> None:
        """Повторное нажатие — тот же экран: «message is not modified» не ошибка."""
        mocker.patch.object(
            Message,
            "edit_text",
            new_callable=mocker.AsyncMock,
            side_effect=TelegramBadRequest(method=mocker.Mock(), message="Bad Request: message is not modified"),
        )
        await stats.cb_stats_calendar(make_callback("stats:cal", make_message()), session)
        cb_answer.assert_awaited()

    async def test_other_bad_request_raises(self, session, user, mocker, cb_answer) -> None:
        mocker.patch.object(
            Message,
            "edit_text",
            new_callable=mocker.AsyncMock,
            side_effect=TelegramBadRequest(method=mocker.Mock(), message="Bad Request: message to edit not found"),
        )
        with pytest.raises(TelegramBadRequest):
            await stats.cb_stats_calendar(make_callback("stats:cal", make_message()), session)


class TestDayPick:
    async def test_first_day_opens_step2(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        first = today - timedelta(days=40)
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(first)}", make_message()), session)
        assert edit.call_args.args[0] == f"Период с {first:%d.%m}. Выберите последний день:"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_calendar_kb(
            first.year, first.month, today, first
        )

    async def test_future_first_day_toast(self, session, user, edit, cb_answer) -> None:
        future = today_of(user) + timedelta(days=1)
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(future)}", make_message()), session)
        cb_answer.assert_awaited_once_with(texts.STATS_CAL_FUTURE)
        edit.assert_not_called()

    async def test_future_last_day_toast(self, session, user, edit, photo, cb_answer) -> None:
        today = today_of(user)
        data = f"stats:day:{ymd(today)}:{ymd(today + timedelta(days=1))}"
        await stats.cb_stats_day(make_callback(data, make_message()), session)
        cb_answer.assert_awaited_once_with("Период не может заканчиваться в будущем")
        edit.assert_not_called()
        photo.assert_not_called()

    async def test_too_long_toast(self, session, user, edit, photo, cb_answer) -> None:
        today = today_of(user)
        data = f"stats:day:{ymd(today - timedelta(days=31))}:{ymd(today)}"  # 32 дня
        await stats.cb_stats_day(make_callback(data, make_message()), session)
        cb_answer.assert_awaited_once_with("Период — не больше 31 дня")
        photo.assert_not_called()

    async def test_31_days_ok(self, session, user, photo, delete, cb_answer) -> None:
        today = today_of(user)
        start = today - timedelta(days=30)
        await add_row(session, user, timestamp=local_noon(user, start))
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(start)}:{ymd(today)}", make_message()), session)
        caption = photo.call_args.kwargs["caption"]
        assert caption.startswith(f"📊 За период {start:%d.%m}–{today:%d.%m} (31 день)")
        image = Image.open(io.BytesIO(photo.call_args.args[0].data))
        assert image.size == (1080, 720)

    async def test_summary_reversed_dates(self, session, user, photo, delete, cb_answer, mocker) -> None:
        today = today_of(user)
        start, end = today - timedelta(days=14), today - timedelta(days=10)
        await add_row(session, user, systolic=150, diastolic=95, timestamp=local_noon(user, start))
        await add_row(session, user, systolic=130, diastolic=85, timestamp=local_noon(user, end))
        await add_row(session, user, systolic=200, diastolic=120, timestamp=local_noon(user, today))  # вне периода
        render = mocker.spy(stats_chart, "render")
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(end)}:{ymd(start)}", make_message()), session)
        caption = photo.call_args.kwargs["caption"]
        assert caption.startswith(f"📊 За период {start:%d.%m}–{end:%d.%m} (5 дней)")
        assert texts.STATS_COUNT.format(count=2) in caption
        assert texts.STATS_SYSTOLIC.format(avg=140, min=130, max=150) in caption
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb(f"{ymd(start)}:{ymd(end)}")
        assert len(render.call_args.args[0]) == 5
        document = photo.call_args.args[0]
        assert document.filename == f"bp_stats_{ymd(start)}-{ymd(end)}.png"

    async def test_single_day(self, session, user, photo, delete, cb_answer) -> None:
        day = today_of(user) - timedelta(days=2)
        await add_row(session, user, timestamp=local_noon(user, day))
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(day)}:{ymd(day)}", make_message()), session)
        assert photo.call_args.kwargs["caption"].startswith(f"📊 За период {day:%d.%m}–{day:%d.%m} (1 день)")

    async def test_empty_period(self, session, user, edit, photo, cb_answer) -> None:
        day = today_of(user) - timedelta(days=2)
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(day)}:{ymd(day)}", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()
        photo.assert_not_called()

    @pytest.mark.parametrize(
        "data",
        [
            "stats:day:",
            "stats:day:2026",
            "stats:day:20261332",
            "stats:day:20261101:x",
            "stats:day:20261101:20261102:20261103",
        ],
    )
    async def test_broken(self, session, user, edit, cb_answer, data) -> None:
        await stats.cb_stats_day(make_callback(data, make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS

    async def test_other_users_data_not_shown(self, session, user, photo, delete, edit, cb_answer) -> None:
        """Изоляция (§7.1): сводка — только по записям текущего пользователя."""
        day = today_of(user) - timedelta(days=1)
        other = UserModel(telegram_id=222_222, timezone=user.timezone)
        session.add(other)
        await session.commit()
        await add_row(session, other, timestamp=local_noon(user, day))
        await stats.cb_stats_day(make_callback(f"stats:day:{ymd(day)}:{ymd(day)}", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_EMPTY
        photo.assert_not_called()


class TestCustomSummaryBack:
    async def test_stats_c_shows_summary(self, session, user, photo, delete, cb_answer) -> None:
        today = today_of(user)
        start = today - timedelta(days=3)
        await add_row(session, user, timestamp=local_noon(user, start))
        await stats.cb_stats_show(make_callback(f"stats:c:{ymd(start)}:{ymd(today)}", make_message()), session)
        assert photo.call_args.kwargs["caption"].startswith(f"📊 За период {start:%d.%m}–{today:%d.%m} (4 дня)")

    @pytest.mark.parametrize("suffix", ["x", "20261101", "20261101:x"])
    async def test_stats_c_broken(self, session, user, edit, photo, cb_answer, suffix) -> None:
        await stats.cb_stats_show(make_callback(f"stats:c:{suffix}", make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        photo.assert_not_called()

    async def test_stats_c_forged_too_long(self, session, user, edit, photo, cb_answer) -> None:
        today = today_of(user)
        data = f"stats:c:{ymd(today - timedelta(days=100))}:{ymd(today)}"
        await stats.cb_stats_show(make_callback(data, make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        photo.assert_not_called()


class TestByDays:
    async def test_list_from_photo(self, session, user, delete, answer, edit, cb_answer) -> None:
        today = today_of(user)
        d1, d3 = today - timedelta(days=6), today - timedelta(days=4)
        await add_row(session, user, systolic=130, diastolic=86, pulse=70, timestamp=local_noon(user, d1))
        await add_row(
            session, user, systolic=138, diastolic=86, pulse=72,
            timestamp=local_noon(user, d1) + timedelta(hours=1),
        )
        await add_row(session, user, systolic=120, diastolic=80, pulse=None, timestamp=local_noon(user, d3))
        await stats.cb_stats_days(make_callback("stats:days:7", photo_message()), session)
        delete.assert_awaited_once()
        edit.assert_not_called()
        text = answer.call_args.args[0]
        assert text == (
            f"<b>📅 Средние по дням</b> · {d1:%d.%m}–{today:%d.%m}\n"
            f"🟡 {d1:%d.%m} — 134/86, пульс 71\n"
            f"🟢 {d3:%d.%m} — 120/80\n"
            "Среднее за день: 127/83, пульс 71"
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.stats_days_kb("7")
        assert answer.call_args.kwargs["parse_mode"] == "HTML"

    async def test_list_30_days(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        first = today - timedelta(days=29)
        await add_row(session, user, systolic=150, diastolic=95, pulse=80, timestamp=local_noon(user, first))
        await stats.cb_stats_days(make_callback("stats:days:30", make_message()), session)
        lines = edit.call_args.args[0].splitlines()
        assert lines[0] == f"<b>📅 Средние по дням</b> · {first:%d.%m}–{today:%d.%m}"
        assert lines[1].startswith("🔴 ")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_days_kb("30")
        assert edit.call_args.kwargs["parse_mode"] == "HTML"

    async def test_list_custom(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        start, end = today - timedelta(days=40), today - timedelta(days=10)
        await add_row(session, user, timestamp=local_noon(user, start))
        key = f"{ymd(start)}:{ymd(end)}"
        await stats.cb_stats_days(make_callback(f"stats:days:{key}", make_message()), session)
        assert edit.call_args.args[0].startswith(f"<b>📅 Средние по дням</b> · {start:%d.%m}–{end:%d.%m}")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_days_kb(key)

    async def test_no_records(self, session, user, edit, cb_answer) -> None:
        await stats.cb_stats_days(make_callback("stats:days:7", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    @pytest.mark.parametrize("key", ["14", "x", "20261101", "20261101:20990101", "7:abc", "30:abc"])
    async def test_broken(self, session, user, edit, cb_answer, key) -> None:
        await stats.cb_stats_days(make_callback(f"stats:days:{key}", make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS

    async def test_back_to_photo(self, session, user, delete, photo, edit, cb_answer) -> None:
        """«Назад» из списка: текст удаляется, приходит фото сводки того же периода."""
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:30", make_message()), session)
        delete.assert_awaited_once()
        assert photo.call_args.kwargs["caption"].startswith("📊 За 30 дней")
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb("30")


# ─────────── Календарь stats:cal:m:/w: и пагинация «По дням» (0.5.10) ───────────


async def make_large(session, user) -> None:
    """Крупный режим пользователя (§11)."""
    user.ui_large = True
    await session.commit()


async def add_recent_rows(session, user, count: int) -> None:
    """По одному измерению в каждый из последних `count` дней (сегодня — последним)."""
    today = today_of(user)
    for offset in range(count):
        await add_row(session, user, timestamp=local_noon(user, today - timedelta(days=offset)))


class TestCalendarFormats:
    """Протокол `stats:cal:m:/w:` и совместимость прежних `stats:cal:{…}` (UI12, UI19)."""

    async def test_m_grid_like_legacy(self, session, user, edit, cb_answer) -> None:
        """`stats:cal:m:{ГГГГММ}` — сетка месяца (обычный режим)."""
        today = today_of(user)
        await stats.cb_stats_calendar(make_callback("stats:cal:m:202501", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_CAL_FIRST
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_calendar_kb(2025, 1, today)

    @pytest.mark.parametrize(
        ("legacy", "modern"),
        [
            ("stats:cal:202501", "stats:cal:m:202501"),
            ("stats:cal:202501:20250115", "stats:cal:m:202501:20250115"),
        ],
    )
    async def test_legacy_same_as_m(self, session, user, edit, cb_answer, legacy, modern) -> None:
        """Прежние `stats:cal:{ГГГГММ}[:{с}]` = `:m:`-вариант (UI19)."""
        await stats.cb_stats_calendar(make_callback(legacy, make_message()), session)
        legacy_markup = edit.call_args.kwargs["reply_markup"]
        await stats.cb_stats_calendar(make_callback(modern, make_message()), session)
        assert edit.call_args.kwargs["reply_markup"] == legacy_markup

    async def test_m_arrows_availability(self, session, user, edit, cb_answer) -> None:
        """«◀»/«▶» по доступности: в текущем месяце «▶» нет, в прошлом — обе стрелки."""
        today = today_of(user)
        current = f"{today.year:04d}{today.month:02d}"
        await stats.cb_stats_calendar(make_callback(f"stats:cal:m:{current}", make_message()), session)
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert texts.BTN_CAL_PREV in labels and texts.BTN_CAL_NEXT not in labels
        prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        assert f"stats:cal:m:{prev[0]:04d}{prev[1]:02d}" in callbacks
        await stats.cb_stats_calendar(make_callback("stats:cal:m:202501", make_message()), session)
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert texts.BTN_CAL_PREV in labels and texts.BTN_CAL_NEXT in labels
        assert "stats:cal:m:202412" in callbacks and "stats:cal:m:202502" in callbacks

    async def test_m_minyear_only_next(self, session, user, edit, cb_answer) -> None:
        """Январь 1 года: «◀» не показывается (MINYEAR), «▶» — в февраль."""
        await stats.cb_stats_calendar(make_callback("stats:cal:m:000101", make_message()), session)
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert texts.BTN_CAL_PREV not in labels and texts.BTN_CAL_NEXT in labels
        assert "stats:cal:m:000102" in callbacks

    async def test_w_week_list(self, session, user, edit, cb_answer) -> None:
        """`stats:cal:w:{пн}` в крупном — список недели вместо сетки (UI12)."""
        await make_large(session, user)
        today = today_of(user)
        monday = today - timedelta(days=today.weekday())
        week_end = monday + timedelta(days=6)
        await stats.cb_stats_calendar(make_callback(f"stats:cal:w:{ymd(monday)}", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_CAL_FIRST
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert texts.CAL_WEEK_LABEL.format(
            date_from=f"{monday.day}.{monday.month}", date_to=f"{week_end.day}.{week_end.month}"
        ) in labels
        for offset in range(7):
            day = monday + timedelta(days=offset)
            if day <= today:
                assert f"{texts.STATS_CAL_WEEKDAYS[offset]} {day.day}.{day.month}" in labels
                assert f"stats:day:{ymd(day)}" in callbacks
            else:
                assert f"stats:day:{ymd(day)}" not in callbacks
        # «◀» — всегда на будущих неделях; для текущей — «▶» недоступен (UI12)
        assert texts.BTN_CAL_PREV in labels and texts.BTN_CAL_NEXT not in labels
        assert callbacks[-1] == "stats:periods"

    async def test_w_week_prev_has_next(self, session, user, edit, cb_answer) -> None:
        """На неделе позади «▶» возвращает к текущей, «◀» листает ещё назад (UI12)."""
        await make_large(session, user)
        today = today_of(user)
        today_week = today - timedelta(days=today.weekday())
        prev_week = today_week - timedelta(days=7)
        await stats.cb_stats_calendar(make_callback(f"stats:cal:w:{ymd(prev_week)}", make_message()), session)
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert texts.BTN_CAL_PREV in labels and texts.BTN_CAL_NEXT in labels
        assert f"stats:cal:w:{ymd(prev_week - timedelta(days=7))}" in callbacks
        assert f"stats:cal:w:{ymd(today_week)}" in callbacks

    async def test_m_step2_marks_day(self, session, user, edit, cb_answer) -> None:
        """Шаг 2 `stats:cal:m:{мм}:{с}`: день отмечен «[N]», клики несут обе даты."""
        start = date(2025, 1, 15)
        await stats.cb_stats_calendar(
            make_callback(f"stats:cal:m:202501:{ymd(start)}", make_message()), session
        )
        assert edit.call_args.args[0] == texts.STATS_CAL_LAST.format(date=f"{start:%d.%m}")
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert "[15]" in labels
        assert f"stats:day:{ymd(start)}:{ymd(start)}" in callbacks
        assert f"stats:day:{ymd(start)}:{ymd(date(2025, 1, 20))}" in callbacks

    async def test_w_step2_marks_day(self, session, user, edit, cb_answer) -> None:
        """Шаг 2 `stats:cal:w:{пн}:{с}`: выбранный день — «✓ N.M», клики — `stats:day:{с}:{д}`."""
        await make_large(session, user)
        today = today_of(user)
        monday = today - timedelta(days=today.weekday())
        await stats.cb_stats_calendar(
            make_callback(f"stats:cal:w:{ymd(monday)}:{ymd(monday)}", make_message()), session
        )
        assert edit.call_args.args[0] == texts.STATS_CAL_LAST.format(date=f"{monday:%d.%m}")
        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        callbacks = [b.callback_data for row in rows for b in row]
        assert f"✓ {monday.day}.{monday.month}" in labels
        assert f"stats:day:{ymd(monday)}:{ymd(monday)}" in callbacks
        assert f"stats:day:{ymd(monday)}:{ymd(today)}" in callbacks

    @pytest.mark.parametrize(
        "data",
        [
            "stats:cal:m:2025",
            "stats:cal:m:209912",  # будущий месяц
            "stats:cal:m:202501:20990101",  # будущий первый день
            "stats:cal:w:2025",
            "stats:cal:w:20250101:20990101",  # будущий первый день
            "stats:cal:w:20250101:20250102:x",
        ],
    )
    async def test_broken(self, session, user, edit, cb_answer, data) -> None:
        """Мусор в любом из форматов — возврат на экран «Статистика»."""
        await stats.cb_stats_calendar(make_callback(data, make_message()), session)
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()


class TestDaysPagination:
    """Пагинация «По дням»: `stats:days:{…}[:{limit}]`, по 5 в крупном (UI13, §13)."""

    @pytest.mark.parametrize(
        ("limit", "rows_shown", "more_count", "more_data"),
        [
            (5, 5, 5, "stats:days:30:10"),
            (10, 10, 2, "stats:days:30:15"),
            (15, 12, None, None),  # 12 измеренных дней — всё показано
        ],
    )
    async def test_large_pages(
        self, session, user, edit, cb_answer, limit, rows_shown, more_count, more_data
    ) -> None:
        """Крупный: показаны первые `limit` строк, «Показать ещё {min(5, N−limit)}»."""
        await make_large(session, user)
        await add_recent_rows(session, user, 12)
        today = today_of(user)
        await stats.cb_stats_days(make_callback(f"stats:days:30:{limit}", make_message()), session)
        lines = edit.call_args.args[0].splitlines()
        assert lines[0] == f"<b>📅 Средние по дням</b> · {today - timedelta(days=29):%d.%m}–{today:%d.%m}"
        assert len(lines) == rows_shown + 3  # шапка + пустая строка (крупный) + итог
        day_lines = lines[2 : 2 + rows_shown]
        oldest = today - timedelta(days=11)  # 12 измеренных дней — со старшего
        assert f"{oldest:%d.%m}" in day_lines[0]
        assert f"{oldest + timedelta(days=rows_shown - 1):%d.%m}" in day_lines[-1]
        assert f"{oldest + timedelta(days=rows_shown):%d.%m}" not in "\n".join(day_lines)
        assert edit.call_args.kwargs["parse_mode"] == "HTML"
        markup = edit.call_args.kwargs["reply_markup"]
        assert markup == keyboards.stats_days_kb("30", large=True, total=12, limit=limit)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        if more_data is None:
            assert not any(d.startswith("stats:days:30:") for d in callbacks)
        else:
            labels = [b.text for row in markup.inline_keyboard for b in row]
            assert texts.BTN_SHOW_MORE.format(count=more_count) in labels
            assert more_data in callbacks

    async def test_normal_ignores_limit(self, session, user, edit, cb_answer) -> None:
        """Обычный режим — весь список сразу, `limit` игнорируется (UI13)."""
        await add_recent_rows(session, user, 12)
        today = today_of(user)
        await stats.cb_stats_days(make_callback("stats:days:30:5", make_message()), session)
        lines = edit.call_args.args[0].splitlines()
        assert len(lines) == 12 + 2  # шапка + 12 строк + итог (без пустой строки)
        assert f"{today:%d.%m}" in lines[-2]  # последняя строка дней — сегодня
        markup = edit.call_args.kwargs["reply_markup"]
        assert markup == keyboards.stats_days_kb("30", large=False, total=12, limit=12)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert not any(d.startswith("stats:days:30:") for d in callbacks)

    async def test_custom_period_with_limit(self, session, user, edit, cb_answer) -> None:
        """Пагинация и своего периода: `stats:days:{с}:{по}:{limit}` (UI13)."""
        await make_large(session, user)
        today = today_of(user)
        start, end = today - timedelta(days=4), today
        for offset in range(3):
            await add_row(session, user, timestamp=local_noon(user, start + timedelta(days=offset)))
        key = f"{ymd(start)}:{ymd(end)}"
        await stats.cb_stats_days(make_callback(f"stats:days:{key}:2", make_message()), session)
        lines = edit.call_args.args[0].splitlines()
        assert lines[0] == f"<b>📅 Средние по дням</b> · {start:%d.%m}–{end:%d.%m}"
        assert len(lines) == 2 + 3  # шапка + пустая + 2 строки + итог
        markup = edit.call_args.kwargs["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert texts.BTN_SHOW_MORE.format(count=1) in labels
        assert f"stats:days:{key}:7" in callbacks
        assert f"stats:c:{key}" in callbacks  # «Назад» — к сводке своего периода
