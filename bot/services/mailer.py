"""Письма врачу со ссылкой доступа (SPEC.md, §21, §4.12).

SMTP своего домена (`SMTP_*`, §3), только текст (plain text, UTF-8);
`smtplib` вызывается в `asyncio.to_thread` (§7.2 п.7), повторов нет. Пустой
`SMTP_HOST` — письма не отправляются, бот работает как без почты.

Лимиты (§21): не больше 3 писем на пациента в сутки и 1 письма на один
адрес в час — по таблице `invite_log` (общей с приглашениями по @username,
`channel`). Проверка лимита и запись попытки — атомарно под asyncio-локом:
две параллельные отправки не проскочат лимит вдвоём (§7.2). Строка пишется
до отправки (`ok=False`) и после успеха помечается `ok=True`; транзакция
БД на время сетевой отправки закрыта.

В лог при ошибке — только тип исключения: адрес врача и ссылка (токен)
туда не попадают.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.models import InviteLog
from bot.services.measurements import utcnow

logger = logging.getLogger(__name__)

#: Канал писем в `invite_log`.
CHANNEL_EMAIL = "email"

#: Писем на пациента за сутки (§21).
DAILY_LIMIT = 3
#: Окно суточного лимита.
DAILY_WINDOW = timedelta(hours=24)
#: Писем на один адрес за окно (§21: одно в час).
TARGET_LIMIT = 1
#: Окно лимита на адрес.
TARGET_WINDOW = timedelta(hours=1)

#: Таймаут SMTP-соединения, секунд: бот не должен висеть на почте.
SMTP_TIMEOUT = 20

#: Срок ссылки в тексте письма (ссылка доступа — 72 часа, §21).
LINK_TTL_TEXT = "72 часа"

#: Тема письма (§21, дословно).
SUBJECT = "Пациент открыл вам доступ к дневнику давления"

#: Текст письма (§21, дословно): `{name}` — имя пациента из Telegram.
BODY = """Здравствуйте!

Пациент {name} открыл вам доступ к своему дневнику давления
в «Дневнике гипертоника».

Посмотреть измерения, графики и отчёт:
{url}

Ссылка действует {ttl} и открывает доступ один раз.
Если вы не знаете этого пациента — просто не открывайте ссылку,
доступ без вашего входа не появится.

—
Дневник гипертоника · https://bp-diary.electroman.by
"""


class SendResult(enum.Enum):
    """Итог отправки — по нему бот выбирает текст пациенту (§21)."""

    SENT = "sent"
    FAILED = "failed"
    LIMITED = "limited"
    DISABLED = "disabled"


_limit_lock: Optional[asyncio.Lock] = None
_limit_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _current_lock() -> asyncio.Lock:
    """Лок лимитов текущего event loop (как лок приёмов в `medications`)."""
    global _limit_lock, _limit_lock_loop
    loop = asyncio.get_running_loop()
    if _limit_lock is None or _limit_lock_loop is not loop:
        _limit_lock = asyncio.Lock()
        _limit_lock_loop = loop
    return _limit_lock


def build_message(settings: Settings, to: str, name: str, url: str) -> EmailMessage:
    """Письмо врачу: plain text, UTF-8, Message-ID на домене отправителя."""
    message = EmailMessage()
    message["Subject"] = SUBJECT
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Date"] = formatdate(localtime=False)
    domain = parseaddr(settings.smtp_from)[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(BODY.format(name=name, url=url, ttl=LINK_TTL_TEXT), charset="utf-8")
    return message


def send_message_sync(settings: Settings, message: EmailMessage) -> None:
    """Отправить письмо (блокирующе — вызывать через `asyncio.to_thread`)."""
    if settings.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT
        )
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)


async def within_limits(
    session: AsyncSession,
    user_id: int,
    channel: str,
    target: str,
    now: Optional[datetime] = None,
) -> bool:
    """Не превышены ли лимиты: 3 в сутки на пациента, 1 в час на `target` (§21)."""
    now = now or utcnow()
    per_user = await session.scalar(
        select(func.count(InviteLog.id)).where(
            InviteLog.user_id == user_id,
            InviteLog.channel == channel,
            InviteLog.created_at > now - DAILY_WINDOW,
        )
    )
    if (per_user or 0) >= DAILY_LIMIT:
        return False
    per_target = await session.scalar(
        select(func.count(InviteLog.id)).where(
            InviteLog.channel == channel,
            InviteLog.target == target,
            InviteLog.created_at > now - TARGET_WINDOW,
        )
    )
    return (per_target or 0) < TARGET_LIMIT


async def reserve(
    session: AsyncSession, user_id: int, channel: str, target: str, kind: str
) -> Optional[int]:
    """Проверить лимит и записать попытку атомарно; id строки или None (лимит).

    Под локом: проверка и вставка не перемежаются с параллельной отправкой.
    Транзакция коммитится сразу — на время отправки она закрыта.
    """
    async with _current_lock():
        if not await within_limits(session, user_id, channel, target):
            await session.rollback()
            return None
        row = InviteLog(user_id=user_id, channel=channel, target=target, kind=kind, ok=False)
        session.add(row)
        await session.flush()
        log_id = row.id  # до commit: после него атрибуты истекают (expire_on_commit)
        await session.commit()
        return log_id


async def mark_sent(session: AsyncSession, log_id: int) -> None:
    """Пометить попытку доставленной (`ok=True`)."""
    await session.execute(update(InviteLog).where(InviteLog.id == log_id).values(ok=True))
    await session.commit()


async def send_doctor_link(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    email: str,
    name: str,
    url: str,
    kind: str,
) -> SendResult:
    """Отправить врачу письмо со ссылкой доступа (приглашение или отчёт, §21)."""
    if not settings.smtp_configured:
        return SendResult.DISABLED
    target = email.strip().lower()
    log_id = await reserve(session, user_id, CHANNEL_EMAIL, target, kind)
    if log_id is None:
        return SendResult.LIMITED
    message = build_message(settings, target, name, url)
    try:
        await asyncio.to_thread(send_message_sync, settings, message)
    except (smtplib.SMTPException, OSError) as exc:
        # Адрес и ссылку не логируем: только тип ошибки (§12, §21).
        logger.warning("Письмо врачу не отправлено: %s", type(exc).__name__)
        return SendResult.FAILED
    await mark_sent(session, log_id)
    return SendResult.SENT
