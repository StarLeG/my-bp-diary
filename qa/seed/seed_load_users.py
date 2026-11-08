"""Подготовка учётных записей для нагрузочного теста кабинета (SPEC.md, §22).

Создаёт N пользователей с web-аккаунтами `load{n}@load.test`, паролями и
историей измерений за несколько недель, чтобы дашборд и CSV-экспорт под
нагрузкой работали с реалистичными данными. Скрипт идемпотентен: готовые
аккаунты не трогаются, недостающая история дозаполняется. Аккаунт и его
история создаются одной транзакцией — сбой посреди запуска не оставляет
«пустый» аккаунт, который повторный запуск пропустил бы молча.

ТОЛЬКО для тестового стенда: БД без «test-stand» в имени отклоняется
(`qa.seed.ensure_stand_db`, SPEC.md §22).

Запуск (из корня репозитория):

    LOAD_PASSWORD='<пароль стенда>' \\
        uv run python qa/seed/seed_load_users.py --db data/test-stand.sqlite3 --count 20

Пароль берётся из `LOAD_PASSWORD` или `--password`; в коде его нет
(CLAUDE.md, «Правила для Claude Code» §2).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select  # noqa: E402

from bot.db import create_engine, create_session_factory, init_db  # noqa: E402
from bot.models import WebUser  # noqa: E402
from bot.services import measurements as measurements_service  # noqa: E402
from qa.seed import ensure_stand_db, load_password_from_env, patient_email, patient_tg_id  # noqa: E402
from web.cabinet import auth as web_auth  # noqa: E402


async def seed(db_path: str, count: int, password: str, days: int) -> None:
    """Создать нагрузочные аккаунты и историю измерений (идемпотентно)."""
    ensure_stand_db(db_path)
    engine = create_engine(db_path)
    await init_db(engine)
    factory = create_session_factory(engine)
    created = 0
    backfilled = 0
    untouched = 0
    for n in range(1, count + 1):
        email = patient_email(n)
        async with factory() as session:
            user = await measurements_service.get_or_create_user(
                session, patient_tg_id(n)
            )
            existing = (
                await session.execute(select(WebUser).where(WebUser.email == email))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    WebUser(
                        user_id=user.id,
                        email=email,
                        password_hash=web_auth.hash_password(password),
                    )
                )
            # История: два измерения в день, значения детерминированные;
            # недостающее дозаполняется, дубли отсекает save_measurement.
            base = datetime.now(timezone.utc).replace(
                hour=7, minute=0, second=0, microsecond=0
            ) - timedelta(days=days)
            added = 0
            for day in range(days):
                for slot, hour in enumerate((7, 20)):
                    ts = base + timedelta(days=day, hours=hour - 7)
                    systolic = 118 + (n * 5 + day * 3 + slot * 7) % 32
                    diastolic = 72 + (n * 3 + day * 2 + slot * 5) % 18
                    try:
                        await measurements_service.save_measurement(
                            session,
                            user,
                            systolic,
                            diastolic,
                            pulse=60 + (n + day + slot) % 25,
                            timestamp=ts,
                        )
                    except measurements_service.DuplicateMeasurementError:
                        continue  # измерение уже есть — не дублируем
                    added += 1
            # Аккаунт и его история — одна транзакция: сбой откатывает всё.
            await session.commit()
            if existing is None:
                created += 1
                print(f"[seed] {email}: пользователь + {added} измерений")
            elif added:
                backfilled += 1
                print(f"[seed] {email}: дозаполнено {added} измерений")
            else:
                untouched += 1
    await engine.dispose()
    print(
        f"[seed] готово: создано {created}, дозаполнено {backfilled}, "
        f"без изменений {untouched}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/test-stand.sqlite3", help="путь к БД стенда")
    parser.add_argument("--count", type=int, default=20, help="число аккаунтов")
    parser.add_argument(
        "--password",
        default=load_password_from_env(),
        help="пароль аккаунтов (по умолчанию — LOAD_PASSWORD из окружения)",
    )
    parser.add_argument("--days", type=int, default=30, help="дней истории измерений")
    args = parser.parse_args()
    if not args.password:
        parser.error(
            "нет пароля: задайте LOAD_PASSWORD в окружении или --password "
            "(в коде пароля нет, CLAUDE.md)"
        )
    asyncio.run(seed(args.db, args.count, args.password, args.days))


if __name__ == "__main__":
    main()
