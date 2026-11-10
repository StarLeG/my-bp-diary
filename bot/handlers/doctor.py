"""Хендлеры «Мой врач» и «Отправить отчет» (SPEC.md, §21).

Пациент выбирает способ: «По e-mail» или «Через Telegram» (§21, п.2).

По e-mail (FSM `DoctorAccess.email`) бот создаёт
запись в `doctors` / `doctor_patients` (статус `pending`), отправляет врачу
письмо со ссылкой (если настроен SMTP, `services/mailer`) и отдаёт ту же
ссылку пациенту для пересылки. Ссылка доступа — 72 часа, одноразовая; её
открытие врачом активирует доступ (`status='active'`). Через Telegram — ссылка-приглашение в бота
(`?start=doc_<code>`, `doctor_invites`) и поиск врача по @username (только
среди врачей); принимает приглашение врач (`doctor_menu.py`). Пациент может
отозвать доступ в любой момент (`status='revoked'`); врачу в Telegram —
уведомление.

Ссылки и контакты берутся только из конфига (`CABINET_URL`, `WEB_SECRET_KEY`):
кабинет врача — часть веб-приложения, в коде бота его данных нет.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, get_settings
from bot.models import Doctor, DoctorPatient, User
from bot.services.doctor import (
    current_invite,
    doctor_by_username,
    doctor_label,
    find_or_create_doctor,
    link_patient,
    links_for_patient,
    normalize_username,
    revoke_access,
    valid_email,
)
from bot.services import consent as consent_service
from bot.services import mailer
from bot.services import profile as profile_service
from bot.services.measurements import get_or_create_user

#: Соль подписи ссылки доступа врача (§21). «doctor-access», а не прежняя
#: «doctor-login»: ссылки до 0.4.11 (15 минут, списание в памяти) не принимаются —
#: иначе продление срока до 72 часов «оживило» бы уже использованные.
DOCTOR_SALT = "doctor-access"

router = Router(name="doctor")

logger = logging.getLogger(__name__)

#: Канал приглашений по @username в `invite_log` (лимиты — как у писем, §21).
CHANNEL_TELEGRAM = "telegram"


class DoctorAccess(StatesGroup):
    """FSM добавления врача (SPEC.md, раздел 8)."""

    email = State()
    username = State()


def make_doctor_link(settings: Settings, doctor_id: int, patient_user_id: int) -> str:
    """Подписать magic link для врача (§21).

    Токен подписан `itsdangerous.URLSafeTimedSerializer` + `WEB_SECRET_KEY`;
    срок (72 часа, `DOCTOR_ACCESS_MAX_AGE`) и одноразовость проверяет
    веб-часть (`web/cabinet/doctor_auth.py::DoctorMagicLink`, `used_tokens`). Payload — `did` (врач),
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
    rows = [(doctor.id, doctor_label(doctor), link.status) for link, doctor in links]
    lines = [texts.DOCTOR_MENU, ""]
    lines.extend(
        texts.DOCTOR_LINK_ITEM.format(
            label=label, status=texts.DOCTOR_STATUS_LABELS[status]
        )
        for _, label, status in rows
    )
    return "\n".join(lines), keyboards.doctor_kb(rows)


async def _doctor_entry(
    session: AsyncSession, state: FSMContext, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «Мой врач» / `/doctor` (§21).

    Три исхода: кабинет не развёрнут → «пока не развёрнут»; открытого врача
    нет → выбор способа «По e-mail» / «Через Telegram»; врач добавлен →
    экран связей с отзывом доступа. Связи ищутся по `users.id` (внутренний
    ключ), а не по telegram_id.
    """
    await state.clear()
    settings = get_settings()
    if not settings.cabinet_button_visible:
        return texts.DOCTOR_WEB_DISABLED, keyboards.back_to_menu()
    user = await get_or_create_user(session, telegram_id)
    links = await links_for_patient(session, user.id)
    if not any(link.status != "revoked" for link, _ in links):
        if not await consent_service.has_invite_consent(session, user.id):
            # Б7: согласие на врача — перед выбором способа (§6.11, §21 п.2).
            return texts.DOCTOR_CONSENT_ASK, keyboards.doctor_consent_kb()
        if not profile_service.has_profile(user):
            # Данные пациента нужны врачу (§6.12, P2-Б): ввод, затем выбор способа.
            from bot.handlers.profile import start_input

            return await start_input(state, user, then_invite=True)
        return texts.DOCTOR_NO_LINK, keyboards.doctor_choice_kb()
    return _doctor_screen(links)


async def _needs_consent(
    callback_or_message, session: AsyncSession, state: FSMContext, user_id: int
) -> bool:
    """Нет согласия на нового врача (Б7) — показать Б7 вместо шага приглашения.

    Кнопки шагов приглашения могли остаться в старом сообщении: согласие
    проверяется на каждом шаге, а не только на входе в «Мой врач».
    """
    if await consent_service.has_invite_consent(session, user_id):
        user = await session.get(User, user_id)
        if user is None or profile_service.has_profile(user):
            return False
        # Без данных пациента приглашения нет (§6.12, P2-Б): сначала ввод.
        from bot.handlers.profile import start_input

        text, markup = await start_input(state, user, then_invite=True)
    else:
        await state.clear()
        text, markup = texts.DOCTOR_CONSENT_ASK, keyboards.doctor_consent_kb()
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback_or_message.answer()
    else:
        await callback_or_message.answer(text, reply_markup=markup)
    return True


async def _has_doctor(session: AsyncSession, user_id: int) -> bool:
    """Есть ли у пациента врач (`pending`/`active`): второго добавить нельзя (C1)."""
    links = await links_for_patient(session, user_id)
    return any(link.status != "revoked" for link, _ in links)


async def _invite_url(bot: Bot, code: str) -> str:
    """Ссылка-приглашение в бота: `https://t.me/<бот>?start=doc_<code>` (§4.13)."""
    me = await bot.me()
    return f"https://t.me/{me.username}?start=doc_{code}"


async def _notify(bot: Bot, telegram_id: int, text: str, markup=None) -> bool:
    """Сообщение второй стороне (врачу или пациенту); False — не доставлено.

    Человек мог заблокировать бота — это не ошибка сценария: пишем в лог без
    текста и id, пациент получает запасной путь (ссылку для пересылки).
    """
    try:
        await bot.send_message(telegram_id, text, reply_markup=markup)
    except TelegramAPIError as exc:
        logger.warning("Сообщение второй стороне не доставлено: %s", type(exc).__name__)
        return False
    return True


# ─────────────────────────── Экраны ───────────────────────────


@router.message(Command("doctor"))
async def cmd_doctor(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/doctor: экран «Мой врач» (§5: команда /doctor, управление доступом).

    Сбрасывает FSM, чтобы пользователь не застрял в режиме ввода; без
    открытого врача — выбор способа приглашения.
    """
    await state.clear()
    text, markup = await _doctor_entry(session, state, message.from_user.id)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "set:doctor")
async def cb_doctor(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Мой врач» из настроек: список врачей или выбор способа (§21, п.1–2).

    Сообщение редактируется, чтобы не плодить визуальный шум (§6.2).
    """
    text, markup = await _doctor_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Добавление врача ───────────────────────────


@router.callback_query(F.data == "set:doctor:agree")
async def cb_doctor_agree(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«✅ Согласен, пригласить» (Б7): согласие на врача — в момент нажатия.

    Врач ещё неизвестен (`doctor_id` NULL): по e-mail он появится сразу, по
    ссылке — при «Принять». Повторное нажатие использует ту же запись.
    """
    user = await get_or_create_user(session, callback.from_user.id)
    if not await _has_doctor(session, user.id):
        await consent_service.give_doctor(session, user.id, None)
    text, markup = await _doctor_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "set:doctor:email")
async def cb_doctor_by_email(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«📧 По e-mail»: ввод e-mail врача (§21, п.3); без почты — про ссылку."""
    user = await get_or_create_user(session, callback.from_user.id)
    if not await _has_doctor(session, user.id) and await _needs_consent(
        callback, session, state, user.id
    ):
        return
    if await _has_doctor(session, user.id):
        text, markup = await _doctor_entry(session, state, callback.from_user.id)
    else:
        await state.set_state(DoctorAccess.email)
        settings = get_settings()
        text = texts.DOCTOR_EMAIL_ASK if settings.smtp_configured else texts.DOCTOR_EMAIL_ASK_NO_SMTP
        markup = keyboards.input_cancel_kb("set:doctor:cancel")
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "set:doctor:tg")
async def cb_doctor_by_telegram(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    """«✈️ Через Telegram»: ссылка-приглашение и «Найти по @username» (§21, п.4)."""
    await state.clear()
    user = await get_or_create_user(session, callback.from_user.id)
    if await _has_doctor(session, user.id):
        text, markup = await _doctor_entry(session, state, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    if await _needs_consent(callback, session, state, user.id):
        return
    invite = await current_invite(session, user.id, callback.from_user.first_name)
    url = await _invite_url(bot, invite.code)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.DOCTOR_TG_INVITE.format(invite_url=url),
        reply_markup=keyboards.doctor_tg_invite_kb(),
        link_preview_options=keyboards.NO_LINK_PREVIEW,
    )
    await callback.answer()


@router.callback_query(F.data == "set:doctor:tg:find")
async def cb_doctor_find(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔎 Найти по @username»: ввод имени пользователя врача (§21, п.4)."""
    await state.set_state(DoctorAccess.username)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.DOCTOR_USERNAME_ASK, reply_markup=keyboards.input_cancel_kb("set:doctor:cancel")
    )
    await callback.answer()


@router.message(DoctorAccess.username)
async def msg_doctor_username(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    """@username врача: поиск только среди врачей, приглашение в Telegram (§21, п.4).

    Ответ «не найден» одинаков для «нет в боте» и «не врач» (T3). Лимит —
    3 приглашения в сутки на пациента и 1 в час одному врачу (`invite_log`,
    `channel='telegram'`); сверх лимита и при недоставке — ссылка-приглашение.
    """
    username = normalize_username(message.text)
    if username is None:
        await message.answer(
            texts.DOCTOR_USERNAME_BAD, reply_markup=keyboards.input_cancel_kb("set:doctor:cancel")
        )
        return
    await state.clear()
    tg_user = message.from_user
    user = await get_or_create_user(session, tg_user.id)  # type: ignore[union-attr]
    if await _needs_consent(message, session, state, user.id):
        return
    invite = await current_invite(session, user.id, tg_user.first_name)  # type: ignore[union-attr]
    url = await _invite_url(bot, invite.code)
    doctor = await doctor_by_username(session, username)
    code, patient_name = invite.code, invite.patient_name
    text = texts.DOCTOR_TG_NOT_FOUND.format(invite_url=url)
    if doctor is not None and doctor.telegram_id not in (None, tg_user.id):  # type: ignore[union-attr]
        doctor_tg = doctor.telegram_id
        await session.commit()
        log_id = await mailer.reserve(session, user.id, CHANNEL_TELEGRAM, username, "invite")
        if log_id is None:
            text = texts.DOCTOR_TG_LIMIT.format(invite_url=url)
        elif await _notify(
            bot,
            doctor_tg,  # type: ignore[arg-type]
            texts.DOCTOR_INVITE_FOR_DOCTOR.format(name=patient_name),
            keyboards.doctor_invite_kb(code),
        ):
            await mailer.mark_sent(session, log_id)
            text = texts.DOCTOR_TG_SENT
    await message.answer(
        text, reply_markup=keyboards.back_to_menu(), link_preview_options=keyboards.NO_LINK_PREVIEW
    )


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
    settings = get_settings()
    user = await get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    if await _needs_consent(message, session, state, user.id):
        return
    doctor, _ = await find_or_create_doctor(session, email)
    await link_patient(session, doctor.id, user.id)
    # Врач стал известен — согласие на приглашение привязывается к нему (Б7).
    await consent_service.attach_doctor(session, user.id, doctor.id)
    await session.commit()
    await state.clear()
    link = make_doctor_link(settings, doctor.id, user.id)
    name = message.from_user.first_name  # type: ignore[union-attr]
    result = await mailer.send_doctor_link(
        session, settings, user_id=user.id, email=email, name=name, url=link, kind="invite"
    )
    template = {
        mailer.SendResult.SENT: texts.DOCTOR_INVITE_SENT,
        mailer.SendResult.FAILED: texts.DOCTOR_INVITE_FAILED,
        mailer.SendResult.LIMITED: texts.DOCTOR_INVITE_LIMIT,
        mailer.SendResult.DISABLED: texts.DOCTOR_INVITE_CREATED,
    }[result]
    await message.answer(
        template.format(email=email, name=name, url=link),
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
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
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
    await consent_service.withdraw_doctor(session, user.id, doctor_id)
    doctor = await session.get(Doctor, doctor_id)
    doctor_tg = doctor.telegram_id if doctor is not None else None
    name = profile_service.notify_name(user, callback.from_user.first_name)
    await session.commit()
    await callback.answer(texts.DOCTOR_REVOKED)
    if doctor_tg is not None:
        await _notify(
            bot, doctor_tg, texts.DOCTOR_ACCESS_REVOKED_FOR_DOCTOR.format(name=name)
        )
    text, markup = await _doctor_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


# ─────────────────────────── Отправить отчет ───────────────────────────


async def _report_text(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    doctor: Doctor,
    callback: CallbackQuery,
    link: str,
    bot: Optional[Bot] = None,
) -> str:
    """Текст «Отправить отчет»: письмо или сообщение врачу + ссылка (§21, п.6).

    Врач в Telegram — уведомление с «Открыть в кабинете» (N6); врач по
    e-mail — письмо (если настроен SMTP). Не доставлено / без почты —
    прежнее сообщение для пересылки.
    """
    if doctor.telegram_id is not None and bot is not None:
        if not consent_service.doctor_ok(doctor):
            # Врач без своего согласия пациента не видит (§6.11): без уведомления.
            return texts.DOCTOR_REPORT_LINK.format(url=link)
        doctor_tg = doctor.telegram_id
        patient = await session.get(User, user_id)
        name = (
            profile_service.notify_name(patient, callback.from_user.first_name)
            if patient is not None
            else callback.from_user.first_name
        )
        await session.commit()  # транзакция закрыта до отправки
        delivered = await _notify(
            bot,
            doctor_tg,
            texts.DOCTOR_REPORT_FOR_DOCTOR.format(name=name),
            keyboards.doctor_open_kb(user_id),
        )
        if delivered:
            return texts.DOCTOR_REPORT_TELEGRAM.format(url=link)
        return texts.DOCTOR_REPORT_LINK.format(url=link)
    if not doctor.email:
        return texts.DOCTOR_REPORT_LINK.format(url=link)
    await session.commit()  # транзакция закрыта до сетевой отправки
    result = await mailer.send_doctor_link(
        session,
        settings,
        user_id=user_id,
        email=doctor.email,
        name=callback.from_user.first_name,
        url=link,
        kind="report",
    )
    if result is mailer.SendResult.SENT:
        return texts.DOCTOR_REPORT_EMAILED.format(email=doctor.email, url=link)
    if result is mailer.SendResult.DISABLED:
        return texts.DOCTOR_REPORT_LINK.format(url=link)
    return texts.DOCTOR_REPORT_EMAIL_FAILED.format(url=link)


@router.callback_query(F.data == "menu:report")
async def cb_send_report(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    """«Отправить отчет» из главного меню: ссылка для врача (§21, п.4–5).

    Сообщение дословно из §21, п.5: «Ссылка для вашего врача сформирована.
    Перешлите её ему: [ссылка]». Без связи с врачом — подсказка добавить;
    отзывавшаяся связь — предложение добавить врача заново.
    """
    text = await report_text(session, callback, bot)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboards.back_to_menu(), link_preview_options=keyboards.NO_LINK_PREVIEW
    )
    await callback.answer()


async def report_text(session: AsyncSession, callback: CallbackQuery, bot: Bot) -> str:
    """Текст «Отправить отчет» (§21): ссылка врачу и пациенту или подсказка.

    Общий для главного меню и кнопки «Отправить врачу» в уведомлении о
    визите (§6.13, 0.5.8).
    """
    settings = get_settings()
    if not settings.cabinet_button_visible:
        return texts.DOCTOR_WEB_DISABLED
    user = await get_or_create_user(session, callback.from_user.id)
    links = await links_for_patient(session, user.id)
    active = [(link, doctor) for link, doctor in links if link.status != "revoked"]
    if not active:
        return texts.DOCTOR_REPORT_REVOKED if links else texts.DOCTOR_REPORT_NO_LINK
    _, doctor = active[0]
    link = make_doctor_link(settings, doctor.id, user.id)
    return await _report_text(session, settings, user.id, doctor, callback, link, bot)
