"""Тесты хендлеров кнопок напоминаний (bot/handlers/reminders.py, SPEC.md, §6.7)."""
from __future__ import annotations

import inspect
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import reminders
from bot.services import medications as medications_service
from bot.models import Medication, MedicationLog, User as UserModel

TG_ID = 111_111


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, message: Message, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message,
    )


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры."""
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def state() -> FSMContext:
    return make_state()


@pytest.fixture
def reminders_svc(mocker):
    return mocker.Mock(
        ensure_remeasure=mocker.AsyncMock(),
        snooze=mocker.AsyncMock(),
        cancel_remeasure=mocker.AsyncMock(),
        skip_slot=mocker.AsyncMock(),
        snooze_slot=mocker.AsyncMock(),
        cancel_followup=mocker.AsyncMock(),
    )


async def add_med(
    session: AsyncSession,
    user: UserModel,
    name: str = "Лозартан",
    dosage: str = "50 мг",
    slot: str = "morning",
) -> Medication:
    row = Medication(user_id=user.id, name=name, dosage=dosage, slot=slot)
    session.add(row)
    await session.commit()
    return row


async def logs_of(session: AsyncSession, owner_id: int) -> list[MedicationLog]:
    rows = await session.execute(
        select(MedicationLog).where(MedicationLog.user_id == owner_id)
    )
    return list(rows.scalars())


class TestTakeAll:
    async def test_logs_all_taken(self, session, user, edit, cb_answer, reminders_svc) -> None:
        med = await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        logs = await logs_of(session, user.id)
        assert [(row.medication_id, row.status, row.slot) for row in logs] == [
            (med.id, "taken", "morning")
        ]
        expected = "☀️ Утро\n" + texts.MEDS_LOGGED.format(
            summary=texts.fmt_meds_summary(["Лозартан 50 мг"], [])
        )
        assert edit.call_args.args[0] == expected
        # U1 (0.5.5): после отметки — «Записать давление» / «В меню».
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_done_kb()

    async def test_schedules_remeasure(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.med_remeasure_delay = 60
        await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.ensure_remeasure.assert_awaited_once_with(user.id, None, 60)

    async def test_no_remeasure_when_disabled(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        user.med_remeasure_delay = None
        await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.ensure_remeasure.assert_not_awaited()

    async def test_double_tap_logs_once(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        user.med_remeasure_delay = 60
        await add_med(session, user)
        for _ in range(2):
            await call(
                reminders.cb_take_all,
                callback=make_callback("rem:all:morning", make_message()),
                session=session,
                state=make_state(),
                reminders=reminders_svc,
            )
        assert len(await logs_of(session, user.id)) == 1
        reminders_svc.ensure_remeasure.assert_awaited_once()

    async def test_garbage_slot_ignored(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:zzz", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_empty_slot_ignored(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        assert await logs_of(session, user.id) == []


def today_of(user: UserModel) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def local_moment(user: UserModel, day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour, 0), ZoneInfo(user.timezone)).astimezone(timezone.utc)


def msg_with(markup, sent_at: datetime | None = None) -> Message:
    """Сообщение бота с клавиатурой (отметки «Частично…» читаются из неё)."""
    return Message(
        message_id=5,
        date=sent_at or datetime.now(timezone.utc),
        chat=Chat(id=TG_ID, type="private"),
        from_user=User(id=TG_ID, is_bot=True, first_name="Бот"),
        text="…",
        reply_markup=markup,
    )


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def partial_kb(slot: str, day: date, checked, meds) -> object:
    return keyboards.partial_meds_kb(
        slot, day, checked=checked, medications=[(m.id, m.name, m.dosage) for m in meds]
    )


class TestPartial:
    """«Частично…» без FSM: состояние — в callback_data и клавиатуре (§6.7, 0.5.4)."""

    async def test_start_marks_all(self, session, user, edit, cb_answer) -> None:
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        med2 = await add_med(session, user, "Амлодипин", "5 мг")
        day = today_of(user)
        await call(
            reminders.cb_partial_start,
            callback=make_callback(f"rem:partial:morning:{ymd(day)}", make_message()),
            session=session,
        )
        assert edit.call_args.args[0] == texts.PARTIAL_PROMPT
        ordered = await medications_service.slot_medications(session, user, "morning")
        assert edit.call_args.kwargs["reply_markup"] == partial_kb("morning", day, {med1.id, med2.id}, ordered)

    async def test_start_legacy_without_date(self, session, user, edit, cb_answer) -> None:
        """Кнопка 0.5.3 без даты: день — день сообщения."""
        med = await add_med(session, user)
        await call(
            reminders.cb_partial_start,
            callback=make_callback("rem:partial:morning", make_message()),
            session=session,
        )
        assert edit.call_args.kwargs["reply_markup"] == partial_kb("morning", today_of(user), {med.id}, [med])

    async def test_toggle_unchecks_and_back(self, session, user, edit, cb_answer) -> None:
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        med2 = await add_med(session, user, "Амлодипин", "5 мг")
        day = today_of(user)
        ordered = await medications_service.slot_medications(session, user, "morning")
        message = msg_with(partial_kb("morning", day, {med1.id, med2.id}, ordered))
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:pt:morning:{ymd(day)}:{med1.id}", message),
            session=session,
        )
        unchecked = edit.call_args.kwargs["reply_markup"]
        assert unchecked == partial_kb("morning", day, {med2.id}, ordered)
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:pt:morning:{ymd(day)}:{med1.id}", msg_with(unchecked)),
            session=session,
        )
        assert edit.call_args.kwargs["reply_markup"] == partial_kb("morning", day, {med1.id, med2.id}, ordered)

    async def test_no_fsm_in_handlers(self) -> None:
        """Ни один обработчик напоминаний не зависит от FSM (перезапуск, запись давления)."""
        for handler in (
            reminders.cb_take_all, reminders.cb_partial_start, reminders.cb_partial_toggle,
            reminders.cb_partial_done, reminders.cb_partial_cancel, reminders.cb_skip_slot,
            reminders.cb_snooze, reminders.cb_remeasure_off,
        ):
            assert "state" not in inspect.signature(handler).parameters, handler.__name__

    async def test_toggle_foreign_med_stale(self, session, user, edit, cb_answer) -> None:
        other = UserModel(telegram_id=222_222, timezone="Europe/Minsk")
        session.add(other)
        await session.commit()
        foreign = await add_med(session, other)
        med = await add_med(session, user)
        day = today_of(user)
        message = msg_with(partial_kb("morning", day, {med.id}, [med]))
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:pt:morning:{ymd(day)}:{foreign.id}", message),
            session=session,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    @pytest.mark.parametrize(
        "suffix", ["x", "", "1:2", "zzz"],
    )
    async def test_toggle_garbage_stale(self, session, user, edit, cb_answer, suffix) -> None:
        await add_med(session, user)
        data = f"rem:pt:morning:{ymd(today_of(user))}:{suffix}" if suffix != "zzz" else "rem:pt:zzz:20261110:1"
        await call(reminders.cb_partial_toggle, callback=make_callback(data, make_message()), session=session)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_toggle_without_date_stale(self, session, user, edit, cb_answer) -> None:
        med = await add_med(session, user)
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:pt:morning:{med.id}", make_message()),
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_done_logs_from_markup(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.med_remeasure_delay = 60
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        med2 = await add_med(session, user, "Амлодипин", "5 мг")
        day = today_of(user)
        message = msg_with(partial_kb("morning", day, {med1.id}, [med1, med2]))
        await call(
            reminders.cb_partial_done,
            callback=make_callback(f"rem:pd:morning:{ymd(day)}", message),
            session=session,
            reminders=reminders_svc,
        )
        statuses = {row.medication_id: row.status for row in await logs_of(session, user.id)}
        assert statuses == {med1.id: "taken", med2.id: "skipped"}
        assert edit.call_args.args[0] == "☀️ Утро\n" + texts.fmt_intake_result(
            ["Лозартан 50 мг"], ["Амлодипин 5 мг"], []
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_done_kb()
        reminders_svc.ensure_remeasure.assert_awaited_once_with(user.id, None, 60)

    async def test_done_nothing_checked_no_remeasure(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.med_remeasure_delay = 60
        med = await add_med(session, user)
        day = today_of(user)
        await call(
            reminders.cb_partial_done,
            callback=make_callback(f"rem:pd:morning:{ymd(day)}", msg_with(partial_kb("morning", day, set(), [med]))),
            session=session,
            reminders=reminders_svc,
        )
        assert [row.status for row in await logs_of(session, user.id)] == ["skipped"]
        reminders_svc.ensure_remeasure.assert_not_awaited()

    async def test_two_open_lists_each_logs_own_slot(self, session, user, edit, cb_answer, reminders_svc) -> None:
        """Регресс 0.5.3: «Готово» в утреннем списке отмечало вечерний слот."""
        morning = await add_med(session, user, "Лозартан", "50 мг", slot="morning")
        evening = await add_med(session, user, "Амлодипин", "5 мг", slot="evening")
        day = today_of(user)
        morning_msg = msg_with(partial_kb("morning", day, {morning.id}, [morning]))
        evening_msg = msg_with(partial_kb("evening", day, set(), [evening]))
        await call(
            reminders.cb_partial_done,
            callback=make_callback(f"rem:pd:morning:{ymd(day)}", morning_msg),
            session=session, reminders=reminders_svc,
        )
        logs = {(row.medication_id, row.slot, row.status) for row in await logs_of(session, user.id)}
        assert logs == {(morning.id, "morning", "taken")}
        await call(
            reminders.cb_partial_done,
            callback=make_callback(f"rem:pd:evening:{ymd(day)}", evening_msg),
            session=session, reminders=reminders_svc,
        )
        logs = {(row.medication_id, row.slot, row.status) for row in await logs_of(session, user.id)}
        assert logs == {(morning.id, "morning", "taken"), (evening.id, "evening", "skipped")}

    async def test_done_empty_slot_stale(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_partial_done,
            callback=make_callback(f"rem:pd:morning:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)
        assert await logs_of(session, user.id) == []

    @pytest.mark.parametrize("data", ["rem:pd:morning", "rem:pc:morning", "rem:pd:zzz:20261110"])
    async def test_done_cancel_garbage_stale(self, session, user, edit, cb_answer, reminders_svc, data) -> None:
        handler = reminders.cb_partial_done if data.startswith("rem:pd") else reminders.cb_partial_cancel
        await call(handler, callback=make_callback(data, make_message()), session=session, reminders=reminders_svc)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_cancel_returns_slot_screen(self, session, user, edit, cb_answer) -> None:
        med = await add_med(session, user)
        day = today_of(user)
        await call(
            reminders.cb_partial_cancel,
            callback=make_callback(f"rem:pc:morning:{ymd(day)}", make_message()),
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_slot_message("morning", ["Лозартан 50 мг"])
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slot_message_kb("morning", day, True)
        assert await logs_of(session, user.id) == []
        assert med.id

    async def test_cancel_then_snooze_works(self, session, user, edit, cb_answer, reminders_svc) -> None:
        """Регресс 0.5.3: после «Отмена» «Через 30 мин» несло id 0 и молчало."""
        await add_med(session, user)
        day = today_of(user)
        await call(
            reminders.cb_partial_cancel,
            callback=make_callback(f"rem:pc:morning:{ymd(day)}", make_message()),
            session=session,
        )
        snooze = next(
            b.callback_data
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for b in row
            if b.callback_data.startswith("rem:snooze:")
        )
        reminders_svc.snooze_slot.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback(snooze, make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze_slot.assert_awaited_once_with(user.id, "morning")
        assert edit.call_args.args[0] == texts.SLOT_REMAINING

    @pytest.mark.parametrize("data", ["rem:med:5", "rem:partial:done", "rem:partial:cancel"])
    async def test_legacy_fsm_buttons_stale(self, session, user, edit, cb_answer, data) -> None:
        handler = reminders.cb_legacy_partial if data.startswith("rem:med") else reminders.cb_partial_start
        await call(handler, callback=make_callback(data, make_message()), session=session)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)


class TestReminderDates:
    """Приём пишется за день напоминания; старше 2 суток — «устарело» (§6.7, 0.5.4)."""

    async def test_same_day_now(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        day = today_of(user)
        before = datetime.now(timezone.utc)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(day)}", make_message()),
            session=session, reminders=reminders_svc,
        )
        (log,) = await logs_of(session, user.id)
        assert as_utc(log.timestamp) >= before.replace(microsecond=0)

    async def test_yesterday_logged_at_message_moment(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.med_remeasure_delay = 60
        await add_med(session, user)
        yesterday = today_of(user) - timedelta(days=1)
        sent = local_moment(user, yesterday, 9)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(yesterday)}", msg_with(None, sent)),
            session=session, reminders=reminders_svc,
        )
        (log,) = await logs_of(session, user.id)
        assert as_utc(log.timestamp) == sent
        reminders_svc.ensure_remeasure.assert_not_awaited()  # повтор — только для сегодняшнего

    async def test_legacy_yesterday_by_message_date(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        sent = local_moment(user, today_of(user) - timedelta(days=1), 20)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", msg_with(None, sent)),
            session=session, reminders=reminders_svc,
        )
        (log,) = await logs_of(session, user.id)
        assert as_utc(log.timestamp) == sent

    async def test_message_outside_day_noon_fallback(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        yesterday = today_of(user) - timedelta(days=1)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(yesterday)}", make_message()),
            session=session, reminders=reminders_svc,
        )
        (log,) = await logs_of(session, user.id)
        assert as_utc(log.timestamp) == local_moment(user, yesterday, 12)

    @pytest.mark.parametrize("days_ago", [3, -1])
    async def test_too_old_or_future_stale(self, session, user, edit, cb_answer, reminders_svc, days_ago) -> None:
        await add_med(session, user)
        day = today_of(user) - timedelta(days=days_ago)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(day)}", make_message()),
            session=session, reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)
        assert await logs_of(session, user.id) == []

    async def test_two_days_ago_still_ok(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        day = today_of(user) - timedelta(days=2)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(day)}", msg_with(None, local_moment(user, day))),
            session=session, reminders=reminders_svc,
        )
        assert len(await logs_of(session, user.id)) == 1

    async def test_bad_date_stale(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning:20261340", make_message()),
            session=session, reminders=reminders_svc,
        )
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_legacy_inaccessible_message_stale(self, session, user, cb_answer, reminders_svc) -> None:
        from aiogram.types import InaccessibleMessage

        await add_med(session, user)
        message = InaccessibleMessage(chat=Chat(id=TG_ID, type="private"), message_id=5, date=0)
        await call(
            reminders.cb_take_all,
            callback=make_callback("rem:all:morning", message),
            session=session, reminders=reminders_svc,
        )
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_snooze_yesterday_stale(self, session, user, edit, cb_answer, reminders_svc) -> None:
        yesterday = today_of(user) - timedelta(days=1)
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:morning:{ymd(yesterday)}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze_slot.assert_not_awaited()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_skip_yesterday_logs_without_reschedule(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        yesterday = today_of(user) - timedelta(days=1)
        sent = local_moment(user, yesterday, 8)
        await call(
            reminders.cb_skip_slot,
            callback=make_callback(f"rem:skip:morning:{ymd(yesterday)}", msg_with(None, sent)),
            session=session, reminders=reminders_svc,
        )
        (log,) = await logs_of(session, user.id)
        assert log.status == "skipped" and as_utc(log.timestamp) == sent
        reminders_svc.skip_slot.assert_not_awaited()
        assert edit.call_args.args[0] == texts.SLOT_SKIPPED

    async def test_yesterday_and_today_both_logged(self, session, user, edit, cb_answer, reminders_svc) -> None:
        """Вчерашнее «Принял всё ✓» не съедает сегодняшний приём (E3)."""
        await add_med(session, user)
        yesterday = today_of(user) - timedelta(days=1)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(yesterday)}", msg_with(None, local_moment(user, yesterday))),
            session=session, reminders=reminders_svc,
        )
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        assert len(await logs_of(session, user.id)) == 2
        assert edit.call_args.args[0].splitlines()[1].startswith("Отметил")


class TestIntakeTexts:
    """Итог отметки: «уже отмечены сегодня» вместо «нет данных» (§6.7, E4)."""

    async def test_all_already(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        data = f"rem:all:morning:{ymd(today_of(user))}"
        for _ in range(2):
            await call(reminders.cb_take_all, callback=make_callback(data, make_message()),
                       session=session, reminders=reminders_svc)
        assert edit.call_args.args[0] == "☀️ Утро\nЭти лекарства уже отмечены сегодня ✓: Лозартан 50 мг"
        assert "нет данных" not in edit.call_args.args[0]

    async def test_mixed(self, session, user, edit, cb_answer, reminders_svc) -> None:
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        day = today_of(user)
        await call(reminders.cb_take_all, callback=make_callback(f"rem:all:morning:{ymd(day)}", make_message()),
                   session=session, reminders=reminders_svc)
        await add_med(session, user, "Амлодипин", "5 мг")
        await call(reminders.cb_take_all, callback=make_callback(f"rem:all:morning:{ymd(day)}", make_message()),
                   session=session, reminders=reminders_svc)
        assert edit.call_args.args[0] == (
            "☀️ Утро\nОтметил: принято: Амлодипин 5 мг\nУже отмечено сегодня: Лозартан 50 мг"
        )
        assert med1.id


class TestSkip:
    async def test_skip_logs_skipped(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_skip_slot,
            callback=make_callback("rem:skip:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        assert [row.status for row in await logs_of(session, user.id)] == ["skipped"]
        assert edit.call_args.args[0] == texts.SLOT_SKIPPED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_skip_reschedules(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_skip_slot,
            callback=make_callback("rem:skip:morning", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.skip_slot.assert_awaited_once_with(user.id, "morning")

    async def test_skip_without_meds(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_skip_slot,
            callback=make_callback("rem:skip:evening", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        assert await logs_of(session, user.id) == []
        reminders_svc.skip_slot.assert_awaited_once_with(user.id, "evening")
        assert edit.call_args.args[0] == texts.SLOT_SKIPPED

    async def test_garbage_slot_ignored(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_skip_slot,
            callback=make_callback("rem:skip:zzz", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        reminders_svc.skip_slot.assert_not_awaited()


class TestSnooze:
    async def test_snooze_moves(self, edit, cb_answer, reminders_svc, session, user) -> None:
        reminders_svc.snooze.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:7", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        # Регресс: в сервис уходит внутренний users.id, а не Telegram id.
        reminders_svc.snooze.assert_awaited_once_with(user.id, 7)
        assert user.id != TG_ID
        assert edit.call_args.args[0] == texts.SLOT_REMAINING
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_missing_row_ignored(self, edit, cb_answer, reminders_svc, session, user) -> None:
        reminders_svc.snooze.return_value = False
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:7", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_garbage_id_ignored(self, edit, cb_answer, reminders_svc, session) -> None:
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:abc", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.snooze.assert_not_awaited()
        edit.assert_not_called()


class TestRemeasureOff:
    async def test_off_removes(self, edit, cb_answer, reminders_svc, session, user) -> None:
        reminders_svc.cancel_remeasure.return_value = True
        await call(
            reminders.cb_remeasure_off,
            callback=make_callback("rem:off:7", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.cancel_remeasure.assert_awaited_once_with(user.id, 7)
        assert edit.call_args.args[0] == texts.REMEASURE_OFF_DONE
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_missing_row_ignored(self, edit, cb_answer, reminders_svc, session, user) -> None:
        reminders_svc.cancel_remeasure.return_value = False
        await call(
            reminders.cb_remeasure_off,
            callback=make_callback("rem:off:7", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_garbage_id_ignored(self, edit, cb_answer, reminders_svc, session) -> None:
        await call(
            reminders.cb_remeasure_off,
            callback=make_callback("rem:off:abc", make_message()),
            session=session,
            state=make_state(),
            reminders=reminders_svc,
        )
        reminders_svc.cancel_remeasure.assert_not_awaited()
        edit.assert_not_called()



class TestSnoozeSlotAndStale:
    """«Через 30 мин» слота по `(users.id, slot)`; повтор старше 24 ч — «устарело» (§6.7, 0.5.4)."""

    async def test_slot_snooze_today(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.snooze_slot.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze_slot.assert_awaited_once_with(user.id, "noon")
        reminders_svc.snooze.assert_not_awaited()
        assert edit.call_args.args[0] == texts.SLOT_REMAINING

    async def test_slot_snooze_legacy_without_date(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.snooze_slot.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:evening", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze_slot.assert_awaited_once_with(user.id, "evening")

    async def test_slot_snooze_disabled_stale(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.snooze_slot.return_value = False
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_legacy_zero_id_stale(self, session, user, edit, cb_answer, reminders_svc) -> None:
        """Кнопка 0.5.3 после «Отмена» (`rem:snooze:0`) — уведомление, а не молчание."""
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:0", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze.assert_not_awaited()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_remeasure_snooze_expired(self, session, user, edit, cb_answer, reminders_svc) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        await call(
            reminders.cb_snooze,
            callback=make_callback("rem:snooze:7", msg_with(None, old)),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.cancel_remeasure.assert_awaited_once_with(user.id, 7)
        reminders_svc.snooze.assert_not_awaited()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)

    async def test_remeasure_off_expired(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.cancel_remeasure.return_value = True
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        await call(
            reminders.cb_remeasure_off,
            callback=make_callback("rem:off:7", msg_with(None, old)),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.cancel_remeasure.assert_awaited_once_with(user.id, 7)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)


class TestStaleBranches:
    """Устаревшая дата во всех кнопках слота — «устарело», данные не меняются (§6.7)."""

    @pytest.mark.parametrize("handler_name, prefix", [
        ("cb_partial_done", "rem:pd"),
        ("cb_partial_cancel", "rem:pc"),
        ("cb_skip_slot", "rem:skip"),
        ("cb_partial_start", "rem:partial"),
    ])
    async def test_old_day_stale(self, session, user, edit, cb_answer, reminders_svc, handler_name, prefix) -> None:
        await add_med(session, user)
        old = today_of(user) - timedelta(days=5)
        await call(
            getattr(reminders, handler_name),
            callback=make_callback(f"{prefix}:morning:{ymd(old)}", make_message()),
            session=session, reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)
        assert await logs_of(session, user.id) == []
        reminders_svc.skip_slot.assert_not_awaited()

    @pytest.mark.parametrize("raw", ["2026", "2026111", "abcdefgh"])
    async def test_malformed_day_stale(self, session, user, edit, cb_answer, reminders_svc, raw) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{raw}", make_message()),
            session=session, reminders=reminders_svc,
        )
        cb_answer.assert_awaited_once_with(texts.REMINDER_STALE)


class TestAfterIntakeU1:
    """После отметки — слот в итоге и «Записать давление» / «В меню» (§6.7, 0.5.5)."""

    @pytest.mark.parametrize(("slot", "header"), [("noon", "🌤 Обед"), ("evening", "🌙 Вечер")])
    async def test_header_per_slot(self, session, user, edit, cb_answer, reminders_svc, slot, header) -> None:
        await add_med(session, user, slot=slot)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:{slot}:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        assert edit.call_args.args[0].splitlines()[0] == header
        buttons = [b for row in edit.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert [(b.text, b.callback_data) for b in buttons] == [
            ("Записать давление", "rem:write"), ("В меню", "menu:back"),
        ]

    async def test_skip_keeps_old_finish(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_skip_slot,
            callback=make_callback(f"rem:skip:morning:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        assert edit.call_args.args[0] == texts.SLOT_SKIPPED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    def test_labels_u2(self) -> None:
        assert texts.BTN_PARTIAL == "Отметить принятые"
        assert texts.BTN_SKIP_SLOT == "Пропускаю сегодня"


# ─────────────────── Ответ на напоминание, режим, пауза (§6.7, 0.5.6) ───────────────────


async def add_slot_row(session: AsyncSession, user: UserModel, slot: str = "morning", mode: str = "both"):
    from bot.models import Reminder

    row = Reminder(
        user_id=user.id,
        type=slot,
        local_time="08:00",
        next_trigger_time=datetime.now(timezone.utc) + timedelta(hours=1),
        enabled=True,
        mode=mode,
    )
    session.add(row)
    await session.commit()
    return row


class TestAnswerCancelsFollowup:
    async def test_take_all(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_take_all,
            callback=make_callback(f"rem:all:morning:{ymd(today_of(user))}", make_message()),
            session=session, state=make_state(), reminders=reminders_svc,
        )
        reminders_svc.cancel_followup.assert_awaited_once_with(user.id, "morning")

    async def test_partial_start(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await call(
            reminders.cb_partial_start,
            callback=make_callback(f"rem:partial:morning:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.cancel_followup.assert_awaited_once_with(user.id, "morning")

    async def test_skip(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await call(
            reminders.cb_skip_slot,
            callback=make_callback(f"rem:skip:noon:{ymd(today_of(user))}", make_message()),
            session=session, state=make_state(), reminders=reminders_svc,
        )
        reminders_svc.cancel_followup.assert_awaited_once_with(user.id, "noon")

    async def test_snooze_moved(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.snooze_slot.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.cancel_followup.assert_awaited_once_with(user.id, "noon")

    async def test_snooze_not_moved_keeps_followup(self, session, user, edit, cb_answer, reminders_svc) -> None:
        reminders_svc.snooze_slot.return_value = False
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.cancel_followup.assert_not_awaited()


class TestPausedSnooze:
    async def test_toast_and_nothing_moved(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.reminders_paused_until = today_of(user) + timedelta(days=2)
        await session.commit()
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        until = (today_of(user) + timedelta(days=2)).strftime("%d.%m")
        cb_answer.assert_awaited_once_with(texts.PAUSED_TOAST.format(date=until))
        reminders_svc.snooze_slot.assert_not_awaited()
        edit.assert_not_awaited()

    async def test_expired_pause_snoozes(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.reminders_paused_until = today_of(user) - timedelta(days=1)
        await session.commit()
        reminders_svc.snooze_slot.return_value = True
        await call(
            reminders.cb_snooze,
            callback=make_callback(f"rem:snooze:noon:{ymd(today_of(user))}", make_message()),
            session=session, reminders=reminders_svc,
        )
        reminders_svc.snooze_slot.assert_awaited_once_with(user.id, "noon")


class TestPressureMode:
    async def test_skip_does_not_log_skipped(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        await add_slot_row(session, user, mode="pressure")
        await call(
            reminders.cb_skip_slot,
            callback=make_callback(f"rem:skip:morning:{ymd(today_of(user))}", make_message()),
            session=session, state=make_state(), reminders=reminders_svc,
        )
        assert await logs_of(session, user.id) == []
        assert edit.call_args.args[0] == texts.SLOT_SKIPPED

    async def test_partial_cancel_redraws_by_mode(self, session, user, edit, cb_answer) -> None:
        await add_med(session, user)
        await add_slot_row(session, user, mode="meds")
        day = today_of(user)
        await call(
            reminders.cb_partial_cancel,
            callback=make_callback(f"rem:pc:morning:{ymd(day)}", make_message()),
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_slot_message("morning", ["Лозартан 50 мг"], mode="meds")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slot_message_kb(
            "morning", day, has_medications=True, mode="meds"
        )


# ─────────────────── Крупный режим: раскладки кнопок (§11, UI3/UI6, 0.5.10) ───────────────────


def buttons_of(markup) -> list[list[tuple[str, str]]]:
    """Ряды кнопок как пары (текст, callback_data)."""
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


class TestLargeButtonsOnePerRow:
    """По одной в строке в крупном, парами — в обычном (§11, UI3/UI6.1)."""

    def test_medication_taken_pairs(self) -> None:
        assert buttons_of(keyboards.medication_taken_kb()) == [
            [(texts.BTN_MED_YES, "m:med:yes"), (texts.BTN_MED_NO, "m:med:no")],
            [(texts.BTN_IN_MENU, "menu:back")],
        ]

    def test_medication_taken_large_one_per_row(self) -> None:
        rows = buttons_of(keyboards.medication_taken_kb(large=True))
        assert rows == [
            [(texts.BTN_MED_YES, "m:med:yes")],
            [(texts.BTN_MED_NO, "m:med:no")],
            [(texts.BTN_IN_MENU, "menu:back")],
        ]
        assert all(len(row) == 1 for row in rows)

    def test_feeling_pairs(self) -> None:
        assert buttons_of(keyboards.feeling_kb()) == [
            [
                (texts.BTN_FEEL_NORMAL, "m:feeling:normal"),
                (texts.BTN_FEEL_HEADACHE, "m:feeling:headache"),
            ],
            [
                (texts.BTN_FEEL_WEAKNESS, "m:feeling:weakness"),
                (texts.BTN_FEEL_OTHER, "m:feeling:other"),
            ],
            [(texts.BTN_IN_MENU, "menu:back")],
        ]

    def test_feeling_large_one_per_row(self) -> None:
        rows = buttons_of(keyboards.feeling_kb(large=True))
        assert rows == [
            [(texts.BTN_FEEL_NORMAL, "m:feeling:normal")],
            [(texts.BTN_FEEL_HEADACHE, "m:feeling:headache")],
            [(texts.BTN_FEEL_WEAKNESS, "m:feeling:weakness")],
            [(texts.BTN_FEEL_OTHER, "m:feeling:other")],
            [(texts.BTN_IN_MENU, "menu:back")],
        ]
        assert all(len(row) == 1 for row in rows)

    def test_archive_medication_pairs(self) -> None:
        assert buttons_of(keyboards.archive_medication_taken_kb()) == [
            [(texts.BTN_MED_YES, "m:arch:med:yes"), (texts.BTN_MED_NO, "m:arch:med:no")],
            [(texts.BTN_IN_MENU, "m:arch:menu")],
        ]

    def test_archive_medication_large_one_per_row(self) -> None:
        rows = buttons_of(keyboards.archive_medication_taken_kb(large=True))
        assert rows == [
            [(texts.BTN_MED_YES, "m:arch:med:yes")],
            [(texts.BTN_MED_NO, "m:arch:med:no")],
            [(texts.BTN_IN_MENU, "m:arch:menu")],
        ]
        assert all(len(row) == 1 for row in rows)

    def test_archive_feeling_pairs(self) -> None:
        assert buttons_of(keyboards.archive_feeling_kb()) == [
            [
                (texts.BTN_FEEL_NORMAL, "m:arch:feel:normal"),
                (texts.BTN_FEEL_HEADACHE, "m:arch:feel:headache"),
            ],
            [
                (texts.BTN_FEEL_WEAKNESS, "m:arch:feel:weakness"),
                (texts.BTN_FEEL_OTHER, "m:arch:feel:other"),
            ],
            [(texts.BTN_IN_MENU, "m:arch:menu")],
        ]

    def test_archive_feeling_large_one_per_row(self) -> None:
        rows = buttons_of(keyboards.archive_feeling_kb(large=True))
        assert rows == [
            [(texts.BTN_FEEL_NORMAL, "m:arch:feel:normal")],
            [(texts.BTN_FEEL_HEADACHE, "m:arch:feel:headache")],
            [(texts.BTN_FEEL_WEAKNESS, "m:arch:feel:weakness")],
            [(texts.BTN_FEEL_OTHER, "m:arch:feel:other")],
            [(texts.BTN_IN_MENU, "m:arch:menu")],
        ]
        assert all(len(row) == 1 for row in rows)


class TestKeypadLayout:
    """Калькулятор — исключение §11: 3 колонки обычный / 2 крупный (§6.3, 0.6.0)."""

    def test_normal_three_columns(self) -> None:
        assert buttons_of(keyboards.keypad_kb("sys", (None, None, None))) == [
            [
                ("▸ САД —", "m:field:sys"),
                ("ДАД —", "m:field:dia"),
                ("Пульс —", "m:field:pulse"),
            ],
            [("7", "m:key:7"), ("8", "m:key:8"), ("9", "m:key:9")],
            [("4", "m:key:4"), ("5", "m:key:5"), ("6", "m:key:6")],
            [("1", "m:key:1"), ("2", "m:key:2"), ("3", "m:key:3")],
            [
                (texts.BTN_KEYPAD_CLEAR, "m:key:clear"),
                ("0", "m:key:0"),
                (texts.BTN_KEYPAD_BACKSPACE, "m:key:bksp"),
            ],
            [(texts.BTN_DONE, "m:key:ok"), (texts.BTN_RESET, "m:reset")],
            [(texts.BTN_CANCEL, "m:cancel")],
        ]

    def test_large_two_columns(self) -> None:
        rows = buttons_of(keyboards.keypad_kb("sys", (None, None, None), large=True))
        assert rows == [
            [("▸ САД —", "m:field:sys")],
            [("ДАД —", "m:field:dia")],
            [("Пульс —", "m:field:pulse")],
            [("1", "m:key:1"), ("2", "m:key:2")],
            [("3", "m:key:3"), ("4", "m:key:4")],
            [("5", "m:key:5"), ("6", "m:key:6")],
            [("7", "m:key:7"), ("8", "m:key:8")],
            [("9", "m:key:9"), ("0", "m:key:0")],
            [
                (texts.BTN_KEYPAD_CLEAR, "m:key:clear"),
                (texts.BTN_KEYPAD_BACKSPACE, "m:key:bksp"),
            ],
            [(texts.BTN_DONE, "m:key:ok")],
            [(texts.BTN_RESET, "m:reset")],
            [(texts.BTN_CANCEL, "m:cancel")],
        ]

    def test_large_not_single_column(self) -> None:
        """Крупный режим не разъезжает калькулятор в один столбец (§11, исключение)."""
        rows = buttons_of(keyboards.keypad_kb("sys", (None, None, None), large=True))
        assert all(len(row) == 2 for row in rows[3:8])  # цифры парами
        assert rows[8] == [
            (texts.BTN_KEYPAD_CLEAR, "m:key:clear"),
            (texts.BTN_KEYPAD_BACKSPACE, "m:key:bksp"),
        ]
        assert rows[9] == [(texts.BTN_DONE, "m:key:ok")]
        assert rows[10] == [(texts.BTN_RESET, "m:reset")]
        assert rows[11] == [(texts.BTN_CANCEL, "m:cancel")]

    def test_no_skip_button(self) -> None:
        """«Пропустить» убрана (R7 = А): пустой пульс — `pulse = NULL`."""
        assert buttons_of(keyboards.keypad_kb("pulse", (None, None, None)))[-2:] == [
            [(texts.BTN_DONE, "m:key:ok"), (texts.BTN_RESET, "m:reset")],
            [(texts.BTN_CANCEL, "m:cancel")],
        ]


class TestSlotMessageLayouts:
    """Ряды кнопок слот-сообщения по режимам (мокап 2.4; §11, UI3/UI6.1)."""

    day = date(2026, 11, 12)
    key = "morning:20261112"

    def test_meds_mode_three_rows(self) -> None:
        """«Только лекарства»: пара + «Через 30 мин» + «Пропускаю сегодня» (ревью 2026-11-12)."""
        rows = buttons_of(
            keyboards.slot_message_kb("morning", self.day, has_medications=True, mode="meds")
        )
        assert rows == [
            [(texts.BTN_TAKE_ALL, f"rem:all:{self.key}"), (texts.BTN_PARTIAL, f"rem:partial:{self.key}")],
            [(texts.BTN_SNOOZE_30, f"rem:snooze:{self.key}")],
            [(texts.BTN_SKIP_SLOT, f"rem:skip:{self.key}")],
        ]

    def test_meds_mode_large_one_per_row(self) -> None:
        rows = buttons_of(
            keyboards.slot_message_kb(
                "morning", self.day, has_medications=True, mode="meds", large=True
            )
        )
        assert rows == [
            [(texts.BTN_TAKE_ALL, f"rem:all:{self.key}")],
            [(texts.BTN_PARTIAL, f"rem:partial:{self.key}")],
            [(texts.BTN_SNOOZE_30, f"rem:snooze:{self.key}")],
            [(texts.BTN_SKIP_SLOT, f"rem:skip:{self.key}")],
        ]
        assert all(len(row) == 1 for row in rows)

    def test_both_mode_two_pairs_and_skip(self) -> None:
        rows = buttons_of(
            keyboards.slot_message_kb("morning", self.day, has_medications=True, mode="both")
        )
        assert rows == [
            [(texts.BTN_MEASURE, f"rem:write:{self.key}"), (texts.BTN_TAKE_ALL, f"rem:all:{self.key}")],
            [(texts.BTN_PARTIAL, f"rem:partial:{self.key}"), (texts.BTN_SNOOZE_30, f"rem:snooze:{self.key}")],
            [(texts.BTN_SKIP_SLOT, f"rem:skip:{self.key}")],
        ]

    def test_both_mode_large_one_per_row(self) -> None:
        rows = buttons_of(
            keyboards.slot_message_kb(
                "morning", self.day, has_medications=True, mode="both", large=True
            )
        )
        assert rows == [
            [(texts.BTN_MEASURE, f"rem:write:{self.key}")],
            [(texts.BTN_TAKE_ALL, f"rem:all:{self.key}")],
            [(texts.BTN_PARTIAL, f"rem:partial:{self.key}")],
            [(texts.BTN_SNOOZE_30, f"rem:snooze:{self.key}")],
            [(texts.BTN_SKIP_SLOT, f"rem:skip:{self.key}")],
        ]
        assert all(len(row) == 1 for row in rows)

    def test_pressure_mode_one_per_row(self) -> None:
        rows = buttons_of(
            keyboards.slot_message_kb("morning", self.day, has_medications=True, mode="pressure")
        )
        assert rows == [
            [(texts.BTN_MEASURE, f"rem:write:{self.key}")],
            [(texts.BTN_SNOOZE_30, f"rem:snooze:{self.key}")],
            [(texts.BTN_SKIP_SLOT, f"rem:skip:{self.key}")],
        ]
