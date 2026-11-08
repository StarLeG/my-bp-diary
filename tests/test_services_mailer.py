"""Письма врачу (bot/services/mailer.py, SPEC.md, §21, §4.12).

Покрывается: текст и заголовки письма (plain text, дословно §21), SMTP по
SSL и STARTTLS (подменённый `smtplib`, письма не уходят), пустой
`SMTP_HOST`, ошибка SMTP — лог без адреса и ссылки, лимиты 3 в сутки на
пациента и 1 в час на адрес по `invite_log`, атомарность лимита при
параллельных отправках, каскадное удаление `invite_log` с пациентом.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from bot.config import Settings
from bot.db import create_engine, create_session_factory, init_db
from bot.models import InviteLog, User
from bot.services import mailer
from bot.services.measurements import utcnow

URL = "https://cabinet.example.org/doctor?token=SECRET-TOKEN"
FROM = "Дневник гипертоника <noreply@electroman.by>"


def smtp_settings(**overrides) -> Settings:
    values = {
        "bot_token": "123:TEST",
        "smtp_host": "smtp.example.org",
        "smtp_port": 465,
        "smtp_user": "noreply@electroman.by",
        "smtp_password": "pw",
        "smtp_from": FROM,
    }
    values.update(overrides)
    return Settings(**values)


class FakeSMTP:
    """Подмена smtplib.SMTP / SMTP_SSL: запоминает вызовы, ничего не шлёт."""

    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user, password) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, message) -> None:
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


async def send(session, settings, user_id=1, email="doc@example.org", kind="invite"):
    return await mailer.send_doctor_link(
        session, settings, user_id=user_id, email=email, name="Иван", url=URL, kind=kind
    )


async def log_rows(session) -> list[InviteLog]:
    return list((await session.execute(select(InviteLog).order_by(InviteLog.id))).scalars())


# ─────────────────────────── Письмо ───────────────────────────


class TestMessage:
    def test_text_and_headers(self) -> None:
        message = mailer.build_message(smtp_settings(), "doc@example.org", "Иван", URL)
        assert message["Subject"] == "Пациент открыл вам доступ к дневнику давления"
        assert message["To"] == "doc@example.org"
        assert "noreply@electroman.by" in message["From"]
        assert message["Message-ID"].endswith("@electroman.by>")
        assert message["Date"]
        assert message.get_content_type() == "text/plain"
        assert message.get_content_charset() == "utf-8"
        assert message.get_content() == (
            "Здравствуйте!\n\n"
            "Пациент Иван открыл вам доступ к своему дневнику давления\n"
            "в «Дневнике гипертоника».\n\n"
            "Посмотреть измерения, графики и отчёт:\n"
            f"{URL}\n\n"
            "Ссылка действует 72 часа и открывает доступ один раз.\n"
            "Если вы не знаете этого пациента — просто не открывайте ссылку,\n"
            "доступ без вашего входа не появится.\n\n"
            "—\n"
            "Дневник гипертоника · https://bp-diary.electroman.by\n"
        )
        assert not message.is_multipart()  # только текст, без HTML

    def test_from_without_domain(self) -> None:
        message = mailer.build_message(smtp_settings(smtp_from="noreply"), "d@e.org", "И", URL)
        assert message["Message-ID"]


class TestSmtp:
    def test_ssl(self, fake_smtp) -> None:
        settings = smtp_settings()
        mailer.send_message_sync(settings, mailer.build_message(settings, "d@e.org", "И", URL))
        client = fake_smtp.instances[0]
        assert (client.host, client.port, client.timeout) == ("smtp.example.org", 465, 20)
        assert client.calls == ["login:noreply@electroman.by", "quit"]
        assert len(client.sent) == 1

    def test_starttls_without_login(self, fake_smtp) -> None:
        settings = smtp_settings(smtp_port=587, smtp_security="STARTTLS", smtp_user="")
        mailer.send_message_sync(settings, mailer.build_message(settings, "d@e.org", "И", URL))
        assert fake_smtp.instances[0].calls == ["starttls", "quit"]

    def test_security_validated(self) -> None:
        with pytest.raises(ValueError):
            smtp_settings(smtp_security="tls")
        assert smtp_settings(smtp_security="").smtp_security == "ssl"

    def test_configured(self) -> None:
        assert smtp_settings().smtp_configured
        assert not smtp_settings(smtp_host="").smtp_configured
        assert not smtp_settings(smtp_from=" ").smtp_configured


# ─────────────────────────── Отправка ───────────────────────────


class TestSend:
    async def test_disabled_without_host(self, session, user, fake_smtp) -> None:
        result = await send(session, smtp_settings(smtp_host=""))
        assert result is mailer.SendResult.DISABLED
        assert fake_smtp.instances == []
        assert await log_rows(session) == []

    async def test_sent_and_logged(self, session, user, fake_smtp) -> None:
        result = await send(session, smtp_settings(), email=" Doc@Example.org ", kind="report")
        assert result is mailer.SendResult.SENT
        assert fake_smtp.instances[0].sent[0]["To"] == "doc@example.org"
        [row] = await log_rows(session)
        assert (row.user_id, row.channel, row.target, row.kind, row.ok) == (
            user.id, "email", "doc@example.org", "report", True,
        )

    @pytest.mark.parametrize(
        "error", [smtplib.SMTPRecipientsRefused({}), ConnectionRefusedError(), TimeoutError()]
    )
    async def test_failure_logged_without_address(
        self, session, user, fake_smtp, caplog, error
    ) -> None:
        fake_smtp.fail_with = error
        with caplog.at_level(logging.WARNING, logger="bot.services.mailer"):
            result = await send(session, smtp_settings())
        assert result is mailer.SendResult.FAILED
        assert type(error).__name__ in caplog.text
        assert "doc@example.org" not in caplog.text
        assert "SECRET-TOKEN" not in caplog.text
        [row] = await log_rows(session)
        assert row.ok is False  # попытка учтена в лимите

    async def test_transaction_closed_during_send(self, session, user, monkeypatch) -> None:
        seen = {}

        def fake_send(settings, message) -> None:
            seen["in_transaction"] = session.in_transaction()

        monkeypatch.setattr(mailer, "send_message_sync", fake_send)
        assert await send(session, smtp_settings()) is mailer.SendResult.SENT
        assert seen == {"in_transaction": False}


class TestLimits:
    async def test_one_per_hour_per_address(self, session, user, fake_smtp) -> None:
        settings = smtp_settings()
        assert await send(session, settings) is mailer.SendResult.SENT
        assert await send(session, settings) is mailer.SendResult.LIMITED
        assert await send(session, settings, email="other@example.org") is mailer.SendResult.SENT
        assert len(await log_rows(session)) == 2

    async def test_address_limit_is_global(self, session, user, fake_smtp) -> None:
        """Один адрес — одно письмо в час, даже от разных пациентов."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.commit()
        settings = smtp_settings()
        assert await send(session, settings, user_id=user.id) is mailer.SendResult.SENT
        assert await send(session, settings, user_id=other.id) is mailer.SendResult.LIMITED

    async def test_three_per_day_per_patient(self, session, user, fake_smtp) -> None:
        settings = smtp_settings()
        for index in range(3):
            assert await send(session, settings, email=f"d{index}@e.org") is mailer.SendResult.SENT
        assert await send(session, settings, email="d4@e.org") is mailer.SendResult.LIMITED

    async def test_windows_expire(self, session, user, fake_smtp) -> None:
        old = utcnow() - timedelta(hours=25)
        session.add_all(
            [
                InviteLog(user_id=user.id, channel="email", target=f"d{i}@e.org",
                          kind="invite", ok=True, created_at=old)
                for i in range(3)
            ]
        )
        session.add(
            InviteLog(user_id=user.id, channel="email", target="doc@example.org",
                      kind="invite", ok=True, created_at=utcnow() - timedelta(minutes=61))
        )
        await session.commit()
        assert await send(session, smtp_settings()) is mailer.SendResult.SENT

    async def test_other_channel_not_counted(self, session, user) -> None:
        session.add_all(
            [
                InviteLog(user_id=user.id, channel="telegram", target=f"doc{i}",
                          kind="invite", ok=True)
                for i in range(3)
            ]
        )
        await session.commit()
        assert await mailer.within_limits(session, user.id, "email", "doc@example.org")
        assert not await mailer.within_limits(session, user.id, "telegram", "x")

    async def test_parallel_sends_respect_limit(self, tmp_path, fake_smtp) -> None:
        """Две параллельные отправки на один адрес — уходит одно письмо (§7.2)."""
        engine = create_engine(str(tmp_path / "mail.sqlite3"))
        await init_db(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            session.add(User(telegram_id=111_111))
            await session.commit()

        async def attempt():
            async with factory() as session:
                return await send(session, smtp_settings())

        try:
            results = await asyncio.gather(attempt(), attempt())
        finally:
            await engine.dispose()
        assert sorted(r.value for r in results) == ["limited", "sent"]

    async def test_log_deleted_with_patient(self, session, user) -> None:
        session.add(InviteLog(user_id=user.id, channel="email", target="d@e.org",
                              kind="invite", ok=True))
        await session.commit()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
        assert await session.scalar(select(func.count(InviteLog.id))) == 0
