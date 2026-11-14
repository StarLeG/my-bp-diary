"""Сборка роутеров хендлеров (SPEC.md, §16.3).

Роутеры подключаются в порядке приоритета: конкретные флоу — первыми,
catch-all (свободный ввод вне FSM → «Выберите пункт меню») — последним.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

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
    """
    if await state.get_state() is not None:
        return
    await message.answer(texts.MENU_HINT, reply_markup=keyboards.back_to_menu())


@fallback_router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    """Кнопка, которую не принял ни один обработчик: «Это сообщение устарело» (§8, U8).

    Только уведомление — без нового сообщения; «часики» на кнопке не висят.
    """
    await callback.answer(texts.STALE_MESSAGE)


router.include_router(fallback_router)
