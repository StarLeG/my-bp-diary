"""Административная панель: статистика, здоровье системы, рассылка (SPEC.md, §18).

Приватность (§18): администратор не видит личные медицинские данные. Запросы к
`measurements` возвращают только `COUNT(*)` и `COUNT(DISTINCT user_id)` —
никаких цифр давления, заметок и названий лекарств конкретных пользователей.

Кэш статистики — 5 минут (§18.2, «Обновить» сбрасывает его). Рассылка
(§18.5): `asyncio.gather` + `Semaphore(20)`, пауза 0.05 с на отправку,
`TelegramForbiddenError` (заблокировал бота) не прерывает отправку.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.models import Measurement, Medication, Reminder, User
from bot.services import consent as consent_service
from bot.services import profile as profile_service

logger = logging.getLogger(__name__)

#: Кэш метрик статистики (§18.2: «кэширование на 5 минут»).
STATS_CACHE_TTL = timedelta(minutes=5)

#: Слоты напоминаний для метрики популярности (§18.2: утро, обед, вечер).
SLOT_TYPES = ("morning", "noon", "evening")

#: Ограничение конкурентности рассылки (не заDDoSить Telegram API).
BROADCAST_CONCURRENCY = 20

#: Пауза между отправками (§18.5: не более 30 сообщений/сек).
BROADCAST_PAUSE = 0.05

#: Как часто обновлять прогресс рассылки (§18.5: «Отправлено 150 из 500...»).
BROADCAST_PROGRESS_EVERY = 10


def read_app_version(project_root: str | Path | None = None) -> str:
    """Версия приложения из `pyproject.toml` (§18.4); не найдено — «unknown»."""
    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = data["project"]["version"]
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError):
        return "unknown"
    return str(version)


class ErrorCounter(logging.Handler):
    """Счётчик ошибок в логах за скользящее окно (§18.4, «счётчик в памяти»).

    Подключается к корневому логгеру; учитывает записи уровня ERROR и выше
    (уровень задан самим хендлером).
    """

    def __init__(self, window: timedelta = timedelta(hours=24)) -> None:
        super().__init__(level=logging.ERROR)
        self._window = window
        self._events: deque[datetime] = deque()

    def emit(self, record: logging.LogRecord) -> None:
        self._events.append(datetime.now(timezone.utc))
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    @property
    def count(self) -> int:
        """Ошибок за окно (по умолчанию — 24 часа)."""
        self._prune()
        return len(self._events)


@dataclass
class DbStats:
    """Сводка по БД (§18.2): только агрегаты, без содержимого таблиц."""

    users: int
    dau: int
    """Активные за 24 часа: `COUNT(DISTINCT user_id)` по `measurements`."""

    wau: int
    """Активные за 7 дней: `COUNT(DISTINCT user_id)` по `measurements`."""

    measurements: int
    medications: int
    db_size: int
    """Размер основного файла SQLite в байтах (`os.path.getsize`)."""

    wal_size: int
    """Размер WAL-файла в байтах (0, если файла нет)."""

    slots_enabled: dict[str, int] = field(default_factory=dict)
    """Сколько пользователей включили напоминания по слотам (§18.2)."""

    consented: int = 0
    """Сколько пользователей дали действующее основное согласие (§18.2, §6.11)."""

    profiles: int = 0
    """Сколько пациентов заполнили данные для врача (§18.2, §6.12) — только счётчик."""


@dataclass(frozen=True)
class SystemHealth:
    """Здоровье системы (§18.4)."""

    uptime_seconds: int
    app_version: str
    python_version: str
    scheduler_running: bool
    jobs_count: int
    errors_24h: int


@dataclass(frozen=True)
class UserCard:
    """Карточка пользователя для админки (§18.6) — без мед-данных."""

    telegram_id: int
    created_at: Optional[datetime]
    timezone: str
    measurements_count: int
    onboarded: bool
    reminder_slots: tuple[str, ...]
    consent_at: Optional[datetime] = None
    """Когда дано действующее основное согласие (None — согласия нет, §18.6)."""


@dataclass(frozen=True)
class BroadcastResult:
    """Итог рассылки (§18.5: «Доставлено: X, Заблокировали: Y»)."""

    delivered: int
    blocked: int
    failed: int


async def broadcast_message(
    bot: Bot,
    user_ids: Sequence[int],
    text: str,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> BroadcastResult:
    """Массовая рассылка (§18.5): `gather` + `Semaphore(20)` + пауза 0.05 с.

    Каждый `send_message` обёрнут в `try/except TelegramForbiddenError`
    (пользователь заблокировал бота): ошибка логируется и не прерывает
    рассылку. `on_progress(done, total)` вызывается каждые
    `BROADCAST_PROGRESS_EVERY` отправок.
    """
    total = len(user_ids)
    semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)
    done = 0
    done_lock = asyncio.Lock()

    async def _one(user_id: int) -> str:
        nonlocal done
        async with semaphore:
            try:
                await bot.send_message(user_id, text, parse_mode=ParseMode.MARKDOWN)
                await asyncio.sleep(BROADCAST_PAUSE)
                status = "delivered"
            except TelegramForbiddenError:
                status = "blocked"
                logger.info("Рассылка: пользователь %s заблокировал бота", user_id)
            except TelegramAPIError as exc:
                status = "failed"
                logger.warning("Рассылка: ошибка отправки %s: %s", user_id, exc)
        async with done_lock:
            done += 1
            notify = on_progress is not None and done % BROADCAST_PROGRESS_EVERY == 0
            current = done
        if notify:
            await on_progress(current, total)
        return status

    statuses = await asyncio.gather(*(_one(user_id) for user_id in user_ids))
    return BroadcastResult(
        delivered=statuses.count("delivered"),
        blocked=statuses.count("blocked"),
        failed=statuses.count("failed"),
    )


class AdminService:
    """Статистика, здоровье системы и карточки пользователей (SPEC.md, §18).

    Сессия БД — на операцию (§7.2); все запросы к `measurements` — только
    агрегаты (§18, приватность). `scheduler` — APScheduler приложения (для
    здоровья системы), `error_counter` — счётчик ошибок логов за 24 часа.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_path: str,
        scheduler=None,
        *,
        error_counter: Optional[ErrorCounter] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._db_path = db_path
        self._scheduler = scheduler
        self._error_counter = error_counter
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._started_monotonic = time.monotonic()
        self._stats_cache: Optional[tuple[datetime, DbStats]] = None

    # ─────────────────────────── Статистика БД (§18.2) ───────────────────────────

    async def get_stats(self, force: bool = False) -> DbStats:
        """Сводка по БД; кэш на 5 минут, `force=True` — пересчитать («Обновить»)."""
        if not force and self._stats_cache is not None:
            cached_at, cached = self._stats_cache
            if self._now() - cached_at < STATS_CACHE_TTL:
                return cached
        stats = await self._collect_stats()
        self._stats_cache = (self._now(), stats)
        return stats

    async def _collect_stats(self) -> DbStats:
        now = self._now()
        async with self._session_factory() as session:
            users = (
                await session.execute(select(func.count(User.id)))
            ).scalar_one()
            measurements = (
                await session.execute(select(func.count(Measurement.id)))
            ).scalar_one()
            medications = (
                await session.execute(select(func.count(Medication.id)))
            ).scalar_one()
            dau = (
                await session.execute(
                    select(func.count(func.distinct(Measurement.user_id))).where(
                        Measurement.timestamp >= now - timedelta(hours=24)
                    )
                )
            ).scalar_one()
            wau = (
                await session.execute(
                    select(func.count(func.distinct(Measurement.user_id))).where(
                        Measurement.timestamp >= now - timedelta(days=7)
                    )
                )
            ).scalar_one()
            consented = await consent_service.count_with_processing(session)
            profiles = await profile_service.count_filled(session)
            slot_rows = (
                await session.execute(
                    select(Reminder.type, func.count())
                    .where(Reminder.enabled.is_(True), Reminder.type.in_(SLOT_TYPES))
                    .group_by(Reminder.type)
                )
            ).all()
        slots_enabled = {slot: 0 for slot in SLOT_TYPES}
        for slot_type, count in slot_rows:
            slots_enabled[slot_type] = count
        return DbStats(
            users=users,
            dau=dau,
            wau=wau,
            measurements=measurements,
            medications=medications,
            db_size=self._file_size(self._db_path),
            wal_size=self._file_size(self._db_path + "-wal"),
            slots_enabled=slots_enabled,
            consented=consented,
            profiles=profiles,
        )

    @staticmethod
    def _file_size(path: str) -> int:
        """Размер файла БД (`os.path.getsize`; `VACUUM` не используется)."""
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    # ─────────────────────────── Здоровье системы (§18.4) ───────────────────────────

    def get_health(self) -> SystemHealth:
        """Аптайм, версии, планировщик и ошибки за 24 часа (§18.4)."""
        jobs = self._scheduler.get_jobs() if self._scheduler is not None else []
        return SystemHealth(
            uptime_seconds=int(time.monotonic() - self._started_monotonic),
            app_version=read_app_version(),
            python_version=platform.python_version(),
            scheduler_running=(
                bool(self._scheduler.running) if self._scheduler is not None else False
            ),
            jobs_count=len(jobs),
            errors_24h=self._error_counter.count if self._error_counter is not None else 0,
        )

    # ─────────────────────────── Поиск пользователя (§18.6) ───────────────────────────

    async def find_user(self, telegram_id: int) -> Optional[UserCard]:
        """Карточка пользователя (§18.6) — только агрегаты, без мед-данных."""
        async with self._session_factory() as session:
            user = (
                await session.execute(select(User).where(User.telegram_id == telegram_id))
            ).scalar_one_or_none()
            if user is None:
                return None
            count = (
                await session.execute(
                    select(func.count(Measurement.id)).where(
                        Measurement.user_id == user.id
                    )
                )
            ).scalar_one()
            slots = (
                await session.execute(
                    select(Reminder.type).where(
                        Reminder.user_id == user.id, Reminder.enabled.is_(True)
                    )
                )
            ).scalars().all()
            consent = await consent_service.current_processing(session, user.id)
        return UserCard(
            telegram_id=user.telegram_id,
            created_at=user.created_at,
            timezone=user.timezone,
            measurements_count=count,
            onboarded=bool(user.onboarded),
            reminder_slots=tuple(slots),
            consent_at=consent.given_at if consent is not None else None,
        )

    # ─────────────────────────── Рассылка (§18.5) ───────────────────────────

    async def all_user_ids(self) -> list[int]:
        """Все `telegram_id` пользователей — получатели рассылки (§18.5)."""
        async with self._session_factory() as session:
            rows = (await session.execute(select(User.telegram_id))).scalars().all()
        return list(rows)
