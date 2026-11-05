"""Тесты сервиса статистики (bot/services/stats.py)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services import stats as service

UTC = timezone.utc


def aware(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestComputeTrend:
    def _ts(self, day: int) -> datetime:
        return aware(2026, 11, day, 8, 0)

    def test_few_measurements(self) -> None:
        assert (
            service.compute_trend([120, 130, 140], [self._ts(1), self._ts(2), self._ts(3)])
            == texts.STATS_TREND_FEW
        )

    def test_growing(self) -> None:
        result = service.compute_trend(
            [120, 120, 140, 140], [self._ts(1), self._ts(2), self._ts(3), self._ts(4)]
        )
        assert result == texts.STATS_TREND_UP

    def test_falling(self) -> None:
        result = service.compute_trend(
            [140, 140, 120, 120], [self._ts(1), self._ts(2), self._ts(3), self._ts(4)]
        )
        assert result == texts.STATS_TREND_DOWN

    def test_stable_within_threshold(self) -> None:
        # Разница 2 мм рт. ст. — не больше порога: стабильно.
        result = service.compute_trend(
            [130, 130, 132, 132], [self._ts(1), self._ts(2), self._ts(3), self._ts(4)]
        )
        assert result == texts.STATS_TREND_STABLE

    def test_threshold_boundary_exact(self) -> None:
        # Ровно +2 — не «растёт» (строго больше порога).
        result = service.compute_trend(
            [130, 130, 132, 132], [self._ts(1), self._ts(2), self._ts(4), self._ts(5)]
        )
        assert result == texts.STATS_TREND_STABLE

    def test_all_same_time_gives_few(self) -> None:
        stamp = self._ts(1)
        result = service.compute_trend([120, 130, 140, 150], [stamp] * 4)
        assert result == texts.STATS_TREND_FEW


class TestComputeGoal:
    def test_spec_example_within(self) -> None:
        # Пример SPEC.md, раздел 6.5: 128/82 при цели 130/80 — в пределах цели.
        assert service.compute_goal(128, 82, 130, 80) == texts.STATS_GOAL_OK

    def test_above_systolic(self) -> None:
        assert service.compute_goal(136, 80, 130, 80) == texts.STATS_GOAL_ABOVE

    def test_above_diastolic(self) -> None:
        assert service.compute_goal(128, 86, 130, 80) == texts.STATS_GOAL_ABOVE

    def test_tolerance_boundary_ok(self) -> None:
        # 135/85 при цели 130/80 — допуск ±5, в пределах.
        assert service.compute_goal(135, 85, 130, 80) == texts.STATS_GOAL_OK

    def test_below(self) -> None:
        assert service.compute_goal(115, 70, 130, 80) == texts.STATS_GOAL_BELOW

    def test_below_requires_both(self) -> None:
        # Систолическое ниже, диастолическое в цели — «в пределах», не «ниже».
        assert service.compute_goal(115, 80, 130, 80) == texts.STATS_GOAL_OK


class TestSummary:
    async def test_empty_period(self, session: AsyncSession, user: User) -> None:
        result = await service.summary(session, user, date(2026, 11, 1), date(2026, 11, 7))
        assert result.count == 0
        assert result.sys_avg is None
        assert result.trend == texts.STATS_TREND_FEW
        assert result.adherence is None

    async def _populate(self, session: AsyncSession, user: User) -> None:
        stamps = [
            aware(2026, 11, 1, 8, 0),
            aware(2026, 11, 2, 8, 0),
            aware(2026, 11, 3, 8, 0),
            aware(2026, 11, 4, 8, 0),
        ]
        values = [(115, 75, 65), (135, 95, 75), (135, 95, None), (115, 75, 65)]
        for stamp, (sys_v, dia_v, pulse) in zip(stamps, values):
            await measurements_service.save_measurement(
                session, user, sys_v, dia_v, pulse, timestamp=stamp
            )
        await session.flush()

    async def test_full_summary(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        result = await service.summary(session, user, date(2026, 11, 1), date(2026, 11, 7))
        assert result.count == 4
        assert result.sys_avg == 125  # (115+125+135+125)/4
        assert result.sys_min == 115 and result.sys_max == 135
        assert result.dia_avg == 85
        assert result.dia_min == 75 and result.dia_max == 95
        assert result.pulse_avg == 68  # (65+75+65)/3
        assert result.trend == texts.STATS_TREND_STABLE  # средние половин равны
        assert result.goal_verdict == texts.STATS_GOAL_OK
        assert result.adherence is None  # лекарств нет — строки приверженности нет

    async def test_pulse_avg_none_without_pulse(self, session: AsyncSession, user: User) -> None:
        await measurements_service.save_measurement(
            session, user, 120, 80, None, timestamp=aware(2026, 11, 1, 8, 0)
        )
        result = await service.summary(session, user, date(2026, 11, 1), date(2026, 11, 1))
        assert result.pulse_avg is None

    async def test_adherence_included_with_medications(
        self, session: AsyncSession, user: User
    ) -> None:
        await self._populate(session, user)
        meds = await medications_service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning",)
        )
        await session.flush()
        await medications_service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 1, 8, 5)
        )
        result = await service.summary(session, user, date(2026, 11, 1), date(2026, 11, 7))
        assert result.adherence == (1, 7)

    async def test_user_isolation(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await self._populate(session, user)
        result = await service.summary(session, other, date(2026, 11, 1), date(2026, 11, 7))
        assert result.count == 0
