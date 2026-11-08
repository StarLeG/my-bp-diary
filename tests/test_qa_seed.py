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
