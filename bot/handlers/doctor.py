"""Хендлеры «Мой врач» и «Отправить отчет» (SPEC.md, §21).

Пациент добавляет врача по e-mail (FSM `DoctorAccess.email`), бот создаёт
запись в `doctors` / `doctor_patients` (статус `pending`) и отдаёт ссылку,
которую пациент пересылает врачу. Открытие ссылки врачом активирует доступ
(`status='active'`). Пациент может отозвать доступ в любой момент
(`status='revoked'`).

Ссылки и контакты берутся только из конфига (`CABINET_URL`, `WEB_SECRET_KEY`):
кабинет врача — часть веб-приложения, в коде бота его данных нет.
"""
from __future__ import annotations

import secrets
from typing import Sequence

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, get_settings
from bot.models import Doctor, DoctorPatient
from bot.services.doctor import (
    find_or_create_doctor,
    link_patient,
    links_for_patient,
    revoke_access,
    valid_email,
)
from bot.services.measurements import get_or_create_user

#: Соль подписи ссылки для врача (как `CABINET_SALT` в support.py, §21).
DOCTOR_SALT = "doctor-login"

#: Время жизни ссылки для врача, минут (§21: magic link, 15 минут).
DOCTOR_TOKEN_MAX_AGE = 15 * 60

router = Router(name="doctor")


class DoctorAccess(StatesGroup):
    """FSM добавления врача (SPEC.md, раздел 8)."""

    email = State()


def make_doctor_link(settings: Settings, doctor_id: int, patient_user_id: int) -> str:
    """Подписать magic link для врача (§21).

    Токен stateless (`itsdangerous.URLSafeTimedSerializer` + `WEB_SECRET_KEY`),
    живёт `DOCTOR_TOKEN_MAX_AGE` секунд; проверяет его веб-часть
    (`web/cabinet/doctor_auth.py::DoctorMagicLink`). Payload — `did` (врач),
    `pid` (пациент, `users.id`) и `jti` для одноразовости. Base URL кабинета —
    из переменной окружения `CABINET_URL` (пустая = кабинет не развёрнут).
    """
    token = URLSafeTimedSerializer(settings.web_secret_key, salt=DOCTOR_SALT).dumps(
        {"did": doctor_id, "pid": patient_user_id, "jti": secrets.token_urlsafe(16)}
    )
    return f"{settings.cabinet_url.rstrip('/')}/doctor?token={token}"


def _doctor_screen(
    links: Sequence[tuple[DoctorPatient, Doctor]],
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «Мой врач»: список связей и кнопки отзыва (§21)."""
    rows = [(doctor.id, doctor.email, link.status) for link, doctor in links]
    lines = [texts.DOCTOR_MENU, ""]
    lines.extend(
        texts.DOCTOR_LINK_ITEM.format(
            email=email, status=texts.DOCTOR_STATUS_LABELS[status]
        )
        for _, email, status in rows
    )
    return "\n".join(lines), keyboards.doctor_kb(rows)


async def _doctor_entry(
    session: AsyncSession, state: FSMContext, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «Мой врач» / `/doctor` (§21).

    Два исхода: кабинет не развёрнут → «пока не развёрнут»; открытого врача
    нет → FSM `DoctorAccess.email` (запрос e-mail, минимум трения); врач
    добавлен → экран связей с отзывом доступа. Связи ищутся по `users.id`
    (внутренний ключ), а не по telegram_id.
    """
    settings = get_settings()
    if not settings.cabinet_button_visible:
        return texts.DOCTOR_WEB_DISABLED, keyboards.back_to_menu()
    user = await get_or_create_user(session, telegram_id)
    links = await links_for_patient(session, user.id)
    if not any(link.status != "revoked" for link, _ in links):
        await state.set_state(DoctorAccess.email)
        return texts.DOCTOR_NO_LINK, keyboards.input_cancel_kb("set:doctor:cancel")
    return _doctor_screen(links)


# ─────────────────────────── Экраны ───────────────────────────


@router.message(Command("doctor"))
async def cmd_doctor(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/doctor: экран «Мой врач» (§5: команда /doctor, управление доступом).

    Сбрасывает FSM, чтобы пользователь не застрял в режиме ввода; без
    открытого врача сразу просит e-mail.
    """
    await state.clear()
    text, markup = await _doctor_entry(session, state, message.from_user.id)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "set:doctor")
async def cb_doctor(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Мой врач» из настроек: список врачей или запрос e-mail (§21, п.1).

    Сообщение редактируется, чтобы не плодить визуальный шум (§6.2).
    """
    text, markup = await _doctor_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Добавление врача ───────────────────────────


@router.message(DoctorAccess.email)
async def msg_doctor_email(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """E-mail врача: формат, создание врача и связи `pending` (§21, п.2–3).

    Ошибка формата — просьба ввести другой (состояние сохраняется). Успех —
    врач и связь созданы, пациенту отправляется сообщение для пересылки
    врачу (§21, п.3).
    """
    email = (message.text or "").strip().lower()
    markup = keyboards.input_cancel_kb("set:doctor:cancel")
    if not valid_email(email):
        await message.answer(texts.DOCTOR_EMAIL_BAD, reply_markup=markup)
        return
    user = await get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    doctor, _ = await find_or_create_doctor(session, email)
    await link_patient(session, doctor.id, user.id)
    await state.clear()
    link = make_doctor_link(get_settings(), doctor.id, user.id)
    await message.answer(
        texts.DOCTOR_INVITE_CREATED.format(
            name=message.from_user.first_name, url=link  # type: ignore[union-attr]
        ),
        reply_markup=keyboards.back_to_menu(),
        link_preview_options=keyboards.NO_LINK_PREVIEW,
    )


@router.callback_query(F.data == "set:doctor:cancel")
async def cb_doctor_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» при вводе e-mail врача: сброс FSM, возврат в главное меню (§21)."""
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu()
    )
    await callback.answer(texts.DOCTOR_ADD_CANCELLED)


# ─────────────────────────── Отзыв доступа ───────────────────────────


@router.callback_query(F.data.startswith("set:doctor:revoke:"))
async def cb_doctor_revoke(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Отозвать доступ»: связь переводится в `revoked` (§21, п. «Безопасность»).

    `doctor_id` из callback_data проверяется на владельца: отзывается только
    связь самого пользователя (CLAUDE.md, «Бот многопользовательский»).
    """
    try:
        doctor_id = int(callback.data.rsplit(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    user = await get_or_create_user(session, callback.from_user.id)
    if not await revoke_access(session, user_id=user.id, doctor_id=doctor_id):
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    await callback.answer(texts.DOCTOR_REVOKED)
    text, markup = await _doctor_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


# ─────────────────────────── Отправить отчет ───────────────────────────


@router.callback_query(F.data == "menu:report")
async def cb_send_report(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Отправить отчет» из главного меню: ссылка для врача (§21, п.4–5).

    Сообщение дословно из §21, п.5: «Ссылка для вашего врача сформирована.
    Перешлите её ему: [ссылка]». Без связи с врачом — подсказка добавить;
    отзывавшаяся связь — предложение добавить врача заново.
    """
    settings = get_settings()
    if not settings.cabinet_button_visible:
        text, markup = texts.DOCTOR_WEB_DISABLED, keyboards.back_to_menu()
    else:
        user = await get_or_create_user(session, callback.from_user.id)
        links = await links_for_patient(session, user.id)
        active = [(link, doctor) for link, doctor in links if link.status != "revoked"]
        if not active:
            text = (
                texts.DOCTOR_REPORT_REVOKED if links else texts.DOCTOR_REPORT_NO_LINK
            )
            markup = keyboards.back_to_menu()
        else:
            _, doctor = active[0]
            link = make_doctor_link(settings, doctor.id, user.id)
            text = texts.DOCTOR_REPORT_LINK.format(url=link)
            markup = keyboards.back_to_menu()
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
    )
    await callback.answer()
