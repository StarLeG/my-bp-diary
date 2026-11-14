"""Юнит-тесты инструментов авто-приёмки (`qa/acceptance_common.py`, SPEC §22).

Сами проверки приёмки (`qa/test_acceptance_*.py`) запускаются только под
`ACCEPTANCE=1` и в `testpaths` не входят; чистые хелперы — метрики, артефакты,
копии стенда — покрываются здесь.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qa import acceptance_common as common


class TestP95:
    def test_nearest_rank(self) -> None:
        assert common.p95([float(n) for n in range(1, 101)]) == 95.0

    def test_small_sample(self) -> None:
        assert common.p95([10.0, 20.0, 30.0]) == 30.0

    def test_empty(self) -> None:
        with pytest.raises(ValueError):
            common.p95([])


class TestSlope:
    def test_linear(self) -> None:
        points = [(0.0, 1.0), (1.0, 3.0), (2.0, 5.0), (3.0, 7.0)]
        assert common.slope_per_unit(points) == pytest.approx(2.0)

    def test_flat(self) -> None:
        assert common.slope_per_unit([(0.0, 5.0), (1.0, 5.0)]) == pytest.approx(0.0)

    def test_too_few_points(self) -> None:
        assert common.slope_per_unit([(0.0, 1.0)]) == 0.0


class TestArtifact:
    def test_written(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(common, "REPORTS_DIR", tmp_path)
        path = common.write_artifact("demo", {"metric": 1})
        document = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == "acceptance_demo.json"
        assert document["name"] == "demo" and document["profile"] == "acceptance"
        assert document["metric"] == 1 and document["finished_at"]


class TestStandDb:
    def test_guard_rejects_foreign_files(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(common.LOAD_DB_ENV, str(tmp_path / "prod.sqlite3"))
        with pytest.raises(AssertionError, match="test-stand"):
            common.stand_db_path()

    def test_accepts_stand(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "some-test-stand.sqlite3"
        monkeypatch.setenv(common.LOAD_DB_ENV, str(target))
        assert common.stand_db_path() == target


class TestCopyStand:
    def test_backup_copy(self, tmp_path, monkeypatch) -> None:
        source = tmp_path / "origin-test-stand.sqlite3"
        con = sqlite3.connect(source)
        con.execute("CREATE TABLE t (x)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
        con.close()
        monkeypatch.setenv(common.LOAD_DB_ENV, str(source))
        dest = common.copy_stand(tmp_path / "copy.sqlite3")
        copied = sqlite3.connect(dest)
        try:
            assert copied.execute("SELECT x FROM t").fetchone()[0] == 42
        finally:
            copied.close()

    def test_missing_source(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(common.LOAD_DB_ENV, str(tmp_path / "absent-test-stand.sqlite3"))
        with pytest.raises(AssertionError, match="нет БД стенда"):
            common.copy_stand(tmp_path / "copy.sqlite3")


class TestRequireVolume:
    def test_small_stand_rejected(self, tmp_path, monkeypatch) -> None:
        db = tmp_path / "small-test-stand.sqlite3"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE measurements (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        monkeypatch.setattr(common, "VOLUME_MIN_MEASUREMENTS", 2)
        with pytest.raises(AssertionError, match="--scale three-years"):
            common.require_volume(db)

    def test_big_stand_accepted(self, tmp_path, monkeypatch) -> None:
        db = tmp_path / "big-test-stand.sqlite3"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE measurements (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO measurements VALUES (?)", [(n,) for n in range(3)])
        con.commit()
        con.close()
        monkeypatch.setattr(common, "VOLUME_MIN_MEASUREMENTS", 2)
        assert common.require_volume(db) == 3
