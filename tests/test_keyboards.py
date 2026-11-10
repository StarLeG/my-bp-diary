"""Тесты клавиатур (bot/keyboards.py): callback_data, состав кнопок, ряды."""
from __future__ import annotations

import re
from datetime import date

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import keyboards, texts
from bot.models import Medication

#: Области callback_data, зафиксированные в SPEC.md, раздел 8 (adm — §18).
KNOWN_AREAS = {"menu", "onb", "m", "d", "stats", "csv", "pdf", "doc", "set", "med", "rem", "about", "adm", "ap"}

CALLBACK_RE = re.compile(r"^(?P<area>[a-z]+):(?P<action>[a-z0-9_-]+)(?::(?P<value>.*))?$")


def buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    """Все кнопки клавиатуры плоским списком."""
    return [button for row in markup.inline_keyboard for button in row]


def value_part(data: str) -> str:
    """Третья часть callback_data (значение) или пустая строка."""
    parts = data.split(":", 2)
    return parts[2] if len(parts) == 3 else ""


def callback_buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    """Только callback-кнопки (без URL)."""
    return [button for button in buttons(markup) if button.callback_data]


def all_keyboards() -> list[tuple[str, InlineKeyboardMarkup]]:
    """Все билдеры клавиатур с именами — для общих проверок."""
    med = (1, "Лозартан", "50 мг")
    med_row = Medication(id=1, user_id=1, name="Лозартан", dosage="50 мг", slot="morning")
    return [
        ("main_menu", keyboards.main_menu()),
        ("back_to_menu", keyboards.back_to_menu()),
        ("keypad_kb", keyboards.keypad_kb("", allow_skip=False)),
        ("keypad_kb_skip", keyboards.keypad_kb("12", allow_skip=True)),
        ("intake_meds_kb", keyboards.intake_meds_kb({1}, [med_row])),
        ("saved_kb", keyboards.saved_kb()),
        ("medication_taken_kb", keyboards.medication_taken_kb()),
        ("feeling_kb", keyboards.feeling_kb()),
        ("note_cancel_kb", keyboards.note_cancel_kb()),
        ("input_cancel_kb", keyboards.input_cancel_kb()),
        ("archive_menu_kb", keyboards.archive_menu_kb()),
        ("archive_date_kb", keyboards.archive_date_kb()),
        ("archive_time_kb", keyboards.archive_time_kb()),
        ("archive_saved_kb", keyboards.archive_saved_kb()),
        ("archive_medication_taken_kb", keyboards.archive_medication_taken_kb()),
        ("archive_feeling_kb", keyboards.archive_feeling_kb()),
        ("no_remeasure_kb", keyboards.no_remeasure_kb(5)),
        ("meds_ask_kb", keyboards.meds_ask_kb()),
        ("diary_kb", keyboards.diary_kb()),
        ("diary_kb_empty", keyboards.diary_kb(is_empty=True)),
        ("diary_kb_mark", keyboards.diary_kb(mark_intake=True)),
        ("mark_intake_slots_kb", keyboards.mark_intake_slots_kb(["morning", "evening"])),
        ("intake_done_kb", keyboards.intake_done_kb()),
        ("slot_mode_kb", keyboards.slot_mode_kb("morning", "both", True)),
        ("pause_kb", keyboards.pause_kb(True)),
        ("reminders_settings_kb_056", keyboards.reminders_settings_kb({"morning": True}, {"morning": "08:00"}, 60, {"morning": "meds"}, paused_until=date(2026, 11, 13))),
        ("partial_meds_kb_diary", keyboards.partial_meds_kb("noon", date(2026, 11, 10), {1}, [med], area="d")),
        ("stats_period_kb", keyboards.stats_period_kb()),
        ("stats_result_kb", keyboards.stats_result_kb("7")),
        ("stats_result_kb_custom", keyboards.stats_result_kb("20261101:20261115")),
        ("stats_days_kb", keyboards.stats_days_kb("30")),
        ("stats_days_kb_custom", keyboards.stats_days_kb("20261101:20261115")),
        ("stats_calendar_kb", keyboards.stats_calendar_kb(2026, 11, date(2026, 11, 10))),
        (
            "stats_calendar_kb_step2",
            keyboards.stats_calendar_kb(2026, 10, date(2026, 11, 10), date(2026, 10, 3)),
        ),
        ("csv_period_kb", keyboards.csv_period_kb()),
        ("pdf_period_kb", keyboards.pdf_period_kb()),
        ("main_menu_doctor", keyboards.main_menu(is_doctor=True)),
        ("doctor_choice_kb", keyboards.doctor_choice_kb()),
        ("doctor_tg_invite_kb", keyboards.doctor_tg_invite_kb()),
        ("doctor_invite_kb", keyboards.doctor_invite_kb("A" * 22)),
        ("doctor_panel_kb", keyboards.doctor_panel_kb(show_diary=True)),
        ("doctor_patients_kb", keyboards.doctor_patients_kb([(1, "Пациент 111111"), (2, "Иванов И. И., 67 лет")])),
        ("doctor_registered_kb", keyboards.doctor_registered_kb()),
        ("doctor_back_kb", keyboards.doctor_back_kb()),
        ("doctor_open_kb", keyboards.doctor_open_kb(42)),
        ("settings_kb", keyboards.settings_kb()),
        ("targets_kb", keyboards.targets_kb()),
        (
            "reminders_settings_kb",
            keyboards.reminders_settings_kb(
                enabled_slots={"morning": True, "noon": False, "evening": True},
                slot_times={"morning": "08:00", "evening": "21:00"},
                med_remeasure_delay=60,
            ),
        ),
        ("slot_time_kb", keyboards.slot_time_kb("morning")),
        ("remeasure_delay_kb", keyboards.remeasure_delay_kb()),
        ("timezone_kb", keyboards.timezone_kb()),
        ("medications_list_kb", keyboards.medications_list_kb(True)),
        ("medications_list_kb_empty", keyboards.medications_list_kb()),
        ("medications_pick_kb_edit", keyboards.medications_pick_kb("med:edit", [med])),
        ("medications_pick_kb_del", keyboards.medications_pick_kb("med:del", [med])),
        ("med_name_input_kb", keyboards.med_name_input_kb()),
        ("dosage_kb", keyboards.dosage_kb()),
        ("dosage_kb_keep", keyboards.dosage_kb(keep=True)),
        ("slots_multiselect_kb", keyboards.slots_multiselect_kb({"morning"})),
        ("slot_message_kb", keyboards.slot_message_kb("morning", date(2026, 11, 10), True)),
        ("slot_message_kb_no_meds", keyboards.slot_message_kb("morning", date(2026, 11, 10), False)),
        ("partial_meds_kb", keyboards.partial_meds_kb("morning", date(2026, 11, 10), {1}, [med])),
        ("remeasure_kb", keyboards.remeasure_kb(5)),
        ("onboarding_start_kb", keyboards.onboarding_start_kb()),
        ("onboarding_skip_kb", keyboards.onboarding_skip_kb("tz")),
        ("onboarding_timezone_kb", keyboards.onboarding_timezone_kb()),
        ("onboarding_targets_kb", keyboards.onboarding_targets_kb()),
        ("onboarding_slots_kb", keyboards.onboarding_slots_kb({"noon"})),
        ("onboarding_time_kb", keyboards.onboarding_time_kb("morning")),
        ("about_kb", keyboards.about_kb(support_visible=True, author_visible=True)),
        ("about_kb_empty", keyboards.about_kb()),
        ("support_kb", keyboards.support_kb("https://example.org/donate")),
        ("delete_account_kb", keyboards.delete_account_kb()),
        ("admin_menu_kb", keyboards.admin_menu_kb()),
        ("admin_stats_kb", keyboards.admin_stats_kb()),
        ("admin_health_kb", keyboards.admin_health_kb()),
        ("admin_backups_kb", keyboards.admin_backups_kb()),
        ("admin_broadcast_preview_kb", keyboards.admin_broadcast_preview_kb()),
        ("admin_search_card_kb", keyboards.admin_search_card_kb(111111)),
        ("doctor_kb", keyboards.doctor_kb([(1, "doc@example.org", "active")])),
        ("doctor_kb_pending", keyboards.doctor_kb([(1, "doc@example.org", "pending")])),
        ("doctor_kb_revoked", keyboards.doctor_kb([(1, "doc@example.org", "revoked")])),
        ("doctor_kb_empty", keyboards.doctor_kb([])),
    ]


class TestCallbackData:
    @pytest.mark.parametrize(
        ("name", "markup"), all_keyboards(), ids=lambda value: value if isinstance(value, str) else ""
    )
    def test_callback_data_format(self, name: str, markup: InlineKeyboardMarkup) -> None:
        for button in callback_buttons(markup):
            assert button.callback_data is not None
            assert (
                len(button.callback_data.encode("utf-8")) <= keyboards.CALLBACK_DATA_LIMIT
            ), f"{name}: слишком длинная callback_data {button.callback_data!r}"
            match = CALLBACK_RE.match(button.callback_data)
            assert match, f"{name}: неверный формат {button.callback_data!r}"
            assert match.group("area") in KNOWN_AREAS, f"{name}: неизвестная область {button.callback_data!r}"

    @pytest.mark.parametrize(
        ("name", "markup"), all_keyboards(), ids=lambda value: value if isinstance(value, str) else ""
    )
    def test_no_duplicate_buttons(self, name: str, markup: InlineKeyboardMarkup) -> None:
        # Уникальность пар (текст, callback_data); одинаковый callback у разных
        # городов с одним IANA допустим (Москва/СПб/Казань — Europe/Moscow).
        # Пустые клетки календаря (`stats:noop`, §6.5) — заглушки, не кнопки выбора.
        pairs = [
            (button.text, button.callback_data)
            for button in buttons(markup)
            if button.callback_data != "stats:noop"
        ]
        assert len(pairs) == len(set(pairs)), f"{name}: дубли кнопок в клавиатуре"

    @pytest.mark.parametrize(
        ("name", "markup"), all_keyboards(), ids=lambda value: value if isinstance(value, str) else ""
    )
    def test_buttons_have_text(self, name: str, markup: InlineKeyboardMarkup) -> None:
        for button in buttons(markup):
            assert button.text, f"{name}: кнопка без текста"

    def test_value_with_colon_allowed(self) -> None:
        # HH:MM в значении callback_data — формат <область>:<действие>[:<значение>].
        markup = keyboards.slot_time_kb("morning")
        times = [
            button.callback_data
            for button in callback_buttons(markup)
            if button.callback_data and button.callback_data.endswith(":08:00")
        ]
        assert times == ["set:time:morning:08:00"]

    def test_timezone_callback_length(self) -> None:
        # Самое длинное значение — IANA-имя.
        markup = keyboards.timezone_kb()
        longest = max(
            button.callback_data.encode("utf-8")
            for button in callback_buttons(markup)
            if button.callback_data
        )
        assert len(longest) <= keyboards.CALLBACK_DATA_LIMIT


class TestKeypadKeyboard:
    def test_calculator_layout(self) -> None:
        """Раскладка как у калькулятора: 7-8-9 / 4-5-6 / 1-2-3 / «C 0 ⌫» (§6.3)."""
        markup = keyboards.keypad_kb("", allow_skip=False)
        digit_rows = [[button.callback_data for button in row] for row in markup.inline_keyboard[:4]]
        assert digit_rows == [
            ["m:key:7", "m:key:8", "m:key:9"],
            ["m:key:4", "m:key:5", "m:key:6"],
            ["m:key:1", "m:key:2", "m:key:3"],
            ["m:key:clear", "m:key:0", "m:key:bksp"],
        ]

    def test_done_cancel_rows(self) -> None:
        markup = keyboards.keypad_kb("", allow_skip=False)
        rows = [[button.callback_data for button in row] for row in markup.inline_keyboard[4:]]
        assert rows == [["m:key:ok"], ["m:cancel"]]

    def test_skip_only_with_allow_skip(self) -> None:
        """«Пропустить» — только у пульса (`allow_skip=True`), §6.3."""
        without = {
            button.callback_data for button in callback_buttons(keyboards.keypad_kb("", allow_skip=False))
        }
        with_skip = {
            button.callback_data for button in callback_buttons(keyboards.keypad_kb("", allow_skip=True))
        }
        assert "m:key:skip" not in without
        assert "m:key:skip" in with_skip
        assert without < with_skip

    def test_all_digit_keys_present(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.keypad_kb("12", allow_skip=True))
        }
        assert {f"m:key:{digit}" for digit in range(10)} <= data
        assert {"m:key:clear", "m:key:bksp", "m:key:ok", "m:cancel"} <= data

    def test_buffer_does_not_change_layout(self) -> None:
        """`buf` на разметку не влияет — её рисует `texts.fmt_keypad_prompt`."""
        empty = keyboards.keypad_kb("", allow_skip=True)
        filled = keyboards.keypad_kb("42", allow_skip=True)
        assert [[b.callback_data for b in row] for row in empty.inline_keyboard] == [
            [b.callback_data for b in row] for row in filled.inline_keyboard
        ]


class TestIntakeMedsKeyboard:
    def test_checkbox_rows_with_intake_labels(self) -> None:
        """Чекбокс на приём: «✓ Лозартан 50 мг · утро» (§6.3, fmt_intake_item)."""
        meds = [
            Medication(id=1, user_id=1, name="Лозартан", dosage="50 мг", slot="morning"),
            Medication(id=2, user_id=1, name="Амлодипин", dosage="", slot="evening"),
        ]
        markup = keyboards.intake_meds_kb({2}, meds)
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data["m:med:pick:1"] == texts.fmt_intake_item("Лозартан", "50 мг", "morning")
        assert texts_by_data["m:med:pick:2"] == f"✓ {texts.fmt_intake_item('Амлодипин', '', 'evening')}"
        assert texts_by_data["m:med:done"] == texts.BTN_DONE
        assert texts_by_data["m:med:cancel"] == texts.BTN_CANCEL

    def test_checked_ids_only_marked(self) -> None:
        meds = [
            Medication(id=1, user_id=1, name="Лозартан", dosage="50 мг", slot="morning"),
            Medication(id=2, user_id=1, name="Амлодипин", dosage="5 мг", slot="evening"),
        ]
        markup = keyboards.intake_meds_kb({1}, meds)
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data["m:med:pick:1"].startswith("✓ ")
        assert not texts_by_data["m:med:pick:2"].startswith("✓ ")

    def test_taken_today_marker_without_checkmark(self) -> None:
        """Принятое сегодня — «· сегодня ✓» в конце подписи, галочка — только у отмеченных (§6.3)."""
        meds = [
            Medication(id=1, user_id=1, name="Лозартан", dosage="50 мг", slot="morning"),
            Medication(id=2, user_id=1, name="Амлодипин", dosage="5 мг", slot="evening"),
        ]
        markup = keyboards.intake_meds_kb({2}, meds, {1, 2})
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data["m:med:pick:1"] == "Лозартан 50 мг · утро · сегодня ✓"
        assert texts_by_data["m:med:pick:2"] == "✓ Амлодипин 5 мг · вечер · сегодня ✓"

    def test_empty_medications_still_has_actions(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.intake_meds_kb(set(), []))
        }
        assert data == {"m:med:done", "m:med:cancel"}


class TestMeasurementKeyboards:
    def test_saved_kb_actions(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.saved_kb())}
        assert data == {"m:edit", "m:note", "m:med", "m:feeling", "menu:back"}

    def test_saved_kb_edit_is_first_row(self) -> None:
        first_row = keyboards.saved_kb().inline_keyboard[0]
        assert [button.callback_data for button in first_row] == ["m:edit"]
        assert first_row[0].text == texts.BTN_EDIT

    def test_diary_kb_edit_button(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.diary_kb())}
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(keyboards.diary_kb())
        }
        assert "m:edit" in data
        assert texts_by_data["m:edit"] == texts.BTN_EDIT_LAST
        empty = {
            button.callback_data
            for button in callback_buttons(keyboards.diary_kb(is_empty=True))
        }
        assert "m:edit" not in empty

    def test_medication_taken_kb_semantics(self) -> None:
        """«Приняли лекарство?» — только Да / Нет (без «Пропустить», §6.3)."""
        data = {button.callback_data for button in callback_buttons(keyboards.medication_taken_kb())}
        assert data == {"m:med:yes", "m:med:no", "menu:back"}

    def test_feeling_kb_options(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.feeling_kb())}
        assert data == {
            "m:feeling:normal",
            "m:feeling:headache",
            "m:feeling:weakness",
            "m:feeling:other",
            "menu:back",
        }

    def test_no_remeasure_kb_carries_measurement_id(self) -> None:
        markup = keyboards.no_remeasure_kb(42)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"m:noremeasure:42"}

    def test_meds_ask_kb_actions(self) -> None:
        markup = keyboards.meds_ask_kb()
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(markup)
            if button.callback_data
        }
        assert texts_by_data == {
            "med:add": texts.BTN_ADD,
            "m:medask:dismiss": texts.BTN_NOT_NOW,
        }


class TestSlotMessageKeyboard:
    """Кнопки напоминания без FSM: слот и дата в callback'ах (§6.7, §8, 0.5.4)."""

    DAY = date(2026, 11, 10)

    def test_with_medications_shows_all_five(self) -> None:
        markup = keyboards.slot_message_kb("morning", self.DAY, True)
        data = [button.callback_data for button in callback_buttons(markup)]
        assert data == [
            "rem:write:morning:20261110",
            "rem:all:morning:20261110",
            "rem:partial:morning:20261110",
            "rem:snooze:morning:20261110",
            "rem:skip:morning:20261110",
        ]

    def test_without_medications_hides_med_buttons(self) -> None:
        markup = keyboards.slot_message_kb("evening", self.DAY, False)
        data = [button.callback_data for button in callback_buttons(markup)]
        assert data == [
            "rem:write:evening:20261110", "rem:snooze:evening:20261110", "rem:skip:evening:20261110",
        ]

    @pytest.mark.parametrize(
        ("mode", "meds", "expected"),
        [
            ("meds", True, ["rem:all", "rem:partial", "rem:snooze", "rem:skip"]),
            ("meds", False, ["rem:write", "rem:snooze", "rem:skip"]),  # без лекарств — как both
            ("pressure", True, ["rem:write", "rem:snooze", "rem:skip"]),
            ("both", True, ["rem:write", "rem:all", "rem:partial", "rem:snooze", "rem:skip"]),
        ],
    )
    def test_modes(self, mode, meds, expected) -> None:
        """Кнопки по режиму слота (§6.7, 0.5.6)."""
        markup = keyboards.slot_message_kb("noon", self.DAY, meds, mode=mode)
        data = [b.callback_data.rsplit(":", 2)[0] for b in callback_buttons(markup)]
        assert data == expected

    def test_partial_checkboxes(self) -> None:
        meds = [(1, "Лозартан", "50 мг"), (2, "Амлодипин", "5 мг"), (3, "Аспирин", "")]
        markup = keyboards.partial_meds_kb("noon", self.DAY, checked={2}, medications=meds)
        texts_by_data = {b.callback_data: b.text for b in callback_buttons(markup)}
        assert texts_by_data["rem:pt:noon:20261110:1"] == "Лозартан 50 мг"
        assert texts_by_data["rem:pt:noon:20261110:2"] == "✓ Амлодипин 5 мг"
        assert texts_by_data["rem:pt:noon:20261110:3"] == "Аспирин"
        assert texts_by_data["rem:pd:noon:20261110"] == texts.BTN_DONE
        assert texts_by_data["rem:pc:noon:20261110"] == texts.BTN_CANCEL

    def test_checked_ids_read_from_markup(self) -> None:
        meds = [(1, "Лозартан", "50 мг"), (2, "Амлодипин", "5 мг")]
        markup = keyboards.partial_meds_kb("noon", self.DAY, checked={1, 2}, medications=meds)
        assert keyboards.partial_checked_ids(markup) == {1, 2}
        markup = keyboards.partial_meds_kb("noon", self.DAY, checked=set(), medications=meds)
        assert keyboards.partial_checked_ids(markup) == set()
        assert keyboards.partial_checked_ids(None) == set()

    def test_checked_ids_ignore_foreign_buttons(self) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✓ чужая", callback_data="rem:all:morning:20261110")],
            [InlineKeyboardButton(text="✓ мусор", callback_data="rem:pt:noon:20261110:x")],
            [InlineKeyboardButton(text="✓ ok", callback_data="rem:pt:noon:20261110:7")],
        ])
        assert keyboards.partial_checked_ids(markup) == {7}

    def test_longest_callback_fits(self) -> None:
        meds = [(123456, "Лозартан", "50 мг")]
        markup = keyboards.partial_meds_kb("morning", self.DAY, checked=set(), medications=meds)
        assert max(len(b.callback_data.encode()) for b in callback_buttons(markup)) <= 30

    def test_remeasure_kb_actions(self) -> None:
        markup = keyboards.remeasure_kb(42)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"rem:now", "rem:snooze:42", "rem:off:42"}


class TestMainMenuKeyboards:
    def test_main_menu_items(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.main_menu())}
        assert data == {
            "menu:measure",
            "menu:diary",
            "ap:list",
            "menu:stats",
            "menu:settings",
            "menu:cabinet",
            "menu:report",
            "menu:about",
        }

    def test_cabinet_button_text(self) -> None:
        buttons = {button.callback_data: button.text for button in callback_buttons(keyboards.main_menu())}
        assert buttons["menu:cabinet"] == texts.BTN_CABINET


class TestPdfKeyboards:
    """Экспорт PDF (§10.1): кнопка под сводкой и экран периода."""

    def test_stats_result_has_pdf(self) -> None:
        buttons = {b.callback_data: b.text for b in callback_buttons(keyboards.stats_result_kb("7"))}
        assert buttons["stats:pdf"] == texts.BTN_EXPORT_PDF == "Экспорт PDF"
        assert "stats:csv" in buttons

    def test_settings_pdf_button_text(self) -> None:
        buttons = {b.callback_data: b.text for b in callback_buttons(keyboards.settings_kb())}
        assert buttons["set:pdf"] == texts.BTN_EXPORT_PDF

    def test_pdf_period_buttons(self) -> None:
        data = [b.callback_data for b in callback_buttons(keyboards.pdf_period_kb())]
        assert data == ["pdf:7", "pdf:30", "pdf:all", "menu:back"]


class TestSettingsKeyboards:
    def test_settings_menu_items(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.settings_kb())}
        assert data == {
            "set:targets",
            "set:reminders",
            "set:meds",
            "set:archive",
            "set:doctor",
            "set:profile",
            "set:tz",
            "set:csv",
            "set:pdf",
            "set:consent",
            "set:delete",
            "menu:back",
        }

    def test_targets_presets(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.targets_kb())}
        assert "set:target:120-80" in data
        assert "set:target:130-80" in data
        assert "set:target:140-90" in data
        assert "set:target:other" in data

    def test_reminders_settings_marks_enabled(self) -> None:
        markup = keyboards.reminders_settings_kb(
            enabled_slots={"morning": True, "noon": False, "evening": False},
            slot_times={"morning": "08:00"},
            med_remeasure_delay=60,
        )
        by_data = {button.callback_data: button.text for button in callback_buttons(markup)}
        assert by_data["set:slot:morning"] == "✅ Утро"
        assert by_data["set:slot:noon"] == "☐ Обед"
        # Кнопка времени — только для включённых слотов.
        assert "set:time:morning:show" in by_data
        assert not any(key.startswith("set:time:noon") for key in by_data)
        assert "60 минут" in by_data["set:remeasure"]

    def test_reminders_settings_remeasure_off(self) -> None:
        markup = keyboards.reminders_settings_kb(
            enabled_slots={}, slot_times={}, med_remeasure_delay=None
        )
        by_data = {button.callback_data: button.text for button in callback_buttons(markup)}
        assert texts.REMEASURE_OFF in by_data["set:remeasure"]

    def test_slot_time_presets(self) -> None:
        markup = keyboards.slot_time_kb("evening")
        data = {button.callback_data for button in callback_buttons(markup)}
        assert {"set:time:evening:20:00", "set:time:evening:21:00", "set:time:evening:22:00"} <= data
        assert "set:time:evening:other" in data

    def test_remeasure_delay_variants(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.remeasure_delay_kb())}
        assert data == {
            "set:remeasure:off",
            "set:remeasure:30",
            "set:remeasure:60",
            "set:remeasure:120",
            "set:reminders",
        }

    def test_timezone_cities(self) -> None:
        markup = keyboards.timezone_kb()
        pairs = {
            (button.text, button.callback_data)
            for button in callback_buttons(markup)
            if button.callback_data
        }
        for city, iana in keyboards.TIMEZONE_CITIES.items():
            assert (city, f"set:tz:{iana}") in pairs
        assert "set:tz:other" in {data for _, data in pairs}


class TestMedicationKeyboards:
    def test_list_actions(self) -> None:
        """При заполненном списке — три действия и «Назад» (§6.6)."""
        markup = keyboards.medications_list_kb(True)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"med:add", "med:edit", "med:del", "set:back"}

    def test_list_empty_actions(self) -> None:
        """Пустой список — только «Добавить» и «Назад» (§6.6)."""
        markup = keyboards.medications_list_kb(False)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"med:add", "set:back"}

    def test_list_action_button_texts(self) -> None:
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(keyboards.medications_list_kb(True))
        }
        assert texts_by_data["med:edit"] == texts.BTN_EDIT
        assert texts_by_data["med:del"] == texts.BTN_DELETE

    def test_pick_edit_actions(self) -> None:
        meds = [(1, "Лозартан", "50 мг"), (2, "Амлодипин", "")]
        markup = keyboards.medications_pick_kb("med:edit", meds)
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data == {
            "med:edit:1": "Лозартан 50 мг",
            "med:edit:2": "Амлодипин",  # без дозировки — без лишнего пробела
            "set:meds": texts.BTN_BACK,
        }

    def test_pick_del_actions(self) -> None:
        markup = keyboards.medications_pick_kb("med:del", [(1, "Лозартан", "50 мг")])
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data == {
            "med:del:1": "Лозартан 50 мг",
            "set:meds": texts.BTN_BACK,
        }

    def test_name_keep_actions(self) -> None:
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(keyboards.med_name_input_kb())
        }
        assert texts_by_data == {
            "med:name:same": texts.BTN_KEEP_AS_IS,
            "med:cancel": texts.BTN_CANCEL,
        }

    def test_dosage_keep_button(self) -> None:
        """«Оставить как есть» — только при правке (§6.6)."""
        plain = {
            button.callback_data for button in callback_buttons(keyboards.dosage_kb())
        }
        keep = {
            button.callback_data: button.text
            for button in callback_buttons(keyboards.dosage_kb(keep=True))
        }
        assert "med:dose:same" not in plain
        assert keep["med:dose:same"] == texts.BTN_KEEP_AS_IS
        assert plain <= set(keep)

    def test_dosage_presets(self) -> None:
        markup = keyboards.dosage_kb()
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(markup)
            if button.callback_data
        }
        for dosage in keyboards.DOSAGE_PRESETS:
            assert texts_by_data[f"med:dose:{dosage}"] == dosage
        assert texts_by_data["med:dose:none"] == texts.BTN_NO_DOSAGE
        assert texts_by_data["med:dose:other"] == texts.BTN_OTHER
        assert "med:cancel" in texts_by_data

    def test_slots_multiselect_marks_selected(self) -> None:
        markup = keyboards.slots_multiselect_kb(selected={"morning", "evening"})
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(markup)
            if button.callback_data
        }
        assert texts_by_data["med:slot:morning"] == "✓ Утро"
        assert texts_by_data["med:slot:noon"] == "Обед"
        assert texts_by_data["med:slot:evening"] == "✓ Вечер"
        assert texts_by_data["med:slot:crisis"] == "Кризис"
        assert "med:slots:done" in texts_by_data


class TestOnboardingKeyboards:
    def test_start(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.onboarding_start_kb())}
        assert data == {"onb:start"}

    def test_skip_targets_next_action(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.onboarding_skip_kb("targets"))
        }
        assert data == {"onb:skip:targets"}

    def test_timezone_step(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.onboarding_timezone_kb())
        }
        assert "onb:tz:Europe/Minsk" in data
        assert "onb:tz:other" in data
        assert "onb:skip:tz" in data

    def test_targets_step(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.onboarding_targets_kb())
        }
        assert {"onb:target:130-80", "onb:target:120-80", "onb:target:other", "onb:skip:targets"} == data

    def test_slots_step(self) -> None:
        markup = keyboards.onboarding_slots_kb(selected={"noon"})
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(markup)
            if button.callback_data
        }
        assert texts_by_data["onb:slot:noon"] == "✓ Обед"
        assert "onb:slots:done" in texts_by_data
        assert "onb:slots:none" in texts_by_data

    def test_time_step(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.onboarding_time_kb("morning"))
        }
        assert {"onb:time:morning:07:00", "onb:time:morning:08:00", "onb:time:morning:09:00"} <= data
        assert "onb:time:morning:other" in data
        assert "onb:time:morning:skip" in data


class TestAboutKeyboards:
    def test_visible_buttons(self) -> None:
        markup = keyboards.about_kb(support_visible=True, author_visible=True)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"about:support", "about:author", "menu:back"}

    def test_hidden_buttons(self) -> None:
        markup = keyboards.about_kb(support_visible=False, author_visible=False)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"menu:back"}

    def test_support_url_button(self) -> None:
        markup = keyboards.support_kb("https://example.org/donate")
        url_buttons = [button for button in buttons(markup) if button.url]
        assert len(url_buttons) == 1
        assert url_buttons[0].url == "https://example.org/donate"
        assert url_buttons[0].text == texts.BTN_OPEN_SUPPORT


class TestDeleteAccountKeyboards:
    def test_delete_account_kb(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.delete_account_kb())
        }
        assert data == {"set:delete:ok", "set:back"}


class TestDoctorKeyboards:
    def test_revoke_buttons_for_open_links(self) -> None:
        markup = keyboards.doctor_kb([(7, "doc@example.org", "active")])
        texts_by_data = {
            button.callback_data: button.text for button in callback_buttons(markup)
        }
        assert texts_by_data == {
            "set:doctor:revoke:7": f"{texts.BTN_REVOKE_DOCTOR}: doc@example.org",
            "set:back": texts.BTN_BACK,
        }

    def test_revoked_link_has_no_revoke_button(self) -> None:
        markup = keyboards.doctor_kb([(7, "doc@example.org", "revoked")])
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"set:back"}

    def test_multiple_links(self) -> None:
        markup = keyboards.doctor_kb(
            [
                (7, "a@example.org", "pending"),
                (8, "b@example.org", "active"),
                (9, "c@example.org", "revoked"),
            ]
        )
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"set:doctor:revoke:7", "set:doctor:revoke:8", "set:back"}

    def test_empty_list_has_only_back(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.doctor_kb([]))}
        assert data == {"set:back"}


class TestAdminKeyboards:
    def test_admin_menu_items(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.admin_menu_kb())}
        assert data == {
            "adm:stats",
            "adm:backups",
            "adm:health",
            "adm:bcast",
            "adm:search",
            "menu:back",
        }

    def test_admin_stats_and_health_refresh(self) -> None:
        stats = {b.callback_data for b in callback_buttons(keyboards.admin_stats_kb())}
        health = {b.callback_data for b in callback_buttons(keyboards.admin_health_kb())}
        assert stats == {"adm:refresh", "adm:back"}
        assert health == {"adm:health", "adm:back"}

    def test_admin_backups_actions(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.admin_backups_kb())
        }
        assert data == {"adm:backup:create", "adm:backup:download", "adm:back"}

    def test_admin_broadcast_preview_actions(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.admin_broadcast_preview_kb())
        }
        assert data == {"adm:bcast:confirm", "adm:bcast:cancel"}

    def test_admin_search_card_carries_telegram_id(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.admin_search_card_kb(42))
        }
        assert data == {"adm:test:42", "adm:back"}


class TestArchiveKeyboards:
    """Клавиатуры архивных записей (SPEC.md, §6.10, §8)."""

    def test_archive_menu_actions(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.archive_menu_kb())}
        assert data == {"set:archive:add", "set:archive:quick", "set:back"}

    def test_archive_date_presets(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.archive_date_kb())}
        assert data == {
            "set:archive:date:today",
            "set:archive:date:yesterday",
            "set:archive:date:3d",
            "set:archive:date:week",
            "set:archive:date:other",
            "m:cancel",
        }
        assert keyboards.ARCHIVE_DATE_PRESETS == {
            "today": 0, "yesterday": 1, "3d": 3, "week": 7,
        }

    def test_archive_time_presets(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.archive_time_kb())}
        assert data == {
            "set:archive:time:08:00",
            "set:archive:time:13:00",
            "set:archive:time:21:00",
            "set:archive:time:other",
            "m:cancel",
        }
        assert keyboards.ARCHIVE_TIME_PRESETS == ("08:00", "13:00", "21:00")

    def test_archive_saved_actions(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.archive_saved_kb())}
        assert data == {
            "m:arch:edit",
            "m:arch:note",
            "m:arch:med",
            "m:arch:feel",
            "m:arch:more",
            "m:arch:menu",
        }

    def test_archive_medication_taken_actions(self) -> None:
        data = {
            button.callback_data
            for button in callback_buttons(keyboards.archive_medication_taken_kb())
        }
        assert data == {"m:arch:med:yes", "m:arch:med:no", "m:arch:menu"}

    def test_archive_feeling_actions(self) -> None:
        data = {
            button.callback_data for button in callback_buttons(keyboards.archive_feeling_kb())
        }
        assert data == {
            "m:arch:feel:normal",
            "m:arch:feel:headache",
            "m:arch:feel:weakness",
            "m:arch:feel:other",
            "m:arch:menu",
        }

    def test_settings_menu_has_archive_entry(self) -> None:
        pairs = {
            (button.text, button.callback_data) for button in callback_buttons(keyboards.settings_kb())
        }
        assert (texts.BTN_ARCHIVE, "set:archive") in pairs


class TestStatsCustomPeriodKeyboards:
    """Свой период и «По дням» (§6.5, §8, 0.5.3)."""

    def test_period_has_custom_button(self) -> None:
        data = [b.callback_data for b in callback_buttons(keyboards.stats_period_kb())]
        assert data == ["stats:7", "stats:30", "stats:cal", "menu:back"]
        texts_ = [b.text for b in buttons(keyboards.stats_period_kb())]
        assert texts.BTN_CUSTOM_PERIOD == "📆 Свой период" in texts_

    @pytest.mark.parametrize("period", ["7", "30", "20261101:20261115"])
    def test_result_has_by_days(self, period: str) -> None:
        data = [b.callback_data for b in callback_buttons(keyboards.stats_result_kb(period))]
        assert data == [f"stats:days:{period}", "stats:csv", "stats:pdf", "stats:periods"]
        assert buttons(keyboards.stats_result_kb(period))[0].text == "📅 По дням"

    @pytest.mark.parametrize(
        ("period", "expected"),
        [("7", "stats:7"), ("30", "stats:30"), ("20261101:20261115", "stats:c:20261101:20261115")],
    )
    def test_days_back_to_summary(self, period: str, expected: str) -> None:
        markup = keyboards.stats_days_kb(period)
        assert [b.callback_data for b in buttons(markup)] == [expected]
        assert buttons(markup)[0].text == texts.BTN_BACK


class TestStatsCalendar:
    TODAY = date(2026, 11, 10)

    def rows(self, markup: InlineKeyboardMarkup) -> list[list[InlineKeyboardButton]]:
        return markup.inline_keyboard

    def test_layout_step1(self) -> None:
        rows = self.rows(keyboards.stats_calendar_kb(2026, 11, self.TODAY))
        assert rows[0][0].text == "Ноябрь 2026" and rows[0][0].callback_data == "stats:noop"
        assert [b.text for b in rows[1]] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        assert all(b.callback_data == "stats:noop" for b in rows[1])
        # 1 ноября 2026 — воскресенье: шесть пустых клеток перед ним.
        first_week = rows[2]
        assert [b.callback_data for b in first_week[:6]] == ["stats:noop"] * 6
        assert first_week[6].text == "1" and first_week[6].callback_data == "stats:day:20261101"
        weeks = rows[2:-2]
        assert all(len(week) == 7 for week in weeks)
        days = [b.text for week in weeks for b in week if b.callback_data != "stats:noop"]
        assert days == [str(d) for d in range(1, 31)]
        # Будущие дни кликабельны (проверка — на сервере).
        assert any(b.callback_data == "stats:day:20261130" for week in weeks for b in week)
        # Текущий месяц: только «◀».
        assert [(b.text, b.callback_data) for b in rows[-2]] == [("◀", "stats:cal:202610")]
        assert rows[-1][0].callback_data == "stats:periods"

    def test_past_month_has_next(self) -> None:
        rows = self.rows(keyboards.stats_calendar_kb(2026, 1, self.TODAY))
        assert [b.callback_data for b in rows[-2]] == ["stats:cal:202512", "stats:cal:202602"]
        assert rows[0][0].text == "Январь 2026"

    def test_december_next_is_january(self) -> None:
        rows = self.rows(keyboards.stats_calendar_kb(2025, 12, self.TODAY))
        assert [b.callback_data for b in rows[-2]] == ["stats:cal:202511", "stats:cal:202601"]

    def test_step2_marks_start_and_keeps_it(self) -> None:
        start = date(2026, 10, 3)
        rows = self.rows(keyboards.stats_calendar_kb(2026, 10, self.TODAY, start))
        days = {b.text: b.callback_data for week in rows[2:-2] for b in week}
        assert days["[3]"] == "stats:day:20261003:20261003"
        assert days["15"] == "stats:day:20261003:20261015"
        assert "3" not in days
        assert [b.callback_data for b in rows[-2]] == [
            "stats:cal:202609:20261003",
            "stats:cal:202611:20261003",
        ]

    def test_step2_other_month_no_mark(self) -> None:
        start = date(2026, 10, 3)
        rows = self.rows(keyboards.stats_calendar_kb(2026, 11, self.TODAY, start))
        labels = [b.text for week in rows[2:-2] for b in week]
        assert not any(label.startswith("[") for label in labels)

    def test_min_year_has_no_prev(self) -> None:
        rows = self.rows(keyboards.stats_calendar_kb(1, 1, self.TODAY))
        assert [b.text for b in rows[-2]] == ["▶"]

    def test_callback_data_fits_limit(self) -> None:
        markup = keyboards.stats_calendar_kb(2026, 10, self.TODAY, date(2026, 10, 31))
        assert max(len(b.callback_data.encode()) for b in callback_buttons(markup)) <= 64


class TestDiaryIntakeKeyboards:
    """«💊 Отметить приём» в дневнике (§6.4, §8, 0.5.5)."""

    def test_diary_mark_button(self) -> None:
        data = [b.callback_data for b in callback_buttons(keyboards.diary_kb(mark_intake=True))]
        assert data == ["m:edit", "d:mt", "menu:back"]
        assert "d:mt" not in [b.callback_data for b in callback_buttons(keyboards.diary_kb())]

    def test_partial_diary_area(self) -> None:
        markup = keyboards.partial_meds_kb("noon", date(2026, 11, 10), {7}, [(7, "Аспирин", "100 мг")], area="d")
        assert [b.callback_data for b in callback_buttons(markup)] == [
            "d:pt:noon:20261110:7", "d:pd:noon:20261110", "d:pc:noon:20261110",
        ]
        assert keyboards.partial_checked_ids(markup) == {7}


class TestReminderSettings056:
    """Экран «Напоминания»: пауза, режим слота, «Повторять, если не ответил» (§6.6, 0.5.6)."""

    def test_rows(self) -> None:
        markup = keyboards.reminders_settings_kb(
            {"morning": True, "noon": False, "evening": True},
            {"morning": "08:00", "evening": "21:00"},
            60,
            {"morning": "both", "evening": "meds"},
            followup_enabled=True,
        )
        rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
        assert rows[0] == [("⏸ Пауза: нет", "set:rem:pause")]
        assert ("💊🩺 Давление и лекарства", "set:rem:mode:morning") in [r[0] for r in rows]
        assert ("💊 Только лекарства", "set:rem:mode:evening") in [r[0] for r in rows]
        assert not any(d == "set:rem:mode:noon" for r in rows for _, d in r)
        assert rows[-2] == [("🔁 Повторять, если не ответил: вкл", "set:rem:followup:off")]

    def test_paused_and_followup_off(self) -> None:
        markup = keyboards.reminders_settings_kb(
            {}, {}, None, followup_enabled=False, paused_until=date(2026, 11, 13)
        )
        rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
        assert rows[0] == [("⏸ На паузе до 13.11", "set:rem:pause")]
        assert rows[-2] == [("🔁 Повторять, если не ответил: выкл", "set:rem:followup:on")]

    def test_mode_kb(self) -> None:
        markup = keyboards.slot_mode_kb("morning", "pressure", has_medications=True)
        rows = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        assert rows == [
            ("💊🩺 Давление и лекарства", "set:rem:mode:morning:both"),
            ("💊 Только лекарства", "set:rem:mode:morning:meds"),
            ("✓ 🩺 Только давление", "set:rem:mode:morning:pressure"),
            ("Назад", "set:reminders"),
        ]
        no_meds = [b.callback_data for row in keyboards.slot_mode_kb("noon", "both", False).inline_keyboard for b in row]
        assert "set:rem:mode:noon:meds" not in no_meds

    def test_pause_kb(self) -> None:
        free = [b.callback_data for row in keyboards.pause_kb(False).inline_keyboard for b in row]
        assert free == ["set:rem:pause:1", "set:rem:pause:3", "set:rem:pause:7", "set:reminders"]
        paused = [b.callback_data for row in keyboards.pause_kb(True).inline_keyboard for b in row]
        assert "set:rem:pause:off" in paused
