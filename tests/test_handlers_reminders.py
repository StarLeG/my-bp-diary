"""Тесты хендлеров кнопок напоминаний (bot/handlers/reminders.py, SPEC.md, §6.7)."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import reminders
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
        expected = texts.MEDS_LOGGED.format(
            summary=texts.fmt_meds_summary(["Лозартан 50 мг"], [])
        )
        assert edit.call_args.args[0] == expected
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

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


class TestPartial:
    async def test_start_marks_all(self, session, user, edit, cb_answer, reminders_svc) -> None:
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        med2 = await add_med(session, user, "Амлодипин", "5 мг", slot="morning")
        state = make_state()
        await call(
            reminders.cb_partial_start,
            callback=make_callback("rem:partial:morning", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.PARTIAL_PROMPT
        # slot_medications сортирует по названию: Амлодипин, затем Лозартан.
        medications = [(med2.id, "Амлодипин", "5 мг"), (med1.id, "Лозартан", "50 мг")]
        assert edit.call_args.kwargs["reply_markup"] == keyboards.partial_meds_kb(
            checked={med1.id, med2.id}, medications=medications
        )
        data = await state.get_data()
        assert data[reminders.P_PARTIAL_SLOT] == "morning"

    async def test_toggle_unchecks(self, session, user, edit, cb_answer, reminders_svc) -> None:
        med = await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: [med.id]}
        )
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:med:{med.id}", make_message()),
            state=state,
            session=session,
        )
        medications = [(med.id, "Лозартан", "50 мг")]
        assert edit.call_args.kwargs["reply_markup"] == keyboards.partial_meds_kb(
            checked=set(), medications=medications
        )
        data = await state.get_data()
        assert data[reminders.P_PARTIAL_CHECKED] == []

    async def test_toggle_checks_back(self, session, user, edit, cb_answer, reminders_svc) -> None:
        med = await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: []}
        )
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:med:{med.id}", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert data[reminders.P_PARTIAL_CHECKED] == [med.id]

    async def test_foreign_med_ignored(self, session, user, edit, cb_answer, reminders_svc) -> None:
        await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: []}
        )
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback("rem:med:999", make_message()),
            state=state,
            session=session,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_done_logs_taken_and_skipped(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        med1 = await add_med(session, user, "Лозартан", "50 мг")
        med2 = await add_med(session, user, "Амлодипин", "5 мг")
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: [med1.id]}
        )
        await call(
            reminders.cb_partial_done,
            callback=make_callback("rem:partial:done", make_message()),
            state=state,
            session=session,
            reminders=reminders_svc,
        )
        by_id = {row.medication_id: row.status for row in await logs_of(session, user.id)}
        assert by_id == {med1.id: "taken", med2.id: "skipped"}
        expected = texts.MEDS_LOGGED.format(
            summary=texts.fmt_meds_summary(["Лозартан 50 мг"], ["Амлодипин 5 мг"])
        )
        assert edit.call_args.args[0] == expected
        assert await state.get_state() is None

    async def test_done_schedules_remeasure(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        user.med_remeasure_delay = 30
        med = await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: [med.id]}
        )
        await call(
            reminders.cb_partial_done,
            callback=make_callback("rem:partial:done", make_message()),
            state=state,
            session=session,
            reminders=reminders_svc,
        )
        reminders_svc.ensure_remeasure.assert_awaited_once_with(user.id, None, 30)

    async def test_done_without_state_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        state = make_state()
        await call(
            reminders.cb_partial_done,
            callback=make_callback("rem:partial:done", make_message()),
            state=state,
            session=session,
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_cancel_returns_slot_screen(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        med = await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: [med.id]}
        )
        await call(
            reminders.cb_partial_cancel,
            callback=make_callback("rem:partial:cancel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_slot_message(
            "morning", [texts.fmt_med_item("Лозартан", "50 мг")]
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slot_message_kb(
            "morning", 0, has_medications=True
        )
        assert await state.get_state() is None

    async def test_cancel_without_state_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        state = make_state()
        await call(
            reminders.cb_partial_cancel,
            callback=make_callback("rem:partial:cancel", make_message()),
            state=state,
            session=session,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_start_garbage_slot_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        await call(
            reminders.cb_partial_start,
            callback=make_callback("rem:partial:zzz", make_message()),
            state=make_state(),
            session=session,
        )
        edit.assert_not_called()

    async def test_start_empty_slot_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        await call(
            reminders.cb_partial_start,
            callback=make_callback("rem:partial:morning", make_message()),
            state=make_state(),
            session=session,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited()

    async def test_done_no_remeasure_when_disabled(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        user.med_remeasure_delay = None
        med = await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: [med.id]}
        )
        await call(
            reminders.cb_partial_done,
            callback=make_callback("rem:partial:done", make_message()),
            state=state,
            session=session,
            reminders=reminders_svc,
        )
        reminders_svc.ensure_remeasure.assert_not_awaited()
        edit.assert_called()

    async def test_toggle_without_state_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        med = await add_med(session, user)
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback(f"rem:med:{med.id}", make_message()),
            state=make_state(),
            session=session,
        )
        edit.assert_not_called()

    async def test_toggle_garbage_id_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        await add_med(session, user)
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: []}
        )
        await call(
            reminders.cb_partial_toggle,
            callback=make_callback("rem:med:abc", make_message()),
            state=state,
            session=session,
        )
        edit.assert_not_called()

    async def test_done_empty_slot_ignored(
        self, session, user, edit, cb_answer, reminders_svc
    ) -> None:
        state = make_state()
        await state.update_data(
            **{reminders.P_PARTIAL_SLOT: "morning", reminders.P_PARTIAL_CHECKED: []}
        )
        await call(
            reminders.cb_partial_done,
            callback=make_callback("rem:partial:done", make_message()),
            state=state,
            session=session,
            reminders=reminders_svc,
        )
        edit.assert_not_called()
        assert await logs_of(session, user.id) == []


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

