"""Тесты сервиса событий дневника (bot/services/events.py): payload,
факторы, идемпотентность, сон, лента (SPEC.md, §4.18, §6.14; 0.6.0)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, Medication, MedicationLog, User
from bot.schemas import resolve_event_moment
from bot.services.events import (
    EventPayloadError,
    FeedItem,
    create_event,
    create_factor_events,
    day_feed,
    delete_event,
    dumps_payload,
    event_payload,
    events_for_day,
    factor_groups,
    get_event,
    update_event,
    upsert_sleep,
)
from bot.services.measurements import save_measurement

UTC = timezone.utc
MOMENT = datetime(2026, 11, 14, 13, 20, tzinfo=UTC)


async def _other_user(session: AsyncSession) -> User:
    user = User(telegram_id=222_222, timezone="Europe/Minsk")
    session.add(user)
    await session.commit()
    return user


class TestEventPayload:
    def test_food_marks_sorted_canonical(self) -> None:
        first = event_payload("food", marks={"coffee", "salt"})
        second = event_payload("food", marks=["salt", "coffee"])
        assert first == second == dumps_payload({"marks": ["coffee", "salt"]})

    def test_food_marks_must_be_1_to_3(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("food", marks=set())
        with pytest.raises(EventPayloadError):
            event_payload("food", marks={"salt", "coffee", "alcohol", "x"})

    def test_food_unknown_mark(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("food", marks={"sugar"})

    def test_food_extra_field(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("food", marks={"salt"}, extra=1)

    @pytest.mark.parametrize(
        "variant", ["stress", "anxiety", "headache", "weakness", "dizziness", "other"]
    )
    def test_stress_variants(self, variant: str) -> None:
        assert event_payload("stress", variant=variant) == dumps_payload(
            {"variant": variant}
        )

    def test_stress_bad_variant(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("stress", variant="calm")

    @pytest.mark.parametrize("rating", ["little", "normal", "much"])
    def test_sleep_ratings(self, rating: str) -> None:
        assert event_payload("sleep", rating=rating) == dumps_payload({"rating": rating})

    def test_sleep_bad_rating(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("sleep", rating="ok")

    @pytest.mark.parametrize("duration", [10, 20, 30, 60])
    def test_activity_durations(self, duration: int) -> None:
        assert event_payload("activity", duration_min=duration) == dumps_payload(
            {"duration_min": duration}
        )

    def test_activity_bad_duration(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("activity", duration_min=15)

    def test_note_has_no_fields(self) -> None:
        assert event_payload("note") is None
        with pytest.raises(EventPayloadError):
            event_payload("note", marks={"salt"})

    def test_unknown_type(self) -> None:
        with pytest.raises(EventPayloadError):
            event_payload("meal", marks={"salt"})


class TestFactorGroups:
    def test_food_marks_collapse_to_one_event(self) -> None:
        groups = factor_groups({"salt", "coffee"})
        assert len(groups) == 1
        assert groups[0][0] == "food"

    def test_groups_by_type(self) -> None:
        groups = factor_groups({"salt", "stress", "sleep"})
        assert [g[0] for g in groups] == ["food", "stress", "sleep"]

    def test_stress_and_sleep_payloads(self) -> None:
        groups = dict(factor_groups({"stress", "sleep"}))
        assert groups["stress"] == dumps_payload({"variant": "stress"})
        assert groups["sleep"] == dumps_payload({"rating": "little"})

    def test_unknown_mark_rejected(self) -> None:
        with pytest.raises(EventPayloadError):
            factor_groups({"salt", "sugar"})

    def test_empty_marks(self) -> None:
        assert factor_groups(set()) == []


class TestResolveEventMoment:
    TZ = "Europe/Minsk"  # UTC+3 без DST

    def test_past_time_today(self) -> None:
        now = datetime(2026, 11, 14, 13, 20, tzinfo=UTC)  # 16:20 Минск
        moment = resolve_event_moment(time(8, 0), self.TZ, now=now)
        assert moment == datetime(2026, 11, 14, 8, 0, tzinfo=moment.tzinfo)
        assert moment.astimezone(UTC) == datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
        assert moment <= now

    def test_future_time_today_means_yesterday(self) -> None:
        now = datetime(2026, 11, 14, 6, 0, tzinfo=UTC)  # 09:00 Минск
        moment = resolve_event_moment(time(23, 0), self.TZ, now=now)
        assert moment < now
        # вчера 23:00 Минск = вчера 20:00 UTC; ночная запись за прошлую ночь (I4)
        assert now - moment == timedelta(hours=10)

    def test_window_is_24h_at_most(self) -> None:
        now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)  # 13:00 Минск
        # время чуть позже текущего по часам → вчера; глубина почти 24 ч, но не больше
        moment = resolve_event_moment(time(13, 1), self.TZ, now=now)
        assert now - moment < timedelta(hours=24)
        assert now - moment > timedelta(hours=23)


class TestCrud:
    async def test_create_and_get(self, session: AsyncSession, user: User) -> None:
        event = await create_event(
            session,
            user,
            "food",
            MOMENT,
            payload=event_payload("food", marks={"salt"}),
        )
        await session.commit()
        found = await get_event(session, user, event.id)
        assert found is not None
        assert found.type == "food"

    async def test_foreign_event_hidden(self, session: AsyncSession, user: User) -> None:
        other = await _other_user(session)
        event = await create_event(session, user, "note", MOMENT, note="привет")
        await session.commit()
        assert await get_event(session, other, event.id) is None
        assert await delete_event(session, other, event.id) is False
        assert await update_event(session, other, event.id, note="чужое") is None

    async def test_delete(self, session: AsyncSession, user: User) -> None:
        event = await create_event(session, user, "note", MOMENT, note="x")
        await session.commit()
        assert await delete_event(session, user, event.id) is True
        await session.commit()
        assert await get_event(session, user, event.id) is None
        assert await delete_event(session, user, event.id) is False

    async def test_update_fields(self, session: AsyncSession, user: User) -> None:
        event = await create_event(
            session, user, "activity", MOMENT, payload=event_payload("activity", duration_min=10)
        )
        await session.commit()
        updated = await update_event(
            session,
            user,
            event.id,
            moment=MOMENT + timedelta(hours=1),
            payload=event_payload("activity", duration_min=30),
        )
        await session.commit()
        assert updated is not None
        assert updated.payload == dumps_payload({"duration_min": 30})
        assert updated.moment == MOMENT + timedelta(hours=1)


class TestFactorEvents:
    async def test_links_to_own_measurement(
        self, session: AsyncSession, user: User
    ) -> None:
        measurement = await save_measurement(session, user, 120, 80, 60, timestamp=MOMENT)
        await session.commit()
        created = await create_factor_events(
            session, user, measurement.id, {"salt", "sleep"}, MOMENT
        )
        await session.commit()
        assert [e.type for e in created] == ["food", "sleep"]
        assert all(e.measurement_id == measurement.id for e in created)

    async def test_idempotent_double_tap(self, session: AsyncSession, user: User) -> None:
        measurement = await save_measurement(session, user, 120, 80, 60, timestamp=MOMENT)
        await session.commit()
        first = await create_factor_events(session, user, measurement.id, {"salt"}, MOMENT)
        second = await create_factor_events(session, user, measurement.id, {"salt"}, MOMENT)
        await session.commit()
        assert len(first) == 1
        assert second == []  # дубль не создан
        rows = await events_for_day(session, user, MOMENT.date())
        assert len(rows) == 1

    async def test_foreign_measurement_unlinked(
        self, session: AsyncSession, user: User
    ) -> None:
        other = await _other_user(session)
        measurement = await save_measurement(session, other, 120, 80, 60)
        await session.commit()
        created = await create_factor_events(
            session, user, measurement.id, {"coffee"}, MOMENT
        )
        await session.commit()
        assert len(created) == 1
        assert created[0].measurement_id is None

    async def test_empty_marks(self, session: AsyncSession, user: User) -> None:
        assert await create_factor_events(session, user, 1, set(), MOMENT) == []


class TestSleepUpsert:
    async def test_one_rating_per_local_day(self, session: AsyncSession, user: User) -> None:
        first = await upsert_sleep(session, user, date(2026, 11, 14), "little", MOMENT)
        await session.commit()
        second = await upsert_sleep(
            session, user, date(2026, 11, 14), "normal", MOMENT + timedelta(hours=1)
        )
        await session.commit()
        assert second.id == first.id
        rows = await events_for_day(session, user, date(2026, 11, 14))
        assert len(rows) == 1
        assert rows[0].payload == dumps_payload({"rating": "normal"})

    async def test_different_days_are_separate(self, session: AsyncSession, user: User) -> None:
        moment_13 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)  # 13.11, 13:00 Минск
        await upsert_sleep(session, user, date(2026, 11, 13), "little", moment_13)
        await upsert_sleep(session, user, date(2026, 11, 14), "much", MOMENT)
        await session.commit()
        assert len(await events_for_day(session, user, date(2026, 11, 13))) == 1
        assert len(await events_for_day(session, user, date(2026, 11, 14))) == 1

    async def test_bad_rating(self, session: AsyncSession, user: User) -> None:
        with pytest.raises(EventPayloadError):
            await upsert_sleep(session, user, date(2026, 11, 14), "ok", MOMENT)


class TestDayFeed:
    async def test_mixed_feed_sorted(self, session: AsyncSession, user: User) -> None:
        measurement = await save_measurement(session, user, 120, 80, 60, timestamp=MOMENT)
        await session.commit()
        medication = Medication(
            user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning"
        )
        session.add(medication)
        await session.commit()
        session.add(
            MedicationLog(
                user_id=user.id,
                medication_id=medication.id,
                timestamp=MOMENT + timedelta(minutes=5),
                status="taken",
                slot="morning",
            )
        )
        await create_event(session, user, "note", MOMENT + timedelta(minutes=10), note="x")
        await session.commit()
        feed = await day_feed(session, user, date(2026, 11, 14))
        assert [item.kind for item in feed] == ["bp", "med", "event"]
        moments = [item.moment for item in feed]
        assert moments == sorted(moments)
        assert feed[0].measurement == measurement

    async def test_empty_day(self, session: AsyncSession, user: User) -> None:
        assert await day_feed(session, user, date(2026, 11, 14)) == []

    async def test_feed_items_are_frozen(self) -> None:
        item = FeedItem(kind="bp", moment=MOMENT)
        with pytest.raises(Exception):
            item.kind = "med"  # type: ignore[misc]
