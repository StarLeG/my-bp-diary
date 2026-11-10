"""Все тексты бота (SPEC.md, раздел 11).

Единственный источник текстов: в хендлерах строки-литералы не разбрасываются.
Тон: по-русски, дружелюбно, коротко; «поддержать» (не «донат»),
«измерение» (не «замер»), «давление» (не «АД»). Без медицинских советов.
"""
from __future__ import annotations

from typing import Optional, Sequence

# ─────────────────────────── Общие ───────────────────────────

ERROR_GENERIC = "Что-то пошло не так, попробуйте ещё раз"
DEBOUNCE_NOTICE = "Уже записываю…"
DUPLICATE_MEASUREMENT = "Это давление уже записано"
MENU_HINT = "Выберите пункт меню"
CSV_TOO_MANY = "Слишком много записей, выберите 7 или 30 дней"
NOT_YOUR_RECORD = "Такой записи нет"

# ─────────────────────────── Кнопки ───────────────────────────

BTN_MEASURE = "Записать давление"
BTN_DIARY = "Дневник за сегодня"
BTN_STATS = "Статистика"
BTN_SETTINGS = "Настройки"
BTN_CABINET = "Личный кабинет"
BTN_SEND_REPORT = "Отправить отчет"
BTN_ABOUT = "О боте / Поддержать"
BTN_BACK = "Назад"
BTN_IN_MENU = "В меню"
BTN_CANCEL = "Отмена"
BTN_SKIP = "Пропустить"
BTN_DONE = "Готово"
BTN_OTHER = "Другое"
BTN_NO_DOSAGE = "Без дозировки"
BTN_KEEP_AS_IS = "Оставить как есть"
BTN_NO_REMINDERS = "Без напоминаний"

BTN_ADD_NOTE = "Добавить заметку"
BTN_TOOK_MED = "Приняли лекарство?"
BTN_FEELING = "Самочувствие"
BTN_EDIT = "✏️ Изменить"
BTN_EDIT_LAST = "✏️ Изменить последнюю"

BTN_MED_YES = "Да"
BTN_MED_NO = "Нет"
BTN_NO_NEED = "Не надо"
BTN_NOT_NOW = "Не сейчас"

BTN_FEEL_NORMAL = "Норма"
BTN_FEEL_HEADACHE = "Головная боль"
BTN_FEEL_WEAKNESS = "Слабость"
BTN_FEEL_OTHER = "Другое"

BTN_7_DAYS = "7 дней"
BTN_30_DAYS = "30 дней"
BTN_ALL_TIME = "всё время"
BTN_EXPORT_CSV = "Экспорт CSV"
BTN_EXPORT_PDF = "Экспорт PDF"
BTN_CUSTOM_PERIOD = "📆 Свой период"
BTN_BY_DAYS = "📅 По дням"

BTN_TARGETS = "Целевые значения"
BTN_REMINDERS = "Напоминания"
BTN_MY_MEDS = "Мои лекарства"
BTN_MY_DOCTOR = "Мой врач"
BTN_TIMEZONE = "Часовой пояс"

BTN_ADD = "Добавить"
BTN_DELETE = "Удалить"

BTN_TAKE_ALL = "Принял всё ✓"
BTN_PARTIAL = "Отметить принятые"  # до 0.5.5 — «Частично…» (U2)
BTN_SNOOZE_30 = "Через 30 мин"
BTN_SKIP_SLOT = "Пропускаю сегодня"  # до 0.5.5 — «Пропускаю» (U2)
BTN_REMEASURE_NOW = "Записать сейчас"
BTN_REMEASURE_OFF = "Больше не напоминать"

BTN_SUPPORT_PROJECT = "Поддержать проект"
BTN_WRITE_AUTHOR = "Написать автору"
BTN_OPEN_GROUP = "📣 Наше сообщество"
BTN_OPEN_SUPPORT = "Перейти на страницу поддержки"
BTN_START = "Начать"

# ─────────────────────────── Онбординг ───────────────────────────

WELCOME = (
    "Привет! Я помогу вести дневник давления: запись — пара нажатий, "
    "статистика — понятная сводка для врача."
)

ONB_TZ_ASK = "Выберите часовой пояс — по нему считаются «сегодня» и время напоминаний."
ONB_TZ_OTHER_PROMPT = (
    "Напишите часовой пояс в формате IANA, например Europe/Minsk или Asia/Yekaterinburg."
)
ONB_TZ_CONFIRM = "Часовой пояс: {tz} (UTC{offset})"

ONB_TARGETS_ASK = "Какие значения считать целью?"
ONB_TARGETS_OTHER_PROMPT = "Введите целевое систолическое (верхнее) число, например 130."
ONB_TARGETS_OTHER_PROMPT_DIA = "Теперь целевое диастолическое (нижнее) число, например 80."
ONB_TARGETS_CONFIRM = "Цель: {target}"

ONB_SLOTS_ASK = "Когда напоминать измерить давление? Можно выбрать несколько."
ONB_TIMES_ASK = "Во сколько напоминать? ({slot})"
ONB_TIME_OTHER_PROMPT = "Введите время в формате ЧЧ:ММ, например 08:30."
ONB_REMEASURE_NOTE = "Повтор измерения после лекарства включён (через 60 минут) — меняется в настройках."

ONB_DONE = "Готово! Всё настроено."
ONB_RESTARTED = "Начнём сначала."

# ─────────────────────────── Главное меню ───────────────────────────

MAIN_MENU = "Главное меню"

# ─────────────────────────── Запись давления ───────────────────────────

ASK_SYSTOLIC = "Систолическое (верхнее)?"
ASK_DIASTOLIC = "Диастолическое (нижнее)?"
ASK_PULSE = "Пульс?"

BTN_KEYPAD_BACKSPACE = "⌫"
BTN_KEYPAD_CLEAR = "C"
KEYPAD_ENTERED = "Введено: {value}"
KEYPAD_ENTERED_EMPTY = "—"
KEYPAD_HINT = "Можно также написать число сообщением."
KEYPAD_EMPTY_INPUT = "Введите число"
DIA_NOT_LOWER = (
    "Диастолическое (нижнее) должно быть ниже систолического (верхнего) — "
    "проверьте цифры."
)

PROMPT_NOTE = "Напишите заметку (до 500 символов)."
PROMPT_FEELING_OTHER = "Коротко опишите самочувствие — сохраню в заметку."

MEASUREMENT_SAVED = "✅ Записано"
MEASUREMENT_UPDATED = "✅ Обновлено"
SUMMARY_SYSTOLIC = "Систолическое: {value}"
SUMMARY_DIASTOLIC = "Диастолическое: {value}"
SUMMARY_PULSE = "Пульс: {value}"
HIGH_PRESSURE_WARNING = "Эти значения стоит обсудить с врачом на ближайшем приёме."
NOTE_SAVED = "Заметка сохранена"

MED_ASK = "Приняли лекарство?"
MEDS_INTAKE_ASK = "Какие лекарства приняли?"
MEDS_INTAKE_DONE = "💊 Принято: {items}"
MEDS_INTAKE_EMPTY = "Отметьте, что приняли"
#: Пометка приёма, уже принятого за сегодня, в мультивыборе — без галочки (§6.3).
INTAKE_TAKEN_TODAY_SUFFIX = " · сегодня ✓"
FEELING_ASK = "Как самочувствие?"
MED_REMEASURE_PLANNED = "Напомню повторить измерение через {delay} минут."
MED_REMEASURE_DISABLED = "Повтор измерения после лекарства выключен — включите в настройках, если нужен."
MED_REMEASURE_CANCELLED = "Хорошо, повторно напоминать не буду."

# ─────────────────── Архивные записи (SPEC.md, §6.10) ───────────────────

BTN_ARCHIVE = "Архивные записи"
ARCHIVE_MENU = (
    "Перенесите прошлые измерения — например, из блокнота.\n"
    "Можно добавить по одной записи или быстро — одной строкой."
)
BTN_ARCHIVE_ADD = "Добавить запись"
BTN_ARCHIVE_QUICK = "Быстрый ввод"
ARCHIVE_DATE_ASK = "Когда было измерение?"
ARCHIVE_DATE_OTHER_PROMPT = "Введите дату в формате ДД.ММ.ГГГГ или ДД.ММ, например 12.03.2025."
ARCHIVE_DATE_FORMAT_HINT = "Нужна дата в формате ДД.ММ.ГГГГ или ДД.ММ (например, 12.03.2025)"
ARCHIVE_TIME_ASK = "Во сколько было измерение?"
ARCHIVE_TIME_OTHER_PROMPT = "Введите время в формате ЧЧ:ММ, например 08:30."
ARCHIVE_FUTURE_ERROR = "Время измерения не может быть в будущем"
ARCHIVE_QUICK_PROMPT = (
    "Введите измерение одной строкой: ДД.ММ ЧЧ:ММ САД/ДАД пульс "
    "(пульс можно не указывать).\nНапример: 12.03 08:30 135/85 72"
)
ARCHIVE_QUICK_FORMAT_HINT = "Не понял формат. Пример: 12.03 08:30 135/85 72"
ARCHIVE_SAVED = "✅ Записано за {moment}"

BTN_ARCHIVE_TODAY = "Сегодня"
BTN_ARCHIVE_YESTERDAY = "Вчера"
BTN_ARCHIVE_3D = "3 дня назад"
BTN_ARCHIVE_WEEK = "Неделю назад"
BTN_ARCHIVE_OTHER_DATE = "Другая дата"
BTN_ARCHIVE_MORE = "➕ Ещё запись"

# ─────────────────────────── Дневник ───────────────────────────

DIARY_EMPTY = "Сегодня записей ещё нет"
DIARY_TOTAL = "Всего записей: {count}"
# Лекарства на сегодня в дневнике (§6.4, 0.5.5, U6)
DIARY_MEDS_PREFIX = "💊 Лекарства: "
DIARY_MEDS_STATUS = {
    "taken": "{slot} ✓",
    "partial": "{slot} — частично",
    "skipped": "{slot} — пропущено",
    "none": "{slot} — ещё не отмечено",
}
BTN_MARK_INTAKE = "💊 Отметить приём"
MARK_INTAKE_ASK = "Какой приём отметить?"
#: Устаревшая или непринятая кнопка (§8, 0.5.5, U8).
STALE_MESSAGE = "Это сообщение устарело"

FEELING_LABELS = {
    "normal": "норма",
    "headache": "головная боль",
    "weakness": "слабость",
    "other": "другое",
}

MED_FLAG_LABELS = {True: "да", False: "нет", None: "пропущено"}


def fmt_measurement_short(systolic: int, diastolic: int, pulse: Optional[int] = None) -> str:
    """«135/85» или «135/85, пульс 72» — краткая запись измерения."""
    result = f"{systolic}/{diastolic}"
    if pulse is not None:
        result += f", пульс {pulse}"
    return result


def _fmt_summary(header: str, systolic: int, diastolic: int, pulse: Optional[int]) -> str:
    """Сводка введённых данных: значения — каждый на своей строке (§6.3)."""
    pulse_str = str(pulse) if pulse is not None else KEYPAD_ENTERED_EMPTY
    return "\n".join(
        [
            header,
            SUMMARY_SYSTOLIC.format(value=systolic),
            SUMMARY_DIASTOLIC.format(value=diastolic),
            SUMMARY_PULSE.format(value=pulse_str),
        ]
    )


def fmt_saved(systolic: int, diastolic: int, pulse: Optional[int] = None) -> str:
    """Сводка после сохранения измерения (SPEC.md, раздел 6.3)."""
    return _fmt_summary(MEASUREMENT_SAVED, systolic, diastolic, pulse)


def fmt_updated(systolic: int, diastolic: int, pulse: Optional[int] = None) -> str:
    """Сводка после обновления записи правки (SPEC.md, раздел 6.3)."""
    return _fmt_summary(MEASUREMENT_UPDATED, systolic, diastolic, pulse)


def fmt_archive_saved(
    moment: str, systolic: int, diastolic: int, pulse: Optional[int] = None
) -> str:
    """Сводка архивной записи: «✅ Записано за 12.03.2025, 08:00» (SPEC.md, §6.10).

    `moment` — уже отформатированный момент измерения («ДД.ММ.ГГГГ, ЧЧ:ММ»).
    """
    return _fmt_summary(ARCHIVE_SAVED.format(moment=moment), systolic, diastolic, pulse)


def fmt_keypad_prompt(question: str, buffer: str) -> str:
    """Экран калькулятора: вопрос, текущий набор и подсказка (SPEC.md, §6.3)."""
    entered = buffer if buffer else KEYPAD_ENTERED_EMPTY
    return f"{question}\n\n{KEYPAD_ENTERED.format(value=entered)}\n{KEYPAD_HINT}"


def fmt_diary_line(
    time_str: str,
    systolic: int,
    diastolic: int,
    pulse: Optional[int],
    medication_taken: Optional[bool],
    feeling: Optional[str],
) -> str:
    """Строка дневника: «08:15 — 135/85, пульс 72 · лекарство: да · норма»."""
    parts = [f"{time_str} — {fmt_measurement_short(systolic, diastolic, pulse)}"]
    if medication_taken is not None:
        parts.append(f"лекарство: {MED_FLAG_LABELS[medication_taken]}")
    if feeling is not None:
        parts.append(FEELING_LABELS.get(feeling, feeling))
    return " · ".join(parts)

# ─────────────────────────── Статистика ───────────────────────────

STATS_HEADER = "📊 За {days} дней ({date_from}–{date_to})"
STATS_COUNT = "Измерений: {count}"
STATS_SYSTOLIC = "Систолическое: в среднем {avg} ({min}…{max})"
STATS_DIASTOLIC = "Диастолическое: в среднем {avg} ({min}…{max})"
STATS_PULSE = "Пульс: в среднем {avg}"
STATS_TREND = "Тренд: {trend}"
STATS_TREND_UP = "растёт"
STATS_TREND_DOWN = "падает"
STATS_TREND_STABLE = "стабильно"
STATS_TREND_FEW = "мало данных для тренда"
STATS_GOAL = "Цель {target} — среднее {verdict}"
STATS_GOAL_OK = "в пределах цели"
STATS_GOAL_ABOVE = "выше цели"
STATS_GOAL_BELOW = "ниже цели"
STATS_ADHERENCE = "Лекарства: принято {taken} из {planned} приёмов за период"
STATS_FOOTER_SUPPORT = "Бот бесплатный. Поддержать → /support"
STATS_EMPTY = "За выбранный период записей нет"

# Свой период и средние по дням (§6.5, 0.5.3)
STATS_HEADER_CUSTOM = "📊 За период {date_from}–{date_to} ({days})"
STATS_CAL_FIRST = "Выберите первый день периода:"
STATS_CAL_LAST = "Период с {date}. Выберите последний день:"
STATS_CAL_FUTURE = "Период не может заканчиваться в будущем"
STATS_CAL_TOO_LONG = "Период — не больше 31 дня"
STATS_CAL_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
STATS_CAL_MONTHS = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
STATS_DAYS_HEADER = "📅 Средние по дням · {date_from}–{date_to}"
STATS_DAYS_LINE = "{icon} {date} — {sys}/{dia}"
STATS_DAYS_PULSE = ", пульс {pulse}"
STATS_DAYS_TOTAL = "Среднее за день: {sys}/{dia}"
#: Кружок зоны дня: 0 — зелёная, 1 — жёлтая, 2 — красная (`chart_zones`).
STATS_ZONE_ICONS = ("🟢", "🟡", "🔴")


def plural_days(count: int) -> str:
    """«1 день», «2 дня», «5 дней», «11 дней», «21 день»."""
    if count % 10 == 1 and count % 100 != 11:
        word = "день"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"{count} {word}"


def fmt_daily_averages(
    date_from: str,
    date_to: str,
    rows: Sequence[tuple[str, int, int, int, Optional[int]]],
    total: tuple[int, int, Optional[int]],
) -> str:
    """Список «По дням» (§6.5): строки дней (дата, зона, САД, ДАД, пульс) и итог."""
    lines = [STATS_DAYS_HEADER.format(date_from=date_from, date_to=date_to)]
    for day, zone, systolic, diastolic, pulse in rows:
        line = STATS_DAYS_LINE.format(
            icon=STATS_ZONE_ICONS[zone], date=day, sys=systolic, dia=diastolic
        )
        if pulse is not None:
            line += STATS_DAYS_PULSE.format(pulse=pulse)
        lines.append(line)
    sys_total, dia_total, pulse_total = total
    line = STATS_DAYS_TOTAL.format(sys=sys_total, dia=dia_total)
    if pulse_total is not None:
        line += STATS_DAYS_PULSE.format(pulse=pulse_total)
    lines.append(line)
    return "\n".join(lines)


def fmt_stats(
    days: int,
    date_from: str,
    date_to: str,
    count: int,
    sys_avg: int,
    sys_min: int,
    sys_max: int,
    dia_avg: int,
    dia_min: int,
    dia_max: int,
    pulse_avg: Optional[int],
    trend: str,
    target: str,
    goal_verdict: str,
    adherence: Optional[str] = None,
    custom: bool = False,
) -> str:
    """Сводка статистики (SPEC.md, раздел 6.5) — блоками, каждый на своей строке.

    `custom` — свой период: заголовок «📊 За период … (N дней)» (0.5.3).
    """
    if custom:
        header = STATS_HEADER_CUSTOM.format(
            date_from=date_from, date_to=date_to, days=plural_days(days)
        )
    else:
        header = STATS_HEADER.format(days=days, date_from=date_from, date_to=date_to)
    lines = [
        header,
        STATS_COUNT.format(count=count),
        STATS_SYSTOLIC.format(avg=sys_avg, min=sys_min, max=sys_max),
        STATS_DIASTOLIC.format(avg=dia_avg, min=dia_min, max=dia_max),
    ]
    if pulse_avg is not None:
        lines.append(STATS_PULSE.format(avg=pulse_avg))
    lines.append(STATS_TREND.format(trend=trend))
    lines.append(STATS_GOAL.format(target=target, verdict=goal_verdict))
    if adherence is not None:
        lines.append(adherence)
    return "\n".join(lines)

# ─────────────────────────── Настройки ───────────────────────────

SETTINGS_MENU = "Настройки"
TARGETS_ASK = "Какие значения считать целью?"
TARGETS_OTHER_PROMPT = "Введите целевое систолическое (верхнее) число, например 130."
TARGETS_OTHER_PROMPT_DIA = "Теперь целевое диастолическое (нижнее) число, например 80."
TARGETS_SAVED = "Цель: {target}"
REMINDERS_MENU = "Напоминания"
SLOT_TIME_ASK = "Во сколько напоминать? ({slot})"
REMEASURE_ASK = "Через сколько минут повторить измерение после лекарства?"
REMEASURE_SAVED = "Повтор измерения: {value}"
REMEASURE_OFF = "выключен"
TZ_ASK = "Выберите часовой пояс"
TZ_OTHER_PROMPT = (
    "Напишите часовой пояс в формате IANA, например Europe/Minsk или Asia/Yekaterinburg."
)
TZ_SAVED = "Часовой пояс: {tz} (UTC{offset})"
CSV_ASK = "За какой период выгрузить?"
CSV_FILE_CAPTION = "Дневник давления за период"

# ─────────────────────────── Экспорт PDF (§10.1) ───────────────────────────

PDF_PREPARING = "Готовлю отчёт…"
PDF_EMPTY = "За этот период записей нет"
PDF_FILE_CAPTION = "Отчёт по давлению за {date_from}–{date_to}"

# ─────────────────────────── Лекарства ───────────────────────────

MEDS_LIST_HEADER = "Мои лекарства"
MEDS_LIST_EMPTY = "Лекарств пока нет. Добавьте — и бот напомнит, какие принимать."
MEDS_ITEM = "• {med} — {slots}"
MEDS_ADD_NAME_PROMPT = "Название лекарства (до 100 символов).\nНапример: Лозартан, Амлодипин"
MEDS_ADD_DOSAGE_PROMPT = "Дозировка?"
MEDS_ADD_DOSAGE_OTHER_PROMPT = "Введите дозировку: число и единицу (мг/мл/таб), например 25 мг"
MEDS_ADD_SLOTS_PROMPT = "Когда принимать? Можно выбрать несколько."
MEDS_ADD_DONE = "Лекарство добавлено. Оно будет появляться в напоминаниях в выбранные слоты"
MEDS_ADD_DONE_CRISIS = (
    "Лекарство добавлено. Напоминаний не будет — выбран только приём «Кризис»"
)
MEDS_ADD_NO_SLOTS = "Выберите хотя бы один приём — утро, обед, вечер или кризис."
MEDS_EDIT_NAME_PROMPT = "Название лекарства (до 100 символов).\nСейчас: {name}"
MEDS_EDIT_DOSAGE_PROMPT = "Дозировка?\nСейчас: {current}"
MEDS_EDIT_SLOTS_PROMPT = "Когда принимать? Можно выбрать несколько.\nСейчас: {slots}"
MEDS_EDIT_DONE = "Лекарство обновлено. Оно будет появляться в напоминаниях в выбранные слоты"
MEDS_EDIT_DONE_CRISIS = (
    "Лекарство обновлено. Напоминаний не будет — выбран только приём «Кризис»"
)
MEDS_EDIT_NAME_TAKEN = "Лекарство с таким названием уже есть — введите другое."
MEDS_EDIT_PICK = "Какое лекарство изменить?"
MEDS_DEL_PICK = "Какое лекарство удалить?"
MEDS_DELETED = "Лекарство удалено"
MEDS_ASK_PROMPT = "Напоминать, какие лекарства принимать?"
MEDS_ASK_DISMISSED = "Хорошо. Добавить лекарства можно позже в настройках."

# ─────────────────────────── Напоминания (слоты) ───────────────────────────

SLOT_LABELS = {"morning": "Утро", "noon": "Обед", "evening": "Вечер", "crisis": "Кризис"}
SLOT_EMOJI = {"morning": "☀️", "noon": "🌤", "evening": "🌙"}
MEDS_LINE_PREFIX = "💊 Лекарства: "

REMEASURE_MESSAGE = "Прошло время после лекарства — пора повторить измерение 🌡"
REMEASURE_OFF_DONE = "Хорошо, больше не буду напоминать."
SLOT_SKIPPED = "Хорошо, напоминание перенёс на завтра."
PARTIAL_PROMPT = "Что приняли? Отметьте и нажмите «Готово»."
MEDS_LOGGED = "Отметил: {summary}"
SLOT_REMAINING = "Осталось напомнить через 30 минут."
#: Устаревшая или мусорная кнопка напоминания — уведомление (§6.7, 0.5.4).
REMINDER_STALE = "Это напоминание устарело"
MEDS_ALREADY = "Эти лекарства уже отмечены сегодня ✓: {meds}"
MEDS_ALREADY_LINE = "Уже отмечено сегодня: {meds}"
HIGH_REMEASURE_HINT = "Повторите измерение — так видно, помогло ли лекарство."


def fmt_slot_message(slot: str, medications: Sequence[str]) -> str:
    """Сообщение слота: «Пора измерить давление ☀️» + строка лекарств (§6.7)."""
    lines = [f"Пора измерить давление {SLOT_EMOJI.get(slot, '⏰')}"]
    if medications:
        lines.append(MEDS_LINE_PREFIX + ", ".join(medications))
    return "\n".join(lines)


def fmt_med_item(name: str, dosage: str) -> str:
    """Лекарство в перечне: «Лозартан 50 мг»; без дозировки — просто «Лозартан»."""
    return f"{name} {dosage}".strip()


def fmt_intake_item(name: str, dosage: str, slot: str) -> str:
    """Приём в мультивыборе «Какие лекарства приняли?»: «Лозартан 50 мг · утро» (§6.3)."""
    return f"{fmt_med_item(name, dosage)} · {SLOT_LABELS.get(slot, slot).lower()}"


def fmt_meds_intake(taken: Sequence[str]) -> str:
    """Итог мультивыбора приёмов: «💊 Принято: Лозартан 50 мг, Амлодипин 5 мг» (§6.3)."""
    return MEDS_INTAKE_DONE.format(items=", ".join(taken))


def fmt_meds_summary(taken: Sequence[str], skipped: Sequence[str]) -> str:
    """Итог отметки приёма: что принято и что пропущено."""
    parts = []
    if taken:
        parts.append("принято: " + ", ".join(taken))
    if skipped:
        parts.append("пропущено: " + ", ".join(skipped))
    return "; ".join(parts) if parts else "нет данных"


def fmt_intake_result(
    taken: Sequence[str], skipped: Sequence[str], already: Sequence[str]
) -> str:
    """Итог отметки из напоминания (§6.7, 0.5.4).

    Ничего нового — «Эти лекарства уже отмечены сегодня ✓: …»; иначе
    «Отметил: …» и, если часть уже была отмечена, строка «Уже отмечено сегодня: …».
    """
    if not taken and not skipped:
        return MEDS_ALREADY.format(meds=", ".join(already))
    text = MEDS_LOGGED.format(summary=fmt_meds_summary(taken, skipped))
    if already:
        text += "\n" + MEDS_ALREADY_LINE.format(meds=", ".join(already))
    return text

# ─────────────────────────── О боте / Поддержать ───────────────────────────

ABOUT_TEXT = (
    "Бота пишет один разработчик в свободное время. Он бесплатный и без рекламы: "
    "все функции доступны всегда.\n\n"
    "Я сделал его, потому что мне самому нужен был простой дневник давления — "
    "записать показание за пару секунд и показать врачу понятную сводку, без лишних экранов.\n\n"
    "Бот только записывает цифры и показывает статистику. "
    "Он не ставит диагнозов и не даёт медицинских советов."
)

SUPPORT_TEXT = (
    "Поддержка добровольная, функции за оплатой не закрыты.\n"
    "Деньги идут на хостинг, развитие бота и тестирование на реальных тонометрах "
    "с разными протоколами."
)

CABINET_LINK_SENT = "Ссылка для входа в личный кабинет (действует 15 минут):\n{url}"
CABINET_DISABLED = "Личный кабинет пока не развёрнут."
CABINET_EMAIL_PROMPT = "Для входа в личный кабинет нужен e-mail. Отправьте его сообщением:"
CABINET_EMAIL_BAD = "Кажется, это не похоже на e-mail. Попробуйте ещё раз:"
CABINET_EMAIL_TAKEN = "Этот e-mail уже занят. Попробуйте другой:"
CABINET_ACCOUNT_CREATED = "Аккаунт создан. Ваша ссылка для входа:\n{url}"
CABINET_REG_CANCELLED = "Создание кабинета отменено."

# ─────────────────── Кабинет врача (SPEC.md, §21) ───────────────────

BTN_REVOKE_DOCTOR = "Отозвать доступ"

DOCTOR_WEB_DISABLED = "Кабинет врача пока не развёрнут."
DOCTOR_MENU = "Мой врач"
#: Экран «Мой врач» без врача — выбор способа приглашения (§21, п.2).
DOCTOR_NO_LINK = "Врач пока не добавлен.\nКак пригласить врача?"
BTN_DOCTOR_BY_EMAIL = "📧 По e-mail"
BTN_DOCTOR_BY_TELEGRAM = "✈️ Через Telegram"
#: Ввод e-mail врача: с почтой — письмо, без почты — только ссылка (§21).
DOCTOR_EMAIL_ASK = "Отправьте e-mail врача сообщением — отправлю ему приглашение."
DOCTOR_EMAIL_ASK_NO_SMTP = "Отправьте e-mail врача сообщением — подготовлю ссылку для него."
DOCTOR_EMAIL_BAD = "Кажется, это не похоже на e-mail. Попробуйте ещё раз:"
#: Сообщение для пересылки врачу — общий хвост текстов приглашения.
_DOCTOR_FORWARD = "Пациент {name} предоставил вам доступ.\n{url}"
DOCTOR_INVITE_SENT = (
    "Приглашение отправлено на {email}.\n"
    "Если письмо не дойдёт, перешлите врачу сообщение ниже:\n\n" + _DOCTOR_FORWARD
)
DOCTOR_INVITE_FAILED = (
    "Не удалось отправить письмо. Перешлите врачу сообщение ниже:\n\n" + _DOCTOR_FORWARD
)
DOCTOR_INVITE_LIMIT = (
    "Письмо уже отправлено недавно. Перешлите врачу сообщение ниже:\n\n" + _DOCTOR_FORWARD
)
DOCTOR_REPORT_EMAILED = "Ссылка отправлена врачу на {email}. Можно также переслать её: {url}"
DOCTOR_REPORT_EMAIL_FAILED = "Не удалось отправить письмо. Перешлите ссылку врачу сами: {url}"
DOCTOR_INVITE_CREATED = (
    "Готово! Перешлите врачу сообщение ниже — по ссылке он получит доступ "
    "к вашим измерениям:\n\nПациент {name} предоставил вам доступ.\n{url}"
)
DOCTOR_REPORT_LINK = "Ссылка для вашего врача сформирована. Перешлите её ему: {url}"
DOCTOR_REPORT_NO_LINK = "Сначала добавьте врача: «Настройки» → «Мой врач»."
DOCTOR_REPORT_REVOKED = (
    "Доступ врача отозван. Добавьте врача заново: «Настройки» → «Мой врач»."
)
DOCTOR_REVOKED = "Доступ отозван."
DOCTOR_ADD_CANCELLED = "Добавление врача отменено."

DOCTOR_LINK_ITEM = "• {label} — {status}"
DOCTOR_STATUS_LABELS = {
    "pending": "ожидает врача",
    "active": "доступ открыт",
    "revoked": "доступ отозван",
}

# ─────────── Приглашение врача через Telegram (SPEC.md, §21) — пациент ───────────

BTN_DOCTOR_FIND = "🔎 Найти по @username"
DOCTOR_TG_INVITE = (
    "Перешлите врачу в Telegram ссылку-приглашение — он нажмёт «Старт» и подтвердит доступ:\n"
    "{invite_url}\n\n"
    "Ссылка действует 72 часа. Если врач уже пользуется ботом, можно найти его по имени пользователя."
)
DOCTOR_USERNAME_ASK = "Напишите имя пользователя врача в Telegram, например @ivanova_doc:"
DOCTOR_USERNAME_BAD = "Имя пользователя — от 5 до 32 латинских букв, цифр и «_». Попробуйте ещё раз:"
DOCTOR_TG_SENT = "Приглашение отправлено врачу в Telegram. Сообщу, когда он ответит."
DOCTOR_TG_NOT_FOUND = "Врач с таким именем не найден. Перешлите ему ссылку-приглашение:\n{invite_url}"
DOCTOR_TG_LIMIT = "Приглашение уже отправлено недавно. Перешлите врачу ссылку-приглашение:\n{invite_url}"
DOCTOR_TG_ACCEPTED = "Врач {doctor} принял приглашение — доступ открыт."
DOCTOR_TG_DECLINED = (
    "Врач не принял приглашение. Можно пригласить другого врача: «Настройки» → «Мой врач»."
)
DOCTOR_REPORT_TELEGRAM = "Отчёт отправлен врачу в Telegram. Можно также переслать ссылку: {url}"

# ─────────── Врач в боте (SPEC.md, §21) ───────────

BTN_ACCEPT = "✅ Принять"
BTN_DECLINE = "Отклонить"
DOCTOR_INVITE_FOR_DOCTOR = (
    "Пациент {name} открыл вам доступ к дневнику давления: измерения, графики, отчёт.\n"
    "Принять приглашение?"
)
DOCTOR_INVITE_INVALID = "Приглашение устарело или уже использовано. Попросите пациента прислать новое."
DOCTOR_INVITE_ACCEPTED = "Доступ открыт. Пациент появится в «Мои пациенты»."
DOCTOR_INVITE_DECLINED = "Приглашение отклонено."
DOCTOR_PANEL = "Меню врача"
BTN_DOCTOR_CABINET = "🩺 Открыть кабинет врача"
BTN_DOCTOR_PATIENTS = "👥 Мои пациенты"
BTN_DOCTOR_DIARY = "Вести свой дневник давления"
#: Кнопка главного меню пациента, который ещё и врач (§21, N5).
BTN_DOCTOR_PANEL = "🩺 Кабинет врача"
DOCTOR_SIGNIN_LINK = "Ссылка для входа в кабинет врача (действует 15 минут): {url}"
DOCTOR_PATIENTS = "Мои пациенты:"
DOCTOR_PATIENT_ITEM = "Пациент {telegram_id}"
DOCTOR_PATIENTS_EMPTY = "Пока нет пациентов с открытым доступом."
DOCTOR_REPORT_FOR_DOCTOR = "Пациент {name} отправил вам отчёт."
BTN_OPEN_IN_CABINET = "Открыть в кабинете"
DOCTOR_ACCESS_REVOKED_FOR_DOCTOR = "Пациент {name} закрыл вам доступ к своему дневнику."
DOCTOR_REGISTER_ASK = "Регистрация врача. Напишите ваше ФИО — его увидят пациенты:"
DOCTOR_REGISTERED = "Вы зарегистрированы как врач: {name}. Изменить ФИО?"
BTN_CHANGE = "Изменить"
DOCTOR_NAME_BAD = "ФИО — от 2 до 100 символов. Попробуйте ещё раз:"
DOCTOR_NAME_SAVED = "Готово! Пациенты могут найти вас по @{username}."
DOCTOR_NAME_SAVED_NO_USERNAME = (
    "Готово! Чтобы пациенты могли найти вас по имени пользователя, задайте его в "
    "настройках Telegram. Пригласить вас можно и ссылкой."
)

# ─────────────────── Административная панель (SPEC.md, §18) ───────────────────

ADMIN_FORBIDDEN = "Доступ запрещен"
ADMIN_MENU = "Административная панель"

BTN_ADMIN_STATS = "📊 Статистика БД"
BTN_ADMIN_BACKUPS = "💾 Бэкапы"
BTN_ADMIN_HEALTH = "🩺 Здоровье системы"
BTN_ADMIN_BROADCAST = "📢 Рассылка"
BTN_ADMIN_SEARCH = "👤 Поиск пользователя"
BTN_REFRESH = "Обновить"
BTN_BACKUP_CREATE = "Создать бэкап сейчас"
BTN_BACKUP_DOWNLOAD = "Скачать последний бэкап"
BTN_BCAST_CONFIRM = "Подтвердить отправку"
BTN_SEND_TEST = "Отправить тестовое сообщение"

# Статистика БД (§18.2) — только агрегаты, без личных данных.
ADMIN_STATS_HEADER = "📊 Статистика БД"
ADMIN_STATS_USERS = "Всего пользователей: {count}"
ADMIN_STATS_DAU = "Активных за 24 часа (DAU): {count}"
ADMIN_STATS_WAU = "Активных за 7 дней (WAU): {count}"
ADMIN_STATS_MEASUREMENTS = "Всего записей давления: {count}"
ADMIN_STATS_MEDICATIONS = "Всего активных лекарств: {count}"
ADMIN_STATS_DB_SIZE = "Размер БД: {size} (WAL: {wal})"
ADMIN_STATS_SLOTS = "Напоминания: утро — {morning}, обед — {noon}, вечер — {evening}"
ADMIN_STATS_CONSENTED = "Дали согласие: {count}"
ADMIN_STATS_CACHE_NOTE = "Данные кэшируются на 5 минут."

# Здоровье системы (§18.4).
ADMIN_HEALTH_HEADER = "🩺 Здоровье системы"
ADMIN_HEALTH_UPTIME = "Аптайм: {uptime}"
ADMIN_HEALTH_VERSION = "Версия: {version} (Python {python})"
ADMIN_HEALTH_SCHEDULER_RUNNING = "Running"
ADMIN_HEALTH_SCHEDULER_STOPPED = "Stopped"
ADMIN_HEALTH_SCHEDULER = "Планировщик: {status}, задач в очереди: {jobs}"
ADMIN_HEALTH_ERRORS = "Ошибок за 24 часа: {count}"

# Рассылка (§18.5).
ADMIN_BCAST_PROMPT = "Введите текст рассылки (с поддержкой Markdown)."
ADMIN_BCAST_EMPTY = "Пустой текст — отправлять нечего."
ADMIN_BCAST_NO_USERS = "Пользователей пока нет."
ADMIN_BCAST_PREVIEW = "Превью сообщения:\n\n{text}"
ADMIN_BCAST_PROGRESS = "Отправлено {done} из {total}..."
ADMIN_BCAST_REPORT = "Рассылка завершена.\nДоставлено: {delivered}, Заблокировали: {blocked}"
ADMIN_BCAST_CANCELLED = "Рассылка отменена."

# Поиск пользователя (§18.6) — без личных медицинских данных.
ADMIN_SEARCH_PROMPT = "Введите telegram_id пользователя."
ADMIN_SEARCH_BAD_INPUT = "Нужен целый telegram_id, например 123456789."
ADMIN_SEARCH_USERNAME_UNSUPPORTED = (
    "Поиск по @username недоступен: хранится только telegram_id."
)
ADMIN_SEARCH_NOT_FOUND = "Пользователь не найден."
ADMIN_SEARCH_CARD = (
    "ID: {telegram_id}\n"
    "Дата регистрации: {created}\n"
    "Часовой пояс: {tz}\n"
    "Записей давления: {count}\n"
    "Онбординг: {onboarded}\n"
    "Напоминания: {reminders}\n"
    "Согласие: {consent}"
)
ADMIN_SEARCH_CONSENT_GIVEN = "дано {date}"
ADMIN_SEARCH_CONSENT_NONE = "согласия нет"
ADMIN_SEARCH_ONBOARDED = "пройден"
ADMIN_SEARCH_NOT_ONBOARDED = "не пройден"
ADMIN_SEARCH_REMINDERS = "включены: {slots}"
ADMIN_SEARCH_REMINDERS_OFF = "выключены"
ADMIN_SEARCH_TEST_SENT = "Тестовое сообщение отправлено."
ADMIN_SEARCH_TEST_FAILED = "Не удалось отправить сообщение."
ADMIN_SEARCH_TEST_MESSAGE = "Тестовое сообщение: проверка доставки уведомлений."

# Бэкапы (§18.3).
ADMIN_BACKUP_HEADER = "💾 Бэкапы"
ADMIN_BACKUP_NONE = "Резервных копий ещё нет."
ADMIN_BACKUP_INTEGRITY_OK = "integrity_check: OK"
ADMIN_BACKUP_INTEGRITY_FAIL = "integrity_check: FAIL"
ADMIN_BACKUP_LAST = (
    "Последний бэкап: {name}\nДата и время: {created}\nРазмер: {size}\n{integrity}"
)
ADMIN_BACKUP_OFFSITE_OK = "Off-site (S3): {url}"
ADMIN_BACKUP_OFFSITE_FAIL = "Off-site (S3): ошибка — {error}"
ADMIN_BACKUP_OFFSITE_NONE = "Off-site (S3): не настроен"
ADMIN_BACKUP_LIST_HEADER = "Локальные копии:"
ADMIN_BACKUP_ITEM = "• {name} — {size}"
ADMIN_BACKUP_CREATING = "Создаю резервную копию…"
ADMIN_BACKUP_DONE = "Бэкап создан: {name}, {size}."
ADMIN_BACKUP_ERROR = "Не удалось создать бэкап: {error}"
ADMIN_BACKUP_TOO_BIG = "Файл слишком большой для отправки в чат ({size}).\nСсылка: {url}"
ADMIN_BACKUP_NO_S3 = (
    "Файл слишком большой для отправки в чат ({size}), "
    "а off-site хранилище не настроено."
)

# ─────────────────── Удаление аккаунта (SPEC.md, §19) ───────────────────

BTN_DELETE_ACCOUNT = "Удалить аккаунт"
DELETE_ACCOUNT_WARNING = (
    "Это действие необратимо...\n"
    "Все записи давления, напоминания и лекарства будут удалены без возможности "
    "восстановления."
)
BTN_CONFIRM_DELETE_ACCOUNT = "Да, удалить"
DELETE_ACCOUNT_DONE = "Ваш аккаунт и все данные удалены"


def fmt_size(size_bytes: int) -> str:
    """Размер файла человекочитаемо: «850 КБ», «12,3 МБ» (§18.3)."""
    if size_bytes < 1024:
        return f"{size_bytes} байт"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} КБ"
    return f"{size_bytes / (1024 * 1024):.1f} МБ".replace(".", ",")


def fmt_uptime(seconds: int) -> str:
    """Аптайм: «2 д 3 ч», «15 мин» (§18.4)."""
    minutes = max(seconds, 0) // 60
    days, rest = divmod(minutes, 60 * 24)
    hours, mins = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if mins or not parts:
        parts.append(f"{mins} мин")
    return " ".join(parts)


def fmt_backup_status(integrity_ok: bool) -> str:
    """Статус верификации копии (§18.3)."""
    return ADMIN_BACKUP_INTEGRITY_OK if integrity_ok else ADMIN_BACKUP_INTEGRITY_FAIL

# ─────────────── Согласие на обработку данных (SPEC.md, §6.11) ───────────────
# Тексты утверждены дословно (doc/PERSONAL-DATA-TEXTS.md, Б1–Б8, Д4).

CONSENT_FIRST_LINE_NEW = "Перед началом — о ваших данных."
CONSENT_FIRST_LINE_EXISTING = "Теперь для работы бота нужно ваше согласие на обработку данных."
CONSENT_BODY = (
    "Бот хранит ваши измерения давления и пульса, лекарства и заметки. "
    "По закону это данные о здоровье, и нужно ваше согласие.\n\n"
    "• Записи видите только вы и врач, которого вы пригласите сами.\n"
    "• Данные хранятся на сервере в Германии, сообщения идут через Telegram.\n"
    "• Отозвать согласие и удалить всё можно в любой момент.\n\n"
    "Бот — для тех, кому исполнилось 16 лет.\n"
    "Полный текст: {privacy_url}#consent"
)
BTN_CONSENT_YES = "✅ Согласен"
BTN_CONSENT_NO = "Не согласен"
BTN_CONSENT_READ = "📄 Прочитать полностью"
BTN_CONSENT_BACK = "Назад к согласию"
CONSENT_DECLINED_NEW = (
    "Без согласия бот не может хранить ваши записи. Мы ничего о вас не сохранили.\n"
    "Если передумаете — нажмите /start."
)
CONSENT_DECLINED_EXISTING = (
    "Без согласия бот не может хранить ваши записи. Удалить аккаунт и все данные?"
)
CONSENT_REQUIRED = "Сначала нужно согласие на обработку данных"
CONSENT_SAVED = "Спасибо! Согласие сохранено."

BTN_CONSENT_SETTINGS = "Согласие на обработку данных"
CONSENT_SETTINGS = (
    "Согласие дано {date}.\n"
    "Полный текст: {privacy_url}#consent\n\n"
    "Если отозвать согласие, аккаунт и все записи будут удалены."
)
BTN_CONSENT_WITHDRAW = "Отозвать согласие"
CONSENT_WITHDRAW_CONFIRM = (
    "Отозвать согласие? Аккаунт и все записи будут удалены без возможности восстановления."
)
BTN_CONSENT_WITHDRAW_OK = "Да, отозвать и удалить"
CONSENT_WITHDRAWN = "Согласие отозвано, ваш аккаунт и все данные удалены."

DOCTOR_CONSENT_ASK = (
    "Врач увидит ваши измерения, лекарства, заметки, графики и отчёты и сможет "
    "оставлять комментарии. Менять записи он не сможет.\n\n"
    "Закрыть доступ можно в любой момент: «Мой врач» → «Отозвать доступ»."
)
BTN_DOCTOR_CONSENT_YES = "✅ Согласен, пригласить"

DOCTOR_SELF_CONSENT_ASK = (
    "Для работы кабинета врача мы храним ваши ФИО, e-mail или имя пользователя "
    "в Telegram и ваши заметки к записям пациентов.\n"
    "Подробнее: {privacy_url}#policy"
)

CONFIRM_DOCTOR_ACTIVE = (
    "Ваш врач {doctor} видит ваши измерения, лекарства, заметки, графики и отчёты.\n"
    "Оставить ему доступ?"
)
BTN_KEEP_ACCESS = "✅ Оставить доступ"
BTN_CLOSE_ACCESS = "Закрыть доступ"
CONFIRM_DOCTOR_PENDING = (
    "Вы пригласили врача {doctor}. После принятия приглашения он увидит ваши "
    "измерения, лекарства, заметки, графики и отчёты.\n"
    "Оставить приглашение?"
)
BTN_KEEP_INVITE = "✅ Оставить"
BTN_CANCEL_INVITE = "Отменить приглашение"


def consent_text(privacy_url: str, existing: bool) -> str:
    """Экран согласия Б1 (новый пользователь) / Б2 (существующий), §6.11."""
    first = CONSENT_FIRST_LINE_EXISTING if existing else CONSENT_FIRST_LINE_NEW
    return f"{first}\n\n{CONSENT_BODY.format(privacy_url=privacy_url)}"

# Кабинет (К2, К3, К5; doc/PERSONAL-DATA-TEXTS.md).
CABINET_CONSENT_TITLE = "Нужно ваше согласие"
CABINET_CONSENT_FULL_LINK = "Полный текст согласия"
CABINET_CONSENT_DECLINED = (
    "Без согласия дневник работать не может. Удалить аккаунт и все данные можно в боте: "
    "«Настройки» → «Удалить аккаунт»."
)
CABINET_CONSENT_LINE = "дано {date}"
CABINET_WITHDRAW_CONFIRM = (
    "Отозвать согласие? Аккаунт и все записи будут удалены в течение часа без "
    "возможности восстановления."
)
CABINET_WITHDRAWN = "Согласие отозвано. Аккаунт и все данные будут удалены в течение часа."
DOCTOR_CABINET_CONSENT_TEXT = (
    "Для работы кабинета мы храним ваши ФИО, e-mail или имя пользователя в Telegram "
    "и ваши заметки к записям пациентов."
)
DOCTOR_CABINET_CONSENT_CHECKBOX = (
    "Согласен на обработку моих данных (ФИО, e-mail и/или Telegram) —"
)
DOCTOR_CABINET_CONSENT_POLICY = "политика"


def consent_paragraphs() -> list[list[str]]:
    """Текст Б1 без строки «Полный текст» — абзацы для страницы согласия кабинета (К3)."""
    body = CONSENT_BODY.split("\nПолный текст:", 1)[0]
    return [paragraph.split("\n") for paragraph in body.split("\n\n")]


# ─────────────── Данные пациента для врача (SPEC.md, §6.12, П1–П7) ───────────────

BTN_PROFILE = "👤 Мои данные"
PROFILE_EMPTY = (
    "Мои данные не заполнены.\n\n"
    "ФИО, дату рождения и пол увидит только врач, которого вы пригласите, — так он "
    "узнает вас и правильно поймёт показатели."
)
PROFILE_FILLED = (
    "Мои данные\n\n"
    "ФИО: {name}\n"
    "Дата рождения: {birth} ({age})\n"
    "Пол: {sex}\n\n"
    "Эти данные видит только врач, которого вы пригласили."
)
BTN_PROFILE_FILL = "Заполнить"
BTN_PROFILE_EDIT = "Изменить"
BTN_PROFILE_DELETE = "Удалить данные"
PROFILE_ASK_LAST = "Введите фамилию:"
PROFILE_ASK_FIRST = "Введите имя:"
PROFILE_ASK_MIDDLE = "Введите отчество:"
BTN_PROFILE_NO_MIDDLE = "Нет отчества"
PROFILE_ASK_BIRTH = "Дата рождения в формате ДД.ММ.ГГГГ, например 12.03.1958:"
PROFILE_ASK_SEX = "Пол:"
BTN_SEX_MALE = "Мужской"
BTN_SEX_FEMALE = "Женский"
PROFILE_CURRENT = "Сейчас: {value}"
PROFILE_NAME_BAD = "От 1 до 50 букв; можно пробел, дефис и апостроф. Попробуйте ещё раз:"
PROFILE_BIRTH_BAD = "Нужна дата в формате ДД.ММ.ГГГГ, например 12.03.1958:"
PROFILE_TOO_YOUNG = (
    "Сервис доступен с 16 лет. Если вы законный представитель — заполните данные и "
    "согласие от своего имени."
)
PROFILE_SAVED = "Данные сохранены."
PROFILE_DELETE_CONFIRM = (
    "Удалить ФИО, дату рождения и пол? Врач снова будет видеть только ваше имя из Telegram."
)
BTN_PROFILE_DELETE_OK = "Да, удалить"
PROFILE_DELETED = "Данные удалены."
PROFILE_BEFORE_INVITE = "Чтобы врач узнал вас, укажите ФИО, дату рождения и пол."
PROFILE_PROMPT = "Заполните ФИО и дату рождения — врач увидит их в кабинете."
BTN_PROFILE_LATER = "Позже"

# Кабинет (§16.2): «Мои данные» в «Личной карточке».
CABINET_PROFILE_NOT_FILLED = "не заполнены"
ADMIN_STATS_PROFILES = "Заполнили данные: {count}"
