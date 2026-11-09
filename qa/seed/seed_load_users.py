"""Сид нагрузочных пациентов и их учётных записей кабинета (SPEC.md, §22).

Пациент n (с 1): `users.telegram_id = 990 000 + n`, онбординг пройден, цель
130/80, часовой пояс по кругу из `PATIENT_TIMEZONES`; `web_users` —
`load{n}@load.test` с Argon2id-хешем пароля из `LOAD_PASSWORD`.

Идемпотентно: существующие пациенты и учётные записи не меняются (в том
числе пароль — для новой версии пароля нужен `seed_all.py --clean`).
Историю измерений создаёт `seed_5000_measurements.py`. Запуск — через
`qa/seed/seed_all.py`.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import User, WebUser
from qa.seed.consents import ensure_patient_consent
from qa.seed import Scale, patient_email, patient_tg_id, patient_timezone


async def seed_users(session: AsyncSession, scale: Scale, password_hash: str) -> dict[str, int]:
    """Создать недостающих пациентов профиля и их `web_users`.

    `password_hash` — готовый Argon2id-хеш (`web.cabinet.auth.hash_password`):
    соль хранится в самом хеше, одна строка годится всем аккаунтам с одним
    паролем. Возвращает счётчики `{"created": …, "existing": …}`; коммит —
    у вызывающего кода.
    """
    created = existing = 0
    for n in range(1, scale.patients + 1):
        tg_id = patient_tg_id(n)
        user = (
            await session.execute(select(User).where(User.telegram_id == tg_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=tg_id,
                timezone=patient_timezone(n),
                target_systolic=130,
                target_diastolic=80,
                onboarded=True,
            )
            session.add(user)
            await session.flush()
        await ensure_patient_consent(session, user.id)
        account = (
            await session.execute(select(WebUser).where(WebUser.user_id == user.id))
        ).scalar_one_or_none()
        if account is None:
            session.add(
                WebUser(user_id=user.id, email=patient_email(n), password_hash=password_hash)
            )
            created += 1
        else:
            existing += 1
    await session.flush()
    return {"created": created, "existing": existing}
