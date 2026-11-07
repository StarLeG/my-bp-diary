"""Тесты текстов (bot/texts.py): состав, шаблоны, терминология, целостность."""
from __future__ import annotations

import bot.texts as texts


def all_text_values() -> list[str]:
    """Все строковые константы модуля (функции не берём)."""
    return [
        value
        for name, value in vars(texts).items()
        if not name.startswith("_") and isinstance(value, str)
    ]


class TestFormatting:
    def test_fmt_saved_without_pulse(self) -> None:
        # SPEC.md, §6.3 — сводка дословно, без пульса — «Пульс: —».
        assert texts.fmt_saved(135, 85) == (
            "✅ Записано\nСистолическое: 135\nДиастолическое: 85\nПульс: —"
        )

    def test_fmt_saved_with_pulse(self) -> None:
        assert texts.fmt_saved(135, 85, 72) == (
            "✅ Записано\nСистолическое: 135\nДиастолическое: 85\nПульс: 72"
        )

    def test_fmt_updated_without_pulse(self) -> None:
        assert texts.fmt_updated(135, 85) == (
            "✅ Обновлено\nСистолическое: 135\nДиастолическое: 85\nПульс: —"
        )

    def test_fmt_updated_with_pulse(self) -> None:
        assert texts.fmt_updated(135, 85, 72) == (
            "✅ Обновлено\nСистолическое: 135\nДиастолическое: 85\nПульс: 72"
        )

    def test_fmt_keypad_prompt(self) -> None:
        """Экран калькулятора: вопрос, набор и подсказка (§6.3)."""
        assert texts.fmt_keypad_prompt(texts.ASK_SYSTOLIC, "12") == (
            "Систолическое (верхнее)?\n\n"
            "Введено: 12\nМожно также написать число сообщением."
        )

    def test_fmt_keypad_prompt_empty_buffer(self) -> None:
        assert texts.fmt_keypad_prompt(texts.ASK_PULSE, "") == (
            "Пульс?\n\nВведено: —\nМожно также написать число сообщением."
        )

    def test_fmt_intake_item(self) -> None:
        """Строка мультивыбора: «Лозартан 50 мг · утро» (§6.3)."""
        assert texts.fmt_intake_item("Лозартан", "50 мг", "morning") == "Лозартан 50 мг · утро"
        assert texts.fmt_intake_item("Амлодипин", "", "evening") == "Амлодипин · вечер"

    def test_fmt_meds_intake(self) -> None:
        assert texts.fmt_meds_intake(["Лозартан 50 мг", "Амлодипин 5 мг"]) == (
            "💊 Принято: Лозартан 50 мг, Амлодипин 5 мг"
        )
        assert texts.fmt_meds_intake([]) == "💊 Принято: "

    def test_fmt_measurement_short(self) -> None:
        assert texts.fmt_measurement_short(135, 85) == "135/85"
        assert texts.fmt_measurement_short(135, 85, 72) == "135/85, пульс 72"

    def test_fmt_diary_line_full(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, 72, True, "normal")
        assert line == "08:15 — 135/85, пульс 72 · лекарство: да · норма"

    def test_fmt_diary_line_minimal(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, None, None, None)
        assert line == "08:15 — 135/85"

    def test_fmt_diary_line_med_flag_false(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, None, False, None)
        assert line == "08:15 — 135/85 · лекарство: нет"

    def test_fmt_diary_line_med_flag_none_skipped(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, None, None, "headache")
        assert line == "08:15 — 135/85 · головная боль"

    def test_fmt_slot_message_with_medications(self) -> None:
        message = texts.fmt_slot_message("morning", ["Лозартан 50 мг", "Амлодипин 5 мг"])
        assert message == "Пора измерить давление ☀️\n💊 Лекарства: Лозартан 50 мг, Амлодипин 5 мг"

    def test_fmt_slot_message_without_medications(self) -> None:
        message = texts.fmt_slot_message("evening", [])
        assert message == "Пора измерить давление 🌙"
        assert "💊" not in message

    def test_fmt_slot_message_unknown_slot_emoji(self) -> None:
        message = texts.fmt_slot_message("unknown", [])
        assert message == "Пора измерить давление ⏰"

    def test_fmt_med_item(self) -> None:
        assert texts.fmt_med_item("Лозартан", "50 мг") == "Лозартан 50 мг"

    def test_fmt_med_item_without_dosage(self) -> None:
        """Без дозировки — просто название, без лишнего пробела."""
        assert texts.fmt_med_item("Лозартан", "") == "Лозартан"

    def test_fmt_meds_summary(self) -> None:
        summary = texts.fmt_meds_summary(["Лозартан 50 мг"], ["Амлодипин 5 мг"])
        assert summary == "принято: Лозартан 50 мг; пропущено: Амлодипин 5 мг"

    def test_fmt_meds_summary_only_taken(self) -> None:
        assert texts.fmt_meds_summary(["Лозартан 50 мг"], []) == "принято: Лозартан 50 мг"

    def test_fmt_meds_summary_empty(self) -> None:
        assert texts.fmt_meds_summary([], []) == "нет данных"

    def test_fmt_stats_full(self) -> None:
        result = texts.fmt_stats(
            days=7,
            date_from="29.10",
            date_to="04.11",
            count=12,
            sys_avg=128,
            sys_min=115,
            sys_max=145,
            dia_avg=82,
            dia_min=72,
            dia_max=92,
            pulse_avg=68,
            trend=texts.STATS_TREND_STABLE,
            target="130/80",
            goal_verdict=texts.STATS_GOAL_OK,
            adherence=texts.STATS_ADHERENCE.format(taken=18, planned=21),
        )
        lines = result.splitlines()
        assert lines[0] == "📊 За 7 дней (29.10–04.11)"
        assert lines[1] == "Измерений: 12"
        assert lines[2] == "Систолическое: в среднем 128 (115…145)"
        assert lines[3] == "Диастолическое: в среднем 82 (72…92)"
        assert lines[4] == "Пульс: в среднем 68"
        assert lines[5] == "Тренд: стабильно"
        assert lines[6] == "Цель 130/80 — среднее в пределах цели"
        assert lines[7] == "Лекарства: принято 18 из 21 приёмов за период"

    def test_fmt_stats_optional_fields(self) -> None:
        result = texts.fmt_stats(
            days=7,
            date_from="29.10",
            date_to="04.11",
            count=3,
            sys_avg=128,
            sys_min=115,
            sys_max=145,
            dia_avg=82,
            dia_min=72,
            dia_max=92,
            pulse_avg=None,
            trend=texts.STATS_TREND_FEW,
            target="130/80",
            goal_verdict=texts.STATS_GOAL_ABOVE,
            adherence=None,
        )
        assert "Пульс" not in result
        assert "Лекарства" not in result
        assert "мало данных для тренда" in result


class TestCanonicalTexts:
    def test_about_text_canonical(self) -> None:
        # Канонические тексты SPEC.md, раздел 6.8.
        assert "Бота пишет один разработчик в свободное время" in texts.ABOUT_TEXT
        assert "не ставит диагнозов и не даёт медицинских советов" in texts.ABOUT_TEXT

    def test_support_text_canonical(self) -> None:
        assert "Поддержка добровольная, функции за оплатой не закрыты" in texts.SUPPORT_TEXT
        assert "Реквизиты" not in texts.SUPPORT_TEXT

    def test_med_add_done_canonical(self) -> None:
        assert texts.MEDS_ADD_DONE == (
            "Лекарство добавлено. Оно будет появляться в напоминаниях в выбранные слоты"
        )

    def test_med_add_done_crisis_canonical(self) -> None:
        """Только кризис — напоминаний не будет (§6.7)."""
        assert texts.MEDS_ADD_DONE_CRISIS == (
            "Лекарство добавлено. Напоминаний не будет — выбран только приём «Кризис»"
        )
        assert texts.SLOT_LABELS["crisis"] == "Кризис"

    def test_med_add_name_prompt_example(self) -> None:
        assert "Например: Лозартан, Амлодипин" in texts.MEDS_ADD_NAME_PROMPT

    def test_med_edit_done_canonical(self) -> None:
        # SPEC.md, §6.6 — финал правки дословно.
        assert texts.MEDS_EDIT_DONE == (
            "Лекарство обновлено. Оно будет появляться в напоминаниях в выбранные слоты"
        )

    def test_med_edit_done_crisis_canonical(self) -> None:
        assert texts.MEDS_EDIT_DONE_CRISIS == (
            "Лекарство обновлено. Напоминаний не будет — выбран только приём «Кризис»"
        )

    def test_med_deleted_toast_canonical(self) -> None:
        # SPEC.md, §6.6 — тост удаления, без подтверждения.
        assert texts.MEDS_DELETED == "Лекарство удалено"
        assert not hasattr(texts, "MEDS_CONFIRM_DELETE")

    def test_med_edit_prompts_canonical(self) -> None:
        # SPEC.md, §6.6 — подсказки шагов правки с текущими значениями.
        assert texts.MEDS_EDIT_NAME_PROMPT.format(name="Лозартан") == (
            "Название лекарства (до 100 символов).\nСейчас: Лозартан"
        )
        assert texts.MEDS_EDIT_DOSAGE_PROMPT.format(current="50 мг") == (
            "Дозировка?\nСейчас: 50 мг"
        )
        assert texts.MEDS_EDIT_SLOTS_PROMPT.format(slots="Утро, Вечер") == (
            "Когда принимать? Можно выбрать несколько.\nСейчас: Утро, Вечер"
        )
        assert texts.MEDS_EDIT_NAME_TAKEN == (
            "Лекарство с таким названием уже есть — введите другое."
        )
        assert texts.MEDS_EDIT_PICK == "Какое лекарство изменить?"
        assert texts.MEDS_DEL_PICK == "Какое лекарство удалить?"
        assert texts.BTN_KEEP_AS_IS == "Оставить как есть"

    def test_high_pressure_warning_is_polite(self) -> None:
        assert texts.HIGH_PRESSURE_WARNING == (
            "Эти значения стоит обсудить с врачом на ближайшем приёме."
        )

    def test_debounce_and_duplicate_texts(self) -> None:
        assert texts.DEBOUNCE_NOTICE == "Уже записываю…"
        assert texts.DUPLICATE_MEASUREMENT == "Это давление уже записано"

    def test_edit_buttons_canonical(self) -> None:
        # SPEC.md, §6.3 — кнопки правки последней записи дословно.
        assert texts.BTN_EDIT == "✏️ Изменить"
        assert texts.BTN_EDIT_LAST == "✏️ Изменить последнюю"
        assert texts.MEASUREMENT_UPDATED == "✅ Обновлено"

    def test_csv_too_many_text(self) -> None:
        assert texts.CSV_TOO_MANY == "Слишком много записей, выберите 7 или 30 дней"

    def test_admin_forbidden_canonical(self) -> None:
        # SPEC.md, §18: ответ на чужой запрос — «Доступ запрещен» (без «ё»).
        assert texts.ADMIN_FORBIDDEN == "Доступ запрещен"

    def test_doctor_report_link_canonical(self) -> None:
        # SPEC.md, §21, п.5 — дословно.
        assert texts.DOCTOR_REPORT_LINK == (
            "Ссылка для вашего врача сформирована. Перешлите её ему: {url}"
        )

    def test_admin_broadcast_texts_canonical(self) -> None:
        # SPEC.md, §18.5 — дословно.
        assert texts.ADMIN_BCAST_PROGRESS == "Отправлено {done} из {total}..."
        assert texts.ADMIN_BCAST_REPORT == (
            "Рассылка завершена.\nДоставлено: {delivered}, Заблокировали: {blocked}"
        )

    def test_admin_menu_buttons_canonical(self) -> None:
        # SPEC.md, §18.1 — пункты inline-клавиатуры дословно.
        assert texts.BTN_ADMIN_STATS == "📊 Статистика БД"
        assert texts.BTN_ADMIN_BACKUPS == "💾 Бэкапы"
        assert texts.BTN_ADMIN_HEALTH == "🩺 Здоровье системы"
        assert texts.BTN_ADMIN_BROADCAST == "📢 Рассылка"
        assert texts.BTN_ADMIN_SEARCH == "👤 Поиск пользователя"

    def test_delete_account_texts_canonical(self) -> None:
        # SPEC.md, §19 — дословно.
        assert texts.DELETE_ACCOUNT_WARNING.startswith("Это действие необратимо...")
        assert texts.DELETE_ACCOUNT_DONE == "Ваш аккаунт и все данные удалены"

    def test_backup_integrity_status_canonical(self) -> None:
        # SPEC.md, §18.3: статус верификации — «integrity_check: OK».
        assert texts.fmt_backup_status(True) == "integrity_check: OK"


class TestTone:
    def test_no_word_donate(self) -> None:
        # Терминология: «поддержать», не «донат» (SPEC.md, раздел 11).
        for value in all_text_values():
            assert "донат" not in value.lower()

    def test_no_medical_advice(self) -> None:
        forbidden = ("скорая", "инфаркт", "инсульт", "риск смерти", "неотложн")
        for value in all_text_values():
            for word in forbidden:
                assert word not in value.lower()

    def test_diagnosis_only_in_disclaimer(self) -> None:
        # Слово «диагноз» допустимо только в дисклеймере «О боте» (§6.8).
        for value in all_text_values():
            if "диагноз" in value.lower():
                assert value is texts.ABOUT_TEXT
                assert "не ставит диагнозов" in value

    def test_measurement_word_not_zamer(self) -> None:
        for value in all_text_values():
            assert "замер" not in value.lower()

    def test_all_texts_are_russian_or_symbols(self) -> None:
        # Тексты не пустые и без латинских предложений (кроме IANA-примеров
        # и технических статусов SPEC.md, §18.3–§18.4: «integrity_check: FAIL»,
        # «Running»/«Stopped» — дословно).
        exceptions = (
            texts.ONB_TZ_OTHER_PROMPT,
            texts.TZ_OTHER_PROMPT,
            texts.CABINET_LINK_SENT,
            texts.ADMIN_BACKUP_INTEGRITY_FAIL,
            texts.ADMIN_HEALTH_SCHEDULER_RUNNING,
            texts.ADMIN_HEALTH_SCHEDULER_STOPPED,
        )
        for value in all_text_values():
            if value in exceptions:
                continue
            assert value.strip(), "пустой текст"
            latin_words = [
                word
                for word in value.split()
                if word.isascii() and word.isalpha() and len(word) > 3
            ]
            assert not latin_words, f"латиница в тексте: {value!r}"
