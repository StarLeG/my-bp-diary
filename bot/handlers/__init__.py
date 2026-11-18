"""Сборка роутеров хендлеров (SPEC.md, §16.3).

Роутеры подключаются в порядке приоритета: конкретные флоу — первыми,
catch-all (свободный ввод вне FSM → «Выберите пункт меню») — последним.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy.exc import SQLAlchemyError

from bot import keyboards, texts
from bot.config import admin_ids_from_env
from bot.handlers import (
    admin,
    appointments,
    consent,
    diary,
    doctor,
    doctor_menu,
    events,
    measure,
    profile,
    reminders,
    settings,
    start,
    stats,
    support,
)

router = Router(name="root")
# Сторона врача — до start: `/start doc_<code>` (приглашение) не должен уйти
# в онбординг пациента (§21).
router.include_router(doctor_menu.router)
router.include_router(consent.router)
router.include_router(start.router)
router.include_router(measure.router)
router.include_router(events.router)
router.include_router(diary.router)
router.include_router(appointments.router)
router.include_router(stats.router)
router.include_router(settings.router)
router.include_router(profile.router)
router.include_router(doctor.router)
router.include_router(support.router)
router.include_router(reminders.router)
# Админ-панель подключается только при заданных ADMIN_TELEGRAM_IDS (§18).
# `admin_ids_from_env` вместо `get_settings`: на импорте не должно требоваться
# BOT_TOKEN.
if admin_ids_from_env():
    router.include_router(admin.router)

fallback_router = Router(name="fallback")


@fallback_router.message()
async def unknown_text(message: Message, state: FSMContext) -> None:
    """Свободный ввод вне режима ввода → мягкая подсказка (§7.3).

    В активном FSM-состоянии сообщение обрабатывает хендлер соответствующего
    модуля; сюда попадают только необработанные события — им молчим.
    Служебные сообщения (закреп канонического меню, §6.2) — не свободный ввод:
    им молчим тоже (находка 2026-11-18: каждый `/start` порождал постороннее
    «Выберите пункт меню»).
    """
    if await state.get_state() is not None:
        return
    if not message.text:
        return
    await message.answer(texts.MENU_HINT, reply_markup=keyboards.back_to_menu())


@fallback_router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    """Кнопка, которую не принял ни один обработчик: «Это сообщение устарело» (§8, U8).

    Только уведомление — без нового сообщения; «часики» на кнопке не висят.
    """
    await callback.answer(texts.STALE_MESSAGE)


logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    """Информация для пользователя при сбое (§12; 0.7.0, ST1-А).

    Системные сбои (БД/инфраструктура: `SQLAlchemyError` — включая «database is
    locked» после ретраев) → «⚙️ Бот временно недоступен…»; прочие ошибки —
    «Что-то пошло не так…». Пользователю — только текст без деталей, полная
    ошибка уходит в лог. Ответ: сообщением («В меню») или, от callback'а, —
    предупреждением (`show_alert`), без нового сообщения (§6.2); если алерт
    доставить нельзя (запрос устарел) — сообщением (2026-11-18). Регистрируется
    в `bot/main.py` (`dp.errors.register`).
    """
    logger.error("Ошибка обработки апдейта: %r", event.exception, exc_info=event.exception)
    text = (
        texts.ERROR_SYSTEMIC
        if isinstance(event.exception, SQLAlchemyError)
        else texts.ERROR_GENERIC
    )
    try:
        if event.update.message is not None:
            await event.update.message.answer(
                text, reply_markup=keyboards.back_to_menu()
            )
        elif event.update.callback_query is not None:
            try:
                await event.update.callback_query.answer(text, show_alert=True)
            except TelegramBadRequest:
                # Ответ на callback живёт ~30 с: алерт не доставить —
                # единственный способ донести сбой — сообщение (2026-11-18).
                message = event.update.callback_query.message
                if message is not None:
                    await message.answer(text, reply_markup=keyboards.back_to_menu())
    except Exception:  # noqa: BLE001 — ответ об ошибке не должен ронять диспетчер
        logger.warning("Не удалось ответить пользователю об ошибке", exc_info=True)
    return True  # ошибка обработана


router.include_router(fallback_router)
