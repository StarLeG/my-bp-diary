"""Сохранение и выборка измерений (SPEC.md, разделы 6.3, 6.4, 7).

Ключевые свойства:
- get-or-create пользователя идемпотентен (гонка двух /start не создаёт дублей);
- сохранение измерения идемпотентно: дубль по `user_id` + `timestamp` (до минуты)
  отсекается INSERT ... SELECT WHERE NOT EXISTS под общим локом — конкурентные
  параллельные сохранения не создают вторую запись;
- опциональные поля (заметка, лекарство, самочувствие) привязываются к id
  сохранённой записи, а не к «последней записи пользователя».
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.expression import Exists
from sqlalchemy.orm.exc import StaleDataError

from bot.models import Measurement, User
from bot.schemas import FeelingLiteral

if TYPE_CHECKING:
    from bot.services.reminders import RemindersService

#: Окно дедупликации: записи в одну минуту считаются одним измерением.
DEDUP_WINDOW = timedelta(minutes=1)

#: Окно правила «не дёргаем зря» (SPEC.md, раздел 6.7) — только для слотов.
SLOT_QUIET_HOURS = 3


class DuplicateMeasurementError(Exception):
    """Измерение с таким временем уже записано (дубль нажатия или повторный вызов)."""


_save_lock: Optional[asyncio.Lock] = None
_save_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _current_save_lock() -> asyncio.Lock:
    """Лок сохранений текущего event loop (SPEC.md, раздел 7.2).

    Дедуп-проверка и вставка должны быть атомарны относительно других
    сохранений. В проде event loop один; в тестах лок пересоздаётся при
    смене loop (не путать с «bound to a different event loop»).
    """
    global _save_lock, _save_lock_loop
    loop = asyncio.get_running_loop()
    if _save_lock is None or _save_lock_loop is not loop:
        _save_lock = asyncio.Lock()
        _save_lock_loop = loop
    return _save_lock


_delete_lock: Optional[asyncio.Lock] = None
_delete_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def minute_window(ts: datetime) -> tuple[datetime, datetime, datetime]:
    """Момент в UTC и границы его минуты дедупа `[start, end)` (§7.2).

    Naive-момент считается UTC.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    start = ts.replace(second=0, microsecond=0)
    return ts, start, start + DEDUP_WINDOW


def duplicate_in_minute(
    user_id: int, start: datetime, end: datetime, *, exclude_id: Optional[int] = None
) -> Exists:
    """Условие `EXISTS` «у пользователя есть запись в минуте `[start, end)`» (§7.2).

    Используется внутри одного `INSERT … SELECT … WHERE NOT EXISTS` /
    `UPDATE … WHERE NOT EXISTS`: проверка и запись атомарны. `exclude_id` —
    правимая запись, которая сама себе не дубль.
    """
    other = aliased(Measurement)
    conditions = [
        other.user_id == user_id,
        other.timestamp >= start,
        other.timestamp < end,
    ]
    if exclude_id is not None:
        conditions.append(other.id != exclude_id)
    return exists(select(literal(1)).select_from(other).where(*conditions))


def _current_delete_lock() -> asyncio.Lock:
    """Лок удалений текущего event loop (гонка двойного подтверждения, §19).

    Удаление аккаунта сериализуется в рамках процесса: проигравший гонку
    находит строку уже стёртой и получает `False` вместо «тихого» повтора.
    """
    global _delete_lock, _delete_lock_loop
    loop = asyncio.get_running_loop()
    if _delete_lock is None or _delete_lock_loop is not loop:
        _delete_lock = asyncio.Lock()
        _delete_lock_loop = loop
    return _delete_lock


def utcnow() -> datetime:
    """Текущий момент в UTC (единая точка получения «сейчас» в сервисе)."""
    return datetime.now(timezone.utc)


def day_bounds_utc(tz_name: str, day: date) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC: [начало дня, начало следующего)."""
    tz = ZoneInfo(tz_name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start_local.astimezone(timezone.utc), (start_local + timedelta(days=1)).astimezone(
        timezone.utc
    )


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, default_timezone: str = "Europe/Minsk"
) -> User:
    """Вернуть пользователя, создав при первом обращении (идемпотентно).

    При конкурентном создании (два параллельных /start) проигравший гонку
    получает уже существующую строку: IntegrityError → повторный SELECT.

    Созданная строка коммитится сразу (§7.2, U2): вызывающие хендлеры дальше
    ждут asyncio-локи (дедуп сохранений, отметки приёмов), и незакоммиченная
    вставка держала бы запись SQLite во время ожидания — взаимоблокировка с
    держателем лока. Создание пользователя идемпотентно, ранний коммит его не
    ломает.
    """
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(telegram_id=telegram_id, timezone=default_timezone)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one()
    return user


async def get_user(session: AsyncSession, telegram_id: int) -> Optional[User]:
    """Пользователь по `telegram_id` без создания записи (§19, удаление)."""
    return (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()


async def delete_user_data(
    session: AsyncSession, reminders: "RemindersService", user: User
) -> bool:
    """Удалить аккаунт (SPEC.md, §19): СНАЧАЛА job'ы, ПОТОМ `session.delete`.

    Порядок обязателен: оставшийся job упал бы с `User not found` при попытке
    отправить напоминание. Удаление выборочное (`remove_user_jobs`), а не
    `scheduler.remove_all_jobs()` — job'ы других пользователей не трогаются
    (изоляция данных, §7.1).

    Гонка двойного подтверждения безопасна: удаление идёт под локом с повторной
    проверкой строки в БД — проигравший получает `False` и ничего не трогает
    (в SQLAlchemy 2.x 0-row DELETE лишь предупреждает, а не бросает
    `StaleDataError`, поэтому одной проверки `get_user` перед вызовом мало).
    `StaleDataError` отменяется в `False` на случай внешнего вмешательства.
    """
    async with _current_delete_lock():
        found = (
            await session.execute(select(User.id).where(User.id == user.id))
        ).first()
        if found is None:
            return False
        await reminders.remove_user_jobs(user.id)
        try:
            await session.delete(user)
            await session.commit()
        except StaleDataError:
            await session.rollback()
            return False
    return True


async def save_measurement(
    session: AsyncSession,
    user: User,
    systolic: int,
    diastolic: int,
    pulse: Optional[int] = None,
    timestamp: Optional[datetime] = None,
) -> Measurement:
    """Сохранить измерение; дубль за ту же минуту — `DuplicateMeasurementError`.

    Проверка дубля и вставка идут под общим локом (`_current_save_lock`):
    два конкурентных сохранения с одним временем создают ровно одну запись.
    """
    ts, minute_start, minute_end = minute_window(timestamp or utcnow())

    async with _current_save_lock():
        duplicate = duplicate_in_minute(user.id, minute_start, minute_end)
        stmt = insert(Measurement).from_select(
            ["user_id", "timestamp", "systolic", "diastolic", "pulse"],
            select(
                literal(user.id),
                literal(ts),
                literal(systolic),
                literal(diastolic),
                literal(pulse),
            ).where(~duplicate),
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            raise DuplicateMeasurementError(
                f"измерение за {minute_start.isoformat()} уже записано"
            )
        await session.flush()
    return (
        await session.execute(
            select(Measurement).where(
                Measurement.user_id == user.id, Measurement.timestamp == ts
            )
        )
    ).scalar_one()


async def get_measurement(
    session: AsyncSession, user: User, measurement_id: int
) -> Optional[Measurement]:
    """Запись по id с проверкой владельца: чужая запись выглядит как отсутствующая."""
    return (
        await session.execute(
            select(Measurement).where(
                Measurement.id == measurement_id, Measurement.user_id == user.id
            )
        )
    ).scalar_one_or_none()


async def get_latest_measurement(
    session: AsyncSession, user: User
) -> Optional[Measurement]:
    """Последняя запись пользователя (SPEC.md, §6.3): по timestamp, при равном — по id.

    Записей нет — None. Выборка строго по user_id (§7.1).
    """
    return (
        await session.execute(
            select(Measurement)
            .where(Measurement.user_id == user.id)
            .order_by(Measurement.timestamp.desc(), Measurement.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def update_measurement_values(
    session: AsyncSession,
    user: User,
    measurement_id: int,
    systolic: int,
    diastolic: int,
    pulse: Optional[int],
    timestamp: Optional[datetime] = None,
) -> Optional[Measurement]:
    """Обновить значения записи правки (по id, с проверкой владельца; §6.3).

    Чужая/удалённая запись выглядит как отсутствующая (None).
    `pulse` записывается как есть (None — «Пропустить», полная перезапись).
    Новый момент (`timestamp`) передаётся только из личного кабинета (§16.2:
    правка меняет момент записи) — с проверкой дубля за минуту по другим
    записям; в боте момент правки не меняется (§6.3) и дедуп не применяется.
    Валидация диапазонов — на уровне схем/хендлеров (§9), CHECK БД — вторым эшелоном.

    Исключения:
        DuplicateMeasurementError: новый момент занят другой записью за минуту.
    """
    if timestamp is None:
        # Полная перезапись всех трёх полей одним UPDATE (§7.2): ORM-flush
        # пишет только поля, отличающиеся от прочитанных, а SELECT pysqlite
        # идёт вне транзакции — две параллельные правки, прочитавшие строку
        # до коммита друг друга, смешали бы наборы значений.
        result = await session.execute(
            update(Measurement)
            .where(Measurement.id == measurement_id, Measurement.user_id == user.id)
            .values(systolic=systolic, diastolic=diastolic, pulse=pulse)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            return None
    else:
        ts, minute_start, minute_end = minute_window(timestamp)
        # Проверка дубля и запись момента — одним UPDATE … WHERE NOT EXISTS под
        # общим локом сохранений: так же атомарно, как вставка в
        # `save_measurement` (§7.2), и параллельное добавление в ту же минуту
        # не проскочит между проверкой и записью.
        async with _current_save_lock():
            duplicate = duplicate_in_minute(
                user.id, minute_start, minute_end, exclude_id=measurement_id
            )
            result = await session.execute(
                update(Measurement)
                .where(
                    Measurement.id == measurement_id,
                    Measurement.user_id == user.id,
                    ~duplicate,
                )
                .values(timestamp=ts, systolic=systolic, diastolic=diastolic, pulse=pulse)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                if await get_measurement(session, user, measurement_id) is None:
                    return None
                raise DuplicateMeasurementError(
                    f"измерение за {minute_start.isoformat()} уже записано"
                )
    # Строка обновлена этой же транзакцией и существует; populate_existing
    # подтягивает в identity map значения, записанные мимо ORM.
    return (
        await session.execute(
            select(Measurement)
            .where(Measurement.id == measurement_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def delete_measurement(
    session: AsyncSession, user: User, measurement_id: int
) -> bool:
    """Удалить запись по id с проверкой владельца (§16.2).

    Связи `measurement_medications` удаляются каскадом (FK ON DELETE CASCADE),
    `medication_logs` не затрагиваются. Чужая или уже удалённая запись — False.
    """
    measurement = await get_measurement(session, user, measurement_id)
    if measurement is None:
        return False
    await session.delete(measurement)
    await session.flush()
    return True


async def add_note(
    session: AsyncSession, user: User, measurement_id: int, note: str
) -> Optional[Measurement]:
    """Добавить заметку к записи (по id записи, с проверкой владельца)."""
    measurement = await get_measurement(session, user, measurement_id)
    if measurement is None:
        return None
    measurement.note = note
    await session.flush()
    return measurement


async def set_medication_taken(
    session: AsyncSession, user: User, measurement_id: int, value: Optional[bool]
) -> Optional[Measurement]:
    """Быстрый флаг «принял лекарство» у записи (TRUE / FALSE / NULL)."""
    measurement = await get_measurement(session, user, measurement_id)
    if measurement is None:
        return None
    measurement.medication_taken = value
    await session.flush()
    return measurement


async def set_feeling(
    session: AsyncSession,
    user: User,
    measurement_id: int,
    feeling: FeelingLiteral,
    other_text: Optional[str] = None,
) -> Optional[Measurement]:
    """Самочувствие записи; при «other» текст сохраняется в note (§6.3)."""
    measurement = await get_measurement(session, user, measurement_id)
    if measurement is None:
        return None
    measurement.feeling = feeling
    if feeling == "other" and other_text:
        measurement.note = f"{measurement.note}\n{other_text}" if measurement.note else other_text
    await session.flush()
    return measurement


async def measurements_for_day(
    session: AsyncSession, user: User, day: date
) -> list[Measurement]:
    """Все измерения за локальные сутки (границы дня — в таймзоне пользователя)."""
    start, end = day_bounds_utc(user.timezone, day)
    return list(
        (
            await session.execute(
                select(Measurement)
                .where(
                    Measurement.user_id == user.id,
                    Measurement.timestamp >= start,
                    Measurement.timestamp < end,
                )
                .order_by(Measurement.timestamp)
            )
        ).scalars()
    )


async def measurements_for_period(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> list[Measurement]:
    """Измерения за период [start_day, end_day] включительно (локальные сутки)."""
    start, _ = day_bounds_utc(user.timezone, start_day)
    _, end = day_bounds_utc(user.timezone, end_day)
    return list(
        (
            await session.execute(
                select(Measurement)
                .where(
                    Measurement.user_id == user.id,
                    Measurement.timestamp >= start,
                    Measurement.timestamp < end,
                )
                .order_by(Measurement.timestamp)
            )
        ).scalars()
    )


async def measurements_all(session: AsyncSession, user: User) -> list[Measurement]:
    """Все измерения пользователя по порядку (экспорт «всё время», §10)."""
    return list(
        (
            await session.execute(
                select(Measurement)
                .where(Measurement.user_id == user.id)
                .order_by(Measurement.timestamp)
            )
        ).scalars()
    )


async def count_measurements(session: AsyncSession, user: User) -> int:
    """Всего записей пользователя (для футера «Поддержать» и лимита CSV)."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Measurement).where(
                    Measurement.user_id == user.id
                )
            )
        ).scalar_one()
    )


async def medication_taken_count(session: AsyncSession, user: User) -> int:
    """Число записей с флагом «принял лекарство» (промпт о лекарствах — один раз)."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Measurement).where(
                    Measurement.user_id == user.id,
                    Measurement.medication_taken.is_(True),
                )
            )
        ).scalar_one()
    )


async def has_recent_measurement(
    session: AsyncSession, user: User, now: Optional[datetime] = None
) -> bool:
    """Правило «не дёргаем зря»: была ли запись за последние 3 часа.

    Применяется только к слотам morning/noon/evening; на med_remeasure и
    перенесённые «Через 30 мин» не распространяется (§6.7).
    """
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    window_start = moment.astimezone(timezone.utc) - timedelta(hours=SLOT_QUIET_HOURS)
    return bool(
        (
            await session.execute(
                select(literal(1)).where(
                    Measurement.user_id == user.id,
                    Measurement.timestamp >= window_start,
                )
            )
        ).first()
    )
