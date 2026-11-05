"""Тесты Pydantic-схем и парсеров ввода (bot/schemas.py)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.schemas import (
    TIME_FORMAT_HINT,
    DosageInput,
    FeelingInput,
    MeasurementInput,
    MedicationNameInput,
    NoteInput,
    TimeInput,
    TimezoneInput,
    validate_int,
    validate_dosage,
    validate_medication_name,
    validate_note,
    validate_time,
    validate_timezone,
)


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
            ("23:59", 23, 59),
            ("  09:05  ", 9, 5),
        ],
    )
    def test_valid(self, raw: str, hour: int, minute: int) -> None:
        value = TimeInput(value=raw).value  # type: ignore[arg-type]
        assert (value.hour, value.minute) == (hour, minute)

    @pytest.mark.parametrize(
        "raw",
        ["8:30", "08:3", "0830", "24:00", "12:60", "08:30:00", "абырвалг", "", "  "],
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


class TestValidateTime:
    def test_valid(self) -> None:
        value = validate_time("08:30")
        assert (value.hour, value.minute) == (8, 30)

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
