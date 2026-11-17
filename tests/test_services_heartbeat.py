"""Тесты хартбита живости бота (0.7.0, ST2-А; `bot/services/heartbeat.py`)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from bot.services import heartbeat


class TestWriteStatus:
    def test_writes_json_with_utc_timestamp(self, tmp_path) -> None:
        path = tmp_path / "landing" / "status.json"
        heartbeat.write_status(str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        moment = datetime.fromisoformat(payload["at"])
        assert moment.tzinfo is not None  # UTC, не наивное время
        assert abs((datetime.now(timezone.utc) - moment).total_seconds()) < 60

    def test_rewrites_previous_status(self, tmp_path) -> None:
        path = tmp_path / "status.json"
        heartbeat.write_status(str(path))
        first = path.read_text(encoding="utf-8")
        heartbeat.write_status(str(path))
        assert path.read_text(encoding="utf-8") != first or json.loads(first)["ok"]

    def test_empty_path_disables_status(self, tmp_path) -> None:
        heartbeat.write_status("")  # без исключений, файлов не создаёт
        assert list(tmp_path.iterdir()) == []

    def test_write_failure_is_swallowed(self, tmp_path) -> None:
        """Переполненный диск / права не роняют бота (решение ST2)."""
        blocker = tmp_path / "file-not-dir"
        blocker.write_text("x", encoding="utf-8")
        heartbeat.write_status(str(blocker / "status.json"))  # путь под файлом — нет ошибки


class TestIsFresh:
    NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    def test_fresh_within_threshold(self) -> None:
        at = (self.NOW - timedelta(seconds=heartbeat.STALE_AFTER_SECONDS)).isoformat()
        assert heartbeat.is_fresh(at, now=self.NOW) is True

    def test_stale_after_threshold(self) -> None:
        at = (self.NOW - timedelta(seconds=heartbeat.STALE_AFTER_SECONDS + 1)).isoformat()
        assert heartbeat.is_fresh(at, now=self.NOW) is False

    def test_future_timestamp_is_not_fresh(self) -> None:
        at = (self.NOW + timedelta(minutes=5)).isoformat()
        assert heartbeat.is_fresh(at, now=self.NOW) is False

    def test_naive_timestamp_treated_as_utc(self) -> None:
        at = self.NOW.replace(tzinfo=None).isoformat()
        assert heartbeat.is_fresh(at, now=self.NOW) is True
