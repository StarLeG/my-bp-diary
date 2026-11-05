"""Хендлеры «О боте / Поддержать» и команды /support, /cabinet (SPEC.md, §6.8–6.9).

Ссылки и контакты берутся только из конфига (`SUPPORT_URL`, `AUTHOR_CONTACT`):
реквизиты в коде бота не хранятся и не показываются. Кнопка скрывается, если
переменная пустая или содержит плейсхолдер. Команды `/support` и `/cabinet`
обрабатываются всегда и сбрасывают FSM, чтобы пользователь не застрял в режиме
свободного ввода (§6.9).
"""
from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from itsdangerous import URLSafeTimedSerializer

from bot import keyboards, texts
from bot.config import Settings, get_settings

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


def _cabinet_screen(settings: Settings, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран личного кабинета: одноразовая ссылка или «не развёрнут» (§16.2)."""
    if settings.cabinet_button_visible:
        text = texts.CABINET_LINK_SENT.format(
            url=make_cabinet_link(settings, telegram_id)
        )
    else:
        text = texts.CABINET_DISABLED
    return text, keyboards.back_to_menu()


@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message, state: FSMContext) -> None:
    """/cabinet: одноразовая ссылка для входа в личный кабинет (§6.9).

    Доступно, когда развёрнут веб-кабинет (заданы `WEB_SECRET_KEY` и
    `CABINET_URL`), иначе мягко сообщает, что кабинет не развёрнут.
    Сбрасывает FSM, чтобы пользователь не застрял в режиме ввода.
    """
    await state.clear()
    text, markup = _cabinet_screen(get_settings(), message.from_user.id)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:cabinet")
async def cb_cabinet(callback: CallbackQuery) -> None:
    """«Личный кабинет» из главного меню: одноразовая ссылка (§16.2).

    Сообщение редактируется, чтобы не плодить визуальный шум (§6.2).
    """
    text, markup = _cabinet_screen(get_settings(), callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
