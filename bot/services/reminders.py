"""Планировщик напоминаний: APScheduler 3.x поверх строк `reminders` (SPEC.md, §6.7).

Архитектура:
- строка `reminders` — источник истины по расписанию; job'ы APScheduler —
  механизм доставки и пересоздаются из строк при старте;
- слот — цепочка одноразовых `DateTrigger`-job'ов: сработал → отправил →
  пересчитал следующий локальный `HH:MM` → новый job;
- планировщик строго `timezone="UTC"` (DST-ловушка ZoneInfo в APScheduler 3.x);
  локальное время конвертируется в UTC здесь, через `zoneinfo`;
- «Через 30 мин» (snooze) — не новая строка: `next_trigger_time` существующей
  строки перезаписывается транзакционно (optimistic locking), job пересоздаётся
  с признаком «snooze» (правило «3 часов» на него не распространяется);
- доставка атомарна: захват строки `UPDATE … WHERE next_trigger_time=?`
  до отправки — двойной тик не отправит напоминание дважды;
- повтор замера (`med_remeasure`) после отправки не удаляется, а помечается
  доставленным (`enabled=False`): кнопки сообщения «Через 30 мин» и «Больше не
  напоминать» действуют по id этой строки. Snooze снова включает её, «Больше
  не напоминать» удаляет; при следующей доставке или постановке повтора
  прежние доставленные строки пользователя убираются (не копятся);
- catch-up при старте: просроченное отправляется один раз, не чаще 1 раза
  в день на тип (`last_sent_date`); ограничение действует только на catch-up,
  snooze в тот же день доставляется.
- точка входа job'а — module-level `fire_reminder`, а не bound method:
  `SQLAlchemyJobStore` сериализует job через `pickle`, а bound method тащит
  весь сервис (включая `_scheduler`) и не сериализуется; экземпляр сервиса
  привязывается через `bind_service`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from bot import keyboards, texts
from bot.models import Reminder, User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service

logger = logging.getLogger(__name__)

#: Время слотов по умолчанию: «Пропустить» онбординга (§6.1) и предотметка
#: лекарств в форме записи кабинета, если напоминание слота выключено (§16.2).
DEFAULT_SLOT_TIMES = {"morning": "08:00", "noon": "13:00", "evening": "21:00"}

#: Тип строки `reminders` для одноразового повтора замера (CHECK в БД).
TYPE_REMEASURE = "med_remeasure"

#: Режимы срабатывания job'а (хранятся в args job'а).
MODE_SLOT = "slot"
MODE_SNOOZE = "snooze"
MODE_REMEASURE = "remeasure"
MODE_CATCHUP = "catchup"

#: Дефолт snooze — «Через 30 мин».
SNOOZE_MINUTES = 30

#: Отправка готового сообщения: (telegram_id, текст, клавиатура).
Sender = Callable[[int, str, Optional[object]], Awaitable[None]]

_remeasure_lock: Optional[asyncio.Lock] = None
_remeasure_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _current_remeasure_lock() -> asyncio.Lock:
    """Лок создания повторов замера текущего event loop (SPEC.md, раздел 7.2).

    Проверка «повтор уже есть» и вставка должны быть атомарны. Для повтора,
    не привязанного к записи (`measurement_id IS NULL`), UNIQUE по
    `measurement_id` не действует (NULL не равны NULL), и без лока двойное
    «Принял всё ✓» создаёт два повтора. В проде event loop один; в тестах
    лок пересоздаётся при смене loop.
    """
    global _remeasure_lock, _remeasure_lock_loop
    loop = asyncio.get_running_loop()
    if _remeasure_lock is None or _remeasure_lock_loop is not loop:
        _remeasure_lock = asyncio.Lock()
        _remeasure_lock_loop = loop
    return _remeasure_lock


def next_slot_occurrence(local_time: str, tz_name: str, now: datetime) -> datetime:
    """Ближайший момент HH:MM в таймзоне пользователя, строго после `now` (UTC)."""
    tz = ZoneInfo(tz_name)
    hour, minute = map(int, local_time.split(":"))
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def enable_slot_row(
    session: AsyncSession, reminder: Reminder, local_time: str, next_time: datetime
) -> None:
    """Включить существующую строку слота: время, следующий момент и `enabled` — одним UPDATE.

    Не через атрибуты прочитанного объекта (§7.2): flush пишет только поля,
    отличающиеся от прочитанных, и две параллельные перенастройки слота,
    прочитавшие строку до коммита друг друга, оставили бы `local_time` от
    одной и `next_trigger_time` от другой — напоминание пришло бы не во
    время настройки. Объект в identity map обновляется без пометки «грязный».
    """
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder.id)
        .values(local_time=local_time, next_trigger_time=next_time, enabled=True)
        .execution_options(synchronize_session=False)
    )
    set_committed_value(reminder, "local_time", local_time)
    set_committed_value(reminder, "next_trigger_time", next_time)
    set_committed_value(reminder, "enabled", True)


class RemindersService:
    """Управление строками `reminders` и job'ами APScheduler.

    `sender` — асинхронный колбэк доставки сообщения (bot.send_message).
    `scheduler` передаётся извне (в тестах — лёгкий фейк с add_job/remove_job).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sender: Sender,
        scheduler=None,
    ) -> None:
        self._session_factory = session_factory
        self._sender = sender
        self._scheduler = scheduler

    # ─────────────────────────── Вспомогательное ───────────────────────────

    def _job_id(self, reminder_id: int) -> str:
        return f"reminder:{reminder_id}"

    def _add_job(self, reminder_id: int, run_date: datetime, mode: str) -> None:
        """Поставить одноразовый DateTrigger-job (или переставить существующий).

        Точка входа — module-level `fire_reminder` (см. докстринг модуля):
        bound method `self._fire` не сериализуется в `SQLAlchemyJobStore`.
        """
        self._scheduler.add_job(
            fire_reminder,
            trigger=DateTrigger(run_date=run_date, timezone="UTC"),
            id=self._job_id(reminder_id),
            args=[reminder_id, mode],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            timezone="UTC",
        )

    def _remove_job(self, reminder_id: int) -> None:
        """Снять job; отсутствующий job — норма (мог уже сработать и удалиться)."""
        try:
            self._scheduler.remove_job(self._job_id(reminder_id))
        except JobLookupError:
            pass

    # ─────────────────────────── Постановка напоминаний ───────────────────────────

    async def ensure_slot(
        self, user_id: int, slot: str, local_time: str, tz_name: str
    ) -> Reminder:
        """Включить/перенастроить слот: строка + job (идемпотентно)."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.user_id == user_id, Reminder.type == slot
                    )
                )
            ).scalar_one_or_none()
            next_time = next_slot_occurrence(local_time, tz_name, now)
            if reminder is None:
                reminder = Reminder(
                    user_id=user_id,
                    type=slot,
                    local_time=local_time,
                    next_trigger_time=next_time,
                    enabled=True,
                )
                session.add(reminder)
            else:
                await enable_slot_row(session, reminder, local_time, next_time)
            await session.commit()
            reminder_id = reminder.id
        self._add_job(reminder_id, next_time, MODE_SLOT)
        return reminder

    async def disable_slot(self, user_id: int, slot: str) -> bool:
        """Выключить слот (строка остаётся, job снимается)."""
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.user_id == user_id, Reminder.type == slot
                    )
                )
            ).scalar_one_or_none()
            if reminder is None:
                return False
            reminder.enabled = False
            await session.commit()
            reminder_id = reminder.id
        self._remove_job(reminder_id)
        return True

    async def ensure_remeasure(
        self, user_id: int, measurement_id: Optional[int], delay_minutes: int
    ) -> bool:
        """Поставить одноразовый повтор замера; идемпотентно по `measurement_id`.

        `measurement_id=None` — повтор не привязан к записи (отметка «Принял всё
        ✓» в слоте): активным считается один такой повтор на пользователя.
        Возвращает False, если подходящий активный повтор уже есть (двойное
        «Приняли лекарство?» → «Да» второй job не плодит). Проверка и вставка
        идут под локом (`_current_remeasure_lock`): конкурентный проигравший
        получает `False`, а не второй повтор.
        """
        now = datetime.now(timezone.utc)
        async with _current_remeasure_lock():
            async with self._session_factory() as session:
                query = select(Reminder).where(Reminder.type == TYPE_REMEASURE)
                if measurement_id is None:
                    query = query.where(
                        Reminder.user_id == user_id, Reminder.measurement_id.is_(None)
                    )
                else:
                    query = query.where(Reminder.measurement_id == measurement_id)
                existing = list((await session.execute(query)).scalars())
                if any(row.enabled for row in existing):
                    return False
                # Доставленные повторы (enabled=False) не мешают новому: убираем
                # их, иначе уникальность по measurement_id не даст вставить строку.
                for row in existing:
                    await session.delete(row)
                await session.flush()
                reminder = Reminder(
                    user_id=user_id,
                    type=TYPE_REMEASURE,
                    local_time=None,
                    next_trigger_time=now + timedelta(minutes=delay_minutes),
                    enabled=True,
                    measurement_id=measurement_id,
                )
                session.add(reminder)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    return False  # конкурентно уже создали повтор (UNIQUE по measurement_id)
                reminder_id = reminder.id
                run_date = reminder.next_trigger_time
        self._add_job(reminder_id, run_date, MODE_REMEASURE)
        return True

    async def skip_slot(self, user_id: int, slot: str) -> bool:
        """«Пропускаю»: тихо перепланировать слот на следующее вхождение (§6.7).

        Расписание не ломается: строка переносится на ближайший `HH:MM`
        (обычно завтра), job пересоздаётся, уведомления нет. Возвращает False,
        если слот не настроен или выключен.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.user_id == user_id,
                        Reminder.type == slot,
                        Reminder.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if reminder is None:
                return False
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            old_time = reminder.next_trigger_time
            next_time = next_slot_occurrence(reminder.local_time, user.timezone, now)
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder.id,
                    Reminder.next_trigger_time == old_time,
                )
                .values(next_trigger_time=next_time)
            )
            await session.commit()
            if result.rowcount == 0:
                return False  # уже перенесли или обработали параллельно
            reminder_id = reminder.id
        self._add_job(reminder_id, next_time, MODE_SLOT)
        return True

    async def snooze(self, user_id: int, reminder_id: int) -> bool:
        """«Через 30 мин»: перезапись `next_trigger_time` существующей строки.

        Optimistic locking: если строку за это время уже перенесли или
        обработали (next_trigger_time изменился) — повтор не применяется.
        Регулярное расписание слота сохраняется (после срабатывания — следующий).
        """
        now = datetime.now(timezone.utc)
        new_time = now + timedelta(minutes=SNOOZE_MINUTES)
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.id == reminder_id, Reminder.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            if reminder is None:
                return False
            # Слот — только включённый; повтор замера после доставки выключен
            # (enabled=False) и по «Через 30 мин» включается снова (§6.7).
            if not reminder.enabled and reminder.type != TYPE_REMEASURE:
                return False
            result = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder_id,
                    Reminder.user_id == user_id,
                    Reminder.next_trigger_time == reminder.next_trigger_time,
                )
                .values(next_trigger_time=new_time, enabled=True)
            )
            await session.commit()
            if result.rowcount == 0:
                return False
        self._add_job(reminder_id, new_time, MODE_SNOOZE)
        return True

    async def cancel_remeasure(self, user_id: int, reminder_id: int) -> bool:
        """«Больше не напоминать»: удалить строку повтора и снять job."""
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.id == reminder_id,
                        Reminder.user_id == user_id,
                        Reminder.type == TYPE_REMEASURE,
                    )
                )
            ).scalar_one_or_none()
            if reminder is None:
                return False
            await session.delete(reminder)
            await session.commit()
        self._remove_job(reminder_id)
        return True

    async def cancel_remeasure_for_measurement(
        self, user_id: int, measurement_id: int
    ) -> bool:
        """«Не надо» для записи: удалить активный повтор замера и снять job.

        Чужая запись выглядит как отсутствующая (фильтр по `user_id`).
        Возвращает False, если активного повтора для этой записи нет —
        повторное нажатие безопасно.
        """
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.user_id == user_id,
                        Reminder.type == TYPE_REMEASURE,
                        Reminder.measurement_id == measurement_id,
                    )
                )
            ).scalar_one_or_none()
            if reminder is None:
                return False
            await session.delete(reminder)
            await session.commit()
            reminder_id = reminder.id
        self._remove_job(reminder_id)
        return True

    async def remove_user_jobs(self, user_id: int) -> int:
        """Снять все job'ы пользователя — перед удалением аккаунта (SPEC.md, §19).

        Удаление выборочное, а не `scheduler.remove_all_jobs()`: job'ы других
        пользователей должны остаться (изоляция данных, §7.1). Возвращает
        количество снятых напоминаний.
        """
        async with self._session_factory() as session:
            reminder_ids = (
                (
                    await session.execute(
                        select(Reminder.id).where(Reminder.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
        for reminder_id in reminder_ids:
            self._remove_job(reminder_id)
        return len(reminder_ids)

    # ─────────────────────────── Доставка ───────────────────────────

    async def _fire(self, reminder_id: int, mode: str) -> None:
        """Точка входа job'а: атомарный захват строки → отправка → перепланировка."""
        try:
            await self._deliver(reminder_id, mode)
        except Exception:  # noqa: BLE001 — job не должен ронять планировщик
            logger.exception("Ошибка доставки напоминания %s", reminder_id)

    async def _deliver(self, reminder_id: int, mode: str) -> bool:
        """Основная логика доставки; возвращает True, если сообщение отправлено."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            reminder = (
                await session.execute(
                    select(Reminder).where(Reminder.id == reminder_id)
                )
            ).scalar_one_or_none()
            if reminder is None or not reminder.enabled:
                return False
            user = (
                await session.execute(select(User).where(User.id == reminder.user_id))
            ).scalar_one()
            local_today = now.astimezone(ZoneInfo(user.timezone)).date()

            if reminder.type != TYPE_REMEASURE and reminder.next_trigger_time > now:
                return False  # строка уже обработана: повторный или ложный тик

            from bot.services import consent as consent_service

            if not await consent_service.has_processing(session, user.id):
                # Без действующего согласия напоминания не отправляются (§6.11):
                # слот молча переносится на следующий день, расписание не меняется.
                if reminder.type == TYPE_REMEASURE:
                    return False
                next_time = await self._silently_reschedule(session, reminder, user, now)
                await session.commit()
                self._add_job(reminder.id, next_time, MODE_SLOT)
                return False

            if reminder.type == TYPE_REMEASURE:
                # «Не чаще 1/день» — только для catch-up; после обычной доставки
                # строка удаляется, поэтому ветка достижима лишь при ручном сбое.
                if mode == MODE_CATCHUP and reminder.last_sent_date == local_today:
                    return False
            else:
                # Правило «не дёргаем зря» — только для регулярных слотов;
                # ограничение «1/день» — только для catch-up.
                quiet = mode == MODE_SLOT and await measurements_service.has_recent_measurement(
                    session, user, now
                )
                already_sent = (
                    mode == MODE_CATCHUP and reminder.last_sent_date == local_today
                )
                if quiet or already_sent:
                    next_time = await self._silently_reschedule(
                        session, reminder, user, now
                    )
                    await session.commit()
                    self._add_job(reminder.id, next_time, MODE_SLOT)
                    return False

            # Атомарный захват: строка меняется только если не изменилась с чтения.
            old_time = reminder.next_trigger_time
            if reminder.type == TYPE_REMEASURE:
                result = await session.execute(
                    update(Reminder)
                    .where(
                        Reminder.id == reminder_id,
                        Reminder.next_trigger_time == old_time,
                        Reminder.enabled.is_(True),
                    )
                    .values(enabled=False, last_sent_date=local_today)
                )
                if result.rowcount:
                    # Прежние доставленные повторы пользователя больше не нужны:
                    # кнопки действуют по последнему сообщению.
                    await session.execute(
                        delete(Reminder).where(
                            Reminder.user_id == reminder.user_id,
                            Reminder.type == TYPE_REMEASURE,
                            Reminder.enabled.is_(False),
                            Reminder.id != reminder_id,
                        )
                    )
                next_time = now
            else:
                next_time = next_slot_occurrence(reminder.local_time, user.timezone, now)
                result = await session.execute(
                    update(Reminder)
                    .where(
                        Reminder.id == reminder_id,
                        Reminder.next_trigger_time == old_time,
                    )
                    .values(
                        next_trigger_time=next_time,
                        last_sent_date=local_today,
                    )
                )
            if result.rowcount == 0:
                await session.rollback()
                return False  # уже обработано параллельно
            await session.commit()
            reminder_type = reminder.type
            user_id = user.id
            telegram_id = user.telegram_id

        text, markup = await self._compose(reminder_id, reminder_type, user_id)
        await self._sender(telegram_id, text, markup)

        if reminder_type != TYPE_REMEASURE:
            self._add_job(reminder.id, next_time, MODE_SLOT)
        return True

    async def _silently_reschedule(
        self, session: AsyncSession, reminder: Reminder, user: User, now: datetime
    ) -> datetime:
        """Тихий пропуск (правило «3 часов» / уже отправлено): следующий день.

        Возвращает новое `next_trigger_time` (job переставляет вызывающий).
        """
        next_time = next_slot_occurrence(reminder.local_time, user.timezone, now)
        await session.execute(
            update(Reminder)
            .where(
                Reminder.id == reminder.id,
                Reminder.next_trigger_time == reminder.next_trigger_time,
            )
            .values(next_trigger_time=next_time)
        )
        return next_time

    async def _compose(
        self, reminder_id: int, reminder_type: str, user_id: int
    ) -> tuple[str, object]:
        """Собрать сообщение: слот с лекарствами или напоминание повторить измерение.

        Клавиатуры несут id строки: «Через 30 мин» и «Больше не напоминать»
        действуют именно по этой строке (§6.7).
        """
        async with self._session_factory() as session:
            if reminder_type == TYPE_REMEASURE:
                return texts.REMEASURE_MESSAGE, keyboards.remeasure_kb(reminder_id)
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            meds = await medications_service.slot_medications(session, user, reminder_type)
            lines = [texts.fmt_med_item(m.name, m.dosage) for m in meds]
            return (
                texts.fmt_slot_message(reminder_type, lines),
                keyboards.slot_message_kb(reminder_type, reminder_id, has_medications=bool(meds)),
            )

    # ─────────────────────────── Catch-up при старте ───────────────────────────

    async def startup(self) -> int:
        """Пересоздать job'ы из строк и доставить просроченное (один раз на тип).

        Возвращает число catch-up отправок.
        """
        now = datetime.now(timezone.utc)
        sent = 0
        async with self._session_factory() as session:
            reminders = list(
                (
                    await session.execute(
                        select(Reminder).where(Reminder.enabled.is_(True))
                    )
                ).scalars()
            )
        for reminder in reminders:
            if reminder.next_trigger_time <= now:
                if await self._deliver(reminder.id, MODE_CATCHUP):
                    sent += 1
            else:
                mode = MODE_REMEASURE if reminder.type == TYPE_REMEASURE else MODE_SLOT
                self._add_job(reminder.id, reminder.next_trigger_time, mode)
        return sent


# ─────────────────── Точка входа job'а (module-level) ───────────────────

#: Привязанный экземпляр сервиса. Job'ы переживают рестарты через
#: `SQLAlchemyJobStore`, поэтому точка входа — сериализуемая по имени
#: module-level функция, а не bound method (см. докстринг модуля).
_service: Optional[RemindersService] = None


def bind_service(service: Optional[RemindersService]) -> None:
    """Привязать экземпляр сервиса к job-точке входа `fire_reminder`.

    Вызывается при сборке приложения (`bot.main.build_app`) и в тестах.
    `None` отвязывает сервис: сработавший без привязки job логируется
    и игнорируется, планировщик не падает.
    """
    global _service
    _service = service


async def fire_reminder(reminder_id: int, mode: str) -> None:
    """Точка входа job'а: сериализуема в `SQLAlchemyJobStore` (SPEC.md, §6.7).

    Делегирует привязанному `RemindersService._fire`. Без привязки — ошибка
    в лог, исключение наружу не выпускается (job не должен ронять планировщик).
    """
    if _service is None:
        logger.error(
            "Сработал job напоминания %s без привязанного сервиса (bind_service)",
            reminder_id,
        )
        return
    await _service._fire(reminder_id, mode)
