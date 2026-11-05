"""Клавиатуры и схемы callback_data (SPEC.md, раздел 8).

Формат callback_data: `<область>:<действие>[:<значение>]`, не длиннее 64 байт.
Значение может содержать двоеточие (HH:MM), поэтому разбор — split(":", 2).
Выбранные значения кнопок передаются в callback_data; состояние мультивыбора
держит вызывающий код, функции здесь — чистые.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts

#: Максимальная длина callback_data в байтах (ограничение Telegram).
CALLBACK_DATA_LIMIT = 64

# ─────────────────────────── Области callback_data ───────────────────────────
# menu:  главное меню
# onb:   онбординг
# m:     запись давления
# d:     дневник
# stats: статистика
# csv:   экспорт CSV
# set:   настройки
# med:   лекарства
# rem:   напоминания (кнопки слот-сообщений)
# about: «О боте / Поддержать»

#: Целевые значения по умолчанию (кнопка → (систолическое, диастолическое)).
TARGET_PRESETS = {
    "120-80": (120, 80),
    "130-80": (130, 80),
    "140-90": (140, 90),
}

#: Популярные города онбординга и настроек (город → IANA).
TIMEZONE_CITIES = {
    "Москва": "Europe/Moscow",
    "Санкт-Петербург": "Europe/Moscow",
    "Екатеринбург": "Asia/Yekaterinburg",
    "Новосибирск": "Asia/Novosibirsk",
    "Казань": "Europe/Moscow",
    "Минск": "Europe/Minsk",
}

#: Популярные дозировки (кнопки шага 2 настройки лекарства).
DOSAGE_PRESETS = ("5 мг", "10 мг", "20 мг", "50 мг", "100 мг")

#: Варианты «Повтор после лекарства».
REMEASURE_VARIANTS = {
    texts.REMEASURE_OFF: "off",
    "30 минут": "30",
    "60 минут": "60",
    "120 минут": "120",
}

#: Слоты и предлагаемое время (SPEC.md, раздел 6.1).
SLOT_ORDER = ("morning", "noon", "evening")
SLOT_TIME_PRESETS = {
    "morning": ("07:00", "08:00", "09:00"),
    "noon": ("12:00", "13:00", "14:00"),
    "evening": ("20:00", "21:00", "22:00"),
}

#: Пределы числовых кнопок записи (SPEC.md, раздел 6.3).
SYSTOLIC_RANGE = range(100, 201, 5)
DIASTOLIC_RANGE = range(60, 121, 5)
PULSE_RANGE = range(50, 121, 5)


def _rows(values: Sequence[object], per_row: int = 4) -> list[list[InlineKeyboardButton]]:
    """Разбить кнопки на ряды по `per_row` штук."""
    return [list(values[i : i + per_row]) for i in range(0, len(values), per_row)]


def _numbers(
    values: Iterable[int], prefix: str
) -> list[InlineKeyboardButton]:
    """Числовые кнопки шага 5: callback_data вида `<prefix>:<число>`."""
    return [
        InlineKeyboardButton(text=str(value), callback_data=f"{prefix}:{value}")
        for value in values
    ]


# ─────────────────────────── Главное меню ───────────────────────────


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню (SPEC.md, раздел 6.2)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="menu:measure"))
    builder.row(InlineKeyboardButton(text=texts.BTN_DIARY, callback_data="menu:diary"))
    builder.row(InlineKeyboardButton(text=texts.BTN_STATS, callback_data="menu:stats"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SETTINGS, callback_data="menu:settings"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CABINET, callback_data="menu:cabinet"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ABOUT, callback_data="menu:about"))
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    """Единственная кнопка «В меню»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Запись давления ───────────────────────────


def systolic_kb() -> InlineKeyboardMarkup:
    """Кнопки систолического 100…200 шаг 5 + «Другое» + «Отмена»."""
    builder = InlineKeyboardBuilder()
    for row in _rows(_numbers(SYSTOLIC_RANGE, "m:sys")):
        builder.row(*row)
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="m:sys:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def diastolic_kb() -> InlineKeyboardMarkup:
    """Кнопки диастолического 60…120 шаг 5 + «Другое» + «Отмена»."""
    builder = InlineKeyboardBuilder()
    for row in _rows(_numbers(DIASTOLIC_RANGE, "m:dia")):
        builder.row(*row)
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="m:dia:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def pulse_kb() -> InlineKeyboardMarkup:
    """Кнопки пульса 50…120 шаг 5 + «Другое» + «Пропустить»."""
    builder = InlineKeyboardBuilder()
    for row in _rows(_numbers(PULSE_RANGE, "m:pulse")):
        builder.row(*row)
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="m:pulse:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP, callback_data="m:pulse:skip"))
    return builder.as_markup()


def saved_kb() -> InlineKeyboardMarkup:
    """Кнопки после сохранения измерения (опциональные шаги)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_ADD_NOTE, callback_data="m:note"))
    builder.row(InlineKeyboardButton(text=texts.BTN_TOOK_MED, callback_data="m:med"))
    builder.row(InlineKeyboardButton(text=texts.BTN_FEELING, callback_data="m:feeling"))
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


def medication_taken_kb() -> InlineKeyboardMarkup:
    """«Приняли лекарство?» → Да / Нет / Пропустил (TRUE / FALSE / NULL)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_MED_YES, callback_data="m:med:yes"),
        InlineKeyboardButton(text=texts.BTN_MED_NO, callback_data="m:med:no"),
        InlineKeyboardButton(text=texts.BTN_MED_SKIPPED, callback_data="m:med:skip"),
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


def feeling_kb() -> InlineKeyboardMarkup:
    """Самочувствие: Норма / Головная боль / Слабость / Другое."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_FEEL_NORMAL, callback_data="m:feeling:normal"),
        InlineKeyboardButton(text=texts.BTN_FEEL_HEADACHE, callback_data="m:feeling:headache"),
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_FEEL_WEAKNESS, callback_data="m:feeling:weakness"),
        InlineKeyboardButton(text=texts.BTN_FEEL_OTHER, callback_data="m:feeling:other"),
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


def note_cancel_kb() -> InlineKeyboardMarkup:
    """Свободный ввод заметки: только «Отмена» (каждое состояние FSM — с ней)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def no_remeasure_kb(measurement_id: int) -> InlineKeyboardMarkup:
    """«Не надо» — отменить запланированный повторный замер записи (§6.3)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_NO_NEED, callback_data=f"m:noremeasure:{measurement_id}"
        )
    )
    return builder.as_markup()


def meds_ask_kb() -> InlineKeyboardMarkup:
    """Подсказка «Напоминать, какие лекарства?»: «Добавить» / «Не сейчас»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_ADD, callback_data="med:add"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_NOT_NOW, callback_data="m:medask:dismiss")
    )
    return builder.as_markup()


def input_cancel_kb(callback: str = "m:cancel") -> InlineKeyboardMarkup:
    """Свободный ввод (число, время, таймзона): кнопка «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=callback))
    return builder.as_markup()


# ─────────────────────────── Дневник ───────────────────────────


def diary_kb(is_empty: bool = False) -> InlineKeyboardMarkup:
    """Дневник за сегодня: при пустом списке — «Записать давление», всегда «Назад»."""
    builder = InlineKeyboardBuilder()
    if is_empty:
        builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="menu:measure"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Статистика ───────────────────────────


def stats_period_kb() -> InlineKeyboardMarkup:
    """Выбор периода статистики: 7 / 30 дней."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_7_DAYS, callback_data="stats:7"),
        InlineKeyboardButton(text=texts.BTN_30_DAYS, callback_data="stats:30"),
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


def stats_result_kb() -> InlineKeyboardMarkup:
    """Под сводкой: экспорт CSV и «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_CSV, callback_data="stats:csv"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="stats:periods"))
    return builder.as_markup()


def csv_period_kb() -> InlineKeyboardMarkup:
    """Период экспорта CSV: 7 дней / 30 дней / всё время."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_7_DAYS, callback_data="csv:7"))
    builder.row(InlineKeyboardButton(text=texts.BTN_30_DAYS, callback_data="csv:30"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ALL_TIME, callback_data="csv:all"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Настройки ───────────────────────────


def settings_kb() -> InlineKeyboardMarkup:
    """Меню настроек (SPEC.md, раздел 6.6)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_TARGETS, callback_data="set:targets"))
    builder.row(InlineKeyboardButton(text=texts.BTN_REMINDERS, callback_data="set:reminders"))
    builder.row(InlineKeyboardButton(text=texts.BTN_MY_MEDS, callback_data="set:meds"))
    builder.row(InlineKeyboardButton(text=texts.BTN_TIMEZONE, callback_data="set:tz"))
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_CSV, callback_data="set:csv"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


def targets_kb() -> InlineKeyboardMarkup:
    """Целевые значения: 120/80, 130/80, 140/90, «Другое»."""
    builder = InlineKeyboardBuilder()
    for key, (sys_v, dia_v) in TARGET_PRESETS.items():
        builder.row(
            InlineKeyboardButton(text=f"{sys_v}/{dia_v}", callback_data=f"set:target:{key}")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="set:target:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def reminders_settings_kb(
    enabled_slots: Mapping[str, bool],
    slot_times: Mapping[str, str],
    med_remeasure_delay: Optional[int],
) -> InlineKeyboardMarkup:
    """Настройка напоминаний: слоты вкл/выкл, время, повтор после лекарства."""
    builder = InlineKeyboardBuilder()
    for slot in SLOT_ORDER:
        mark = "✅" if enabled_slots.get(slot) else "☐"
        label = f"{mark} {texts.SLOT_LABELS[slot]}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"set:slot:{slot}"))
        if enabled_slots.get(slot):
            time_str = slot_times.get(slot, "?")
            builder.row(
                InlineKeyboardButton(
                    text=f"⏰ {time_str}", callback_data=f"set:time:{slot}:show"
                )
            )
    delay_label = (
        texts.REMEASURE_OFF if med_remeasure_delay is None else f"{med_remeasure_delay} минут"
    )
    builder.row(
        InlineKeyboardButton(
            text=f"Повтор после лекарства: {delay_label}", callback_data="set:remeasure"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def slot_time_kb(slot: str) -> InlineKeyboardMarkup:
    """Время слотов: 07:00/08:00/09:00, 12:00/13:00/14:00, 20:00/21:00/22:00 + «Другое»."""
    builder = InlineKeyboardBuilder()
    presets = SLOT_TIME_PRESETS.get(slot, ())
    builder.row(
        *[
            InlineKeyboardButton(text=t, callback_data=f"set:time:{slot}:{t}")
            for t in presets
        ]
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data=f"set:time:{slot}:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:reminders"))
    return builder.as_markup()


def remeasure_delay_kb() -> InlineKeyboardMarkup:
    """«Повтор после лекарства»: выкл / 30 / 60 / 120 минут."""
    builder = InlineKeyboardBuilder()
    for label, value in REMEASURE_VARIANTS.items():
        builder.row(InlineKeyboardButton(text=label, callback_data=f"set:remeasure:{value}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:reminders"))
    return builder.as_markup()


def timezone_kb(callback_prefix: str = "set:tz") -> InlineKeyboardMarkup:
    """Часовой пояс: популярные города + «Другой» (IANA-ввод)."""
    builder = InlineKeyboardBuilder()
    for city, iana in TIMEZONE_CITIES.items():
        builder.row(InlineKeyboardButton(text=city, callback_data=f"{callback_prefix}:{iana}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data=f"{callback_prefix}:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


# ─────────────────────────── Лекарства ───────────────────────────


def medications_list_kb(
    medications: Sequence[tuple[int, str, str]],
) -> InlineKeyboardMarkup:
    """Список лекарств: у каждого «Удалить», внизу «Добавить» и «Назад».

    `medications` — кортежи (id, название, дозировка).
    """
    builder = InlineKeyboardBuilder()
    for med_id, name, dosage in medications:
        builder.row(
            InlineKeyboardButton(
                text=f"{texts.BTN_DELETE}: {name} {dosage}", callback_data=f"med:del:{med_id}"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_ADD, callback_data="med:add"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def dosage_kb() -> InlineKeyboardMarkup:
    """Дозировка: 5/10/20/50/100 мг + «Другое» (число + мг/мл/таб)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        *[
            InlineKeyboardButton(text=d, callback_data=f"med:dose:{d}")
            for d in DOSAGE_PRESETS[:3]
        ]
    )
    builder.row(
        *[
            InlineKeyboardButton(text=d, callback_data=f"med:dose:{d}")
            for d in DOSAGE_PRESETS[3:]
        ]
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="med:dose:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="med:cancel"))
    return builder.as_markup()


def med_confirm_delete_kb(medication_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления лекарства: «Удалить» / «Отмена» (§6.6)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_DELETE, callback_data=f"med:delok:{medication_id}"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="med:cancel"))
    return builder.as_markup()


def slots_multiselect_kb(
    selected: Iterable[str], done_callback: str = "med:slots:done", toggle_prefix: str = "med:slot"
) -> InlineKeyboardMarkup:
    """Мультивыбор слотов: ✓ у отмеченных + «Готово»."""
    chosen = set(selected)
    builder = InlineKeyboardBuilder()
    for slot in SLOT_ORDER:
        mark = "✓ " if slot in chosen else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{texts.SLOT_LABELS[slot]}", callback_data=f"{toggle_prefix}:{slot}"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data=done_callback))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="med:cancel"))
    return builder.as_markup()


# ─────────────────────────── Напоминания (слот-сообщения) ───────────────────────────


def slot_message_kb(
    slot: str, reminder_id: int, has_medications: bool = True
) -> InlineKeyboardMarkup:
    """Кнопки слот-сообщения (SPEC.md, раздел 6.7).

    В callback_data несётся слот и id строки напоминания: «Принял всё ✓» /
    «Частично…» / «Пропускаю» действуют по слоту, «Через 30 мин» — по строке.
    Без лекарств в слоте кнопки «Принял всё ✓» и «Частично…» не показываются.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="rem:write"))
    if has_medications:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_TAKE_ALL, callback_data=f"rem:all:{slot}")
        )
        builder.row(
            InlineKeyboardButton(text=texts.BTN_PARTIAL, callback_data=f"rem:partial:{slot}")
        )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_SNOOZE_30, callback_data=f"rem:snooze:{reminder_id}")
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_SKIP_SLOT, callback_data=f"rem:skip:{slot}")
    )
    return builder.as_markup()


def partial_meds_kb(
    checked: Iterable[int], medications: Sequence[tuple[int, str, str]]
) -> InlineKeyboardMarkup:
    """Чекбоксы «Частично…»: ✓ у отмеченных + «Готово».

    `medications` — кортежи (id, название, дозировка); `checked` — id отмеченных.
    """
    chosen = set(checked)
    builder = InlineKeyboardBuilder()
    for med_id, name, dosage in medications:
        mark = "✓ " if med_id in chosen else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{name} {dosage}", callback_data=f"rem:med:{med_id}"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data="rem:partial:done"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rem:partial:cancel"))
    return builder.as_markup()


def remeasure_kb(reminder_id: int) -> InlineKeyboardMarkup:
    """Кнопки повторного замера после лекарства (§6.7).

    «Через 30 мин» и «Больше не напоминать» действуют по строке повтора,
    поэтому несут её id в callback_data.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_REMEASURE_NOW, callback_data="rem:now"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_SNOOZE_30, callback_data=f"rem:snooze:{reminder_id}")
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_REMEASURE_OFF, callback_data=f"rem:off:{reminder_id}")
    )
    return builder.as_markup()


# ─────────────────────────── Онбординг ───────────────────────────


def onboarding_start_kb() -> InlineKeyboardMarkup:
    """Приветствие: «Начать»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_START, callback_data="onb:start"))
    return builder.as_markup()


def onboarding_skip_kb(next_action: str) -> InlineKeyboardMarkup:
    """Кнопка «Пропустить» — принимаются значения по умолчанию."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP, callback_data=f"onb:skip:{next_action}"))
    return builder.as_markup()


def onboarding_timezone_kb() -> InlineKeyboardMarkup:
    """Шаг таймзоны онбординга: города + «Другой»."""
    builder = InlineKeyboardBuilder()
    for city, iana in TIMEZONE_CITIES.items():
        builder.row(InlineKeyboardButton(text=city, callback_data=f"onb:tz:{iana}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="onb:tz:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP, callback_data="onb:skip:tz"))
    return builder.as_markup()


def onboarding_targets_kb() -> InlineKeyboardMarkup:
    """Шаг целей онбординга: 130/80 (по умолчанию), 120/80, «Другое» + «Пропустить»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="130/80 (по умолчанию)", callback_data="onb:target:130-80")
    )
    builder.row(InlineKeyboardButton(text="120/80", callback_data="onb:target:120-80"))
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="onb:target:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP, callback_data="onb:skip:targets"))
    return builder.as_markup()


def onboarding_slots_kb(selected: Iterable[str]) -> InlineKeyboardMarkup:
    """Шаг слотов онбординга: мультивыбор + «Готово» + «Без напоминаний»."""
    chosen = set(selected)
    builder = InlineKeyboardBuilder()
    for slot in SLOT_ORDER:
        mark = "✓ " if slot in chosen else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{texts.SLOT_LABELS[slot]}", callback_data=f"onb:slot:{slot}"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data="onb:slots:done"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_NO_REMINDERS, callback_data="onb:slots:none")
    )
    return builder.as_markup()


def onboarding_time_kb(slot: str) -> InlineKeyboardMarkup:
    """Шаг времени слота онбординга: пресеты + «Другое»."""
    builder = InlineKeyboardBuilder()
    presets = SLOT_TIME_PRESETS.get(slot, ())
    builder.row(
        *[
            InlineKeyboardButton(text=t, callback_data=f"onb:time:{slot}:{t}")
            for t in presets
        ]
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_OTHER, callback_data=f"onb:time:{slot}:other"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP, callback_data=f"onb:time:{slot}:skip"))
    return builder.as_markup()


# ─────────────────────────── О боте / Поддержать ───────────────────────────


def about_kb(
    support_visible: bool = False,
    author_visible: bool = False,
    back_callback: str = "menu:back",
) -> InlineKeyboardMarkup:
    """Экран «О боте»: «Поддержать проект» и «Написать автору» — по видимости."""
    builder = InlineKeyboardBuilder()
    if support_visible:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_SUPPORT_PROJECT, callback_data="about:support")
        )
    if author_visible:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_WRITE_AUTHOR, callback_data="about:author")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data=back_callback))
    return builder.as_markup()


def support_kb(url: str) -> InlineKeyboardMarkup:
    """Экран «Поддержать»: одна URL-кнопка (реквизитов в боте нет)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_OPEN_SUPPORT, url=url))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()
