"""Тесты «Мои данные» в боте (SPEC.md, §6.12).

Покрывается: экран П1 / П2, ввод по шагам с ошибками П4 и «Нет отчества»,
младше 16 (П5), «Отмена», удаление, ввод перед приглашением врача и возврат
к выбору способа, П7 (один раз, после действия, не на `prof:*`), обновление
имени из Telegram, подписи пациента в «Мои пациенты» бота врача.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import doctor as doctor_handlers
from bot.handlers import doctor_menu
from bot.handlers import profile as profile_handlers
from bot.middlewares import ConsentMiddleware, TelegramNameMiddleware
from bot.models import Doctor, DoctorPatient, User
from bot.services import consent as consent_service
from tests.test_handlers_doctor import call, make_callback, make_message, make_state

DOC_TG = 900_900


@pytest.fixture(autouse=True)
def settings(mocker) -> Settings:
    value = Settings(bot_token="123:TEST", web_secret_key="s", cabinet_url="https://cab.example.org")
    mocker.patch.object(doctor_handlers, "get_settings", return_value=value)
    mocker.patch.object(doctor_menu, "get_settings", return_value=value)
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


async def fill(session: AsyncSession, user: User) -> User:
    user.last_name, user.first_name, user.middle_name = "Иванов", "Иван", "Иванович"
    user.birth_date, user.sex = date(1958, 3, 12), "m"
    await session.commit()
    return user


async def walk(session, state, *, last="Иванов", first="Иван", middle="Иванович", birth="12.03.1958"):
    """Пройти шаги до пола (сообщения)."""
    await call(profile_handlers.msg_last_name, message=make_message(last), state=state, session=session)
    await call(profile_handlers.msg_first_name, message=make_message(first), state=state, session=session)
    await call(profile_handlers.msg_middle_name, message=make_message(middle), state=state, session=session)
    await call(profile_handlers.msg_birth, message=make_message(birth), state=state, session=session)


class TestScreen:
    async def test_empty(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await call(profile_handlers.cb_profile, callback=make_callback("set:profile"), state=make_state(), session=session)
        assert edit.call_args.args[0] == texts.PROFILE_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.profile_kb(filled=False)

    async def test_filled(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await fill(session, user)
        await call(profile_handlers.cb_profile, callback=make_callback("set:profile"), state=make_state(), session=session)
        text = edit.call_args.args[0]
        assert "ФИО: Иванов Иван Иванович" in text and "Дата рождения: 12.03.1958 (" in text and "Пол: мужской" in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.profile_kb(filled=True)

    async def test_later(self, edit, cb_answer, session: AsyncSession) -> None:
        await call(profile_handlers.cb_later, callback=make_callback("prof:later"), session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU


class TestInput:
    async def test_full_flow(self, edit, answer, cb_answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await call(profile_handlers.cb_edit, callback=make_callback("prof:edit"), state=state, session=session)
        assert edit.call_args.args[0] == texts.PROFILE_ASK_LAST
        await walk(session, state)
        assert answer.call_args.args[0] == texts.PROFILE_ASK_SEX
        assert answer.call_args.kwargs["reply_markup"] == keyboards.profile_sex_kb()
        await call(profile_handlers.cb_sex, callback=make_callback("prof:sex:f"), state=state, session=session)
        await session.refresh(user)
        assert (user.last_name, user.middle_name, user.birth_date, user.sex) == ("Иванов", "Иванович", date(1958, 3, 12), "f")
        assert edit.call_args.args[0].startswith(texts.PROFILE_SAVED)
        assert await state.get_state() is None

    async def test_edit_shows_current(self, edit, answer, cb_answer, session: AsyncSession, user: User) -> None:
        await fill(session, user)
        state = make_state()
        await call(profile_handlers.cb_edit, callback=make_callback("prof:edit"), state=state, session=session)
        assert edit.call_args.args[0] == f"{texts.PROFILE_ASK_LAST}\nСейчас: Иванов"
        await call(profile_handlers.msg_last_name, message=make_message("Петров"), state=state, session=session)
        assert answer.call_args.args[0] == f"{texts.PROFILE_ASK_FIRST}\nСейчас: Иван"

    async def test_bad_names(self, answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await state.set_state(profile_handlers.PatientProfile.last_name)
        await call(profile_handlers.msg_last_name, message=make_message("Ив4н"), state=state, session=session)
        assert answer.call_args.args[0] == texts.PROFILE_NAME_BAD
        assert await state.get_state() == profile_handlers.PatientProfile.last_name.state
        await state.set_state(profile_handlers.PatientProfile.middle_name)
        await call(profile_handlers.msg_middle_name, message=make_message("1"), state=state, session=session)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.profile_middle_kb()

    async def test_no_middle(self, edit, answer, cb_answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await state.set_state(profile_handlers.PatientProfile.middle_name)
        await state.update_data(pf_last="Иванов", pf_first="Иван")
        await call(profile_handlers.cb_no_middle, callback=make_callback("prof:nomiddle"), state=state, session=session)
        assert edit.call_args.args[0] == texts.PROFILE_ASK_BIRTH
        await call(profile_handlers.msg_birth, message=make_message("12.03.1958"), state=state, session=session)
        await call(profile_handlers.cb_sex, callback=make_callback("prof:sex:m"), state=state, session=session)
        await session.refresh(user)
        assert user.middle_name is None and user.last_name == "Иванов"

    async def test_bad_birth(self, answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await state.set_state(profile_handlers.PatientProfile.birth_date)
        await call(profile_handlers.msg_birth, message=make_message("31.02.1990"), state=state, session=session)
        assert answer.call_args.args[0] == texts.PROFILE_BIRTH_BAD

    async def test_too_young(self, answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await walk(session, state, birth="01.01.2015")
        assert answer.call_args.args[0] == texts.PROFILE_TOO_YOUNG
        assert await state.get_state() is None
        await session.refresh(user)
        assert user.last_name is None

    async def test_stale_sex_button(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await state.set_state(profile_handlers.PatientProfile.birth_date)
        await call(profile_handlers.cb_sex, callback=make_callback("prof:sex:m"), state=state, session=session)
        assert edit.call_args.args[0] == texts.PROFILE_ASK_LAST
        await session.refresh(user)
        assert user.last_name is None

    async def test_cancel_shows_screen(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        state = make_state()
        await state.set_state(profile_handlers.PatientProfile.first_name)
        await call(profile_handlers.cb_profile, callback=make_callback("prof:show"), state=state, session=session)
        assert await state.get_state() is None
        assert edit.call_args.args[0] == texts.PROFILE_EMPTY


class TestDelete:
    async def test_delete(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        await fill(session, user)
        await profile_handlers.cb_delete(make_callback("prof:del"))
        assert edit.call_args.args[0] == texts.PROFILE_DELETE_CONFIRM
        await call(profile_handlers.cb_delete_ok, callback=make_callback("prof:del:ok"), session=session)
        await session.refresh(user)
        assert user.last_name is None and user.sex is None
        assert edit.call_args.args[0] == f"{texts.PROFILE_DELETED}\n\n{texts.PROFILE_EMPTY}"


class TestBeforeInvite:
    async def test_entry_asks_profile_then_choice(self, edit, answer, cb_answer, session: AsyncSession, user: User) -> None:
        await consent_service.give_doctor(session, user.id, None)
        state = make_state()
        await call(doctor_handlers.cb_doctor, callback=make_callback("set:doctor"), state=state, session=session)
        assert edit.call_args.args[0] == f"{texts.PROFILE_BEFORE_INVITE}\n\n{texts.PROFILE_ASK_LAST}"
        await walk(session, state)
        await call(profile_handlers.cb_sex, callback=make_callback("prof:sex:m"), state=state, session=session)
        assert edit.call_args.args[0] == f"{texts.PROFILE_SAVED}\n\n{texts.DOCTOR_NO_LINK}"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.doctor_choice_kb()

    async def test_email_step_requires_profile(self, edit, answer, cb_answer, session: AsyncSession, user: User) -> None:
        await consent_service.give_doctor(session, user.id, None)
        await call(doctor_handlers.cb_doctor_by_email, callback=make_callback("set:doctor:email"), state=make_state(), session=session)
        assert edit.call_args.args[0].startswith(texts.PROFILE_BEFORE_INVITE)
        await call(doctor_handlers.msg_doctor_email, message=make_message("d@example.org"), state=make_state(), session=session)
        assert answer.call_args.args[0].startswith(texts.PROFILE_BEFORE_INVITE)


class TestPromptAndName:
    async def _with_doctor(self, session: AsyncSession, user: User) -> None:
        user.onboarded = True
        await session.commit()
        doctor = Doctor(email="d@example.org", name="Врач", password_hash="x")
        session.add(doctor)
        await session.commit()
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()

    async def test_prompt_after_action_once(self, answer, session: AsyncSession, user: User) -> None:
        await self._with_doctor(session, user)
        handler = AsyncMock(return_value="done")
        data = {"session": session, "state": make_state()}
        assert await ConsentMiddleware()(handler, make_message("120/80"), data) == "done"
        handler.assert_awaited_once()
        assert answer.call_args.args[0] == texts.PROFILE_PROMPT
        answer.reset_mock()
        assert await ConsentMiddleware()(handler, make_message("120/80"), data) == "done"
        answer.assert_not_awaited()

    async def test_prompt_not_on_profile_buttons(self, answer, session: AsyncSession, user: User) -> None:
        await self._with_doctor(session, user)
        handler = AsyncMock(return_value="done")
        for data_cb in ("prof:edit", "set:profile"):
            await ConsentMiddleware()(handler, make_callback(data_cb), {"session": session, "state": make_state()})
        answer.assert_not_awaited()
        await session.refresh(user)
        assert user.profile_prompted_at is None

    async def test_prompt_on_callback_goes_to_chat(self, answer, session: AsyncSession, user: User) -> None:
        await self._with_doctor(session, user)
        handler = AsyncMock(return_value=None)
        callback = make_callback("menu:stats")
        await ConsentMiddleware()(handler, callback, {"session": session, "state": make_state()})
        assert answer.call_args.args[0] == texts.PROFILE_PROMPT

    async def test_prompt_callback_without_message(self, session: AsyncSession, user: User) -> None:
        await self._with_doctor(session, user)
        handler = AsyncMock(return_value=None)
        callback = make_callback("menu:stats").model_copy(update={"message": None})
        await ConsentMiddleware()(handler, callback, {"session": session, "state": make_state()})
        handler.assert_awaited_once()

    async def test_telegram_name_middleware(self, session: AsyncSession, user: User) -> None:
        handler = AsyncMock(return_value="ok")
        assert await TelegramNameMiddleware()(handler, make_message("x"), {"session": session}) == "ok"
        await session.refresh(user)
        assert user.telegram_name == "Тест"
        assert await TelegramNameMiddleware()(handler, make_message("x"), {}) == "ok"

    async def test_telegram_name_error_ignored(self, mocker, session: AsyncSession, user: User) -> None:
        from bot.services import profile as profile_service

        mocker.patch.object(profile_service, "touch_telegram_name", side_effect=RuntimeError("db"))
        handler = AsyncMock(return_value="ok")
        assert await TelegramNameMiddleware()(handler, make_message("x"), {"session": session}) == "ok"


class TestDoctorBotLabels:
    async def test_patients_sorted_with_labels(self, edit, cb_answer, session: AsyncSession, user: User) -> None:
        doctor = Doctor(telegram_id=DOC_TG, name="Врач", password_hash="x")
        session.add(doctor)
        other = User(telegram_id=222_222, timezone="Europe/Minsk", telegram_name="Анна Иванова")
        named = User(telegram_id=333_333, timezone="Europe/Minsk", last_name="Абрамов", first_name="Пётр",
                     birth_date=date(1980, 1, 1), sex="m")
        session.add_all([other, named])
        await session.commit()
        for patient in (user, other, named):
            session.add(DoctorPatient(doctor_id=doctor.id, user_id=patient.id, status="active"))
        await session.commit()
        await call(doctor_menu.cb_patients, callback=make_callback("doc:patients", user_id=DOC_TG), session=session)
        labels = [row[0].text for row in edit.call_args.kwargs["reply_markup"].inline_keyboard[:-1]]
        assert labels[0].startswith("Абрамов П., ")
        assert set(labels[1:]) == {"Пациент 111111", "Анна Иванова"}
