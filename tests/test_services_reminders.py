"""Тесты сервиса напоминаний (bot/services/reminders.py): APScheduler-обёртка,
цепочка DateTrigger-job'ов, catch-up, snooze, атомарная доставка (SPEC.md, §6.7)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, Reminder, User
from bot.services import medications as meds_service
from bot.services import reminders as rem

UTC = timezone.utc
MINSK = ZoneInfo("Europe/Minsk")


def now_utc() -> datetime:
    return datetime.now(UTC)


def today_minsk() -> date:
    return datetime.now(MINSK).date()


class FakeScheduler:
    """Лёгкий фейк APScheduler: фиксирует add_job/remove_job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, **kwargs) -> str:
        self.jobs[kwargs["id"]] = {"func": func, **kwargs}
        return kwargs["id"]

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def get_job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def sent() -> list[tuple[int, str, object]]:
    return []


@pytest.fixture
def svc(session_factory, scheduler, sent) -> rem.RemindersService:
    async def sender(user_id: int, text: str, markup) -> None:
        sent.append((user_id, text, markup))

    return rem.RemindersService(session_factory, sender, scheduler)


def kb_texts(markup) -> list[str]:
    """Тексты всех кнопок клавиатуры."""
    return [button.text for row in markup.inline_keyboard for button in row]


async def reload_reminder(session: AsyncSession, reminder_id: int) -> Reminder | None:
    """Перечитать строку напоминания из БД (обходит identity map сессии)."""
    return (
        await session.execute(
            select(Reminder)
            .where(Reminder.id == reminder_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def add_measurement(session: AsyncSession, user: User, when: datetime) -> Measurement:
    measurement = Measurement(
        user_id=user.id,
        timestamp=when,
        systolic=135,
        diastolic=85,
        pulse=72,
    )
    session.add(measurement)
    await session.flush()
    return measurement


async def make_slot(
    session: AsyncSession,
    user: User,
    slot: str = "morning",
    local_time: str = "08:00",
    next_time: datetime | None = None,
    enabled: bool = True,
    last_sent: date | None = None,
) -> Reminder:
    reminder = Reminder(
        user_id=user.id,
        type=slot,
        local_time=local_time,
        next_trigger_time=next_time or now_utc() + timedelta(hours=1),
        enabled=enabled,
        last_sent_date=last_sent,
    )
    session.add(reminder)
    await session.flush()
    return reminder


async def make_remeasure(
    session: AsyncSession,
    user: User,
    measurement_id: int,
    next_time: datetime | None = None,
    last_sent: date | None = None,
) -> Reminder:
    reminder = Reminder(
        user_id=user.id,
        type="med_remeasure",
        local_time=None,
        next_trigger_time=next_time or now_utc() + timedelta(minutes=60),
        enabled=True,
        measurement_id=measurement_id,
        last_sent_date=last_sent,
    )
    session.add(reminder)
    await session.flush()
    return reminder


# ─────────────────────────── next_slot_occurrence ───────────────────────────


class TestNextSlotOccurrence:
    def test_before_time_same_day(self) -> None:
        now = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
        result = rem.next_slot_occurrence("08:00", "UTC", now)
        assert result == datetime(2026, 11, 4, 8, 0, tzinfo=UTC)

    def test_after_time_tomorrow(self) -> None:
        now = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        result = rem.next_slot_occurrence("08:00", "UTC", now)
        assert result == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)

    def test_exactly_at_time_goes_tomorrow(self) -> None:
        # Срабатывание в момент HH:MM — следующий строго после now.
        now = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
        result = rem.next_slot_occurrence("08:00", "UTC", now)
        assert result == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)

    def test_non_utc_timezone(self) -> None:
        # 05:00 UTC = 08:00 в Минске; до 08:30 ещё есть время — тот же день.
        now = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
        result = rem.next_slot_occurrence("08:30", "Europe/Minsk", now)
        assert result == datetime(2026, 11, 4, 5, 30, tzinfo=UTC)


# ─────────────────────────── ensure_slot / disable_slot ───────────────────────────


class TestEnsureSlot:
    async def test_creates_row_and_job(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        reminder = await svc.ensure_slot(user.id, "morning", "08:30", "Europe/Minsk")
        assert reminder.id is not None
        assert reminder.enabled is True
        assert reminder.local_time == "08:30"

        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert job is not None
        assert job["args"] == [reminder.id, rem.MODE_SLOT]
        assert job["trigger"].run_date == reminder.next_trigger_time
        assert job["coalesce"] is True
        assert job["max_instances"] == 1
        assert job["misfire_grace_time"] == 300

    async def test_reconfigure_keeps_single_row(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        first = await svc.ensure_slot(user.id, "morning", "08:30", "Europe/Minsk")
        second = await svc.ensure_slot(user.id, "morning", "09:15", "Europe/Minsk")
        assert second.id == first.id
        assert second.local_time == "09:15"
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1

    async def test_reenables_disabled_slot(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        await svc.disable_slot(user.id, "morning")
        reminder = await svc.ensure_slot(user.id, "morning", "08:30", "Europe/Minsk")
        assert reminder.enabled is True


class TestDisableSlot:
    async def test_disables_and_removes_job(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        reminder = await svc.ensure_slot(user.id, "morning", "08:30", "Europe/Minsk")
        assert await svc.disable_slot(user.id, "morning") is True
        assert scheduler.get_job(f"reminder:{reminder.id}") is None
        row = (
            await session.execute(select(Reminder).where(Reminder.id == reminder.id))
        ).scalar_one()
        assert row.enabled is False

    async def test_unknown_slot_returns_false(self, user: User, svc) -> None:
        assert await svc.disable_slot(user.id, "evening") is False


# ─────────────────────────── ensure_remeasure ───────────────────────────


class TestEnsureRemeasure:
    async def test_creates_row_and_job(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        before = now_utc()
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is True

        row = (
            await session.execute(select(Reminder))
        ).scalars().one()
        assert row.type == "med_remeasure"
        assert row.measurement_id == measurement.id
        assert before + timedelta(minutes=60) <= row.next_trigger_time <= now_utc() + timedelta(minutes=60)

        job = scheduler.get_job(f"reminder:{row.id}")
        assert job is not None
        assert job["args"] == [row.id, rem.MODE_REMEASURE]

    async def test_second_call_is_idempotent(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is True
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is False
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1

    async def test_unbound_remeasure_created(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        """«Принял всё ✓»: повтор без привязки к записи (§6.7)."""
        assert await svc.ensure_remeasure(user.id, None, 60) is True

        row = (await session.execute(select(Reminder))).scalars().one()
        assert row.measurement_id is None
        assert row.user_id == user.id
        assert row.type == "med_remeasure"

    async def test_unbound_second_call_is_idempotent(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        assert await svc.ensure_remeasure(user.id, None, 60) is True
        assert await svc.ensure_remeasure(user.id, None, 60) is False
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1

    async def test_unbound_does_not_block_bound(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        assert await svc.ensure_remeasure(user.id, None, 60) is True
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is True
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 2


# ─────────────────────────── snooze ───────────────────────────


class TestSnooze:
    async def test_reschedules_in_30_minutes(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() + timedelta(hours=1))
        await session.commit()
        before = now_utc()
        assert await svc.snooze(user.id, reminder.id) is True

        row = await reload_reminder(session, reminder.id)
        assert before + timedelta(minutes=30) <= row.next_trigger_time <= now_utc() + timedelta(minutes=30)
        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert job is not None
        assert job["args"] == [reminder.id, rem.MODE_SNOOZE]

    async def test_unknown_reminder_returns_false(self, user: User, svc) -> None:
        assert await svc.snooze(user.id, 999) is False

    async def test_other_user_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        reminder = await make_slot(session, user)
        await session.commit()
        other = User(telegram_id=222_222)
        session.add(other)
        await session.commit()
        assert await svc.snooze(other.id, reminder.id) is False

    async def test_disabled_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        reminder = await make_slot(session, user, enabled=False)
        await session.commit()
        assert await svc.snooze(user.id, reminder.id) is False

    async def test_concurrent_snooze_applies_once(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() + timedelta(hours=1))
        await session.commit()
        results = await asyncio.gather(
            svc.snooze(user.id, reminder.id),
            svc.snooze(user.id, reminder.id),
        )
        assert sorted(results) == [False, True]


# ─────────────────────────── skip_slot ───────────────────────────


class TestSkipSlot:
    async def test_moves_to_next_occurrence(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        reminder = await make_slot(session, user, local_time="08:00")
        await session.commit()
        before = now_utc()
        assert await svc.skip_slot(user.id, "morning") is True

        row = await reload_reminder(session, reminder.id)
        expected = rem.next_slot_occurrence("08:00", user.timezone, before)
        assert row.next_trigger_time == expected
        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert job is not None
        assert job["args"] == [reminder.id, rem.MODE_SLOT]

    async def test_unknown_slot_returns_false(self, user: User, svc) -> None:
        assert await svc.skip_slot(user.id, "morning") is False

    async def test_disabled_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        await make_slot(session, user, enabled=False)
        await session.commit()
        assert await svc.skip_slot(user.id, "morning") is False

    async def test_other_user_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        await make_slot(session, user)
        await session.commit()
        other = User(telegram_id=222_222)
        session.add(other)
        await session.commit()
        assert await svc.skip_slot(other.id, "morning") is False

    async def test_concurrent_skip_applies_once(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        await make_slot(session, user)
        await session.commit()
        results = await asyncio.gather(
            svc.skip_slot(user.id, "morning"),
            svc.skip_slot(user.id, "morning"),
        )
        assert sorted(results) == [False, True]


# ─────────────────────────── cancel_remeasure ───────────────────────────


class TestCancelRemeasure:
    async def test_deletes_row_and_job(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        await svc.ensure_remeasure(user.id, measurement.id, 60)
        row = (await session.execute(select(Reminder))).scalars().one()
        assert await svc.cancel_remeasure(user.id, row.id) is True
        assert scheduler.get_job(f"reminder:{row.id}") is None
        assert (await session.execute(select(Reminder))).scalars().all() == []

    async def test_unknown_returns_false(self, user: User, svc) -> None:
        assert await svc.cancel_remeasure(user.id, 999) is False

    async def test_slot_type_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        reminder = await make_slot(session, user)
        await session.commit()
        assert await svc.cancel_remeasure(user.id, reminder.id) is False
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1


class TestCancelRemeasureForMeasurement:
    async def test_deletes_row_and_job(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        await svc.ensure_remeasure(user.id, measurement.id, 60)
        row = (await session.execute(select(Reminder))).scalars().one()
        assert await svc.cancel_remeasure_for_measurement(user.id, measurement.id) is True
        assert scheduler.get_job(f"reminder:{row.id}") is None
        assert (await session.execute(select(Reminder))).scalars().all() == []

    async def test_unknown_measurement_returns_false(self, user: User, svc) -> None:
        assert await svc.cancel_remeasure_for_measurement(user.id, 999) is False

    async def test_foreign_measurement_returns_false(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        measurement = await add_measurement(session, other, now_utc())
        await session.commit()
        await svc.ensure_remeasure(other.id, measurement.id, 60)
        # Чужая запись выглядит как отсутствующая; строка повтора остаётся.
        assert await svc.cancel_remeasure_for_measurement(user.id, measurement.id) is False
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1

    async def test_repeat_press_is_safe(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        await svc.ensure_remeasure(user.id, measurement.id, 60)
        assert await svc.cancel_remeasure_for_measurement(user.id, measurement.id) is True
        assert await svc.cancel_remeasure_for_measurement(user.id, measurement.id) is False

    async def test_missing_job_is_ignored(
        self, session: AsyncSession, user: User, session_factory, sent
    ) -> None:
        """Job мог уже сработать и удалиться — JobLookupError не должен всплывать."""

        class StrictScheduler(FakeScheduler):
            def remove_job(self, job_id: str) -> None:
                if job_id not in self.jobs:
                    raise JobLookupError(job_id)
                del self.jobs[job_id]

        async def sender(user_id: int, text: str, markup) -> None:
            sent.append((user_id, text, markup))

        svc = rem.RemindersService(session_factory, sender, StrictScheduler())
        measurement = await add_measurement(session, user, now_utc())
        await session.commit()
        await svc.ensure_remeasure(user.id, measurement.id, 60)
        row = (await session.execute(select(Reminder))).scalars().one()
        # Job снят вручную — сервис всё равно удалит строку и не упадёт.
        svc._scheduler.jobs.clear()
        assert await svc.cancel_remeasure_for_measurement(user.id, measurement.id) is True
        assert (await session.execute(select(Reminder))).scalars().all() == []


# ─────────────────────────── Доставка слота ───────────────────────────


class TestDeliverSlot:
    async def test_without_medications(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT) is True

        assert len(sent) == 1
        chat_id, text, markup = sent[0]
        assert chat_id == user.telegram_id
        assert text == texts.fmt_slot_message("morning", [])
        assert texts.BTN_TAKE_ALL not in kb_texts(markup)
        assert texts.BTN_MEASURE in kb_texts(markup)

        row = await reload_reminder(session, reminder.id)
        assert row.last_sent_date == today_minsk()
        assert row.next_trigger_time > now_utc()
        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert job is not None
        assert job["args"] == [reminder.id, rem.MODE_SLOT]

    async def test_with_medications(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        await meds_service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT) is True

        _, text, markup = sent[0]
        assert "💊 Лекарства: Лозартан 50 мг" in text
        assert texts.BTN_TAKE_ALL in kb_texts(markup)
        assert texts.BTN_PARTIAL in kb_texts(markup)

    async def test_quiet_rule_skips_send(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        # Правило «3 часов»: свежее измерение — слот не отправляем.
        await add_measurement(session, user, now_utc() - timedelta(minutes=30))
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT) is False

        assert sent == []
        row = await reload_reminder(session, reminder.id)
        assert row.next_trigger_time > now_utc()
        assert scheduler.get_job(f"reminder:{reminder.id}") is not None

    async def test_missing_reminder_returns_false(self, svc, sent) -> None:
        assert await svc._deliver(999, rem.MODE_SLOT) is False
        assert sent == []

    async def test_disabled_reminder_returns_false(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        reminder = await make_slot(session, user, enabled=False)
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT) is False
        assert sent == []


# ─────────────────────────── Доставка med_remeasure ───────────────────────────


class TestDeliverRemeasure:
    async def test_sends_and_keeps_row_disabled(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        """Строка не удаляется: кнопки сообщения действуют по её id (§6.7)."""
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(
            session, user, measurement.id, next_time=now_utc() - timedelta(minutes=1)
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE) is True

        assert len(sent) == 1
        _, text, markup = sent[0]
        assert text == texts.REMEASURE_MESSAGE
        assert texts.BTN_REMEASURE_NOW in kb_texts(markup)
        [row] = (await session.execute(select(Reminder))).scalars().all()
        await session.refresh(row)
        assert row.id == reminder.id and row.enabled is False
        assert scheduler.get_job(f"reminder:{reminder.id}") is None
        # повторный тик по доставленной строке ничего не шлёт
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE) is False
        assert len(sent) == 1

    async def test_not_blocked_by_quiet_rule(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        # Правило «3 часов» не распространяется на med_remeasure.
        await add_measurement(session, user, now_utc() - timedelta(minutes=5))
        measurement = (
            await session.execute(select(Measurement))
        ).scalars().first()
        reminder = await make_remeasure(
            session, user, measurement.id, next_time=now_utc() - timedelta(minutes=1)
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE) is True
        assert len(sent) == 1

    async def test_catchup_skips_if_already_sent_today(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        # Строка уже «отправлялась» сегодня (ручной сбой) — catch-up не дублирует.
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(
            session,
            user,
            measurement.id,
            next_time=now_utc() - timedelta(hours=1),
            last_sent=today_minsk(),
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_CATCHUP) is False
        assert sent == []
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1  # строка цела


# ─────────────────────────── Режимы snooze / catch-up ───────────────────────────


class TestDeliverModes:
    async def test_snooze_ignores_quiet_rule(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        # «Через 30 мин» доставляется даже при свежем измерении.
        await add_measurement(session, user, now_utc() - timedelta(minutes=5))
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SNOOZE) is True
        assert len(sent) == 1

    async def test_catchup_skips_if_slot_already_sent_today(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder = await make_slot(
            session,
            user,
            next_time=now_utc() - timedelta(hours=5),
            last_sent=today_minsk(),
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_CATCHUP) is False
        assert sent == []
        row = await reload_reminder(session, reminder.id)
        assert row.next_trigger_time > now_utc()
        assert scheduler.get_job(f"reminder:{reminder.id}") is not None


# ─────────────────────────── Защита от двойной доставки ───────────────────────────


class TestDoubleDelivery:
    async def test_sequential_second_call_sends_nothing(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        first = await svc._deliver(reminder.id, rem.MODE_SLOT)
        second = await svc._deliver(reminder.id, rem.MODE_SLOT)
        # Второй тик в тот же день ловится ограничением «1 раз в день».
        assert (first, second) == (True, False)
        assert len(sent) == 1

    async def test_concurrent_delivery_sends_once(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        results = await asyncio.gather(
            svc._deliver(reminder.id, rem.MODE_SLOT),
            svc._deliver(reminder.id, rem.MODE_SLOT),
        )
        assert sorted(results) == [False, True]
        assert len(sent) == 1

    async def test_concurrent_remeasure_delivery_sends_once(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(
            session, user, measurement.id, next_time=now_utc() - timedelta(minutes=1)
        )
        await session.commit()
        results = await asyncio.gather(
            svc._deliver(reminder.id, rem.MODE_REMEASURE),
            svc._deliver(reminder.id, rem.MODE_REMEASURE),
        )
        assert sorted(results) == [False, True]
        assert len(sent) == 1


# ─────────────────────────── _fire ───────────────────────────


class TestFire:
    async def test_swallows_sender_errors(
        self, session: AsyncSession, user: User, session_factory, scheduler
    ) -> None:
        async def broken_sender(user_id, text, markup) -> None:
            raise RuntimeError("Telegram недоступен")

        svc = rem.RemindersService(session_factory, broken_sender, scheduler)
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        await svc._fire(reminder.id, rem.MODE_SLOT)  # не должно пробросить

    async def test_missing_reminder_is_noop(self, svc) -> None:
        await svc._fire(999, rem.MODE_SLOT)


# ─────────────────────────── Catch-up при старте ───────────────────────────


class TestStartup:
    async def test_delivers_overdue_and_reschedules(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder = await make_slot(
            session, user, next_time=now_utc() - timedelta(hours=2)
        )
        await session.commit()
        assert await svc.startup() == 1
        assert len(sent) == 1
        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert job is not None
        assert job["args"] == [reminder.id, rem.MODE_SLOT]

    async def test_overdue_already_sent_today_is_only_rescheduled(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder = await make_slot(
            session,
            user,
            next_time=now_utc() - timedelta(hours=2),
            last_sent=today_minsk(),
        )
        await session.commit()
        assert await svc.startup() == 0
        assert sent == []
        assert scheduler.get_job(f"reminder:{reminder.id}") is not None

    async def test_overdue_remeasure_delivered_and_disabled(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await make_remeasure(
            session, user, measurement.id, next_time=now_utc() - timedelta(hours=2)
        )
        await session.commit()
        assert await svc.startup() == 1
        assert len(sent) == 1
        [row] = (await session.execute(select(Reminder))).scalars().all()
        await session.refresh(row)
        assert row.enabled is False
        assert await svc.startup() == 0  # доставленная строка не шлётся повторно

    async def test_future_jobs_are_scheduled(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        slot = await make_slot(session, user, next_time=now_utc() + timedelta(hours=1))
        remeasure = await make_remeasure(session, user, measurement.id)
        await session.commit()
        assert await svc.startup() == 0
        assert sent == []
        assert scheduler.get_job(f"reminder:{slot.id}")["args"] == [slot.id, rem.MODE_SLOT]
        assert scheduler.get_job(f"reminder:{remeasure.id}")["args"] == [
            remeasure.id,
            rem.MODE_REMEASURE,
        ]

    async def test_disabled_rows_ignored(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        await make_slot(
            session,
            user,
            next_time=now_utc() - timedelta(hours=2),
            enabled=False,
        )
        await session.commit()
        assert await svc.startup() == 0
        assert sent == []
        assert scheduler.jobs == {}


# ─────────────────────────── Конфигурация job'а ───────────────────────────


class TestJobConfig:
    async def test_date_trigger_utc(
        self, session: AsyncSession, user: User, svc, scheduler
    ) -> None:
        reminder = await svc.ensure_slot(user.id, "evening", "21:00", "Europe/Minsk")
        job = scheduler.get_job(f"reminder:{reminder.id}")
        assert isinstance(job["trigger"], DateTrigger)
        assert job["trigger"].run_date.tzinfo is not None
        assert job["id"] == f"reminder:{reminder.id}"


class TestRemeasureButtonsAfterDelivery:
    """Регресс: «Через 30 мин» и «Больше не напоминать» под пришедшим повтором замера.

    Раньше строка удалялась при доставке, и кнопки (по её id) ничего не находили.
    """

    async def _delivered(self, session, user, svc, measurement_id=None):
        reminder = await make_remeasure(
            session, user, measurement_id, next_time=now_utc() - timedelta(minutes=1)
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE) is True
        return reminder.id

    async def test_snooze_after_delivery(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder_id = await self._delivered(session, user, svc)
        assert await svc.snooze(user.id, reminder_id) is True
        row = await session.get(Reminder, reminder_id)
        await session.refresh(row)
        assert row.enabled is True
        assert row.next_trigger_time > now_utc() + timedelta(minutes=29)
        assert scheduler.get_job(f"reminder:{reminder_id}")["args"] == [
            reminder_id, rem.MODE_SNOOZE,
        ]
        # отложенный повтор приходит ещё раз
        await session.execute(
            update(Reminder).where(Reminder.id == reminder_id)
            .values(next_trigger_time=now_utc() - timedelta(seconds=1))
        )
        await session.commit()
        assert await svc._deliver(reminder_id, rem.MODE_SNOOZE) is True
        assert len(sent) == 2

    async def test_off_after_delivery(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder_id = await self._delivered(session, user, svc)
        assert await svc.cancel_remeasure(user.id, reminder_id) is True
        assert (await session.execute(select(Reminder))).scalars().all() == []
        assert await svc.cancel_remeasure(user.id, reminder_id) is False

    async def test_off_after_snooze_cancels_pending(
        self, session: AsyncSession, user: User, svc, scheduler, sent
    ) -> None:
        reminder_id = await self._delivered(session, user, svc)
        assert await svc.snooze(user.id, reminder_id) is True
        assert await svc.cancel_remeasure(user.id, reminder_id) is True
        assert scheduler.get_job(f"reminder:{reminder_id}") is None

    async def test_buttons_need_internal_user_id(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        """Telegram id ≠ users.id: по Telegram id строка не находится."""
        reminder_id = await self._delivered(session, user, svc)
        assert user.telegram_id != user.id
        assert await svc.snooze(user.telegram_id, reminder_id) is False
        assert await svc.snooze(user.id, reminder_id) is True

    async def test_delivered_row_does_not_block_new_remeasure(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        """После доставки следующий приём лекарства ставит новый повтор."""
        await self._delivered(session, user, svc)
        assert await svc.ensure_remeasure(user.id, None, 60) is True
        rows = (await session.execute(select(Reminder))).scalars().all()
        assert len(rows) == 1 and rows[0].enabled is True

    async def test_delivered_row_for_measurement_replaced(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        measurement = await add_measurement(session, user, now_utc())
        await self._delivered(session, user, svc, measurement.id)
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is True
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is False

    async def test_old_delivered_rows_cleaned_on_next_delivery(
        self, session: AsyncSession, user: User, svc, sent
    ) -> None:
        first = await self._delivered(session, user, svc)
        measurement = await add_measurement(session, user, now_utc())
        second = await self._delivered(session, user, svc, measurement.id)
        ids = [r.id for r in (await session.execute(select(Reminder))).scalars().all()]
        assert ids == [second] and first != second

    async def test_disabled_slot_still_not_snoozed(
        self, session: AsyncSession, user: User, svc
    ) -> None:
        slot = await make_slot(session, user, next_time=now_utc() + timedelta(hours=1))
        slot.enabled = False
        await session.commit()
        assert await svc.snooze(user.id, slot.id) is False


# ─────────────────────────── 0.5.4: кнопки, уборка, catch-up ───────────────────────────


class FakeTelegram:
    """Отправка с `message_id` и снятие кнопок (контракты `Sender` / `MarkupClearer`)."""

    def __init__(self, fail_clear: bool = False) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.cleared: list[tuple[int, int]] = []
        self.fail_clear = fail_clear
        self.next_id = 100

    async def send(self, telegram_id: int, text: str, markup) -> int:
        self.sent.append((telegram_id, text, markup))
        self.next_id += 1
        return self.next_id

    async def clear(self, telegram_id: int, message_id: int) -> None:
        if self.fail_clear:
            raise RuntimeError("Bad Request: message to edit not found")
        self.cleared.append((telegram_id, message_id))


def tg_svc(session_factory, scheduler, fake: FakeTelegram) -> rem.RemindersService:
    return rem.RemindersService(session_factory, fake.send, scheduler, clear_markup=fake.clear)


class TestSlotMessageKeyboardDay:
    async def test_buttons_carry_local_day(self, session, user, svc, sent) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT)
        data = [b.callback_data for row in sent[0][2].inline_keyboard for b in row]
        key = today_minsk().strftime("%Y%m%d")
        assert f"rem:snooze:morning:{key}" in data and f"rem:skip:morning:{key}" in data


class TestLastMessage:
    async def test_message_id_saved_and_previous_cleared(self, session, user, session_factory, scheduler) -> None:
        fake = FakeTelegram()
        svc = tg_svc(session_factory, scheduler, fake)
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT)
        assert (await reload_reminder(session, reminder.id)).last_message_id == 101
        assert fake.cleared == []  # прежнего сообщения не было
        await session.execute(
            update(Reminder).where(Reminder.id == reminder.id)
            .values(next_trigger_time=now_utc() - timedelta(minutes=1))
        )
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT)
        assert (await reload_reminder(session, reminder.id)).last_message_id == 102
        assert fake.cleared == [(user.telegram_id, 101)]

    async def test_clear_failure_is_not_fatal(self, session, user, session_factory, scheduler) -> None:
        fake = FakeTelegram(fail_clear=True)
        svc = tg_svc(session_factory, scheduler, fake)
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        reminder.last_message_id = 55
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT) is True
        assert (await reload_reminder(session, reminder.id)).last_message_id == 101
        assert len(fake.sent) == 1

    async def test_without_clearer_only_saves(self, session, user, session_factory, scheduler) -> None:
        fake = FakeTelegram()
        svc = rem.RemindersService(session_factory, fake.send, scheduler)
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        reminder.last_message_id = 55
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT)
        assert (await reload_reminder(session, reminder.id)).last_message_id == 101

    async def test_sender_without_id_keeps_previous(self, session, user, svc) -> None:
        """Отправка без `message_id` (старый контракт) — прежний id не затирается."""
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(minutes=1))
        reminder.last_message_id = 55
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_SLOT)
        assert (await reload_reminder(session, reminder.id)).last_message_id == 55

    async def test_remeasure_has_no_cleanup(self, session, user, session_factory, scheduler) -> None:
        fake = FakeTelegram()
        svc = tg_svc(session_factory, scheduler, fake)
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(session, user, measurement.id, next_time=now_utc() - timedelta(minutes=1))
        reminder.last_message_id = 55
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE)
        assert fake.cleared == []


class TestSnoozeResetsLastSent:
    async def test_slot_snooze_resets(self, session, user, svc) -> None:
        reminder = await make_slot(session, user, last_sent=today_minsk())
        await session.commit()
        assert await svc.snooze_slot(user.id, "morning")
        row = await reload_reminder(session, reminder.id)
        assert row.last_sent_date is None
        assert row.next_trigger_time > now_utc() + timedelta(minutes=29)

    async def test_snooze_slot_missing_or_disabled(self, session, user, svc) -> None:
        assert await svc.snooze_slot(user.id, "evening") is False
        await make_slot(session, user, slot="noon", local_time="13:00", enabled=False)
        await session.commit()
        assert await svc.snooze_slot(user.id, "noon") is False

    async def test_snooze_slot_other_user(self, session, user, svc) -> None:
        other = User(telegram_id=222_222, timezone="Europe/Minsk")
        session.add(other)
        await session.flush()
        await make_slot(session, other)
        await session.commit()
        assert await svc.snooze_slot(user.id, "morning") is False

    async def test_snoozed_delivered_by_catchup(self, session, user, svc, sent) -> None:
        """Регресс E5: отложенное, не сработавшее при простое, доставляется при старте."""
        reminder = await make_slot(session, user, last_sent=today_minsk())
        await session.commit()
        assert await svc.snooze_slot(user.id, "morning")
        await session.execute(
            update(Reminder).where(Reminder.id == reminder.id)
            .values(next_trigger_time=now_utc() - timedelta(minutes=10))
        )
        await session.commit()
        assert await svc.startup() == 1
        assert len(sent) == 1

    async def test_remeasure_snooze_resets(self, session, user, svc) -> None:
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(session, user, measurement.id, last_sent=today_minsk())
        reminder.enabled = False
        await session.commit()
        assert await svc.snooze(user.id, reminder.id)
        assert (await reload_reminder(session, reminder.id)).last_sent_date is None


class TestCatchupWindow:
    async def test_slot_older_than_window_rescheduled_silently(self, session, user, svc, scheduler, sent) -> None:
        reminder = await make_slot(session, user, next_time=now_utc() - timedelta(hours=7))
        await session.commit()
        assert await svc.startup() == 0
        assert sent == []
        row = await reload_reminder(session, reminder.id)
        assert row.next_trigger_time > now_utc()
        assert scheduler.get_job(f"reminder:{reminder.id}")["args"] == [reminder.id, rem.MODE_SLOT]

    async def test_slot_inside_window_delivered(self, session, user, svc, sent) -> None:
        await make_slot(session, user, next_time=now_utc() - timedelta(hours=5, minutes=50))
        await session.commit()
        assert await svc.startup() == 1

    async def test_remeasure_older_than_window_deleted(self, session, user, svc, sent) -> None:
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(session, user, measurement.id, next_time=now_utc() - timedelta(hours=7))
        await session.commit()
        assert await svc.startup() == 0
        assert sent == []
        assert await reload_reminder(session, reminder.id) is None

    async def test_undelivered_remeasure_deleted_and_new_allowed(self, session, user, svc, sent) -> None:
        """Регресс E5: застрявший повтор блокировал новые повторы после «Принял всё ✓»."""
        reminder = Reminder(
            user_id=user.id, type="med_remeasure", local_time=None,
            next_trigger_time=now_utc() - timedelta(minutes=20), enabled=True,
            measurement_id=None, last_sent_date=today_minsk(),
        )
        session.add(reminder)
        await session.commit()
        assert await svc.startup() == 0
        assert await reload_reminder(session, reminder.id) is None
        assert await svc.ensure_remeasure(user.id, None, 60) is True

    async def test_remeasure_without_consent_deleted(self, session, user, svc, sent) -> None:
        from bot.models import Consent

        await session.execute(
            update(Consent).where(Consent.user_id == user.id).values(withdrawn_at=now_utc())
        )
        measurement = await add_measurement(session, user, now_utc())
        reminder = await make_remeasure(session, user, measurement.id, next_time=now_utc() - timedelta(minutes=1))
        await session.commit()
        assert await svc._deliver(reminder.id, rem.MODE_REMEASURE) is False
        assert await reload_reminder(session, reminder.id) is None
        assert sent == []


class TestCatchupHelpersMissingRow:
    async def test_reschedule_missing_row_is_noop(self, svc, scheduler) -> None:
        await svc._reschedule_overdue_slot(999_999)
        assert scheduler.jobs == {}

    async def test_drop_missing_remeasure_is_noop(self, svc) -> None:
        await svc._drop_undelivered_remeasure(999_999)
