"""Middleware: сессия БД на апдейт, debounce и throttle (SPEC.md, §7.1–7.2).

- `DbSessionMiddleware` — сессия SQLAlchemy живёт в рамках одного апдейта
  и передаётся хендлерам в `data["session"]`; закрывается всегда (finally).
- `DebounceMiddleware` — 1.5 с на callback-кнопки флоу записи давления (`m:`):
  повторный тап той же кнопки игнорируется, ответ «Уже записываю…». Правка
  буфера калькулятора (цифры, «⌫», «C») и чекбоксы мультивыбора лекарств
  дебаунсом не защищаются — экран редактируется на каждое нажатие (§6.3).
- `ThrottleMiddleware` — не более одного действия в секунду на остальные
  callback'и: быстрые повторы игнорируются молча (снятие «часиков»).

Время берётся из инжектируемого `clock` (по умолчанию `time.monotonic`) —
тесты управляют временем без sleep. Служебные словари живут в одном процессе
и используются только для защиты от двойных нажатий (память: один float на
пользователя).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts

#: Окно debounce для кнопок флоу записи давления (секунды).
DEBOUNCE_SECONDS = 1.5

#: Окно общего throttle для остальных callback'ов (секунды).
THROTTLE_SECONDS = 1.0

#: Callback-префикс флоу записи давления (`m:key:5`, `m:cancel`, …).
FLOW_PREFIX = "m:"

#: Ключ сессии БД в data хендлеров.
SESSION_KEY = "session"

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Сессия БД на каждый апдейт (SPEC.md, §7.1): одна сессия — один апдейт."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        async with self._session_factory() as session:
            data[SESSION_KEY] = session
            return await handler(event, data)


def _debounce_exempt(callback_data: str) -> bool:
    """Действия без записи в БД: правка буфера калькулятора и чекбоксы (§6.3).

    Они обязаны срабатывать на каждое нажатие («экран редактируется на каждое
    нажатие»), поэтому из-под дебаунса выведены; защита от двойного нажатия
    нужна только действиям, которые сохраняют данные («Готово», «Да», …).
    """
    if callback_data.startswith("m:med:pick:"):
        return True
    if callback_data.startswith("m:key:"):
        action = callback_data[len("m:key:") :]
        return action.isdigit() or action in {"bksp", "clear"}
    return False


class DebounceMiddleware(BaseMiddleware):
    """Защита от двойных тапов в флоу записи давления (SPEC.md, §7.2, п. 4).

    Окно считается по паре (пользователь, callback_data): игнорируется именно
    повторный тап той же кнопки, нажатия разных кнопок флоу не задерживаются.
    """

    def __init__(
        self,
        seconds: float = DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._last_seen: dict[tuple[int, str], float] = {}

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if (
            not isinstance(event, CallbackQuery)
            or not event.data
            or not event.data.startswith(FLOW_PREFIX)
            or _debounce_exempt(event.data)
        ):
            return await handler(event, data)
        key = (event.from_user.id, event.data)
        now = self._clock()
        last = self._last_seen.get(key)
        if last is not None and now - last < self._seconds:
            await event.answer(texts.DEBOUNCE_NOTICE)
            return None
        self._last_seen[key] = now
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Общий throttle: не более одного действия в секунду на callback'и (§7.2)."""

    def __init__(
        self,
        seconds: float = THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if (
            not isinstance(event, CallbackQuery)
            or not event.data
            or event.data.startswith(FLOW_PREFIX)
        ):
            return await handler(event, data)
        now = self._clock()
        last = self._last_seen.get(event.from_user.id)
        if last is not None and now - last < self._seconds:
            await event.answer()  # снять «часики», без текста
            return None
        self._last_seen[event.from_user.id] = now
        return await handler(event, data)
