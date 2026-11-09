"""Тесты хендлеров «Мой врач» и «Отправить отчет» (SPEC.md, §21).

Покрывается: экран «Мой врач» (нет врача → FSM e-mail, есть → список с
отзывом), валидация e-mail, выдача magic link (payload did/pid/jti),
отмена, отзыв доступа с проверкой владельца и «Отправить отчет» с
дословным текстом §21, п.5.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import doctor
from bot.models import Doctor as DbDoctor, DoctorPatient, User as User_
from bot.services import doctor as doctor_service

TG_ID = 111_111
SECRET = "test-secret"
CABINET_URL = "https://cabinet.example.org"


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, message: Message | None = None, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message or make_message(),
    )


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def make_bot(username: str = "my_bp_diary_bot"):
    """Подмена Bot: `me()` с username и `send_message` (AsyncMock)."""
    from unittest.mock import AsyncMock, MagicMock

    bot = MagicMock()
    bot.me = AsyncMock(return_value=MagicMock(username=username))
    bot.send_message = AsyncMock()
    return bot


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры.

    `bot` по умолчанию — подмена (`make_bot`), если хендлер его просит.
    """
    wanted = inspect.signature(handler).parameters
    if "bot" in wanted and "bot" not in kwargs:
        kwargs["bot"] = make_bot()
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


def extract_url(text: str) -> str:
    """Ссылка из сообщения бота (единственный URL в тексте)."""
    match = re.search(r"https://\S+", text)
    assert match is not None, f"в тексте нет ссылки: {text!r}"
    return match.group(0)


def parse_token(url: str) -> dict:
    """Payload токена из ссылки врача (did/pid/jti)."""
    token = url.split("token=", 1)[1]
    return URLSafeTimedSerializer(SECRET, salt=doctor.DOCTOR_SALT).loads(token)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def state() -> FSMContext:
    return make_state()


@pytest.fixture
def with_settings(mocker):
    """Подменить get_settings в doctor: по умолчанию кабинет не развёрнут."""

    def apply(**env) -> Settings:
        settings = Settings(bot_token="123:TEST", **env)
        mocker.patch.object(doctor, "get_settings", return_value=settings)
        return settings

    return apply


def cabinet_on(**env) -> dict:
    """Настройки с развёрнутым кабинетом (кнопка «Мой врач» видна)."""
    return {"web_secret_key": SECRET, "cabinet_url": CABINET_URL, **env}


async def make_link(session: AsyncSession, status: str = "pending") -> DbDoctor:
    """Врач и связь с пользователем 111111 (users.id = 1) в нужном статусе."""
    db_doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
    await doctor_service.link_patient(session, db_doctor.id, 1)
    if status != "pending":
        link = await doctor_service.link_for_pair(session, db_doctor.id, 1)
        assert link is not None
        link.status = status
        await session.commit()
    return db_doctor


# ─────────────────────────── Экран «Мой врач» ───────────────────────────


class TestDoctorEntry:
    async def test_web_disabled(self, answer, state, with_settings) -> None:
        with_settings()
        await call(
            doctor.cmd_doctor, message=make_message(), state=state, session=None
        )
        assert answer.call_args.args[0] == texts.DOCTOR_WEB_DISABLED
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_no_link_shows_choice(
        self, answer, state, with_settings, session, user
    ) -> None:
        """Врача нет — выбор способа «По e-mail» / «Через Telegram» (§21, п.2)."""
        with_settings(**cabinet_on())
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_NO_LINK == (
            "Врач пока не добавлен.\nКак пригласить врача?"
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_choice_kb()
        assert await state.get_state() is None

    async def test_open_link_shows_screen(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        text = answer.call_args.args[0]
        assert text.startswith(texts.DOCTOR_MENU)
        assert texts.DOCTOR_LINK_ITEM.format(
            label="doc@example.org",
            status=texts.DOCTOR_STATUS_LABELS["pending"],
        ) in text
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_kb(
            [(db_doctor.id, "doc@example.org", "pending")]
        )
        assert await state.get_state() is None

    async def test_revoked_only_shows_choice_again(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await make_link(session, status="revoked")
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_NO_LINK
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_choice_kb()

    async def test_menu_callback_edits_message(
        self, edit, cb_answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await call(
            doctor.cb_doctor,
            callback=make_callback("set:doctor"),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_NO_LINK
        cb_answer.assert_awaited()


# ─────────────────────────── Добавление врача ───────────────────────────


class TestDoctorEmail:
    async def _in_state(self) -> FSMContext:
        state = make_state()
        await state.set_state(doctor.DoctorAccess.email)
        return state

    async def test_bad_format_keeps_state(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        state = await self._in_state()
        await call(
            doctor.msg_doctor_email,
            message=make_message("не email"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.DOCTOR_EMAIL_BAD
        assert await state.get_state() == doctor.DoctorAccess.email.state

    async def test_empty_text_rejected(self, answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        state = await self._in_state()
        await call(
            doctor.msg_doctor_email,
            message=make_message("   "),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.DOCTOR_EMAIL_BAD
        assert await state.get_state() == doctor.DoctorAccess.email.state

    async def test_success_creates_doctor_and_link(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        state = await self._in_state()
        await call(
            doctor.msg_doctor_email,
            message=make_message("Doc@Example.ORG"),
            state=state,
            session=session,
        )
        text = answer.call_args.args[0]
        url = extract_url(text)
        assert text == texts.DOCTOR_INVITE_CREATED.format(name="Тест", url=url)
        assert url.startswith(f"{CABINET_URL}/doctor?token=")
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        assert await state.get_state() is None

        payload = parse_token(url)
        assert set(payload) == {"did", "pid", "jti"}
        row = (
            await session.execute(select(DbDoctor).where(DbDoctor.email == "doc@example.org"))
        ).scalar_one()
        assert row.id == payload["did"]
        assert row.password_hash.startswith("$argon2id$")
        link = await doctor_service.link_for_pair(session, row.id, payload["pid"])
        assert link is not None
        assert link.status == "pending"

    async def test_existing_doctor_is_reused(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await doctor_service.find_or_create_doctor(session, "doc@example.org")
        state = await self._in_state()
        await call(
            doctor.msg_doctor_email,
            message=make_message("doc@example.org"),
            state=state,
            session=session,
        )
        rows = (
            await session.execute(select(DbDoctor).where(DbDoctor.email == "doc@example.org"))
        ).all()
        assert len(rows) == 1

    async def test_cancel_returns_to_menu(
        self, edit, cb_answer, state, with_settings
    ) -> None:
        with_settings(**cabinet_on())
        await state.set_state(doctor.DoctorAccess.email)
        await call(
            doctor.cb_doctor_cancel,
            callback=make_callback("set:doctor:cancel"),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        cb_answer.assert_awaited_with(texts.DOCTOR_ADD_CANCELLED)
        assert await state.get_state() is None


# ─────────────────────────── Отзыв доступа ───────────────────────────


class TestRevokeAccess:
    async def test_revoke_marks_link_revoked(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)
        await call(
            doctor.cb_doctor_revoke,
            callback=make_callback(f"set:doctor:revoke:{db_doctor.id}"),
            state=make_state(),
            session=session,
        )
        cb_answer.assert_awaited_with(texts.DOCTOR_REVOKED)
        link = await doctor_service.link_for_pair(session, db_doctor.id, 1)
        assert link is not None
        assert link.status == "revoked"
        # Экран перерисован: после отзыва всех врачей снова предлагается e-mail.
        assert edit.call_args.args[0] == texts.DOCTOR_NO_LINK

    async def test_revoked_revoke_is_idempotent(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        """Повторный отзыв уже отзёванной связи: без ошибок, статус прежний."""
        with_settings(**cabinet_on())
        db_doctor = await make_link(session, status="revoked")
        await call(
            doctor.cb_doctor_revoke,
            callback=make_callback(f"set:doctor:revoke:{db_doctor.id}"),
            state=make_state(),
            session=session,
        )
        cb_answer.assert_awaited_with(texts.DOCTOR_REVOKED)
        link = await doctor_service.link_for_pair(session, db_doctor.id, 1)
        assert link is not None
        assert link.status == "revoked"

    async def test_foreign_doctor_id_is_rejected(
        self, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await call(
            doctor.cb_doctor_revoke,
            callback=make_callback("set:doctor:revoke:999"),
            state=make_state(),
            session=session,
        )
        cb_answer.assert_awaited_with(texts.NOT_YOUR_RECORD)

    async def test_broken_value_is_rejected(self, cb_answer, with_settings) -> None:
        with_settings(**cabinet_on())
        await call(
            doctor.cb_doctor_revoke,
            callback=make_callback("set:doctor:revoke:abc"),
            state=make_state(),
            session=None,
        )
        cb_answer.assert_awaited_with(texts.NOT_YOUR_RECORD)

    async def test_other_user_cannot_revoke(
        self, cb_answer, with_settings, session, user
    ) -> None:
        """Чужой doctor_id существует, но связь принадлежит другому пользователю."""
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)  # связь с пользователем 1 (fixture user)
        await call(
            doctor.cb_doctor_revoke,
            callback=make_callback(f"set:doctor:revoke:{db_doctor.id}", user_id=222_222),
            state=make_state(),
            session=session,
        )
        cb_answer.assert_awaited_with(texts.NOT_YOUR_RECORD)
        link = await doctor_service.link_for_pair(session, db_doctor.id, 1)
        assert link is not None
        assert link.status == "pending"


# ─────────────────────────── Отправить отчет ───────────────────────────


class TestSendReport:
    async def test_web_disabled(self, edit, cb_answer, with_settings) -> None:
        with_settings()
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=None,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_WEB_DISABLED
        cb_answer.assert_awaited()

    async def test_no_doctor(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_REPORT_NO_LINK

    async def test_only_revoked(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await make_link(session, status="revoked")
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_REPORT_REVOKED

    async def test_pending_link_gives_report_url(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        text = edit.call_args.args[0]
        url = extract_url(text)
        assert text == texts.DOCTOR_REPORT_LINK.format(url=url)
        assert url.startswith(f"{CABINET_URL}/doctor?token=")
        payload = parse_token(url)
        assert payload["did"] == db_doctor.id
        assert payload["pid"] == 1

    async def test_active_link_gives_report_url(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await make_link(session, status="active")
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        assert texts.DOCTOR_REPORT_LINK.format(url=extract_url(edit.call_args.args[0])) == (
            edit.call_args.args[0]
        )


class TestNoLinkPreview:
    """Регресс: превью Telegram сжигает одноразовый токен — выключаем его.

    Превью открывает ссылку до клика, `consume` списывает jti — врач попадает
    на `link_expired`. Все отправки со ссылками дают
    `keyboards.NO_LINK_PREVIEW` (как в support.py).
    """

    async def test_invite_disables_preview(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        state = make_state()
        await state.set_state(doctor.DoctorAccess.email)
        await call(
            doctor.msg_doctor_email,
            message=make_message("doc@example.org"),
            state=state,
            session=session,
        )
        assert answer.call_args.kwargs["link_preview_options"] is keyboards.NO_LINK_PREVIEW

    async def test_report_link_disables_preview(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await make_link(session)
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        assert edit.call_args.kwargs["link_preview_options"] is keyboards.NO_LINK_PREVIEW


# ─────────────────────────── Подпись ссылки ───────────────────────────


class TestMakeDoctorLink:
    def test_format(self) -> None:
        settings = Settings(bot_token="123:TEST", **cabinet_on())
        url = doctor.make_doctor_link(settings, 7, 42)
        assert url.startswith(f"{CABINET_URL}/doctor?token=")

    def test_payload_roundtrip(self) -> None:
        settings = Settings(bot_token="123:TEST", **cabinet_on())
        url = doctor.make_doctor_link(settings, 7, 42)
        payload = parse_token(extract_url(url))
        assert payload["did"] == 7
        assert payload["pid"] == 42
        assert isinstance(payload["jti"], str) and payload["jti"]

    def test_foreign_secret_is_rejected(self) -> None:
        settings = Settings(bot_token="123:TEST", **cabinet_on())
        url = doctor.make_doctor_link(settings, 7, 42)
        token = url.split("token=", 1)[1]
        with pytest.raises(BadSignature):
            URLSafeTimedSerializer("other-secret", salt=doctor.DOCTOR_SALT).loads(token)

    def test_trailing_slash_in_cabinet_url(self) -> None:
        settings = Settings(
            bot_token="123:TEST",
            web_secret_key=SECRET,
            cabinet_url=f"{CABINET_URL}/",
        )
        url = doctor.make_doctor_link(settings, 7, 42)
        assert url.startswith(f"{CABINET_URL}/doctor?token=")


# ─────────────────── Письма врачу (SMTP, §21, релиз 0.4.11) ───────────────────


def smtp_on(**env) -> dict:
    """Кабинет и почта настроены."""
    return cabinet_on(
        smtp_host="smtp.example.org",
        smtp_from="Дневник гипертоника <noreply@electroman.by>",
        **env,
    )


@pytest.fixture
def sent(monkeypatch):
    """Подменить отправку: письма не уходят, а копятся в списке."""
    from bot.services import mailer

    class Outbox(list):
        """Отправленные письма + доступ к подменённой отправке (`fake`)."""

    outbox = Outbox()

    def fake_send(settings, message) -> None:
        if getattr(fake_send, "error", None) is not None:
            raise fake_send.error
        outbox.append(message)

    monkeypatch.setattr(mailer, "send_message_sync", fake_send)
    outbox.fake = fake_send  # type: ignore[attr-defined]
    return outbox


async def _email_state() -> FSMContext:
    state = make_state()
    await state.set_state(doctor.DoctorAccess.email)
    return state


class TestDoctorEmailSending:
    async def test_prompt_mentions_invitation(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**smtp_on())
        state = make_state()
        await call(doctor.cb_doctor_by_email, callback=make_callback("set:doctor:email"), state=state, session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_EMAIL_ASK == (
            "Отправьте e-mail врача сообщением — отправлю ему приглашение."
        )
        assert await state.get_state() == doctor.DoctorAccess.email.state

    async def test_prompt_without_smtp(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        state = make_state()
        await call(doctor.cb_doctor_by_email, callback=make_callback("set:doctor:email"), state=state, session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_EMAIL_ASK_NO_SMTP == (
            "Отправьте e-mail врача сообщением — подготовлю ссылку для него."
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb("set:doctor:cancel")

    async def test_invite_sent_same_link_as_forward(
        self, answer, with_settings, session, user, sent
    ) -> None:
        with_settings(**smtp_on())
        await call(
            doctor.msg_doctor_email,
            message=make_message("Doc@Example.ORG"),
            state=await _email_state(),
            session=session,
        )
        text = answer.call_args.args[0]
        url = extract_url(text)
        assert text == texts.DOCTOR_INVITE_SENT.format(email="doc@example.org", name="Тест", url=url)
        assert text.startswith("Приглашение отправлено на doc@example.org.\nЕсли письмо не дойдёт")
        assert answer.call_args.kwargs["link_preview_options"] is keyboards.NO_LINK_PREVIEW
        [message] = sent
        assert message["To"] == "doc@example.org"
        assert url in message.get_content()  # письмо и пересылка — один токен
        assert "Пациент Тест открыл вам доступ" in message.get_content()

    async def test_invite_failed(self, answer, with_settings, session, user, sent) -> None:
        import smtplib

        with_settings(**smtp_on())
        sent.fake.error = smtplib.SMTPServerDisconnected()
        await call(
            doctor.msg_doctor_email,
            message=make_message("doc@example.org"),
            state=await _email_state(),
            session=session,
        )
        text = answer.call_args.args[0]
        assert text == texts.DOCTOR_INVITE_FAILED.format(name="Тест", url=extract_url(text))

    async def test_invite_limited(self, answer, with_settings, session, user, sent) -> None:
        """Повторное добавление того же врача в течение часа — письмо не уходит."""
        with_settings(**smtp_on())
        for _ in range(2):
            await call(
                doctor.msg_doctor_email,
                message=make_message("doc@example.org"),
                state=await _email_state(),
                session=session,
            )
        text = answer.call_args.args[0]
        assert text == texts.DOCTOR_INVITE_LIMIT.format(name="Тест", url=extract_url(text))
        assert len(sent) == 1

    async def test_readd_after_revoke_sends_again(
        self, answer, with_settings, session, user, sent
    ) -> None:
        """E5-3: повторное добавление после отзыва — новое письмо (вне часового окна)."""
        from bot.models import InviteLog
        from bot.services.measurements import utcnow
        from datetime import timedelta

        with_settings(**smtp_on())
        await make_link(session, status="revoked")
        session.add(InviteLog(user_id=1, channel="email", target="doc@example.org",
                              kind="invite", ok=True, created_at=utcnow() - timedelta(hours=2)))
        await session.commit()
        await call(
            doctor.msg_doctor_email,
            message=make_message("doc@example.org"),
            state=await _email_state(),
            session=session,
        )
        assert answer.call_args.args[0].startswith("Приглашение отправлено на doc@example.org.")
        link = await doctor_service.link_for_pair(session, 1, 1)
        assert link is not None and link.status == "pending"
        assert len(sent) == 1

    async def test_report_emailed(self, edit, cb_answer, with_settings, session, user, sent) -> None:
        with_settings(**smtp_on())
        await make_link(session, status="active")
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        text = edit.call_args.args[0]
        url = extract_url(text)
        assert text == texts.DOCTOR_REPORT_EMAILED.format(email="doc@example.org", url=url)
        assert url in sent[0].get_content()

    async def test_report_email_failed_or_limited(
        self, edit, cb_answer, with_settings, session, user, sent
    ) -> None:
        with_settings(**smtp_on())
        await make_link(session, status="active")
        for _ in range(2):  # второе — сверх лимита «1 в час на адрес»
            await call(
                doctor.cb_send_report,
                callback=make_callback("menu:report"),
                state=make_state(),
                session=session,
            )
        text = edit.call_args.args[0]
        assert text == texts.DOCTOR_REPORT_EMAIL_FAILED.format(url=extract_url(text))
        assert len(sent) == 1

    async def test_report_without_smtp_unchanged(
        self, edit, cb_answer, with_settings, session, user, sent
    ) -> None:
        with_settings(**cabinet_on())
        await make_link(session, status="active")
        await call(
            doctor.cb_send_report,
            callback=make_callback("menu:report"),
            state=make_state(),
            session=session,
        )
        text = edit.call_args.args[0]
        assert text == texts.DOCTOR_REPORT_LINK.format(url=extract_url(text))
        assert sent == []


def test_email_texts_verbatim() -> None:
    """Тексты §21 дословно."""
    assert texts.DOCTOR_INVITE_FAILED.format(name="И", url="U") == (
        "Не удалось отправить письмо. Перешлите врачу сообщение ниже:\n\n"
        "Пациент И предоставил вам доступ.\nU"
    )
    assert texts.DOCTOR_INVITE_LIMIT.format(name="И", url="U") == (
        "Письмо уже отправлено недавно. Перешлите врачу сообщение ниже:\n\n"
        "Пациент И предоставил вам доступ.\nU"
    )
    assert texts.DOCTOR_REPORT_EMAILED.format(email="e", url="U") == (
        "Ссылка отправлена врачу на e. Можно также переслать её: U"
    )
    assert texts.DOCTOR_REPORT_EMAIL_FAILED.format(url="U") == (
        "Не удалось отправить письмо. Перешлите ссылку врачу сами: U"
    )


async def test_report_text_doctor_without_email() -> None:
    """Врач без e-mail (появится с Telegram-приглашением, §4.8) — ссылка для пересылки."""
    text = await doctor._report_text(
        None, Settings(bot_token="123:TEST"), 1, DbDoctor(name="Врач", email=None),
        make_callback("menu:report"), "https://x/doctor?token=t",
    )
    assert text == texts.DOCTOR_REPORT_LINK.format(url="https://x/doctor?token=t")


# ─────────────────── Приглашение через Telegram — пациент (§21, релиз 0.4.14) ───────────────────

from aiogram.exceptions import TelegramForbiddenError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402

from bot.models import DoctorInvite, InviteLog  # noqa: E402

DOC_TG = 555_555


async def make_tg_doctor(session: AsyncSession, username: str = "ivanova_doc") -> DbDoctor:
    doctor, _ = await doctor_service.get_or_create_telegram_doctor(
        session, DOC_TG, username, "Анна Иванова"
    )
    return doctor


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")


class TestDoctorByTelegram:
    async def test_invite_link_screen(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        bot = make_bot("my_bp_diary_bot")
        await call(
            doctor.cb_doctor_by_telegram,
            callback=make_callback("set:doctor:tg"),
            state=make_state(),
            session=session,
            bot=bot,
        )
        text = edit.call_args.args[0]
        [invite] = (await session.execute(select(DoctorInvite))).scalars().all()
        url = f"https://t.me/my_bp_diary_bot?start=doc_{invite.code}"
        assert text == texts.DOCTOR_TG_INVITE.format(invite_url=url)
        assert text.startswith("Перешлите врачу в Telegram ссылку-приглашение")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_tg_invite_kb()
        assert edit.call_args.kwargs["link_preview_options"] is keyboards.NO_LINK_PREVIEW
        assert invite.patient_name == "Тест"
        # повторный заход — та же ссылка (уже пересланная не сгорает)
        await call(
            doctor.cb_doctor_by_telegram,
            callback=make_callback("set:doctor:tg"),
            state=make_state(),
            session=session,
            bot=bot,
        )
        assert edit.call_args.args[0] == text

    async def test_doctor_already_added(self, edit, cb_answer, with_settings, session, user) -> None:
        """Один врач на пациента (C1): вместо приглашения — экран «Мой врач»."""
        with_settings(**cabinet_on())
        await make_link(session)
        await call(
            doctor.cb_doctor_by_telegram,
            callback=make_callback("set:doctor:tg"),
            state=make_state(),
            session=session,
        )
        assert edit.call_args.args[0].startswith(texts.DOCTOR_MENU)
        assert (await session.execute(select(DoctorInvite))).scalars().all() == []

    async def test_email_choice_with_doctor_already(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await make_link(session)
        state = make_state()
        await call(doctor.cb_doctor_by_email, callback=make_callback("set:doctor:email"), state=state, session=session)
        assert edit.call_args.args[0].startswith(texts.DOCTOR_MENU)
        assert await state.get_state() is None

    async def test_find_prompt(self, edit, cb_answer) -> None:
        state = make_state()
        await call(doctor.cb_doctor_find, callback=make_callback("set:doctor:tg:find"), state=state)
        assert edit.call_args.args[0] == texts.DOCTOR_USERNAME_ASK
        assert await state.get_state() == doctor.DoctorAccess.username.state


class TestFindByUsername:
    async def _send(self, session, text, bot=None, user_id=TG_ID):
        state = make_state(user_id)
        await state.set_state(doctor.DoctorAccess.username)
        bot = bot or make_bot()
        await call(
            doctor.msg_doctor_username,
            message=make_message(text, user_id=user_id),
            state=state,
            session=session,
            bot=bot,
        )
        return state, bot

    async def test_bad_format_keeps_state(self, answer, session, user) -> None:
        state, _ = await self._send(session, "иванова")
        assert answer.call_args.args[0] == texts.DOCTOR_USERNAME_BAD
        assert await state.get_state() == doctor.DoctorAccess.username.state

    async def test_found_sends_invite_to_doctor(self, answer, session, user) -> None:
        await make_tg_doctor(session)
        state, bot = await self._send(session, "@Ivanova_Doc")
        assert answer.call_args.args[0] == texts.DOCTOR_TG_SENT == (
            "Приглашение отправлено врачу в Telegram. Сообщу, когда он ответит."
        )
        assert await state.get_state() is None
        [invite] = (await session.execute(select(DoctorInvite))).scalars().all()
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.call_args
        assert args[0] == DOC_TG
        assert args[1] == texts.DOCTOR_INVITE_FOR_DOCTOR.format(name="Тест")
        assert kwargs["reply_markup"] == keyboards.doctor_invite_kb(invite.code)
        [row] = (await session.execute(select(InviteLog))).scalars().all()
        assert (row.channel, row.target, row.kind, row.ok) == ("telegram", "ivanova_doc", "invite", True)

    async def test_not_found_same_answer_for_patient(self, answer, session, user) -> None:
        """«Не найден» одинаков для «нет в боте» и «не врач» — пациенты не ищутся (T3)."""
        other = User_(telegram_id=222_222)
        session.add(other)
        await session.commit()
        _, bot = await self._send(session, "@somebody")
        [invite] = (await session.execute(select(DoctorInvite))).scalars().all()
        url = f"https://t.me/my_bp_diary_bot?start=doc_{invite.code}"
        assert answer.call_args.args[0] == texts.DOCTOR_TG_NOT_FOUND.format(invite_url=url)
        bot.send_message.assert_not_awaited()

    async def test_self_is_not_found(self, answer, session, user) -> None:
        """Пациент, который сам врач, не приглашает себя."""
        await doctor_service.get_or_create_telegram_doctor(session, TG_ID, "self_doc", "Я")
        _, bot = await self._send(session, "@self_doc")
        assert answer.call_args.args[0].startswith("Врач с таким именем не найден.")
        bot.send_message.assert_not_awaited()

    async def test_limit_one_per_hour_per_doctor(self, answer, session, user) -> None:
        await make_tg_doctor(session)
        await self._send(session, "@ivanova_doc")
        _, bot = await self._send(session, "@ivanova_doc")
        assert answer.call_args.args[0].startswith("Приглашение уже отправлено недавно.")
        bot.send_message.assert_not_awaited()

    async def test_doctor_blocked_bot(self, answer, session, user) -> None:
        await make_tg_doctor(session)
        bot = make_bot()
        bot.send_message.side_effect = forbidden()
        await self._send(session, "@ivanova_doc", bot=bot)
        assert answer.call_args.args[0].startswith("Врач с таким именем не найден.")
        [row] = (await session.execute(select(InviteLog))).scalars().all()
        assert row.ok is False


class TestTelegramDoctorReportAndRevoke:
    async def _tg_link(self, session) -> DbDoctor:
        tg_doctor = await make_tg_doctor(session)
        await doctor_service.grant_access(session, tg_doctor.id, 1)
        return tg_doctor

    async def test_report_notifies_doctor(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await self._tg_link(session)
        bot = make_bot()
        await call(
            doctor.cb_send_report, callback=make_callback("menu:report"),
            state=make_state(), session=session, bot=bot,
        )
        text = edit.call_args.args[0]
        assert text == texts.DOCTOR_REPORT_TELEGRAM.format(url=extract_url(text))
        args, kwargs = bot.send_message.call_args
        assert args == (DOC_TG, texts.DOCTOR_REPORT_FOR_DOCTOR.format(name="Тест"))
        assert kwargs["reply_markup"] == keyboards.doctor_open_kb(1)

    async def test_report_doctor_unreachable(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await self._tg_link(session)
        bot = make_bot()
        bot.send_message.side_effect = forbidden()
        await call(
            doctor.cb_send_report, callback=make_callback("menu:report"),
            state=make_state(), session=session, bot=bot,
        )
        text = edit.call_args.args[0]
        assert text == texts.DOCTOR_REPORT_LINK.format(url=extract_url(text))

    async def test_list_shows_doctor_name(self, answer, state, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        await self._tg_link(session)
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        assert "• Анна Иванова — доступ открыт" in answer.call_args.args[0]

    async def test_revoke_notifies_doctor(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        tg_doctor = await self._tg_link(session)
        bot = make_bot()
        await call(
            doctor.cb_doctor_revoke, callback=make_callback(f"set:doctor:revoke:{tg_doctor.id}"),
            state=make_state(), session=session, bot=bot,
        )
        bot.send_message.assert_awaited_once_with(
            DOC_TG, texts.DOCTOR_ACCESS_REVOKED_FOR_DOCTOR.format(name="Тест"), reply_markup=None
        )

    async def test_revoke_email_doctor_no_notification(self, edit, cb_answer, with_settings, session, user) -> None:
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)
        bot = make_bot()
        await call(
            doctor.cb_doctor_revoke, callback=make_callback(f"set:doctor:revoke:{db_doctor.id}"),
            state=make_state(), session=session, bot=bot,
        )
        bot.send_message.assert_not_awaited()


def test_telegram_texts_verbatim() -> None:
    assert texts.DOCTOR_TG_NOT_FOUND.format(invite_url="U") == (
        "Врач с таким именем не найден. Перешлите ему ссылку-приглашение:\nU"
    )
    assert texts.DOCTOR_TG_DECLINED == (
        "Врач не принял приглашение. Можно пригласить другого врача: «Настройки» → «Мой врач»."
    )
    assert texts.DOCTOR_USERNAME_BAD == (
        "Имя пользователя — от 5 до 32 латинских букв, цифр и «_». Попробуйте ещё раз:"
    )
