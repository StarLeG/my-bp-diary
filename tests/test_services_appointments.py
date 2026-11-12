"""Сервис визитов и анализов (bot/services/appointments.py; SPEC.md, §4.16, §6.13).

Время фиксировано: «сейчас» — 11.11.2026 12:00 по Минску (09:00 UTC, UTC+3
без перехода на летнее время), поэтому моменты уведомлений проверяются точно.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Appointment, Consent, Doctor, DoctorPatient, User
from bot.services import appointments as ap

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)  # 12:00 Минск
TODAY = date(2026, 11, 11)


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def make(kind="doctor", title="Кардиолог", starts_on=TODAY, starts_at="10:30", note="", status="planned", **kw):
    """Несохранённое событие для чистых функций."""
    return Appointment(
        id=kw.pop("id", 1), user_id=1, kind=kind, title=title, starts_on=starts_on,
        starts_at=starts_at, note=note, status=status, **kw,
    )


async def add(session, user, *, now=NOW, **kw) -> Appointment:
    params = {"kind": "doctor", "title": "Кардиолог", "starts_on": TODAY + timedelta(days=3), "starts_at": "10:30", "note": ""}
    params.update(kw)
    appt = await ap.create(session, user, params["kind"], params["title"], params["starts_on"],
                           params["starts_at"], params["note"], now=now)
    await session.commit()
    return appt


async def reload(session: AsyncSession, appointment_id: int) -> Appointment | None:
    return (
        await session.execute(
            select(Appointment).where(Appointment.id == appointment_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ─────────────────────────── Чистые функции ───────────────────────────


class TestDates:
    @pytest.mark.parametrize(
        ("day", "months", "expected"),
        [
            (date(2027, 1, 31), 1, date(2027, 2, 28)),
            (date(2028, 1, 31), 1, date(2028, 2, 29)),
            (date(2026, 11, 14), 3, date(2027, 2, 14)),
            (date(2026, 11, 14), 12, date(2027, 11, 14)),
            (date(2026, 8, 31), 6, date(2027, 2, 28)),
        ],
    )
    def test_add_months(self, day, months, expected) -> None:
        assert ap.add_months(day, months) == expected

    def test_max_day(self) -> None:
        assert ap.max_day(TODAY) == date(2028, 11, 11)
        assert ap.max_day(date(2028, 2, 29)) == date(2030, 2, 28)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("10:30", "10:30"), ("9:05", "09:05"), ("1030", "10:30"), ("10.30", "10:30"), (" 7:00 ", "07:00"),
         ("25:00", None), ("10:60", None), ("10:3", None), (":30", None), ("abc", None), ("103", None)],
    )
    def test_parse_hhmm(self, raw, expected) -> None:
        assert ap.parse_hhmm(raw) == expected

    def test_valid_title_and_note(self) -> None:
        assert ap.valid_title("  Кар   диолог ") == "Кар диолог"
        assert ap.valid_title("   ") is None
        assert ap.valid_title("я" * 60) == "я" * 60
        assert ap.valid_title("я" * 61) is None
        assert ap.valid_note("  каб. 212 ") == "каб. 212"
        assert ap.valid_note("я" * 200) == "я" * 200
        assert ap.valid_note("я" * 201) is None

    def test_time_passed(self) -> None:
        user = User(timezone="Europe/Minsk")
        assert ap.time_passed(user, TODAY, None, NOW) is False
        assert ap.time_passed(user, TODAY, "11:59", NOW) is True
        assert ap.time_passed(user, TODAY, "12:00", NOW) is True
        assert ap.time_passed(user, TODAY, "12:01", NOW) is False
        assert ap.time_passed(user, TODAY + timedelta(days=1), "08:00", NOW) is False

    def test_day_problem(self) -> None:
        user = User(timezone="Europe/Minsk")
        assert ap.day_problem(user, TODAY - timedelta(days=1), NOW) == texts.AP_DAY_PAST
        assert ap.day_problem(user, TODAY, NOW) is None
        assert ap.day_problem(user, date(2028, 11, 11), NOW) is None
        assert ap.day_problem(user, date(2028, 11, 12), NOW) == texts.AP_DAY_TOO_FAR


class TestMoments:
    def test_with_time(self) -> None:
        m = ap.notice_moments(TODAY, "10:30", "Europe/Minsk")
        assert m["eve"] == utc(date(2026, 11, 10), 17)
        assert m["soon"] == utc(TODAY, 5, 30)
        assert m["morning"] is None
        assert m["after"] == utc(TODAY, 17)

    @pytest.mark.parametrize("starts_at", ["07:30", "06:00", "01:00", "07:59"])
    def test_soon_not_before_six(self, starts_at) -> None:
        assert ap.notice_moments(TODAY, starts_at, "Europe/Minsk")["soon"] is None

    def test_soon_exactly_six(self) -> None:
        assert ap.notice_moments(TODAY, "08:00", "Europe/Minsk")["soon"] == utc(TODAY, 3)

    def test_after_for_late_event(self) -> None:
        assert ap.notice_moments(TODAY, "19:00", "Europe/Minsk")["after"] == utc(TODAY + timedelta(days=1), 7)
        assert ap.notice_moments(TODAY, "18:00", "Europe/Minsk")["after"] == utc(TODAY, 17)

    def test_without_time(self) -> None:
        m = ap.notice_moments(TODAY, None, "Europe/Minsk")
        assert m["soon"] is None
        assert m["morning"] == utc(TODAY, 4)
        assert m["after"] == utc(TODAY, 17)

    def test_timezone(self) -> None:
        assert ap.notice_moments(TODAY, None, "Asia/Yekaterinburg")["morning"] == utc(TODAY, 2)

    def test_start_moment(self) -> None:
        assert ap.start_moment(TODAY, None, "Europe/Minsk") is None
        assert ap.start_moment(TODAY, "10:30", "Europe/Minsk") == utc(TODAY, 7, 30)

    def test_fresh_flags(self) -> None:
        flags = ap.fresh_flags(TODAY + timedelta(days=1), "10:30", "Europe/Minsk", NOW)
        assert flags == {"sent_eve_at": None, "sent_soon_at": None, "sent_morning_at": NOW, "sent_after_at": None}
        late = ap.fresh_flags(TODAY, "15:00", "Europe/Minsk", NOW)  # накануне уже прошло
        assert late["sent_eve_at"] == NOW and late["sent_soon_at"] is None

    @pytest.mark.parametrize(
        ("starts_on", "starts_at", "line"),
        [
            (TODAY + timedelta(days=2), "10:30", texts.AP_SCHEDULE[("eve", "soon")]),
            (TODAY + timedelta(days=2), None, texts.AP_SCHEDULE[("eve", "morning")]),
            (TODAY + timedelta(days=2), "07:00", texts.AP_SCHEDULE[("eve",)]),
            (TODAY, "16:00", texts.AP_SCHEDULE[("soon",)]),
            (TODAY, "13:00", texts.AP_SCHEDULE[()]),
            (TODAY, None, texts.AP_SCHEDULE[()]),
        ],
    )
    def test_schedule_line(self, starts_on, starts_at, line) -> None:
        assert ap.schedule_line(starts_on, starts_at, "Europe/Minsk", NOW) == line

    def test_schedule_line_morning_only(self) -> None:
        early = datetime(2026, 11, 11, 2, 0, tzinfo=UTC)  # 05:00 Минск
        assert ap.schedule_line(TODAY, None, "Europe/Minsk", early) == texts.AP_SCHEDULE[("morning",)]


class TestStatuses:
    def test_upcoming_and_waiting(self) -> None:
        assert ap.is_upcoming(make(starts_on=TODAY), TODAY)
        assert not ap.is_upcoming(make(starts_on=TODAY, status="done"), TODAY)
        assert ap.awaiting_answer(make(starts_on=TODAY), TODAY)
        assert ap.awaiting_answer(make(starts_on=TODAY - timedelta(days=3)), TODAY)
        assert not ap.awaiting_answer(make(starts_on=TODAY - timedelta(days=4)), TODAY)
        assert not ap.awaiting_answer(make(starts_on=TODAY + timedelta(days=1)), TODAY)
        assert not ap.awaiting_answer(make(starts_on=TODAY, status="missed"), TODAY)

    @pytest.mark.parametrize(
        ("kind", "status", "offset", "label"),
        [
            ("doctor", "done", 0, "был(а) ✓"),
            ("lab", "done", 0, "сдано ✓"),
            ("lab", "missed", 0, "не сдано"),
            ("exam", "missed", 0, "не был(а)"),
            ("doctor", "moved", 0, "перенесено"),
            ("doctor", "cancelled", 0, "отменено"),
            ("doctor", "planned", 2, "ждёт отметки"),
            ("lab", "planned", 4, "без отметки"),
        ],
    )
    def test_status_label(self, kind, status, offset, label) -> None:
        appt = make(kind=kind, status=status, starts_on=TODAY - timedelta(days=offset))
        assert ap.status_label(appt, TODAY) == label


class TestFormat:
    def test_event(self) -> None:
        assert ap.fmt_day(TODAY) == "ср 11.11"
        assert ap.fmt_event(make(starts_on=date(2026, 11, 14))) == "🩺 Кардиолог — сб 14.11, 10:30"
        assert ap.fmt_event(make(kind="lab", title="Биохимия", starts_at=None), icon=False) == "Биохимия — ср 11.11, без времени"
        block = ap.fmt_event_block(make(note="каб. 212"))
        assert block == "🩺 Кардиолог — ср 11.11, 10:30\n   каб. 212"

    def test_card_upcoming_and_past(self) -> None:
        user = User(timezone="Europe/Minsk")
        card = ap.fmt_card(make(starts_on=TODAY + timedelta(days=3), note="каб. 212"), user, NOW)
        assert card.split("\n") == [
            "🩺 Кардиолог", "сб 14.11.2026, 10:30", "каб. 212", texts.AP_SCHEDULE[("eve", "soon")],
        ]
        past = ap.fmt_card(make(starts_on=TODAY - timedelta(days=1), status="done"), user, NOW)
        assert past.endswith("Статус: был(а) ✓")

    def test_saved(self) -> None:
        user = User(timezone="Europe/Minsk")
        text = ap.fmt_saved(make(starts_on=TODAY + timedelta(days=3)), user, NOW)
        assert text == "Готово ✓\n🩺 Кардиолог — сб 14.11, 10:30\nНапомню накануне в 20:00 и за 2 часа."

    def test_diary_line(self) -> None:
        assert ap.diary_line(None, []) is None
        assert ap.diary_line("tomorrow", [make()]) == "📅 Завтра: Кардиолог, 10:30"
        assert ap.diary_line("today", [make(starts_at=None, title="Анализы")]) == "📅 Сегодня: Анализы"
        two = [make(starts_at=None, title="Анализы"), make(starts_at="15:00")]
        assert ap.diary_line("today", two) == "📅 Сегодня: Анализы, Кардиолог 15:00"
        long = [make(title="я" * 40, starts_at="08:00"), make(title="ю" * 40, starts_at="09:00")]
        assert ap.diary_line("today", long) == "📅 Сегодня: " + "я" * 40 + " 08:00, …"

    def test_notice_text(self) -> None:
        appt = make(kind="lab", title="Биохимия", starts_at=None, note="натощак")
        assert ap.notice_text(appt, "eve") == "Завтра анализы 🧪\nБиохимия — ср 11.11, без времени\n   натощак"
        assert ap.notice_text(make(), "after").startswith("Как прошёл визит? 🩺\nКардиолог")


# ─────────────────────────── Запись в БД ───────────────────────────


class TestCreate:
    async def test_create_sets_flags(self, session, user) -> None:
        appt = await add(session, user, starts_on=TODAY, starts_at="16:00", note="каб. 212")
        row = await reload(session, appt.id)
        assert (row.kind, row.title, row.starts_on, row.starts_at, row.note, row.status) == (
            "doctor", "Кардиолог", TODAY, "16:00", "каб. 212", "planned",
        )
        assert row.sent_eve_at == NOW and row.sent_morning_at == NOW
        assert row.sent_soon_at is None and row.sent_after_at is None

    async def test_limit(self, session, user) -> None:
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"Врач {index}")
        with pytest.raises(ap.LimitReached):
            await add(session, user, title="Лишний")
        assert await ap.count_upcoming(session, user, TODAY) == ap.UPCOMING_LIMIT

    async def test_past_not_counted(self, session, user) -> None:
        session.add(Appointment(user_id=user.id, kind="lab", title="Старое", starts_on=TODAY - timedelta(days=5)))
        await session.commit()
        assert await ap.count_upcoming(session, user, TODAY) == 0

    async def test_create_next(self, session, user) -> None:
        appt = await add(session, user, starts_on=date(2027, 1, 31), starts_at="09:00", note="каб. 5")
        nxt = await ap.create_next(session, user, appt, 1, now=NOW)
        assert (nxt.starts_on, nxt.starts_at, nxt.note, nxt.title) == (date(2027, 2, 28), "09:00", "каб. 5", "Кардиолог")
        assert await ap.create_next(session, user, appt, 1, now=NOW) is None  # такое же уже есть

    async def test_create_next_limit(self, session, user) -> None:
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"Врач {index}")
        appt = (await ap.list_upcoming(session, user, TODAY))[0]
        with pytest.raises(ap.LimitReached):
            await ap.create_next(session, user, appt, 3, now=NOW)

    async def test_reschedule(self, session, user) -> None:
        appt = await add(session, user)
        await session.execute(update(Appointment).where(Appointment.id == appt.id).values(sent_eve_at=NOW))
        assert await ap.reschedule(session, user, appt, TODAY + timedelta(days=5), None, now=NOW)
        await session.commit()
        row = await reload(session, appt.id)
        assert row.starts_on == TODAY + timedelta(days=5) and row.starts_at is None
        assert row.sent_eve_at is None and row.sent_soon_at == NOW
        await ap.set_status(session, row, "cancelled")
        assert not await ap.reschedule(session, user, row, TODAY + timedelta(days=6), None, now=NOW)

    async def test_moved_after(self, session, user) -> None:
        old = await add(session, user, starts_on=TODAY, starts_at="13:00", note="каб. 1", kind="exam", title="ЭКГ")
        new = await ap.moved_after(session, user, old, TODAY + timedelta(days=7), "09:00", now=NOW)
        await session.commit()
        assert (await reload(session, old.id)).status == "moved"
        assert (new.kind, new.title, new.note, new.starts_on, new.starts_at, new.status) == (
            "exam", "ЭКГ", "каб. 1", TODAY + timedelta(days=7), "09:00", "planned",
        )
        assert await ap.moved_after(session, user, old, TODAY + timedelta(days=8), None, now=NOW) is None

    async def test_moved_after_limit(self, session, user) -> None:
        old = await add(session, user, starts_on=TODAY - timedelta(days=1), now=NOW - timedelta(days=2))
        old_id = old.id
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"Врач {index}")
        with pytest.raises(ap.LimitReached):
            await ap.moved_after(session, user, old, TODAY + timedelta(days=3), None, now=NOW)
        await session.rollback()
        assert (await reload(session, old_id)).status == "planned"

    async def test_status_title_note_remove(self, session, user, session_factory) -> None:
        appt = await add(session, user)
        assert await ap.set_status(session, appt, "done")
        assert not await ap.set_status(session, appt, "missed")  # уже не planned
        await ap.set_title(session, appt, "Терапевт")
        await ap.set_note(session, appt, "каб. 3")
        await session.commit()
        row = await reload(session, appt.id)
        assert (row.status, row.title, row.note) == ("done", "Терапевт", "каб. 3")
        other = User(telegram_id=222, timezone="Europe/Minsk")
        session.add(other)
        await session.commit()
        assert await ap.get_owned(session, other, appt.id) is None
        assert not await ap.remove(session, other, appt.id)
        assert await ap.remove(session, user, appt.id)

    async def test_lists(self, session, user) -> None:
        a = await add(session, user, starts_on=TODAY + timedelta(days=1), starts_at="15:00", title="Б")
        b = await add(session, user, starts_on=TODAY + timedelta(days=1), starts_at=None, title="А")
        c = await add(session, user, starts_on=TODAY, starts_at="18:00", title="В")
        session.add(Appointment(user_id=user.id, kind="lab", title="Прошлое", starts_on=TODAY - timedelta(days=2)))
        await session.commit()
        await ap.set_status(session, c, "cancelled")
        await session.commit()
        assert [x.title for x in await ap.list_upcoming(session, user, TODAY)] == ["А", "Б"]
        past, total = await ap.list_past(session, user, TODAY)
        assert [x.title for x in past] == ["В", "Прошлое"] and total == 2
        _, total_only = await ap.list_past(session, user, TODAY, limit=0)
        assert total_only == 2
        assert await ap.count_planned(session) == 3
        key, events = await ap.day_events(session, user, TODAY)
        assert key == "tomorrow" and [e.title for e in events] == ["А", "Б"]
        assert a.id and b.id

    async def test_day_events_today_first(self, session, user) -> None:
        await add(session, user, starts_on=TODAY, starts_at="15:00", title="Сегодня")
        await add(session, user, starts_on=TODAY + timedelta(days=1), title="Завтра")
        key, events = await ap.day_events(session, user, TODAY)
        assert key == "today" and [e.title for e in events] == ["Сегодня"]
        assert await ap.day_events(session, User(id=999, timezone="Europe/Minsk"), TODAY) == (None, [])


# ─────────────────────────── Доставка (тик) ───────────────────────────


class Telegram:
    def __init__(self, fail: bool = False, fail_clear: bool = False, no_id: bool = False) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.cleared: list[tuple[int, int]] = []
        self.fail, self.fail_clear, self.no_id = fail, fail_clear, no_id
        self.next_id = 500

    async def send(self, telegram_id, text, markup):
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((telegram_id, text, markup))
        self.next_id += 1
        return None if self.no_id else self.next_id

    async def clear(self, telegram_id, message_id):
        if self.fail_clear:
            raise RuntimeError("message to edit not found")
        self.cleared.append((telegram_id, message_id))


def markup_for(calls):
    def build(appt, notice, with_doctor, large=False):
        calls.append((appt.id, notice, with_doctor))
        return f"kb:{notice}"
    return build


async def deliver(session_factory, tg, now, calls=None):
    return await ap.deliver_due(session_factory, tg.send, markup_for(calls if calls is not None else []), tg.clear, now=now)


class TestDeliver:
    async def test_eve_sent_once(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1), note="каб. 212")
        tg, calls = Telegram(), []
        eve = utc(TODAY, 17, 1)
        assert await deliver(session_factory, tg, eve, calls) == 1
        assert tg.sent == [(111_111, "Завтра визит 🩺\nКардиолог — чт 12.11, 10:30\n   каб. 212", "kb:eve")]
        assert calls == [(appt.id, "eve", False)]
        row = await reload(session, appt.id)
        assert row.sent_eve_at == eve and row.last_message_id == 501
        assert await deliver(session_factory, tg, eve + timedelta(minutes=1)) == 0

    async def test_markup_receives_ui_large(self, session, user, session_factory) -> None:
        """Клавиатура уведомления получает `large` из `users.ui_large` (§11)."""
        await session.execute(update(User).where(User.id == user.id).values(ui_large=True))
        await session.commit()
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        calls: list[bool] = []

        def build(appt, notice, with_doctor, large=False):
            calls.append(large)
            return "kb"

        assert (
            await ap.deliver_due(session_factory, Telegram().send, build, None, now=utc(TODAY, 17, 1))
            == 1
        )
        assert calls == [True]

    async def test_future_nothing(self, session, user, session_factory) -> None:
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        assert await deliver(session_factory, Telegram(), utc(TODAY, 16, 59)) == 0

    async def test_window_passed_silent(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1), starts_at=None)
        tg = Telegram()
        late = utc(TODAY + timedelta(days=1), 4, 30)  # 07:30 Минск: «накануне» — 11,5 ч назад
        assert await deliver(session_factory, tg, late) == 1  # только «утром»
        assert [t.split("\n")[0] for _, t, _ in tg.sent] == ["Сегодня визит 🩺"]
        row = await reload(session, appt.id)
        assert row.sent_eve_at == late and row.sent_morning_at == late

    async def test_soon_after_start_silent(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1), starts_at="10:30")
        await session.execute(update(Appointment).where(Appointment.id == appt.id).values(sent_eve_at=NOW))
        await session.commit()
        tg = Telegram()
        assert await deliver(session_factory, tg, utc(TODAY + timedelta(days=1), 7, 40)) == 0
        assert (await reload(session, appt.id)).sent_soon_at is not None

    async def test_after_question_and_cleanup(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY, starts_at="13:00")
        await session.execute(update(Appointment).where(Appointment.id == appt.id).values(last_message_id=77))
        await session.commit()
        tg, calls = Telegram(), []
        assert await deliver(session_factory, tg, utc(TODAY, 17), calls) == 1
        assert tg.sent[0][1].startswith("Как прошёл визит? 🩺")
        assert calls == [(appt.id, "after", False)]
        assert tg.cleared == [(111_111, 77)]

    async def test_no_consent_silent(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1))
        await session.execute(update(Consent).where(Consent.user_id == user.id).values(withdrawn_at=NOW))
        await session.commit()
        tg = Telegram()
        assert await deliver(session_factory, tg, utc(TODAY, 17, 1)) == 0
        assert (await reload(session, appt.id)).sent_eve_at is not None

    async def test_pause_does_not_apply(self, session, user, session_factory) -> None:
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        await session.execute(update(User).where(User.id == user.id).values(reminders_paused_until=TODAY + timedelta(days=5)))
        await session.commit()
        assert await deliver(session_factory, Telegram(), utc(TODAY, 17, 1)) == 1

    async def test_cancelled_not_sent(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1))
        await ap.set_status(session, appt, "cancelled")
        await session.commit()
        assert await deliver(session_factory, Telegram(), utc(TODAY, 17, 1)) == 0

    async def test_doctor_access_flag(self, session, user, session_factory) -> None:
        doctor = Doctor(email="d@example.org", name="Смирнова", password_hash="x")
        session.add(doctor)
        await session.flush()
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="active"))
        await session.commit()
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1))
        lab = await add(session, user, starts_on=TODAY + timedelta(days=1), kind="lab", title="Биохимия")
        calls = []
        assert await deliver(session_factory, Telegram(), utc(TODAY, 17, 1), calls) == 2
        assert sorted(calls) == sorted([(appt.id, "eve", True), (lab.id, "eve", False)])
        assert await ap.has_doctor_access(session, user.id)

    async def test_sender_errors(self, session, user, session_factory, caplog) -> None:
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        assert await deliver(session_factory, Telegram(fail=True), utc(TODAY, 17, 1)) == 0
        assert "Не удалось отправить уведомление" in caplog.text

    async def test_no_message_id_and_clear_failure(self, session, user, session_factory) -> None:
        appt = await add(session, user, starts_on=TODAY + timedelta(days=1))
        await session.execute(update(Appointment).where(Appointment.id == appt.id).values(last_message_id=9))
        await session.commit()
        assert await deliver(session_factory, Telegram(no_id=True), utc(TODAY, 17, 1)) == 1
        assert (await reload(session, appt.id)).last_message_id == 9
        appt2 = await add(session, user, starts_on=TODAY + timedelta(days=2), title="Терапевт")
        await session.execute(update(Appointment).where(Appointment.id == appt2.id).values(last_message_id=10))
        await session.commit()
        assert await deliver(session_factory, Telegram(fail_clear=True), utc(TODAY + timedelta(days=1), 17, 1)) >= 1

    async def test_timezone_change_recomputes(self, session, user, session_factory) -> None:
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        await session.execute(update(User).where(User.id == user.id).values(timezone="Asia/Yekaterinburg"))
        await session.commit()
        # 20:00 Екатеринбурга = 15:00 UTC; по Минску было бы 17:00 UTC
        assert await deliver(session_factory, Telegram(), utc(TODAY, 15, 1)) == 1


class TestEdges:
    async def test_list_past_without_limit(self, session, user) -> None:
        for index in range(ap.PAST_LIMIT + 2):
            session.add(Appointment(user_id=user.id, kind="lab", title=f"А{index}", starts_on=TODAY - timedelta(days=index + 1)))
        await session.commit()
        rows, total = await ap.list_past(session, user, TODAY)
        assert len(rows) == ap.PAST_LIMIT and total == ap.PAST_LIMIT + 2
        everything, _ = await ap.list_past(session, user, TODAY, limit=None)
        assert len(everything) == ap.PAST_LIMIT + 2

    async def test_no_doctor_access(self, session, user) -> None:
        assert not await ap.has_doctor_access(session, user.id)
        doctor = Doctor(email="r@example.org", name="Иванова", password_hash="x")
        session.add(doctor)
        await session.flush()
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="revoked"))
        await session.commit()
        assert not await ap.has_doctor_access(session, user.id)

    async def test_lost_claim_sends_nothing(self, session, user, session_factory, mocker) -> None:
        await add(session, user, starts_on=TODAY + timedelta(days=1))
        mocker.patch.object(ap, "_claim", mocker.AsyncMock(return_value=False))  # другой тик успел первым
        tg = Telegram()
        assert await deliver(session_factory, tg, utc(TODAY, 17, 1)) == 0
        assert tg.sent == []


class TestAccountDeletion:
    async def test_cascade(self, session, user) -> None:
        await add(session, user)
        await session.delete(user)
        await session.commit()
        assert (await session.execute(select(Appointment))).scalars().all() == []
