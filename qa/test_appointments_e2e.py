"""E2E «Визиты и анализы» через Telethon (SPEC.md, §6.13; 0.5.8).

Добавление кнопками (тип → подсказка → календарь → время → «Пропустить»),
уведомление «за 2 часа» от тика (событие ставится в БД стенда на «через 2 ч
1 мин», тик раз в минуту), «Как прошёл визит?» из карточки — «✓ Был(а)» →
«Записать следующий» → «Через 3 мес». События сценария удаляются после теста.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot import texts

#: Ожидание уведомления от тика (раз в минуту) с запасом.
TICK_TIMEOUT = 150.0


@pytest.fixture
def visits(db, tg_me, ensure_onboarded):
    ensure_onboarded()
    db.ensure_patient_consents(tg_me.id)
    db.delete_appointments(tg_me.id)
    yield
    db.delete_appointments(tg_me.id)


def _local_now(db, tg_me) -> datetime:
    user = db.user(tg_me.id)
    return datetime.now(ZoneInfo(user["timezone"] if user is not None else "Europe/Minsk"))


class TestVisits:
    def test_add_by_buttons(self, dialog, db, tg_me, visits) -> None:
        day = _local_now(db, tg_me).date() + timedelta(days=3)
        dialog.send("/visits")
        dialog.wait_screen(texts.AP_HEADER)
        dialog.press("ap:add", contains=texts.AP_KIND_ASK)
        dialog.press("ap:kind:doctor", contains="К какому врачу?")
        dialog.press("ap:title:doctor:1", contains=texts.AP_DAY_ASK)
        if day.month != _local_now(db, tg_me).month:
            dialog.press(f"ap:cal:{day:%Y%m}", contains=texts.AP_DAY_ASK)
        dialog.press(f"ap:day:{day:%Y%m%d}", contains=texts.AP_TIME_ASK)
        dialog.press("ap:time:1000", contains=texts.AP_NOTE_ASK)
        screen = dialog.press("ap:note:skip", contains=texts.AP_SAVED)
        assert "Кардиолог" in (screen.text or "") and "Напомню накануне в 20:00 и за 2 часа." in (screen.text or "")
        [row] = db.appointments(tg_me.id)
        assert (row["title"], row["starts_on"], row["starts_at"]) == ("Кардиолог", day.isoformat(), "10:00")

    def test_soon_notice_from_tick(self, dialog, db, tg_me, visits) -> None:
        now = _local_now(db, tg_me)
        start = (now + timedelta(hours=2, minutes=2)).replace(second=0, microsecond=0)
        if start.date() != now.date() or start.hour < 8:
            pytest.skip("«за 2 часа» — только днём (Н3: не раньше 06:00, событие сегодня)")
        db.add_appointment(tg_me.id, start.date().isoformat(), start.strftime("%H:%M"))
        notice = dialog.wait_new("Через 2 часа визит", "QA-Кардиолог", timeout=TICK_TIMEOUT)
        assert {b.data.decode() for row in notice.buttons for b in row} >= {"ap:seen:%d" % db.appointments(tg_me.id)[0]["id"]}
        dialog.press(f"ap:seen:{db.appointments(tg_me.id)[0]['id']}", message=notice)
        assert db.appointments(tg_me.id)[0]["sent_soon_at"] is not None

    def test_done_and_next(self, dialog, db, tg_me, visits) -> None:
        yesterday = _local_now(db, tg_me).date() - timedelta(days=1)
        appt_id = db.add_appointment(tg_me.id, yesterday.isoformat(), "10:00", title="QA-ЭКГ", kind="exam")
        dialog.send("/visits")
        dialog.press("ap:past", contains=texts.AP_PAST_HEADER)
        dialog.press(f"ap:open:{appt_id}", contains="QA-ЭКГ")
        dialog.press(f"ap:after:{appt_id}:done", contains="Записать следующий?")
        screen = dialog.press(f"ap:next:{appt_id}:3", contains=texts.AP_SAVED)
        assert "QA-ЭКГ" in (screen.text or "")
        old, new = db.appointments(tg_me.id)
        assert old["status"] == "done" and new["status"] == "planned"
