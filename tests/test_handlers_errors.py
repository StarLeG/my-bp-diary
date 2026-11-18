"""Тесты информации об ошибках для пользователя (0.7.0, ST1-А; §12).

`bot/handlers/__init__.py::on_error`: системные сбои (БД) → «⚙️ Бот временно
недоступен…» (дословно), прочие → «Что-то пошло не так…»; ответ на сообщение —
новым сообщением с «В меню», на callback — предупреждением без сообщения.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent, Update
from sqlalchemy.exc import OperationalError

from bot import keyboards, texts
from bot.handlers import on_error


def make_event(exception: BaseException, *, with_message: bool = True) -> ErrorEvent:
    update = MagicMock(spec=Update)
    if with_message:
        update.message = MagicMock()
        update.message.answer = AsyncMock()
        update.callback_query = None
    else:
        update.message = None
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
    return ErrorEvent(update=update, exception=exception)


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("database is locked"))


class TestOnError:
    @pytest.mark.parametrize(
        "exception, expected",
        [
            (db_error(), texts.ERROR_SYSTEMIC),
            (Valueerror := ValueError("битый ввод"), texts.ERROR_GENERIC),
            (RuntimeError("что-то"), texts.ERROR_GENERIC),
        ],
    )
    async def test_reply_text_by_exception_kind(
        self, exception: BaseException, expected: str
    ) -> None:
        event = make_event(exception)
        assert await on_error(event) is True
        event.update.message.answer.assert_awaited_once()
        assert event.update.message.answer.await_args.args[0] == expected

    async def test_systemic_text_is_verbatim(self) -> None:
        """Текст ST1-А утверждён дословно — сверяем целиком (§12)."""
        assert texts.ERROR_SYSTEMIC == (
            "⚙️ Бот временно недоступен — идёт восстановление. "
            "Ваши данные в безопасности, попробуйте через несколько минут."
        )

    async def test_message_reply_has_menu_button(self) -> None:
        event = make_event(db_error())
        await on_error(event)
        markup = event.update.message.answer.await_args.kwargs["reply_markup"]
        assert markup == keyboards.back_to_menu()

    async def test_callback_reply_is_alert_without_message(self) -> None:
        event = make_event(ValueError("x"), with_message=False)
        assert await on_error(event) is True
        event.update.callback_query.answer.assert_awaited_once_with(
            texts.ERROR_GENERIC, show_alert=True
        )

    async def test_expired_callback_answer_falls_back_to_message(self) -> None:
        """Алерт доставить нельзя (запрос устарел) — сбой сообщением (2026-11-18).

        Ответ на callback живёт ~30 с: без фолбэка пользователь не получал
        ничего (находка «бот в телефоне не отвечает»).
        """
        event = make_event(ValueError("сбой"), with_message=False)
        event.update.callback_query.answer = AsyncMock(
            side_effect=TelegramBadRequest(
                method="answerCallbackQuery",
                message="Bad Request: query is too old and response timeout expired"
                " or query ID is invalid",
            )
        )
        event.update.callback_query.message = MagicMock()
        event.update.callback_query.message.answer = AsyncMock()
        await on_error(event)
        message_answer = event.update.callback_query.message.answer
        message_answer.assert_awaited_once()
        assert message_answer.await_args.args[0] == texts.ERROR_GENERIC
        markup = message_answer.await_args.kwargs["reply_markup"]
        assert "menu:back" in repr(markup)  # кнопка «В меню», как у ответа на сообщение

    async def test_reply_failure_does_not_raise(self) -> None:
        """Сбой ответа об ошибке не должен ронять диспетчер (§12)."""
        event = make_event(db_error())
        event.update.message.answer = AsyncMock(side_effect=RuntimeError("нет сети"))
        assert await on_error(event) is True
