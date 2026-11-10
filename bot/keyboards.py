"""Клавиатуры и схемы callback_data (SPEC.md, раздел 8).

Формат callback_data: `<область>:<действие>[:<значение>]`, не длиннее 64 байт.
Значение может содержать двоеточие (HH:MM), поэтому разбор — split(":", 2).
Выбранные значения кнопок передаются в callback_data; состояние мультивыбора
держит вызывающий код, функции здесь — чистые.
"""
from __future__ import annotations

import calendar
from datetime import MINYEAR, date
from typing import Collection, Iterable, Mapping, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.models import Medication

#: Максимальная длина callback_data в байтах (ограничение Telegram).
CALLBACK_DATA_LIMIT = 64

# ─────────────────────────── Области callback_data ───────────────────────────
# menu:  главное меню
# onb:   онбординг
# m:     запись давления
# d:     дневник
# stats: статистика
# csv:   экспорт CSV
# pdf:   экспорт PDF (§10.1)
# set:   настройки
# med:   лекарства (add, edit, edit:{id}, del, del:{id}, dose:*, dose:same,
#        slot:*, slots:done, name:same, cancel)
# rem:   напоминания (кнопки слот-сообщений)
# about: «О боте / Поддержать»
# adm:   административная панель
# doc:   сторона врача в боте (приглашение, меню врача, §21)

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

#: callback_data кнопок дозировки (frozenset, а не генератор: генератор
#: исчерпывается после первого прохода и фильтр перестаёт срабатывать).
DOSAGE_CALLBACKS = frozenset(f"med:dose:{d}" for d in DOSAGE_PRESETS)

#: Отключить превью ссылок (одноразовые magic link сжигаются превью Telegram).
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

#: Варианты «Повтор после лекарства».
REMEASURE_VARIANTS = {
    texts.REMEASURE_OFF: "off",
    "30 минут": "30",
    "60 минут": "60",
    "120 минут": "120",
}

#: Слоты и предлагаемое время (SPEC.md, раздел 6.1).
SLOT_ORDER = ("morning", "noon", "evening")

#: Слоты приёма лекарств: слоты давления + препарат на случай кризиса.
#: Кризис — только для лекарств (напоминания и онбординг живут в SLOT_ORDER).
MED_SLOT_ORDER = ("morning", "noon", "evening", "crisis")
SLOT_TIME_PRESETS = {
    "morning": ("07:00", "08:00", "09:00"),
    "noon": ("12:00", "13:00", "14:00"),
    "evening": ("20:00", "21:00", "22:00"),
}

# ─────────────────────────── Главное меню ───────────────────────────


def main_menu(is_doctor: bool = False) -> InlineKeyboardMarkup:
    """Главное меню (SPEC.md, раздел 6.2; «Отправить отчет» — §21).

    `is_doctor` — пользователь ещё и врач: внизу кнопка «🩺 Кабинет врача»
    (меню врача, §21, N5).
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="menu:measure"))
    builder.row(InlineKeyboardButton(text=texts.BTN_DIARY, callback_data="menu:diary"))
    builder.row(InlineKeyboardButton(text=texts.BTN_APPOINTMENTS, callback_data="ap:list"))
    builder.row(InlineKeyboardButton(text=texts.BTN_STATS, callback_data="menu:stats"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SETTINGS, callback_data="menu:settings"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CABINET, callback_data="menu:cabinet"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SEND_REPORT, callback_data="menu:report"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ABOUT, callback_data="menu:about"))
    if is_doctor:
        builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_PANEL, callback_data="doc:menu"))
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    """Единственная кнопка «В меню»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Запись давления ───────────────────────────


def keypad_kb(buf: str, *, allow_skip: bool) -> InlineKeyboardMarkup:
    """Клавиатура-калькулятор ввода значения (SPEC.md, раздел 6.3).

    Цифры 0…9, «C» (`m:key:clear`) — очистить, «⌫» (`m:key:bksp`) — стереть
    последнюю цифру, «Готово» (`m:key:ok`) — подтвердить, «Пропустить»
    (`m:key:skip`) — только для пульса (`allow_skip=True`), «Отмена» — в меню.

    `buf` — текущий буфер набора; на разметку не влияет (значение показывает
    `texts.fmt_keypad_prompt`), передаётся для единообразия с текстом экрана.
    """
    builder = InlineKeyboardBuilder()
    # Раскладка как у калькулятора: 7-8-9 сверху, 1-2-3 снизу, «C 0 ⌫» последним рядом.
    for digits in ((7, 8, 9), (4, 5, 6), (1, 2, 3)):
        builder.row(
            *[
                InlineKeyboardButton(text=str(d), callback_data=f"m:key:{d}")
                for d in digits
            ]
        )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_KEYPAD_CLEAR, callback_data="m:key:clear"),
        InlineKeyboardButton(text="0", callback_data="m:key:0"),
        InlineKeyboardButton(text=texts.BTN_KEYPAD_BACKSPACE, callback_data="m:key:bksp"),
    )
    if allow_skip:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_DONE, callback_data="m:key:ok"),
            InlineKeyboardButton(text=texts.BTN_SKIP, callback_data="m:key:skip"),
        )
    else:
        builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data="m:key:ok"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def intake_meds_kb(
    checked: set[int],
    medications: Sequence[Medication],
    taken_today: Collection[int] = frozenset(),
) -> InlineKeyboardMarkup:
    """Мультивыбор принятых лекарств (SPEC.md, раздел 6.3).

    Строка-чекбокс на приём («✓ Лозартан 50 мг · утро», § fmt_intake_item):
    «✓ » — только у отмеченных в этом выборе; у принятых за сегодня
    (`taken_today`) — пометка «· сегодня ✓» в конце подписи, без галочки.
    `m:med:pick:{id}` переключает отметку, «Готово» (`m:med:done`) сохраняет
    журнал приёмов, «Отмена» (`m:med:cancel`) возвращается к сводке.
    """
    builder = InlineKeyboardBuilder()
    for med in medications:
        mark = "✓ " if med.id in checked else ""
        today = texts.INTAKE_TAKEN_TODAY_SUFFIX if med.id in taken_today else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{texts.fmt_intake_item(med.name, med.dosage, med.slot)}{today}",
                callback_data=f"m:med:pick:{med.id}",
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data="m:med:done"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:med:cancel"))
    return builder.as_markup()


def saved_kb() -> InlineKeyboardMarkup:
    """Кнопки после сохранения измерения: правка последней записи + опциональные шаги."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_EDIT, callback_data="m:edit"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ADD_NOTE, callback_data="m:note"))
    builder.row(InlineKeyboardButton(text=texts.BTN_TOOK_MED, callback_data="m:med"))
    builder.row(InlineKeyboardButton(text=texts.BTN_FEELING, callback_data="m:feeling"))
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


def medication_taken_kb() -> InlineKeyboardMarkup:
    """«Приняли лекарство?» → Да / Нет (TRUE / FALSE; NULL — не отмечал)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_MED_YES, callback_data="m:med:yes"),
        InlineKeyboardButton(text=texts.BTN_MED_NO, callback_data="m:med:no"),
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


# ─────────────────── Архивные записи (SPEC.md, §6.10) ───────────────────


#: Пресеты времени шага «Во сколько?» архива (§6.10).
ARCHIVE_TIME_PRESETS = ("08:00", "13:00", "21:00")

#: Относительные даты шага «Когда?»: callback-суффикс → дней назад (§6.10).
ARCHIVE_DATE_PRESETS = {"today": 0, "yesterday": 1, "3d": 3, "week": 7}


def archive_menu_kb() -> InlineKeyboardMarkup:
    """Экран «Архивные записи»: по одной / быстрый ввод / назад (§6.10)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_ARCHIVE_ADD, callback_data="set:archive:add")
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_QUICK, callback_data="set:archive:quick"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def archive_date_kb() -> InlineKeyboardMarkup:
    """Шаг «Когда?»: сегодня / вчера / 3 дня / неделя / другая дата (§6.10)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_TODAY, callback_data="set:archive:date:today"
        ),
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_YESTERDAY, callback_data="set:archive:date:yesterday"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_3D, callback_data="set:archive:date:3d"
        ),
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_WEEK, callback_data="set:archive:date:week"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_OTHER_DATE, callback_data="set:archive:date:other"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def archive_time_kb() -> InlineKeyboardMarkup:
    """Шаг «Во сколько?»: 08:00 / 13:00 / 21:00 / «Другое» (§6.10)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        *[
            InlineKeyboardButton(text=t, callback_data=f"set:archive:time:{t}")
            for t in ARCHIVE_TIME_PRESETS
        ]
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_OTHER, callback_data="set:archive:time:other"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="m:cancel"))
    return builder.as_markup()


def archive_saved_kb() -> InlineKeyboardMarkup:
    """Кнопки после сохранения архивной записи (§6.10), «Ещё запись» вместо «В меню»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_EDIT, callback_data="m:arch:edit"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_ADD_NOTE, callback_data="m:arch:note")
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_TOOK_MED, callback_data="m:arch:med")
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_FEELING, callback_data="m:arch:feel")
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_ARCHIVE_MORE, callback_data="m:arch:more"
        )
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="m:arch:menu")
    )
    return builder.as_markup()


def archive_medication_taken_kb() -> InlineKeyboardMarkup:
    """«Приняли лекарство?» архива: Да / Нет — только исторический флаг (§6.10)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_MED_YES, callback_data="m:arch:med:yes"),
        InlineKeyboardButton(text=texts.BTN_MED_NO, callback_data="m:arch:med:no"),
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="m:arch:menu")
    )
    return builder.as_markup()


def archive_feeling_kb() -> InlineKeyboardMarkup:
    """Самочувствие архива: те же варианты, кнопки `m:arch:feel:*` (§6.10)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_FEEL_NORMAL, callback_data="m:arch:feel:normal"
        ),
        InlineKeyboardButton(
            text=texts.BTN_FEEL_HEADACHE, callback_data="m:arch:feel:headache"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_FEEL_WEAKNESS, callback_data="m:arch:feel:weakness"
        ),
        InlineKeyboardButton(
            text=texts.BTN_FEEL_OTHER, callback_data="m:arch:feel:other"
        ),
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="m:arch:menu")
    )
    return builder.as_markup()


# ─────────────────────────── Дневник ───────────────────────────


def diary_kb(is_empty: bool = False, mark_intake: bool = False) -> InlineKeyboardMarkup:
    """Дневник за сегодня: при пустом списке — «Записать давление»,
    при непустом — «Изменить последнюю» (§6.3); «💊 Отметить приём», если есть
    неотмеченный слот лекарств (§6.4, 0.5.5); всегда «Назад»."""
    builder = InlineKeyboardBuilder()
    if is_empty:
        builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="menu:measure"))
    else:
        builder.row(InlineKeyboardButton(text=texts.BTN_EDIT_LAST, callback_data="m:edit"))
    if mark_intake:
        builder.row(InlineKeyboardButton(text=texts.BTN_MARK_INTAKE, callback_data="d:mt"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


def mark_intake_slots_kb(slots: Sequence[str]) -> InlineKeyboardMarkup:
    """«Какой приём отметить?» — неотмеченные слоты и «Назад» к дневнику (§6.4)."""
    builder = InlineKeyboardBuilder()
    for slot in slots:
        builder.row(
            InlineKeyboardButton(text=texts.SLOT_LABELS[slot], callback_data=f"d:mt:{slot}")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:diary"))
    return builder.as_markup()


def intake_done_kb() -> InlineKeyboardMarkup:
    """После отметки в напоминании: «Записать давление» / «В меню» (§6.7, 0.5.5, U1)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data="rem:write"))
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Статистика ───────────────────────────


def stats_period_kb() -> InlineKeyboardMarkup:
    """Выбор периода статистики: 7 / 30 дней / свой период (§6.5)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_7_DAYS, callback_data="stats:7"),
        InlineKeyboardButton(text=texts.BTN_30_DAYS, callback_data="stats:30"),
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CUSTOM_PERIOD, callback_data="stats:cal"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


def stats_result_kb(period: str) -> InlineKeyboardMarkup:
    """Под сводкой: «По дням», экспорт CSV, экспорт PDF и «Назад».

    `period` — `7` / `30` или `ГГГГММДД:ГГГГММДД` (свой период, §8).
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_BY_DAYS, callback_data=f"stats:days:{period}")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_CSV, callback_data="stats:csv"))
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_PDF, callback_data="stats:pdf"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="stats:periods"))
    return builder.as_markup()


def stats_summary_callback(period: str) -> str:
    """Callback сводки периода: `stats:7` / `stats:30` / `stats:c:{с}:{по}`."""
    return f"stats:{period}" if period in ("7", "30") else f"stats:c:{period}"


def stats_days_kb(period: str) -> InlineKeyboardMarkup:
    """Под списком «По дням»: «Назад» — к фото сводки того же периода."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_BACK, callback_data=stats_summary_callback(period)
        )
    )
    return builder.as_markup()


def _noop(text: str = " ") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="stats:noop")


def stats_calendar_kb(
    year: int, month: int, today: date, start: Optional[date] = None
) -> InlineKeyboardMarkup:
    """Календарь своего периода (§6.5): шаг 1 — `start` не задан, шаг 2 — задан.

    Неделя — с понедельника; клетки вне месяца — пустые заглушки; будущие дни
    кликабельны (проверка — на сервере); «▶» нет в текущем месяце; выбранный
    первый день — «[N]».
    """
    suffix = f":{start:%Y%m%d}" if start is not None else ""
    builder = InlineKeyboardBuilder()
    builder.row(_noop(f"{texts.STATS_CAL_MONTHS[month - 1]} {year}"))
    builder.row(*(_noop(name) for name in texts.STATS_CAL_WEEKDAYS))
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(_noop())
                continue
            current = date(year, month, day)
            label = f"[{day}]" if current == start else str(day)
            data = (
                f"stats:day:{start:%Y%m%d}:{current:%Y%m%d}"
                if start is not None
                else f"stats:day:{current:%Y%m%d}"
            )
            row.append(InlineKeyboardButton(text=label, callback_data=data))
        builder.row(*row)
    nav = []
    if (year, month) > (MINYEAR, 1):
        prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
        nav.append(
            InlineKeyboardButton(
                text="◀", callback_data=f"stats:cal:{prev_year:04d}{prev_month:02d}{suffix}"
            )
        )
    if (year, month) < (today.year, today.month):
        next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
        nav.append(
            InlineKeyboardButton(
                text="▶", callback_data=f"stats:cal:{next_year:04d}{next_month:02d}{suffix}"
            )
        )
    builder.row(*nav)  # хотя бы одна стрелка есть всегда: «◀» или «▶»
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


def pdf_period_kb() -> InlineKeyboardMarkup:
    """Период экспорта PDF: 7 дней / 30 дней / всё время (§10.1)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_7_DAYS, callback_data="pdf:7"))
    builder.row(InlineKeyboardButton(text=texts.BTN_30_DAYS, callback_data="pdf:30"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ALL_TIME, callback_data="pdf:all"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────────────── Настройки ───────────────────────────


def settings_kb() -> InlineKeyboardMarkup:
    """Меню настроек (SPEC.md, раздел 6.6; удаление аккаунта — §19, врач — §21)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_TARGETS, callback_data="set:targets"))
    builder.row(InlineKeyboardButton(text=texts.BTN_REMINDERS, callback_data="set:reminders"))
    builder.row(InlineKeyboardButton(text=texts.BTN_MY_MEDS, callback_data="set:meds"))
    builder.row(InlineKeyboardButton(text=texts.BTN_MY_DOCTOR, callback_data="set:doctor"))
    builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE, callback_data="set:profile"))
    builder.row(InlineKeyboardButton(text=texts.BTN_TIMEZONE, callback_data="set:tz"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_ARCHIVE, callback_data="set:archive")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_CSV, callback_data="set:csv"))
    builder.row(InlineKeyboardButton(text=texts.BTN_EXPORT_PDF, callback_data="set:pdf"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_CONSENT_SETTINGS, callback_data="set:consent")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_DELETE_ACCOUNT, callback_data="set:delete"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


def doctor_kb(links: Sequence[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """Экран «Мой врач»: отзыв доступа по связям и «Назад» (§21).

    `links` — кортежи (id врача, подпись врача, статус); ожидающие и открытые
    связи получают кнопку отзыва. Врач у пациента один: пока связь есть,
    добавить вторую нельзя (отзыв → снова выбор способа приглашения).
    """
    builder = InlineKeyboardBuilder()
    for doctor_id, label, status in links:
        if status == "revoked":
            continue
        builder.row(
            InlineKeyboardButton(
                text=f"{texts.BTN_REVOKE_DOCTOR}: {label}",
                callback_data=f"set:doctor:revoke:{doctor_id}",
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def doctor_choice_kb() -> InlineKeyboardMarkup:
    """«Мой врач» без врача: «По e-mail» / «Через Telegram» / «Назад» (§21, п.2)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_BY_EMAIL, callback_data="set:doctor:email"))
    builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_BY_TELEGRAM, callback_data="set:doctor:tg"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def doctor_tg_invite_kb() -> InlineKeyboardMarkup:
    """Под ссылкой-приглашением: «🔎 Найти по @username» и «Назад» (§21, п.4)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_FIND, callback_data="set:doctor:tg:find"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:doctor"))
    return builder.as_markup()


def doctor_invite_kb(code: str) -> InlineKeyboardMarkup:
    """Приглашение у врача: «✅ Принять» / «Отклонить» (§21, п.5)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_ACCEPT, callback_data=f"doc:accept:{code}"),
        InlineKeyboardButton(text=texts.BTN_DECLINE, callback_data=f"doc:decline:{code}"),
    )
    return builder.as_markup()


def doctor_panel_kb(show_diary: bool) -> InlineKeyboardMarkup:
    """Меню врача (§21): кабинет, пациенты, свой дневник (если онбординг не пройден)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_CABINET, callback_data="doc:cabinet"))
    builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_PATIENTS, callback_data="doc:patients"))
    if show_diary:
        builder.row(InlineKeyboardButton(text=texts.BTN_DOCTOR_DIARY, callback_data="doc:diary"))
    return builder.as_markup()


def doctor_patients_kb(patients: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """«Мои пациенты»: кнопка пациента → «Открыть в кабинете», и «Назад».

    `patients` — пары (users.id, подпись: «Иванов И. И., 67 лет», имя из
    Telegram или «Пациент {telegram_id}», §6.12); в callback — users.id,
    доступ проверяется при нажатии.
    """
    builder = InlineKeyboardBuilder()
    for user_id, label in patients:
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"doc:open:{user_id}")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="doc:menu"))
    return builder.as_markup()


def doctor_registered_kb() -> InlineKeyboardMarkup:
    """`/iamdoctor` для зарегистрированного врача: «Изменить» / «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_CHANGE, callback_data="doc:name"),
        InlineKeyboardButton(text=texts.BTN_BACK, callback_data="doc:menu"),
    )
    return builder.as_markup()


def doctor_back_kb() -> InlineKeyboardMarkup:
    """Единственная кнопка «Назад» в меню врача (`doc:menu`)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="doc:menu"))
    return builder.as_markup()


def doctor_open_kb(user_id: int) -> InlineKeyboardMarkup:
    """Уведомление врачу об отчёте: «Открыть в кабинете» (§21, N6)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_OPEN_IN_CABINET, callback_data=f"doc:open:{user_id}")
    )
    return builder.as_markup()


def delete_account_kb() -> InlineKeyboardMarkup:
    """Подтверждение удаления аккаунта: «Да, удалить» / «Отмена» (§19)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_CONFIRM_DELETE_ACCOUNT, callback_data="set:delete:ok"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="set:back"))
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
    slot_modes: Optional[Mapping[str, str]] = None,
    followup_enabled: bool = True,
    paused_until: Optional[date] = None,
) -> InlineKeyboardMarkup:
    """Настройка напоминаний: пауза, слоты вкл/выкл, время, режим слота,
    повтор после лекарства, «Повторять, если не ответил» (§6.6, 0.5.6)."""
    builder = InlineKeyboardBuilder()
    pause_label = (
        texts.PAUSE_ROW_ON.format(date=paused_until.strftime("%d.%m"))
        if paused_until is not None
        else texts.PAUSE_ROW_OFF
    )
    builder.row(InlineKeyboardButton(text=pause_label, callback_data="set:rem:pause"))
    modes = slot_modes or {}
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
            builder.row(
                InlineKeyboardButton(
                    text=texts.MODE_LABELS[modes.get(slot, "both")],
                    callback_data=f"set:rem:mode:{slot}",
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
    builder.row(
        InlineKeyboardButton(
            text=texts.FOLLOWUP_ROW.format(
                state=texts.FOLLOWUP_ON if followup_enabled else texts.FOLLOWUP_OFF
            ),
            callback_data=f"set:rem:followup:{'off' if followup_enabled else 'on'}",
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def slot_mode_kb(slot: str, current: str, has_medications: bool) -> InlineKeyboardMarkup:
    """«О чём напоминать: {слот}?» — три режима (✓ у текущего) и «Назад» (§6.7, 0.5.6).

    Без лекарств в слоте «Только лекарства» не предлагается.
    """
    builder = InlineKeyboardBuilder()
    for mode, label in texts.MODE_LABELS.items():
        if mode == "meds" and not has_medications:
            continue
        mark = PARTIAL_MARK if mode == current else ""
        builder.row(
            InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set:rem:mode:{slot}:{mode}")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:reminders"))
    return builder.as_markup()


def pause_kb(paused: bool) -> InlineKeyboardMarkup:
    """Пауза напоминаний: 1 / 3 / 7 дней, на паузе — «▶️ Снять паузу», «Назад» (§6.7)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        *(
            InlineKeyboardButton(text=label, callback_data=f"set:rem:pause:{days}")
            for days, label in texts.PAUSE_OPTIONS.items()
        )
    )
    if paused:
        builder.row(InlineKeyboardButton(text=texts.BTN_PAUSE_OFF, callback_data="set:rem:pause:off"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:reminders"))
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


def medications_list_kb(has_medications: bool = False) -> InlineKeyboardMarkup:
    """Список лекарств: «Добавить», «Изменить», «Удалить» и «Назад» (§6.6).

    У пустого списка (`has_medications=False`) — только «Добавить» и «Назад»:
    изменять и удалять нечего.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_ADD, callback_data="med:add"))
    if has_medications:
        builder.row(InlineKeyboardButton(text=texts.BTN_EDIT, callback_data="med:edit"))
        builder.row(InlineKeyboardButton(text=texts.BTN_DELETE, callback_data="med:del"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def medications_pick_kb(
    action: str, medications: Sequence[tuple[int, str, str]]
) -> InlineKeyboardMarkup:
    """Выбор лекарства для действия `action` (`med:edit` / `med:del`) (§6.6).

    По кнопке на лекарство (группу строк по названию, §4.4); значение
    callback_data — `id` любой строки группы. Внизу — «Назад» к списку.
    """
    builder = InlineKeyboardBuilder()
    for med_id, name, dosage in medications:
        builder.row(
            InlineKeyboardButton(
                text=texts.fmt_med_item(name, dosage),
                callback_data=f"{action}:{med_id}",
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:meds"))
    return builder.as_markup()


def med_name_input_kb() -> InlineKeyboardMarkup:
    """Шаг названия при правке: «Оставить как есть» / «Отмена» (§6.6)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_KEEP_AS_IS, callback_data="med:name:same")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="med:cancel"))
    return builder.as_markup()


def dosage_kb(*, keep: bool = False) -> InlineKeyboardMarkup:
    """Дозировка: 5/10/20/50/100 мг + «Без дозировки» / «Другое» (число + мг/мл/таб).

    При правке (`keep=True`) добавлен ряд «Оставить как есть» — текущая
    дозировка уже в черновике (§6.6).
    """
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
    builder.row(
        InlineKeyboardButton(text=texts.BTN_NO_DOSAGE, callback_data="med:dose:none"),
        InlineKeyboardButton(text=texts.BTN_OTHER, callback_data="med:dose:other"),
    )
    if keep:
        builder.row(
            InlineKeyboardButton(
                text=texts.BTN_KEEP_AS_IS, callback_data="med:dose:same"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="med:cancel"))
    return builder.as_markup()


def slots_multiselect_kb(
    selected: Iterable[str], done_callback: str = "med:slots:done", toggle_prefix: str = "med:slot"
) -> InlineKeyboardMarkup:
    """Мультивыбор слотов приёма: ✓ у отмеченных + «Готово».

    Слоты — `MED_SLOT_ORDER` (включая «Кризис»): это клавиатура лекарств.
    """
    chosen = set(selected)
    builder = InlineKeyboardBuilder()
    for slot in MED_SLOT_ORDER:
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


#: Префикс отметки лекарства в списке «Частично…»: по нему читаются отметки (§6.7).
PARTIAL_MARK = "✓ "


def reminder_day_key(day: date) -> str:
    """Дата напоминания в callback_data: `ГГГГММДД` (§8, 0.5.4)."""
    return day.strftime("%Y%m%d")


def slot_message_kb(
    slot: str, day: date, has_medications: bool = True, mode: str = "both"
) -> InlineKeyboardMarkup:
    """Кнопки слот-сообщения (SPEC.md, §6.7, 0.5.4–0.5.6).

    Каждая кнопка несёт слот и дату напоминания (`{д}`): приём пишется за
    этот день, строка слота находится на сервере по `(users.id, type)`;
    нажатие любой — ответ для повтора «если не ответил» (0.5.6).
    Без лекарств в слоте кнопки «Принял всё ✓» и «Отметить принятые» не
    показываются; режим `meds` — без «Записать давление», `pressure` — без
    кнопок лекарств.
    """
    if mode == "meds" and not has_medications:
        mode = "both"
    key = f"{slot}:{reminder_day_key(day)}"
    builder = InlineKeyboardBuilder()
    if mode != "meds":
        builder.row(
            InlineKeyboardButton(text=texts.BTN_MEASURE, callback_data=f"rem:write:{key}")
        )
    if has_medications and mode != "pressure":
        builder.row(InlineKeyboardButton(text=texts.BTN_TAKE_ALL, callback_data=f"rem:all:{key}"))
        builder.row(InlineKeyboardButton(text=texts.BTN_PARTIAL, callback_data=f"rem:partial:{key}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SNOOZE_30, callback_data=f"rem:snooze:{key}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_SKIP_SLOT, callback_data=f"rem:skip:{key}"))
    return builder.as_markup()


def partial_meds_kb(
    slot: str,
    day: date,
    checked: Iterable[int],
    medications: Sequence[tuple[int, str, str]],
    area: str = "rem",
) -> InlineKeyboardMarkup:
    """Список «Отметить принятые» без FSM: отметки — «✓ » в подписях кнопок (§6.7).

    `medications` — кортежи (id, название, дозировка); `checked` — id отмеченных.
    `area` — `rem` (напоминание) или `d` (дневник, §6.4: «Готово» и «Отмена»
    возвращают «Дневник за сегодня»).
    """
    key = f"{slot}:{reminder_day_key(day)}"
    chosen = set(checked)
    builder = InlineKeyboardBuilder()
    for med_id, name, dosage in medications:
        mark = PARTIAL_MARK if med_id in chosen else ""
        label = f"{name} {dosage}".strip()
        builder.row(
            InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"{area}:pt:{key}:{med_id}")
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_DONE, callback_data=f"{area}:pd:{key}"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=f"{area}:pc:{key}"))
    return builder.as_markup()


def partial_checked_ids(markup: Optional[InlineKeyboardMarkup]) -> set[int]:
    """Id отмеченных лекарств из клавиатуры списка «Частично…» (кнопки с «✓ »)."""
    checked: set[int] = set()
    for row in markup.inline_keyboard if markup is not None else []:
        for button in row:
            data = button.callback_data or ""
            if data.startswith(("rem:pt:", "d:pt:")) and button.text.startswith(PARTIAL_MARK):
                value = data.rsplit(":", 1)[1]
                if value.isdigit():
                    checked.add(int(value))
    return checked


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
    group_url: str = "",
    back_callback: str = "menu:back",
) -> InlineKeyboardMarkup:
    """Экран «О боте»: «Поддержать проект», «Написать автору», «Наше сообщество».

    Кнопка сообщества — URL-кнопка на `group_url` (группа в Telegram); пустая
    строка — кнопки нет (§6.8: переменная `GROUP_URL` пустая или плейсхолдер).
    """
    builder = InlineKeyboardBuilder()
    if support_visible:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_SUPPORT_PROJECT, callback_data="about:support")
        )
    if author_visible:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_WRITE_AUTHOR, callback_data="about:author")
        )
    if group_url:
        builder.row(InlineKeyboardButton(text=texts.BTN_OPEN_GROUP, url=group_url))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data=back_callback))
    return builder.as_markup()


def support_kb(url: str) -> InlineKeyboardMarkup:
    """Экран «Поддержать»: одна URL-кнопка (реквизитов в боте нет)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_OPEN_SUPPORT, url=url))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu:back"))
    return builder.as_markup()


# ─────────────────── Административная панель (SPEC.md, §18) ───────────────────


def admin_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню админки (§18.1): пять разделов + «В меню»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_ADMIN_STATS, callback_data="adm:stats"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ADMIN_BACKUPS, callback_data="adm:backups"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ADMIN_HEALTH, callback_data="adm:health"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ADMIN_BROADCAST, callback_data="adm:bcast"))
    builder.row(InlineKeyboardButton(text=texts.BTN_ADMIN_SEARCH, callback_data="adm:search"))
    builder.row(InlineKeyboardButton(text=texts.BTN_IN_MENU, callback_data="menu:back"))
    return builder.as_markup()


def admin_stats_kb() -> InlineKeyboardMarkup:
    """Статистика БД: «Обновить» (кэш 5 минут сбрасывается) и «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_REFRESH, callback_data="adm:refresh"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="adm:back"))
    return builder.as_markup()


def admin_health_kb() -> InlineKeyboardMarkup:
    """Здоровье системы: «Обновить» (перечитать экран) и «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_REFRESH, callback_data="adm:health"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="adm:back"))
    return builder.as_markup()


def admin_backups_kb() -> InlineKeyboardMarkup:
    """Бэкапы: создать сейчас / скачать последний / назад (§18.3)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_BACKUP_CREATE, callback_data="adm:backup:create")
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_BACKUP_DOWNLOAD, callback_data="adm:backup:download"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="adm:back"))
    return builder.as_markup()


def admin_broadcast_preview_kb() -> InlineKeyboardMarkup:
    """Превью рассылки: «Подтвердить отправку» / «Отмена» (§18.5)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_BCAST_CONFIRM, callback_data="adm:bcast:confirm")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="adm:bcast:cancel"))
    return builder.as_markup()


def admin_search_card_kb(telegram_id: int) -> InlineKeyboardMarkup:
    """Карточка пользователя: «Отправить тестовое сообщение» (§18.6)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_SEND_TEST, callback_data=f"adm:test:{telegram_id}"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="adm:back"))
    return builder.as_markup()


# ─────────────── Согласие на обработку данных (SPEC.md, §6.11) ───────────────


def consent_kb(privacy_url: str, existing: bool) -> InlineKeyboardMarkup:
    """Б1: «Согласен» / «Не согласен» / «Прочитать полностью»; существующему —
    ещё «Удалить аккаунт»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_CONSENT_YES, callback_data="cons:yes"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CONSENT_NO, callback_data="cons:no"))
    builder.row(
        InlineKeyboardButton(text=texts.BTN_CONSENT_READ, url=f"{privacy_url}#consent")
    )
    if existing:
        builder.row(
            InlineKeyboardButton(text=texts.BTN_DELETE_ACCOUNT, callback_data="set:delete")
        )
    return builder.as_markup()


def consent_declined_kb() -> InlineKeyboardMarkup:
    """Б3 существующему: «Удалить аккаунт» / «Назад к согласию»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_DELETE_ACCOUNT, callback_data="set:delete"))
    builder.row(InlineKeyboardButton(text=texts.BTN_CONSENT_BACK, callback_data="cons:back"))
    return builder.as_markup()


def consent_settings_kb() -> InlineKeyboardMarkup:
    """Б6: «Отозвать согласие» / «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_CONSENT_WITHDRAW, callback_data="set:consent:withdraw")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def consent_withdraw_kb() -> InlineKeyboardMarkup:
    """Подтверждение отзыва: «Да, отозвать и удалить» / «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_CONSENT_WITHDRAW_OK, callback_data="set:consent:withdraw:ok"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="set:consent"))
    return builder.as_markup()


def doctor_consent_kb() -> InlineKeyboardMarkup:
    """Б7: «Согласен, пригласить» / «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_DOCTOR_CONSENT_YES, callback_data="set:doctor:agree")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def doctor_self_consent_kb(code: Optional[str] = None) -> InlineKeyboardMarkup:
    """Б8: «Согласен» / «Отмена»; при приглашении код едет в callback'е."""
    yes = f"doc:consent:yes:{code}" if code else "doc:consent:yes"
    no = f"doc:consent:no:{code}" if code else "doc:consent:no"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_CONSENT_YES, callback_data=yes))
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=no))
    return builder.as_markup()


def confirm_doctor_kb(doctor_id: int, pending: bool) -> InlineKeyboardMarkup:
    """Д4: оставить доступ (приглашение) врачу или закрыть (отменить)."""
    keep = texts.BTN_KEEP_INVITE if pending else texts.BTN_KEEP_ACCESS
    close = texts.BTN_CANCEL_INVITE if pending else texts.BTN_CLOSE_ACCESS
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=keep, callback_data=f"cons:doc:keep:{doctor_id}"))
    builder.row(InlineKeyboardButton(text=close, callback_data=f"cons:doc:close:{doctor_id}"))
    return builder.as_markup()



# ─────────────── Данные пациента (SPEC.md, §6.12) ───────────────


def profile_kb(filled: bool) -> InlineKeyboardMarkup:
    """П1 «Заполнить» / П2 «Изменить» + «Удалить данные»; «Назад» — в настройки."""
    builder = InlineKeyboardBuilder()
    if filled:
        builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE_EDIT, callback_data="prof:edit"))
        builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE_DELETE, callback_data="prof:del"))
    else:
        builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE_FILL, callback_data="prof:edit"))
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


def profile_middle_kb() -> InlineKeyboardMarkup:
    """Шаг отчества: «Нет отчества» и «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_PROFILE_NO_MIDDLE, callback_data="prof:nomiddle")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="prof:show"))
    return builder.as_markup()


def profile_sex_kb() -> InlineKeyboardMarkup:
    """Шаг пола: «Мужской» / «Женский» и «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_SEX_MALE, callback_data="prof:sex:m"),
        InlineKeyboardButton(text=texts.BTN_SEX_FEMALE, callback_data="prof:sex:f"),
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="prof:show"))
    return builder.as_markup()


def profile_delete_kb() -> InlineKeyboardMarkup:
    """Подтверждение удаления данных: «Да, удалить» / «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_PROFILE_DELETE_OK, callback_data="prof:del:ok")
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="prof:show"))
    return builder.as_markup()


def profile_prompt_kb() -> InlineKeyboardMarkup:
    """П7: «Заполнить» / «Позже»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE_FILL, callback_data="prof:edit"))
    builder.row(InlineKeyboardButton(text=texts.BTN_PROFILE_LATER, callback_data="prof:later"))
    return builder.as_markup()


# ─────────────────── Визиты и анализы (SPEC.md, §6.13, 0.5.8) ───────────────────


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def ap_list_kb(upcoming: Sequence[tuple[int, str]], has_past: bool) -> InlineKeyboardMarkup:
    """Список: кнопка на событие «Скоро», «➕ Добавить», «🗂 Прошедшие», «В меню»."""
    builder = InlineKeyboardBuilder()
    for appointment_id, label in upcoming:
        builder.row(_btn(label, f"ap:open:{appointment_id}"))
    builder.row(_btn(texts.BTN_AP_ADD, "ap:add"))
    if has_past:
        builder.row(_btn(texts.BTN_AP_PAST, "ap:past"))
    builder.row(_btn(texts.BTN_IN_MENU, "menu:back"))
    return builder.as_markup()


def ap_past_kb(items: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """«Прошедшие»: кнопка на событие и «Назад» к списку."""
    builder = InlineKeyboardBuilder()
    for appointment_id, label in items:
        builder.row(_btn(label, f"ap:open:{appointment_id}"))
    builder.row(_btn(texts.BTN_BACK, "ap:list"))
    return builder.as_markup()


def ap_kind_kb() -> InlineKeyboardMarkup:
    """Шаг 1: тип события."""
    builder = InlineKeyboardBuilder()
    for kind in texts.AP_KINDS:
        builder.row(_btn(texts.AP_KIND_BUTTONS[kind], f"ap:kind:{kind}"))
    builder.row(_btn(texts.BTN_BACK, "ap:list"))
    return builder.as_markup()


def ap_title_kb(kind: str, back: str = "ap:add") -> InlineKeyboardMarkup:
    """Шаг 2: подсказки по две в строке, «Другой…», «Назад»."""
    builder = InlineKeyboardBuilder()
    buttons = [
        _btn(title, f"ap:title:{kind}:{index}")
        for index, title in enumerate(texts.AP_SUGGESTIONS[kind])
    ]
    for start in range(0, len(buttons), 2):
        builder.row(*buttons[start : start + 2])
    builder.row(_btn(texts.BTN_AP_OTHER_TITLE, "ap:title:other"))
    builder.row(_btn(texts.BTN_BACK, back))
    return builder.as_markup()


def ap_calendar_kb(year: int, month: int, today: date, last: date, back: str) -> InlineKeyboardMarkup:
    """Шаг 3: календарь дат от `today` до `last`, прочие дни — «·» (§6.13)."""
    builder = InlineKeyboardBuilder()
    builder.row(_noop(f"{texts.STATS_CAL_MONTHS[month - 1]} {year}"))
    builder.row(*(_noop(name) for name in texts.STATS_CAL_WEEKDAYS))
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(_noop())
                continue
            current = date(year, month, day)
            if current < today or current > last:
                row.append(_noop("·"))
            else:
                row.append(_btn(str(day), f"ap:day:{current:%Y%m%d}"))
        builder.row(*row)
    nav = []
    if (year, month) > (today.year, today.month):
        prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
        nav.append(_btn("◀", f"ap:cal:{prev_year:04d}{prev_month:02d}"))
    if (year, month) < (last.year, last.month):
        next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
        nav.append(_btn("▶", f"ap:cal:{next_year:04d}{next_month:02d}"))
    builder.row(*nav)  # хотя бы одна стрелка есть всегда: «▶» до месяца сегодня + 2 года
    builder.row(_btn(texts.BTN_BACK, back))
    return builder.as_markup()


def ap_time_kb(back: str) -> InlineKeyboardMarkup:
    """Шаг 4: 07:00…18:00 по 4, «Другое время», «Без времени», «Назад»."""
    builder = InlineKeyboardBuilder()
    hours = [_btn(f"{hour:02d}:00", f"ap:time:{hour:02d}00") for hour in range(7, 19)]
    for start in range(0, len(hours), 4):
        builder.row(*hours[start : start + 4])
    builder.row(_btn(texts.BTN_AP_TIME_OTHER, "ap:time:other"))
    builder.row(_btn(texts.BTN_AP_NO_TIME, "ap:time:none"))
    builder.row(_btn(texts.BTN_BACK, back))
    return builder.as_markup()


def ap_note_kb(back: str, clear_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Шаг 5: «Пропустить» (при правке заполненной — ещё «Убрать заметку»), «Назад»."""
    builder = InlineKeyboardBuilder()
    builder.row(_btn(texts.BTN_AP_SKIP, "ap:note:skip"))
    if clear_id is not None:
        builder.row(_btn(texts.BTN_AP_NOTE_CLEAR, f"ap:edit:{clear_id}:note:clear"))
    builder.row(_btn(texts.BTN_BACK, back))
    return builder.as_markup()


def ap_back_kb() -> InlineKeyboardMarkup:
    """Только «Назад» — под приглашением свободного ввода (название, время)."""
    builder = InlineKeyboardBuilder()
    builder.row(_btn(texts.BTN_BACK, "ap:back"))
    return builder.as_markup()


def ap_done_kb() -> InlineKeyboardMarkup:
    """«📅 К списку» / «В меню»."""
    builder = InlineKeyboardBuilder()
    builder.row(_btn(texts.BTN_AP_LIST, "ap:list"), _btn(texts.BTN_IN_MENU, "menu:back"))
    return builder.as_markup()


def ap_after_kb(appointment_id: int, kind: str) -> InlineKeyboardMarkup:
    """«✓ Был(а)» / «Перенёс(ла)» / «Не был(а)» (у анализов — «Сдал(а)»), §6.13."""
    done, moved, missed = texts.AP_AFTER_BUTTONS[kind]
    builder = InlineKeyboardBuilder()
    builder.row(_btn(done, f"ap:after:{appointment_id}:done"))
    builder.row(
        _btn(moved, f"ap:after:{appointment_id}:moved"),
        _btn(missed, f"ap:after:{appointment_id}:missed"),
    )
    return builder.as_markup()


def ap_notice_kb(appointment_id: int, kind: str, notice: str, with_doctor: bool = False) -> InlineKeyboardMarkup:
    """Кнопки уведомления (§6.13, В9, В12); «Как прошёл визит?» — `ap_after_kb`."""
    if notice == "after":
        return ap_after_kb(appointment_id, kind)
    builder = InlineKeyboardBuilder()
    if kind == "doctor":
        top = [_btn(texts.BTN_AP_REPORT, f"ap:report:{appointment_id}")]
        if with_doctor:
            top.append(_btn(texts.BTN_AP_SEND, f"ap:send:{appointment_id}"))
        builder.row(*top)
    builder.row(_btn(texts.BTN_AP_SEEN, f"ap:seen:{appointment_id}"))
    builder.row(
        _btn(texts.BTN_AP_MOVE, f"ap:move:{appointment_id}"),
        _btn(texts.BTN_AP_CANCEL[kind], f"ap:cancel:{appointment_id}"),
    )
    return builder.as_markup()


def ap_card_kb(appointment_id: int, kind: str, state: str, back: str) -> InlineKeyboardMarkup:
    """Карточка: `upcoming` — отчёт, правка, отмена; `waiting` — ответ; дальше — «Удалить», «Назад»."""
    builder = InlineKeyboardBuilder()
    if state == "upcoming":
        if kind == "doctor":
            builder.row(_btn(texts.BTN_AP_REPORT, f"ap:report:{appointment_id}"))
        builder.row(_btn(texts.BTN_AP_EDIT, f"ap:edit:{appointment_id}"))
        builder.row(_btn(texts.BTN_AP_CANCEL[kind], f"ap:cancel:{appointment_id}"))
    elif state == "waiting":
        for row in ap_after_kb(appointment_id, kind).inline_keyboard:
            builder.row(*row)
    builder.row(_btn(texts.BTN_AP_DELETE, f"ap:del:{appointment_id}"))
    builder.row(_btn(texts.BTN_BACK, back))
    return builder.as_markup()


def ap_edit_kb(appointment_id: int) -> InlineKeyboardMarkup:
    """«Что изменить?»: дата и время / название / заметка (Н4)."""
    builder = InlineKeyboardBuilder()
    builder.row(_btn(texts.BTN_AP_EDIT_WHEN, f"ap:edit:{appointment_id}:when"))
    builder.row(_btn(texts.BTN_AP_EDIT_TITLE, f"ap:edit:{appointment_id}:title"))
    builder.row(_btn(texts.BTN_AP_EDIT_NOTE, f"ap:edit:{appointment_id}:note"))
    builder.row(_btn(texts.BTN_BACK, f"ap:open:{appointment_id}"))
    return builder.as_markup()


def ap_confirm_kb(yes_text: str, yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    """Подтверждение: «Да, …» / «Нет»."""
    builder = InlineKeyboardBuilder()
    builder.row(_btn(yes_text, yes_data), _btn(texts.BTN_AP_NO, no_data))
    return builder.as_markup()


def ap_next_kb(appointment_id: int) -> InlineKeyboardMarkup:
    """«Записать следующий?» — через 1 / 3 / 6 / 12 месяцев, «Не нужно» (В11)."""
    builder = InlineKeyboardBuilder()
    choices = [_btn(label, f"ap:next:{appointment_id}:{months}") for months, label in texts.AP_NEXT_BUTTONS]
    builder.row(*choices[:2])
    builder.row(*choices[2:4])
    builder.row(choices[4])
    return builder.as_markup()
