"""PDF-отчёт по давлению (bot/services/pdf_report.py, SPEC.md, §10.1).

Покрывается: ячейка «Лекарство» (как в «Истории записей» ЛК), подсветка
строк по худшей зоне и непрозрачные цвета из `chart_zones.py`, строки
сводки (те же, что `stats.summary`), сбор данных за период и «всё время»,
пустой период и лимит (файл не формируется), сам PDF — шапка врача и
пациента, легенда, колонки, перенос длинного текста, повтор шапки таблицы
на новой странице, «Стр. N из M». Текст PDF читается `pypdf`.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Appointment, AppointmentConclusion, Measurement, MeasurementMedication, Medication, User
from bot.services import csv_export, pdf_report, stats
from bot.services import medications as medications_service
from web.cabinet.chart_zones import CHART_ZONES

TZ = "Europe/Minsk"
NOW = datetime(2026, 11, 8, 11, 5, tzinfo=timezone.utc)  # 14:05 по Минску


# ─────────────────────────── Вспомогательное ───────────────────────────


def _pdf_text(payload: bytes) -> list[str]:
    """Текст каждой страницы PDF."""
    return [page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages]


def _pdf_fills(payload: bytes) -> set[tuple[float, float, float]]:
    """Цвета заливки (оператор `rg`) со всех страниц PDF."""
    fills: set[tuple[float, float, float]] = set()
    for page in PdfReader(io.BytesIO(payload)).pages:
        stream = page.get_contents().get_data().decode("latin-1")
        for r, g, b in re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) rg", stream):
            fills.add((float(r), float(g), float(b)))
    return fills


def _has_fill(fills: set[tuple[float, float, float]], rgb: tuple[int, int, int]) -> bool:
    """Есть ли в PDF заливка цвета `rgb` (0–255; в PDF — доли с округлением)."""
    target = tuple(channel / 255 for channel in rgb)
    return any(all(abs(a - b) < 0.002 for a, b in zip(fill, target)) for fill in fills)


def _row(**overrides) -> pdf_report.ReportRow:
    values = {
        "date": "01.11.2026",
        "time": "08:15",
        "systolic": "120",
        "diastolic": "80",
        "pulse": "72",
        "medication": "—",
        "feeling": "—",
        "note": "",
        "zone": None,
    }
    values.update(overrides)
    return pdf_report.ReportRow(**values)


def _data(rows=None, subject: str = pdf_report.PATIENT_SUBJECT, lines=None) -> pdf_report.ReportData:
    return pdf_report.ReportData(
        subject=subject,
        start_day=date(2026, 10, 1),
        end_day=date(2026, 10, 30),
        generated_at=NOW.astimezone(ZoneInfo(TZ)),
        summary_lines=tuple(lines or ("Записей: 1", "Тренд: стабильно")),
        rows=tuple(rows if rows is not None else [_row()]),
    )


async def _add(
    session: AsyncSession,
    user: User,
    moment: datetime,
    systolic: int = 128,
    diastolic: int = 82,
    **extra,
) -> Measurement:
    row = Measurement(
        user_id=user.id, timestamp=moment, systolic=systolic, diastolic=diastolic, **extra
    )
    session.add(row)
    await session.commit()
    return row


# ─────────────────────────── Ячейка «Лекарство» ───────────────────────────


class TestMedicationCell:
    @pytest.mark.parametrize(
        ("flag", "expected"), [(True, "Принято"), (False, "Пропущено"), (None, "—")]
    )
    def test_flag_without_intake(self, flag, expected) -> None:
        assert medications_service.medication_cell(flag, []) == expected

    def test_intake_list(self) -> None:
        cell = medications_service.medication_cell(True, ["Лозартан 50 мг", "Амлодипин 5 мг"])
        assert cell == "Принято: Лозартан 50 мг, Амлодипин 5 мг"

    async def test_cells_by_measurement(self, session, user) -> None:
        med = Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning")
        session.add(med)
        await session.commit()
        linked = await _add(session, user, NOW - timedelta(hours=2), medication_taken=True)
        flag_only = await _add(session, user, NOW - timedelta(hours=1), medication_taken=False)
        session.add(MeasurementMedication(measurement_id=linked.id, medication_id=med.id))
        await session.commit()
        cells = await medications_service.medication_cells(session, user, [linked, flag_only])
        assert cells == {linked.id: "Принято: Лозартан 50 мг", flag_only.id: "Пропущено"}


# ─────────────────────────── Зоны и цвета ───────────────────────────


class TestZones:
    @pytest.mark.parametrize(
        ("systolic", "diastolic", "level"),
        [
            (120, 80, None),
            (129, 84, None),
            (130, 80, 1),
            (120, 85, 1),
            (139, 89, 1),
            (140, 80, 2),
            (154, 84, 2),  # худшая из двух — САД красная
            (132, 90, 2),  # ДАД красная при жёлтой САД
        ],
    )
    def test_row_zone_level(self, systolic, diastolic, level) -> None:
        assert pdf_report.row_zone_level(systolic, diastolic) == level

    def test_opaque_fill_blends_on_white(self) -> None:
        assert pdf_report.opaque_fill("rgba(192,57,43,0.12)") == (247, 231, 230)
        assert pdf_report.opaque_fill(" rgba(0, 0, 0, 1) ") == (0, 0, 0)
        assert pdf_report.opaque_fill("rgba(10,20,30,0)") == (255, 255, 255)

    def test_opaque_fill_rejects_other_formats(self) -> None:
        with pytest.raises(ValueError):
            pdf_report.opaque_fill("#ffcc00")

    @pytest.mark.parametrize("level", [1, 2])
    def test_zone_fill_from_chart_zones(self, level) -> None:
        """Цвет — из `chart_zones.py`, альфа наложена на белый, а не отброшена."""
        match = re.fullmatch(
            r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", CHART_ZONES["systolic"][level]["fill"]
        )
        assert match is not None
        base = [int(match.group(i)) for i in (1, 2, 3)]
        alpha = float(match.group(4))
        expected = tuple(round(255 - (255 - c) * alpha) for c in base)
        assert pdf_report.zone_fill(level) == expected
        assert pdf_report.zone_fill(level) != tuple(base)


# ─────────────────────────── Сводка ───────────────────────────


class TestSummaryLines:
    def test_full(self) -> None:
        summary = stats.StatsSummary(
            count=12,
            sys_avg=128,
            sys_min=115,
            sys_max=145,
            dia_avg=82,
            dia_min=72,
            dia_max=92,
            pulse_avg=68,
            trend="стабильно",
            goal_verdict="в пределах цели",
            adherence=(18, 21),
        )
        assert pdf_report.summary_lines(summary) == (
            "Записей: 12",
            "Систолическое: в среднем 128 (115–145)",
            "Диастолическое: в среднем 82 (72–92)",
            "Пульс: в среднем 68",
            "Тренд: стабильно",
            "Цель: в пределах цели",
            "Лекарства: принято 18 из 21 приёмов",
        )

    def test_without_pulse_goal_and_medications(self) -> None:
        summary = stats.StatsSummary(
            count=1, sys_avg=120, sys_min=120, sys_max=120, dia_avg=80, dia_min=80, dia_max=80
        )
        lines = pdf_report.summary_lines(summary)
        assert lines == (
            "Записей: 1",
            "Систолическое: в среднем 120 (120–120)",
            "Диастолическое: в среднем 80 (80–80)",
            f"Тренд: {texts.STATS_TREND_FEW}",
        )


# ─────────────────────────── Строки таблицы ───────────────────────────


class TestReportRows:
    def test_values_like_cabinet_history(self) -> None:
        moment = datetime(2026, 11, 1, 5, 15, tzinfo=timezone.utc)  # 08:15 Минск
        full = Measurement(
            id=1, user_id=1, timestamp=moment, systolic=154, diastolic=84,
            pulse=72, feeling="headache", note="после прогулки",
        )
        bare = Measurement(id=2, user_id=1, timestamp=moment, systolic=120, diastolic=80)
        rows = pdf_report.report_rows(
            [full, bare], {1: "Принято: Лозартан 50 мг", 2: "—"}, TZ
        )
        assert rows[0].cells() == (
            "01.11.2026", "08:15", "154", "84", "72",
            "Принято: Лозартан 50 мг", "головная боль", "после прогулки",
        )
        assert rows[0].zone == 2
        assert rows[1].cells() == ("01.11.2026", "08:15", "120", "80", "—", "—", "—", "")
        assert rows[1].zone is None


# ─────────────────────────── Сбор данных ───────────────────────────


class TestCollectReport:
    async def test_period(self, session, user) -> None:
        today = NOW.astimezone(ZoneInfo(TZ)).date()
        start = today - timedelta(days=6)
        await _add(session, user, NOW - timedelta(days=10))  # вне периода
        await _add(session, user, NOW - timedelta(days=2), 150, 95, pulse=70)
        await _add(session, user, NOW - timedelta(days=1), 122, 78)
        data = await pdf_report.collect_report(
            session, user, start, today, subject="Пациент 111111", now=NOW
        )
        assert data.subject == "Пациент 111111"
        assert (data.start_day, data.end_day) == (start, today)
        assert data.generated_at == NOW.astimezone(ZoneInfo(TZ))
        assert [row.systolic for row in data.rows] == ["150", "122"]  # по возрастанию
        assert [row.zone for row in data.rows] == [2, None]
        expected = pdf_report.summary_lines(await stats.summary(session, user, start, today))
        assert data.summary_lines == expected
        assert data.file_name == csv_export.file_name("bp_diary", start, today, ext="pdf")
        assert data.file_name == f"bp_diary_{start:%Y%m%d}-{today:%Y%m%d}.pdf"

    async def test_all_time_from_first_record(self, session, user) -> None:
        first = NOW - timedelta(days=40)
        await _add(session, user, first)
        await _add(session, user, NOW - timedelta(days=1))
        data = await pdf_report.collect_report(
            session, user, None, None, subject=pdf_report.PATIENT_SUBJECT, now=NOW
        )
        assert data.start_day == first.astimezone(ZoneInfo(TZ)).date()
        assert data.end_day == NOW.astimezone(ZoneInfo(TZ)).date()
        assert len(data.rows) == 2

    async def test_generated_at_defaults_to_now(self, session, user) -> None:
        await _add(session, user, datetime.now(timezone.utc) - timedelta(hours=1))
        data = await pdf_report.collect_report(
            session, user, None, None, subject=pdf_report.PATIENT_SUBJECT
        )
        assert data.generated_at.tzinfo is not None
        assert data.generated_at.utcoffset() == timedelta(hours=3)  # Минск

    async def test_empty_period(self, session, user) -> None:
        await _add(session, user, NOW - timedelta(days=30))
        today = NOW.astimezone(ZoneInfo(TZ)).date()
        with pytest.raises(pdf_report.ReportEmptyError):
            await pdf_report.collect_report(
                session, user, today - timedelta(days=6), today, subject="x", now=NOW
            )

    async def test_empty_all_time(self, session, user) -> None:
        with pytest.raises(pdf_report.ReportEmptyError):
            await pdf_report.collect_report(session, user, None, None, subject="x", now=NOW)

    async def test_limit(self, session, user) -> None:
        await _add(session, user, NOW - timedelta(hours=2))
        await _add(session, user, NOW - timedelta(hours=1))
        with pytest.raises(pdf_report.ReportTooLargeError):
            await pdf_report.collect_report(
                session, user, None, None, subject="x", limit=1, now=NOW
            )
        data = await pdf_report.collect_report(
            session, user, None, None, subject="x", limit=2, now=NOW
        )
        assert len(data.rows) == 2

    async def test_default_limit_is_csv_limit(self, session, user, monkeypatch) -> None:
        assert pdf_report.REPORT_LIMIT == csv_export.ALL_TIME_LIMIT == 5000
        monkeypatch.setattr(pdf_report, "REPORT_LIMIT", 1)
        await _add(session, user, NOW - timedelta(hours=2))
        await _add(session, user, NOW - timedelta(hours=1))
        with pytest.raises(pdf_report.ReportTooLargeError):
            await pdf_report.collect_report(session, user, None, None, subject="x", now=NOW)

    async def test_only_own_records(self, session, user) -> None:
        other = User(telegram_id=222_222, timezone=TZ)
        session.add(other)
        await session.commit()
        await _add(session, other, NOW - timedelta(hours=1), 199, 119)
        await _add(session, user, NOW - timedelta(hours=1), 121, 79)
        data = await pdf_report.collect_report(session, user, None, None, subject="x", now=NOW)
        assert [row.systolic for row in data.rows] == ["121"]


# ─────────────────────────── Сам PDF ───────────────────────────


class TestBuildPdf:
    def test_patient_header_without_telegram_id(self) -> None:
        payload = pdf_report.build_pdf(_data())
        assert payload.startswith(b"%PDF")
        text = _pdf_text(payload)[0]
        assert "Отчёт по давлению" in text
        assert "Период: 01.10.2026 — 30.10.2026" in text
        assert "Дневник давления · сформирован 08.11.2026 14:05" in text
        assert "Пациент" not in text

    def test_doctor_header(self) -> None:
        subject = pdf_report.doctor_subject(111_111)
        assert subject == "Пациент 111111"
        text = _pdf_text(pdf_report.build_pdf(_data(subject=subject)))[0]
        assert "Пациент 111111 · сформирован 08.11.2026 14:05" in text

    def test_summary_legend_and_columns(self) -> None:
        lines = ("Записей: 2", "Систолическое: в среднем 128 (115–145)", "Тренд: растёт")
        text = _pdf_text(pdf_report.build_pdf(_data(lines=lines)))[0]
        for line in lines:
            assert line in text
        assert "САД 130–139 · ДАД 85–89" in text
        assert "САД ≥ 140 · ДАД ≥ 90" in text
        for column in pdf_report.COLUMNS:
            assert column in text

    def test_row_values(self) -> None:
        row = _row(
            systolic="154", diastolic="84", medication="Принято: Лозартан 50 мг",
            feeling="головная боль", note="после прогулки", zone=2,
        )
        text = _pdf_text(pdf_report.build_pdf(_data(rows=[row])))[0]
        for value in ("01.11.2026", "08:15", "154", "84", "Лозартан 50 мг", "головная боль", "после прогулки"):
            assert value in text

    def test_landscape_a4(self) -> None:
        page = PdfReader(io.BytesIO(pdf_report.build_pdf(_data()))).pages[0]
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        assert width > height
        assert round(width / 72 * 25.4) == 297 and round(height / 72 * 25.4) == 210

    def test_zone_fills(self) -> None:
        rows = [_row(zone=None), _row(zone=1), _row(zone=2)]
        fills = _pdf_fills(pdf_report.build_pdf(_data(rows=rows)))
        assert _has_fill(fills, pdf_report.zone_fill(1))
        assert _has_fill(fills, pdf_report.zone_fill(2))

    def test_no_zone_fill_without_highlighted_rows_except_legend(self) -> None:
        """Заливка зон в PDF без подсвеченных строк — только у плашек легенды."""
        payload = pdf_report.build_pdf(_data(rows=[_row(zone=None)]))
        stream = PdfReader(io.BytesIO(payload)).pages[0].get_contents().get_data().decode("latin-1")
        red = pdf_report.zone_fill(2)
        target = " ".join(f"{c / 255:.4f}".rstrip("0").rstrip(".") for c in red) + " rg"
        assert stream.count(target) == 1

    def test_long_note_is_wrapped_not_cut(self) -> None:
        note = " ".join(f"слово{i}" for i in range(80))
        text = _pdf_text(pdf_report.build_pdf(_data(rows=[_row(note=note)])))[0]
        assert "слово0" in text and "слово79" in text

    def test_pages_numbered_and_headings_repeated(self) -> None:
        rows = [_row(note="длинная заметка " * 20) for _ in range(40)]
        pages = _pdf_text(pdf_report.build_pdf(_data(rows=rows)))
        total = len(pages)
        assert total > 1
        for number, text in enumerate(pages, start=1):
            assert f"Стр. {number} из {total}" in text
            assert "Самочувствие" in text and "Заметка" in text  # шапка таблицы
        assert "Сводка" not in pages[1]  # шапка отчёта — только на первой

    def test_empty_rows_still_render(self) -> None:
        """Генератор не падает на пустой таблице (пустой период отсекается раньше)."""
        assert pdf_report.build_pdf(_data(rows=[])).startswith(b"%PDF")


# ─────────────── Раздел «Заключения врачей» (0.5.11, В32-Г, У2-А) ───────────────


async def _add_event(
    session: AsyncSession, user: User, title: str, starts_on: date, conclusion: str = ""
) -> Appointment:
    appt = Appointment(
        user_id=user.id, kind="doctor", title=title, starts_on=starts_on, starts_at="10:00", status="done"
    )
    session.add(appt)
    await session.commit()
    if conclusion:
        session.add(AppointmentConclusion(appointment_id=appt.id, text=conclusion))
        await session.commit()
    return appt


class TestConclusionsSection:
    async def test_collect_included_for_patient(self, session, user) -> None:
        today = NOW.astimezone(ZoneInfo(TZ)).date()
        await _add(session, user, NOW - timedelta(days=2))
        await _add_event(session, user, "Кардиолог", today - timedelta(days=1), "Продолжить приём")
        await _add_event(session, user, "ЭКГ", today - timedelta(days=3), "Ритм синусовый")
        data = await pdf_report.collect_report(
            session, user, today - timedelta(days=7), today,
            subject=pdf_report.PATIENT_SUBJECT, now=NOW, include_conclusions=True,
        )
        assert [group.header for group in data.conclusions] == [
            f"ЭКГ — {today - timedelta(days=3):%d.%m.%Y}",
            f"Кардиолог — {today - timedelta(days=1):%d.%m.%Y}",
        ]  # по дате события по возрастанию; иконки в PDF нет (DejaVu без эмодзи)
        assert data.conclusions[1].items == ("Продолжить приём",)

    async def test_collect_excludes_without_conclusions_and_outside_period(self, session, user) -> None:
        today = NOW.astimezone(ZoneInfo(TZ)).date()
        await _add(session, user, NOW - timedelta(days=2))
        await _add_event(session, user, "Без заключения", today - timedelta(days=1))
        await _add_event(session, user, "Вне периода", today - timedelta(days=40), "Старое")
        data = await pdf_report.collect_report(
            session, user, today - timedelta(days=7), today,
            subject="x", now=NOW, include_conclusions=True,
        )
        assert data.conclusions == ()  # фильтр по дате события, не по created_at

    async def test_doctor_pdf_has_no_section(self, session, user) -> None:
        """У2-А: у врача (и без флага) раздела нет."""
        today = NOW.astimezone(ZoneInfo(TZ)).date()
        await _add(session, user, NOW - timedelta(days=2))
        await _add_event(session, user, "Кардиолог", today - timedelta(days=1), "Продолжить")
        data = await pdf_report.collect_report(
            session, user, today - timedelta(days=7), today, subject="Пациент 111111", now=NOW
        )
        assert data.conclusions == ()
        assert pdf_report.CONCLUSIONS_TITLE not in "".join(_pdf_text(pdf_report.build_pdf(data)))

    def test_pdf_renders_grouping(self) -> None:
        """Р7: одно заключение — текстом, несколько — списком 1., 2. (после таблицы, Р6)."""
        data = pdf_report.ReportData(
            subject=pdf_report.PATIENT_SUBJECT,
            start_day=date(2026, 10, 1),
            end_day=date(2026, 10, 30),
            generated_at=NOW.astimezone(ZoneInfo(TZ)),
            summary_lines=("Записей: 1",),
            rows=(_row(),),
            conclusions=(
                pdf_report.ConclusionGroup(header="Кардиолог — 05.11.2026", items=("Продолжить приём",)),
                pdf_report.ConclusionGroup(
                    header="Биохимия — 06.11.2026", items=("Первый", "Второй"),
                ),
            ),
        )
        pages = _pdf_text(pdf_report.build_pdf(data))
        text = "\n".join(pages)
        assert pdf_report.CONCLUSIONS_TITLE in text
        assert "Кардиолог — 05.11.2026" in text
        assert "Продолжить приём" in text
        assert "1. Первый" in text and "2. Второй" in text
        # раздел — после таблицы: заголовок заключений встречается позже даты строки
        assert text.index(pdf_report.CONCLUSIONS_TITLE) > text.index("01.11.2026")

    def test_pdf_without_conclusions_has_no_section(self) -> None:
        pages = _pdf_text(pdf_report.build_pdf(_data()))
        assert pdf_report.CONCLUSIONS_TITLE not in "\n".join(pages)
