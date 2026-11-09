"""Тесты журнала удалений и ежечасного удаления (SPEC.md, §19, §20).

Покрывается: метка HMAC без telegram_id в файле, чистка строк старше 15
дней, выключенный журнал, ошибка записи не мешает удалению, `--apply` после
восстановления копии (сначала job'ы, потом `users`), удаление отозвавших
согласие в кабинете и job-обёртка.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.models import User
from bot.services import deletion_log
from bot.services.deletion_log import DeletionLog
from bot.services.measurements import delete_user_data

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
KEY = "test-deletion-key"


def make_log(tmp_path: Path, key: str = KEY) -> DeletionLog:
    return DeletionLog(tmp_path / "data" / "deletions.log", key)


def fake_reminders() -> MagicMock:
    reminders = MagicMock()
    reminders.remove_user_jobs = AsyncMock(return_value=0)
    return reminders


class TestDeletionLog:
    def test_mark_is_hmac_not_id(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        mark = log.mark(111_111)
        assert len(mark) == 64 and "111111" not in mark
        assert mark == make_log(tmp_path).mark(111_111)
        assert mark != make_log(tmp_path, "other-key").mark(111_111)

    def test_record_and_marks(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.record(111_111, NOW)
        text = log.path.read_text(encoding="utf-8")
        assert "111111" not in text
        assert log.marks(NOW) == {log.mark(111_111)}
        assert log.path.parent.is_dir()

    def test_old_lines_dropped(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.record(1, NOW - timedelta(days=16))
        log.record(2, NOW - timedelta(days=10))
        assert log.marks(NOW) == {log.mark(2)}
        log.record(3, NOW)
        lines = log.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # строка 16-дневной давности выброшена при записи

    def test_broken_lines_skipped(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.path.parent.mkdir(parents=True)
        log.path.write_text("garbage\n\tnot-a-date\nabc\t2026-11-10T09:00:00+00:00\n", encoding="utf-8")
        assert log.marks(NOW) == {"abc"}

    def test_no_file(self, tmp_path: Path) -> None:
        assert make_log(tmp_path).marks(NOW) == set()

    def test_disabled_without_key(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, key="")
        assert not log.enabled
        log.record(1, NOW)
        assert not log.path.exists()


class TestRecordDeletion:
    async def test_none_and_disabled(self, tmp_path: Path) -> None:
        await deletion_log.record_deletion(None, 1)
        await deletion_log.record_deletion(make_log(tmp_path, ""), 1)
        assert not (tmp_path / "data").exists()

    async def test_os_error_logged(self, tmp_path: Path, caplog, mocker) -> None:
        log = make_log(tmp_path)
        mocker.patch.object(DeletionLog, "record", side_effect=OSError("disk full"))
        with caplog.at_level(logging.ERROR):
            await deletion_log.record_deletion(log, 1)
        assert "журнал удалений" in caplog.text

    async def test_delete_user_data_writes_log(
        self, tmp_path: Path, session: AsyncSession, user: User
    ) -> None:
        log = make_log(tmp_path)
        assert await delete_user_data(session, fake_reminders(), user, log)
        assert log.marks() == {log.mark(111_111)}


class TestApply:
    async def test_apply_deletes_restored_accounts(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession], session: AsyncSession, user: User
    ) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.commit()
        log = make_log(tmp_path)
        log.record(111_111)
        reminders = fake_reminders()
        removed = await deletion_log.apply(session_factory, reminders, log)
        assert removed == 1
        reminders.remove_user_jobs.assert_awaited_once()
        async with session_factory() as s:
            ids = (await s.execute(select(User.telegram_id))).scalars().all()
        assert ids == [222_222]

    async def test_apply_empty_log(self, tmp_path: Path, session_factory, user: User) -> None:
        assert await deletion_log.apply(session_factory, fake_reminders(), make_log(tmp_path)) == 0


class TestPurgeRequested:
    async def test_purge_deletes_requested_only(
        self, tmp_path: Path, session_factory, session: AsyncSession, user: User
    ) -> None:
        other = User(telegram_id=222_222, deletion_requested_at=NOW)
        session.add(other)
        await session.commit()
        log = make_log(tmp_path)
        reminders = fake_reminders()
        assert await deletion_log.purge_requested(session_factory, reminders, log) == 1
        reminders.remove_user_jobs.assert_awaited_once_with(other.id)
        assert log.marks() == {log.mark(222_222)}
        async with session_factory() as s:
            ids = (await s.execute(select(User.telegram_id))).scalars().all()
        assert ids == [111_111]

    async def test_fire_purge_unbound(self, caplog) -> None:
        deletion_log.bind_purge(None)
        with caplog.at_level(logging.ERROR):
            await deletion_log.fire_purge()
        assert "bind_purge" in caplog.text

    async def test_fire_purge_bound(self, tmp_path: Path, session_factory, session, caplog) -> None:
        session.add(User(telegram_id=333_333, deletion_requested_at=NOW))
        await session.commit()
        deletion_log.bind_purge(session_factory, fake_reminders(), make_log(tmp_path))
        try:
            with caplog.at_level(logging.INFO):
                await deletion_log.fire_purge()
        finally:
            deletion_log.bind_purge(None)
        assert "отзыва согласия" in caplog.text

    async def test_fire_purge_nothing_to_do(self, tmp_path: Path, session_factory, caplog) -> None:
        deletion_log.bind_purge(session_factory, fake_reminders(), make_log(tmp_path))
        try:
            with caplog.at_level(logging.INFO):
                await deletion_log.fire_purge()
        finally:
            deletion_log.bind_purge(None)
        assert "отзыва согласия" not in caplog.text

    async def test_fire_purge_error_logged(self, mocker, caplog) -> None:
        deletion_log.bind_purge(MagicMock(), fake_reminders(), None)
        mocker.patch.object(deletion_log, "purge_requested", side_effect=RuntimeError("boom"))
        try:
            with caplog.at_level(logging.ERROR):
                await deletion_log.fire_purge()
        finally:
            deletion_log.bind_purge(None)
        assert "не удалось" in caplog.text

    def test_schedule_purge(self) -> None:
        scheduler = MagicMock()
        deletion_log.schedule_purge(scheduler)
        kwargs = scheduler.add_job.call_args.kwargs
        assert scheduler.add_job.call_args.args[0] is deletion_log.fire_purge
        assert kwargs["id"] == deletion_log.PURGE_JOB_ID and kwargs["replace_existing"]
