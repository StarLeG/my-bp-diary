"""Burst Telegram-бота через Dispatcher (SPEC.md, §22, уровень 3).

Виртуальные пользователи — пациенты шаблона стенда (онбординг пройден,
лекарства есть) — одновременно проходят полный флоу через настоящий
`Dispatcher.feed_update`: весь стек middleware (сессия БД, debounce,
throttle) и хендлеры, сеть Telegram подменена (`make_fake_telegram`).

Флоу каждого: `/start` → 10 записей (быстрый ввод «120/80 72» + 9
калькулятором; на последней «Готово» жмётся дважды — debounce обязан отсечь
второе нажатие) → «➕ Фактор» к записи → правка последней ×3 → «Приняли
лекарство?» → «Да» → мультивыбор → «Готово» → 2 архивные записи (по кнопкам и
быстрым вводом) → «Принял всё ✓» по слоту с лекарствами → «➕ Добавить запись»
(0.6.0): прогулка, питание, стресс, заметка, сон — с проверками «Отмена»/«Назад»
на каждом шаге (SPEC §6.14: с первого шага — в «➕ Добавить запись», со следующих
— на предыдущий шаг, из ввода ЧЧ:ММ — на вызвавший экран) → «Дневник за
сегодня» → «Статистика» 7 и 30 дней → экспорт CSV.

«Принял всё ✓» нажимается как на доставленном напоминании (`rem:all:{slot}`):
обычная доставка слота после свежих записей подавляется правилом «не дёргаем
зря» (§6.7), а доставку как таковую проверяет `test_load_reminders.py`.

Время записей: `measurements.utcnow` сдвигается фикстурой — каждый вызов на
минуту позже (иначе дедуп «одна запись в минуту» оставил бы одну запись из
десяти); моменты — от «сейчас» вперёд, мимо минут сида. Часы middleware —
`VirtualClock`: окно debounce/throttle «проходит» только по `tick()`.

Критерии: все пользователи проходят флоу без исключений; ровно 12 новых
строк на пользователя (10 обычных + 2 архивные, всего 240 в полном
профиле); правки — UPDATE последней записи; `measurement_medications`
заполнена; «Принял всё ✓» — ровно один набор логов слота за день; события
нововедений — ровно 7 на пользователя (5 событий «Добавить запись» + 2
фактора «➕ Фактор»).
"""
from __future__ import annotations

import asyncio
import itertools
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from bot import texts
from bot.models import (
    DiaryEvent,
    Measurement,
    MeasurementMedication,
    Medication,
    MedicationLog,
    User,
)
from bot.services import measurements as measurements_service
from bot.services.measurements import day_bounds_utc
from qa.seed import load_patients

#: Записей калькулятором на пользователя и архивных записей (SPEC §22, уровень 3).
RECORDS = 10
ARCHIVE_RECORDS = 2

#: Событий дневника на пользователя (0.6.0): 5 событий «Добавить запись»
#: (прогулка, питание, стресс, заметка, сон) + 2 события «➕ Фактор» (B6:
#: метки salt+stress складываются в группы food и stress).
NOVELTY_EVENTS = 7

#: Значения правок «последней записи» (три прохода; итог — последний набор).
EDITS = ((150, 95, 80), (151, 96, 81), (152, 97, 82))

#: Архивные моменты (локальное время пользователя): вне окна сида и «сейчас».
ARCHIVE_BUTTONS = (date(2024, 3, 16), "08:00")
ARCHIVE_QUICK = "15.03.2024 09:00 130/85 70"

_update_ids = itertools.count(1)


@pytest.fixture
def shifted_utcnow(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """`measurements.utcnow`: каждый вызов — на минуту позже (от «сейчас» вперёд)."""
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    issued: list[datetime] = []

    def fake_utcnow() -> datetime:
        moment = base + timedelta(minutes=len(issued))
        issued.append(moment)
        return moment

    monkeypatch.setattr(measurements_service, "utcnow", fake_utcnow)
    return issued


class VirtualUser:
    """Пользователь Telegram: шлёт апдейты в Dispatcher и замеряет каждый шаг."""

    def __init__(self, app, user: User, clock) -> None:
        self.app = app
        self.user = user
        self.clock = clock
        self.tg = TgUser(id=user.telegram_id, is_bot=False, first_name="Нагрузка")
        self.chat = Chat(id=user.telegram_id, type="private")
        self.durations: list[float] = []
        self.callback_ids: set[str] = set()
        self.step = ""
        self.error_trace = ""

    def _bot_message(self) -> Message:
        """Последнее сообщение бота в чате — к нему «привязана» нажимаемая кнопка."""
        for call in reversed(self.app.bot.session.for_chat(self.chat.id)):
            if isinstance(call, (SendMessage, EditMessageText, SendDocument)):
                message_id = getattr(call, "message_id", None) or 1
                return Message(
                    message_id=message_id,
                    date=datetime.now(timezone.utc),
                    chat=self.chat,
                    text=getattr(call, "text", None) or "…",
                )
        return Message(message_id=1, date=datetime.now(timezone.utc), chat=self.chat, text="…")

    async def _feed(self, update: Update) -> None:
        self.step = update.callback_query.data if update.callback_query else update.message.text
        started = time.perf_counter()
        try:
            await self.app.dp.feed_update(self.app.bot, update)
        except BaseException:
            self.error_trace = traceback.format_exc()
            raise
        finally:
            self.durations.append((time.perf_counter() - started) * 1000)

    async def send(self, text: str) -> None:
        """Текстовое сообщение пользователя (команда или ввод)."""
        self.clock.tick()
        await self._feed(
            Update(
                update_id=next(_update_ids),
                message=Message(
                    message_id=next(_update_ids),
                    date=datetime.now(timezone.utc),
                    chat=self.chat,
                    from_user=self.tg,
                    text=text,
                ),
            )
        )

    async def tap(self, data: str, *, tick: bool = True) -> None:
        """Нажатие inline-кнопки; `tick=False` — без паузы (двойное нажатие)."""
        if tick:
            self.clock.tick()
        callback_id = str(next(_update_ids))
        self.callback_ids.add(callback_id)
        await self._feed(
            Update(
                update_id=next(_update_ids),
                callback_query=CallbackQuery(
                    id=callback_id,
                    from_user=self.tg,
                    chat_instance="load",
                    data=data,
                    message=self._bot_message(),
                ),
            )
        )

    async def keypad(self, *values: Optional[int], double_ok_last: bool = False) -> None:
        """Одноэкранный калькулятор (0.6.0, §6.3): значения по полям + «Готово».

        Фокус в поле — тап `m:field:{sys|dia|pulse}` (коммитит буфер предыдущего),
        цифры — в буфер активного поля, «Готово» (`m:key:ok`) — один раз в конце
        (сохраняет запись). `None` — поле не трогать («Пропустить» убрано, R7).
        `double_ok_last` — двойной тап «Готово» на последней записи: debounce
        обязан отсечь второе нажатие (§7.2).
        """
        fields = ("sys", "dia", "pulse")
        for index, value in enumerate(values):
            if index:
                await self.tap(f"m:field:{fields[index]}")
            if value is None:
                continue
            for digit in str(value):
                await self.tap(f"m:key:{digit}", tick=False)  # цифры — без debounce (§6.3)
        if double_ok_last:
            self.clock.tick()
            await asyncio.gather(
                self.tap("m:key:ok", tick=False), self.tap("m:key:ok", tick=False)
            )
        else:
            await self.tap("m:key:ok")

    def answered(self, text: str) -> int:
        """Сколько раз бот ответил на callback'и этого пользователя этим текстом."""
        return sum(
            1
            for call in self.app.bot.session.calls
            if isinstance(call, AnswerCallbackQuery)
            and call.callback_query_id in self.callback_ids
            and call.text == text
        )

    def _last_screen(self):
        """Последний экран бота (`EditMessageText`/`SendMessage`) в чате пользователя."""
        for call in reversed(self.app.bot.session.for_chat(self.chat.id)):
            if isinstance(call, (EditMessageText, SendMessage)):
                return call
        return None

    def last_text(self) -> str:
        """Текст последнего экрана бота — для проверок навигации (SPEC §6.14)."""
        screen = self._last_screen()
        return (screen.text if screen else "") or ""

    def last_buttons(self) -> set[str]:
        """`callback_data` кнопок последнего экрана."""
        screen = self._last_screen()
        markup = getattr(screen, "reply_markup", None) if screen else None
        if markup is None:
            return set()
        return {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }

    def last_labels(self) -> set[str]:
        """Тексты кнопок последнего экрана (метки «✓ …», radio времени)."""
        screen = self._last_screen()
        markup = getattr(screen, "reply_markup", None) if screen else None
        if markup is None:
            return set()
        return {
            button.text for row in markup.inline_keyboard for button in row
        }


async def run_flow(vu: VirtualUser, regular_med_id: int, slot: str) -> None:
    """Полный флоу одного пользователя (см. докстринг модуля)."""
    await vu.send("/start")
    # Быстрый ввод «120/80 72» (B5) — одна из 10 записей вместо калькулятора.
    await vu.tap("menu:measure")
    await vu.send("120/80 72")
    await vu.tap("m:key:ok")
    for k in range(1, RECORDS):
        await vu.tap("menu:measure")
        await vu.keypad(120 + k, 75 + k % 10, 60 + k, double_ok_last=k == RECORDS - 1)
    # «➕ Фактор» (B6): метки к последней записи.
    await vu.tap("m:fact")
    await vu.tap("m:fact:pick:salt", tick=False)
    await vu.tap("m:fact:pick:stress", tick=False)
    await vu.tap("m:fact:done")
    for systolic, diastolic, pulse in EDITS:
        await vu.tap("m:edit")
        await vu.keypad(systolic, diastolic, pulse)
    await vu.tap("m:med")
    await vu.tap("m:med:yes")
    await vu.tap(f"m:med:pick:{regular_med_id}", tick=False)
    await vu.tap("m:med:done")
    archive_day, archive_time = ARCHIVE_BUTTONS
    await vu.tap("set:archive:add")
    await vu.tap("set:archive:date:other")
    await vu.send(archive_day.strftime("%d.%m.%Y"))
    await vu.tap(f"set:archive:time:{archive_time}")
    await vu.keypad(128, 84, 66)
    await vu.tap("set:archive:quick")
    await vu.send(ARCHIVE_QUICK)
    await vu.tap(f"rem:all:{slot}")
    await run_add_record_scenario(vu, regular_med_id)
    await vu.tap("menu:diary")
    await vu.tap("menu:stats")
    await vu.tap("stats:7")
    await vu.tap("stats:30")
    await vu.tap("csv:30")


async def run_add_record_scenario(vu: VirtualUser, regular_med_id: int) -> None:
    """«➕ Добавить запись» (0.6.0): все типы событий + «Отмена»/«Назад» (SPEC §6.14).

    Каждый «Отмена» проверяет свой маршрут: V5 — с первого шага типа в «Добавить
    запись»; V8 — со шага времени на предыдущий шаг с сохранением значений;
    M2 — из ввода `ЧЧ:ММ` на вызвавший экран (у «Питания» — с метками и radio
    «Сейчас»); из шага статуса мед-фасада — к списку приёмов.
    """
    # Прогулка: «Отмена» с первого шага (V5) → назад в «Добавить запись».
    await vu.tap("add:menu")
    await vu.tap("add:walk")
    await vu.tap("add:cancel")
    assert texts.EVENT_MENU_ASK in vu.last_text(), "V5: add:cancel не вернул в меню типов"
    # Прогулка: «Назад» со шага времени (V8) → шаг длительности, значение цело.
    await vu.tap("add:walk")
    await vu.tap("add:walk:20")
    await vu.tap("add:back")
    assert texts.WALK_ASK in vu.last_text(), "V8: add:back не вернул к длительности"
    assert "add:walk:20" in vu.last_buttons(), "V8: пресет длительности исчез"
    await vu.tap("add:walk:20")
    await vu.tap("add:time:now")  # событие «Прогулка»
    # Питание: «Назад» из ввода ЧЧ:ММ (M2) → экран с метками и «Сейчас».
    await vu.tap("add:menu")
    await vu.tap("add:food")
    await vu.tap("add:food:pick:salt", tick=False)
    await vu.tap("add:food:time:other")
    await vu.tap("add:back")
    assert "Питание" in vu.last_text(), "M2: add:back не вернул на экран питания"
    assert f"✓ {texts.FACTOR_LABELS['salt']}" in vu.last_labels(), "M2: метка потеряна"
    assert f"✓ {texts.EVENT_TIME_NOW}" in vu.last_labels(), "M2: radio «Сейчас» сбит"
    await vu.tap("add:food:done")  # событие «Питание»
    # Стресс: «Назад» с обязательного текста (V1/M6) → шаг варианта.
    await vu.tap("add:menu")
    await vu.tap("add:stress")
    await vu.tap("add:stress:other")
    await vu.tap("add:back")
    assert texts.STRESS_ASK in vu.last_text(), "add:back не вернул к вариантам стресса"
    await vu.tap("add:stress:anxiety")
    await vu.tap("add:text:skip")
    await vu.tap("add:time:now")  # событие «Стресс»
    # Заметка: «Назад» с текста (первый шаг) → в «Добавить запись».
    await vu.tap("add:menu")
    await vu.tap("add:note")
    await vu.tap("add:back")
    assert texts.EVENT_MENU_ASK in vu.last_text(), "add:back с заметки не вернул в меню"
    await vu.tap("add:note")
    await vu.send("После обеда")
    await vu.tap("add:time:now")  # событие «Заметка»
    # Сон: оценка без времени (одна на сутки).
    await vu.tap("add:menu")
    await vu.tap("add:sleep")
    await vu.tap("add:sleep:little")  # событие «Сон»
    # Лекарство: «Назад» со статуса (§6.14) → список приёмов; «Отмена» → меню типов.
    await vu.tap("add:menu")
    await vu.tap("add:med")
    await vu.tap(f"add:med:pick:{regular_med_id}")
    await vu.tap("add:back")
    assert texts.MED_PICK_ASK in vu.last_text(), "add:back не вернул к списку приёмов"
    await vu.tap("add:cancel")
    assert texts.EVENT_MENU_ASK in vu.last_text(), "add:cancel не вернул в меню типов"


def local_moment(day: date, hhmm: str, tz_name: str) -> datetime:
    """Локальные дата и ЧЧ:ММ пользователя → UTC."""
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz_name)).astimezone(
        timezone.utc
    )


def p95(values: list[float]) -> float:
    """p95 по ближайшему рангу."""
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered) + 0.5) - 1)] if ordered else 0.0


async def test_bot_flow(bot_app, virtual_clock, shifted_utcnow, load_scale, load_log, load_report) -> None:
    """Все пользователи одновременно проходят флоу; данные — ровно как ожидается."""
    factory = bot_app.session_factory
    async with factory() as session:
        pairs = await load_patients(session, load_scale)
        plans: dict[int, tuple[int, str]] = {}
        for _, user in pairs:
            meds = list(
                (
                    await session.execute(
                        select(Medication)
                        .where(Medication.user_id == user.id, Medication.slot != "crisis")
                        .order_by(Medication.id)
                    )
                ).scalars()
            )
            assert meds, f"user {user.id}: в шаблоне нет плановых лекарств"
            plans[user.id] = (meds[0].id, meds[0].slot)
        before = dict(
            (
                await session.execute(
                    select(Measurement.user_id, func.count()).group_by(Measurement.user_id)
                )
            ).all()
        )
    users = [user for _, user in pairs]
    assert len(users) == load_scale.patients
    virtual_users = [VirtualUser(bot_app, user, virtual_clock) for user in users]

    started = time.perf_counter()
    results = await asyncio.gather(
        *(run_flow(vu, *plans[vu.user.id]) for vu in virtual_users), return_exceptions=True
    )
    elapsed = time.perf_counter() - started
    failures = {
        vu.user.id: {"step": vu.step, "error": repr(result), "trace": vu.error_trace[-1500:]}
        for vu, result in zip(virtual_users, results)
        if isinstance(result, BaseException)
    }

    violations: list[dict] = []
    async with factory() as session:
        for vu in virtual_users:
            user = vu.user
            rows = list(
                (
                    await session.execute(
                        select(Measurement)
                        .where(Measurement.user_id == user.id)
                        .order_by(Measurement.timestamp.desc(), Measurement.id.desc())
                    )
                ).scalars()
            )
            added = len(rows) - before.get(user.id, 0)
            if added != RECORDS + ARCHIVE_RECORDS:
                violations.append({"user_id": user.id, "check": "rows", "added": added})
            latest = rows[0]
            if (latest.systolic, latest.diastolic, latest.pulse) != EDITS[-1]:
                violations.append({"user_id": user.id, "check": "edits", "latest": latest.id})
            archive_moments = {
                local_moment(ARCHIVE_BUTTONS[0], ARCHIVE_BUTTONS[1], user.timezone),
                local_moment(date(2024, 3, 15), "09:00", user.timezone),
            }
            found = {row.timestamp for row in rows} & archive_moments
            if found != archive_moments:
                violations.append({"user_id": user.id, "check": "archive", "found": len(found)})
            linked = (
                await session.execute(
                    select(func.count())
                    .select_from(MeasurementMedication)
                    .join(Measurement, Measurement.id == MeasurementMedication.measurement_id)
                    .where(Measurement.user_id == user.id)
                )
            ).scalar_one()
            if linked < 1:
                violations.append({"user_id": user.id, "check": "measurement_medications"})
            slot = plans[user.id][1]
            day_start, day_end = day_bounds_utc(
                user.timezone, datetime.now(ZoneInfo(user.timezone)).date()
            )
            slot_meds = (
                await session.execute(
                    select(func.count()).select_from(Medication).where(
                        Medication.user_id == user.id, Medication.slot == slot
                    )
                )
            ).scalar_one()
            slot_logs = (
                await session.execute(
                    select(func.count()).select_from(MedicationLog).where(
                        MedicationLog.user_id == user.id,
                        MedicationLog.slot == slot,
                        MedicationLog.timestamp >= day_start,
                        MedicationLog.timestamp < day_end,
                    )
                )
            ).scalar_one()
            if slot_logs != slot_meds:
                violations.append(
                    {"user_id": user.id, "check": "take_all_logs",
                     "expected": slot_meds, "got": slot_logs}
                )
            debounced = vu.answered(texts.DEBOUNCE_NOTICE)
            if debounced != 1:  # двойное «Готово» — второе нажатие отсечено
                violations.append({"user_id": user.id, "check": "debounce", "got": debounced})
            novelty_events = (
                await session.execute(
                    select(func.count())
                    .select_from(DiaryEvent)
                    .where(DiaryEvent.user_id == user.id)
                )
            ).scalar_one()
            if novelty_events != NOVELTY_EVENTS:
                violations.append(
                    {"user_id": user.id, "check": "novelty_events",
                     "expected": NOVELTY_EVENTS, "got": novelty_events}
                )

    durations = [ms for vu in virtual_users for ms in vu.durations]
    async with factory() as session:
        total_added = (
            await session.execute(select(func.count()).select_from(Measurement))
        ).scalar_one() - sum(before.values())
    expected_rows = len(users) * (RECORDS + ARCHIVE_RECORDS)
    documents = sum(1 for call in bot_app.bot.session.calls if isinstance(call, SendDocument))
    report = {
        "passed": not failures
        and not violations
        and total_added == expected_rows
        and documents == 2 * len(users),
        "users": len(users),
        "updates": len(durations),
        "elapsed_s": round(elapsed, 3),
        "p95_update_ms": round(p95(durations), 2),
        "rows_added": total_added,
        "rows_expected": expected_rows,
        "failures": failures,
        "violations": violations,
        "documents_sent": documents,
    }
    load_report("test_load_bot", report)
    load_log.summary(
        f"bot: {len(users)} пользователей, {len(durations)} апдейтов за {elapsed:.2f} с, "
        f"p95 {report['p95_update_ms']} мс, строк +{total_added}/{expected_rows}, "
        f"исключений {len(failures)}, нарушений {len(violations)}",
        **{key: value for key, value in report.items() if key not in {"failures", "violations"}},
    )
    assert not failures, f"флоу упал у пользователей: {failures}"
    assert not violations, f"нарушения: {violations[:10]}"
    assert total_added == expected_rows, f"строк +{total_added}, ожидалось {expected_rows}"
    # Экспорт CSV — два файла: измерения и приёмы лекарств (§10).
    assert report["documents_sent"] == 2 * len(users), "экспорт CSV дошёл не у всех"
