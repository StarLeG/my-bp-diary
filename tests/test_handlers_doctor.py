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
from bot.models import Doctor as DbDoctor, DoctorPatient
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


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры."""
    wanted = inspect.signature(handler).parameters
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

    async def test_no_link_prompts_email(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_NO_LINK
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb(
            "set:doctor:cancel"
        )
        assert await state.get_state() == doctor.DoctorAccess.email.state

    async def test_open_link_shows_screen(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        db_doctor = await make_link(session)
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        text = answer.call_args.args[0]
        assert text.startswith(texts.DOCTOR_MENU)
        assert texts.DOCTOR_LINK_ITEM.format(
            email="doc@example.org",
            status=texts.DOCTOR_STATUS_LABELS["pending"],
        ) in text
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_kb(
            [(db_doctor.id, "doc@example.org", "pending")]
        )
        assert await state.get_state() is None

    async def test_revoked_only_prompts_email_again(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(**cabinet_on())
        await make_link(session, status="revoked")
        await call(doctor.cmd_doctor, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_NO_LINK
        assert await state.get_state() == doctor.DoctorAccess.email.state

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
