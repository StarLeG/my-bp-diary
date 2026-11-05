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
        assert texts.fmt_saved(135, 85) == "✅ 135/85. Записано."

    def test_fmt_saved_with_pulse(self) -> None:
        assert texts.fmt_saved(135, 85, 72) == "✅ 135/85, пульс 72. Записано."

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

    def test_med_add_name_prompt_example(self) -> None:
        assert "Например: Лозартан, Амлодипин" in texts.MEDS_ADD_NAME_PROMPT

    def test_high_pressure_warning_is_polite(self) -> None:
        assert texts.HIGH_PRESSURE_WARNING == (
            "Эти значения стоит обсудить с врачом на ближайшем приёме."
        )

    def test_debounce_and_duplicate_texts(self) -> None:
        assert texts.DEBOUNCE_NOTICE == "Уже записываю…"
        assert texts.DUPLICATE_MEASUREMENT == "Это давление уже записано"

    def test_csv_too_many_text(self) -> None:
        assert texts.CSV_TOO_MANY == "Слишком много записей, выберите 7 или 30 дней"


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
        # Тексты не пустые и без латинских предложений (кроме IANA-примеров).
        exceptions = (texts.ONB_TZ_OTHER_PROMPT, texts.TZ_OTHER_PROMPT, texts.CABINET_LINK_SENT)
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
