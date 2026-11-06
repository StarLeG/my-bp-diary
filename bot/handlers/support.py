"""Хендлеры «О боте / Поддержать» и команды /support, /cabinet (SPEC.md, §6.8–6.9).

Ссылки и контакты берутся только из конфига (`SUPPORT_URL`, `AUTHOR_CONTACT`):
реквизиты в коде бота не хранятся и не показываются. Кнопка скрывается, если
переменная пустая или содержит плейсхолдер. Команды `/support` и `/cabinet`
обрабатываются всегда и сбрасывают FSM, чтобы пользователь не застрял в режиме
свободного ввода (§6.9).

Личный кабинет (§16.2): «Личный кабинет» / `/cabinet` проверяют запись в
`web_users`; без аккаунта запускается FSM `CabinetRegistration.email` (ввод
e-mail, создание записи со случайным паролем — Argon2id), с аккаунтом сразу
отправляется одноразовая magic link.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from argon2 import PasswordHasher
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, get_settings
from bot.models import User, WebUser
from bot.services.measurements import get_or_create_user

#: Соль подписи magic link (§16.2: stateless-токен, без таблицы в БД).
CABINET_SALT = "cabinet-login"

#: Время жизни magic link, минут (§16.2).
CABINET_TOKEN_MAX_AGE = 15 * 60

router = Router(name="support")


def _about_screen(settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «О боте» с кнопками по видимости SUPPORT_URL / AUTHOR_CONTACT."""
    return texts.ABOUT_TEXT, keyboards.about_kb(
        support_visible=settings.support_button_visible,
        author_visible=settings.author_button_visible,
    )


# ─────────────────────────── Экраны ───────────────────────────


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    """/support: «Поддержать» при заданном SUPPORT_URL, иначе «О боте» (§6.9).

    Команда обрабатывается всегда и сбрасывает FSM.
    """
    await state.clear()
    settings = get_settings()
    if settings.support_button_visible:
        text, markup = texts.SUPPORT_TEXT, keyboards.support_kb(settings.support_url)
    else:
        text, markup = _about_screen(settings)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:about")
async def cb_about(callback: CallbackQuery) -> None:
    """«О боте / Поддержать» из главного меню (сообщение редактируется, §6.8)."""
    text, markup = _about_screen(get_settings())
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "about:support")
async def cb_about_support(callback: CallbackQuery) -> None:
    """«Поддержать проект»: текст + URL-кнопка (реквизитов в боте нет, §6.8)."""
    settings = get_settings()
    if not settings.support_button_visible:
        text, markup = _about_screen(settings)
    else:
        text, markup = texts.SUPPORT_TEXT, keyboards.support_kb(settings.support_url)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "about:author")
async def cb_about_author(callback: CallbackQuery) -> None:
    """«Написать автору»: показать контакт из конфига (§6.8)."""
    settings = get_settings()
    if not settings.author_button_visible:
        text, markup = _about_screen(settings)
    else:
        text, markup = settings.author_contact, keyboards.about_kb(
            support_visible=settings.support_button_visible,
            author_visible=settings.author_button_visible,
        )
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Личный кабинет ───────────────────────────


class CabinetRegistration(StatesGroup):
    """FSM создания личного кабинета (SPEC.md, раздел 8)."""

    email = State()


#: Проверка формата e-mail (§16.2: «с валидацией формата»).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Хеш пароля кабинета — только Argon2id (CLAUDE.md, «Безопасность и платежи»).
_password_hasher = PasswordHasher()


def make_cabinet_link(settings: Settings, telegram_id: int) -> str:
    """Подписать magic link для входа в кабинет (§16.2).

    Токен stateless (`itsdangerous.URLSafeTimedSerializer` + `WEB_SECRET_KEY`),
    живёт `CABINET_TOKEN_MAX_AGE` секунд; проверяет его веб-часть. Payload —
    как в `web/cabinet/auth.py::MagicLink` (`uid` + `jti` для одноразовости);
    совместимость закреплена контрактным тестом `tests/test_web_auth.py`.
    Base URL кабинета — из переменной окружения `CABINET_URL` (пустая =
    кабинет не развёрнут).
    """
    token = URLSafeTimedSerializer(settings.web_secret_key, salt=CABINET_SALT).dumps(
        {"uid": telegram_id, "jti": secrets.token_urlsafe(16)}
    )
    return f"{settings.cabinet_url.rstrip('/')}/login?token={token}"


async def _find_web_user(session: AsyncSession, telegram_id: int) -> Optional[WebUser]:
    """Аккаунт кабинета пользователя: `web_users` по `users.telegram_id` (§16.2)."""
    return (
        await session.execute(
            select(WebUser)
            .join(User, WebUser.user_id == User.id)
            .where(User.telegram_id == telegram_id)
        )
    ).scalar_one_or_none()


async def _create_web_account(session: AsyncSession, user: User, email: str) -> bool:
    """Создать запись `web_users` со случайным паролем (§16.2, п.1).

    Пароль генерируется случайным и хранится только как хеш Argon2id —
    пользователь входит по magic link, пароль ему не нужен. `False` — e-mail
    уже занят (UNIQUE) или аккаунт уже есть: гонка двух вводов отсекается
    самим ограничением UNIQUE (§7.2), вторая вставка получает
    `IntegrityError`.
    """
    session.add(
        WebUser(
            user_id=user.id,
            email=email,
            password_hash=_password_hasher.hash(secrets.token_urlsafe(18)),
        )
    )
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def _cabinet_entry(
    session: AsyncSession, state: FSMContext, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «Личный кабинет» / `/cabinet` (§6.2, §16.2).

    Три исхода: кабинет не развёрнут → «пока не развёрнут»; аккаунта нет →
    FSM `CabinetRegistration.email` (запрос e-mail); аккаунт есть →
    одноразовая ссылка.
    """
    settings = get_settings()
    if not settings.cabinet_button_visible:
        return texts.CABINET_DISABLED, keyboards.back_to_menu()
    if await _find_web_user(session, telegram_id) is None:
        await state.set_state(CabinetRegistration.email)
        return texts.CABINET_EMAIL_PROMPT, keyboards.input_cancel_kb("cabinet:cancel")
    return (
        texts.CABINET_LINK_SENT.format(url=make_cabinet_link(settings, telegram_id)),
        keyboards.back_to_menu(),
    )


@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/cabinet: ссылка для входа в личный кабинет (§6.9).

    Если аккаунта в `web_users` нет — запускает FSM создания ЛК (запрос
    e-mail); если есть — генерирует одноразовую ссылку (15 минут).
    Доступно, когда развёрнут веб-кабинет (заданы `WEB_SECRET_KEY` и
    `CABINET_URL`). Сбрасывает FSM, чтобы пользователь не застрял в режиме
    ввода.
    """
    await state.clear()
    text, markup = await _cabinet_entry(session, state, message.from_user.id)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW)


@router.callback_query(F.data == "menu:cabinet")
async def cb_cabinet(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Личный кабинет» из главного меню: ссылка или создание аккаунта (§6.2).

    Сообщение редактируется, чтобы не плодить визуальный шум (§6.2).
    """
    text, markup = await _cabinet_entry(session, state, callback.from_user.id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
    )
    await callback.answer()


@router.message(CabinetRegistration.email)
async def msg_cabinet_email(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """E-mail для создания ЛК: формат, занятость, создание аккаунта (§16.2).

    Ошибка формата или занятый e-mail — просьба ввести другой (состояние
    сохраняется). Успех — аккаунт создан, сразу отправляется magic link:
    «Аккаунт создан. Ваша ссылка для входа: …».
    """
    email = (message.text or "").strip().lower()
    markup = keyboards.input_cancel_kb("cabinet:cancel")
    if not _EMAIL_RE.match(email):
        await message.answer(texts.CABINET_EMAIL_BAD, reply_markup=markup)
        return
    user = await get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    if not await _create_web_account(session, user, email):
        await message.answer(texts.CABINET_EMAIL_TAKEN, reply_markup=markup)
        return
    await state.clear()
    link = make_cabinet_link(get_settings(), message.from_user.id)  # type: ignore[union-attr]
    await message.answer(
        texts.CABINET_ACCOUNT_CREATED.format(url=link),
        reply_markup=keyboards.back_to_menu(),
        link_preview_options=keyboards.NO_LINK_PREVIEW,
    )


@router.callback_query(F.data == "cabinet:cancel")
async def cb_cabinet_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» при вводе e-mail: сброс FSM, возврат в главное меню (§6.2)."""
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu()
    )
    await callback.answer(texts.CABINET_REG_CANCELLED)
