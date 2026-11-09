"""Тесты экранов согласия в боте (SPEC.md, §6.11).

Без триггеров согласия по умолчанию (`real_consent`). Покрывается: Б1 новому
и существующему (`/start`, «Начать», «Вести свой дневник»), «Согласен» /
«Не согласен» / «Назад к согласию», Д4 для `active` и `pending`, настройки и
отзыв согласия (удаление + журнал), `ConsentMiddleware` (блокировка,
исключения, Д4), Б7 у пациента, Б8 у врача, напоминания без согласия.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import consent as consent_handlers
from bot.handlers import doctor as doctor_handlers
from bot.handlers import doctor_menu, start
from bot.middlewares import ConsentMiddleware
from bot.models import Consent, Doctor, DoctorInvite, DoctorPatient, Reminder, User
from bot.services import consent as consent_service
from bot.services import doctor as doctor_service
from bot.services.deletion_log import DeletionLog
from tests.test_handlers_doctor import (
    TG_ID,
    call,
    make_bot,
    make_callback,
    make_message,
    make_state,
)

pytestmark = pytest.mark.real_consent

PRIVACY = "https://bp-diary.example.org/privacy.html"
DOC_TG = 900_900


@pytest.fixture(autouse=True)
def settings(mocker) -> Settings:
    """Настройки с адресом политики и развёрнутым кабинетом."""
    value = Settings(
        bot_token="123:TEST",
        privacy_url=PRIVACY,
        web_secret_key="secret",
        cabinet_url="https://cabinet.example.org",
    )
    for module in (consent_handlers, doctor_handlers, doctor_menu):
        mocker.patch.object(module, "get_settings", return_value=value)
    return value


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


def fake_reminders() -> MagicMock:
    reminders = MagicMock()
    reminders.remove_user_jobs = AsyncMock(return_value=0)
    return reminders


async def onboard(session: AsyncSession, user: User) -> User:
    user.onboarded = True
    await session.commit()
    return user


async def make_doctor(session: AsyncSession, telegram_id: int = DOC_TG, *, agreed: bool = True) -> Doctor:
    doctor = Doctor(telegram_id=telegram_id, name="Анна Иванова", password_hash="x")
    if agreed:
        doctor.consent_version = consent_service.DOCTOR_CONSENT_VERSION
        doctor.consent_at = datetime.now(timezone.utc)
        doctor.consent_channel = "bot"
    session.add(doctor)
    await session.commit()
    return doctor


async def consents(session: AsyncSession, **where) -> int:
    query = select(func.count(Consent.id))
    for key, value in where.items():
        column = getattr(Consent, key)
        query = query.where(column.is_(None) if value is None else column == value)
    return int((await session.execute(query)).scalar_one())


# ─────────────────────────── /start и Б1 ───────────────────────────


class TestStart:
    async def test_new_user_sees_consent(self, answer, session: AsyncSession) -> None:
        await start.cmd_start(make_message("/start"), make_state(), session)
        text = answer.call_args.args[0]
        assert text == texts.consent_text(PRIVACY, existing=False)
        assert text.startswith(texts.CONSENT_FIRST_LINE_NEW)
        assert f"{PRIVACY}#consent" in text and "16 лет" in text
        assert answer.call_args.kwargs["reply_markup"] == keyboards.consent_kb(PRIVACY, existing=False)

    async def test_existing_without_consent(self, answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await start.cmd_start(make_message("/start"), make_state(), session)
        assert answer.call_args.args[0].startswith(texts.CONSENT_FIRST_LINE_EXISTING)
        markup = answer.call_args.kwargs["reply_markup"]
        assert markup == keyboards.consent_kb(PRIVACY, existing=True)
        assert any(b.callback_data == "set:delete" for row in markup.inline_keyboard for b in row)

    async def test_consented_onboarded_gets_menu(self, answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await consent_service.give_processing(session, user.id, "bot")
        await start.cmd_start(make_message("/start"), make_state(), session)
        assert answer.call_args.args[0] == texts.MAIN_MENU

    async def test_consented_with_unconfirmed_doctor_gets_d4(
        self, answer, session: AsyncSession, user: User
    ) -> None:
        await onboard(session, user)
        await consent_service.give_processing(session, user.id, "bot")
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()
        await start.cmd_start(make_message("/start"), make_state(), session)
        assert answer.call_args.args[0] == texts.CONFIRM_DOCTOR_ACTIVE.format(doctor="Анна Иванова")

    async def test_onb_start_without_consent(self, edit, cb_answer, session: AsyncSession) -> None:
        await start.cb_onb_start(make_callback("onb:start"), make_state(), session)
        assert edit.call_args.args[0] == texts.consent_text(PRIVACY, existing=False)
        cb_answer.assert_awaited_once_with(texts.CONSENT_REQUIRED)


# ─────────────────────────── Согласен / не согласен ───────────────────────────


class TestConsentButtons:
    async def test_yes_new_user_starts_onboarding(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(consent_handlers.cb_consent_yes, callback=make_callback("cons:yes"), state=make_state(), session=session)
        assert edit.call_args.args[0] == f"{texts.CONSENT_SAVED}\n\n{texts.WELCOME}"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.onboarding_start_kb()
        assert await consents(session, kind="processing", channel="bot") == 1

    async def test_yes_existing_gets_menu(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await call(consent_handlers.cb_consent_yes, callback=make_callback("cons:yes"), state=make_state(), session=session)
        assert edit.call_args.args[0] == f"{texts.CONSENT_SAVED}\n\n{texts.MAIN_MENU}"

    async def test_yes_clears_deletion_request(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        user.deletion_requested_at = datetime.now(timezone.utc)
        await onboard(session, user)
        await call(consent_handlers.cb_consent_yes, callback=make_callback("cons:yes"), state=make_state(), session=session)
        await session.refresh(user)
        assert user.deletion_requested_at is None
        assert await consent_service.has_processing(session, user.id)

    async def test_yes_existing_with_pending_doctor_gets_d4(
        self, edit, cb_answer, session: AsyncSession, user: User
    ) -> None:
        await onboard(session, user)
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="pending"))
        await session.commit()
        await call(consent_handlers.cb_consent_yes, callback=make_callback("cons:yes"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.CONFIRM_DOCTOR_PENDING.format(doctor="Анна Иванова")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.confirm_doctor_kb(doctor.id, pending=True)

    async def test_no_new_user_deleted(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await call(
            consent_handlers.cb_consent_no,
            callback=make_callback("cons:no"),
            state=make_state(),
            session=session,
            reminders=fake_reminders(),
        )
        assert edit.call_args.args[0] == texts.CONSENT_DECLINED_NEW
        assert (await session.execute(select(User))).first() is None

    async def test_no_without_user_row(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(
            consent_handlers.cb_consent_no,
            callback=make_callback("cons:no"),
            state=make_state(),
            session=session,
            reminders=fake_reminders(),
        )
        assert edit.call_args.args[0] == texts.CONSENT_DECLINED_NEW

    async def test_no_existing_offers_delete(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await call(
            consent_handlers.cb_consent_no,
            callback=make_callback("cons:no"),
            state=make_state(),
            session=session,
            reminders=fake_reminders(),
        )
        assert edit.call_args.args[0] == texts.CONSENT_DECLINED_EXISTING
        assert edit.call_args.kwargs["reply_markup"] == keyboards.consent_declined_kb()
        assert (await session.execute(select(User))).first() is not None

    async def test_back(self, edit, cb_answer) -> None:
        await consent_handlers.cb_consent_back(make_callback("cons:back"))
        assert edit.call_args.args[0] == texts.consent_text(PRIVACY, existing=True)


# ─────────────────────────── Д4 ───────────────────────────


class TestConfirmDoctor:
    async def _setup(self, session: AsyncSession, user: User, status: str) -> Doctor:
        await onboard(session, user)
        await consent_service.give_processing(session, user.id, "bot")
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status=status))
        await session.commit()
        return doctor

    async def test_keep(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user, "active")
        await consent_handlers.cb_keep_doctor(make_callback(f"cons:doc:keep:{doctor.id}"), session)
        assert await consents(session, kind="doctor", doctor_id=doctor.id) == 1
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert await consent_service.doctor_can_see(session, doctor, user.id)

    async def test_close_active_notifies_doctor(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user, "active")
        bot = make_bot()
        await consent_handlers.cb_close_doctor(make_callback(f"cons:doc:close:{doctor.id}"), session, bot)
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link.status == "revoked"
        bot.send_message.assert_awaited_once_with(
            DOC_TG, texts.DOCTOR_ACCESS_REVOKED_FOR_DOCTOR.format(name="Тест")
        )
        cb_answer.assert_awaited_with(texts.DOCTOR_REVOKED)

    async def test_close_active_doctor_blocked_bot(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import SendMessage

        doctor = await self._setup(session, user, "active")
        bot = make_bot()
        bot.send_message.side_effect = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")
        await consent_handlers.cb_close_doctor(make_callback(f"cons:doc:close:{doctor.id}"), session, bot)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_cancel_pending_invite(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user, "pending")
        invite = await doctor_service.create_invite(session, user.id, "Тест")
        bot = make_bot()
        await consent_handlers.cb_close_doctor(make_callback(f"cons:doc:close:{doctor.id}"), session, bot)
        assert (await session.get(DoctorInvite, invite.code, populate_existing=True)).used_at is not None
        bot.send_message.assert_not_awaited()

    @pytest.mark.parametrize("data", ["cons:doc:keep:abc", "cons:doc:keep:999", "cons:doc:close:999"])
    async def test_foreign_or_bad_id(self, edit, cb_answer, session: AsyncSession, user: User, data) -> None:
        await self._setup(session, user, "active")
        handler = consent_handlers.cb_keep_doctor if ":keep:" in data else consent_handlers.cb_close_doctor
        await call(handler, callback=make_callback(data), session=session)
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)

    async def test_without_user(self, edit, cb_answer, session: AsyncSession) -> None:
        await consent_handlers.cb_keep_doctor(make_callback("cons:doc:keep:1"), session)
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)


# ─────────────────────────── Настройки и отзыв ───────────────────────────


class TestSettingsConsent:
    async def test_settings_screen(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await consent_service.give_processing(
            session, user.id, "bot", datetime(2026, 11, 10, 21, 30, tzinfo=timezone.utc)
        )
        await consent_handlers.cb_consent_settings(make_callback("set:consent"), session)
        # 21:30 UTC — уже 11.11 в Минске
        assert edit.call_args.args[0] == texts.CONSENT_SETTINGS.format(date="11.11.2026", privacy_url=PRIVACY)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.consent_settings_kb()

    async def test_withdraw_confirm(self, edit, cb_answer) -> None:
        await consent_handlers.cb_consent_withdraw(make_callback("set:consent:withdraw"))
        assert edit.call_args.args[0] == texts.CONSENT_WITHDRAW_CONFIRM
        assert edit.call_args.kwargs["reply_markup"] == keyboards.consent_withdraw_kb()

    async def test_withdraw_deletes_and_logs(
        self, edit, cb_answer, session: AsyncSession, user: User, tmp_path: Path
    ) -> None:
        log = DeletionLog(tmp_path / "deletions.log", "key")
        reminders = fake_reminders()
        await call(
            consent_handlers.cb_consent_withdraw_ok,
            callback=make_callback("set:consent:withdraw:ok"),
            state=make_state(),
            session=session,
            reminders=reminders,
            deletion_log=log,
        )
        reminders.remove_user_jobs.assert_awaited_once_with(user.id)
        assert (await session.execute(select(User))).first() is None
        assert log.marks() == {log.mark(TG_ID)}
        assert edit.call_args.args[0] == texts.CONSENT_WITHDRAWN

    async def test_withdraw_twice(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(
            consent_handlers.cb_consent_withdraw_ok,
            callback=make_callback("set:consent:withdraw:ok"),
            state=make_state(),
            session=session,
            reminders=fake_reminders(),
        )
        assert edit.call_args.args[0] == texts.CONSENT_WITHDRAWN


# ─────────────────────────── ConsentMiddleware ───────────────────────────


class TestMiddleware:
    async def _run(self, event, session: AsyncSession):
        handler = AsyncMock(return_value="handled")
        state = make_state()
        await state.set_state("Some:state")
        result = await ConsentMiddleware()(handler, event, {"session": session, "state": state})
        return handler, result, state

    async def test_new_user_passes(self, session: AsyncSession) -> None:
        handler, result, _ = await self._run(make_message("привет"), session)
        assert result == "handled"

    async def test_not_onboarded_passes(self, session: AsyncSession, user: User) -> None:
        handler, result, _ = await self._run(make_message("привет"), session)
        assert result == "handled"

    async def test_onboarded_without_consent_message(self, answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        handler, result, state = await self._run(make_message("120/80"), session)
        handler.assert_not_awaited()
        assert result is None
        assert answer.call_args.args[0] == texts.consent_text(PRIVACY, existing=True)
        assert await state.get_state() is None

    async def test_onboarded_without_consent_callback(
        self, answer, cb_answer, session: AsyncSession, user: User
    ) -> None:
        await onboard(session, user)
        handler, result, _ = await self._run(make_callback("menu:stats"), session)
        handler.assert_not_awaited()
        cb_answer.assert_awaited_once_with(texts.CONSENT_REQUIRED)
        assert answer.call_args.args[0] == texts.consent_text(PRIVACY, existing=True)

    @pytest.mark.parametrize(
        "event",
        [
            make_message("/start"),
            make_message("/start doc_abc"),
            make_message("/iamdoctor"),
            make_message("/admin@my_bp_diary_bot"),
            make_callback("cons:yes"),
            make_callback("set:delete"),
            make_callback("set:delete:ok"),
            make_callback("doc:menu"),
            make_callback("adm:stats"),
        ],
    )
    async def test_exempt(self, event, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        handler, result, _ = await self._run(event, session)
        assert result == "handled"

    async def test_consented_passes(self, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await consent_service.give_processing(session, user.id, "bot")
        handler, result, _ = await self._run(make_callback("menu:stats"), session)
        assert result == "handled"

    async def test_unconfirmed_doctor_shows_d4(self, answer, cb_answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        await consent_service.give_processing(session, user.id, "bot")
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()
        handler, result, _ = await self._run(make_callback("menu:report"), session)
        handler.assert_not_awaited()
        cb_answer.assert_awaited_once_with(None)
        assert answer.call_args.args[0] == texts.CONFIRM_DOCTOR_ACTIVE.format(doctor="Анна Иванова")

    async def test_without_session_or_user(self) -> None:
        handler = AsyncMock(return_value="ok")
        assert await ConsentMiddleware()(handler, make_message("x"), {}) == "ok"
        assert await ConsentMiddleware()(handler, object(), {"session": object()}) == "ok"

    async def test_callback_without_message(self, cb_answer, session: AsyncSession, user: User) -> None:
        await onboard(session, user)
        callback = make_callback("menu:stats").model_copy(update={"message": None})
        handler, result, _ = await self._run(callback, session)
        assert result is None
        cb_answer.assert_awaited_once_with(texts.CONSENT_REQUIRED)


# ─────────────────────────── Б7: согласие на врача ───────────────────────────


class TestInviteConsent:
    async def test_entry_without_consent_shows_b7(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await call(doctor_handlers.cb_doctor, callback=make_callback("set:doctor"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_CONSENT_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_consent_kb()

    async def test_agree_shows_choice(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await call(doctor_handlers.cb_doctor_agree, callback=make_callback("set:doctor:agree"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_NO_LINK
        assert await consent_service.has_invite_consent(session, user.id)

    async def test_agree_with_doctor_skips(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="pending"))
        await session.commit()
        await call(doctor_handlers.cb_doctor_agree, callback=make_callback("set:doctor:agree"), state=make_state(), session=session)
        assert not await consent_service.has_invite_consent(session, user.id)

    @pytest.mark.parametrize("handler_name", ["cb_doctor_by_email", "cb_doctor_by_telegram"])
    async def test_steps_require_consent(self, edit, cb_answer, session: AsyncSession, user: User, handler_name) -> None:
        handler = getattr(doctor_handlers, handler_name)
        await call(handler, callback=make_callback("set:doctor:x"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_CONSENT_ASK

    async def test_email_message_requires_consent(self, answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await call(doctor_handlers.msg_doctor_email, message=make_message("doc@example.org"), state=state, session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_CONSENT_ASK
        assert (await session.execute(select(Doctor))).first() is None

    async def test_username_message_requires_consent(self, answer, session: AsyncSession, user: User) -> None:
        await call(doctor_handlers.msg_doctor_username, message=make_message("@ivanova_doc"), state=make_state(), session=session)
        assert answer.call_args.args[0] == texts.DOCTOR_CONSENT_ASK

    async def test_email_attaches_consent(self, answer, session: AsyncSession, user: User) -> None:
        await consent_service.give_doctor(session, user.id, None)
        await call(doctor_handlers.msg_doctor_email, message=make_message("doc@example.org"), state=make_state(), session=session)
        doctor = (await session.execute(select(Doctor))).scalar_one()
        assert await consents(session, kind="doctor", doctor_id=doctor.id, withdrawn_at=None) == 1

    async def test_revoke_withdraws_consent(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()
        await consent_service.give_doctor(session, user.id, doctor.id)
        await call(
            doctor_handlers.cb_doctor_revoke,
            callback=make_callback(f"set:doctor:revoke:{doctor.id}"),
            state=make_state(),
            session=session,
        )
        assert await consents(session, kind="doctor", withdrawn_at=None) == 0

    async def test_report_to_doctor_without_own_consent(self, session: AsyncSession, user: User, settings) -> None:
        doctor = await make_doctor(session, agreed=False)
        bot = make_bot()
        text = await doctor_handlers._report_text(
            session, settings, user.id, doctor, make_callback("menu:report"), "https://x/link", bot
        )
        assert text == texts.DOCTOR_REPORT_LINK.format(url="https://x/link")
        bot.send_message.assert_not_awaited()


# ─────────────────────────── Б8: согласие врача ───────────────────────────


class TestDoctorSelfConsent:
    async def _invite(self, session: AsyncSession, user: User) -> DoctorInvite:
        return await doctor_service.create_invite(session, user.id, "Тест")

    def _doc_cb(self, data: str):
        return make_callback(data, user_id=DOC_TG)

    async def test_accept_new_doctor_attaches_patient_consent(
        self, edit, cb_answer, session: AsyncSession, user: User
    ) -> None:
        await consent_service.give_processing(session, user.id, "bot")
        await consent_service.give_doctor(session, user.id, None)
        invite = await self._invite(session, user)
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=self._doc_cb(f"doc:consent:yes:{invite.code}"),
            state=make_state(DOC_TG),
            session=session,
        )
        doctor = await doctor_service.doctor_by_telegram(session, DOC_TG)
        assert consent_service.doctor_ok(doctor) and doctor.consent_channel == "bot"
        assert await consent_service.doctor_can_see(session, doctor, user.id)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_ACCEPTED

    async def test_accept_existing_doctor_without_consent(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await make_doctor(session, agreed=False)
        invite = await self._invite(session, user)
        await call(doctor_menu.cb_accept, callback=self._doc_cb(f"doc:accept:{invite.code}"), session=session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb(invite.code)
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=self._doc_cb(f"doc:consent:yes:{invite.code}"),
            state=make_state(DOC_TG),
            session=session,
        )
        doctor = await doctor_service.doctor_by_telegram(session, DOC_TG)
        assert consent_service.doctor_ok(doctor)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_ACCEPTED

    async def test_accept_invalid_code_without_consent(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(doctor_menu.cb_accept, callback=self._doc_cb("doc:accept:nope"), session=session)
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_INVALID

    async def test_consent_yes_expired_code(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=self._doc_cb("doc:consent:yes:nope"),
            state=make_state(DOC_TG),
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_INVITE_INVALID

    async def test_consent_yes_existing_doctor_shows_panel(self, edit, cb_answer, session: AsyncSession) -> None:
        await make_doctor(session, agreed=False)
        await call(
            doctor_menu.cb_self_consent_yes,
            callback=self._doc_cb("doc:consent:yes"),
            state=make_state(DOC_TG),
            session=session,
        )
        assert edit.call_args.args[0] == texts.DOCTOR_PANEL
        assert consent_service.doctor_ok(await doctor_service.doctor_by_telegram(session, DOC_TG))

    async def test_consent_no_with_invite(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        invite = await self._invite(session, user)
        await call(doctor_menu.cb_self_consent_no, callback=self._doc_cb(f"doc:consent:no:{invite.code}"), session=session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_invite_kb(invite.code)

    async def test_consent_no_without_invite(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(doctor_menu.cb_self_consent_no, callback=self._doc_cb("doc:consent:no"), session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_decline_withdraws_patient_invite_consent(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await consent_service.give_doctor(session, user.id, None)
        invite = await self._invite(session, user)
        await call(doctor_menu.cb_decline, callback=self._doc_cb(f"doc:decline:{invite.code}"), session=session)
        assert not await consent_service.has_invite_consent(session, user.id)

    async def test_panel_requires_consent(self, edit, cb_answer, session: AsyncSession) -> None:
        await make_doctor(session, agreed=False)
        await call(doctor_menu.cb_panel, callback=self._doc_cb("doc:menu"), state=make_state(DOC_TG), session=session)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb()

    async def test_iamdoctor_registered_without_consent(self, answer, session: AsyncSession) -> None:
        await make_doctor(session, agreed=False)
        await call(doctor_menu.cmd_iamdoctor, message=make_message("/iamdoctor", DOC_TG), state=make_state(DOC_TG), session=session)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb()

    async def test_name_saved_with_consent_flag(self, answer, session: AsyncSession) -> None:
        state = make_state(DOC_TG)
        await state.set_state(doctor_menu.DoctorRegistration.name)
        await state.update_data(doctor_consent=True)
        await call(doctor_menu.msg_doctor_name, message=make_message("Анна Иванова", DOC_TG), state=state, session=session)
        assert consent_service.doctor_ok(await doctor_service.doctor_by_telegram(session, DOC_TG))

    async def test_open_patient_without_consent(self, answer, cb_answer, session: AsyncSession, user: User) -> None:
        await make_doctor(session, agreed=False)
        await call(doctor_menu.cb_open_patient, callback=self._doc_cb(f"doc:open:{user.id}"), session=session)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.doctor_self_consent_kb()

    async def test_open_patient_without_patient_consent(self, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()
        await call(doctor_menu.cb_open_patient, callback=self._doc_cb(f"doc:open:{user.id}"), session=session)
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)

    async def test_diary_without_consent(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(doctor_menu.cb_diary, callback=self._doc_cb("doc:diary"), state=make_state(DOC_TG), session=session)
        assert edit.call_args.args[0] == texts.consent_text(PRIVACY, existing=False)


# ─────────────────────────── Напоминания без согласия ───────────────────────────


class TestRemindersWithoutConsent:
    async def _service(self, session_factory):
        from bot.services.reminders import RemindersService

        scheduler = MagicMock()
        sender = AsyncMock()
        return RemindersService(session_factory, sender, scheduler), sender, scheduler

    async def test_slot_skipped_and_rescheduled(self, session_factory, session: AsyncSession, user: User) -> None:
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = Reminder(user_id=user.id, type="morning", local_time="08:00", next_trigger_time=due, enabled=True)
        session.add(reminder)
        await session.commit()
        service, sender, scheduler = await self._service(session_factory)
        assert await service._deliver(reminder.id, "slot") is False
        sender.assert_not_awaited()
        scheduler.add_job.assert_called_once()
        await session.refresh(reminder)
        assert reminder.next_trigger_time > datetime.now(timezone.utc)

    async def test_remeasure_skipped(self, session_factory, session: AsyncSession, user: User) -> None:
        reminder = Reminder(
            user_id=user.id, type="med_remeasure", next_trigger_time=datetime.now(timezone.utc), enabled=True
        )
        session.add(reminder)
        await session.commit()
        service, sender, _ = await self._service(session_factory)
        assert await service._deliver(reminder.id, "remeasure") is False
        sender.assert_not_awaited()
