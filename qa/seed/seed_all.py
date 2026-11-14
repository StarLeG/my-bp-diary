"""Оркестратор сида нагрузочного стенда (SPEC.md, §22, «Сид данных»).

Порядок: [clean] → users → measurements → medications → appointments →
reminders → doctors → admin. Каждый шаг — своя короткая транзакция. По
умолчанию идемпотентное дозаполнение: повторный запуск ничего не дублирует.
`--clean` сначала удаляет только нагрузочные данные: пациентов из диапазона
TG ID (остальное — каскадом) и врачей `*@load.test` (связи и заметки —
каскадом); job'ы их напоминаний снимаются из таблицы APScheduler до удаления
строк (CLAUDE.md).

Профили (`--scale`): `full`, `smoke` и `three-years` — полный объём
авто-приёмки (~50 000 измерений за 3 года; `doc/LOAD-CRITERIA-QUESTIONS.md`,
В1/В2).

ТОЛЬКО для тестового стенда: БД без «test-stand» в имени отклоняется.

Запуск (из корня репозитория):

    LOAD_PASSWORD='<пароль стенда>' \\
        uv run python qa/seed/seed_all.py --db data/test-stand.sqlite3 \\
        [--scale smoke|full|three-years] [--clean]

Пароль — только из `LOAD_PASSWORD` (или `--password`); в коде его нет.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from bot.db import create_engine, create_session_factory, init_db  # noqa: E402
from bot.models import Doctor, Reminder, User  # noqa: E402
from qa.seed import (  # noqa: E402
    LOAD_EMAIL_DOMAIN,
    PATIENT_TG_RANGE,
    SCALES,
    Scale,
    StandGuardError,
    ensure_stand_db,
    load_password_from_env,
    stand_db_path,
)
from qa.seed.seed_5000_measurements import seed_measurements  # noqa: E402
from qa.seed.seed_admin import seed_admin  # noqa: E402
from qa.seed.seed_appointments import seed_appointments  # noqa: E402
from qa.seed.seed_doctors import seed_doctors  # noqa: E402
from qa.seed.seed_load_users import seed_users  # noqa: E402
from qa.seed.seed_medications import seed_medications  # noqa: E402
from qa.seed.seed_reminders import seed_reminders  # noqa: E402
from web.cabinet import auth as web_auth  # noqa: E402

#: Таблица job'ов APScheduler в той же БД (bot/main.py, SQLAlchemyJobStore).
_JOBS_TABLE = "apscheduler_jobs"


async def clean_load_data(session: AsyncSession) -> dict[str, int]:
    """Удалить только нагрузочные данные стенда; вернуть число удалённых строк.

    Порядок по CLAUDE.md: сначала job'ы напоминаний пациентов из таблицы
    APScheduler (если бот когда-то работал на стенде), потом строки `users`
    (каскадом — всё остальное) и врачи `*@load.test`.
    """
    load_users = select(User.id).where(
        User.telegram_id >= PATIENT_TG_RANGE.start, User.telegram_id < PATIENT_TG_RANGE.stop
    )
    reminder_ids = list(
        (await session.execute(select(Reminder.id).where(Reminder.user_id.in_(load_users)))).scalars()
    )
    jobs_removed = 0
    has_jobs_table = (
        await session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": _JOBS_TABLE},
        )
    ).first() is not None
    if has_jobs_table and reminder_ids:
        for reminder_id in reminder_ids:
            result = await session.execute(
                text(f"DELETE FROM {_JOBS_TABLE} WHERE id = :job_id"),
                {"job_id": f"reminder:{reminder_id}"},
            )
            jobs_removed += result.rowcount or 0
    users_removed = (await session.execute(delete(User).where(User.id.in_(load_users)))).rowcount
    doctors_removed = (
        await session.execute(delete(Doctor).where(Doctor.email.like(f"%@{LOAD_EMAIL_DOMAIN}")))
    ).rowcount
    return {"jobs": jobs_removed, "users": users_removed or 0, "doctors": doctors_removed or 0}


async def seed_all(
    db_path: str | os.PathLike,
    scale: Scale,
    password: str,
    *,
    clean: bool = False,
    admin_ids: str = "",
    now: Optional[datetime] = None,
    verbose: bool = True,
) -> dict[str, dict]:
    """Засеять стенд по профилю `scale`; вернуть счётчики и время шагов.

    `db_path` проходит защиту стенда до любого обращения к файлу. `now` —
    момент сида для напоминаний (по умолчанию текущий), `admin_ids` —
    значение `ADMIN_TELEGRAM_IDS`.
    """
    ensure_stand_db(db_path)
    if not password:
        raise ValueError("нет пароля нагрузочных аккаунтов: задайте LOAD_PASSWORD")
    engine = create_engine(str(db_path))
    await init_db(engine)
    session_factory = create_session_factory(engine)
    password_hash = web_auth.hash_password(password)  # один хеш на все аккаунты запуска
    report: dict[str, dict] = {}

    async def step(name: str, action) -> None:
        started = time.perf_counter()
        async with session_factory() as session:
            stats = await action(session)
            await session.commit()
        stats = {**stats, "seconds": round(time.perf_counter() - started, 3)}
        report[name] = stats
        if verbose:
            print(f"[seed] {name}: {stats}")

    try:
        if clean:
            await step("clean", clean_load_data)
        await step("users", lambda s: seed_users(s, scale, password_hash))
        await step("measurements", lambda s: seed_measurements(s, scale))
        await step("medications", lambda s: seed_medications(s, scale))
        await step("appointments", lambda s: seed_appointments(s, scale))
        await step("reminders", lambda s: seed_reminders(s, scale, now=now))
        await step("doctors", lambda s: seed_doctors(s, scale, password_hash))
        await step("admin", lambda s: seed_admin(s, admin_ids))
    finally:
        await engine.dispose()
    return report


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: засеять стенд; 0 — успех, 2 — ошибка аргументов или защиты стенда."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="БД стенда (по умолчанию LOAD_DB или data/test-stand.sqlite3)")
    parser.add_argument("--scale", choices=sorted(SCALES), default="full", help="профиль объёмов")
    parser.add_argument("--clean", action="store_true", help="сначала удалить нагрузочные данные")
    parser.add_argument(
        "--password",
        default=load_password_from_env(),
        help="пароль нагрузочных аккаунтов (по умолчанию LOAD_PASSWORD из окружения)",
    )
    args = parser.parse_args(argv)
    if not args.password:
        parser.error("нет пароля: задайте LOAD_PASSWORD в окружении или --password (в коде пароля нет)")
    try:
        db_path = ensure_stand_db(args.db) if args.db else stand_db_path()
    except StandGuardError as exc:  # понятное сообщение без traceback
        parser.error(str(exc))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = asyncio.run(
        seed_all(
            db_path,
            SCALES[args.scale],
            args.password,
            clean=args.clean,
            admin_ids=os.environ.get("ADMIN_TELEGRAM_IDS", ""),
        )
    )
    print(f"[seed] готово за {time.perf_counter() - started:.1f} с: {db_path} (профиль {args.scale})")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    sys.exit(main())
