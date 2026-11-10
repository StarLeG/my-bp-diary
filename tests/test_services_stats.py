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


# ─────────────────────────── Средние по дням (0.5.3) ───────────────────────────


def row(ts: datetime, systolic: int, diastolic: int, pulse: int | None = None):
    """Запись без БД: `daily_averages` работает с готовыми объектами."""
    from bot.models import Measurement

    return Measurement(user_id=1, timestamp=ts, systolic=systolic, diastolic=diastolic, pulse=pulse)


class TestDailyAverages:
    TZ = "Europe/Minsk"  # UTC+3

    def test_every_day_of_period_including_empty(self) -> None:
        result = service.daily_averages(
            [row(aware(2026, 11, 2, 9), 130, 80, 70)], self.TZ, date(2026, 11, 1), date(2026, 11, 3)
        )
        assert [d.day for d in result.days] == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]
        empty = result.days[0]
        assert (empty.count, empty.systolic, empty.diastolic, empty.pulse, empty.zone) == (0, None, None, None, None)
        assert [d.day for d in result.measured_days] == [date(2026, 11, 2)]

    def test_day_means_and_pulse_only_where_present(self) -> None:
        result = service.daily_averages(
            [
                row(aware(2026, 11, 2, 6), 130, 80, 70),
                row(aware(2026, 11, 2, 12), 141, 85, None),
                row(aware(2026, 11, 2, 18), 140, 90, 75),
            ],
            self.TZ, date(2026, 11, 2), date(2026, 11, 2),
        )
        day = result.days[0]
        assert day.count == 3
        assert day.systolic == round(411 / 3)  # 137
        assert day.diastolic == 85
        assert day.pulse == 72  # round(72.5) — банковское, как в сводке
        assert day.zone == 1  # 137 — жёлтая; 85 — жёлтая

    def test_no_pulse_anywhere(self) -> None:
        result = service.daily_averages(
            [row(aware(2026, 11, 2, 9), 120, 80)], self.TZ, date(2026, 11, 2), date(2026, 11, 2)
        )
        assert result.days[0].pulse is None and result.pulse is None
        assert (result.systolic, result.diastolic) == (120, 80)

    def test_total_is_mean_of_unrounded_day_means(self) -> None:
        # День 1: 120.5 (→ 120), день 2: 121.5 (→ 122). Среднее неокруглённых — 121,
        # среднее округлённых тоже 121, поэтому берём пример, где они расходятся:
        # день 1: 120.4, день 2: 120.4, день 3: 121.4 → итог round(120.733) = 121,
        # а из округлённых (120, 120, 121) — round(120.33) = 120.
        measurements = []
        for day, values in ((1, (120, 120, 121, 120, 121)), (2, (120, 120, 121, 120, 121)), (3, (121, 121, 122, 121, 122))):
            for hour, value in enumerate(values):
                measurements.append(row(aware(2026, 11, day, 6 + hour), value, 80, 60 + day))
        result = service.daily_averages(measurements, self.TZ, date(2026, 11, 1), date(2026, 11, 3))
        assert [d.systolic for d in result.days] == [120, 120, 121]
        assert result.systolic == 121
        assert result.pulse == 62

    def test_each_day_weighs_equally(self) -> None:
        measurements = [row(aware(2026, 11, 1, 6 + i), 160, 100) for i in range(9)]
        measurements.append(row(aware(2026, 11, 2, 9), 120, 80))
        result = service.daily_averages(measurements, self.TZ, date(2026, 11, 1), date(2026, 11, 3))
        assert (result.systolic, result.diastolic) == (140, 90)  # не 156/98, как по всем записям

    def test_local_day_bounds(self) -> None:
        # 21:30 UTC 1 ноября — уже 2 ноября в Минске (UTC+3).
        result = service.daily_averages(
            [row(aware(2026, 11, 1, 21, 30), 150, 95)], self.TZ, date(2026, 11, 1), date(2026, 11, 2)
        )
        assert [d.count for d in result.days] == [0, 1]

    def test_dst_transition(self) -> None:
        # Берлин: 29.03.2026 переход на летнее время (сутки — 23 часа).
        tz = "Europe/Berlin"
        result = service.daily_averages(
            [
                row(aware(2026, 3, 28, 23, 30), 120, 80),  # 29.03 00:30 CET
                row(aware(2026, 3, 29, 21, 59), 130, 85),  # 29.03 23:59 CEST
                row(aware(2026, 3, 29, 22, 0), 140, 90),   # 30.03 00:00 CEST
            ],
            tz, date(2026, 3, 29), date(2026, 3, 30),
        )
        assert [d.count for d in result.days] == [2, 1]

    def test_outside_period_ignored(self) -> None:
        result = service.daily_averages(
            [row(aware(2026, 10, 20, 9), 200, 120)], self.TZ, date(2026, 11, 1), date(2026, 11, 1)
        )
        assert result.measured_days == [] and result.systolic is None

    def test_empty_period(self) -> None:
        result = service.daily_averages([], self.TZ, date(2026, 11, 1), date(2026, 11, 1))
        assert (result.systolic, result.diastolic, result.pulse) == (None, None, None)

    def test_zone_worst_of_two(self) -> None:
        assert service.day_zone(129, 84) == 0
        assert service.day_zone(130, 70) == 1
        assert service.day_zone(120, 89) == 1
        assert service.day_zone(154, 84) == 2
        assert service.day_zone(120, 90) == 2

    def test_zone_uses_rounded_values(self) -> None:
        # 129.5 → round → 130 (банковское: 130) — жёлтая зона по показанному числу.
        result = service.daily_averages(
            [row(aware(2026, 11, 2, 6), 129, 80), row(aware(2026, 11, 2, 7), 130, 80),
             row(aware(2026, 11, 2, 8), 130, 80), row(aware(2026, 11, 2, 9), 129, 80)],
            self.TZ, date(2026, 11, 2), date(2026, 11, 2),
        )
        day = result.days[0]
        assert day.zone == service.day_zone(day.systolic, day.diastolic)

    def test_chart_values_match(self) -> None:
        from bot.services import stats_chart

        measurements = [row(aware(2026, 11, 2, 9), 133, 87, 70), row(aware(2026, 11, 2, 15), 140, 90)]
        averages = service.daily_averages(measurements, self.TZ, date(2026, 11, 1), date(2026, 11, 3))
        chart = stats_chart.daily_values(measurements, self.TZ, date(2026, 11, 1), date(2026, 11, 3))
        assert [(c.systolic, c.diastolic, c.pulse) for c in chart] == [
            (d.systolic, d.diastolic, d.pulse) for d in averages.days
        ]
