"""Тесты резервного копирования (bot/services/backup.py, SPEC.md, §20).

Проверяются: VACUUM INTO + integrity_check, AES-GCM (без незашифрованной
копии на диске), sidecar-статус, retention GFS (7 ежедневных + 4
еженедельных), совместимость по `PRAGMA user_version`, ежедневный job
03:00 UTC и off-site в S3 (мок boto3).
"""
from __future__ import annotations

import hashlib
import pickle
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from bot.db import create_engine, init_db
from bot.services import backup as bkp
from bot.services.backup import (
    MAGIC,
    BackupError,
    BackupService,
    S3Uploader,
    bind_backup,
    decrypt_file,
    derive_key,
    encrypt_file,
    fire_backup,
)

FIXED_NOW = datetime(2026, 11, 5, 3, 0, 0, tzinfo=timezone.utc)
SECRET = "test-secret"


class FakeScheduler:
    """Лёгкий фейк APScheduler: фиксирует add_job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, **kwargs) -> str:
        self.jobs[kwargs["id"]] = {"func": func, **kwargs}
        return kwargs["id"]


@pytest.fixture(autouse=True)
def restore_binding() -> Iterator[None]:
    """Изоляция: прежняя привязка сервиса восстанавливается после теста."""
    previous = bkp._service
    yield
    bkp.bind_backup(previous)


@pytest.fixture
async def db_file(tmp_path: Path) -> Path:
    """Файловая SQLite с одной строкой: VACUUM INTO требует путь к файлу."""
    path = tmp_path / "bot.sqlite3"
    engine = create_engine(str(path))
    await init_db(engine)
    await engine.dispose()
    return path


def make_service(db_path: Path, backup_dir: Path, **kwargs) -> BackupService:
    return BackupService(
        str(db_path),
        backup_dir,
        kwargs.pop("encryption_key", SECRET),
        now=kwargs.pop("now", lambda: FIXED_NOW),
        **kwargs,
    )


def write_backup_file(backup_dir: Path, name: str, size: int = 10) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    path.write_bytes(b"x" * size)
    return path


class TestCrypto:
    """Шифрование AES-GCM (§20): магия + nonce + шифротекст."""

    def test_derive_key_is_sha256(self) -> None:
        assert derive_key("abc") == hashlib.sha256(b"abc").digest()

    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain.sqlite3"
        enc = tmp_path / "plain.aes"
        out = tmp_path / "out.sqlite3"
        plain.write_bytes(b"payload-data")
        key = derive_key(SECRET)
        encrypt_file(plain, enc, key)
        raw = enc.read_bytes()
        assert raw.startswith(MAGIC)
        assert b"payload-data" not in raw  # содержимое не в открытом виде
        decrypt_file(enc, out, key)
        assert out.read_bytes() == b"payload-data"

    def test_decrypt_with_wrong_key_raises(self, tmp_path: Path) -> None:
        plain = tmp_path / "p"
        enc = tmp_path / "f.aes"
        plain.write_bytes(b"data")
        encrypt_file(plain, enc, derive_key("right"))
        with pytest.raises(BackupError):
            decrypt_file(enc, tmp_path / "out", derive_key("wrong"))

    def test_decrypt_without_magic_raises(self, tmp_path: Path) -> None:
        enc = tmp_path / "f.aes"
        enc.write_bytes(b"not-a-backup")
        with pytest.raises(BackupError):
            decrypt_file(enc, tmp_path / "out", derive_key(SECRET))


class TestCreateBackup:
    """Создание копии (§20): проверка целостности, шифрование, очистка."""

    async def test_creates_encrypted_copy_and_removes_plain(
        self, db_file: Path, tmp_path: Path
    ) -> None:
        backup_dir = tmp_path / "backups"
        service = make_service(db_file, backup_dir)
        result = await service.create_backup()
        assert result.name == "backup-20261105-030000.sqlite3.aes"
        assert result.integrity_ok is True
        assert result.size_bytes > 0
        enc_path = backup_dir / result.name
        assert enc_path.exists()
        assert enc_path.read_bytes().startswith(MAGIC)
        # Незашифрованная копия не остаётся на диске.
        assert list(backup_dir.glob("*.sqlite3")) == []
        assert result.uploaded_url is None

    async def test_without_key_raises(self, db_file: Path, tmp_path: Path) -> None:
        service = make_service(db_file, tmp_path / "backups", encryption_key="")
        with pytest.raises(BackupError):
            await service.create_backup()
        assert not (tmp_path / "backups").exists() or not any(
            (tmp_path / "backups").iterdir()
        )

    async def test_failed_integrity_removes_files(
        self, db_file: Path, tmp_path: Path, mocker
    ) -> None:
        backup_dir = tmp_path / "backups"
        service = make_service(db_file, backup_dir)
        mocker.patch.object(
            BackupService, "_integrity_check", new=mocker.AsyncMock(return_value=False)
        )
        with pytest.raises(BackupError):
            await service.create_backup()
        assert list(backup_dir.iterdir()) == []

    async def test_upload_success_sets_url(
        self, db_file: Path, tmp_path: Path, mocker
    ) -> None:
        uploader = mocker.Mock()
        uploader.upload = mocker.AsyncMock(return_value="s3://bucket/backups/x.aes")
        service = make_service(db_file, tmp_path / "backups", uploader=uploader)
        result = await service.create_backup()
        assert result.uploaded_url == "s3://bucket/backups/x.aes"
        assert result.upload_error is None

    async def test_upload_failure_keeps_local_copy(
        self, db_file: Path, tmp_path: Path, mocker
    ) -> None:
        uploader = mocker.Mock()
        uploader.upload = mocker.AsyncMock(side_effect=RuntimeError("s3 недоступен"))
        service = make_service(db_file, tmp_path / "backups", uploader=uploader)
        result = await service.create_backup()
        assert result.uploaded_url is None
        assert "s3 недоступен" in (result.upload_error or "")
        assert (tmp_path / "backups" / result.name).exists()

    async def test_status_sidecar_survives_restart(
        self, db_file: Path, tmp_path: Path
    ) -> None:
        backup_dir = tmp_path / "backups"
        result = await make_service(db_file, backup_dir).create_backup()
        # Новый экземпляр — как после рестарта процесса.
        assert make_service(db_file, backup_dir).last_status() == result


class TestListAndPrune:
    """Локальные копии и retention GFS (§18.3, §20)."""

    def test_list_backups_sorted_newest_first(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        write_backup_file(backup_dir, "backup-20260101-010101.sqlite3.aes", size=1)
        write_backup_file(backup_dir, "backup-20260202-020202.sqlite3.aes", size=22)
        service = make_service(tmp_path / "db", backup_dir)
        infos = service.list_backups()
        assert [info.name for info in infos] == [
            "backup-20260202-020202.sqlite3.aes",
            "backup-20260101-010101.sqlite3.aes",
        ]
        assert infos[0].size_bytes == 22
        assert infos[0].created_at == datetime(2026, 2, 2, 2, 2, 2, tzinfo=timezone.utc)

    def test_list_backups_missing_dir_is_empty(self, tmp_path: Path) -> None:
        service = make_service(tmp_path / "db", tmp_path / "nope")
        assert service.list_backups() == []

    def test_list_backups_skips_foreign_files(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        write_backup_file(backup_dir, "backup-20260101-010101.sqlite3.aes")
        write_backup_file(backup_dir, "notes.aes")  # не наш суффикс
        write_backup_file(backup_dir, "backup-mystery.sqlite3.aes")  # без даты
        service = make_service(tmp_path / "db", backup_dir)
        infos = service.list_backups()
        assert {info.name for info in infos} == {
            "backup-20260101-010101.sqlite3.aes",
            "backup-mystery.sqlite3.aes",
        }
        mystery = next(i for i in infos if i.name == "backup-mystery.sqlite3.aes")
        assert mystery.created_at is None

    def test_prune_keeps_seven_daily_and_weekly(self, tmp_path: Path) -> None:
        # 11 файлов подряд (5–15 января 2026, пн–чт): держим 7 свежайших
        # (9–15), самые свежие за ISO-недели уже входят в семёрку.
        backup_dir = tmp_path / "backups"
        names = []
        for day in range(5, 16):
            name = f"backup-202601{day:02d}-120000.sqlite3.aes"
            write_backup_file(backup_dir, name)
            names.append(name)
        service = make_service(tmp_path / "db", backup_dir)
        removed = service.prune()
        kept = {info.name for info in service.list_backups()}
        assert sorted(removed) == [
            "backup-20260105-120000.sqlite3.aes",
            "backup-20260106-120000.sqlite3.aes",
            "backup-20260107-120000.sqlite3.aes",
            "backup-20260108-120000.sqlite3.aes",
        ]
        assert kept == {f"backup-202601{day:02d}-120000.sqlite3.aes" for day in range(9, 16)}
        assert len(kept) == 7

    def test_prune_weekly_cap_removes_oldest_weeks(self, tmp_path: Path) -> None:
        # 12 еженедельных копий (каждый понедельник): держим 7 свежайших
        # (дневное окно) — 5 старейших удаляются.
        backup_dir = tmp_path / "backups"
        monday = datetime(2026, 1, 5, tzinfo=timezone.utc)  # понедельник ISO-недели 2
        dates = [monday + timedelta(weeks=i) for i in range(12)]
        for moment in dates:
            write_backup_file(
                backup_dir, moment.strftime("backup-%Y%m%d-%H%M%S.sqlite3.aes")
            )
        service = make_service(tmp_path / "db", backup_dir)
        removed = service.prune()
        kept = [info.name for info in service.list_backups()]
        assert len(kept) == 7
        assert len(removed) == 5

    def test_prune_keeps_undated_files(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        write_backup_file(backup_dir, "backup-mystery.sqlite3.aes")
        service = make_service(tmp_path / "db", backup_dir)
        assert service.prune() == []
        assert (backup_dir / "backup-mystery.sqlite3.aes").exists()

    def test_last_status_without_file_is_none(self, tmp_path: Path) -> None:
        service = make_service(tmp_path / "db", tmp_path / "backups")
        assert service.last_status() is None

    def test_last_status_with_corrupt_file_is_none(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / bkp.STATUS_FILE).write_text("{{{", encoding="utf-8")
        service = make_service(tmp_path / "db", backup_dir)
        assert service.last_status() is None


class TestVerifyCompatibility:
    """Восстановление (§20): копия не должна быть новее схемы БД."""

    async def test_backup_not_newer_than_schema(
        self, db_file: Path, tmp_path: Path
    ) -> None:
        service = make_service(db_file, tmp_path / "backups")
        old_copy = tmp_path / "old.sqlite3"
        old_copy.write_bytes(b"")
        sqlite3.connect(old_copy).executescript("PRAGMA user_version = 0;")
        assert await service.verify_compatibility(old_copy) is True

        new_copy = tmp_path / "new.sqlite3"
        new_copy.write_bytes(b"")
        sqlite3.connect(new_copy).executescript("PRAGMA user_version = 99;")
        assert await service.verify_compatibility(new_copy) is False


class TestScheduleAndFire:
    """Ежедневный job (§20) и module-level точка входа."""

    def test_schedule_daily_adds_utc_job(self, tmp_path: Path) -> None:
        scheduler = FakeScheduler()
        service = make_service(tmp_path / "db", tmp_path / "backups")
        service.schedule_daily(scheduler)
        job = scheduler.jobs[bkp.BACKUP_JOB_ID]
        assert job["func"] is fire_backup
        assert isinstance(job["trigger"], CronTrigger)
        assert str(job["trigger"].timezone) == "UTC"
        assert job["replace_existing"] is True

    def test_job_func_pickles_by_reference(self, tmp_path: Path) -> None:
        scheduler = FakeScheduler()
        make_service(tmp_path / "db", tmp_path / "backups").schedule_daily(scheduler)
        func = scheduler.jobs[bkp.BACKUP_JOB_ID]["func"]
        assert pickle.loads(pickle.dumps(func)) is fire_backup

    async def test_fire_backup_delegates_to_bound_service(self, mocker) -> None:
        service = mocker.Mock()
        service.create_backup = mocker.AsyncMock(
            return_value=bkp.BackupResult(
                name="x.aes", created_at=FIXED_NOW, size_bytes=1, integrity_ok=True
            )
        )
        bind_backup(service)
        await fire_backup()
        service.create_backup.assert_awaited_once()

    async def test_fire_backup_unbound_is_ignored(self) -> None:
        bind_backup(None)
        await fire_backup()  # не должно упасть

    async def test_fire_backup_swallows_errors(self, mocker) -> None:
        service = mocker.Mock()
        service.create_backup = mocker.AsyncMock(side_effect=BackupError("нет ключа"))
        bind_backup(service)
        await fire_backup()  # ошибка логируется, планировщик живёт дальше


class TestS3Uploader:
    """Off-site загрузка (§20): ключ объекта и адрес в хранилище."""

    def test_url_and_key_with_prefix(self) -> None:
        uploader = S3Uploader("https://s3", "bucket", "key", "secret", prefix="/backups/")
        assert uploader.bucket == "bucket"
        assert uploader.url_for("a.aes") == "s3://bucket/backups/a.aes"

    def test_url_without_prefix(self) -> None:
        uploader = S3Uploader("https://s3", "bucket", "key", "secret", prefix="")
        assert uploader.url_for("a.aes") == "s3://bucket/a.aes"

    async def test_upload_calls_boto3(self, tmp_path: Path, mocker) -> None:
        client = mocker.Mock()
        mocker.patch("boto3.client", return_value=client)
        uploader = S3Uploader("https://s3", "bucket", "key", "secret")
        path = tmp_path / "a.aes"
        path.write_bytes(b"data")
        url = await uploader.upload(path)
        assert url == "s3://bucket/backups/a.aes"
        client.upload_file.assert_called_once_with(str(path), "bucket", "backups/a.aes")

    def test_remote_url_none_without_uploader(self, tmp_path: Path) -> None:
        service = make_service(tmp_path / "db", tmp_path / "backups")
        assert service.remote_url("x.aes") is None

    def test_remote_url_with_uploader(self, tmp_path: Path) -> None:
        uploader = S3Uploader("https://s3", "bucket", "key", "secret")
        service = make_service(tmp_path / "db", tmp_path / "backups", uploader=uploader)
        assert service.remote_url("x.aes") == "s3://bucket/backups/x.aes"
