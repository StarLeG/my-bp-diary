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
        assert seed.SCALES == {"full": FULL, "smoke": SMOKE}


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

import sqlite3  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from qa.seed import SEED_SECOND, anchor_day, seed_moment  # noqa: E402
from qa.seed import seed_all as seed_all_module  # noqa: E402
from qa.seed.seed_5000_measurements import plan_measurements  # noqa: E402
from qa.seed.seed_admin import ADMIN_HINT  # noqa: E402
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
