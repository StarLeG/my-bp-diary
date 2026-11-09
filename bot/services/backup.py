"""Резервное копирование SQLite (SPEC.md, §20; админка — §18.3).

Порядок `create_backup`: `VACUUM INTO` (не `cp` — согласованное состояние) →
`PRAGMA integrity_check` на файле копии → шифрование AES-GCM (`cryptography`,
ключ — из `BACKUP_ENCRYPTION_KEY`) → удаление незашифрованной копии →
off-site в S3-совместимое хранилище (если настроено) → retention GFS
(7 ежедневных + 4 еженедельных). Незашифрованная копия на диске не остаётся.

Запуск: ежедневно в 03:00 UTC (job `backup:daily`) и вручную из админ-панели.
Точка входа job'а — module-level `fire_backup`, а не bound method:
`SQLAlchemyJobStore` сериализует job через `pickle`, а `pickle` тащит весь
экземпляр сервиса (как и в `bot.services.reminders`) — экземпляр привязывается
через `bind_backup`.

Совместимость восстановления: `verify_compatibility` сравнивает
`PRAGMA user_version` копии и рабочей БД (копия не должна быть новее схемы).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import aiosqlite
from apscheduler.triggers.cron import CronTrigger
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

#: Магия шифрованного файла: магия + nonce (12 байт) + AESGCM-шифротекст.
MAGIC = b"BPBACKUP1"
NONCE_SIZE = 12

#: Имя ежедневного job'а и час расписания (§20: 03:00 UTC).
BACKUP_JOB_ID = "backup:daily"
BACKUP_JOB_HOUR = 3

#: Формат имён файлов копий (метка времени — UTC).
BACKUP_NAME_PREFIX = "backup-"
BACKUP_NAME_SUFFIX = ".sqlite3.aes"
BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Retention GFS (§20): 7 ежедневных + 1 еженедельная копия (до 0.5.0 — 7 + 4).
DAILY_KEEP = 7
WEEKLY_KEEP = 1

#: Копии старше этого срока удаляются всегда: удалённый аккаунт не должен
#: жить в копиях дольше 15 дней (Закон № 99-З, ст. 10, 13; §20).
MAX_BACKUP_AGE = timedelta(days=14)

#: Лимит отправки файла в чат (§18.3: больше — ссылка на S3).
MAX_SEND_BYTES = 50 * 1024 * 1024

#: Sidecar-файл статуса последнего бэкапа (переживает рестарт).
STATUS_FILE = "last-status.json"


class BackupError(Exception):
    """Ошибка создания или проверки резервной копии (SPEC.md, §20)."""


def derive_key(secret: str) -> bytes:
    """Ключ AES-256 из `BACKUP_ENCRYPTION_KEY` (SHA-256 секретной строки)."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt_file(plain_path: Path, enc_path: Path, key: bytes) -> None:
    """Зашифровать файл AES-GCM (SPEC.md, §20): магия + nonce + шифротекст."""
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, plain_path.read_bytes(), None)
    enc_path.write_bytes(MAGIC + nonce + ciphertext)


def decrypt_file(enc_path: Path, out_path: Path, key: bytes) -> None:
    """Расшифровать файл из `encrypt_file`; неверный ключ — `BackupError`."""
    raw = enc_path.read_bytes()
    if not raw.startswith(MAGIC):
        raise BackupError("Файл не похож на резервную копию: нет заголовка")
    nonce = raw[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    ciphertext = raw[len(MAGIC) + NONCE_SIZE :]
    try:
        payload = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise BackupError("Не удалось расшифровать: неверный ключ") from exc
    out_path.write_bytes(payload)


def timestamp_from_name(name: str) -> Optional[datetime]:
    """Момент UTC из имени `backup-YYYYMMDD-HHMMSS.sqlite3.aes` (или None)."""
    body = name
    if body.startswith(BACKUP_NAME_PREFIX):
        body = body[len(BACKUP_NAME_PREFIX) :]
    if body.endswith(BACKUP_NAME_SUFFIX):
        body = body[: -len(BACKUP_NAME_SUFFIX)]
    try:
        return datetime.strptime(body, BACKUP_STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class BackupResult:
    """Итог создания копии (§18.3: статус последнего бэкапа)."""

    name: str
    """Имя файла зашифрованной копии."""

    created_at: datetime
    """Момент создания (UTC)."""

    size_bytes: int
    """Размер зашифрованного файла."""

    integrity_ok: bool
    """`PRAGMA integrity_check` копии прошёл (`integrity_check: OK`)."""

    uploaded_url: Optional[str] = None
    """Адрес off-site копии в S3-хранилище (§18.3: статус off-site)."""

    upload_error: Optional[str] = None
    """Ошибка off-site загрузки (локальная копия при этом сохранена)."""


@dataclass(frozen=True)
class BackupFileInfo:
    """Строка списка локальных копий (§18.3): имя, размер, момент создания."""

    name: str
    size_bytes: int
    created_at: Optional[datetime]


class S3Uploader:
    """Off-site загрузка копии в S3-совместимое хранилище (SPEC.md, §20).

    Клиент `boto3` создаётся лениво; блокирующий вызов идёт в thread pool
    (SPEC.md, §7.2, п. 7).
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "backups",
    ) -> None:
        self._endpoint_url = endpoint_url
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._prefix = prefix.strip("/")

    @property
    def bucket(self) -> str:
        """Имя бакета."""
        return self._bucket

    def _key(self, name: str) -> str:
        return f"{self._prefix}/{name}" if self._prefix else name

    def url_for(self, name: str) -> str:
        """Адрес копии в хранилище (§18.3: ссылка на S3)."""
        return f"s3://{self._bucket}/{self._key(name)}"

    async def upload(self, file_path: Path) -> str:
        """Загрузить файл в бакет; возвращает его адрес в хранилище."""
        key = self._key(file_path.name)

        def _put() -> None:
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
            )
            client.upload_file(str(file_path), self._bucket, key)

        await asyncio.to_thread(_put)
        return self.url_for(file_path.name)


class BackupService:
    """Создание, проверка и шифрование бэкапов (SPEC.md, §20).

    `uploader` — off-site хранилище (обычно `S3Uploader`); `None` = только
    локальные копии. `now` — источник текущего момента (в тестах — фиксированный).
    """

    def __init__(
        self,
        db_path: str,
        backup_dir: str | Path,
        encryption_key: str,
        *,
        uploader: Optional[S3Uploader] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._db_path = db_path
        self._backup_dir = Path(backup_dir)
        self._key = derive_key(encryption_key) if encryption_key else b""
        self._uploader = uploader
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def backup_dir(self) -> Path:
        """Каталог локальных копий."""
        return self._backup_dir

    def remote_url(self, name: str) -> Optional[str]:
        """Адрес копии в off-site хранилище (§18.3), если оно настроено."""
        return self._uploader.url_for(name) if self._uploader is not None else None

    # ─────────────────────────── Создание и проверка ───────────────────────────

    async def create_backup(self) -> BackupResult:
        """Создать зашифрованную копию БД (§20). Ошибки — `BackupError`."""
        if not self._key:
            raise BackupError("BACKUP_ENCRYPTION_KEY не задан")
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime(BACKUP_STAMP_FORMAT)
        plain_path = self._backup_dir / f"{BACKUP_NAME_PREFIX}{stamp}.sqlite3"
        enc_path = self._backup_dir / f"{BACKUP_NAME_PREFIX}{stamp}{BACKUP_NAME_SUFFIX}"
        try:
            await self._vacuum_into(plain_path)
            if not await self._integrity_check(plain_path):
                raise BackupError(f"Копия {plain_path.name} не прошла integrity_check")
            encrypt_file(plain_path, enc_path, self._key)
        except Exception:
            enc_path.unlink(missing_ok=True)
            raise
        finally:
            # Незашифрованная копия не остаётся на диске ни при каком исходе.
            plain_path.unlink(missing_ok=True)

        size_bytes = enc_path.stat().st_size
        uploaded_url: Optional[str] = None
        upload_error: Optional[str] = None
        if self._uploader is not None:
            try:
                uploaded_url = await self._uploader.upload(enc_path)
            except Exception as exc:  # off-site не должен ломать локальную копию
                upload_error = str(exc)
                logger.warning("Off-site загрузка копии %s не удалась: %s", enc_path.name, exc)
        result = BackupResult(
            name=enc_path.name,
            created_at=self._now(),
            size_bytes=size_bytes,
            integrity_ok=True,
            uploaded_url=uploaded_url,
            upload_error=upload_error,
        )
        self._write_status(result)
        removed = self.prune()
        if removed:
            logger.info("Retention GFS: удалено копий — %s", len(removed))
        logger.info("Бэкап создан: %s (%s байт)", result.name, result.size_bytes)
        return result

    async def verify_compatibility(self, backup_path: Path) -> bool:
        """Копия пригодна к восстановлению: её схема не новее текущей (§20).

        Сравнивается `PRAGMA user_version` файла копии и рабочей БД.
        """
        backup_version = await self._user_version(backup_path)
        current_version = await self._user_version(Path(self._db_path))
        return backup_version <= current_version

    async def _vacuum_into(self, target: Path) -> None:
        """Снять копию через `VACUUM INTO` (§20: не `cp`, без открытых транзакций)."""
        path_sql = str(target).replace("'", "''")
        async with aiosqlite.connect(self._db_path) as conn:
            await conn.execute(f"VACUUM INTO '{path_sql}'")

    async def _integrity_check(self, path: Path) -> bool:
        """`PRAGMA integrity_check` на файле копии (§20) — «ok» = прошла."""
        async with aiosqlite.connect(path) as conn:
            async with conn.execute("PRAGMA integrity_check") as cursor:
                row = await cursor.fetchone()
        return row is not None and row[0] == "ok"

    async def _user_version(self, path: Path) -> int:
        async with aiosqlite.connect(path) as conn:
            async with conn.execute("PRAGMA user_version") as cursor:
                row = await cursor.fetchone()
        return int(row[0])

    # ─────────────────────────── Локальные копии ───────────────────────────

    def list_backups(self) -> list[BackupFileInfo]:
        """Локальные копии, новые первыми (§18.3: вывод последних 7 файлов)."""
        if not self._backup_dir.is_dir():
            return []
        infos = [
            BackupFileInfo(
                name=path.name,
                size_bytes=path.stat().st_size,
                created_at=timestamp_from_name(path.name),
            )
            for path in self._backup_dir.iterdir()
            if path.is_file() and path.name.endswith(BACKUP_NAME_SUFFIX)
        ]
        infos.sort(
            key=lambda info: (
                info.created_at or datetime.min.replace(tzinfo=timezone.utc),
                info.name,
            ),
            reverse=True,
        )
        return infos

    def last_status(self) -> Optional[BackupResult]:
        """Статус последнего бэкапа (§18.3) — из sidecar-файла, после рестарта тоже."""
        status_path = self._backup_dir / STATUS_FILE
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            return BackupResult(
                name=str(data["name"]),
                created_at=datetime.fromisoformat(data["created_at"]),
                size_bytes=int(data["size_bytes"]),
                integrity_ok=bool(data["integrity_ok"]),
                uploaded_url=data.get("uploaded_url"),
                upload_error=data.get("upload_error"),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_status(self, result: BackupResult) -> None:
        payload = {
            "name": result.name,
            "created_at": result.created_at.isoformat(),
            "size_bytes": result.size_bytes,
            "integrity_ok": result.integrity_ok,
            "uploaded_url": result.uploaded_url,
            "upload_error": result.upload_error,
        }
        (self._backup_dir / STATUS_FILE).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def prune(self, now: Optional[datetime] = None) -> list[str]:
        """Retention GFS (§20): 7 ежедневных + 1 еженедельная, не старше 14 дней.

        Еженедельная копия — самая свежая за ISO-неделю. Любая копия старше
        `MAX_BACKUP_AGE` удаляется, даже если подходит под GFS. Файлы с
        нераспознанными именами не трогаются (не наши).
        """
        now = now or datetime.now(timezone.utc)
        dated = [info for info in self.list_backups() if info.created_at is not None]
        keep: set[str] = {info.name for info in dated[:DAILY_KEEP]}
        weeks: set[tuple[int, int]] = set()
        for info in dated:
            iso = info.created_at.isocalendar()  # type: ignore[union-attr]
            week_key = (iso[0], iso[1])
            if week_key in weeks:
                continue
            if len(weeks) >= WEEKLY_KEEP:
                break
            weeks.add(week_key)
            keep.add(info.name)
        keep = {
            info.name
            for info in dated
            if info.name in keep and now - info.created_at <= MAX_BACKUP_AGE  # type: ignore[operator]
        }
        removed: list[str] = []
        for info in dated:
            if info.name not in keep:
                (self._backup_dir / info.name).unlink(missing_ok=True)
                removed.append(info.name)
        return removed

    # ─────────────────────────── Расписание ───────────────────────────

    def schedule_daily(self, scheduler) -> None:
        """Ежедневный job в 03:00 UTC (§20). Идемпотентно (`replace_existing`).

        Точка входа — module-level `fire_backup`: bound method не
        сериализуется в `SQLAlchemyJobStore` (см. докстринг модуля).
        """
        scheduler.add_job(
            fire_backup,
            trigger=CronTrigger(hour=BACKUP_JOB_HOUR, minute=0, timezone="UTC"),
            id=BACKUP_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
            timezone="UTC",
        )


# ─────────────────── Точка входа job'а (module-level) ───────────────────

_service: Optional[BackupService] = None


def bind_backup(service: Optional[BackupService]) -> None:
    """Привязать экземпляр сервиса к job-точке входа `fire_backup`.

    Вызывается из `bot.main.build_app`; в тестах привязка восстанавливается.
    """
    global _service
    _service = service


async def fire_backup() -> None:
    """Точка входа ежедневного job'а бэкапа (§20): сериализуем в jobstore.

    Ошибки логируются и не роняют планировщик: следующий запуск — через сутки,
    вручную копию можно создать из админ-панели (§18.3).
    """
    if _service is None:
        logger.error("Сработал job бэкапа без привязанного сервиса (bind_backup)")
        return
    try:
        result = await _service.create_backup()
    except Exception:
        logger.exception("Ежедневный бэкап не удался")
        return
    logger.info("Ежедневный бэкап создан: %s", result.name)
