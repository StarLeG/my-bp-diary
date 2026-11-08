"""Сид администратора стенда (SPEC.md, §22, §18).

Администратор — не строка БД, а Telegram ID из `ADMIN_TELEGRAM_IDS`. Сид
создаёт для каждого такого ID строку `users` (онбординг пройден), чтобы
админ-панель и рассылка на стенде видели живого получателя, и печатает
подсказку, какую строку держать в `.env` стенда.

Админ не входит в нагрузочный диапазон TG ID: `--clean` его не удаляет.
Идемпотентно (`get_or_create_user`). Запуск — через `qa/seed/seed_all.py`.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import parse_admin_ids
from bot.services import measurements as measurements_service

#: Подсказка, если ID не задан: админ-панель на стенде отключена (§18).
ADMIN_HINT = (
    "ADMIN_TELEGRAM_IDS не задан: добавьте в .env стенда строку "
    "ADMIN_TELEGRAM_IDS=<ваш Telegram ID> (несколько — через запятую)."
)


async def seed_admin(session: AsyncSession, raw_ids: str) -> dict[str, object]:
    """Создать строки `users` для ID администраторов из `raw_ids`.

    `raw_ids` — значение `ADMIN_TELEGRAM_IDS`. Возвращает
    `{"admins": [...], "hint": …}`; коммит — у вызывающего кода.
    """
    admin_ids = parse_admin_ids(raw_ids)
    for admin_id in admin_ids:
        user = await measurements_service.get_or_create_user(session, admin_id)
        user.onboarded = True
    await session.flush()
    hint = (
        f"В .env стенда: ADMIN_TELEGRAM_IDS={','.join(map(str, admin_ids))}"
        if admin_ids
        else ADMIN_HINT
    )
    return {"admins": list(admin_ids), "hint": hint}
