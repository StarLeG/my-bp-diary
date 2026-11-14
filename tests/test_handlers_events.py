"""Тесты «➕ Добавить запись» (bot/handlers/events.py, SPEC.md, §6.14; 0.6.0)."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import events
from bot.models import DiaryEvent, Medication, MedicationLog
from bot.models import User as UserModel
from bot.services import measurements as measurements_service
from bot.services.events import dumps_payload

TG_ID = 111_111


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(
    data: str, message: Message | None = None, user_id: int = TG_ID
) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message or make_message(user_id=user_id),
    )


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер с параметрами по именам (как aiogram DI)."""
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
def reminders(mocker):
    return mocker.Mock(ensure_remeasure=mocker.AsyncMock())


async def tap(state: FSMContext, data: str, session: AsyncSession, **kwargs) -> None:
    """Не используется в тестах напрямую; оставлено для совместимости импортов."""
    raise NotImplementedError


class TestMenu:
    async def test_add_menu_shows_seven_types(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            events.cb_add_menu, callback=make_callback("add:menu"), state=state
        )
        assert edit.call_args.args[0] == texts.EVENT_MENU_ASK
        data = {
            button.callback_data
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        assert data == {
            "add:bp",
            "add:med",
            "add:walk",
            "add:food",
            "add:stress",
            "add:sleep",
            "add:note",
            "menu:back",
        }

    async def test_cancel_returns_to_menu_screen(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            events.cb_add_cancel, callback=make_callback("add:cancel"), state=state
        )
        assert edit.call_args.args[0] == texts.EVENT_MENU_ASK


class TestWalk:
    async def test_pick_duration_then_save(
        self, session, edit, cb_answer, answer
    ) -> None:
        """Прогулка: длительность → «Сейчас» → событие `activity` (R17 = Б)."""
        state = make_state()
        await call(
            events.cb_add_walk, callback=make_callback("add:walk"), state=state, session=session
        )
        assert edit.call_args.args[0] == texts.WALK_ASK
        await call(
            events.cb_add_walk_pick,
            callback=make_callback("add:walk:20"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_TIME_ASK
        await call(
            events.cb_add_time_now,
            callback=make_callback("add:time:now"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.FACTOR_DONE
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.type == "activity"
        assert json.loads(row.payload) == {"duration_min": 20}

    async def test_bad_duration_stale(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            events.cb_add_walk_pick,
            callback=make_callback("add:walk:15"),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)


class TestFood:
    async def test_one_screen_saves_with_marks(
        self, session, edit, cb_answer
    ) -> None:
        """K1 = А: метки и время — один экран; «Готово» сохраняет «сейчас»."""
        state = make_state()
        await call(
            events.cb_add_food, callback=make_callback("add:food"), state=state
        )
        for mark in ("salt", "coffee"):
            await call(
                events.cb_add_food_pick,
                callback=make_callback(f"add:food:pick:{mark}"),
                state=state,
            )
        await call(
            events.cb_add_food_done,
            callback=make_callback("add:food:done"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.FACTOR_DONE
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.type == "food"
        assert json.loads(row.payload) == {"marks": ["coffee", "salt"]}

    async def test_done_without_marks_toast(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(events.cb_add_food, callback=make_callback("add:food"), state=state)
        cb_answer.reset_mock()
        await call(
            events.cb_add_food_done,
            callback=make_callback("add:food:done"),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.FACTOR_EMPTY)
        assert (await session.execute(select(DiaryEvent))).scalars().all() == []

    async def test_other_time_without_input_toast(self, session, edit, cb_answer) -> None:
        """V6: «Готово» при «Другое время» без ввода — тост «Введите время»."""
        state = make_state()
        await call(events.cb_add_food, callback=make_callback("add:food"), state=state)
        await call(
            events.cb_add_food_pick,
            callback=make_callback("add:food:pick:salt"),
            state=state,
        )
        await call(
            events.cb_add_food_time,
            callback=make_callback("add:food:time:other"),
            state=state,
            session=session,
        )
        cb_answer.reset_mock()
        await call(
            events.cb_add_food_done,
            callback=make_callback("add:food:done"),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.EVENT_TIME_NEEDED)

    async def test_back_from_time_returns_with_marks(
        self, session, edit, cb_answer
    ) -> None:
        """M2: «Отмена» ввода времени — на экран питания с метками и «Сейчас»."""
        state = make_state()
        await call(events.cb_add_food, callback=make_callback("add:food"), state=state)
        await call(
            events.cb_add_food_pick,
            callback=make_callback("add:food:pick:alcohol"),
            state=state,
        )
        await call(
            events.cb_add_food_time,
            callback=make_callback("add:food:time:other"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_back, callback=make_callback("add:back"), state=state, session=session
        )
        assert "Питание" in edit.call_args.args[0]
        labels = {
            button.text
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        assert f"✓ {texts.FACTOR_LABELS['alcohol']}" in labels
        assert f"✓ {texts.EVENT_TIME_NOW}" in labels


class TestStressAndNote:
    async def test_stress_variant_then_skip_text(
        self, session, edit, cb_answer
    ) -> None:
        state = make_state()
        await call(
            events.cb_add_stress, callback=make_callback("add:stress"), state=state
        )
        assert edit.call_args.args[0] == texts.STRESS_ASK
        await call(
            events.cb_add_stress_pick,
            callback=make_callback("add:stress:anxiety"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_TEXT_PROMPT  # «Пропустить» есть
        await call(
            events.cb_add_text_skip,
            callback=make_callback("add:text:skip"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_time_now,
            callback=make_callback("add:time:now"),
            state=state,
            session=session,
        )
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.type == "stress"
        assert json.loads(row.payload) == {"variant": "anxiety"}
        assert row.note is None

    async def test_stress_other_requires_text(
        self, session, edit, cb_answer, answer
    ) -> None:
        """«Другое» без «Пропустить»; пустой текст — «Напишите, что чувствуете» (V1)."""
        state = make_state()
        await call(
            events.cb_add_stress_pick,
            callback=make_callback("add:stress:other"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_TEXT_REQUIRED
        markup = edit.call_args.kwargs["reply_markup"]
        data = {b.callback_data for row in markup.inline_keyboard for b in row}
        assert data == {"add:back"}  # без «Пропустить» (M6)
        await call(
            events.msg_event_text,
            message=make_message("   "),
            state=state,
            session=session,
        )
        answer.assert_awaited_once_with(texts.EVENT_TEXT_REQUIRED)

    async def test_note_text_and_time(self, session, edit, cb_answer, answer) -> None:
        state = make_state()
        await call(
            events.cb_add_note, callback=make_callback("add:note"), state=state, session=session
        )
        assert edit.call_args.args[0] == texts.NOTE_ASK
        await call(
            events.msg_event_text,
            message=make_message("Выспался наконец"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_time_now,
            callback=make_callback("add:time:now"),
            state=state,
            session=session,
        )
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.type == "note"
        assert row.note == "Выспался наконец"

    async def test_note_empty_rejected(self, session, edit, cb_answer, answer) -> None:
        state = make_state()
        await call(
            events.cb_add_note, callback=make_callback("add:note"), state=state, session=session
        )
        await call(
            events.msg_event_text,
            message=make_message("  "),
            state=state,
            session=session,
        )
        answer.assert_awaited_once_with("Заметка не может быть пустой")


class TestSleep:
    async def test_one_rating_per_day_updates(self, session, edit, cb_answer) -> None:
        """C23: повторный ввод сна за сутки обновляет событие (одно на сутки)."""
        state = make_state()
        await call(
            events.cb_add_sleep, callback=make_callback("add:sleep"), state=state
        )
        await call(
            events.cb_add_sleep_pick,
            callback=make_callback("add:sleep:little"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_sleep_pick,
            callback=make_callback("add:sleep:much"),
            state=state,
            session=session,
        )
        rows = (await session.execute(select(DiaryEvent))).scalars().all()
        assert len(rows) == 1
        assert json.loads(rows[0].payload) == {"rating": "much"}


class TestTime:
    async def test_other_time_within_24h(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        state = make_state()
        await call(
            events.cb_add_note, callback=make_callback("add:note"), state=state, session=session
        )
        await call(
            events.msg_event_text, message=make_message("метка"), state=state, session=session
        )
        await call(
            events.cb_add_time_other,
            callback=make_callback("add:time:other"),
            state=state,
            session=session,
        )
        await call(
            events.msg_event_time,
            message=make_message("00:05"),
            state=state,
            session=session,
            reminders=reminders,
        )
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.moment.tzinfo is not None

    async def test_bad_time_rejected(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        state = make_state()
        await state.set_state(events.EventInput.time)
        await state.update_data(**{events.EV_TYPE: "note"})
        await call(
            events.msg_event_time,
            message=make_message("25:99"),
            state=state,
            session=session,
            reminders=reminders,
        )
        answer.assert_awaited_once_with("Нужно время в формате ЧЧ:ММ (например, 08:30)")


class TestCardEditDelete:
    async def _event(self, session: AsyncSession, user) -> DiaryEvent:
        from bot.services import events as events_service

        event = await events_service.create_event(
            session,
            user,
            "activity",
            datetime.now(timezone.utc),
            payload=dumps_payload({"duration_min": 30}),
        )
        await session.commit()
        return event

    async def test_open_card_and_delete(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        event = await self._event(session, user)
        state = make_state()
        await call(
            events.cb_event_open,
            callback=make_callback(f"ev:open:{event.id}"),
            state=state,
            session=session,
        )
        assert "Прогулка: 30 минут" in edit.call_args.args[0]
        await call(
            events.cb_event_del,
            callback=make_callback(f"ev:del:{event.id}"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_DEL_CONFIRM
        await call(
            events.cb_event_del,
            callback=make_callback(f"ev:del:{event.id}:ok"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_DELETED
        assert (await session.execute(select(DiaryEvent))).scalars().all() == []

    async def test_edit_keeps_moment(self, session, edit, cb_answer) -> None:
        """V9: в правке шаг времени — «Оставить {момент}» / «Другое»."""
        user = await measurements_service.get_or_create_user(session, TG_ID)
        event = await self._event(session, user)
        state = make_state()
        await call(
            events.cb_event_edit,
            callback=make_callback(f"ev:edit:{event.id}"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_TIME_ASK
        labels = [
            button.text
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert labels[0].startswith("🕓 Оставить")
        # «Оставить {момент}» → ev:save:{id} — обновление без смены момента
        await call(
            events.cb_event_save,
            callback=make_callback(f"ev:save:{event.id}"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_UPDATED
        row = (await session.execute(select(DiaryEvent))).scalars().one()
        assert row.moment == event.moment

    async def test_foreign_event_hidden(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        event = await self._event(session, user)
        state = make_state(user_id=222_222)
        await call(
            events.cb_event_open,
            callback=make_callback(f"ev:open:{event.id}", user_id=222_222),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)


class TestMedFacade:
    async def test_empty_meds_hint(self, session, edit, cb_answer) -> None:
        """C10: без лекарств — «Сначала добавьте лекарства в настройках»."""
        state = make_state()
        await call(
            events.cb_add_med, callback=make_callback("add:med"), state=state, session=session
        )
        assert edit.call_args.args[0] == texts.MEDS_NONE_HINT

    async def test_pick_status_saves_log(
        self, session, edit, cb_answer, reminders
    ) -> None:
        """B3/C4: приём → статус → «Сейчас» → `medication_logs`."""
        user = await measurements_service.get_or_create_user(session, TG_ID)
        med = Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning")
        session.add(med)
        await session.commit()
        state = make_state()
        await call(
            events.cb_add_med_pick,
            callback=make_callback(f"add:med:pick:{med.id}"),
            state=state,
            session=session,
        )
        assert "Лозартан 50 мг · утро" in edit.call_args.args[0]
        await call(
            events.cb_add_med_status,
            callback=make_callback(f"add:med:status:taken:{med.id}"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.EVENT_TIME_ASK
        await call(
            events.cb_add_med_time,
            callback=make_callback("add:med:time:now"),
            state=state,
            session=session,
            reminders=reminders,
        )
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == "taken"
        reminders.ensure_remeasure.assert_awaited_once()  # taken → повтор (C1)

    async def test_skipped_no_remeasure(
        self, session, edit, cb_answer, reminders
    ) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        user.med_remeasure_delay = 60
        await session.commit()
        med = Medication(user_id=user.id, name="Амлодипин", dosage="5 мг", slot="evening")
        session.add(med)
        await session.commit()
        state = make_state()
        await call(
            events.cb_add_med_pick,
            callback=make_callback(f"add:med:pick:{med.id}"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_med_status,
            callback=make_callback(f"add:med:status:skipped:{med.id}"),
            state=state,
            session=session,
        )
        await call(
            events.cb_add_med_time,
            callback=make_callback("add:med:time:now"),
            state=state,
            session=session,
            reminders=reminders,
        )
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert [log.status for log in logs] == ["skipped"]
        reminders.ensure_remeasure.assert_not_awaited()
