"""Тесты Pydantic-схем и парсеров ввода (bot/schemas.py)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from bot.schemas import (
    TIME_FORMAT_HINT,
    ArchiveQuick,
    DosageInput,
    FeelingInput,
    MeasurementInput,
    MedicationNameInput,
    NoteInput,
    TimeInput,
    TimezoneInput,
    combine_moment,
    parse_archive_date,
    parse_archive_quick,
    validate_int,
    validate_dosage,
    validate_medication_name,
    validate_note,
    validate_pair,
    validate_time,
    validate_timezone,
)
from bot import texts


class TestMeasurementInput:
    @pytest.mark.parametrize("systolic", [70, 120, 260])
    def test_systolic_boundaries_ok(self, systolic: int) -> None:
        assert MeasurementInput(systolic=systolic, diastolic=80).systolic == systolic

    @pytest.mark.parametrize("systolic", [69, 261, 0, -10])
    def test_systolic_out_of_range(self, systolic: int) -> None:
        with pytest.raises(ValidationError):
            MeasurementInput(systolic=systolic, diastolic=80)

    @pytest.mark.parametrize("diastolic", [40, 80, 160])
    def test_diastolic_boundaries_ok(self, diastolic: int) -> None:
        assert MeasurementInput(systolic=120, diastolic=diastolic).diastolic == diastolic

    @pytest.mark.parametrize("diastolic", [39, 161])
    def test_diastolic_out_of_range(self, diastolic: int) -> None:
        with pytest.raises(ValidationError):
            MeasurementInput(systolic=120, diastolic=diastolic)

    def test_pulse_default_none(self) -> None:
        assert MeasurementInput(systolic=120, diastolic=80).pulse is None

    @pytest.mark.parametrize("pulse", [30, 60, 220])
    def test_pulse_boundaries_ok(self, pulse: int) -> None:
        assert MeasurementInput(systolic=120, diastolic=80, pulse=pulse).pulse == pulse

    @pytest.mark.parametrize("pulse", [29, 221])
    def test_pulse_out_of_range(self, pulse: int) -> None:
        with pytest.raises(ValidationError):
            MeasurementInput(systolic=120, diastolic=80, pulse=pulse)

    def test_non_integer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MeasurementInput(systolic="сто двадцать", diastolic=80)  # type: ignore[arg-type]


class TestNoteInput:
    def test_valid(self) -> None:
        assert NoteInput(note="после кофе").note == "после кофе"

    def test_strip(self) -> None:
        assert NoteInput(note="  после кофе  ").note == "после кофе"

    def test_max_length_ok(self) -> None:
        assert NoteInput(note="x" * 500).note == "x" * 500

    def test_too_long(self) -> None:
        with pytest.raises(ValidationError):
            NoteInput(note="x" * 501)

    @pytest.mark.parametrize("note", ["", "   "])
    def test_empty_or_blank_rejected(self, note: str) -> None:
        with pytest.raises(ValidationError):
            NoteInput(note=note)


class TestFeelingInput:
    @pytest.mark.parametrize("feeling", ["normal", "headache", "weakness", "other"])
    def test_valid(self, feeling: str) -> None:
        assert FeelingInput(feeling=feeling).feeling == feeling

    def test_invalid_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FeelingInput(feeling="great")  # type: ignore[arg-type]

    def test_other_text_default_none(self) -> None:
        assert FeelingInput(feeling="normal").other_text is None

    def test_other_text_explicit_none(self) -> None:
        assert FeelingInput(feeling="other", other_text=None).other_text is None

    def test_other_text_stripped(self) -> None:
        assert FeelingInput(feeling="other", other_text="  тошнота  ").other_text == "тошнота"

    def test_other_text_blank_becomes_none(self) -> None:
        assert FeelingInput(feeling="other", other_text="   ").other_text is None


class TestMedicationNameInput:
    def test_valid(self) -> None:
        assert MedicationNameInput(name="Лозартан").name == "Лозартан"

    def test_strip(self) -> None:
        assert MedicationNameInput(name="  Лозартан  ").name == "Лозартан"

    def test_max_length_ok(self) -> None:
        assert MedicationNameInput(name="x" * 100).name == "x" * 100

    def test_too_long(self) -> None:
        with pytest.raises(ValidationError):
            MedicationNameInput(name="x" * 101)

    @pytest.mark.parametrize("name", ["", "   "])
    def test_empty_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError):
            MedicationNameInput(name=name)


class TestDosageInput:
    def test_valid(self) -> None:
        assert DosageInput(dosage="50 мг").dosage == "50 мг"

    def test_strip(self) -> None:
        assert DosageInput(dosage="  50 мг  ").dosage == "50 мг"

    def test_max_length_ok(self) -> None:
        assert DosageInput(dosage="x" * 50).dosage == "x" * 50

    def test_too_long(self) -> None:
        with pytest.raises(ValidationError):
            DosageInput(dosage="x" * 51)

    @pytest.mark.parametrize("dosage", ["", "   "])
    def test_empty_rejected(self, dosage: str) -> None:
        with pytest.raises(ValidationError):
            DosageInput(dosage=dosage)


class TestTimeInput:
    @pytest.mark.parametrize(
        ("raw", "hour", "minute"),
        [
            ("00:00", 0, 0),
            ("08:30", 8, 30),
            ("2:30", 2, 30),  # час без ведущего нуля = 02:30
            ("8:30", 8, 30),
            ("23:59", 23, 59),
            ("  09:05  ", 9, 5),
        ],
    )
    def test_valid(self, raw: str, hour: int, minute: int) -> None:
        value = TimeInput(value=raw).value  # type: ignore[arg-type]
        assert (value.hour, value.minute) == (hour, minute)

    @pytest.mark.parametrize(
        "raw",
        ["08:3", "0830", "24:00", "12:60", "08:30:00", "абырвалг", "", "  "],
    )
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TimeInput(value=raw)  # type: ignore[arg-type]
        assert TIME_FORMAT_HINT in str(exc_info.value)

    def test_time_object_passed_through(self) -> None:
        from datetime import time

        assert TimeInput(value=time(8, 30)).value == time(8, 30)


class TestTimezoneInput:
    @pytest.mark.parametrize("tz", ["Europe/Minsk", "UTC", "Asia/Yekaterinburg"])
    def test_valid(self, tz: str) -> None:
        assert TimezoneInput(value=tz).value == tz

    def test_strip(self) -> None:
        assert TimezoneInput(value="  Europe/Minsk  ").value == "Europe/Minsk"

    @pytest.mark.parametrize("tz", ["Minsk", "Europe/Nowhere", "GMT+3", "", "  "])
    def test_invalid(self, tz: str) -> None:
        with pytest.raises(ValidationError):
            TimezoneInput(value=tz)


class TestValidateInt:
    @pytest.mark.parametrize(("raw", "expected"), [("120", 120), (" 80 ", 80), (70, 70)])
    def test_valid(self, raw, expected: int) -> None:
        assert validate_int(raw, 70, 260) == expected

    def test_boundaries_ok(self) -> None:
        assert validate_int("70", 70, 260) == 70
        assert validate_int("260", 70, 260) == 260

    def test_not_a_number(self) -> None:
        with pytest.raises(ValueError, match="Нужно целое число от 70 до 260"):
            validate_int("abc", 70, 260)

    def test_out_of_range_message_includes_label(self) -> None:
        with pytest.raises(ValueError, match="Нужно систолическое от 70 до 260, а получилось 300"):
            validate_int("300", 70, 260, label="систолическое")

    def test_float_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_int("120.5", 70, 260)


class TestValidatePair:
    @pytest.mark.parametrize(
        ("systolic", "diastolic"),
        [(120, 80), (260, 40), (71, 70), (135, 85)],
    )
    def test_valid_pairs(self, systolic: int, diastolic: int) -> None:
        validate_pair(systolic, diastolic)  # не бросает

    @pytest.mark.parametrize(
        ("systolic", "diastolic"),
        [(120, 120), (120, 130), (70, 90)],
    )
    def test_diastolic_not_lower_raises(self, systolic: int, diastolic: int) -> None:
        """Равные или более высокие значения — ошибка (§9: строго ниже)."""
        with pytest.raises(ValueError) as exc_info:
            validate_pair(systolic, diastolic)
        assert str(exc_info.value) == texts.DIA_NOT_LOWER

    def test_error_text_is_canonical(self) -> None:
        assert texts.DIA_NOT_LOWER == (
            "Диастолическое (нижнее) должно быть ниже систолического (верхнего) — "
            "проверьте цифры."
        )


class TestValidateTime:
    def test_valid(self) -> None:
        value = validate_time("08:30")
        assert (value.hour, value.minute) == (8, 30)

    def test_single_digit_hour(self) -> None:
        """«2:30» — то же, что «02:30» (минимум трения, §2)."""
        assert validate_time("2:30") == time(2, 30)

    def test_invalid_raises_clean_value_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_time("25:00")
        assert str(exc_info.value) == TIME_FORMAT_HINT

    def test_not_pydantic_formatted(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_time("абырвалг")
        assert "validation error" not in str(exc_info.value)


class TestValidateTimezone:
    def test_valid(self) -> None:
        assert validate_timezone("Europe/Minsk") == "Europe/Minsk"

    def test_invalid_raises_clean_value_error(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_timezone("Minsk")
        assert str(exc_info.value) == (
            "Не знаю такой часовой пояс. Нужно IANA-имя, например Europe/Minsk"
        )


class TestValidateNote:
    def test_valid(self) -> None:
        assert validate_note("после кофе") == "после кофе"

    def test_strip(self) -> None:
        assert validate_note("  после кофе  ") == "после кофе"

    @pytest.mark.parametrize("note", ["", "   "])
    def test_empty_raises_clean_value_error(self, note: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_note(note)
        assert "validation error" not in str(exc_info.value)

    def test_too_long(self) -> None:
        with pytest.raises(ValueError):
            validate_note("x" * 501)


class TestValidateMedicationName:
    def test_valid(self) -> None:
        assert validate_medication_name("Лозартан") == "Лозартан"

    def test_strip(self) -> None:
        assert validate_medication_name("  Амлодипин  ") == "Амлодипин"

    @pytest.mark.parametrize("name", ["", "   "])
    def test_empty_raises_clean_value_error(self, name: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_medication_name(name)
        assert "validation error" not in str(exc_info.value)

    def test_too_long(self) -> None:
        with pytest.raises(ValueError):
            validate_medication_name("x" * 101)


class TestValidateDosage:
    def test_valid(self) -> None:
        assert validate_dosage("25 мг") == "25 мг"

    def test_strip(self) -> None:
        assert validate_dosage("  1 таб  ") == "1 таб"

    @pytest.mark.parametrize("dosage", ["", "   "])
    def test_empty_raises_clean_value_error(self, dosage: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_dosage(dosage)
        assert "validation error" not in str(exc_info.value)

    def test_too_long(self) -> None:
        with pytest.raises(ValueError):
            validate_dosage("x" * 51)


#: Фиксированное «сейчас» для архивных парсеров (§9, §6.10).
NOW = datetime(2025, 3, 20, 12, 0, tzinfo=ZoneInfo("Europe/Minsk"))
TODAY = NOW.date()


class TestParseArchiveDate:
    """Дата архива `ДД.ММ[.ГГГГ]` (§9, §6.10)."""

    def test_full_date(self) -> None:
        assert parse_archive_date("12.03.2025", TODAY) == date(2025, 3, 12)

    def test_short_date_uses_current_year(self) -> None:
        assert parse_archive_date("12.03", TODAY) == date(2025, 3, 12)

    def test_strip(self) -> None:
        assert parse_archive_date("  01.03.2024  ", TODAY) == date(2024, 3, 1)

    @pytest.mark.parametrize("raw", ["1.3.2025", "1.03.2025", "01.3.2025"])
    def test_single_digit_day_or_month(self, raw: str) -> None:
        """«1.3» — то же, что «01.03» (минимум трения, §2)."""
        assert parse_archive_date(raw, TODAY) == date(2025, 3, 1)

    def test_single_digit_short_date(self) -> None:
        assert parse_archive_date("1.3", TODAY) == date(2025, 3, 1)

    def test_today_ok(self) -> None:
        assert parse_archive_date("20.03.2025", TODAY) == date(2025, 3, 20)

    @pytest.mark.parametrize(
        "raw",
        ["2025-03-12", "12/03/2025", "абырвалг", "", "   ", "12.03.2025 08:00"],
    )
    def test_bad_format(self, raw: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_date(raw, TODAY)
        assert str(exc_info.value) == texts.ARCHIVE_DATE_FORMAT_HINT

    def test_impossible_calendar_date(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_date("31.02.2025", TODAY)
        assert str(exc_info.value) == texts.ARCHIVE_DATE_FORMAT_HINT

    def test_future_rejected(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_date("21.03.2025", TODAY)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR

    def test_future_short_date_rejected(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_date("25.12", TODAY)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR


class TestCombineMoment:
    """Момент архива: дата + время в таймзоне пользователя, ≤ текущего (§9)."""

    def test_combines_in_user_timezone(self) -> None:
        moment = combine_moment(date(2025, 3, 12), time(8, 30), "Europe/Minsk", now=NOW)
        assert (moment.year, moment.month, moment.day) == (2025, 3, 12)
        assert (moment.hour, moment.minute) == (8, 30)
        assert moment.utcoffset() == timedelta(hours=3)

    def test_past_ok(self) -> None:
        combine_moment(date(2025, 3, 12), time(23, 59), "Europe/Minsk", now=NOW)

    def test_equal_to_now_ok(self) -> None:
        combine_moment(NOW.date(), time(12, 0), "Europe/Minsk", now=NOW)

    def test_future_raises(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            combine_moment(date(2025, 3, 21), time(8, 0), "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR

    def test_future_time_same_day_raises(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            combine_moment(NOW.date(), time(13, 0), "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR

    def test_naive_now_treated_as_local(self) -> None:
        moment = combine_moment(
            date(2025, 3, 12), time(8, 0), "Europe/Minsk", now=datetime(2025, 3, 20, 12, 0)
        )
        assert moment.day == 12

    def test_naive_now_before_moment_raises(self) -> None:
        with pytest.raises(ValueError):
            combine_moment(
                date(2025, 3, 21), time(8, 0), "Europe/Minsk",
                now=datetime(2025, 3, 20, 12, 0),
            )


class TestParseArchiveQuick:
    """Быстрый ввод `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]` (§6.10)."""

    def test_full_row(self) -> None:
        parsed = parse_archive_quick("12.03.2025 08:30 135/85 72", "Europe/Minsk", now=NOW)
        assert isinstance(parsed, ArchiveQuick)
        assert (parsed.moment.day, parsed.moment.hour, parsed.moment.minute) == (12, 8, 30)
        assert (parsed.systolic, parsed.diastolic, parsed.pulse) == (135, 85, 72)

    def test_short_date_and_no_pulse(self) -> None:
        parsed = parse_archive_quick("12.03 08:30 135/85", "Europe/Minsk", now=NOW)
        assert parsed.moment.date() == date(2025, 3, 12)
        assert parsed.pulse is None

    def test_extra_spaces_between_parts(self) -> None:
        parsed = parse_archive_quick("12.03.2025  08:30  135/85  72", "Europe/Minsk", now=NOW)
        assert (parsed.systolic, parsed.diastolic, parsed.pulse) == (135, 85, 72)

    def test_single_digit_time(self) -> None:
        """«8:30» — то же, что «08:30» (минимум трения, §2)."""
        parsed = parse_archive_quick("12.03.2025 8:30 135/85", "Europe/Minsk", now=NOW)
        assert (parsed.moment.hour, parsed.moment.minute) == (8, 30)

    def test_single_digit_everywhere(self) -> None:
        """«2.3 2:30 120/80» = «02.03 02:30 120/80» — ввод как в чате."""
        parsed = parse_archive_quick("2.3 2:30 120/80", "Europe/Minsk", now=NOW)
        assert parsed.moment.date() == date(2025, 3, 2)
        assert (parsed.moment.hour, parsed.moment.minute) == (2, 30)
        assert (parsed.systolic, parsed.diastolic, parsed.pulse) == (120, 80, None)

    @pytest.mark.parametrize(
        "raw",
        [
            "12.03.2025 08:30",  # без давления
            "12.03.2025 08:30 135",  # без диастолического
            "12.03 08:30 135/85 72 5",  # лишнее поле
            "абырвалг",
            "",
        ],
    )
    def test_bad_format(self, raw: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick(raw, "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_QUICK_FORMAT_HINT

    def test_impossible_calendar_date(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("31.02.2025 08:30 135/85", "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_QUICK_FORMAT_HINT

    def test_future_moment(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("21.03.2025 08:30 135/85", "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR

    def test_moment_checked_before_values(self) -> None:
        """Будущий момент важнее ошибок диапазона (порядок проверок §6.10)."""
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("21.03.2025 08:30 300/200", "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.ARCHIVE_FUTURE_ERROR

    def test_systolic_out_of_range(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("12.03 08:30 300/85", "Europe/Minsk", now=NOW)
        assert "систолическое" in str(exc_info.value)

    def test_pair_validation(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("12.03 08:30 120/130", "Europe/Minsk", now=NOW)
        assert str(exc_info.value) == texts.DIA_NOT_LOWER

    def test_pulse_out_of_range(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            parse_archive_quick("12.03 08:30 135/85 5", "Europe/Minsk", now=NOW)
        assert "пульс" in str(exc_info.value)
