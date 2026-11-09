"""Сторона врача в боте (bot/handlers/doctor_menu.py, SPEC.md, §21).

Покрывается: `/start doc_<code>` (действующий, просроченный, списанный,
неизвестный код, приглашение самому себе), «Принять» (врач с именем из
профиля, связь `active`, уведомление пациенту, двойное «Принять»),
«Отклонить» (уведомление пациенту), `/iamdoctor` (регистрация, смена ФИО,
неверное ФИО, с username и без), меню врача (не врач — главное меню),
ссылка входа (15 минут, соль `doctor-signin`, с пациентом), «Мои пациенты»
(только `active`), «Открыть в кабинете» (чужой пациент — отказ), «Вести свой
дневник», `/start` и «Назад» по контексту (N5), middleware username (T10).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select, update

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import doctor_menu, start
from bot.middlewares import DoctorUsernameMiddleware
from bot.models import Doctor, DoctorInvite, DoctorPatient, User as UserModel
from bot.services import doctor as doctor_service
from web.cabinet.doctor_auth import DOCTOR_SIGNIN_MAX_AGE, DoctorMagicLink, DoctorSignInLink

PATIENT_TG = 111_111
DOC_TG = 555_555
SECRET = "test-secret"
CABINET_URL = "https://cabinet.example.org"


def tg_user(user_id: int = DOC_TG, username: str | None = "Ivanova_Doc") -> User:
    return User(id=user_id, is_bot=False, first_name="Анна", last_name="Иванова", username=username)


def make_message(text: str | None = None, user: User | None = None) -> Message:
    user = user or tg_user()
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=text,
    )


def make_callback(data: str, user: User | None = None) -> CallbackQuery:
    user = user or tg_user()
    return CallbackQuery(
        id="q1", from_user=user, chat_instance="ci", data=data, message=make_message(user=user)
    )


def make_state(user_id: int = DOC_TG) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


async def call(handler, **kwargs) -> None:
    wanted = inspect.signature(handler).parameters
    if "bot" in wanted and "bot" not in kwargs:
        kwargs["bot"] = make_bot()
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture(autouse=True)
def default_settings(mocker) -> Settings:
    """Настройки без `.env`: экраны врача (Б8) читают `PRIVACY_URL` (§6.11)."""
    settings = Settings(bot_token="123:TEST")
    mocker.patch.object(doctor_menu, "get_settings", return_value=settings)
    return settings


@pytest.fixture
def cabinet(mocker) -> Settings:
    settings = Settings(bot_token="123:TEST", web_secret_key=SECRET, cabinet_url=CABINET_URL)
    mocker.patch.object(doctor_menu, "get_settings", return_value=settings)
    return settings


async def invite_for(session, user, **overrides) -> DoctorInvite:
    invite = await doctor_service.create_invite(session, user.id, "Пётр")
    if overrides:
        await session.execute(
            update(DoctorInvite).where(DoctorInvite.code == invite.code).values(**overrides)
        )
        await session.commit()
    return invite


async def start_with(session, code: str, user: User | None = None):
    await call(
        doctor_menu.cmd_start_invite,
        message=make_message(f"/start doc_{code}", user),
        command=CommandObject(prefix="/", command="start", args=f"doc_{code}"),
        state=make_state(),
        session=session,
    )


async def make_doctor(session) -> Doctor:
    doctor, _ = await doctor_service.get_or_create_telegram_doctor(session, DOC_TG, "ivanova_doc", "Анна Иванова")
    return doctor


# ─────────────────────────── Приглашение ───────────────────────────


class TestStartInvite:
    async def test_valid_invite(self, answer, session, user) -> None:
        invite = await invite_for(session, user)
        await start_with(session, invite.code)
        assert answer.call_args.args[0] == (
            "Пациент Пётр открыл вам доступ к дневнику давления: измерения, графики, отчёт.\n"
            "Принять приглашение?"
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_invite_kb(invite.code)
        # онбординг пациента врач не проходит: строки users нет
        assert (await session.execute(select(UserModel).where(UserModel.telegram_id == DOC_TG))).first() is None

    @pytest.mark.parametrize("case", ["expired", "used", "unknown", "self"])
    async def test_invalid_invite(self, answer, session, user, case) -> None:
        now = datetime.now(timezone.utc)
        overrides = {
            "expired": {"expires_at": now - timedelta(minutes=1)},
            "used": {"used_at": now},
        }.get(case, {})
        invite = await invite_for(session, user, **overrides)
        code = "nope" if case == "unknown" else invite.code
        who = tg_user(PATIENT_TG) if case == "self" else None
        await start_with(session, code, who)
        assert answer.call_args.args[0] == texts.DOCTOR_INVITE_INVALID == (
            "Приглашение устарело или уже использовано. Попросите пациента прислать новое."
        )

    async def test_router_catches_only_doc_prefix(self) -> None:
        """`/start landing` уходит в обычный /start, `/start doc_…` — сюда (роутер раньше start)."""
        from bot.handlers import router

        names = [r.name for r in router.sub_routers]
        assert names.index("doctor_menu") < names.index("start")


class TestAcceptDecline:
    async def test_accept(self, edit, cb_answer, session, user) -> None:
        invite = await invite_for(session, user)
        bot = make_bot()
        # Новый врач: сначала согласие врача (Б8, §6.11), затем приём.
        await call(doctor_menu.cb_accept, callback=make_callback(f"doc:accept:{invite.code}"), session=session, bot=bot)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb(invite.code)
        assert await doctor_service.doctor_by_telegram(session, DOC_TG) is None
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=make_callback(f"doc:consent:yes:{invite.code}"),
            state=make_state(),
            session=session,
            bot=bot,
        )
        doctor = await doctor_service.doctor_by_telegram(session, DOC_TG)
        assert doctor is not None and doctor.name == "Анна Иванова" and doctor.telegram_username == "ivanova_doc"
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link.status == "active"
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_ACCEPTED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_panel_kb(show_diary=True)
        bot.send_message.assert_awaited_once_with(PATIENT_TG, "Врач Анна Иванова принял приглашение — доступ открыт.")
        # двойное «Принять» — «устарело», второе уведомление не уходит
        await call(doctor_menu.cb_accept, callback=make_callback(f"doc:accept:{invite.code}"), session=session, bot=bot)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_INVALID
        assert bot.send_message.await_count == 1

    async def test_accept_own_invite_rejected(self, edit, cb_answer, session, user) -> None:
        invite = await invite_for(session, user)
        await call(
            doctor_menu.cb_accept,
            callback=make_callback(f"doc:accept:{invite.code}", tg_user(PATIENT_TG)),
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_INVALID
        assert await doctor_service.active_invite(session, invite.code) is not None  # не списан

    async def test_accept_patient_unreachable(self, edit, cb_answer, session, user) -> None:
        invite = await invite_for(session, user)
        bot = make_bot()
        bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=1, text="x"), message="blocked"
        )
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=make_callback(f"doc:consent:yes:{invite.code}"),
            state=make_state(),
            session=session,
            bot=bot,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_ACCEPTED

    async def test_decline(self, edit, cb_answer, session, user) -> None:
        invite = await invite_for(session, user)
        bot = make_bot()
        await call(doctor_menu.cb_decline, callback=make_callback(f"doc:decline:{invite.code}"), session=session, bot=bot)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_DECLINED
        bot.send_message.assert_awaited_once_with(PATIENT_TG, texts.DOCTOR_TG_DECLINED)
        assert (await session.execute(select(DoctorPatient))).first() is None
        assert await doctor_service.active_invite(session, invite.code) is None
        await call(doctor_menu.cb_decline, callback=make_callback(f"doc:decline:{invite.code}"), session=session, bot=bot)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_INVALID


# ─────────────────────────── /iamdoctor ───────────────────────────


class TestIamDoctor:
    async def test_new_doctor_asks_name(self, answer, edit, cb_answer, session) -> None:
        state = make_state()
        await call(doctor_menu.cmd_iamdoctor, message=make_message("/iamdoctor"), state=state, session=session)
        # Сначала согласие врача (Б8, §6.11), затем ввод ФИО.
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb()
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=make_callback("doc:consent:yes"),
            state=state,
            session=session,
            bot=make_bot(),
        )
        assert edit.call_args.args[0] == texts.DOCTOR_REGISTER_ASK
        assert await state.get_state() == doctor_menu.DoctorRegistration.name.state

    async def test_registered_doctor(self, answer, session) -> None:
        await make_doctor(session)
        await call(doctor_menu.cmd_iamdoctor, message=make_message("/iamdoctor"), state=make_state(), session=session)
        assert answer.call_args.args[0] == "Вы зарегистрированы как врач: Анна Иванова. Изменить ФИО?"
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_registered_kb()

    async def test_change_name_button(self, edit, cb_answer) -> None:
        state = make_state()
        await call(doctor_menu.cb_change_name, callback=make_callback("doc:name"), state=state)
        assert edit.call_args.args[0] == texts.DOCTOR_REGISTER_ASK
        assert await state.get_state() == doctor_menu.DoctorRegistration.name.state

    @pytest.mark.parametrize("name", ["А", "", "Я" * 101])
    async def test_bad_name(self, answer, session, name) -> None:
        state = make_state()
        await state.set_state(doctor_menu.DoctorRegistration.name)
        await call(doctor_menu.msg_doctor_name, message=make_message(name), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_NAME_BAD
        assert await state.get_state() == doctor_menu.DoctorRegistration.name.state

    async def test_saved_with_username(self, answer, session) -> None:
        state = make_state()
        await state.set_state(doctor_menu.DoctorRegistration.name)
        await call(doctor_menu.msg_doctor_name, message=make_message("  Иванова   Анна  "), state=state, session=session)
        assert answer.call_args.args[0] == "Готово! Пациенты могут найти вас по @ivanova_doc."
        doctor = await doctor_service.doctor_by_telegram(session, DOC_TG)
        assert doctor.name == "Иванова Анна"
        assert await state.get_state() is None

    async def test_saved_without_username(self, answer, session) -> None:
        state = make_state()
        await state.set_state(doctor_menu.DoctorRegistration.name)
        await call(
            doctor_menu.msg_doctor_name,
            message=make_message("Иванова Анна", tg_user(username=None)),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.DOCTOR_NAME_SAVED_NO_USERNAME


# ─────────────────────────── Меню врача ───────────────────────────


class TestPanel:
    async def test_not_doctor_gets_main_menu(self, edit, cb_answer, session) -> None:
        await call(doctor_menu.cb_panel, callback=make_callback("doc:menu"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_panel_for_doctor_patient(self, edit, cb_answer, session) -> None:
        """Врач, прошедший онбординг пациента, — без «Вести свой дневник»."""
        await make_doctor(session)
        session.add(UserModel(telegram_id=DOC_TG, onboarded=True))
        await session.commit()
        await call(doctor_menu.cb_panel, callback=make_callback("doc:menu"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_PANEL
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_panel_kb(show_diary=False)

    async def test_cabinet_signin_link(self, edit, cb_answer, session, cabinet) -> None:
        doctor = await make_doctor(session)
        await call(doctor_menu.cb_cabinet, callback=make_callback("doc:cabinet"), session=session)
        text = edit.call_args.args[0]
        assert text.startswith("Ссылка для входа в кабинет врача (действует 15 минут): ")
        token = text.split("signin=", 1)[1]
        signin = DoctorSignInLink(SECRET).load(token)
        assert signin.doctor_id == doctor.id and signin.patient_user_id is None
        assert DOCTOR_SIGNIN_MAX_AGE == 15 * 60
        assert DoctorMagicLink(SECRET).load(token) is None  # своя соль
        assert edit.call_args.kwargs["link_preview_options"] is keyboards.NO_LINK_PREVIEW

    async def test_cabinet_disabled(self, edit, cb_answer, session, mocker) -> None:
        await make_doctor(session)
        mocker.patch.object(doctor_menu, "get_settings", return_value=Settings(bot_token="123:TEST"))
        await call(doctor_menu.cb_cabinet, callback=make_callback("doc:cabinet"), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_WEB_DISABLED

    async def test_cabinet_not_doctor(self, edit, cb_answer, session) -> None:
        await call(doctor_menu.cb_cabinet, callback=make_callback("doc:cabinet"), session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_patients_only_active(self, edit, cb_answer, session, user) -> None:
        doctor = await make_doctor(session)
        other = UserModel(telegram_id=222_222)
        session.add(other)
        await session.commit()
        await doctor_service.grant_access(session, doctor.id, user.id)
        await doctor_service.link_patient(session, doctor.id, other.id)  # pending
        await call(doctor_menu.cb_patients, callback=make_callback("doc:patients"), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_PATIENTS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_patients_kb([(user.id, PATIENT_TG)])

    async def test_patients_empty(self, edit, cb_answer, session) -> None:
        await make_doctor(session)
        await call(doctor_menu.cb_patients, callback=make_callback("doc:patients"), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_PATIENTS_EMPTY

    async def test_patients_not_doctor(self, edit, cb_answer, session) -> None:
        await call(doctor_menu.cb_patients, callback=make_callback("doc:patients"), session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU


class TestOpenPatient:
    async def test_open_active_patient(self, answer, cb_answer, session, user, cabinet) -> None:
        doctor = await make_doctor(session)
        await doctor_service.grant_access(session, doctor.id, user.id)
        await call(doctor_menu.cb_open_patient, callback=make_callback(f"doc:open:{user.id}"), session=session)
        text = answer.call_args.args[0]
        signin = DoctorSignInLink(SECRET).load(text.split("signin=", 1)[1])
        assert (signin.doctor_id, signin.patient_user_id) == (doctor.id, user.id)

    @pytest.mark.parametrize("status", ["pending", "revoked", None])
    async def test_open_without_access(self, answer, cb_answer, session, user, cabinet, status) -> None:
        doctor = await make_doctor(session)
        if status is not None:
            await doctor_service.link_patient(session, doctor.id, user.id)
            if status == "revoked":
                await doctor_service.revoke_access(session, user.id, doctor.id)
        await call(doctor_menu.cb_open_patient, callback=make_callback(f"doc:open:{user.id}"), session=session)
        answer.assert_not_awaited()
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)

    async def test_open_garbage_and_not_doctor(self, answer, cb_answer, session, cabinet) -> None:
        await call(doctor_menu.cb_open_patient, callback=make_callback("doc:open:abc"), session=session)
        cb_answer.assert_awaited_with(texts.NOT_YOUR_RECORD)

    async def test_open_cabinet_disabled(self, answer, cb_answer, session, user, mocker) -> None:
        doctor = await make_doctor(session)
        await doctor_service.grant_access(session, doctor.id, user.id)
        mocker.patch.object(doctor_menu, "get_settings", return_value=Settings(bot_token="123:TEST"))
        await call(doctor_menu.cb_open_patient, callback=make_callback(f"doc:open:{user.id}"), session=session)
        cb_answer.assert_awaited_once_with(texts.DOCTOR_WEB_DISABLED)


class TestDiary:
    async def test_starts_onboarding(self, edit, cb_answer, session) -> None:
        await make_doctor(session)
        await call(doctor_menu.cb_diary, callback=make_callback("doc:diary"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.WELCOME

    async def test_onboarded_gets_main_menu(self, edit, cb_answer, session) -> None:
        session.add(UserModel(telegram_id=DOC_TG, onboarded=True))
        await session.commit()
        await call(doctor_menu.cb_diary, callback=make_callback("doc:diary"), state=make_state(), session=session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu(is_doctor=True)


# ─────────────────── /start и «Назад» по контексту (N5) ───────────────────


class TestContextMenu:
    async def test_start_doctor_without_diary(self, answer, session) -> None:
        await make_doctor(session)
        await start.cmd_start(make_message("/start"), make_state(), session)
        assert answer.call_args.args[0] == texts.DOCTOR_PANEL
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_panel_kb(show_diary=True)
        assert (await session.execute(select(UserModel).where(UserModel.telegram_id == DOC_TG))).first() is None

    async def test_start_doctor_patient(self, answer, session) -> None:
        await make_doctor(session)
        session.add(UserModel(telegram_id=DOC_TG, onboarded=True))
        await session.commit()
        await start.cmd_start(make_message("/start"), make_state(), session)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.main_menu(is_doctor=True)
        buttons = [b.callback_data for row in keyboards.main_menu(is_doctor=True).inline_keyboard for b in row]
        assert buttons[-1] == "doc:menu"

    async def test_menu_back_for_doctor(self, edit, cb_answer, session) -> None:
        await make_doctor(session)
        await start.cb_menu_back(make_callback("menu:back"), make_state(), session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu(is_doctor=True)


class TestUsernameMiddleware:
    async def test_updates_doctor_username(self, session) -> None:
        await make_doctor(session)
        handler = AsyncMock(return_value="ok")
        result = await DoctorUsernameMiddleware()(
            handler, make_message("привет", tg_user(username="New_Doc")), {"session": session}
        )
        assert result == "ok"
        doctor = await doctor_service.doctor_by_telegram(session, DOC_TG)
        assert doctor.telegram_username == "new_doc"

    async def test_skips_without_session_and_survives_errors(self, session, mocker) -> None:
        handler = AsyncMock(return_value="ok")
        assert await DoctorUsernameMiddleware()(handler, make_message("x"), {}) == "ok"
        mocker.patch.object(doctor_service, "touch_username", side_effect=RuntimeError("db"))
        assert await DoctorUsernameMiddleware()(handler, make_message("x"), {"session": session}) == "ok"


async def test_decline_by_patient_himself_no_notification(edit, cb_answer, session, user) -> None:
    invite = await invite_for(session, user)
    bot = make_bot()
    await call(
        doctor_menu.cb_decline,
        callback=make_callback(f"doc:decline:{invite.code}", tg_user(PATIENT_TG)),
        session=session,
        bot=bot,
    )
    assert edit.call_args.args[0] == texts.DOCTOR_INVITE_DECLINED
    bot.send_message.assert_not_awaited()


async def test_grant_access_already_active(session, user) -> None:
    doctor = await make_doctor(session)
    first = await doctor_service.grant_access(session, doctor.id, user.id)
    again = await doctor_service.grant_access(session, doctor.id, user.id)
    assert first.id == again.id and again.status == "active"
