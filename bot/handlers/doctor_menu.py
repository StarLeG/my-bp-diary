"""Сторона врача в боте (SPEC.md, §21, «Врач в боте»).

- `/start doc_<code>` — приглашение пациента: «✅ Принять» / «Отклонить».
  Онбординг пациента врач не проходит (строка `users` не создаётся).
  «Принять» — врач по `telegram_id` (новый — с именем из профиля Telegram)
  и связь `active`; «Отклонить» — связь не создаётся. Код списывается
  атомарно (`close_invite`), пациенту — уведомление.
- `/iamdoctor` — регистрация врача без приглашения (ФИО) или смена ФИО;
  зарегистрированного врача пациенты находят по @username.
- Меню врача: «🩺 Открыть кабинет врача» (ссылка входа, 15 минут), «👥 Мои
  пациенты» (только `active`) с «Открыть в кабинете», «Вести свой дневник
  давления» (онбординг пациента, если он не пройден).

Ссылка входа подписывается солью `doctor-signin` (`web/cabinet/doctor_auth.py::
DoctorSignInLink`) — не та, что у ссылки доступа к пациенту.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, get_settings
from bot.models import Doctor, User
from bot.services import consent as consent_service
from bot.services import doctor as doctor_service
from bot.services import profile as profile_service
from bot.services import measurements as measurements_service
from web.cabinet.doctor_auth import DoctorSignInLink

router = Router(name="doctor_menu")

logger = logging.getLogger(__name__)

#: Префикс параметра `/start` ссылки-приглашения (§4.13).
INVITE_PREFIX = "doc_"


class DoctorRegistration(StatesGroup):
    """FSM `/iamdoctor`: ввод ФИО врача (SPEC.md, §8)."""

    name = State()


def make_signin_link(
    settings: Settings, doctor_id: int, patient_user_id: Optional[int] = None
) -> str:
    """Ссылка входа врача в кабинет (15 минут, одноразовая, §21).

    С пациентом — сразу в его карточку («Открыть в кабинете»); доступ к ней
    кабинет всё равно проверяет по `status='active'`.
    """
    token = DoctorSignInLink(settings.web_secret_key).make(doctor_id, patient_user_id)
    return f"{settings.cabinet_url.rstrip('/')}/doctor?signin={token}"


async def _notify(bot: Bot, telegram_id: int, text: str) -> None:
    """Уведомление пациенту; заблокировал бота — не ошибка сценария (в лог без id)."""
    try:
        await bot.send_message(telegram_id, text)
    except TelegramAPIError as exc:
        logger.warning("Уведомление пациенту не доставлено: %s", type(exc).__name__)


async def _panel(session: AsyncSession, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Меню врача; «Вести свой дневник» — только если онбординг пациента не пройден."""
    user = await measurements_service.get_user(session, telegram_id)
    show_diary = user is None or not user.onboarded
    return texts.DOCTOR_PANEL, keyboards.doctor_panel_kb(show_diary)


def _self_consent_screen(code: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Б8: согласие врача на обработку его данных (§6.11)."""
    return (
        texts.DOCTOR_SELF_CONSENT_ASK.format(privacy_url=get_settings().privacy_url),
        keyboards.doctor_self_consent_kb(code),
    )


async def _require_doctor(
    callback: CallbackQuery, session: AsyncSession
) -> Optional[Doctor]:
    """Врач по Telegram-аккаунту с действующим согласием (§6.11).

    Не врач — главное меню вместо меню врача; врач без согласия — Б8.
    """
    doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id)
    if doctor is None:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.MAIN_MENU, reply_markup=keyboards.main_menu()
        )
        await callback.answer()
        return None
    if not consent_service.doctor_ok(doctor):
        text, markup = _self_consent_screen()
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        await callback.answer()
        return None
    return doctor


# ─────────────────────────── Приглашение ───────────────────────────


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(INVITE_PREFIX)))
async def cmd_start_invite(
    message: Message, command: CommandObject, state: FSMContext, session: AsyncSession
) -> None:
    """`/start doc_<code>`: приглашение пациента с «Принять» / «Отклонить» (§21, п.5).

    Недействительный код (просрочен, списан, не существует) и приглашение
    самому себе — «Приглашение устарело…».
    """
    await state.clear()
    tg_user = message.from_user
    await doctor_service.touch_username(session, tg_user.id, tg_user.username)  # type: ignore[union-attr]
    code = (command.args or "")[len(INVITE_PREFIX):]
    invite = await doctor_service.active_invite(session, code)
    patient = await session.get(User, invite.user_id) if invite is not None else None
    if invite is None or patient is None or patient.telegram_id == tg_user.id:  # type: ignore[union-attr]
        await message.answer(texts.DOCTOR_INVITE_INVALID)
        return
    await message.answer(
        texts.DOCTOR_INVITE_FOR_DOCTOR.format(name=invite.patient_name),
        reply_markup=keyboards.doctor_invite_kb(invite.code),
    )


@router.callback_query(F.data.startswith("doc:accept:"))
async def cb_accept(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    """«✅ Принять»: без согласия врача — сначала Б8, затем приём (§6.11, §21 п.5)."""
    code = (callback.data or "").split(":", 2)[2]
    doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id)
    if doctor is None or not consent_service.doctor_ok(doctor):
        invite = await doctor_service.active_invite(session, code)
        patient = await session.get(User, invite.user_id) if invite is not None else None
        if invite is None or patient is None or patient.telegram_id == callback.from_user.id:
            await callback.message.edit_text(texts.DOCTOR_INVITE_INVALID)  # type: ignore[union-attr]
            await callback.answer()
            return
        text, markup = _self_consent_screen(code)
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        await callback.answer()
        return
    await _accept(callback, session, bot, code)


async def _accept(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, code: str
) -> None:
    """Приём приглашения: врач и связь `active`, код списан, пациенту — уведомление.

    Списание атомарно: из двух нажатий проходит одно, второе — «устарело».
    Согласие пациента на приглашение (Б7) привязывается к этому врачу; если
    его нет (приглашение до 0.5.0), доступ откроется после подтверждения
    пациентом (Д4).
    """
    tg_user = callback.from_user
    invite = await doctor_service.active_invite(session, code)
    patient = await session.get(User, invite.user_id) if invite is not None else None
    if (
        invite is None
        or patient is None
        or patient.telegram_id == tg_user.id
        or await doctor_service.close_invite(session, code) is None
    ):
        await callback.message.edit_text(texts.DOCTOR_INVITE_INVALID)  # type: ignore[union-attr]
        await callback.answer()
        return
    patient_user_id, patient_tg = patient.id, patient.telegram_id
    doctor, _ = await doctor_service.get_or_create_telegram_doctor(
        session,
        tg_user.id,
        tg_user.username,
        doctor_service.full_name(tg_user.first_name, tg_user.last_name),
    )
    await doctor_service.grant_access(session, doctor.id, patient_user_id)
    await consent_service.attach_doctor(session, patient_user_id, doctor.id)
    if not consent_service.doctor_ok(doctor):
        await consent_service.give_doctor_self(session, doctor, consent_service.CHANNEL_BOT)
    label = doctor_service.doctor_label(doctor)
    _, markup = await _panel(session, tg_user.id)
    await session.commit()
    await callback.message.edit_text(texts.DOCTOR_INVITE_ACCEPTED, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
    await _notify(bot, patient_tg, texts.DOCTOR_TG_ACCEPTED.format(doctor=label))


@router.callback_query(F.data.startswith("doc:decline:"))
async def cb_decline(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    """«Отклонить»: код списан, связь не создаётся, пациенту — уведомление (§21, N3)."""
    code = (callback.data or "").split(":", 2)[2]
    invite = await doctor_service.close_invite(session, code)
    if invite is None:
        await callback.message.edit_text(texts.DOCTOR_INVITE_INVALID)  # type: ignore[union-attr]
        await callback.answer()
        return
    patient = await session.get(User, invite.user_id)
    if patient is not None:
        # Согласие пациента на приглашение без врача закрывается (§6.11, Б7).
        await consent_service.withdraw_doctor(session, patient.id, None)
    await callback.message.edit_text(texts.DOCTOR_INVITE_DECLINED)  # type: ignore[union-attr]
    await callback.answer()
    if patient is not None and patient.telegram_id != callback.from_user.id:
        await _notify(bot, patient.telegram_id, texts.DOCTOR_TG_DECLINED)


# ─────────────────────────── Согласие врача (Б8) ───────────────────────────


@router.callback_query(F.data.startswith("doc:consent:yes"))
async def cb_self_consent_yes(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    """«✅ Согласен» (Б8): при приглашении — сразу «Принять», иначе меню врача.

    Новый врач без записи `doctors` (`/iamdoctor`): согласие запоминается в
    FSM и сохраняется вместе с ФИО.
    """
    parts = (callback.data or "").split(":", 3)
    code = parts[3] if len(parts) == 4 else None
    if code is not None:
        if await doctor_service.active_invite(session, code) is None:
            await callback.message.edit_text(texts.DOCTOR_INVITE_INVALID)  # type: ignore[union-attr]
            await callback.answer()
            return
        doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id)
        if doctor is not None:
            await consent_service.give_doctor_self(session, doctor, consent_service.CHANNEL_BOT)
        await _accept(callback, session, bot, code)
        return
    doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id)
    if doctor is None:
        await state.set_state(DoctorRegistration.name)
        await state.update_data(doctor_consent=True)
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.DOCTOR_REGISTER_ASK, reply_markup=keyboards.input_cancel_kb("menu:back")
        )
        await callback.answer()
        return
    await consent_service.give_doctor_self(session, doctor, consent_service.CHANNEL_BOT)
    text, markup = await _panel(session, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("doc:consent:no"))
async def cb_self_consent_no(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Отмена» (Б8): при приглашении — снова «Принять» / «Отклонить», иначе меню."""
    parts = (callback.data or "").split(":", 3)
    code = parts[3] if len(parts) == 4 else None
    invite = await doctor_service.active_invite(session, code) if code else None
    if invite is not None:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.DOCTOR_INVITE_FOR_DOCTOR.format(name=invite.patient_name),
            reply_markup=keyboards.doctor_invite_kb(invite.code),
        )
    else:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.MAIN_MENU, reply_markup=keyboards.main_menu()
        )
    await callback.answer()


# ─────────────────────────── /iamdoctor ───────────────────────────


@router.message(Command("iamdoctor"))
async def cmd_iamdoctor(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """`/iamdoctor`: регистрация врача (ФИО) или «Изменить ФИО?» (§21, T4)."""
    await state.clear()
    tg_user = message.from_user
    await doctor_service.touch_username(session, tg_user.id, tg_user.username)  # type: ignore[union-attr]
    doctor = await doctor_service.doctor_by_telegram(session, tg_user.id)  # type: ignore[union-attr]
    if doctor is None or not consent_service.doctor_ok(doctor):
        # Б8: согласие врача — до регистрации и до любых действий врача (§6.11).
        text, markup = _self_consent_screen()
        await message.answer(
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        return
    await message.answer(
        texts.DOCTOR_REGISTERED.format(name=doctor.name),
        reply_markup=keyboards.doctor_registered_kb(),
    )


@router.callback_query(F.data == "doc:name")
async def cb_change_name(callback: CallbackQuery, state: FSMContext) -> None:
    """«Изменить»: ввод нового ФИО."""
    await state.set_state(DoctorRegistration.name)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.DOCTOR_REGISTER_ASK, reply_markup=keyboards.input_cancel_kb("menu:back")
    )
    await callback.answer()


@router.message(DoctorRegistration.name)
async def msg_doctor_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """ФИО врача: 2–100 символов → врач зарегистрирован / ФИО обновлено (§21)."""
    name = " ".join((message.text or "").split())
    if not doctor_service.DOCTOR_NAME_MIN <= len(name) <= doctor_service.DOCTOR_NAME_MAX:
        await message.answer(texts.DOCTOR_NAME_BAD, reply_markup=keyboards.input_cancel_kb("menu:back"))
        return
    consent_given = bool((await state.get_data()).get("doctor_consent"))
    await state.clear()
    tg_user = message.from_user
    doctor = await doctor_service.register_doctor(session, tg_user.id, tg_user.username, name)  # type: ignore[union-attr]
    if consent_given and not consent_service.doctor_ok(doctor):
        await consent_service.give_doctor_self(session, doctor, consent_service.CHANNEL_BOT)
    username = (tg_user.username or "").lower()  # type: ignore[union-attr]
    text = (
        texts.DOCTOR_NAME_SAVED.format(username=username)
        if username
        else texts.DOCTOR_NAME_SAVED_NO_USERNAME
    )
    _, markup = await _panel(session, tg_user.id)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=markup)


# ─────────────────────────── Меню врача ───────────────────────────


@router.callback_query(F.data == "doc:menu")
async def cb_panel(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Меню врача» (кнопка главного меню «🩺 Кабинет врача», «Назад»)."""
    await state.clear()
    if await _require_doctor(callback, session) is None:
        return
    text, markup = await _panel(session, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "doc:cabinet")
async def cb_cabinet(callback: CallbackQuery, session: AsyncSession) -> None:
    """«🩺 Открыть кабинет врача»: ссылка входа на 15 минут (§21)."""
    doctor = await _require_doctor(callback, session)
    if doctor is None:
        return
    settings = get_settings()
    if not settings.cabinet_button_visible:
        text = texts.DOCTOR_WEB_DISABLED
    else:
        text = texts.DOCTOR_SIGNIN_LINK.format(url=make_signin_link(settings, doctor.id))
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboards.doctor_back_kb(), link_preview_options=keyboards.NO_LINK_PREVIEW
    )
    await callback.answer()


@router.callback_query(F.data == "doc:patients")
async def cb_patients(callback: CallbackQuery, session: AsyncSession) -> None:
    """«👥 Мои пациенты»: только пациенты с `status='active'` (§21)."""
    doctor = await _require_doctor(callback, session)
    if doctor is None:
        return
    rows = profile_service.sorted_patients(await consent_service.visible_patients(session, doctor))
    patients = [(user.id, profile_service.bot_label(user)) for _, user in rows]
    text = texts.DOCTOR_PATIENTS if patients else texts.DOCTOR_PATIENTS_EMPTY
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboards.doctor_patients_kb(patients)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("doc:open:"))
async def cb_open_patient(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Открыть в кабинете»: ссылка входа сразу в карточку пациента (§21).

    Пациент из callback_data проверяется по связи `active` этого врача —
    чужой или отозвавший доступ выглядит как отсутствующий (§7.1). Ссылка —
    новым сообщением: уведомление об отчёте остаётся в чате.
    """
    raw = (callback.data or "").split(":", 2)[2]
    doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id)
    if doctor is not None and not consent_service.doctor_ok(doctor):
        text, markup = _self_consent_screen()
        await callback.message.answer(  # type: ignore[union-attr]
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        await callback.answer()
        return
    visible = (
        doctor is not None
        and raw.isdigit()
        and await consent_service.doctor_can_see(session, doctor, int(raw))
    )
    link = (
        await doctor_service.link_for_pair(session, doctor.id, int(raw))  # type: ignore[union-attr]
        if visible
        else None
    )
    if doctor is None or link is None:
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    settings = get_settings()
    if not settings.cabinet_button_visible:
        await callback.answer(texts.DOCTOR_WEB_DISABLED)
        return
    url = make_signin_link(settings, doctor.id, link.user_id)
    await callback.message.answer(  # type: ignore[union-attr]
        texts.DOCTOR_SIGNIN_LINK.format(url=url),
        reply_markup=keyboards.doctor_back_kb(),
        link_preview_options=keyboards.NO_LINK_PREVIEW,
    )
    await callback.answer()


@router.callback_query(F.data == "doc:diary")
async def cb_diary(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Вести свой дневник давления»: онбординг пациента (§21, N5)."""
    from bot.handlers.start import _welcome_screen

    from bot.handlers.consent import consent_screen

    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await session.commit()
    if not await consent_service.has_processing(session, user.id):
        text, markup = consent_screen(existing=user.onboarded)  # Б1 (§6.11)
    elif user.onboarded:
        text, markup = texts.MAIN_MENU, keyboards.main_menu(is_doctor=True)
    else:
        text, markup = _welcome_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
