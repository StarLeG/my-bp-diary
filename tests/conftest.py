"""Общие fixtures: in-memory БД, фабрики данных, моки aiogram (по мере надобности)."""
from __future__ import annotations

import threading
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, init_db
from bot.models import Base, User
from bot.services.consent import CONSENT_VERSION, DOCTOR_CONSENT_VERSION

# ─────────────────── Согласие по умолчанию в тестовых БД (§6.11) ───────────────────
#
# С 0.5.0 без согласия пациент видит только экран согласия, напоминания не
# уходят, врач пациента не видит. Тесты, написанные до 0.5.0, проверяют своё
# (запись, лекарства, кабинет, врача) и о согласии не знают: в их БД триггеры
# SQLite дают согласие каждому новому пациенту, врачу и связи врач–пациент —
# и для вставок через ORM, и для сырых SQL-вставок. Тесты самих согласий
# помечаются `@pytest.mark.real_consent` — у них триггеров нет.

_CONSENT_TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS test_consent_user AFTER INSERT ON users
    BEGIN
        INSERT OR IGNORE INTO consents (user_id, kind, version, channel, given_at)
        VALUES (NEW.id, 'processing', '{CONSENT_VERSION}', 'bot', CURRENT_TIMESTAMP);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS test_consent_doctor AFTER INSERT ON doctors
    WHEN NEW.consent_at IS NULL
    BEGIN
        UPDATE doctors SET consent_version = '{DOCTOR_CONSENT_VERSION}',
            consent_at = CURRENT_TIMESTAMP, consent_channel = 'bot'
        WHERE id = NEW.id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS test_consent_link AFTER INSERT ON doctor_patients
    BEGIN
        INSERT OR IGNORE INTO consents (user_id, kind, doctor_id, version, channel, given_at)
        SELECT NEW.user_id, 'doctor', NEW.doctor_id, version, 'bot', CURRENT_TIMESTAMP
        FROM (SELECT ? AS version);
    END""".replace("?", f"'{CONSENT_VERSION}'"),
    f"""CREATE TRIGGER IF NOT EXISTS test_consent_relink AFTER UPDATE OF status ON doctor_patients
    WHEN NEW.status IN ('pending', 'active')
    BEGIN
        INSERT OR IGNORE INTO consents (user_id, kind, doctor_id, version, channel, given_at)
        VALUES (NEW.user_id, 'doctor', NEW.doctor_id, '{CONSENT_VERSION}', 'bot', CURRENT_TIMESTAMP);
    END""",
    # Согласие на приглашение нового врача (Б7) у пациента есть всегда: при
    # создании и снова — как только прежнее привязано к врачу или закрыто.
    f"""CREATE TRIGGER IF NOT EXISTS test_consent_invite AFTER INSERT ON users
    BEGIN
        INSERT OR IGNORE INTO consents (user_id, kind, version, channel, given_at)
        VALUES (NEW.id, 'doctor', '{CONSENT_VERSION}', 'bot', CURRENT_TIMESTAMP);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS test_consent_invite_again AFTER UPDATE ON consents
    WHEN OLD.kind = 'doctor' AND OLD.doctor_id IS NULL
    BEGIN
        INSERT OR IGNORE INTO consents (user_id, kind, version, channel, given_at)
        VALUES (OLD.user_id, 'doctor', '{CONSENT_VERSION}', 'bot', CURRENT_TIMESTAMP);
    END""",
)


def _doctor_consent_defaults(_mapper, _connection, target) -> None:
    """Врач, созданный через ORM, — сразу с согласием и в памяти, а не только в БД."""
    from datetime import datetime, timezone

    if target.consent_at is None:
        target.consent_version = DOCTOR_CONSENT_VERSION
        target.consent_at = datetime.now(timezone.utc)
        target.consent_channel = "bot"


def _create_consent_triggers(target, connection, **_kw) -> None:
    """После `create_all` (init_db) — триггеры согласия по умолчанию."""
    names = {row[0] for row in connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {"users", "consents", "doctors", "doctor_patients"} <= names:
        return
    for ddl in _CONSENT_TRIGGERS:
        connection.exec_driver_sql(ddl)


@pytest.fixture(autouse=True)
def auto_consent(request):
    """Согласие по умолчанию для тестов до 0.5.0; `real_consent` — без него."""
    if request.node.get_closest_marker("real_consent"):
        yield
        return
    from sqlalchemy import event

    from bot.models import Doctor

    event.listen(Base.metadata, "after_create", _create_consent_triggers)
    event.listen(Doctor, "before_insert", _doctor_consent_defaults)
    try:
        yield
    finally:
        event.remove(Base.metadata, "after_create", _create_consent_triggers)
        event.remove(Doctor, "before_insert", _doctor_consent_defaults)


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
