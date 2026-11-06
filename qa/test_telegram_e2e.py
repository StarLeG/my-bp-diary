"""E2E-сценарии Telegram-бота (SPEC.md, §22, пп. 1–3).

1. **Онбординг:** регистрация, часовой пояс, целевые значения, слот напоминаний.
2. **Запись давления:** эмуляция нажатия кнопок, проверка сохранения в БД.
3. **Напоминания:** ускоренная проверка — слот на минуту вперёд, ожидание
   реальной отправки планировщиком.

Порядок тестов в файле важен: онбординг создаёт пользователя, запись —
измерение, напоминание ждёт отправки. Каждый тест устойчив и к отдельному
запуску: сценарий регистрации начинается с чистой записи пользователя,
остальные — с гарантии онбординга (`ensure_onboarded`).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot import texts

#: Таймаут ожидания слот-напоминания (§22: «время напоминания через 1 минуту»).
REMINDER_TIMEOUT = 150.0


class TestTelegramE2E:
    """Сценарии §22 для бота."""

    def test_onboarding(self, dialog, db, tg_me, onboard) -> None:
        """§22, п.1: регистрация, ввод часового пояса, выбор целевых значений."""
        db.reset_user(tg_me.id)
        onboard()

        user = db.user(tg_me.id)
        assert user is not None, "пользователь не создан в БД"
        assert user["onboarded"] == 1, "онбординг не завершён"
        assert user["timezone"] == "Europe/Minsk"
        assert (user["target_systolic"], user["target_diastolic"]) == (130, 80)
        assert db.reminder_count(tg_me.id, "morning") == 1, "слот утра не включён"

    def test_measurement_flow(self, dialog, db, tg_me, ensure_onboarded) -> None:
        """§22, п.2: запись давления нажатиями кнопок и сохранение в БД."""
        ensure_onboarded()
        before = db.measurement_count(tg_me.id)

        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:measure", contains=texts.ASK_SYSTOLIC)
        dialog.press("m:sys:130", contains=texts.ASK_DIASTOLIC)
        dialog.press("m:dia:85", contains=texts.ASK_PULSE)
        dialog.press("m:pulse:70", contains=texts.fmt_saved(130, 85, 70))

        assert db.measurement_count(tg_me.id) == before + 1, "измерение не сохранено"
        row = db.last_measurement(tg_me.id)
        assert row is not None
        assert (row["systolic"], row["diastolic"], row["pulse"]) == (130, 85, 70)

    def test_edit_last_measurement(self, dialog, db, tg_me, ensure_onboarded) -> None:
        """§6.3: «✏️ Изменить» после сохранения правит последнюю запись (UPDATE)."""
        ensure_onboarded()
        # Старые записи не конфликтуют с новой по дедупу за минуту (§6.3).
        db.backdate_measurements(tg_me.id)
        before = db.measurement_count(tg_me.id)

        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:measure", contains=texts.ASK_SYSTOLIC)
        dialog.press("m:sys:135", contains=texts.ASK_DIASTOLIC)
        dialog.press("m:dia:85", contains=texts.ASK_PULSE)
        dialog.press("m:pulse:70", contains=texts.fmt_saved(135, 85, 70))
        row = db.last_measurement(tg_me.id)
        assert row is not None
        saved_ts = row["timestamp"]

        dialog.press("m:edit", contains=texts.ASK_SYSTOLIC)
        dialog.press("m:sys:140", contains=texts.ASK_DIASTOLIC)
        dialog.press("m:dia:90", contains=texts.ASK_PULSE)
        dialog.press("m:pulse:75", contains=texts.fmt_updated(140, 90, 75))

        # Правка — UPDATE: записей не прибавилось, время записи прежнее.
        assert db.measurement_count(tg_me.id) == before + 1
        row = db.last_measurement(tg_me.id)
        assert row is not None
        assert (row["systolic"], row["diastolic"], row["pulse"]) == (140, 90, 75)
        assert row["timestamp"] == saved_ts, "время записи изменилось при правке"

    def test_edit_from_diary_skips_pulse(self, dialog, db, tg_me, ensure_onboarded) -> None:
        """§6.3–6.4: «Изменить последнюю» из дневника; «Пропустить» очищает пульс."""
        ensure_onboarded()
        if db.measurement_count(tg_me.id) == 0:
            db.add_measurement(tg_me.id, 135, 85, 72)
        before = db.measurement_count(tg_me.id)

        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:diary")
        # Кнопка «✏️ Изменить последнюю» есть в непустом дневнике (§6.4).
        dialog.press("m:edit", contains=texts.ASK_SYSTOLIC)
        dialog.press("m:sys:140", contains=texts.ASK_DIASTOLIC)
        dialog.press("m:dia:90", contains=texts.ASK_PULSE)
        dialog.press("m:pulse:skip", contains=texts.fmt_updated(140, 90, None))

        assert db.measurement_count(tg_me.id) == before
        row = db.last_measurement(tg_me.id)
        assert row is not None
        assert (row["systolic"], row["diastolic"], row["pulse"]) == (140, 90, None)

    def test_reminder_fires(self, dialog, db, tg_me, ensure_onboarded) -> None:
        """§22, п.3: ускоренная проверка — напоминание приходит через минуту."""
        ensure_onboarded()
        # §6.7 «не дёргаем зря»: свежая запись за 3 часа гасит слот —
        # отодвигаем измерения (шаг 2 пишет «сейчас»), иначе напоминание
        # будет корректно пропущено.
        db.backdate_measurements(tg_me.id)
        user = db.user(tg_me.id)
        timezone = user["timezone"] if user is not None else "Europe/Minsk"

        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:settings")  # экран настроек
        dialog.press("set:reminders", contains=texts.REMINDERS_MENU)
        dialog.press(
            "set:time:morning:show",
            contains=texts.SLOT_TIME_ASK.format(slot=texts.SLOT_LABELS["morning"]),
        )
        dialog.press(
            "set:time:morning:other",
            contains=texts.SLOT_TIME_ASK.format(slot=texts.SLOT_LABELS["morning"]),
        )
        # §22: «задать время напоминания на через 1 минуту».
        moment = datetime.now(ZoneInfo(timezone)) + timedelta(minutes=1)
        dialog.send(moment.strftime("%H:%M"))
        dialog.wait_screen(texts.REMINDERS_MENU)

        message = dialog.wait_new("Пора измерить давление", timeout=REMINDER_TIMEOUT)
        assert "Пора измерить давление" in (message.text or "")
