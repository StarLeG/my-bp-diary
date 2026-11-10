"""Устаревшие кнопки (SPEC.md, §8, 0.5.5, U8).

Общий последний обработчик отвечает «Это сообщение устарело» на любой
callback, который не принял никто другой; места аудита «тихих» ответов
отвечают тем же уведомлением.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from bot import texts
from bot.handlers import fallback_router, measure, settings, stale_callback

TG_ID = 111_111


def make_callback(data: str) -> CallbackQuery:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=TG_ID, type="private"),
        from_user=User(id=TG_ID, is_bot=True, first_name="Бот"),
        text="…",
    )
    return CallbackQuery(
        id="q1",
        from_user=User(id=TG_ID, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message,
    )


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID))


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


class TestFallback:
    async def test_unknown_callback_stale(self, cb_answer, edit) -> None:
        await stale_callback(make_callback("zzz:unknown"))
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)
        edit.assert_not_called()

    def test_registered_last_for_callbacks(self) -> None:
        """Обработчик без фильтров — в `fallback_router`, который подключён последним."""
        from bot.handlers import router

        assert router.sub_routers[-1] is fallback_router
        handlers = fallback_router.callback_query.handlers
        assert [h.callback for h in handlers] == [stale_callback]
        assert handlers[0].filters == []

    def test_text(self) -> None:
        assert texts.STALE_MESSAGE == "Это сообщение устарело"


class TestAuditPlaces:
    """Места аудита §8, переведённые на «Это сообщение устарело»."""

    async def test_slot_toggle(self, session, user, cb_answer, edit, mocker) -> None:
        await settings.cb_slot_toggle(make_callback("set:slot:zzz"), session, mocker.Mock())
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)
        edit.assert_not_called()

    async def test_slot_time(self, session, user, cb_answer, edit, mocker) -> None:
        await settings.cb_slot_time(make_callback("set:time:zzz:show"), make_state(), session, mocker.Mock())
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)

    async def test_med_slot(self, cb_answer, edit) -> None:
        await settings.cb_med_slot(make_callback("med:slot:zzz"), make_state())
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)

    async def test_archive_date(self, session, user, cb_answer, edit) -> None:
        await measure.cb_archive_date(make_callback("set:archive:date:zzz"), make_state(), session)
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)
