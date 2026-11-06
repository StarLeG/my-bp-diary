"""Подготовка учётных записей для нагрузочного теста кабинета (SPEC.md, §22).

Создаёт N пользователей с web-аккаунтами `load{n}@load.test`, паролями и
историей измерений за несколько недель, чтобы дашборд и CSV-экспорт под
нагрузкой работали с реалистичными данными. Скрипт идемпотентен: уже
созданные аккаунты пропускаются.

ТОЛЬКО для тестового стенда: в прод-БД запускать нельзя.

Запуск (из корня репозитория):

    uv run python qa/seed_load_users.py --db data/test-stand.sqlite3 --count 20

Пароль по умолчанию совпадает с `LOAD_PASSWORD` в `qa/locustfile.py`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from bot.db import create_engine, create_session_factory, init_db  # noqa: E402
from bot.models import WebUser  # noqa: E402
from bot.services import measurements as measurements_service  # noqa: E402
from web.cabinet import auth as web_auth  # noqa: E402

#: Telegram ID нагрузочных пользователей (вне диапазона живых).
TG_ID_BASE = 990_000
DEFAULT_PASSWORD = "LoadTest!2026"


async def seed(db_path: str, count: int, password: str, days: int) -> None:
    """Создать нагрузочные аккаунты и историю измерений."""
    engine = create_engine(db_path)
    await init_db(engine)
    factory = create_session_factory(engine)
    created = 0
    skipped = 0
    for n in range(1, count + 1):
        email = f"load{n}@load.test"
        async with factory() as session:
            existing = (
                await session.execute(select(WebUser).where(WebUser.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            user = await measurements_service.get_or_create_user(
                session, TG_ID_BASE + n
            )
            session.add(
                WebUser(
                    user_id=user.id,
                    email=email,
                    password_hash=web_auth.hash_password(password),
                )
            )
            await session.commit()
            # История: два измерения в день, значения — детерминированные.
            base = datetime.now(timezone.utc).replace(
                hour=7, minute=0, second=0, microsecond=0
            ) - timedelta(days=days)
            for day in range(days):
                for slot, hour in enumerate((7, 20)):
                    ts = base + timedelta(days=day, hours=hour - 7)
                    systolic = 118 + (n * 5 + day * 3 + slot * 7) % 32
                    diastolic = 72 + (n * 3 + day * 2 + slot * 5) % 18
                    await measurements_service.save_measurement(
                        session,
                        user,
                        systolic,
                        diastolic,
                        pulse=60 + (n + day + slot) % 25,
                        timestamp=ts,
                    )
            await session.commit()
            created += 1
            print(f"[seed] {email}: пользователь + {days * 2} измерений")
    await engine.dispose()
    print(f"[seed] готово: создано {created}, пропущено {skipped} (уже были).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/test-stand.sqlite3", help="путь к БД стенда")
    parser.add_argument("--count", type=int, default=20, help="число аккаунтов")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="пароль аккаунтов")
    parser.add_argument("--days", type=int, default=30, help="дней истории измерений")
    args = parser.parse_args()
    asyncio.run(seed(args.db, args.count, args.password, args.days))


if __name__ == "__main__":
    main()
