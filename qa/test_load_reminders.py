"""100 напоминаний одновременно на настоящем планировщике (SPEC.md, §22, уровень 5).

Все активные напоминания стенда (60 слотовых + 40 `med_remeasure` в полном
профиле) переводятся на `next_trigger_time = now + 1 с`; запускается
прод-планировщик (`build_scheduler`: `AsyncIOScheduler` +
`SQLAlchemyJobStore` в отдельном файле рядом с копией стенда), job'ы
ставит `RemindersService.startup()`, как при старте бота. Отправки ловит
мок-sender; ожидание — `asyncio.Event`, без sleep-опроса.

Подготовка данных (только копия стенда): `local_time` слота = момент
срабатывания в таймзоне пользователя (тогда следующий слот — завтра), записи
моложе 3 часов сдвигаются на сутки назад — иначе правило «не дёргаем зря»
(§6.7) законно подавило бы слот, и 100 отправок не получилось бы.

Критерии: ровно столько сообщений, сколько напоминаний; каждое напоминание —
ровно одно сообщение (по `rem:snooze:{id}` в клавиатуре); `last_sent_date`
слотов — сегодня, `next_trigger_time` — завтра; `med_remeasure` после
доставки снят; двойной тик (повторный `fire_reminder` по всем) ничего не
отправляет. Итог — `qa/reports/load/test_load_reminders.json`.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest_asyncio
from sqlalchemy import select

from bot.db import create_engine, create_session_factory
from bot.main import build_scheduler, jobs_db_path
from bot.models import Measurement, Reminder, User
from bot.services import reminders as reminders_module
from bot.services.reminders import MODE_REMEASURE, MODE_SLOT, RemindersService, fire_reminder

#: Сколько ждать всех доставок (с запасом на медленный диск).
DELIVERY_TIMEOUT_S = 60.0

#: Пауза после последней доставки: лишние (дублирующие) отправки успели бы прийти.
SETTLE_S = 2.0

_REMINDER_ID = re.compile(r"^rem:snooze:(\d+)$")


@pytest_asyncio.fixture
async def session_factory(load_db: str):
    engine = create_engine(load_db)
    yield create_session_factory(engine)
    await engine.dispose()


def reminder_id_of(markup) -> int:
    """Id строки напоминания из клавиатуры сообщения (`rem:snooze:{id}`)."""
    for row in markup.inline_keyboard:
        for button in row:
            match = _REMINDER_ID.match(button.callback_data or "")
            if match:
                return int(match.group(1))
    raise AssertionError("в клавиатуре напоминания нет rem:snooze:{id}")


async def prepare(session_factory, trigger: datetime) -> dict[int, tuple[str, int, str]]:
    """Все активные напоминания — на `trigger`; вернуть {id: (type, user_id, tz)}."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Reminder, User.timezone)
                .join(User, User.id == Reminder.user_id)
                .where(Reminder.enabled.is_(True))
            )
        ).all()
        for reminder, tz_name in rows:
            reminder.next_trigger_time = trigger
            reminder.last_sent_date = None
            if reminder.type != "med_remeasure":
                reminder.local_time = trigger.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
        recent = (
            await session.execute(
                select(Measurement).where(Measurement.timestamp >= trigger - timedelta(hours=3))
            )
        ).scalars()
        for measurement in recent:
            measurement.timestamp = measurement.timestamp - timedelta(days=1)
        await session.commit()
        return {reminder.id: (reminder.type, reminder.user_id, tz) for reminder, tz in rows}


async def test_reminders_burst(session_factory, load_db, load_scale, load_log, load_report) -> None:
    trigger = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    planned = await prepare(session_factory, trigger)
    assert len(planned) == load_scale.reminders, "в шаблоне не то число активных напоминаний"

    sends: list[tuple[int, int, float]] = []  # (telegram_id, reminder_id, момент)
    all_sent = asyncio.Event()

    async def sender(telegram_id: int, text: str, markup) -> None:
        sends.append((telegram_id, reminder_id_of(markup), time.perf_counter()))
        if len(sends) >= len(planned):
            all_sent.set()

    scheduler = build_scheduler(load_db)
    service = RemindersService(session_factory, sender, scheduler)
    reminders_module.bind_service(service)
    scheduler.start()
    started = time.perf_counter()
    timed_out = False
    try:
        catch_up = await service.startup()  # как при старте бота: job'ы из строк
        try:
            await asyncio.wait_for(all_sent.wait(), timeout=DELIVERY_TIMEOUT_S)
        except TimeoutError:
            timed_out = True
        await asyncio.sleep(SETTLE_S)  # лишние отправки успели бы прийти
        delivered_s = (max(moment for *_, moment in sends) - started) if sends else 0.0
        before_double = len(sends)
        # Двойной тик: повторная доставка каждой строки ничего не должна отправить.
        await asyncio.gather(
            *(
                fire_reminder(rid, MODE_REMEASURE if kind == "med_remeasure" else MODE_SLOT)
                for rid, (kind, _, _) in planned.items()
            )
        )
        double_tick_sends = len(sends) - before_double
    finally:
        scheduler.shutdown(wait=False)
        reminders_module.bind_service(None)

    per_reminder = Counter(rid for _, rid, _ in sends)
    duplicates = {rid: count for rid, count in per_reminder.items() if count > 1}
    missing = sorted(set(planned) - set(per_reminder))
    async with session_factory() as session:
        rows = {
            reminder.id: reminder
            for reminder in (await session.execute(select(Reminder))).scalars()
        }
    wrong_slots: list[dict] = []
    remeasure_left: list[int] = []
    for rid, (kind, _, tz_name) in planned.items():
        row = rows.get(rid)
        if kind == "med_remeasure":
            if row is not None:
                remeasure_left.append(rid)
            continue
        today = trigger.astimezone(ZoneInfo(tz_name)).date()
        next_local = row.next_trigger_time.astimezone(ZoneInfo(tz_name)) if row else None
        if row is None or row.last_sent_date != today or next_local.date() != today + timedelta(days=1):
            wrong_slots.append(
                {"id": rid, "last_sent_date": str(row.last_sent_date) if row else None,
                 "next": str(next_local) if row else None}
            )

    report = {
        "passed": not timed_out
        and len(sends) == len(planned)
        and not duplicates
        and not missing
        and not wrong_slots
        and not remeasure_left
        and double_tick_sends == 0,
        "reminders": len(planned),
        "sent": len(sends),
        "catch_up": catch_up,
        "timed_out": timed_out,
        "delivered_in_s": round(delivered_s, 3),
        "duplicates": duplicates,
        "missing": missing,
        "wrong_slots": wrong_slots[:10],
        "remeasure_left": remeasure_left,
        "double_tick_sends": double_tick_sends,
        "jobs_file": jobs_db_path(load_db),
    }
    load_report("test_load_reminders", report)
    load_log.summary(
        f"reminders: {len(sends)}/{len(planned)} доставлено за {delivered_s:.2f} с, "
        f"дублей {len(duplicates)}, двойной тик +{double_tick_sends}",
        **{key: value for key, value in report.items() if key not in {"wrong_slots"}},
    )
    assert not timed_out, f"за {DELIVERY_TIMEOUT_S} с доставлено {len(sends)} из {len(planned)}"
    assert len(sends) == len(planned), f"сообщений {len(sends)}, ожидалось {len(planned)}"
    assert not duplicates, f"напоминания отправлены больше одного раза: {duplicates}"
    assert not missing, f"не доставлены: {missing}"
    assert not wrong_slots, f"слоты не перепланированы на завтра: {wrong_slots[:5]}"
    assert not remeasure_left, f"повторы замера не сняты после доставки: {remeasure_left}"
    assert double_tick_sends == 0, f"двойной тик отправил ещё {double_tick_sends}"
