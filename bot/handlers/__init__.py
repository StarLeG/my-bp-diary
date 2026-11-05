"""Сборка роутеров хендлеров (SPEC.md, §16.3).

Роутеры подключаются в порядке приоритета: конкретные флоу — первыми,
catch-all (свободный ввод вне FSM → «Выберите пункт меню») — последним.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import keyboards, texts
from bot.handlers import diary, measure, reminders, settings, start, stats, support

router = Router(name="root")
router.include_router(start.router)
router.include_router(measure.router)
router.include_router(diary.router)
router.include_router(stats.router)
router.include_router(settings.router)
router.include_router(support.router)
router.include_router(reminders.router)

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


router.include_router(fallback_router)
