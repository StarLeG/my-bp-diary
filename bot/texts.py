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
BTN_MED_SKIPPED = "Пропустил"
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

BTN_TARGETS = "Целевые значения"
BTN_REMINDERS = "Напоминания"
BTN_MY_MEDS = "Мои лекарства"
BTN_MY_DOCTOR = "Мой врач"
BTN_TIMEZONE = "Часовой пояс"

BTN_ADD = "Добавить"
BTN_DELETE = "Удалить"

BTN_TAKE_ALL = "Принял всё ✓"
BTN_PARTIAL = "Частично…"
BTN_SNOOZE_30 = "Через 30 мин"
BTN_SKIP_SLOT = "Пропускаю"
BTN_REMEASURE_NOW = "Записать сейчас"
BTN_REMEASURE_OFF = "Больше не напоминать"

BTN_SUPPORT_PROJECT = "Поддержать проект"
BTN_WRITE_AUTHOR = "Написать автору"
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

PROMPT_SYSTOLIC_OTHER = "Введите систолическое (верхнее) число от 70 до 260."
PROMPT_DIASTOLIC_OTHER = "Введите диастолическое (нижнее) число от 40 до 160."
PROMPT_PULSE_OTHER = "Введите пульс от 30 до 220."
PROMPT_NOTE = "Напишите заметку (до 500 символов)."
PROMPT_FEELING_OTHER = "Коротко опишите самочувствие — сохраню в заметку."

MEASUREMENT_SAVED = "✅ {measurement}. Записано."
MEASUREMENT_SAVED_PULSE = "✅ {measurement}, пульс {pulse}. Записано."
MEASUREMENT_UPDATED = "✅ {measurement}. Обновлено."
MEASUREMENT_UPDATED_PULSE = "✅ {measurement}, пульс {pulse}. Обновлено."
HIGH_PRESSURE_WARNING = "Эти значения стоит обсудить с врачом на ближайшем приёме."
NOTE_SAVED = "Заметка сохранена"

MED_ASK = "Приняли лекарство?"
FEELING_ASK = "Как самочувствие?"
MED_REMEASURE_PLANNED = "Напомню повторить измерение через {delay} минут."
MED_REMEASURE_DISABLED = "Повтор измерения после лекарства выключен — включите в настройках, если нужен."
MED_REMEASURE_CANCELLED = "Хорошо, повторно напоминать не буду."

# ─────────────────────────── Дневник ───────────────────────────

DIARY_EMPTY = "Сегодня записей ещё нет"
DIARY_TOTAL = "Всего записей: {count}"

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


def fmt_saved(systolic: int, diastolic: int, pulse: Optional[int] = None) -> str:
    """Сообщение о сохранённом измерении (SPEC.md, раздел 6.3)."""
    if pulse is None:
        return MEASUREMENT_SAVED.format(measurement=fmt_measurement_short(systolic, diastolic))
    return MEASUREMENT_SAVED_PULSE.format(
        measurement=fmt_measurement_short(systolic, diastolic), pulse=pulse
    )


def fmt_updated(systolic: int, diastolic: int, pulse: Optional[int] = None) -> str:
    """Сообщение об обновлённой записи правки (SPEC.md, раздел 6.3)."""
    if pulse is None:
        return MEASUREMENT_UPDATED.format(measurement=fmt_measurement_short(systolic, diastolic))
    return MEASUREMENT_UPDATED_PULSE.format(
        measurement=fmt_measurement_short(systolic, diastolic), pulse=pulse
    )


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
) -> str:
    """Сводка статистики (SPEC.md, раздел 6.5) — блоками, каждый на своей строке."""
    lines = [
        STATS_HEADER.format(days=days, date_from=date_from, date_to=date_to),
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


def fmt_meds_summary(taken: Sequence[str], skipped: Sequence[str]) -> str:
    """Итог отметки приёма: что принято и что пропущено."""
    parts = []
    if taken:
        parts.append("принято: " + ", ".join(taken))
    if skipped:
        parts.append("пропущено: " + ", ".join(skipped))
    return "; ".join(parts) if parts else "нет данных"

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
DOCTOR_NO_LINK = (
    "Врач пока не добавлен.\n"
    "Отправьте e-mail врача сообщением — подготовлю ссылку для него."
)
DOCTOR_EMAIL_BAD = "Кажется, это не похоже на e-mail. Попробуйте ещё раз:"
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

DOCTOR_LINK_ITEM = "• {email} — {status}"
DOCTOR_STATUS_LABELS = {
    "pending": "ожидает врача",
    "active": "доступ открыт",
    "revoked": "доступ отозван",
}

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
    "Напоминания: {reminders}"
)
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
