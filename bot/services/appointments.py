"""Визиты к врачу, анализы и обследования (SPEC.md, §4.16, §6.13; 0.5.8).

Дата и время события хранятся **локальными** (`users.timezone`): моменты
уведомлений считаются в момент проверки, смена пояса пересчитывает их сама.

Уведомления доставляет тик бота раз в минуту (`RemindersService.tick`, §6.7):
`deliver_due` находит созревшие и захватывает каждое атомарно —
`UPDATE … SET sent_*_at = now WHERE id = ? AND sent_*_at IS NULL AND status =
'planned'`; отправка — только после успешного захвата и вне сессии (§7.2).
Флаг — «обработано»: уведомления, чей момент прошёл при сохранении или
переносе (В7), просроченные дольше окна catch-up, без согласия — получают флаг
без отправки. Job'ов на событие нет: строки — единственный источник истины,
поэтому события из личного кабинета работают без синхронизации процессов.

Лимит «30 событий «Скоро»» и «следующий визит» — условные вставки одним
`INSERT … SELECT … WHERE` (гонка двух вкладок или двойное нажатие не создаёт
лишнего, §7.2). Статусы меняются `UPDATE … WHERE status = 'planned'`.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.models import Appointment, AppointmentConclusion, User

logger = logging.getLogger(__name__)

#: Типы событий (CHECK в БД).
KINDS = texts.AP_KINDS
#: Длина названия и заметки (В21).
TITLE_MAX = 60
NOTE_MAX = 200
#: Предстоящих событий («Скоро») на пользователя (В21).
UPCOMING_LIMIT = 30
#: Насколько вперёд можно запланировать (В21).
HORIZON_YEARS = 2
#: Ответ на «Как прошёл визит?» принимается столько дней после даты; дальше — «без отметки» (Н2).
APPOINTMENT_STATUS_TIMEOUT = timedelta(days=3)
#: «Прошедшие» в боте — последние столько (В13).
PAST_LIMIT = 20

#: Моменты уведомлений (локальное время, §6.13).
EVE_TIME = time(20, 0)
MORNING_TIME = time(7, 0)
SOON_BEFORE = timedelta(hours=2)
SOON_EARLIEST = time(6, 0)
AFTER_TIME = time(20, 0)
AFTER_LATE_START = time(18, 0)
AFTER_NEXT_DAY_TIME = time(10, 0)

#: Окно catch-up: просроченное дольше — обрабатывается без отправки (как у слотов, §6.7).
CATCHUP_WINDOW = timedelta(hours=6)

#: Уведомления и их флаги, в порядке дня.
NOTICES = ("eve", "soon", "morning", "after")
FLAG = {
    "eve": "sent_eve_at",
    "soon": "sent_soon_at",
    "morning": "sent_morning_at",
    "after": "sent_after_at",
}

STATUSES = ("planned", "done", "moved", "missed", "cancelled")

#: Отправка: (telegram_id, текст, клавиатура) → message_id или None.
Sender = Callable[..., Awaitable[Optional[int]]]
#: Снять кнопки сообщения: (telegram_id, message_id).
MarkupClearer = Callable[[int, int], Awaitable[None]]
#: Клавиатура уведомления: (событие, уведомление, есть ли врач с доступом, large) → разметка.
NoticeMarkup = Callable[..., object]


class LimitReached(Exception):
    """Запланировано уже `UPCOMING_LIMIT` событий (В21)."""


# ─────────────────────────── Даты и проверки ───────────────────────────


def local_now(user: User, now: Optional[datetime] = None) -> datetime:
    """Текущий момент в поясе пользователя."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(user.timezone))


def today(user: User, now: Optional[datetime] = None) -> date:
    return local_now(user, now).date()


def add_months(day: date, months: int) -> date:
    """`day` + N месяцев; несуществующее число — последний день месяца (В11)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def max_day(current: date) -> date:
    """Последний допустимый день: сегодня + 2 года (29.02 → 28.02)."""
    return add_months(current, 12 * HORIZON_YEARS)


def parse_hhmm(raw: str) -> Optional[str]:
    """«10:30», «9:05», «1030» (кнопки) → «10:30»; иначе None."""
    value = raw.strip().replace(".", ":")
    if ":" not in value and len(value) == 4 and value.isdigit():
        value = f"{value[:2]}:{value[2:]}"
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or not parts[0] or len(parts[1]) != 2:
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def valid_title(raw: str) -> Optional[str]:
    title = " ".join(raw.split())
    return title if 1 <= len(title) <= TITLE_MAX else None


def valid_note(raw: str) -> Optional[str]:
    note = raw.strip()
    return note if len(note) <= NOTE_MAX else None


def time_passed(user: User, day: date, hhmm: Optional[str], now: Optional[datetime] = None) -> bool:
    """Сегодня и время уже прошло (шаг 4, §6.13)."""
    if hhmm is None:
        return False
    current = local_now(user, now)
    return day == current.date() and _hhmm(hhmm) <= current.time().replace(second=0, microsecond=0)


def day_problem(user: User, day: date, now: Optional[datetime] = None) -> Optional[str]:
    """Текст всплывающего уведомления для недопустимой даты (шаг 3) или None."""
    current = today(user, now)
    if day < current:
        return texts.AP_DAY_PAST
    if day > max_day(current):
        return texts.AP_DAY_TOO_FAR
    return None


# ─────────────────────────── Моменты уведомлений ───────────────────────────


def _at(day: date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz).astimezone(timezone.utc)


def notice_moments(
    starts_on: date, starts_at: Optional[str], tz_name: str
) -> dict[str, Optional[datetime]]:
    """Моменты уведомлений события (UTC); None — уведомления у события нет (§6.13)."""
    tz = ZoneInfo(tz_name)
    moments: dict[str, Optional[datetime]] = {
        "eve": _at(starts_on - timedelta(days=1), EVE_TIME, tz),
        "soon": None,
        "morning": None,
    }
    if starts_at is None:
        moments["morning"] = _at(starts_on, MORNING_TIME, tz)
        moments["after"] = _at(starts_on, AFTER_TIME, tz)
    else:
        start = datetime.combine(starts_on, _hhmm(starts_at))
        soon = start - SOON_BEFORE
        if soon.date() == starts_on and soon.time() >= SOON_EARLIEST:  # Н3: не раньше 06:00
            moments["soon"] = soon.replace(tzinfo=tz).astimezone(timezone.utc)
        if _hhmm(starts_at) > AFTER_LATE_START:
            moments["after"] = _at(starts_on + timedelta(days=1), AFTER_NEXT_DAY_TIME, tz)
        else:
            moments["after"] = _at(starts_on, AFTER_TIME, tz)
    return moments


def start_moment(starts_on: date, starts_at: Optional[str], tz_name: str) -> Optional[datetime]:
    """Начало события (UTC); без времени — None."""
    if starts_at is None:
        return None
    return _at(starts_on, _hhmm(starts_at), ZoneInfo(tz_name))


def fresh_flags(
    starts_on: date, starts_at: Optional[str], tz_name: str, now: datetime
) -> dict[str, Optional[datetime]]:
    """Флаги при сохранении или переносе (В7): прошедшие и неприменимые — `now`, будущие — None."""
    flags: dict[str, Optional[datetime]] = {}
    for notice, moment in notice_moments(starts_on, starts_at, tz_name).items():
        flags[FLAG[notice]] = None if moment is not None and moment > now else now
    return flags


def schedule_line(starts_on: date, starts_at: Optional[str], tz_name: str, now: datetime) -> str:
    """Строка расписания по уведомлениям, которые ещё впереди (§6.13)."""
    moments = notice_moments(starts_on, starts_at, tz_name)
    ahead = tuple(n for n in ("eve", "soon", "morning") if moments[n] is not None and moments[n] > now)
    return texts.AP_SCHEDULE[ahead]


# ─────────────────────────── Статусы и форматирование ───────────────────────────


def awaiting_answer(appt: Appointment, current: date) -> bool:
    """«Как прошёл визит?» ещё принимается: `planned`, дата наступила, прошло ≤ 3 дней."""
    return (
        appt.status == "planned"
        and appt.starts_on <= current
        and current - appt.starts_on <= APPOINTMENT_STATUS_TIMEOUT
    )


def marking_allowed(appt: Appointment, current: date) -> bool:
    """Отметка из карточки (В26-А, У1-А): `planned`, дата наступила — без окна в 3 дня.

    Окно 3 дня (`awaiting_answer`) — только для кнопок уведомления `ap:after`.
    """
    return appt.status == "planned" and appt.starts_on <= current


def is_upcoming(appt: Appointment, current: date) -> bool:
    """В «Скоро»: `planned` и дата не прошла (сегодняшнее — до полуночи, Н2)."""
    return appt.status == "planned" and appt.starts_on >= current


def status_key(appt: Appointment, current: date) -> str:
    """Статус для «Прошедших» и карточки; `waiting` / `unmarked` — вычисляемые (Н2)."""
    if appt.status != "planned":
        return appt.status
    return "waiting" if current - appt.starts_on <= APPOINTMENT_STATUS_TIMEOUT else "unmarked"


def status_label(appt: Appointment, current: date) -> str:
    key = status_key(appt, current)
    if appt.kind == "lab" and key in texts.AP_STATUS_LAB:
        return texts.AP_STATUS_LAB[key]
    return texts.AP_STATUS[key]


def fmt_day(day: date) -> str:
    """«пт 14.11»."""
    return f"{texts.AP_WEEKDAYS[day.weekday()]} {day:%d.%m}"


def fmt_event(appt: Appointment, icon: bool = True) -> str:
    """«🩺 Кардиолог — пт 14.11, 10:30» / «…, без времени» (§6.13)."""
    when = f"{fmt_day(appt.starts_on)}, {appt.starts_at or texts.AP_NO_TIME}"
    prefix = f"{texts.AP_ICONS[appt.kind]} " if icon else ""
    return f"{prefix}{appt.title} — {when}"


def fmt_event_block(appt: Appointment, icon: bool = True) -> str:
    """Событие и заметка следующей строкой с отступом."""
    lines = [fmt_event(appt, icon)]
    if appt.note:
        lines.append(f"   {appt.note}")
    return "\n".join(lines)


def fmt_card(
    appt: Appointment,
    user: User,
    now: Optional[datetime] = None,
    conclusions: Sequence[AppointmentConclusion] = (),
) -> str:
    """Карточка события (§6.13); строка заключения — превью без типа (0.5.11)."""
    moment = now or datetime.now(timezone.utc)
    current = today(user, moment)
    when = f"{texts.AP_WEEKDAYS[appt.starts_on.weekday()]} {appt.starts_on:%d.%m.%Y}"
    lines = [
        f"{texts.AP_ICONS[appt.kind]} {appt.title}",
        f"{when}, {appt.starts_at or texts.AP_NO_TIME}",
    ]
    if appt.note:
        lines.append(appt.note)
    lines.append(fmt_conclusion_line(conclusions))
    if is_upcoming(appt, current):
        lines.append(schedule_line(appt.starts_on, appt.starts_at, user.timezone, moment))
    else:
        lines.append(texts.AP_STATUS_LINE.format(status=status_label(appt, current)))
    return "\n".join(line for line in lines if line)


def fmt_saved(appt: Appointment, user: User, now: Optional[datetime] = None) -> str:
    """«Готово ✓» + событие + строка расписания (после добавления, переноса, правки)."""
    moment = now or datetime.now(timezone.utc)
    return "\n".join(
        [
            texts.AP_SAVED,
            fmt_event_block(appt),
            schedule_line(appt.starts_on, appt.starts_at, user.timezone, moment),
        ]
    )


def _sort_key(appt: Appointment) -> tuple:
    return (appt.starts_on, appt.starts_at is not None, appt.starts_at or "", appt.id)


# ─────────────────────────── Чтение ───────────────────────────


async def get_owned(session: AsyncSession, user: User, appointment_id: int) -> Optional[Appointment]:
    """Событие пользователя по id; чужое или удалённое — None (§7.1)."""
    return (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.user_id == user.id
            )
        )
    ).scalar_one_or_none()


async def list_upcoming(session: AsyncSession, user: User, current: date) -> list[Appointment]:
    rows = (
        await session.execute(
            select(Appointment).where(
                Appointment.user_id == user.id,
                Appointment.status == "planned",
                Appointment.starts_on >= current,
            )
        )
    ).scalars().all()
    return sorted(rows, key=_sort_key)


def _past_clause(user: User, current: date):
    return and_(
        Appointment.user_id == user.id,
        or_(Appointment.status != "planned", Appointment.starts_on < current),
    )


async def list_past(
    session: AsyncSession, user: User, current: date, limit: Optional[int] = PAST_LIMIT
) -> tuple[list[Appointment], int]:
    """Прошедшие (новые сверху) и их общее число (§6.13)."""
    query = (
        select(Appointment)
        .where(_past_clause(user, current))
        .order_by(Appointment.starts_on.desc(), Appointment.starts_at.desc(), Appointment.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    rows = list((await session.execute(query)).scalars().all())
    total = (
        await session.execute(select(func.count()).select_from(Appointment).where(_past_clause(user, current)))
    ).scalar_one()
    return rows, int(total)


async def count_upcoming(session: AsyncSession, user: User, current: date) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Appointment).where(
                    Appointment.user_id == user.id,
                    Appointment.status == "planned",
                    Appointment.starts_on >= current,
                )
            )
        ).scalar_one()
    )


async def day_events(session: AsyncSession, user: User, current: date) -> tuple[Optional[str], list[Appointment]]:
    """Ближайший день с событиями — «today» / «tomorrow» — и его события (§6.4, В15)."""
    rows = (
        await session.execute(
            select(Appointment).where(
                Appointment.user_id == user.id,
                Appointment.status == "planned",
                Appointment.starts_on.in_([current, current + timedelta(days=1)]),
            )
        )
    ).scalars().all()
    for key, day in (("today", current), ("tomorrow", current + timedelta(days=1))):
        found = sorted((r for r in rows if r.starts_on == day), key=_sort_key)
        if found:
            return key, found
    return None, []


#: Длина строки «📅 Сегодня: …», после которой — первое событие и «…» (§6.4).
DIARY_LINE_MAX = 80


def diary_line(key: Optional[str], events: Sequence[Appointment]) -> Optional[str]:
    """«📅 Сегодня: Анализы 08:00, Кардиолог 15:00» / «📅 Завтра: Кардиолог, 10:30» (§6.4).

    Названия экранируются (HTML): строка входит в экран дневника с `<b>`-шапкой.
    """
    if key is None or not events:
        return None
    prefix = texts.DIARY_AP_DAY[key]
    if len(events) == 1:
        event = events[0]
        return (
            prefix
            + texts.esc(event.title)
            + (f", {event.starts_at}" if event.starts_at else "")
        )
    parts = [
        texts.esc(e.title) + (f" {e.starts_at}" if e.starts_at else "") for e in events
    ]
    line = prefix + ", ".join(parts)
    if len(line) > DIARY_LINE_MAX:
        line = prefix + parts[0] + ", …"
    return line


# ─────────────────────────── Запись ───────────────────────────

_INSERT_LIMITED = text(
    """
    INSERT INTO appointments (
        user_id, kind, title, starts_on, starts_at, note, status,
        sent_eve_at, sent_soon_at, sent_morning_at, sent_after_at, created_at
    )
    SELECT :user_id, :kind, :title, :starts_on, :starts_at, :note, 'planned',
           :sent_eve_at, :sent_soon_at, :sent_morning_at, :sent_after_at, :created_at
    WHERE (
        SELECT COUNT(*) FROM appointments
        WHERE user_id = :user_id AND status = 'planned' AND starts_on >= :today
    ) < :limit
    AND (:unique_check = 0 OR NOT EXISTS (
        SELECT 1 FROM appointments
        WHERE user_id = :user_id AND status = 'planned' AND kind = :kind
          AND title = :title AND starts_on = :starts_on
    ))
    """
)


def _db_time(value: Optional[datetime]) -> Optional[str]:
    """UTC-момент в формате столбцов `UTCDateTime` (сырой SQL)."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


async def _insert(
    session: AsyncSession,
    user: User,
    kind: str,
    title: str,
    starts_on: date,
    starts_at: Optional[str],
    note: str,
    now: datetime,
    unique_check: bool = False,
) -> Optional[int]:
    """Условная вставка: лимит «Скоро» (и, для «следующего», отсутствие такого же)."""
    flags = fresh_flags(starts_on, starts_at, user.timezone, now)
    result = await session.execute(
        _INSERT_LIMITED,
        {
            "user_id": user.id,
            "kind": kind,
            "title": title,
            "starts_on": starts_on.isoformat(),
            "starts_at": starts_at,
            "note": note,
            "created_at": _db_time(now),
            "today": today(user, now).isoformat(),
            "limit": UPCOMING_LIMIT,
            "unique_check": 1 if unique_check else 0,
            **{name: _db_time(value) for name, value in flags.items()},
        },
    )
    return result.lastrowid if result.rowcount == 1 else None


async def create(
    session: AsyncSession,
    user: User,
    kind: str,
    title: str,
    starts_on: date,
    starts_at: Optional[str],
    note: str = "",
    now: Optional[datetime] = None,
) -> Appointment:
    """Новое событие (§6.13); лимит — `LimitReached`. Данные уже проверены вызывающим."""
    moment = now or datetime.now(timezone.utc)
    new_id = await _insert(session, user, kind, title, starts_on, starts_at, note, moment)
    if new_id is None:
        raise LimitReached
    created = await get_owned(session, user, new_id)
    assert created is not None
    return created


async def create_next(
    session: AsyncSession, user: User, appt: Appointment, months: int, now: Optional[datetime] = None
) -> Optional[Appointment]:
    """«Записать следующий» (В11): +N месяцев, то же время; повтор — None; лимит — `LimitReached`."""
    moment = now or datetime.now(timezone.utc)
    if await count_upcoming(session, user, today(user, moment)) >= UPCOMING_LIMIT:
        raise LimitReached
    new_id = await _insert(
        session,
        user,
        appt.kind,
        appt.title,
        add_months(appt.starts_on, months),
        appt.starts_at,
        appt.note,
        moment,
        unique_check=True,
    )
    return None if new_id is None else await get_owned(session, user, new_id)


async def reschedule(
    session: AsyncSession,
    user: User,
    appt: Appointment,
    starts_on: date,
    starts_at: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Перенос до события / правка даты и времени: та же строка, флаги заново (Н1, Н4)."""
    moment = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(Appointment)
        .where(Appointment.id == appt.id, Appointment.status == "planned")
        .values(
            starts_on=starts_on,
            starts_at=starts_at,
            **fresh_flags(starts_on, starts_at, user.timezone, moment),
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(appt)
    return bool(result.rowcount)


async def moved_after(
    session: AsyncSession,
    user: User,
    appt: Appointment,
    starts_on: date,
    starts_at: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[Appointment]:
    """«Перенёс(ла)» после события (Н1): прежняя → `moved`, новая `planned` — одной транзакцией.

    Прежняя уже не `planned` (двойное нажатие) — None; лимит — `LimitReached`
    (прежняя строка не меняется: вызывающий откатывает транзакцию).
    """
    moment = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(Appointment)
        .where(Appointment.id == appt.id, Appointment.status == "planned")
        .values(status="moved")
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return None
    new_id = await _insert(session, user, appt.kind, appt.title, starts_on, starts_at, appt.note, moment)
    if new_id is None:
        raise LimitReached
    return await get_owned(session, user, new_id)


async def set_status(session: AsyncSession, appt: Appointment, status: str) -> bool:
    """`planned` → `done` / `missed` / `cancelled`; повторно — False (идемпотентно)."""
    assert status in ("done", "missed", "cancelled")
    result = await session.execute(
        update(Appointment)
        .where(Appointment.id == appt.id, Appointment.status == "planned")
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(appt)
    return bool(result.rowcount)


async def set_kind(session: AsyncSession, appt: Appointment, kind: str) -> None:
    """Сменить вид события: в ЛК можно (§16.2, 0.5.9), в боте — нет (Н4)."""
    appt.kind = kind
    await session.flush()


async def set_title(session: AsyncSession, appt: Appointment, title: str) -> None:
    appt.title = title
    await session.flush()


async def set_note(session: AsyncSession, appt: Appointment, note: str) -> None:
    appt.note = note
    await session.flush()


async def remove(session: AsyncSession, user: User, appointment_id: int) -> bool:
    result = await session.execute(
        delete(Appointment).where(Appointment.id == appointment_id, Appointment.user_id == user.id)
    )
    return bool(result.rowcount)


async def count_planned(session: AsyncSession) -> int:
    """Админка (§18.2): только `COUNT(*)`, без названий и заметок."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Appointment).where(Appointment.status == "planned")
            )
        ).scalar_one()
    )


# ─────────────────────────── Доставка (тик) ───────────────────────────


@dataclass
class Notice:
    """Захваченное уведомление, готовое к отправке."""

    appointment_id: int
    notice: str
    telegram_id: int
    previous_message_id: Optional[int]
    text: str
    markup: object


def notice_text(appt: Appointment, notice: str) -> str:
    """Текст уведомления: первая строка по типу, затем событие без иконки и заметка (§6.13)."""
    first = texts.AP_AFTER_ASK[appt.kind] if notice == "after" else texts.AP_NOTICE[notice][appt.kind]
    return f"{first}\n{fmt_event_block(appt, icon=False)}"


async def _claim(session: AsyncSession, appointment_id: int, notice: str, now: datetime) -> bool:
    column = getattr(Appointment, FLAG[notice])
    result = await session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id, column.is_(None), Appointment.status == "planned")
        .values({FLAG[notice]: now})
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def has_doctor_access(session: AsyncSession, user_id: int) -> bool:
    """Есть ли врач, который сейчас видит пациента (три условия §6.11)."""
    from bot.services import consent as consent_service
    from bot.services.doctor import links_for_patient

    for link, doctor in await links_for_patient(session, user_id):
        if link.status == "active" and await consent_service.doctor_can_see(session, doctor, user_id):
            return True
    return False


async def collect_due(
    session_factory: async_sessionmaker[AsyncSession],
    markup_for: NoticeMarkup,
    now: Optional[datetime] = None,
) -> list[Notice]:
    """Созревшие уведомления: захватить (или обработать без отправки) и подготовить (§6.13).

    Транзакции короткие: выборка, затем захват по одному уведомлению.
    """
    from bot.services import consent as consent_service

    moment = now or datetime.now(timezone.utc)
    utc_day = moment.date()
    flags_null = or_(*(getattr(Appointment, FLAG[n]).is_(None) for n in NOTICES))
    ready: list[Notice] = []
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Appointment, User)
                .join(User, User.id == Appointment.user_id)
                .where(
                    Appointment.status == "planned",
                    Appointment.starts_on.between(utc_day - timedelta(days=2), utc_day + timedelta(days=2)),
                    flags_null,
                )
            )
        ).all()
        for appt, user in rows:
            moments = notice_moments(appt.starts_on, appt.starts_at, user.timezone)
            start = start_moment(appt.starts_on, appt.starts_at, user.timezone)
            for notice in NOTICES:
                if getattr(appt, FLAG[notice]) is not None:
                    continue
                at = moments[notice]
                if at is not None and at > moment:
                    continue
                silent = (
                    at is None
                    or moment - at > CATCHUP_WINDOW
                    or (notice == "soon" and start is not None and moment >= start)
                    or not await consent_service.has_processing(session, user.id)
                )
                if not await _claim(session, appt.id, notice, moment):
                    continue
                await session.commit()
                if silent:
                    continue
                with_doctor = appt.kind == "doctor" and notice != "after" and await has_doctor_access(
                    session, user.id
                )
                ready.append(
                    Notice(
                        appointment_id=appt.id,
                        notice=notice,
                        telegram_id=user.telegram_id,
                        previous_message_id=appt.last_message_id,
                        text=notice_text(appt, notice),
                        markup=markup_for(appt, notice, with_doctor, user.ui_large),
                    )
                )
        await session.commit()
    return ready


async def deliver_due(
    session_factory: async_sessionmaker[AsyncSession],
    sender: Sender,
    markup_for: NoticeMarkup,
    clear_markup: Optional[MarkupClearer] = None,
    now: Optional[datetime] = None,
) -> int:
    """Тик: захватить и отправить созревшие уведомления; число отправленных (§6.13)."""
    sent = 0
    for notice in await collect_due(session_factory, markup_for, now):
        try:
            message_id = await sender(notice.telegram_id, notice.text, notice.markup)
        except Exception:
            logger.exception("Не удалось отправить уведомление о событии %s", notice.appointment_id)
            continue
        sent += 1
        if not isinstance(message_id, int):
            continue
        async with session_factory() as session:
            await session.execute(
                update(Appointment)
                .where(Appointment.id == notice.appointment_id)
                .values(last_message_id=message_id)
            )
            await session.commit()
        if clear_markup is not None and notice.previous_message_id not in (None, message_id):
            try:
                await clear_markup(notice.telegram_id, notice.previous_message_id)  # type: ignore[arg-type]
            except Exception:
                logger.info("Кнопки прежнего уведомления события %s не сняты", notice.appointment_id)
    return sent


# ─────────────────────────── Заключения (0.5.11) ───────────────────────────

#: Заключений на событие (В36-А) — общий лимит для бота и ЛК.
CONCLUSION_LIMIT = 5
#: Длина текста заключения (В36-А); пустой (после `trim()`) отклоняется.
CONCLUSION_TEXT_MAX = 4000
#: Длина превью в списке, карточке и подтверждении удаления.
CONCLUSION_PREVIEW = 60


def valid_conclusion_text(raw: str) -> Optional[str]:
    """Текст заключения: убраны краевые пробелы, 1–4000 символов; иначе None (§4.17)."""
    cleaned = (raw or "").strip()
    return cleaned if 1 <= len(cleaned) <= CONCLUSION_TEXT_MAX else None


def conclusion_preview(value: str, size: int = CONCLUSION_PREVIEW) -> str:
    """Превью одной строкой: первые 60 символов + «…», если длиннее (§6.13)."""
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= size else one_line[:size] + "…"


def fmt_conclusion_line(conclusions: Sequence[AppointmentConclusion]) -> str:
    """Строка заключения в карточке: превью без типа (одно) или счётчик (несколько)."""
    if not conclusions:
        return ""
    if len(conclusions) == 1:
        return texts.AP_CONC_PREVIEW.format(preview=conclusion_preview(conclusions[0].text))
    return texts.AP_CONC_PREVIEW_MANY.format(count=len(conclusions))


_INSERT_CONCLUSION_LIMITED = text(
    """
    INSERT INTO appointment_conclusions (appointment_id, text, photo_file_id, created_at)
    SELECT :appointment_id, :text, :photo_file_id, :created_at
    WHERE (
        SELECT COUNT(*) FROM appointment_conclusions WHERE appointment_id = :appointment_id
    ) < :limit
    """
)


async def list_conclusions(
    session: AsyncSession, appointment_id: int
) -> list[AppointmentConclusion]:
    """Заключения события по порядку добавления (§6.13)."""
    result = await session.execute(
        select(AppointmentConclusion)
        .where(AppointmentConclusion.appointment_id == appointment_id)
        .order_by(AppointmentConclusion.created_at, AppointmentConclusion.id)
    )
    return list(result.scalars())


async def count_conclusions(session: AsyncSession, appointment_id: int) -> int:
    """Число заключений события (правило кнопки «📝 …», §6.13)."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(AppointmentConclusion)
                .where(AppointmentConclusion.appointment_id == appointment_id)
            )
        ).scalar_one()
    )


async def get_owned_conclusion(
    session: AsyncSession, user: User, conclusion_id: int
) -> Optional[AppointmentConclusion]:
    """Заключение текущего пользователя — через владельца события (§7.1)."""
    result = await session.execute(
        select(AppointmentConclusion)
        .join(Appointment, Appointment.id == AppointmentConclusion.appointment_id)
        .where(AppointmentConclusion.id == conclusion_id, Appointment.user_id == user.id)
    )
    return result.scalars().first()


async def add_conclusion(
    session: AsyncSession,
    appointment_id: int,
    text_value: str,
    photo_file_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Условная вставка: лимит 5 на событие; переполнение — None (§7.2, В36-А)."""
    result = await session.execute(
        _INSERT_CONCLUSION_LIMITED,
        {
            "appointment_id": appointment_id,
            "text": text_value,
            "photo_file_id": photo_file_id,
            "created_at": _db_time(now or datetime.now(timezone.utc)),
            "limit": CONCLUSION_LIMIT,
        },
    )
    return result.lastrowid if result.rowcount == 1 else None


async def set_conclusion_text(
    session: AsyncSession, user: User, conclusion_id: int, text_value: str
) -> bool:
    """Заменить текст заключения (чужое — False, §7.1)."""
    result = await session.execute(
        update(AppointmentConclusion)
        .where(
            AppointmentConclusion.id == conclusion_id,
            AppointmentConclusion.appointment_id.in_(
                select(Appointment.id).where(Appointment.user_id == user.id)
            ),
        )
        .values(text=text_value)
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


async def set_conclusion_photo(
    session: AsyncSession,
    user: User,
    conclusion_id: int,
    photo_file_id: Optional[str],
) -> bool:
    """Установить, заменить или убрать (`None`) фото заключения (чужое — False)."""
    result = await session.execute(
        update(AppointmentConclusion)
        .where(
            AppointmentConclusion.id == conclusion_id,
            AppointmentConclusion.appointment_id.in_(
                select(Appointment.id).where(Appointment.user_id == user.id)
            ),
        )
        .values(photo_file_id=photo_file_id)
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


async def remove_conclusion(
    session: AsyncSession, user: User, conclusion_id: int
) -> bool:
    """Удалить заключение (чужое — False); событие не трогается."""
    result = await session.execute(
        delete(AppointmentConclusion).where(
            AppointmentConclusion.id == conclusion_id,
            AppointmentConclusion.appointment_id.in_(
                select(Appointment.id).where(Appointment.user_id == user.id)
            ),
        )
    )
    return bool(result.rowcount)


async def conclusions_in_period(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> list[tuple[Appointment, list[AppointmentConclusion]]]:
    """События периода, у которых есть заключения, по дате события (§10.1, Р7).

    Фильтр — по `starts_on` события, не по `created_at` заключения; сортировка
    по возрастанию (как таблица отчёта).
    """
    appts = (
        (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.user_id == user.id,
                    Appointment.starts_on >= start_day,
                    Appointment.starts_on <= end_day,
                )
                .order_by(Appointment.starts_on, Appointment.starts_at, Appointment.id)
            )
        )
        .scalars()
        .all()
    )
    result: list[tuple[Appointment, list[AppointmentConclusion]]] = []
    for appt in appts:
        items = await list_conclusions(session, appt.id)
        if items:
            result.append((appt, items))
    return result
