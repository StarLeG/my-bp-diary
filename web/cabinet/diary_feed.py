"""Смешанная лента дня и формы событий/приёмов (SPEC.md, §16.2; 0.6.2, п. 27).

Слой представления кабинета над общими сервисами (`bot.services.events`,
`bot.services.medications`): разбор форм событий и «💊 Отметить приём»,
строки ленты, чипы «Фактор» и чипы-фильтры (состав — как в боте, §6.4,
`d:filt:*`). Журнал записи (пара `medication_id + timestamp` совпадает со
связью измерения этого момента, §4.11) в ленте отдельно не рендерится и
standalone-редактору не доступен.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal, Mapping, Optional
from zoneinfo import ZoneInfo

from starlette.datastructures import FormData  # request.form() возвращает starlette-экземпляр

from bot import schemas, texts
from bot.models import DiaryEvent, Measurement, Medication, MedicationLog
from bot.services import events as events_service

#: Типы событий в порядке кнопок «➕ Добавить событие» (§6.14, R2 = А).
EVENT_TYPES = events_service.EVENT_TYPES

#: Шаг 1 добавления: (тип, подпись кнопки) дословно из §16.2, §6.14.
EVENT_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("food", texts.EVENT_TYPE_LABELS["food"]),
    ("stress", texts.EVENT_TYPE_LABELS["stress"]),
    ("sleep", texts.EVENT_TYPE_LABELS["sleep"]),
    ("activity", texts.EVENT_TYPE_LABELS["walk"]),
    ("note", texts.EVENT_TYPE_LABELS["note"]),
)

#: Чипы-фильтры ленты (§16.2; «Все» — стартовое состояние, §6.4).
FILTER_LABELS: dict[str, str] = {"all": "Все", **texts.DIARY_FILTER_LABELS}
#: Порядок чипов-фильтров.
FILTER_ORDER: tuple[str, ...] = ("all", "bp", "med", "fact", "other")
#: «Факторы» — события этих типов (§6.4, `d:filt:fact`).
FACT_TYPES: tuple[str, ...] = ("food", "stress", "sleep")
#: «Другое» — события этих типов (§6.4, `d:filt:other`).
OTHER_TYPES: tuple[str, ...] = ("activity", "note")

#: Статусы приёмов в строках ленты (§16.2, M1).
INTAKE_STATUS_LABELS: dict[str, str] = {
    "taken": "Принято ✓",
    "skipped": "Пропущено ✗",
    "partial": "Частично ~",
}
#: Порядок статусов в селектах форм приёмов.
INTAKE_STATUS_ORDER: tuple[str, ...] = ("taken", "skipped", "partial")

#: Метки питания в порядке §4.18 (та же сортировка, что в CSV, §10).
FOOD_MARK_ORDER: tuple[str, ...] = events_service.FOOD_MARKS

#: Эмодзи меток «Фактор» в чипах строки измерения («☕ Кофе», §16.2).
_MARK_EMOJI: dict[str, str] = {"salt": "🧂", "coffee": "☕", "alcohol": "🍷"}

#: Обрезка заметки в строке ленты (§6.4, M7: первые 60 символов).
FEED_NOTE_LIMIT = 60


class EventFormError(ValueError):
    """Поля формы события не прошли валидацию (§9, §16.2).

    `code` — «future» («Время измерения не может быть в будущем», единый
    текст §9) или «bad» (остальные поля).
    """

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


class IntakeFormError(ValueError):
    """Поля формы «💊 Отметить приём» не прошли валидацию (§16.2)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


# ─────────────────────────── Формы ───────────────────────────


def parse_moment(form: FormData, tz_name: str) -> datetime:
    """Момент формы «дата + время» (поля даты и времени, канон формы измерения).

    Исключения:
        EventFormError: будущее («future») или поля не разобраны («bad»).
    """
    try:
        day = date.fromisoformat(str(form.get("date", "")).strip())
        moment_time = schemas.validate_time(str(form.get("time", "")))
        return schemas.combine_moment(day, moment_time, tz_name)
    except ValueError as exc:
        code = "future" if str(exc) == texts.ARCHIVE_FUTURE_ERROR else "bad"
        raise EventFormError(code, str(exc)) from exc


def _note_or_none(form: FormData) -> Optional[str]:
    """Поле «Заметка» (≤ 500): пустое — None, иначе валидация §9."""
    raw = str(form.get("note", "")).strip()
    if not raw:
        return None
    try:
        return schemas.validate_note(raw)
    except ValueError as exc:
        raise EventFormError("bad", "заметка длиннее 500 символов") from exc


def parse_event_form(
    form: FormData, type_: str, tz_name: str, *, with_moment: bool
) -> dict:
    """Разобрать форму события (§4.18, §6.14, §16.2).

    `with_moment=False` — момент не редактируется (`sleep`, linked-события):
    поля даты/времени не читаются, `moment` в результате None.

    Возвращает {"moment": Optional[datetime], "payload": Optional[str],
    "note": Optional[str]} — готовые поля `create_event` / `update_event`.

    Исключения:
        EventFormError: «future» — будущий момент (единый текст §9);
            «bad» — остальные поля (метки, вариант, оценка, длительность,
            обязательный текст).
    """
    if type_ not in EVENT_TYPES:
        raise EventFormError("bad", "тип события не распознан")
    moment: Optional[datetime] = parse_moment(form, tz_name) if with_moment else None
    note = _note_or_none(form)
    payload: Optional[str]
    if type_ == "food":
        marks = {
            str(raw)
            for raw in form.getlist("marks")
            if str(raw) in events_service.FOOD_MARKS
        }
        if not 1 <= len(marks) <= 3:
            raise EventFormError("bad", texts.FACTOR_EMPTY)
        payload = events_service.event_payload("food", marks=marks)
    elif type_ == "stress":
        variant = str(form.get("variant", ""))
        if variant not in events_service.STRESS_VARIANTS:
            raise EventFormError("bad", "вариант не распознан")
        if variant == "other" and note is None:
            raise EventFormError("bad", texts.EVENT_TEXT_REQUIRED)
        payload = events_service.event_payload("stress", variant=variant)
    elif type_ == "sleep":
        rating = str(form.get("rating", ""))
        if rating not in events_service.SLEEP_RATINGS:
            raise EventFormError("bad", "оценка сна не распознана")
        payload = events_service.event_payload("sleep", rating=rating)
    elif type_ == "activity":
        raw = str(form.get("duration_min", ""))
        if not raw.isdigit() or int(raw) not in events_service.ACTIVITY_DURATIONS:
            raise EventFormError("bad", "длительность не распознана")
        payload = events_service.event_payload("activity", duration_min=int(raw))
    else:  # note — весь текст в заметке (§16.2)
        if note is None:
            raise EventFormError("bad", "Напишите заметку")
        payload = None
    return {"moment": moment, "payload": payload, "note": note}


def parse_intake_form(form: FormData, tz_name: str) -> tuple[datetime, dict[int, str]]:
    """Разобрать форму «💊 Отметить приём» (§16.2): момент и решения по лекарствам.

    Статус каждого лекарства — селект `status_{id}` («Принято» / «Пропущено» /
    «Частично»); не выбранные («—») и заблокированные (уже `taken`) в решения
    не попадают. Чужие и несуществующие id игнорируются (как в
    `log_slot_intake`).

    Исключения:
        IntakeFormError: «future» — будущий момент; «bad» — дата/время.
    """
    try:
        moment = parse_moment(form, tz_name)
    except EventFormError as exc:
        raise IntakeFormError(exc.code, str(exc)) from exc
    decisions: dict[int, str] = {}
    for key, value in form.items():
        if not key.startswith("status_"):
            continue
        raw_id = key[len("status_") :]
        status = str(value)
        if raw_id.isdigit() and status in INTAKE_STATUS_ORDER:
            decisions[int(raw_id)] = status
    return moment, decisions


def parse_intake_edit(form: FormData, tz_name: str) -> tuple[str, datetime]:
    """Разобрать форму правки standalone-приёма (§16.2): статус и момент.

    Исключения:
        IntakeFormError: «future» — будущий момент; «bad» — поля.
    """
    status = str(form.get("status", ""))
    if status not in INTAKE_STATUS_ORDER:
        raise IntakeFormError("bad", "статус не распознан")
    try:
        moment = parse_moment(form, tz_name)
    except EventFormError as exc:
        raise IntakeFormError(exc.code, str(exc)) from exc
    return status, moment


# ─────────────────────────── Значения форм ───────────────────────────


def payload_dict(event: DiaryEvent) -> dict:
    """`payload` события словарём (битый JSON — пустой словарь)."""
    try:
        payload = json.loads(event.payload) if event.payload else {}
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def event_form_values(event: Optional[DiaryEvent], tz: ZoneInfo, now: datetime) -> dict:
    """Значения формы события: правка — из события, добавление — текущие (§16.2).

    Момент «Сейчас» — поля даты и времени текущими значениями (канон формы
    измерения); у `sleep` и linked-событий момент не показывается.
    """
    moment = (event.moment if event is not None else now).astimezone(tz)
    payload = payload_dict(event) if event is not None else {}
    return {
        "type": event.type if event is not None else "",
        "date": moment.date().isoformat(),
        "time": moment.strftime("%H:%M"),
        "marks": {str(m) for m in payload.get("marks", [])} & set(events_service.FOOD_MARKS),
        "variant": str(payload.get("variant", "")),
        "rating": str(payload.get("rating", "")),
        "duration_min": payload.get("duration_min"),
        "note": (event.note or "") if event is not None else "",
    }


@dataclass
class IntakeMedRow:
    """Лекарство в форме «💊 Отметить приём» (§16.2)."""

    med: Medication
    #: Текущий статус за локальные сутки момента (None — ещё не отмечено).
    status: Optional[str] = None
    #: Уже `taken` за сутки — показывается отмеченным и заблокированным.
    taken_today: bool = False


@dataclass
class IntakeEditRow:
    """Standalone-строка приёма в ленте (§16.2, M1, M3)."""

    log: MedicationLog
    name: str
    dosage: str
    #: True — строку можно править и удалять (`source='cabinet'`, не журнал).
    editable: bool = False


# ─────────────────────────── Лента ───────────────────────────


@dataclass(frozen=True)
class FeedRow:
    """Строка смешанной ленты дня (§16.2): измерение, событие или приём."""

    kind: Literal["bp", "event", "intake"]
    moment: datetime
    measurement: Optional[Measurement] = None
    event: Optional[DiaryEvent] = None
    intake: Optional[IntakeEditRow] = None


def event_chips(event: DiaryEvent) -> list[tuple[str, str]]:
    """Чипы «Фактор» строки измерения: «☕ Кофе» и т.п. (§16.2, §6.3).

    Метки `food` — по одной чипе на метку в порядке §4.18; `stress` / `sleep` —
    по одной чипе (созданные «➕ Фактор» — подписями меток «Стресс» / «Недосып»).
    """
    payload = payload_dict(event)
    if event.type == "food":
        return [
            (_MARK_EMOJI.get(mark, ""), texts.FACTOR_LABELS[mark])
            for mark in FOOD_MARK_ORDER
            if mark in payload.get("marks", []) and mark in texts.FACTOR_LABELS
        ]
    if event.type == "stress":
        variant = str(payload.get("variant", "stress"))
        label = (
            texts.FACTOR_LABELS["stress"]
            if variant == "stress"
            else texts.STRESS_VARIANT_LABELS.get(variant, texts.FACTOR_LABELS["stress"])
        )
        return [("😐", label)]
    if event.type == "sleep":
        rating = str(payload.get("rating", "little"))
        label = (
            texts.FACTOR_LABELS["sleep"]
            if rating == "little"
            else texts.SLEEP_RATING_LABELS.get(rating, texts.FACTOR_LABELS["sleep"])
        )
        return [("😴", label)]
    return []


def event_preview(event: DiaryEvent) -> str:
    """Короткая подпись события для подтверждения удаления («…» в каноне)."""
    if event.type == "note":
        return _truncate(event.note or "Заметка", FEED_NOTE_LIMIT)
    return texts.event_kind_line(event.type, payload_dict(event))


def intake_label(row: IntakeEditRow) -> str:
    """Подпись приёма для подтверждения удаления: «Лозартан 50 мг · утро» (§16.2)."""
    return texts.fmt_intake_item(row.name, row.dosage, row.log.slot)


def _truncate(text: str, limit: int) -> str:
    """Обрезать текст с «…» до `limit` символов (§6.4, M7)."""
    return text if len(text) <= limit else text[:limit] + "…"


def event_row_text(event: DiaryEvent, tz_name: str) -> str:
    """Строка события ленты: «13:20 — 🥗 Питание: солёное, кофе» (§6.4, §16.2).

    У `note` — «13:20 — 📝 Заметка: …» (первые 60 символов, полный — в
    карточке/форме); `note` у `stress` в ленте не показывается (§6.4).
    """
    time_str = event.moment.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
    emoji = texts.EVENT_EMOJI.get(event.type, "📝")
    if event.type == "note":
        return f"{time_str} — {emoji} Заметка: {_truncate(event.note or '', FEED_NOTE_LIMIT)}"
    kind_line = texts.event_kind_line(event.type, payload_dict(event))
    return texts.fmt_diary_event_line(time_str, kind_line, emoji)


def intake_row_text(row: IntakeEditRow, tz_name: str) -> str:
    """Строка приёма ленты: «💊 Приём лекарств: Лозартан 50 мг · утро — Принято ✓».

    Слот — подписью (`fmt_intake_item`); статус — как в боте (§6.4, B2/I2),
    полными словами («Принято ✓» / «Пропущено ✗» / «Частично ~», §16.2, M1).
    """
    time_str = row.log.timestamp.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
    item = texts.fmt_intake_item(row.name, row.dosage, row.log.slot)
    status = INTAKE_STATUS_LABELS.get(row.log.status, row.log.status)
    return f"{time_str} — 💊 Приём лекарств: {item} — {status}"


def is_journal_log(
    log: MedicationLog,
    measurements: Iterable[Measurement],
    linked_med_ids: Mapping[int, set[int]],
) -> bool:
    """Строка — журнал записи измерения (§4.11, §16.2): пара `medication_id + timestamp`.

    Пара совпадает со связью `measurement_medications` измерения этого момента —
    строка управляется формой измерения и в ленте отдельно не рендерится.
    """
    for measurement in measurements:
        if (
            measurement.timestamp == log.timestamp
            and log.medication_id in linked_med_ids.get(measurement.id, ())
        ):
            return True
    return False


def row_visible(row: FeedRow, filt: str, linked_med_ids: Mapping[int, set[int]]) -> bool:
    """Видимость строки под чип-фильтр (§16.2; состав — как `d:filt:*` §6.4).

    «Лекарства» — измерения с отмеченными приёмами (`measurement_medications`)
    и standalone-строки приёмов; «Факторы» — `food`/`stress`/`sleep`;
    «Другое» — `activity`/`note`.
    """
    if filt == "all":
        return True
    if filt == "bp":
        return row.kind == "bp"
    if filt == "med":
        if row.kind == "intake":
            return True
        return row.kind == "bp" and bool(linked_med_ids.get(row.measurement.id))
    if filt == "fact":
        return row.kind == "event" and row.event.type in FACT_TYPES
    if filt == "other":
        return row.kind == "event" and row.event.type in OTHER_TYPES
    return True


def build_feed(
    measurements: Iterable[Measurement],
    events: Iterable[DiaryEvent],
    intakes: Iterable[IntakeEditRow],
    linked_med_ids: Mapping[int, set[int]],
    filt: str,
    tz_name: str,
    window_start: date,
    end_day: date,
) -> dict[date, list[FeedRow]]:
    """Строки ленты по локальным дням (новые дни снаружи, строки — по времени).

    Учитываются только моменты внутри `[window_start, end_day]` (окно показа
    «Истории записей»); строки вне фильтра `filt` отбрасываются.
    """
    tz = ZoneInfo(tz_name)
    rows: list[FeedRow] = []
    for measurement in measurements:
        rows.append(FeedRow("bp", measurement.timestamp, measurement=measurement))
    for event in events:
        rows.append(FeedRow("event", event.moment, event=event))
    for intake in intakes:
        rows.append(FeedRow("intake", intake.log.timestamp, intake=intake))
    by_day: dict[date, list[FeedRow]] = {}
    for row in rows:
        day = row.moment.astimezone(tz).date()
        if not (window_start <= day <= end_day):
            continue
        if not row_visible(row, filt, linked_med_ids):
            continue
        by_day.setdefault(day, []).append(row)
    for day_rows in by_day.values():
        day_rows.sort(
            key=lambda row: (
                row.moment,
                {"bp": 0, "event": 1, "intake": 2}[row.kind],
                row.measurement.id if row.measurement else 0,
                row.event.id if row.event else 0,
                row.intake.log.id if row.intake else 0,
            )
        )
    return by_day
