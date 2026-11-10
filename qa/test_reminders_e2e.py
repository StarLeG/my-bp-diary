"""E2E кнопок напоминаний о лекарствах (SPEC.md, §6.7, 0.5.4) через Telethon.

Регрессы 0.5.3, найденные проверкой на стенде (`doc/REMINDERS-QUESTIONS.md`):
- «Через 30 мин» после «Отмена» в «Частично…» не работало (id строки 0);
- при двух открытых списках «Частично…» «Готово» сохраняло чужой слот;
- повторное «Принял всё ✓» отвечало «Отметил: нет данных».

Слоты ставятся через «Настройки» → «Напоминания» на «через минуту» (как
`test_reminder_fires`), лекарства — напрямую в БД стенда и удаляются после
теста. Потеря FSM при перезапуске бота здесь не воспроизводится (QA-контейнер
бота не перезапускает) — её закрывают юнит-тесты: обработчики без FSM.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot import texts

#: Таймаут ожидания слот-напоминания.
REMINDER_TIMEOUT = 150.0


@pytest.fixture
def meds(db, tg_me, ensure_onboarded):
    """Два утренних и одно вечернее лекарство сценария; после теста — удалить."""
    ensure_onboarded()
    db.ensure_patient_consents(tg_me.id)  # иначе бот показывает экран согласия (§6.11)
    db.backdate_measurements(tg_me.id)  # «не дёргаем зря» (§6.7) не должно гасить слоты
    ids = {
        "morning": [
            db.add_medication(tg_me.id, "QA-Лозартан", "50 мг", "morning"),
            db.add_medication(tg_me.id, "QA-Амлодипин", "5 мг", "morning"),
        ],
        "evening": [db.add_medication(tg_me.id, "QA-Индапамид", "2,5 мг", "evening")],
    }
    yield ids
    db.delete_medications(ids["morning"] + ids["evening"])


def _today_key(db, tg_me) -> tuple[str, str]:
    user = db.user(tg_me.id)
    tz = ZoneInfo(user["timezone"] if user is not None else "Europe/Minsk")
    return user["timezone"], datetime.now(tz).strftime("%Y%m%d")


def _set_slot(dialog, slot: str, tz_name: str, minutes: int = 1) -> None:
    """Время слота — «через `minutes` минут» через настройки (§6.6)."""
    label = texts.SLOT_LABELS[slot]
    dialog.send("/start")
    dialog.wait_screen(texts.MAIN_MENU)
    dialog.press("menu:settings")
    screen = dialog.press("set:reminders", contains=texts.REMINDERS_MENU)
    if dialog._find_button(screen, f"set:time:{slot}:show") is None:
        dialog.press(f"set:slot:{slot}")  # слот выключен — включить (время по умолчанию)
    dialog.press(f"set:time:{slot}:show", contains=texts.SLOT_TIME_ASK.format(slot=label))
    dialog.press(f"set:time:{slot}:other", contains=texts.SLOT_TIME_ASK.format(slot=label))
    moment = datetime.now(ZoneInfo(tz_name)) + timedelta(minutes=minutes)
    dialog.send(moment.strftime("%H:%M"))
    dialog.wait_screen(texts.REMINDERS_MENU)


class TestReminderButtons:
    def test_cancel_then_snooze(self, dialog, db, tg_me, meds) -> None:
        """«Частично…» → снять отметку → «Отмена» → «Через 30 мин» работает (E1)."""
        tz_name, day = _today_key(db, tg_me)
        _set_slot(dialog, "morning", tz_name)
        reminder = dialog.wait_new("Пора измерить давление", "QA-Лозартан", timeout=REMINDER_TIMEOUT)
        dialog.press(f"rem:partial:morning:{day}", contains=texts.PARTIAL_PROMPT, message=reminder)
        dialog.press(f"rem:pt:morning:{day}:{meds['morning'][1]}", message=reminder)
        dialog.press(f"rem:pc:morning:{day}", contains="Пора измерить давление", message=reminder)
        dialog.press(f"rem:snooze:morning:{day}", contains=texts.SLOT_REMAINING, message=reminder)
        assert db.intake_logs(meds["morning"]) == set(), "«Отмена» ничего не записывает"

    def test_two_open_lists_each_logs_own_slot(self, dialog, db, tg_me, meds) -> None:
        """Два открытых списка: «Готово» в каждом отмечает свой слот (E2)."""
        tz_name, day = _today_key(db, tg_me)
        _set_slot(dialog, "morning", tz_name, minutes=1)
        # Вечер — на минуту позже: оба сообщения в одну минуту `wait_new` не различит.
        _set_slot(dialog, "evening", tz_name, minutes=2)
        morning = dialog.wait_new("Пора измерить давление", "QA-Лозартан", timeout=REMINDER_TIMEOUT)
        evening = dialog.wait_new("Пора измерить давление", "QA-Индапамид", timeout=REMINDER_TIMEOUT)

        dialog.press(f"rem:partial:morning:{day}", contains=texts.PARTIAL_PROMPT, message=morning)
        dialog.press(f"rem:partial:evening:{day}", contains=texts.PARTIAL_PROMPT, message=evening)
        losartan, amlodipine = meds["morning"]
        dialog.press(f"rem:pt:morning:{day}:{amlodipine}", message=morning)  # снять отметку
        dialog.press(f"rem:pd:morning:{day}", contains="Отметил", message=morning)
        assert db.intake_logs(meds["morning"] + meds["evening"]) == {
            (losartan, "morning", "taken"),
            (amlodipine, "morning", "skipped"),
        }
        dialog.press(f"rem:pd:evening:{day}", contains="Отметил", message=evening)
        assert (meds["evening"][0], "evening", "taken") in db.intake_logs(meds["evening"])

    def test_repeat_take_all_says_already(self, dialog, db, tg_me, meds) -> None:
        """Повторное «Принял всё ✓» за день — «уже отмечены сегодня», не «нет данных» (E4)."""
        tz_name, day = _today_key(db, tg_me)
        for _ in range(2):
            _set_slot(dialog, "evening", tz_name)
            reminder = dialog.wait_new("Пора измерить давление", "QA-Индапамид", timeout=REMINDER_TIMEOUT)
            screen = dialog.press(f"rem:all:evening:{day}", contains=("Индапамид",), message=reminder)
        assert (screen.text or "").startswith("Эти лекарства уже отмечены сегодня ✓")
        assert "нет данных" not in (screen.text or "")
