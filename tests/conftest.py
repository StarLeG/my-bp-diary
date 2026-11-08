"""Общие fixtures: in-memory БД, фабрики данных, моки aiogram (по мере надобности)."""
from __future__ import annotations

import threading
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, init_db
from bot.models import User


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    """In-memory SQLite с созданными таблицами и PRAGMA из продакшена."""
    engine = create_engine(":memory:")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх тестовой БД."""
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Сессия на тест: коммитит явно, в конце — rollback и закрытие."""
    session = session_factory()
    yield session
    await session.rollback()
    await session.close()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    """Пользователь с настройками по умолчанию."""
    user = User(telegram_id=111_111, timezone="Europe/Minsk")
    session.add(user)
    await session.commit()
    return user


# ─────────────────── Блокирующий код вне event loop (§7.2 п.7) ───────────────────


class LoopGate:
    """Заглушка тяжёлой синхронной функции (PDF, Argon2id), ждущая сигнала.

    Вызов отмечает `started` и висит, пока тест не вызовет `release()` (или
    `timeout` секунд). Если хендлер вызывает функцию прямо в event loop, весь
    кабинет стоит это время, и параллельный лёгкий запрос не отвечает —
    `assert_loop_free` падает. Если через `asyncio.to_thread` — висит только
    поток пула, лёгкий запрос проходит. Ожидание по событиям, а не по
    таймингам: исход не зависит от скорости машины.
    """

    def __init__(self, result: Any, timeout: float = 5.0) -> None:
        self.result = result
        self.timeout = timeout
        self.started = threading.Event()
        self._release = threading.Event()
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.started.set()
        self._release.wait(self.timeout)
        return self.result

    def release(self) -> None:
        self._release.set()

    def assert_loop_free(self, heavy: Callable[[], Any], light: Callable[[], Any]) -> tuple[Any, Any]:
        """Запустить `heavy` в фоне и, пока заглушка висит, выполнить `light`.

        Возвращает (результат heavy, результат light). Падает, если `light`
        не завершился, пока `heavy` ещё ждал внутри заглушки.
        """
        results: dict[str, Any] = {}
        heavy_thread = threading.Thread(target=lambda: results.__setitem__("heavy", heavy()))
        heavy_thread.start()
        try:
            assert self.started.wait(self.timeout), "тяжёлая функция так и не была вызвана"
            light_thread = threading.Thread(target=lambda: results.__setitem__("light", light()))
            light_thread.start()
            light_thread.join(self.timeout / 2)
            light_done_while_heavy = "light" in results and not self._release.is_set()
        finally:
            self.release()
            heavy_thread.join(self.timeout * 2)
        light_thread.join(self.timeout)
        assert light_done_while_heavy, (
            "лёгкий запрос не ответил, пока шла тяжёлая операция: она выполняется "
            "в event loop, а не в asyncio.to_thread"
        )
        return results.get("heavy"), results.get("light")


@pytest.fixture
def loop_gate() -> Callable[..., LoopGate]:
    """Фабрика `LoopGate(result)` — см. докстринг класса."""
    return LoopGate
