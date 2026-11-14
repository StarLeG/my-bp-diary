"""Сид нагрузочного стенда `qa/seed` (SPEC.md, §22, «Сид данных»).

Код сидов — инфраструктура нагрузочных тестов, поэтому покрыт юнит-тестами
и входит в обычный прогон и CI: защита от запуска не на стенде, профили
масштаба, идентификаторы нагрузочных аккаунтов.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qa import seed
from qa.seed import (
    FULL,
    SMOKE,
    THREE_YEARS,
    StandGuardError,
    ensure_stand_db,
    stand_db_path,
)


class TestStandGuard:
    """Нагрузочный код работает только с БД, в имени которой есть `test-stand`."""

    @pytest.mark.parametrize(
        "path",
        [
            "data/test-stand.sqlite3",
            "test-stand.sqlite3",
            "/tmp/pytest-1/test_x0/test-stand.sqlite3",
            "data/my-test-stand-copy.sqlite3",
        ],
    )
    def test_stand_names_accepted(self, path: str) -> None:
        assert ensure_stand_db(path) == Path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "data/bot.sqlite3",  # прод
            "data/flashmob.sqlite3",  # живой флешмоб (SPEC §22)
            "data/test-stand/bot.sqlite3",  # метка в папке, а не в имени файла
            "data/teststand.sqlite3",
        ],
    )
    def test_other_names_rejected(self, path: str) -> None:
        with pytest.raises(StandGuardError) as exc_info:
            ensure_stand_db(path)
        assert "test-stand" in str(exc_info.value)

    def test_load_db_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOAD_DB", raising=False)
        assert stand_db_path() == Path("data/test-stand.sqlite3")

    def test_load_db_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOAD_DB", "data/other-test-stand.sqlite3")
        assert stand_db_path() == Path("data/other-test-stand.sqlite3")

    def test_load_db_env_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOAD_DB", "data/bot.sqlite3")
        with pytest.raises(StandGuardError):
            stand_db_path()


class TestScales:
    """Объёмы профилей — как в SPEC §22."""

    def test_full_volumes(self) -> None:
        assert FULL.patients == 20
        assert FULL.measurements == 5000
        assert FULL.patients * FULL.medications_per_patient == 100
        assert FULL.patients * FULL.logs_per_patient == 500
        assert FULL.reminders == 100
        assert FULL.patients * FULL.slot_reminders_per_patient == 60
        assert FULL.doctors == 20

    def test_smoke_is_small(self) -> None:
        assert SMOKE.patients == 3
        assert SMOKE.measurements_per_patient == 25
        assert SMOKE.doctors == 3
        assert SMOKE.measurements < FULL.measurements

    def test_scales_registry(self) -> None:
        assert seed.SCALES == {"full": FULL, "smoke": SMOKE, "three-years": THREE_YEARS}


class TestAccounts:
    """Идентификаторы нагрузочных аккаунтов — в своём диапазоне."""

    def test_patient_ids_in_clean_range(self) -> None:
        ids = [seed.patient_tg_id(n) for n in range(1, FULL.patients + 1)]
        assert ids[0] == 990_001
        assert all(tg_id in seed.PATIENT_TG_RANGE for tg_id in ids)

    def test_emails(self) -> None:
        assert seed.patient_email(7) == "load7@load.test"
        assert seed.doctor_email(3) == "doctor3@load.test"

    def test_password_only_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOAD_PASSWORD", raising=False)
        assert seed.load_password_from_env() == ""
        monkeypatch.setenv("LOAD_PASSWORD", "from-env")
        assert seed.load_password_from_env() == "from-env"


# ─────────────────────────── Сиды на временной БД стенда ───────────────────────────

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from qa.seed import SEED_SECOND, anchor_day, seed_moment  # noqa: E402
from qa.seed import seed_all as seed_all_module  # noqa: E402
from qa.seed import seed_appointments as seed_appts  # noqa: E402
from qa.seed.seed_5000_measurements import plan_measurements  # noqa: E402
from qa.seed.seed_admin import ADMIN_HINT  # noqa: E402
from qa.seed.seed_appointments import CONCLUSIONS_MAX, FORWARD_DAYS, plan_appointments  # noqa: E402
from qa.seed.seed_medications import plan_medications  # noqa: E402

#: Момент сида в тестах: напоминания — «в ближайший час» от него.
NOW = datetime(2026, 3, 20, 9, 0, tzinfo=timezone.utc)

#: Условие «нагрузочный пациент» для SQL-проверок.
LOAD = "user_id IN (SELECT id FROM users WHERE telegram_id BETWEEN 990001 AND 990999)"


@pytest.fixture
def stand(tmp_path: Path) -> Path:
    """Путь к временной БД стенда (имя проходит защиту `test-stand`)."""
    return tmp_path / "test-stand.sqlite3"


def sql(db: Path, query: str) -> list[tuple]:
    """Синхронный SELECT к файлу БД стенда."""
    con = sqlite3.connect(db)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def execute(db: Path, *statements: str) -> None:
    """Синхронно выполнить и закоммитить изменения (подготовка данных теста)."""
    con = sqlite3.connect(db)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def count(db: Path, query: str) -> int:
    """Скалярный COUNT(*)."""
    return int(sql(db, query)[0][0])


async def run_seed(db: Path, scale: seed.Scale = SMOKE, **kwargs) -> dict:
    """Сид без печати в консоль."""
    return await seed_all_module.seed_all(
        db, scale, "test-pass", now=NOW, verbose=False, **kwargs
    )


class TestSeedVolumes:
    """Дымовой профиль: объёмы и инварианты данных (SPEC §22)."""

    async def test_counts(self, stand: Path) -> None:
        await run_seed(stand)
        patients = "SELECT COUNT(*) FROM users WHERE telegram_id BETWEEN 990001 AND 990999"
        assert count(stand, patients) == 3
        assert count(stand, f"SELECT COUNT(*) FROM web_users WHERE {LOAD}") == 3
        assert count(stand, f"SELECT COUNT(*) FROM measurements WHERE {LOAD}") == SMOKE.measurements
        logs = count(stand, f"SELECT COUNT(*) FROM medication_logs WHERE {LOAD}")
        assert logs == 3 * SMOKE.logs_per_patient
        names = count(stand, "SELECT COUNT(*) FROM (SELECT DISTINCT user_id, name FROM medications)")
        assert names == 3 * SMOKE.medications_per_patient
        assert count(stand, f"SELECT COUNT(*) FROM reminders WHERE {LOAD}") == SMOKE.reminders
        active = count(stand, "SELECT COUNT(*) FROM doctor_patients WHERE status = 'active'")
        assert active == SMOKE.doctors

    async def test_measurement_invariants(self, stand: Path) -> None:
        await run_seed(stand)
        row = sql(
            stand,
            "SELECT MIN(systolic), MAX(systolic), MIN(diastolic), MAX(diastolic), "
            f"MIN(pulse), MAX(pulse), SUM(systolic - diastolic < 20) FROM measurements WHERE {LOAD}",
        )[0]
        assert 100 <= row[0] and row[1] <= 180
        assert 60 <= row[2] and row[3] <= 110
        assert 50 <= row[4] and row[5] <= 110
        assert row[6] == 0
        marked = count(
            stand,
            f"SELECT COUNT(*) FROM measurements WHERE {LOAD} "
            f"AND strftime('%S', timestamp) = '{SEED_SECOND}'",
        )
        assert marked == SMOKE.measurements, "все сидированные записи — с секундой-меткой"

    async def test_one_crisis_per_patient(self, stand: Path) -> None:
        await run_seed(stand)
        rows = sql(
            stand,
            f"SELECT COUNT(*) FROM medications WHERE {LOAD} AND slot = 'crisis' GROUP BY user_id",
        )
        assert rows == [(1,)] * 3

    async def test_reminders_within_hour(self, stand: Path) -> None:
        await run_seed(stand)
        for (raw,) in sql(stand, f"SELECT next_trigger_time FROM reminders WHERE {LOAD}"):
            trigger = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            assert NOW < trigger <= NOW + timedelta(hours=1)

    async def test_remeasure_points_to_own_measurement(self, stand: Path) -> None:
        await run_seed(stand)
        foreign = count(
            stand,
            "SELECT COUNT(*) FROM reminders r JOIN measurements m ON m.id = r.measurement_id "
            "WHERE r.type = 'med_remeasure' AND m.user_id != r.user_id",
        )
        assert foreign == 0
        assert count(stand, "SELECT COUNT(*) FROM reminders WHERE type = 'med_remeasure'") == 3


class TestSeedIdempotency:
    """Повторный запуск ничего не дублирует (ограничение 4 TEST TZ)."""

    async def test_second_run_adds_nothing(self, stand: Path) -> None:
        await run_seed(stand)
        report = await run_seed(stand)
        assert report["users"]["created"] == 0
        assert report["measurements"]["created"] == 0
        assert report["medications"]["medications_created"] == 0
        assert report["medications"]["logs_created"] == 0
        assert report["reminders"]["slots_created"] == 0
        assert report["reminders"]["remeasure_created"] == 0
        assert report["doctors"]["doctors_created"] == 0
        assert count(stand, f"SELECT COUNT(*) FROM measurements WHERE {LOAD}") == SMOKE.measurements

    async def test_new_measurements_do_not_add_remeasure(self, stand: Path) -> None:
        """Свежие записи от нагрузки не порождают лишних повторов при повторном сиде."""
        await run_seed(stand)
        execute(
            stand,
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic) "
            "SELECT id, '2026-03-20 08:59:00', 130, 85 FROM users WHERE telegram_id = 990001",
        )
        report = await run_seed(stand)
        assert report["reminders"]["remeasure_created"] == 0

    async def test_revoked_link_reactivated(self, stand: Path) -> None:
        await run_seed(stand)
        execute(stand, "UPDATE doctor_patients SET status = 'revoked'")
        report = await run_seed(stand)
        assert report["doctors"]["links_activated"] == SMOKE.doctors
        active = count(stand, "SELECT COUNT(*) FROM doctor_patients WHERE status = 'active'")
        assert active == SMOKE.doctors

    def test_schedule_is_anchored_to_creation(self) -> None:
        """Расписание пациента не зависит от дня повторного запуска."""

        class FakeUser:
            created_at = datetime(2026, 3, 1, 21, 30, tzinfo=timezone.utc)
            timezone = "Europe/Moscow"  # 2 марта 00:30 по Москве

        plan = plan_measurements(1, FakeUser(), FULL)  # type: ignore[arg-type]
        assert len(plan) == FULL.measurements_per_patient
        assert anchor_day(FakeUser.created_at, FakeUser.timezone).isoformat() == "2026-03-02"
        assert all(p.timestamp < FakeUser.created_at for p in plan), "моментов в будущем нет"
        minutes = {p.timestamp.replace(second=0) for p in plan}
        assert len(minutes) == len(plan), "одна запись в минуту"
        assert plan == plan_measurements(1, FakeUser(), FULL)  # type: ignore[arg-type]


class TestSeedClean:
    """`--clean` удаляет только нагрузочные данные."""

    async def test_clean_keeps_foreign_data(self, stand: Path) -> None:
        await run_seed(stand)
        execute(
            stand,
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic, "
            "onboarded, created_at) VALUES (555, 'Europe/Minsk', 130, 80, 1, '2026-01-01 00:00:00')",
            "INSERT INTO doctors (email, name, password_hash, created_at) "
            "VALUES ('real@clinic.by', 'Настоящий врач', 'x', '2026-01-01 00:00:00')",
        )
        report = await run_seed(stand, clean=True)
        assert report["clean"]["users"] == 3
        assert report["clean"]["doctors"] == SMOKE.doctors
        assert count(stand, "SELECT COUNT(*) FROM users WHERE telegram_id = 555") == 1
        assert count(stand, "SELECT COUNT(*) FROM doctors WHERE email = 'real@clinic.by'") == 1
        # Пересоздано заново, без хвостов прошлого сида.
        assert count(stand, f"SELECT COUNT(*) FROM measurements WHERE {LOAD}") == SMOKE.measurements
        assert count(stand, "SELECT COUNT(*) FROM doctor_patients") == SMOKE.doctors

    async def test_clean_removes_scheduler_jobs_first(self, stand: Path) -> None:
        """Job'ы напоминаний пациентов снимаются из таблицы APScheduler (CLAUDE.md)."""
        await run_seed(stand)
        reminder_ids = [row[0] for row in sql(stand, f"SELECT id FROM reminders WHERE {LOAD}")]
        inserts = [
            f"INSERT INTO apscheduler_jobs (id, next_run_time, job_state) "
            f"VALUES ('reminder:{rid}', 0, x'00')"
            for rid in [*reminder_ids, 999_999]
        ]
        execute(
            stand,
            "CREATE TABLE apscheduler_jobs (id TEXT PRIMARY KEY, next_run_time REAL, job_state BLOB)",
            *inserts,
        )
        report = await run_seed(stand, clean=True)
        assert report["clean"]["jobs"] == len(reminder_ids)
        assert sql(stand, "SELECT id FROM apscheduler_jobs") == [("reminder:999999",)]

    async def test_clean_on_empty_stand(self, stand: Path) -> None:
        report = await run_seed(stand, clean=True)
        assert (report["clean"]["jobs"], report["clean"]["users"], report["clean"]["doctors"]) == (0, 0, 0)


class TestSeedAdmin:
    async def test_admin_rows_created_and_kept_by_clean(self, stand: Path) -> None:
        report = await run_seed(stand, admin_ids="123456789, 42")
        assert report["admin"]["admins"] == [123456789, 42]
        assert "ADMIN_TELEGRAM_IDS=123456789,42" in report["admin"]["hint"]
        await run_seed(stand, clean=True)
        admins = "SELECT COUNT(*) FROM users WHERE telegram_id IN (123456789, 42) AND onboarded = 1"
        assert count(stand, admins) == 2

    async def test_admin_hint_without_ids(self, stand: Path) -> None:
        report = await run_seed(stand)
        assert report["admin"]["admins"] == []
        assert report["admin"]["hint"] == ADMIN_HINT


class TestSeedGuards:
    async def test_foreign_db_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(StandGuardError):
            await seed_all_module.seed_all(tmp_path / "bot.sqlite3", SMOKE, "x", verbose=False)
        assert not (tmp_path / "bot.sqlite3").exists(), "в чужую БД ничего не записано"

    async def test_password_required(self, stand: Path) -> None:
        with pytest.raises(ValueError):
            await seed_all_module.seed_all(stand, SMOKE, "", verbose=False)

    async def test_verbose_prints_steps(self, stand: Path, capsys) -> None:
        await seed_all_module.seed_all(stand, SMOKE, "x", now=NOW)
        assert "[seed] measurements:" in capsys.readouterr().out


class TestSeedDoctors:
    async def test_doctor_without_patient_has_no_link(self, stand: Path) -> None:
        """Врачей больше, чем пациентов: лишний врач создаётся без связи."""
        scale = seed.Scale(
            name="custom", patients=1, measurements_per_patient=3, medications_per_patient=2,
            logs_per_patient=2, slot_reminders_per_patient=1, remeasure_per_patient=1, doctors=2,
        )
        report = await run_seed(stand, scale)
        assert report["doctors"]["doctors_created"] == 2
        assert report["doctors"]["links_created"] == 1


class TestSeedPlans:
    def test_medication_plan(self) -> None:
        rows = plan_medications(1, FULL)
        names = {name for name, _, _ in rows}
        assert len(names) == FULL.medications_per_patient
        assert [slot for _, _, slot in rows].count("crisis") == 1
        per_name = {name: sum(1 for n, _, _ in rows if n == name) for name in names}
        assert all(1 <= c <= 2 for c in per_name.values())

    def test_seed_moment_local_time(self) -> None:
        moment = seed_moment(datetime(2026, 3, 1).date(), 8, 5, "Asia/Yekaterinburg")
        assert moment == datetime(2026, 3, 1, 3, 5, SEED_SECOND, tzinfo=timezone.utc)


class TestSeedCli:
    def test_cli_seeds_smoke(self, stand: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setenv("LOAD_PASSWORD", "cli-pass")
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        assert seed_all_module.main(["--db", str(stand), "--scale", "smoke"]) == 0
        assert "готово" in capsys.readouterr().out
        assert count(stand, f"SELECT COUNT(*) FROM measurements WHERE {LOAD}") == SMOKE.measurements

    def test_cli_uses_load_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "nested" / "test-stand.sqlite3"
        monkeypatch.setenv("LOAD_DB", str(target))
        monkeypatch.setenv("LOAD_PASSWORD", "cli-pass")
        assert seed_all_module.main(["--scale", "smoke"]) == 0
        assert target.exists()

    def test_cli_rejects_foreign_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOAD_PASSWORD", "cli-pass")
        with pytest.raises(SystemExit) as exc_info:
            seed_all_module.main(["--db", str(tmp_path / "bot.sqlite3")])
        assert exc_info.value.code == 2

    def test_cli_requires_password(self, stand: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOAD_PASSWORD", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            seed_all_module.main(["--db", str(stand)])
        assert exc_info.value.code == 2


@pytest.mark.real_consent
class TestSeedConsents:
    """Стенд с 0.5.0 (§6.11): пациенты и врачи — с согласиями, врач видит пациентов."""

    async def test_consents_seeded_once(self, stand: Path) -> None:
        await run_seed(stand)
        await run_seed(stand)  # повторный прогон — без дублей
        patients = count(stand, "SELECT COUNT(*) FROM users WHERE telegram_id BETWEEN 990001 AND 990999")
        processing = count(stand, f"SELECT COUNT(*) FROM consents WHERE kind = 'processing' AND {LOAD}")
        assert processing == patients
        links = count(stand, "SELECT COUNT(*) FROM doctor_patients WHERE status = 'active'")
        doctor = count(stand, "SELECT COUNT(*) FROM consents WHERE kind = 'doctor' AND withdrawn_at IS NULL")
        assert doctor == links
        assert count(stand, "SELECT COUNT(*) FROM doctors WHERE consent_at IS NULL") == 0


# ─────────────────── Профиль «три года» (авто-приёмка, SPEC §22) ───────────────────


class FakePatient:
    """Пациент для чистых планов: фиксированные момент создания и пояс."""

    created_at = datetime(2026, 3, 1, 21, 30, tzinfo=timezone.utc)
    timezone = "Europe/Moscow"  # 2 марта 00:30 по Москве → anchor = 2026-03-02


def plus_two_years(day: date) -> date:
    """«День + 2 года» (§6.13); 29 февраля — последний день февраля."""
    try:
        return day.replace(year=day.year + 2)
    except ValueError:
        return day.replace(year=day.year + 2, month=2, day=28)


@pytest.fixture(scope="class")
def three_stand(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Стенд «три года» на временной БД стенда: один `seed_all` на класс тестов."""
    db = tmp_path_factory.mktemp("three-years") / "test-stand.sqlite3"
    asyncio.run(seed_all_module.seed_all(db, THREE_YEARS, "test-pass", now=NOW, verbose=False))
    return db


class TestThreeYearsScale:
    """Профиль «три года»: ~50 000 измерений; масштабируемые и фиксированные таблицы."""

    def test_volume_about_50k(self) -> None:
        assert THREE_YEARS.patients == 20
        assert THREE_YEARS.measurements_per_patient == 2500
        assert abs(THREE_YEARS.measurements - 50_000) <= 50_000 * 0.05  # ~50k ± 5%
        assert THREE_YEARS.days == 1095  # история за 3 года

    def test_scaled_tables_grow(self) -> None:
        """Масштабируем: измерения, лекарства, логи приёма, визиты и заключения."""
        assert THREE_YEARS.measurements > FULL.measurements
        assert THREE_YEARS.medications_per_patient > FULL.medications_per_patient
        assert THREE_YEARS.logs_per_patient > FULL.logs_per_patient
        assert THREE_YEARS.appointments_per_patient > 0  # визиты → и заключения

    def test_not_scaled_same_as_full(self) -> None:
        """Не масштабируем: иначе получим нереалистичные строки (В1)."""
        assert THREE_YEARS.reminders == FULL.reminders == 100  # активные не накапливаются
        assert THREE_YEARS.doctors == FULL.doctors == 20  # связи врач–пациент


class TestThreeYearsPlans:
    """Планы «три года»: окна дат, детерминизм, вымышленные данные."""

    def test_measurements_span_three_years(self) -> None:
        plan = plan_measurements(1, FakePatient(), THREE_YEARS)  # type: ignore[arg-type]
        assert len(plan) == THREE_YEARS.measurements_per_patient
        assert all(p.timestamp < FakePatient.created_at for p in plan), "моментов в будущем нет"
        first_day = anchor_day(FakePatient.created_at, FakePatient.timezone) - timedelta(
            days=THREE_YEARS.days
        )
        per_day: dict[date, int] = {}
        for planned in plan:
            local = planned.timestamp.astimezone(ZoneInfo(FakePatient.timezone)).date()
            per_day[local] = per_day.get(local, 0) + 1
        assert set(per_day) == {
            first_day + timedelta(days=d) for d in range(THREE_YEARS.days)
        }, "записи — во всех 1095 днях окна"
        assert max(per_day.values()) == 3 and min(per_day.values()) == 2, "2–3 измерения в день"

    def test_measurement_values_in_ranges(self) -> None:
        plan = plan_measurements(1, FakePatient(), THREE_YEARS)  # type: ignore[arg-type]
        for planned in plan:
            assert 100 <= planned.systolic <= 180
            assert 60 <= planned.diastolic <= 110
            assert 50 <= planned.pulse <= 110
            assert planned.systolic - planned.diastolic >= 20

    def test_appointments_within_two_years(self) -> None:
        anchor = anchor_day(FakePatient.created_at, FakePatient.timezone)
        plan = plan_appointments(1, FakePatient(), THREE_YEARS)  # type: ignore[arg-type]
        assert len(plan) == THREE_YEARS.appointments_per_patient
        for planned in plan:
            assert anchor - timedelta(days=THREE_YEARS.days) <= planned.starts_on
            assert planned.starts_on <= anchor + timedelta(days=FORWARD_DAYS)
            assert planned.starts_on <= plus_two_years(anchor), "не дальше «сегодня + 2 года» (§6.13)"
            if planned.starts_on >= anchor:
                assert planned.status == "planned"
            else:
                assert planned.status in ("done", "missed", "moved", "cancelled")
        upcoming = [p for p in plan if p.starts_on >= anchor]
        assert 0 < len(upcoming) <= 30, "в «Скоро» — не больше лимита 30 событий (§6.13)"

    def test_conclusions_on_done_visits_only(self) -> None:
        anchor = anchor_day(FakePatient.created_at, FakePatient.timezone)
        plan = plan_appointments(1, FakePatient(), THREE_YEARS)  # type: ignore[arg-type]
        done = [p for p in plan if p.status == "done"]
        assert done
        for planned in plan:
            if planned.status != "done":
                assert planned.conclusions == (), "заключения — только к прошедшим визитам"
                continue
            assert planned.starts_on < anchor
            assert 1 <= len(planned.conclusions) <= CONCLUSIONS_MAX, "до 5 на событие (§4.17)"
            texts = [c.text for c in planned.conclusions]
            assert len(set(texts)) == len(texts), "тексты внутри события не повторяются"
            for text in texts:
                assert text in seed_appts._CONCLUSION_TEXTS, "вымышленные фразы врача"
                assert 1 <= len(text) <= 4000

    def test_fictional_titles_and_notes(self) -> None:
        plan = plan_appointments(1, FakePatient(), THREE_YEARS)  # type: ignore[arg-type]
        for planned in plan:
            assert planned.title in seed_appts._TITLES[planned.kind], "подсказки шага 2 (§6.13)"
            assert 1 <= len(planned.title) <= 60
            assert planned.note in seed_appts._NOTES

    def test_plans_are_deterministic(self) -> None:
        """Seeded RNG: повторный прогон даёт те же данные (В1)."""
        assert plan_measurements(1, FakePatient(), THREE_YEARS) == plan_measurements(  # type: ignore[arg-type]
            1, FakePatient(), THREE_YEARS  # type: ignore[arg-type]
        )
        assert plan_appointments(1, FakePatient(), THREE_YEARS) == plan_appointments(  # type: ignore[arg-type]
            1, FakePatient(), THREE_YEARS  # type: ignore[arg-type]
        )


class TestThreeYearsSeed:
    """Стенд «три года» на временной БД: объёмы, даты, не-масштабируемые таблицы."""

    def test_counts(self, three_stand: Path) -> None:
        measurements = count(three_stand, f"SELECT COUNT(*) FROM measurements WHERE {LOAD}")
        assert abs(measurements - 50_000) <= 50_000 * 0.05  # ~50k ± 5%
        assert measurements == THREE_YEARS.measurements
        logs = count(three_stand, f"SELECT COUNT(*) FROM medication_logs WHERE {LOAD}")
        assert logs == THREE_YEARS.patients * THREE_YEARS.logs_per_patient
        names = count(
            three_stand,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT user_id, name FROM medications WHERE {LOAD})",
        )
        assert names == THREE_YEARS.patients * THREE_YEARS.medications_per_patient
        appointments = count(three_stand, f"SELECT COUNT(*) FROM appointments WHERE {LOAD}")
        assert appointments == THREE_YEARS.patients * THREE_YEARS.appointments_per_patient
        conclusions = count(three_stand, "SELECT COUNT(*) FROM appointment_conclusions")
        assert conclusions > 0, "заключения — к прошедшим визитам"
        assert count(three_stand, f"SELECT COUNT(*) FROM web_users WHERE {LOAD}") == THREE_YEARS.patients

    def test_measurements_invariants(self, three_stand: Path) -> None:
        row = sql(
            three_stand,
            "SELECT MIN(systolic), MAX(systolic), MIN(diastolic), MAX(diastolic), "
            f"MIN(pulse), MAX(pulse), SUM(systolic - diastolic < 20) FROM measurements WHERE {LOAD}",
        )[0]
        assert 100 <= row[0] and row[1] <= 180
        assert 60 <= row[2] and row[3] <= 110
        assert 50 <= row[4] and row[5] <= 110
        assert row[6] == 0
        marked = count(
            three_stand,
            f"SELECT COUNT(*) FROM measurements WHERE {LOAD} "
            f"AND strftime('%S', timestamp) = '{SEED_SECOND}'",
        )
        assert marked == THREE_YEARS.measurements, "все сидированные записи — с секундой-меткой"

    def test_visit_dates(self, three_stand: Path) -> None:
        created_raw, tz_name = sql(
            three_stand,
            f"SELECT created_at, timezone FROM users WHERE telegram_id = {seed.patient_tg_id(1)}",
        )[0]
        anchor = anchor_day(
            datetime.fromisoformat(created_raw).replace(tzinfo=timezone.utc), tz_name
        )
        for raw_day, status in sql(
            three_stand, f"SELECT starts_on, status FROM appointments WHERE {LOAD}"
        ):
            day = date.fromisoformat(raw_day)
            assert anchor - timedelta(days=THREE_YEARS.days) <= day <= anchor + timedelta(days=FORWARD_DAYS)
            assert day <= plus_two_years(anchor), "никаких дат > «сегодня + 2 года»"
            if day >= anchor:
                assert status == "planned"
            else:
                assert status != "planned"
        upcoming = sql(
            three_stand,
            f"SELECT COUNT(*) FROM appointments WHERE {LOAD} AND status = 'planned' GROUP BY user_id",
        )
        assert upcoming and all(count_per_user <= 30 for (count_per_user,) in upcoming)

    def test_conclusions(self, three_stand: Path) -> None:
        rows = sql(
            three_stand,
            "SELECT appointment_id, COUNT(*), MAX(length(text)) "
            "FROM appointment_conclusions GROUP BY appointment_id",
        )
        for _, per_event, longest in rows:
            assert 1 <= per_event <= CONCLUSIONS_MAX, "до 5 заключений на событие (§4.17)"
            assert 1 <= longest <= 4000
        foreign = count(
            three_stand,
            "SELECT COUNT(*) FROM appointment_conclusions c "
            "JOIN appointments a ON a.id = c.appointment_id WHERE a.status != 'done'",
        )
        assert foreign == 0, "заключения — только к прошедшим визитам со статусом done"

    def test_not_scaled_counters(self, three_stand: Path) -> None:
        """`reminders`, `doctor_patients`, `web_sessions`, `invite_log` — как в FULL."""
        reminders = count(three_stand, f"SELECT COUNT(*) FROM reminders WHERE {LOAD}")
        assert reminders == FULL.reminders == 100
        active = count(three_stand, "SELECT COUNT(*) FROM doctor_patients WHERE status = 'active'")
        assert active == FULL.doctors == 20
        assert count(three_stand, "SELECT COUNT(*) FROM web_sessions") == 0
        assert count(three_stand, "SELECT COUNT(*) FROM invite_log") == 0

    def test_second_run_gives_identical_data(self, three_stand: Path) -> None:
        """Два запуска → идентичные данные: второй ничего не добавляет и не меняет."""
        tables = (
            "users",
            "web_users",
            "measurements",
            "medications",
            "medication_logs",
            "appointments",
            "appointment_conclusions",
            "reminders",
            "doctor_patients",
        )
        before = {t: sql(three_stand, f"SELECT * FROM {t} ORDER BY 1") for t in tables}
        report = asyncio.run(
            seed_all_module.seed_all(three_stand, THREE_YEARS, "test-pass", now=NOW, verbose=False)
        )
        assert report["users"]["created"] == 0
        assert report["measurements"]["created"] == 0
        assert report["medications"]["medications_created"] == 0
        assert report["medications"]["logs_created"] == 0
        assert report["appointments"]["created"] == 0
        assert report["appointments"]["conclusions_created"] == 0
        assert report["reminders"]["slots_created"] == 0
        assert report["reminders"]["remeasure_created"] == 0
        assert report["doctors"]["doctors_created"] == 0
        assert report["doctors"]["links_created"] == 0
        after = {t: sql(three_stand, f"SELECT * FROM {t} ORDER BY 1") for t in tables}
        assert after == before
