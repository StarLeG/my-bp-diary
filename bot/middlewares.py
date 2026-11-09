"""Middleware: сессия БД на апдейт, debounce и throttle (SPEC.md, §7.1–7.2).

- `DbSessionMiddleware` — сессия SQLAlchemy живёт в рамках одного апдейта
  и передаётся хендлерам в `data["session"]`; закрывается всегда (finally).
- `DebounceMiddleware` — 1.5 с на callback-кнопки флоу записи давления (`m:`):
  повторный тап той же кнопки игнорируется, ответ «Уже записываю…». Правка
  буфера калькулятора (цифры, «⌫», «C») и чекбоксы мультивыбора лекарств
  дебаунсом не защищаются — экран редактируется на каждое нажатие (§6.3).
- `ThrottleMiddleware` — не более одного действия в секунду на остальные
  callback'и: быстрые повторы игнорируются молча (снятие «часиков»).
- `DoctorUsernameMiddleware` — на каждое сообщение врача обновляет его
  `doctors.telegram_username`, если он сменился (§21, T10): по нему врача
  находят пациенты.

Время берётся из инжектируемого `clock` (по умолчанию `time.monotonic`) —
тесты управляют временем без sleep. Служебные словари живут в одном процессе
и используются только для защиты от двойных нажатий; записи старше окна
периодически выбрасываются (память не растёт).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts

#: Окно debounce для кнопок флоу записи давления (секунды).
DEBOUNCE_SECONDS = 1.5

#: Окно общего throttle для остальных callback'ов (секунды).
THROTTLE_SECONDS = 1.0

#: Как часто чистить служебные словари от записей старше окна (секунды).
CLEANUP_INTERVAL = 60.0

#: Callback-префикс флоу записи давления (`m:key:5`, `m:cancel`, …).
FLOW_PREFIX = "m:"

#: Ключ сессии БД в data хендлеров.
SESSION_KEY = "session"

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _drop_stale(seen: dict[Any, float], now: float, ttl: float) -> None:
    """Выбросить записи старше `ttl`: они уже не подавят повтор.

    Вызывается не чаще `CLEANUP_INTERVAL` (см. классы ниже), между чистками
    словарь живёт без изменений.
    """
    cutoff = now - ttl
    for key in [key for key, stamp in seen.items() if stamp < cutoff]:
        del seen[key]


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
        self._last_cleanup = clock()

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
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            _drop_stale(self._last_seen, now, self._seconds)
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
        self._last_cleanup = clock()

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
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            _drop_stale(self._last_seen, now, self._seconds)
        return await handler(event, data)


class DoctorUsernameMiddleware(BaseMiddleware):
    """Обновить username врача при каждом его сообщении (SPEC.md, §21, T10).

    Сначала чтение (врач ли это и сменился ли username) — запись только при
    расхождении, так что обычные сообщения пациентов БД не нагружают.
    Ошибка обновления не мешает обработке сообщения.
    """

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        session = data.get(SESSION_KEY)
        if isinstance(event, Message) and event.from_user is not None and session is not None:
            from bot.services import doctor as doctor_service

            try:
                await doctor_service.touch_username(
                    session, event.from_user.id, event.from_user.username
                )
            except Exception:  # noqa: BLE001 — сообщение важнее username
                await session.rollback()
        return await handler(event, data)


#: Команды, которые проверка согласия пропускает (§6.11): `/start` сам
#: показывает Б1, сторона врача и админка — со своими правилами.
_CONSENT_EXEMPT_COMMANDS = ("/start", "/iamdoctor", "/admin")

#: Callback'и, доступные без согласия: экран согласия, удаление аккаунта,
#: сторона врача (`doc:*`), админка (`adm:*`).
_CONSENT_EXEMPT_CALLBACKS = ("cons:", "doc:", "adm:")
_CONSENT_EXEMPT_EXACT = frozenset({"set:delete", "set:delete:ok"})


def _consent_exempt(event: TelegramObject) -> bool:
    """Апдейт, который проверка согласия пропускает без вопросов (§6.11)."""
    if isinstance(event, Message):
        command = (event.text or "").split(maxsplit=1)[0].split("@", 1)[0] if event.text else ""
        return command in _CONSENT_EXEMPT_COMMANDS
    if isinstance(event, CallbackQuery):
        data = event.data or ""
        return data in _CONSENT_EXEMPT_EXACT or data.startswith(_CONSENT_EXEMPT_CALLBACKS)
    return True


class ConsentMiddleware(BaseMiddleware):
    """Без действующего согласия пациент видит только экран согласия (§6.11).

    Блокирует апдейт, только если у отправителя есть строка `users` с
    `onboarded=1` и нет действующего основного согласия: новые пользователи
    и врачи без дневника обрабатываются `/start`. Согласие есть, но есть
    связи с врачом без согласия на этого врача (до 0.5.0) — экран Д4.
    На кнопку — всплывающая подсказка и экран новым сообщением, на
    сообщение — сразу экран.
    """

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        session = data.get(SESSION_KEY)
        from_user = getattr(event, "from_user", None)
        if session is None or from_user is None or _consent_exempt(event):
            return await handler(event, data)
        from bot.handlers import consent as consent_handlers
        from bot.services import consent as consent_service
        from bot.services import measurements as measurements_service

        user = await measurements_service.get_user(session, from_user.id)
        if user is None or not user.onboarded:
            return await handler(event, data)
        if not await consent_service.has_processing(session, user.id):
            screen = consent_handlers.consent_screen(existing=True)
            toast = texts.CONSENT_REQUIRED
        else:
            confirm = await consent_handlers.next_confirm(session, user.id)
            if confirm is None:
                return await handler(event, data)
            screen, toast = confirm, None
        state = data.get("state")
        if state is not None:
            await state.clear()
        text, markup = screen
        if isinstance(event, CallbackQuery):
            await event.answer(toast)
            if event.message is not None:
                await event.message.answer(
                    text, reply_markup=markup, link_preview_options=_no_preview()
                )
        else:
            await event.answer(text, reply_markup=markup, link_preview_options=_no_preview())  # type: ignore[union-attr]
        return None


def _no_preview():
    """Без превью ссылки на политику (экран согласия короткий)."""
    from bot.keyboards import NO_LINK_PREVIEW

    return NO_LINK_PREVIEW
