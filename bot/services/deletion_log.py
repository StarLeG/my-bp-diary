"""Журнал удалений аккаунтов (SPEC.md, §19, §20).

Резервные копии хранятся до 14 дней: удалённый аккаунт ещё живёт в них. Если
базу восстановят из копии, удалённые аккаунты нужно удалить снова — для этого
журнал хранит метку аккаунта `HMAC-SHA256(DELETION_LOG_KEY, telegram_id)` и
дату удаления, без других данных. По метке нельзя узнать `telegram_id` без
ключа, но можно проверить, удалён ли конкретный аккаунт.

Файл журнала — вне файла БД и `BACKUP_DIR`: восстановление копии его не
перезаписывает. Строки старше `RETENTION` (15 дней) удаляются при каждой
записи: копий старше 14 дней нет.

Формат строки: `<метка hex>\t<дата ISO 8601 UTC>`.

Запуск после ручного восстановления копии:

    python -m bot.services.deletion_log --apply
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.models import User

if TYPE_CHECKING:  # pragma: no cover
    from bot.services.reminders import RemindersService

logger = logging.getLogger(__name__)

#: Сколько хранится строка журнала: копии живут не дольше 14 дней (§20).
RETENTION = timedelta(days=15)

#: Запись и чистка файла — из потоков (`asyncio.to_thread`), под одним замком.
_file_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeletionLog:
    """Журнал удалений: запись метки, чтение действующих меток, чистка."""

    def __init__(self, path: str | Path, key: str) -> None:
        """`key` — `DELETION_LOG_KEY`; пустой ключ — журнал выключен."""
        self._path = Path(path)
        self._key = key.encode("utf-8")

    @property
    def enabled(self) -> bool:
        """Журнал ведётся, только если задан ключ (бэкапы включены, §3)."""
        return bool(self._key)

    @property
    def path(self) -> Path:
        """Файл журнала."""
        return self._path

    def mark(self, telegram_id: int) -> str:
        """Метка аккаунта: HMAC-SHA256 от `telegram_id` (hex)."""
        return hmac.new(self._key, str(telegram_id).encode("ascii"), hashlib.sha256).hexdigest()

    def _read(self) -> list[tuple[str, datetime]]:
        """Строки журнала; битые строки пропускаются."""
        if not self._path.exists():
            return []
        entries: list[tuple[str, datetime]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            mark, _, stamp = line.partition("\t")
            try:
                moment = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if mark:
                entries.append((mark, moment.astimezone(timezone.utc)))
        return entries

    def _write(self, entries: list[tuple[str, datetime]]) -> None:
        """Перезаписать журнал атомарно (временный файл → замена)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            "".join(f"{mark}\t{moment.isoformat()}\n" for mark, moment in entries),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def record(self, telegram_id: int, now: Optional[datetime] = None) -> None:
        """Записать удаление аккаунта и выбросить строки старше 15 дней."""
        if not self.enabled:
            return
        now = now or _utcnow()
        with _file_lock:
            entries = [entry for entry in self._read() if now - entry[1] <= RETENTION]
            entries.append((self.mark(telegram_id), now))
            self._write(entries)

    def marks(self, now: Optional[datetime] = None) -> set[str]:
        """Метки, удалённые не раньше 15 дней назад."""
        now = now or _utcnow()
        with _file_lock:
            return {mark for mark, moment in self._read() if now - moment <= RETENTION}


async def record_deletion(log: Optional[DeletionLog], telegram_id: int) -> None:
    """Записать удаление (файловый ввод-вывод — вне event loop, §7.2 п.7)."""
    if log is None or not log.enabled:
        return
    try:
        await asyncio.to_thread(log.record, telegram_id)
    except OSError:
        # Журнал — страховка восстановления; удаление аккаунта важнее.
        logger.exception("Не удалось записать журнал удалений")


async def apply(
    session_factory: async_sessionmaker[AsyncSession],
    reminders: "RemindersService",
    log: DeletionLog,
) -> int:
    """Удалить аккаунты из журнала (после восстановления копии, §19).

    Порядок — как у обычного удаления: сначала job'ы, потом `users`.
    Возвращает число удалённых аккаунтов.
    """
    from bot.services.measurements import delete_user_data

    marks = await asyncio.to_thread(log.marks)
    if not marks:
        return 0
    async with session_factory() as session:
        users = list((await session.execute(select(User))).scalars())
        targets = [user for user in users if log.mark(user.telegram_id) in marks]
        removed = 0
        for user in targets:
            if await delete_user_data(session, reminders, user):
                removed += 1
    return removed


async def purge_requested(
    session_factory: async_sessionmaker[AsyncSession],
    reminders: "RemindersService",
    log: Optional[DeletionLog],
) -> int:
    """Удалить аккаунты, отозвавшие согласие в кабинете (К5, §19).

    Кабинет не управляет планировщиком бота, поэтому удаление делает бот:
    сначала job'ы, потом `users`, затем журнал. Возвращает число удалённых.
    """
    from bot.services.consent import users_to_delete
    from bot.services.measurements import delete_user_data

    async with session_factory() as session:
        removed = 0
        for user in await users_to_delete(session):
            if await delete_user_data(session, reminders, user, log):
                removed += 1
    return removed


# ─────────────────── Ежечасный job (module-level, как у бэкапа) ───────────────────

#: Id job'а ежечасного удаления отозвавших согласие в кабинете.
PURGE_JOB_ID = "purge_withdrawn"

_purge: Optional[tuple] = None


def bind_purge(
    session_factory: Optional[async_sessionmaker[AsyncSession]],
    reminders: Optional["RemindersService"] = None,
    log: Optional[DeletionLog] = None,
) -> None:
    """Привязать зависимости к job-точке входа `fire_purge` (`None` — отвязать)."""
    global _purge
    _purge = None if session_factory is None else (session_factory, reminders, log)


def schedule_purge(scheduler) -> None:
    """Ежечасный job удаления (UTC), идемпотентно (`replace_existing`)."""
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        fire_purge,
        trigger=IntervalTrigger(hours=1, timezone="UTC"),
        id=PURGE_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )


async def fire_purge() -> None:
    """Точка входа ежечасного job'а: ошибки — в лог, планировщик не падает."""
    if _purge is None:
        logger.error("Сработал job удаления без привязки (bind_purge)")
        return
    try:
        removed = await purge_requested(*_purge)
    except Exception:
        logger.exception("Удаление отозвавших согласие не удалось")
        return
    if removed:
        logger.info("Удалено аккаунтов после отзыва согласия в кабинете: %s", removed)


async def _main() -> int:  # pragma: no cover — ручной запуск на сервере
    """`--apply`: удалить из восстановленной БД аккаунты из журнала."""
    from bot.config import get_settings
    from bot.db import create_engine, create_session_factory, init_db
    from bot.main import build_scheduler
    from bot.services.reminders import RemindersService

    settings = get_settings()
    log = DeletionLog(settings.deletion_log_path, settings.deletion_log_key)
    if not log.enabled:
        print("DELETION_LOG_KEY не задан — журнала нет.", file=sys.stderr)
        return 1
    engine = create_engine(settings.db_path)
    await init_db(engine)
    factory = create_session_factory(engine)
    scheduler = build_scheduler(settings.db_path)
    scheduler.start(paused=True)

    async def _no_send(*_args: object) -> None:
        return None

    try:
        removed = await apply(factory, RemindersService(factory, _no_send, scheduler), log)
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
    print(f"Удалено аккаунтов из журнала: {removed}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] != ["--apply"]:
        print("Использование: python -m bot.services.deletion_log --apply", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main()))
