"""«Экспорт PDF» в боте (bot/handlers/stats.py, settings.py; SPEC.md, §6.5, §6.6, §10.1).

Покрывается: вход из статистики и настроек (экран периода `pdf:*`),
выгрузка за 7 / 30 дней и «всё время» (от первой записи до сегодня):
«Готовлю отчёт…» → документ с подписью «Отчёт по давлению за ДД.ММ–ДД.ММ»
и именем `bp_diary_<дата1>-<дата2>.pdf` → снова экран периода; пустой
период и лимит — текст вместо файла; транзакция закрыта до генерации;
двойное нажатие — одна выгрузка (§7.2).
"""
from __future__ import annotations

import asyncio
import io
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import settings as settings_handlers
from bot.handlers import stats
from bot.models import Measurement, User as UserModel
from bot.services import pdf_report

TG_ID = 111_111
TZ = "Europe/Minsk"


def make_message(user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text="x",
    )


def make_callback(data: str, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=make_message(user_id),
    )


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def doc(mocker):
    return mocker.patch.object(Message, "answer_document", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


async def add_row(session: AsyncSession, user: UserModel, ago: timedelta, systolic: int = 135) -> None:
    session.add(
        Measurement(
            user_id=user.id,
            timestamp=datetime.now(timezone.utc) - ago,
            systolic=systolic,
            diastolic=85,
            pulse=72,
        )
    )
    await session.commit()


def today():
    return datetime.now(ZoneInfo(TZ)).date()


def edits(edit) -> list[str]:
    return [call.args[0] for call in edit.call_args_list]


# ─────────────────────────── Вход ───────────────────────────


class TestPdfMenu:
    async def test_from_stats(self, edit, cb_answer) -> None:
        await stats.cb_pdf_menu(make_callback("stats:pdf"))
        assert edit.call_args.args[0] == texts.CSV_ASK == "За какой период выгрузить?"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pdf_period_kb()
        cb_answer.assert_awaited_once()

    async def test_from_settings(self, edit, cb_answer) -> None:
        await settings_handlers.cb_pdf(make_callback("set:pdf"))
        assert edit.call_args.args[0] == texts.CSV_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pdf_period_kb()
        cb_answer.assert_awaited_once()


# ─────────────────────────── Выгрузка ───────────────────────────


class TestPdfExport:
    @pytest.mark.parametrize("days", [7, 30])
    async def test_period(self, session, user, edit, doc, cb_answer, days) -> None:
        await add_row(session, user, timedelta(hours=1), systolic=154)
        await stats.cb_pdf_export(make_callback(f"pdf:{days}"), session)
        start = today() - timedelta(days=days - 1)
        # «Готовлю отчёт за ДД.ММ–ДД.ММ…» — период виден сразу (R4, 2026-11-18).
        assert edits(edit) == [
            texts.PDF_PREPARING_PERIOD.format(
                date_from=f"{start:%d.%m}", date_to=f"{today():%d.%m}"
            ),
            texts.CSV_ASK,
        ]
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pdf_period_kb()
        document = doc.call_args.args[0]
        assert document.filename == f"bp_diary_{start:%Y%m%d}-{today():%Y%m%d}.pdf"
        assert doc.call_args.kwargs["caption"] == (
            f"Отчёт по давлению за {start:%d.%m}–{today():%d.%m}"
        )
        text = PdfReader(io.BytesIO(document.data)).pages[0].extract_text()
        assert "Дневник давления · сформирован" in text
        assert str(TG_ID) not in text
        assert "154" in text
        cb_answer.assert_awaited_once_with()

    async def test_all_time_from_first_record(self, session, user, edit, doc, cb_answer) -> None:
        await add_row(session, user, timedelta(days=100))
        await add_row(session, user, timedelta(hours=1))
        await stats.cb_pdf_export(make_callback("pdf:all"), session)
        first = (datetime.now(timezone.utc) - timedelta(days=100)).astimezone(ZoneInfo(TZ)).date()
        assert doc.call_args.args[0].filename == f"bp_diary_{first:%Y%m%d}-{today():%Y%m%d}.pdf"
        assert doc.call_args.kwargs["caption"] == (
            f"Отчёт по давлению за {first:%d.%m}–{today():%d.%m}"
        )

    async def test_empty(self, session, user, edit, doc, cb_answer) -> None:
        await add_row(session, user, timedelta(days=20))
        await stats.cb_pdf_export(make_callback("pdf:7"), session)
        doc.assert_not_awaited()
        assert texts.PDF_EMPTY in edits(edit)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pdf_period_kb()
        assert TG_ID not in stats._pdf_in_progress

    async def test_new_user_empty(self, session, edit, doc, cb_answer) -> None:
        """Пользователь без записи в БД — создаётся, отчёт пустой (без падения)."""
        await stats.cb_pdf_export(make_callback("pdf:all"), session)
        doc.assert_not_awaited()
        assert edit.call_args.args[0] == texts.PDF_EMPTY

    async def test_too_many(self, session, user, edit, doc, cb_answer, monkeypatch) -> None:
        monkeypatch.setattr(pdf_report, "REPORT_LIMIT", 1)
        await add_row(session, user, timedelta(hours=2))
        await add_row(session, user, timedelta(hours=1))
        await stats.cb_pdf_export(make_callback("pdf:all"), session)
        doc.assert_not_awaited()
        assert edit.call_args.args[0] == texts.CSV_TOO_MANY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pdf_period_kb()

    async def test_transaction_closed_before_generation(
        self, session, user, edit, doc, cb_answer, monkeypatch
    ) -> None:
        """Генерация — в потоке и при закрытой транзакции (§7.2 п.7, CLAUDE.md §3)."""
        await add_row(session, user, timedelta(hours=1))
        seen: dict[str, object] = {}

        def fake_build(data: pdf_report.ReportData) -> bytes:
            seen["in_transaction"] = session.in_transaction()
            seen["thread"] = threading.current_thread() is not threading.main_thread()
            return b"%PDF-fake"

        monkeypatch.setattr(pdf_report, "build_pdf", fake_build)
        await stats.cb_pdf_export(make_callback("pdf:7"), session)
        assert seen == {"in_transaction": False, "thread": True}
        assert doc.call_args.args[0].data == b"%PDF-fake"

    async def test_double_tap_one_export(
        self, session, user, edit, doc, cb_answer, monkeypatch
    ) -> None:
        """Пока отчёт готовится, повторное нажатие новую выгрузку не запускает (§7.2)."""
        await add_row(session, user, timedelta(hours=1))
        started, release = threading.Event(), threading.Event()

        def slow_build(data: pdf_report.ReportData) -> bytes:
            started.set()
            release.wait(5)
            return b"%PDF-slow"

        monkeypatch.setattr(pdf_report, "build_pdf", slow_build)
        first = asyncio.create_task(stats.cb_pdf_export(make_callback("pdf:7"), session))
        assert await asyncio.to_thread(started.wait, 5)
        await stats.cb_pdf_export(make_callback("pdf:30"), session)
        cb_answer.assert_awaited_with(texts.PDF_PREPARING)
        release.set()
        await first
        assert doc.await_count == 1
        assert TG_ID not in stats._pdf_in_progress
        # после завершения кнопка снова работает
        await stats.cb_pdf_export(make_callback("pdf:7"), session)
        assert doc.await_count == 2


# ─────────────────── Свой период (2026-11-18, R1–R4) ───────────────────


def ymd(day) -> str:
    return day.strftime("%Y%m%d")


class TestPdfCustomPeriod:
    """«📆 Свой период» выгрузки PDF: календарь §6.5 без лимита длины (R3)."""

    async def test_custom_opens_calendar(self, session, user, edit, cb_answer) -> None:
        """`pdf:custom` — календарь области pdf: дни `pdf:day:…`, «Назад» — вопрос."""
        await stats.cb_export_calendar(make_callback("pdf:custom"), session)
        assert edits(edit) == [texts.STATS_CAL_FIRST]
        markup = edit.call_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert any(data.startswith("pdf:day:") for data in callbacks)
        assert "stats:pdf" in callbacks  # «Назад» — на «За какой период»
        assert not any(data.startswith("stats:") for data in callbacks if data != "stats:pdf")
        cb_answer.assert_awaited_once_with()

    async def test_two_days_flow(self, session, user, edit, doc, cb_answer) -> None:
        """Тап-начало, тап-конец → сразу генерация (R4), «Готовлю…» с периодом."""
        await add_row(session, user, timedelta(days=3))
        await add_row(session, user, timedelta(days=1))
        start, end = today() - timedelta(days=3), today()
        await stats.cb_export_day(
            make_callback(f"pdf:day:{ymd(start)}:{ymd(end)}"), session
        )
        assert edits(edit) == [
            texts.PDF_PREPARING_PERIOD.format(
                date_from=f"{start:%d.%m}", date_to=f"{end:%d.%m}"
            ),
            texts.CSV_ASK,
        ]
        assert doc.call_args.args[0].filename == (
            f"bp_diary_{start:%Y%m%d}-{end:%Y%m%d}.pdf"
        )
        assert doc.call_args.kwargs["caption"] == (
            f"Отчёт по давлению за {start:%d.%m}–{end:%d.%m}"
        )

    async def test_longer_than_31_days_ok(self, session, user, edit, doc, cb_answer) -> None:
        """Лимита длины нет (R3): 91 день выгружается, как «всё время»."""
        await add_row(session, user, timedelta(days=90))
        await add_row(session, user, timedelta(hours=1))
        start = today() - timedelta(days=90)
        await stats.cb_export_day(
            make_callback(f"pdf:day:{ymd(start)}:{ymd(today())}"), session
        )
        assert doc.await_count == 1
        cb_answer.assert_awaited_once_with()  # без уведомления об ошибке

    async def test_reversed_dates_swapped(self, session, user, edit, doc, cb_answer) -> None:
        """Обратный порядок дат — меняются местами (§6.5, R2)."""
        await add_row(session, user, timedelta(days=2))
        await add_row(session, user, timedelta(hours=1))
        start, end = today() - timedelta(days=2), today()
        await stats.cb_export_day(
            make_callback(f"pdf:day:{ymd(end)}:{ymd(start)}"), session
        )
        assert doc.call_args.args[0].filename == (
            f"bp_diary_{start:%Y%m%d}-{end:%Y%m%d}.pdf"
        )

    async def test_future_day_toast(self, session, user, edit, doc, cb_answer) -> None:
        """Будущее — всплывающее уведомление, без выгрузки (§6.5)."""
        future = today() + timedelta(days=1)
        await stats.cb_export_day(make_callback(f"pdf:day:{ymd(future)}"), session)
        cb_answer.assert_awaited_once_with(texts.STATS_CAL_FUTURE)
        doc.assert_not_awaited()

    async def test_empty_uses_retry_kb(self, session, user, edit, doc, cb_answer) -> None:
        """Пустой период (R4): «За этот период записей нет» + «Изменить период»."""
        await stats.cb_export_day(
            make_callback(f"pdf:day:{ymd(today())}:{ymd(today())}"), session
        )
        doc.assert_not_awaited()
        assert edits(edit) == [texts.PDF_PREPARING_PERIOD.format(
            date_from=f"{today():%d.%m}", date_to=f"{today():%d.%m}"
        ), texts.PDF_EMPTY]
        assert edit.call_args.kwargs["reply_markup"] == keyboards.export_retry_kb("pdf")

    async def test_too_many_uses_retry_kb(
        self, session, user, edit, doc, cb_answer, monkeypatch
    ) -> None:
        """Лимит 5000 (R3): «Слишком много записей…» + «Изменить период» / «Назад»."""
        monkeypatch.setattr(pdf_report, "REPORT_LIMIT", 1)
        await add_row(session, user, timedelta(hours=2))
        await add_row(session, user, timedelta(hours=1))
        await stats.cb_export_day(
            make_callback(f"pdf:day:{ymd(today())}:{ymd(today())}"), session
        )
        doc.assert_not_awaited()
        assert edit.call_args.args[0] == texts.CSV_TOO_MANY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.export_retry_kb("pdf")


def test_texts() -> None:
    assert texts.BTN_EXPORT_PDF == "Экспорт PDF"
    assert texts.PDF_PREPARING == "Готовлю отчёт…"
    assert texts.PDF_PREPARING_PERIOD.format(date_from="12.10", date_to="20.10") == (
        "Готовлю отчёт за 12.10–20.10…"
    )
    assert texts.PDF_EMPTY == "За этот период записей нет"
    assert texts.PDF_FILE_CAPTION.format(date_from="01.10", date_to="30.10") == (
        "Отчёт по давлению за 01.10–30.10"
    )
