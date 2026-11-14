"""Тесты middleware (bot/middlewares.py): сессия на апдейт, debounce, throttle."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.middlewares import (
    CLEANUP_INTERVAL,
    DEBOUNCE_SECONDS,
    SESSION_KEY,
    THROTTLE_SECONDS,
    DbSessionMiddleware,
    DebounceMiddleware,
    ThrottleMiddleware,
)


class FakeClock:
    """Управляемое время вместо time.monotonic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_callback(data: str | None, user_id: int = 1) -> CallbackQuery:
    """CallbackQuery; data=None — событие без callback_data."""
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
    )


def make_message() -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
    )


@pytest.fixture
def answer_mock(mocker):
    """Мок answer() у CallbackQuery — проверяем ответы пользователю."""
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


# ─────────────────────────── DbSessionMiddleware ───────────────────────────


class TestDbSessionMiddleware:
    async def test_session_injected_into_data(
        self, session_factory: async_sessionmaker[AsyncSession], user, mocker
    ) -> None:
        close_spy = mocker.spy(AsyncSession, "close")
        seen: list[AsyncSession] = []

        async def handler(event, data):
            session = data[SESSION_KEY]
            assert isinstance(session, AsyncSession)
            seen.append(session)
            return "ok"

        middleware = DbSessionMiddleware(session_factory)
        result = await middleware(handler, make_message(), {})
        assert result == "ok"
        assert len(seen) == 1
        assert any(call.args[0] is seen[0] for call in close_spy.call_args_list)

    async def test_session_usable_in_handler(
        self, session_factory: async_sessionmaker[AsyncSession], user
    ) -> None:
        from sqlalchemy import select

        from bot.models import User as UserModel

        async def handler(event, data):
            rows = (await data[SESSION_KEY].execute(select(UserModel))).scalars().all()
            return len(rows)

        middleware = DbSessionMiddleware(session_factory)
        assert await middleware(handler, make_message(), {}) == 1

    async def test_session_closed_on_handler_error(
        self, session_factory: async_sessionmaker[AsyncSession], mocker
    ) -> None:
        close_spy = mocker.spy(AsyncSession, "close")
        seen: list[AsyncSession] = []

        async def handler(event, data):
            seen.append(data[SESSION_KEY])
            raise RuntimeError("упал")

        middleware = DbSessionMiddleware(session_factory)
        with pytest.raises(RuntimeError):
            await middleware(handler, make_message(), {})
        assert any(call.args[0] is seen[0] for call in close_spy.call_args_list)


# ─────────────────────────── DebounceMiddleware ───────────────────────────


class TestDebounceMiddleware:
    async def test_first_flow_tap_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        result = await middleware(handler, make_callback("m:med:yes"), {})
        assert result == "ok"
        handler.assert_awaited_once()
        answer_mock.assert_not_awaited()

    async def test_double_tap_blocked(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        await middleware(handler, make_callback("m:med:yes"), {})
        second = await middleware(handler, make_callback("m:med:yes"), {})
        assert second is None
        handler.assert_awaited_once()
        answer_mock.assert_awaited_once_with(texts.DEBOUNCE_NOTICE)

    async def test_passes_after_window(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = DebounceMiddleware(clock=clock)
        await middleware(handler, make_callback("m:med:yes"), {})
        clock.advance(DEBOUNCE_SECONDS + 0.1)
        assert await middleware(handler, make_callback("m:med:yes"), {}) == "ok"
        assert handler.await_count == 2

    async def test_still_blocked_inside_window(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = DebounceMiddleware(clock=clock)
        await middleware(handler, make_callback("m:med:yes"), {})
        clock.advance(DEBOUNCE_SECONDS - 0.1)
        assert await middleware(handler, make_callback("m:med:yes"), {}) is None
        assert handler.await_count == 1

    async def test_users_are_independent(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        await middleware(handler, make_callback("m:med:yes", user_id=1), {})
        assert await middleware(handler, make_callback("m:med:yes", user_id=2), {}) == "ok"
        assert handler.await_count == 2

    async def test_different_buttons_not_blocked(self, answer_mock) -> None:
        """Окно — по паре (пользователь, callback_data): разные кнопки флоу не ждут."""
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback("m:med:yes"), {}) == "ok"
        assert await middleware(handler, make_callback("m:note"), {}) == "ok"
        assert handler.await_count == 2

    async def test_confirm_actions_debounced(self, answer_mock) -> None:
        """Пишущие в БД («Готово», «Сейчас», …) — двойной тап глушится (V2/V4/V5)."""
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        await middleware(handler, make_callback("m:key:ok"), {})
        assert await middleware(handler, make_callback("m:key:ok"), {}) is None
        handler2 = AsyncMock(return_value="ok")
        await middleware(handler2, make_callback("add:food:done"), {})
        assert await middleware(handler2, make_callback("add:food:done"), {}) is None
        handler3 = AsyncMock(return_value="ok")
        await middleware(handler3, make_callback("ev:del:7:ok"), {})
        assert await middleware(handler3, make_callback("ev:del:7:ok"), {}) is None

    @pytest.mark.parametrize(
        "data",
        ["m:key:5", "m:key:0", "m:key:bksp", "m:key:clear", "m:med:pick:7"],
    )
    async def test_screen_edits_exempt(self, answer_mock, data: str) -> None:
        """Цифры, «⌫», «C» и чекбоксы — на каждое нажатие (§6.3, §7.2)."""
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        await middleware(handler, make_callback(data), {})
        assert await middleware(handler, make_callback(data), {}) == "ok"
        assert handler.await_count == 2
        answer_mock.assert_not_awaited()

    async def test_non_flow_callback_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback("menu:stats"), {}) == "ok"
        assert await middleware(handler, make_callback("menu:stats"), {}) == "ok"
        assert handler.await_count == 2
        answer_mock.assert_not_awaited()

    async def test_message_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        assert await middleware(handler, make_message(), {}) == "ok"
        handler.assert_awaited_once()

    async def test_callback_without_data_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = DebounceMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback(None), {}) == "ok"

    async def test_stale_entries_dropped(self, answer_mock) -> None:
        """Словарь не растёт: записи старше окна выбрасываются при чистке (§7.2)."""
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = DebounceMiddleware(clock=clock)
        await middleware(handler, make_callback("m:med:yes", user_id=1), {})
        await middleware(handler, make_callback("m:med:yes", user_id=2), {})
        assert len(middleware._last_seen) == 2
        clock.advance(CLEANUP_INTERVAL + DEBOUNCE_SECONDS)
        await middleware(handler, make_callback("m:med:no", user_id=3), {})
        assert list(middleware._last_seen) == [(3, "m:med:no")]

    async def test_fresh_entries_survive_cleanup(self, answer_mock) -> None:
        """Чистка не трогает записи внутри окна — подавление повтора не ломается."""
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = DebounceMiddleware(clock=clock)
        clock.advance(CLEANUP_INTERVAL - 0.5)
        await middleware(handler, make_callback("m:med:yes", user_id=1), {})
        clock.advance(1.0)  # окно чистки наступило, запись user1 младше окна
        await middleware(handler, make_callback("m:med:yes", user_id=2), {})
        assert (1, "m:med:yes") in middleware._last_seen
        assert len(middleware._last_seen) == 2
        # повтор user1 всё ещё глушится: запись пережила чистку
        assert await middleware(handler, make_callback("m:med:yes", user_id=1), {}) is None


# ─────────────────────────── ThrottleMiddleware ───────────────────────────


class TestThrottleMiddleware:
    async def test_first_tap_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback("menu:stats"), {}) == "ok"
        handler.assert_awaited_once()
        answer_mock.assert_not_awaited()

    async def test_rapid_tap_blocked_silently(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        await middleware(handler, make_callback("menu:stats"), {})
        second = await middleware(handler, make_callback("menu:diary"), {})
        assert second is None
        handler.assert_awaited_once()
        answer_mock.assert_awaited_once_with()  # без текста — только снять «часики»

    async def test_passes_after_window(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = ThrottleMiddleware(clock=clock)
        await middleware(handler, make_callback("menu:stats"), {})
        clock.advance(THROTTLE_SECONDS + 0.1)
        assert await middleware(handler, make_callback("menu:stats"), {}) == "ok"
        assert handler.await_count == 2

    async def test_flow_callbacks_not_throttled(self, answer_mock) -> None:
        # Флоу записи контролирует DebounceMiddleware; throttle его пропускает.

        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback("m:med:yes"), {}) == "ok"
        assert await middleware(handler, make_callback("m:note"), {}) == "ok"
        assert handler.await_count == 2

    async def test_users_are_independent(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        await middleware(handler, make_callback("menu:stats", user_id=1), {})
        assert await middleware(handler, make_callback("menu:stats", user_id=2), {}) == "ok"
        assert handler.await_count == 2

    async def test_message_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        assert await middleware(handler, make_message(), {}) == "ok"
        handler.assert_awaited_once()

    async def test_callback_without_data_passes(self, answer_mock) -> None:
        handler = AsyncMock(return_value="ok")
        middleware = ThrottleMiddleware(clock=FakeClock())
        assert await middleware(handler, make_callback(None), {}) == "ok"

    async def test_stale_entries_dropped(self, answer_mock) -> None:
        """Словарь не растёт: записи старше окна выбрасываются при чистке (§7.2)."""
        handler = AsyncMock(return_value="ok")
        clock = FakeClock()
        middleware = ThrottleMiddleware(clock=clock)
        await middleware(handler, make_callback("menu:stats", user_id=1), {})
        await middleware(handler, make_callback("menu:stats", user_id=2), {})
        assert len(middleware._last_seen) == 2
        clock.advance(CLEANUP_INTERVAL + THROTTLE_SECONDS)
        await middleware(handler, make_callback("menu:stats", user_id=3), {})
        assert list(middleware._last_seen) == [3]
