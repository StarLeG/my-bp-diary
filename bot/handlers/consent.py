"""Экраны согласия пациента (SPEC.md, §6.11).

- Б1 / Б2 — основное согласие: новому пользователю — первым шагом онбординга,
  существующему — при любом обращении, пока согласия нет (`ConsentMiddleware`).
- Б3 — «Не согласен»: новому — запись `users` удаляется, существующему —
  предложение удалить аккаунт (§19).
- Б6 — «Настройки» → «Согласие на обработку данных»: дата и отзыв; отзыв =
  удаление аккаунта (§19) с записью в журнал удалений.
- Д4 — связи с врачом без согласия на этого врача (созданные до 0.5.0):
  «Оставить доступ» / «Закрыть доступ» (`active`), «Оставить» / «Отменить
  приглашение» (`pending`). До ответа врач пациента не видит.

Согласие врача (Б8) — в `doctor_menu.py`, согласие на нового врача (Б7) — в
`doctor.py`.
"""
from __future__ import annotations

import logging
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import get_settings
from bot.models import Doctor, DoctorPatient, User
from bot.services import consent as consent_service
from bot.services import doctor as doctor_service
from bot.services import measurements as measurements_service
from bot.services.deletion_log import DeletionLog
from bot.services.reminders import RemindersService

router = Router(name="consent")

logger = logging.getLogger(__name__)


def consent_screen(existing: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Б1 (новый) / Б2 (существующий) с кнопками (§6.11)."""
    url = get_settings().privacy_url
    return texts.consent_text(url, existing), keyboards.consent_kb(url, existing)


def confirm_screen(link: DoctorPatient, doctor: Doctor) -> tuple[str, InlineKeyboardMarkup]:
    """Д4: подтверждение доступа (`active`) или приглашения (`pending`) врачу."""
    pending = link.status == "pending"
    template = texts.CONFIRM_DOCTOR_PENDING if pending else texts.CONFIRM_DOCTOR_ACTIVE
    return (
        template.format(doctor=doctor_service.doctor_label(doctor)),
        keyboards.confirm_doctor_kb(doctor.id, pending),
    )


async def next_confirm(
    session: AsyncSession, user_id: int
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Первый экран Д4 для связи без согласия (или None — подтверждать нечего)."""
    links = await consent_service.links_to_confirm(session, user_id)
    if not links:
        return None
    link, doctor = links[0]
    return confirm_screen(link, doctor)


async def _menu_screen(session: AsyncSession, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Главное меню пациента (врачу — с кнопкой «🩺 Кабинет врача»)."""
    is_doctor = await doctor_service.doctor_by_telegram(session, telegram_id) is not None
    return texts.MAIN_MENU, keyboards.main_menu(is_doctor=is_doctor)


async def _edit(callback: CallbackQuery, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
    )


# ─────────────────────────── Основное согласие ───────────────────────────


@router.callback_query(F.data == "cons:yes")
async def cb_consent_yes(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«✅ Согласен» (Б5): запись `consents`, дальше онбординг, Д4 или меню.

    Повторное и параллельное нажатие не создают вторую запись (§4.15).
    Пользователь, отозвавший согласие в кабинете (ждёт удаления), согласием
    снова его отменяет — он передумал.
    """
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if user.deletion_requested_at is not None:
        user.deletion_requested_at = None
        await session.commit()
    await consent_service.give_processing(session, user.id, consent_service.CHANNEL_BOT)
    if not user.onboarded:
        from bot.handlers.start import _welcome_screen

        welcome, markup = _welcome_screen()
        await _edit(callback, f"{texts.CONSENT_SAVED}\n\n{welcome}", markup)
    else:
        screen = await next_confirm(session, user.id)
        if screen is None:
            text, markup = await _menu_screen(session, callback.from_user.id)
            screen = (f"{texts.CONSENT_SAVED}\n\n{text}", markup)
        await _edit(callback, *screen)
    await callback.answer()


@router.callback_query(F.data == "cons:no")
async def cb_consent_no(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Не согласен» (Б3): новый — запись удаляется; существующий — удалить аккаунт?"""
    await state.clear()
    user = await measurements_service.get_user(session, callback.from_user.id)
    if user is not None and user.onboarded:
        await _edit(callback, texts.CONSENT_DECLINED_EXISTING, keyboards.consent_declined_kb())
    else:
        if user is not None:
            # Данных ещё нет ни в БД, ни в копиях — журнал удалений не нужен.
            await measurements_service.delete_user_data(session, reminders, user)
        await _edit(callback, texts.CONSENT_DECLINED_NEW, None)
    await callback.answer()


@router.callback_query(F.data == "cons:back")
async def cb_consent_back(callback: CallbackQuery) -> None:
    """«Назад к согласию»: снова Б2."""
    await _edit(callback, *consent_screen(existing=True))
    await callback.answer()


# ─────────────────────────── Д4: врачи до 0.5.0 ───────────────────────────


async def _own_link(
    callback: CallbackQuery, session: AsyncSession
) -> Optional[tuple[User, DoctorPatient, Doctor]]:
    """Связь пациента с врачом из callback'а (проверка владельца, §7.1)."""
    raw = (callback.data or "").rsplit(":", 1)[-1]
    user = await measurements_service.get_user(session, callback.from_user.id)
    if user is None or not raw.isdigit():
        return None
    link = await doctor_service.link_for_pair(session, int(raw), user.id)
    doctor = await session.get(Doctor, int(raw))
    if link is None or doctor is None or link.status == "revoked":
        return None
    return user, link, doctor


async def _after_confirm(callback: CallbackQuery, session: AsyncSession, user_id: int) -> None:
    """Следующий экран Д4 или главное меню."""
    screen = await next_confirm(session, user_id)
    if screen is None:
        screen = await _menu_screen(session, callback.from_user.id)
    await _edit(callback, *screen)


@router.callback_query(F.data.startswith("cons:doc:keep:"))
async def cb_keep_doctor(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Оставить доступ» / «Оставить»: согласие на этого врача (Д4)."""
    found = await _own_link(callback, session)
    if found is None:
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    user, _, doctor = found
    await consent_service.give_doctor(session, user.id, doctor.id)
    await _after_confirm(callback, session, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("cons:doc:close:"))
async def cb_close_doctor(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """«Закрыть доступ» / «Отменить приглашение» (Д4).

    `active` — как «Отозвать доступ»: `revoked`, врачу в Telegram —
    уведомление. `pending` — `revoked` и списание неиспользованного кода
    приглашения: врач в старом приглашении увидит «устарело».
    """
    found = await _own_link(callback, session)
    if found is None:
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    user, link, doctor = found
    was_active = link.status == "active"
    doctor_tg = doctor.telegram_id
    await doctor_service.revoke_access(session, user.id, doctor.id)
    await consent_service.withdraw_doctor(session, user.id, doctor.id)
    if not was_active:
        await consent_service.cancel_pending_invites(session, user.id)
    await callback.answer(texts.DOCTOR_REVOKED)
    if was_active and doctor_tg is not None:
        try:
            await bot.send_message(
                doctor_tg,
                texts.DOCTOR_ACCESS_REVOKED_FOR_DOCTOR.format(name=callback.from_user.first_name),
            )
        except TelegramAPIError as exc:
            logger.warning("Уведомление врачу не доставлено: %s", type(exc).__name__)
    await _after_confirm(callback, session, user.id)


# ─────────────────────────── Настройки: согласие (Б6) ───────────────────────────


@router.callback_query(F.data == "set:consent")
async def cb_consent_settings(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Согласие на обработку данных»: дата согласия и «Отозвать согласие» (Б6)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    record = await consent_service.current_processing(session, user.id)
    if record is None:  # pragma: no cover — без согласия сюда не пускает middleware
        await _edit(callback, *consent_screen(existing=True))
        await callback.answer()
        return
    local = record.given_at.astimezone(ZoneInfo(user.timezone))
    await _edit(
        callback,
        texts.CONSENT_SETTINGS.format(
            date=local.strftime("%d.%m.%Y"), privacy_url=get_settings().privacy_url
        ),
        keyboards.consent_settings_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "set:consent:withdraw")
async def cb_consent_withdraw(callback: CallbackQuery) -> None:
    """«Отозвать согласие»: подтверждение."""
    await _edit(callback, texts.CONSENT_WITHDRAW_CONFIRM, keyboards.consent_withdraw_kb())
    await callback.answer()


@router.callback_query(F.data == "set:consent:withdraw:ok")
async def cb_consent_withdraw_ok(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
    deletion_log: Optional[DeletionLog] = None,
) -> None:
    """«Да, отозвать и удалить»: удаление аккаунта (§19) + журнал удалений."""
    await state.clear()
    user = await measurements_service.get_user(session, callback.from_user.id)
    if user is not None:
        await measurements_service.delete_user_data(session, reminders, user, deletion_log)
    await _edit(callback, texts.CONSENT_WITHDRAWN, None)
    await callback.answer()
