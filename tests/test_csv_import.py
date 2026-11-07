"""Тесты CSV-импорта архива (web/cabinet/csv_import.py, SPEC.md, §16.2).

Разбор (`parse`) — чистая функция без БД; вставка (`import_rows`) — одна
транзакция с дедупом по минутам (двойная отправка формы → все дубли, §7.2 п.4).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, User
from web.cabinet import csv_import
from web.cabinet.csv_import import (
    FILE_NO_HEADER,
    FILE_NOT_UTF8,
    FILE_TOO_MANY_ROWS,
    MAX_ERRORS_SHOWN,
    MAX_IMPORT_ROWS,
)

TZ = "Europe/Minsk"
#: «Сейчас» для детерминированных проверок «время в будущем».
NOW = datetime(2025, 3, 20, 12, 0, tzinfo=ZoneInfo(TZ))

HEADER = "timestamp_local;systolic;diastolic;pulse;medication_taken;feeling;note"


def csv_bytes(*rows: str, header: str = HEADER) -> bytes:
    """Собрать файл импорта: заголовок + строки через CRLF (как экспорт §10)."""
    return ("\r\n".join([header, *rows]) + "\r\n").encode("utf-8-sig")


class TestParseFile:
    """Ошибки всего файла: кодировка, заголовок, лимит строк (§16.2)."""

    def test_not_utf8(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            csv_import.parse("timestamp_local;systolic;diastolic\r\n".encode("utf-16"), TZ)
        assert str(exc_info.value) == FILE_NOT_UTF8

    def test_empty_file(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            csv_import.parse(b"", TZ)
        assert str(exc_info.value) == FILE_NO_HEADER

    @pytest.mark.parametrize("column", ["timestamp_local", "systolic", "diastolic"])
    def test_missing_required_column(self, column: str) -> None:
        header = ";".join(name for name in HEADER.split(";") if name != column)
        with pytest.raises(ValueError) as exc_info:
            csv_import.parse(csv_bytes(header=header), TZ)
        assert column in str(exc_info.value)

    def test_optional_columns_may_be_missing(self) -> None:
        parsed = csv_import.parse(
            csv_bytes("12.03.2024 08:30;135;85", header="timestamp_local;systolic;diastolic"),
            TZ, now=NOW,
        )
        assert parsed.error_count == 0
        assert (parsed.rows[0].systolic, parsed.rows[0].diastolic) == (135, 85)
        assert parsed.rows[0].pulse is None

    def test_too_many_rows(self) -> None:
        rows = ["12.03.2024 08:30;135;85"] * (MAX_IMPORT_ROWS + 1)
        with pytest.raises(ValueError) as exc_info:
            csv_import.parse(csv_bytes(*rows, header="timestamp_local;systolic;diastolic"), TZ)
        assert str(exc_info.value) == FILE_TOO_MANY_ROWS

    def test_exactly_limit_rows_ok(self) -> None:
        minutes = [
            f"12.03.2024 {hour:02d}:{minute:02d};135;85"
            for hour in range(24)
            for minute in range(60)
        ]  # 1440 уникальных минут
        rows = (minutes * 4)[:MAX_IMPORT_ROWS]
        parsed = csv_import.parse(
            csv_bytes(*rows, header="timestamp_local;systolic;diastolic"), TZ, now=NOW
        )
        assert len(parsed.rows) == 24 * 60  # уникальные минуты
        assert parsed.duplicates == MAX_IMPORT_ROWS - 24 * 60

    def test_blank_lines_skipped(self) -> None:
        parsed = csv_import.parse(
            csv_bytes("", "12.03.2024 08:30;135;85", ";;;"), TZ, now=NOW
        )
        assert len(parsed.rows) == 1
        assert parsed.error_count == 0

    def test_header_case_and_spaces_tolerated(self) -> None:
        parsed = csv_import.parse(
            csv_bytes(
                "12.03.2024 08:30;135;85",
                header=" Timestamp_local ; Systolic ; Diastolic ",
            ),
            TZ, now=NOW,
        )
        assert len(parsed.rows) == 1


class TestParseRows:
    """Ошибки строк: короткая причина, перечисление до 10 (§16.2)."""

    def test_full_row(self) -> None:
        parsed = csv_import.parse(
            csv_bytes("12.03.2024 08:30;135;85;72;да;головная боль;после кофе"),
            TZ, now=NOW,
        )
        row = parsed.rows[0]
        assert row.moment == datetime(2024, 3, 12, 8, 30, tzinfo=ZoneInfo(TZ))
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)
        assert row.medication_taken is True
        assert row.feeling == "headache"
        assert row.note == "после кофе"

    def test_single_digit_day_month_hour(self) -> None:
        """«1.3.2024 8:30» — то же, что «01.03.2024 08:30» (минимум трения, §2)."""
        parsed = csv_import.parse(csv_bytes("1.3.2024 8:30;135;85"), TZ, now=NOW)
        row = parsed.rows[0]
        assert row.moment == datetime(2024, 3, 1, 8, 30, tzinfo=ZoneInfo(TZ))
        assert (row.systolic, row.diastolic) == (135, 85)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12.03.2024 08:30;135;85;;нет;;", False),
            ("12.03.2024 08:30;135;85;;;;", None),
        ],
    )
    def test_medication_flag(self, raw: str, expected) -> None:
        parsed = csv_import.parse(csv_bytes(raw), TZ, now=NOW)
        assert parsed.rows[0].medication_taken is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12.03.2024 08:30;135;85;;;норма;", "normal"),
            ("12.03.2024 08:30;135;85;;;Слабость;", "weakness"),
            ("12.03.2024 08:30;135;85;;;другое;", "other"),
        ],
    )
    def test_feeling_values(self, raw: str, expected: str) -> None:
        parsed = csv_import.parse(csv_bytes(raw), TZ, now=NOW)
        assert parsed.rows[0].feeling == expected

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ("вчера утром;135;85", "дата и время не распознаны"),
            ("12.03.2024 08:30:30;110;70", "дата и время не распознаны"),  # формат без секунд
            ("12.03.24 08:30;135;85", "дата и время не распознаны"),  # год — 4 цифры
            ("12.03.2024 8:3;135;85", "дата и время не распознаны"),  # минуты — 2 цифры
            ("25.03.2025 08:30;135;85", "время в будущем"),
            ("31.02.2024 08:30;135;85", "дата и время не распознаны"),
            ("12.03.2024 08:30;300;85", "Нужно систолическое от 70 до 260"),
            ("12.03.2024 08:30;abc;85", "Нужно целое число от 70 до 260"),
            ("12.03.2024 08:30;120;130", texts.DIA_NOT_LOWER),
            ("12.03.2024 08:30;135;85;;может быть;", "флаг лекарства"),
            ("12.03.2024 08:30;135;85;;;отлично;", "самочувствие не распознано"),
            ("12.03.2024 08:30;135;85;;;;" + "x" * 501, "заметка длиннее 500"),
        ],
    )
    def test_row_errors(self, raw: str, fragment: str) -> None:
        parsed = csv_import.parse(csv_bytes(raw), TZ, now=NOW)
        assert parsed.rows == []
        assert parsed.error_count == 1
        assert fragment in parsed.errors[0]
        assert parsed.errors[0].startswith("Строка 2:")

    def test_errors_capped_but_counted(self) -> None:
        rows = ["абырвалг;135;85"] * (MAX_ERRORS_SHOWN + 5)
        parsed = csv_import.parse(
            csv_bytes(*rows, header="timestamp_local;systolic;diastolic"), TZ, now=NOW
        )
        assert len(parsed.errors) == MAX_ERRORS_SHOWN
        assert parsed.error_count == MAX_ERRORS_SHOWN + 5

    def test_line_numbers_follow_header(self) -> None:
        parsed = csv_import.parse(
            csv_bytes("12.03.2024 08:30;135;85", "абырвалг;135;85"), TZ, now=NOW
        )
        assert parsed.errors == ["Строка 3: дата и время не распознаны"]

    def test_in_file_duplicates_counted(self) -> None:
        parsed = csv_import.parse(
            csv_bytes(
                "12.03.2024 08:30;135;85",
                "12.03.2024 08:30;120;80",  # та же минута
                "12.03.2024 08:31;135;85",
            ),
            TZ, now=NOW,
        )
        assert len(parsed.rows) == 2
        assert parsed.duplicates == 1


class TestImportRows:
    """Вставка одной транзакцией с дедупом против БД (§16.2, §7.2 п.4)."""

    async def _parsed(self, *rows: str):
        return csv_import.parse(csv_bytes(*rows), TZ, now=NOW)

    async def test_imports_valid_rows(self, session: AsyncSession, user: User) -> None:
        parsed = await self._parsed("12.03.2024 08:30;135;85;72;да;норма;после кофе")
        report = await csv_import.import_rows(session, user, parsed)
        assert report.text() == "Импортировано: 1 · Пропущено дублей: 0 · Ошибок: 0"
        await session.commit()
        rows = (
            await session.execute(select(Measurement).execution_options(populate_existing=True))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].timestamp == datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc)
        assert rows[0].medication_taken is True

    async def test_skips_existing_db_duplicates(
        self, session: AsyncSession, user: User
    ) -> None:
        session.add(
            Measurement(
                user_id=user.id,
                timestamp=datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc),
                systolic=111, diastolic=70,
            )
        )
        await session.commit()
        parsed = await self._parsed("12.03.2024 08:30;135;85")
        report = await csv_import.import_rows(session, user, parsed)
        assert report.text() == "Импортировано: 0 · Пропущено дублей: 1 · Ошибок: 0"
        await session.commit()
        rows = (
            await session.execute(select(Measurement).execution_options(populate_existing=True))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].systolic == 111  # существующая запись не тронута

    async def test_double_submit_all_duplicates(
        self, session: AsyncSession, user: User
    ) -> None:
        """Двойная отправка формы: второй прогон — все дубли (§7.2 п.4)."""
        parsed = await self._parsed("12.03.2024 08:30;135;85", "13.03.2024 09:00;120;80")
        first = await csv_import.import_rows(session, user, parsed)
        await session.commit()
        assert first.imported == 2
        second = await csv_import.import_rows(session, user, parsed)
        await session.commit()
        assert second.text() == "Импортировано: 0 · Пропущено дублей: 2 · Ошибок: 0"
        rows = (
            await session.execute(select(Measurement).execution_options(populate_existing=True))
        ).scalars().all()
        assert len(rows) == 2

    async def test_other_user_rows_do_not_block(
        self, session: AsyncSession, user: User
    ) -> None:
        """Изоляция данных: чужая запись в ту же минуту не считается дублем (§7.1)."""
        other = User(telegram_id=222_222, timezone=TZ)
        session.add(other)
        await session.flush()
        session.add(
            Measurement(
                user_id=other.id,
                timestamp=datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc),
                systolic=111, diastolic=70,
            )
        )
        await session.commit()
        parsed = await self._parsed("12.03.2024 08:30;135;85")
        report = await csv_import.import_rows(session, user, parsed)
        assert report.imported == 1

    async def test_report_carries_errors(self, session: AsyncSession, user: User) -> None:
        parsed = await self._parsed("12.03.2024 08:30;135;85", "абырвалг;135;85")
        report = await csv_import.import_rows(session, user, parsed)
        assert report.text() == "Импортировано: 1 · Пропущено дублей: 0 · Ошибок: 1"
        assert report.error_lines == ["Строка 3: дата и время не распознаны"]

    async def test_empty_parse_imports_nothing(
        self, session: AsyncSession, user: User
    ) -> None:
        parsed = await self._parsed()
        report = await csv_import.import_rows(session, user, parsed)
        assert report.text() == "Импортировано: 0 · Пропущено дублей: 0 · Ошибок: 0"
