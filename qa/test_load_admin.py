"""Админ-панель на полном объёме стенда (SPEC.md, §22, уровень 8; §18).

На приложении бота (`bot_app`: копия стенда, мок Telegram, прод-планировщик):

- «Статистика БД» (`AdminService.get_stats(force=True)`) на 5000 записях:
  время, совпадение агрегатов с БД и приватность — каждый SQL-запрос,
  касающийся `measurements`, только считает (`count(`) и не читает
  `systolic` / `diastolic` / `pulse` / `note` / `feeling` (CLAUDE.md, §18.6);
- поиск пользователя — карточки всех пациентов одновременно, число записей
  совпадает с БД, запросы — те же правила приватности;
- рассылка (`broadcast_message`) на всех пользователей стенда плюс
  синтетических получателей (больше `Semaphore(20)`), часть получателей
  «заблокировала бота» (`TelegramForbiddenError`): отчёт «Доставлено /
  Заблокировали» сходится, одновременных отправок не больше 20.

Итог — `qa/reports/load/test_load_admin.json`.
"""
from __future__ import annotations

import asyncio
import re
import time

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import event, func, select

from bot.models import Measurement, User
from bot.services.admin import BROADCAST_CONCURRENCY, broadcast_message
from qa.seed import load_patients

#: Синтетические получатели рассылки (вне стенда) — чтобы получателей было больше семафора.
SYNTHETIC_TG_BASE = 850_000
SYNTHETIC = 30

#: Каждый пятый получатель «заблокировал бота».
BLOCKED_EVERY = 5

#: Мед-данные, которых в запросах админки быть не должно (CLAUDE.md, «Приватность админки»).
_PRIVATE_COLUMNS = re.compile(r"measurements\.(systolic|diastolic|pulse|note|feeling)\b")


class SqlRecorder:
    """Пишет SQL, который выполняет engine, — для проверки приватности."""

    def __init__(self, engine) -> None:
        self.statements: list[str] = []
        self._engine = engine.sync_engine

    def __enter__(self) -> "SqlRecorder":
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(statement)

    def privacy_violations(self) -> list[str]:
        """Запросы к `measurements`, которые не только считают или читают мед-поля."""
        bad = []
        for statement in self.statements:
            lowered = statement.lower()
            if "measurements" not in lowered:
                continue
            if _PRIVATE_COLUMNS.search(lowered) or "count(" not in lowered:
                bad.append(" ".join(statement.split())[:200])
        return bad


async def test_admin_panel(bot_app, load_scale, load_log, load_report) -> None:
    admin = bot_app.admin
    factory = bot_app.session_factory
    async with factory() as session:
        patients = [user for _, user in await load_patients(session, load_scale)]
        totals = {
            "users": (await session.execute(select(func.count()).select_from(User))).scalar_one(),
            "measurements": (
                await session.execute(select(func.count()).select_from(Measurement))
            ).scalar_one(),
        }
        per_user = dict(
            (
                await session.execute(
                    select(Measurement.user_id, func.count()).group_by(Measurement.user_id)
                )
            ).all()
        )

    # 1. Статистика БД на полном объёме.
    with SqlRecorder(bot_app.engine) as stats_sql:
        started = time.perf_counter()
        stats = await admin.get_stats(force=True)
        stats_s = time.perf_counter() - started
    stats_ok = stats.users == totals["users"] and stats.measurements == totals["measurements"]

    # 2. Поиск: карточки всех пациентов одновременно.
    with SqlRecorder(bot_app.engine) as search_sql:
        started = time.perf_counter()
        cards = await asyncio.gather(*(admin.find_user(user.telegram_id) for user in patients))
        search_s = time.perf_counter() - started
    wrong_cards = [
        user.id
        for user, card in zip(patients, cards)
        if card is None or card.measurements_count != per_user.get(user.id, 0)
    ]

    # 3. Рассылка: все пользователи стенда + синтетические, часть «заблокировала бота».
    recipients = await admin.all_user_ids()
    recipients += [SYNTHETIC_TG_BASE + index for index in range(SYNTHETIC)]
    session = bot_app.bot.session
    blocked = recipients[::BLOCKED_EVERY]
    for telegram_id in blocked:
        session.fail_for(
            telegram_id,
            TelegramForbiddenError(
                method=SendMessage(chat_id=telegram_id, text="…"),
                message="Forbidden: bot was blocked by the user",
            ),
        )
    in_flight = 0
    peak = 0
    original = session.make_request

    async def tracked(bot, method, timeout=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)  # «сеть»: даёт отправкам пересечься
            return await original(bot, method, timeout)
        finally:
            in_flight -= 1

    session.make_request = tracked
    started = time.perf_counter()
    try:
        result = await broadcast_message(bot_app.bot, recipients, "Нагрузочная рассылка")
    finally:
        session.make_request = original
    broadcast_s = time.perf_counter() - started
    expected_delivered = len(recipients) - len(blocked)

    privacy = stats_sql.privacy_violations() + search_sql.privacy_violations()
    report = {
        "passed": stats_ok
        and not wrong_cards
        and not privacy
        and result.delivered == expected_delivered
        and result.blocked == len(blocked)
        and result.failed == 0
        and peak <= BROADCAST_CONCURRENCY,
        "stats": {"users": stats.users, "measurements": stats.measurements, "seconds": round(stats_s, 3)},
        "stats_match_db": stats_ok,
        "search": {"cards": len(cards), "wrong": wrong_cards, "seconds": round(search_s, 3)},
        "broadcast": {
            "recipients": len(recipients),
            "delivered": result.delivered,
            "blocked": result.blocked,
            "failed": result.failed,
            "peak_concurrency": peak,
            "limit": BROADCAST_CONCURRENCY,
            "seconds": round(broadcast_s, 3),
        },
        "privacy_violations": privacy[:10],
        "measurements_queries_checked": sum(
            "measurements" in s.lower() for s in stats_sql.statements + search_sql.statements
        ),
    }
    load_report("test_load_admin", report)
    load_log.summary(
        f"admin: статистика за {stats_s * 1000:.0f} мс ({stats.measurements} записей), "
        f"поиск {len(cards)} карточек за {search_s * 1000:.0f} мс, рассылка "
        f"{result.delivered}/{len(recipients)} (заблокировали {result.blocked}) за {broadcast_s:.2f} с, "
        f"пик {peak} одновременных",
        **{key: value for key, value in report.items() if key != "privacy_violations"},
    )
    assert stats_ok, f"статистика {stats.users}/{stats.measurements} ≠ БД {totals}"
    assert not wrong_cards, f"карточки с неверным числом записей: {wrong_cards}"
    assert not privacy, f"админка читает мед-данные: {privacy[:3]}"
    assert report["measurements_queries_checked"] > 0, "запросы к measurements не перехвачены"
    assert (result.delivered, result.blocked, result.failed) == (expected_delivered, len(blocked), 0)
    assert peak <= BROADCAST_CONCURRENCY, f"одновременных отправок {peak} > {BROADCAST_CONCURRENCY}"
