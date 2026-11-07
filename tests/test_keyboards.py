"""Тесты клавиатур (bot/keyboards.py): callback_data, состав кнопок, ряды."""
from __future__ import annotations

import re

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import keyboards, texts
from bot.models import Medication

#: Области callback_data, зафиксированные в SPEC.md, раздел 8 (adm — §18).
KNOWN_AREAS = {"menu", "onb", "m", "d", "stats", "csv", "set", "med", "rem", "about", "adm"}

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
        ("stats_period_kb", keyboards.stats_period_kb()),
        ("stats_result_kb", keyboards.stats_result_kb()),
        ("csv_period_kb", keyboards.csv_period_kb()),
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
        ("slot_message_kb", keyboards.slot_message_kb("morning", 5, True)),
        ("slot_message_kb_no_meds", keyboards.slot_message_kb("morning", 5, False)),
        ("partial_meds_kb", keyboards.partial_meds_kb({1}, [med])),
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
        pairs = [(button.text, button.callback_data) for button in buttons(markup)]
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
    def test_with_medications_shows_all_five(self) -> None:
        markup = keyboards.slot_message_kb("morning", 5, True)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {
            "rem:write",
            "rem:all:morning",
            "rem:partial:morning",
            "rem:snooze:5",
            "rem:skip:morning",
        }

    def test_without_medications_hides_med_buttons(self) -> None:
        markup = keyboards.slot_message_kb("evening", 7, False)
        data = {button.callback_data for button in callback_buttons(markup)}
        assert data == {"rem:write", "rem:snooze:7", "rem:skip:evening"}

    def test_partial_checkboxes(self) -> None:
        meds = [(1, "Лозартан", "50 мг"), (2, "Амлодипин", "5 мг")]
        markup = keyboards.partial_meds_kb(checked={2}, medications=meds)
        texts_by_data = {
            button.callback_data: button.text
            for button in callback_buttons(markup)
            if button.callback_data
        }
        assert texts_by_data["rem:med:1"] == "Лозартан 50 мг"
        assert texts_by_data["rem:med:2"] == "✓ Амлодипин 5 мг"
        assert "rem:partial:done" in texts_by_data

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
            "menu:stats",
            "menu:settings",
            "menu:cabinet",
            "menu:report",
            "menu:about",
        }

    def test_cabinet_button_text(self) -> None:
        buttons = {button.callback_data: button.text for button in callback_buttons(keyboards.main_menu())}
        assert buttons["menu:cabinet"] == texts.BTN_CABINET


class TestSettingsKeyboards:
    def test_settings_menu_items(self) -> None:
        data = {button.callback_data for button in callback_buttons(keyboards.settings_kb())}
        assert data == {
            "set:targets",
            "set:reminders",
            "set:meds",
            "set:archive",
            "set:doctor",
            "set:tz",
            "set:csv",
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
