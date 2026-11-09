"""Тесты хендлеров статистики и экспорта CSV (bot/handlers/stats.py, SPEC.md, §6.5, §10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        await stats.cb_stats_periods(make_callback("menu:stats", make_message()))
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_periods_returns(self, session, edit, cb_answer) -> None:
        await stats.cb_stats_periods(make_callback("stats:periods", make_message()))
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
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb()

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
        assert photo.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb()

    async def test_render_in_thread_with_day_values(self, session, user, photo, delete, cb_answer, mocker) -> None:
        """В поток уходят значения по дням (не ORM), транзакция закрыта (§7.2 п.7)."""
        import threading

        seen = {}

        def fake_render(days):
            seen["thread"] = threading.current_thread() is not threading.main_thread()
            seen["days"] = days
            seen["in_tx"] = session.in_transaction()
            return b"PNG"

        mocker.patch.object(stats_chart, "render", side_effect=fake_render)
        await add_row(session, user, systolic=150, diastolic=95)
        await stats.cb_stats_show(make_callback("stats:30", make_message()), session)
        assert seen["thread"] is True and seen["in_tx"] is False
        assert len(seen["days"]) == 30
        assert all(isinstance(d, stats_chart.DayValues) for d in seen["days"])
        assert seen["days"][-1].systolic == 150

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
    async def test_buttons_under_photo(self, delete, answer, edit, cb_answer, handler, data, text, markup) -> None:
        """«Назад» / «Экспорт CSV» / «Экспорт PDF» под фото: фото удаляется, экран — новым сообщением."""
        await getattr(stats, handler)(make_callback(data, photo_message()))
        delete.assert_awaited_once()
        edit.assert_not_called()
        assert answer.call_args.args[0] == text
        assert answer.call_args.kwargs["reply_markup"] == markup
