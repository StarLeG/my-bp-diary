"""Приёмка: штатная остановка под нагрузкой (SPEC.md, §22; В8).

«Штатно завершить программу под нагрузкой»; «процессы или потоки остались,
файл повреждён» — отказ. Поднимаются оба процесса на одной копии стенда (как
в проде): «бот» (`qa/acceptance_common runtime`) и «кабинет» (тот же runtime
с `--serve`); кабинет получает HTTP-нагрузку, затем обоим посылается команда
`stop` (штатное завершение; в Docker это SIGTERM при `docker stop` с
`stop_grace_period: 30s`). Критерии: завершение ≤ 30 с каждый, нет оставшихся
потоков `asyncio*` / `sqlalchemy*` / `aiosqlite*` после
`scheduler.shutdown(wait=True)` и `engine.dispose()`; после остановки **обоих**
процессов `-wal` не осиротел (исчез или закрыт checkpoint'ом) и
`PRAGMA integrity_check` = `ok`. Итог —
`qa/reports/load/acceptance_shutdown.json`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    copy_stand,
    free_port,
    spawn_runtime,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: Порог штатного завершения (В8; при провале на первом прогоне — пересмотреть до 60 с).
SHUTDOWN_LIMIT_S = 30


async def _http_load(port: int, stop: asyncio.Event) -> None:
    """Нагрузка на кабинет до сигнала остановки (запросы раз в ~0,2 с)."""
    import httpx

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            while not stop.is_set():
                try:
                    await client.get("/login", timeout=5)
                except Exception:
                    return  # сервер завершается — нагрузка прекращается
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
    except Exception:
        return


async def _run(db: Path) -> dict:
    port = free_port()
    status_bot = db.parent / "status-bot.json"
    status_cabinet = db.parent / "status-cabinet.json"
    bot = await spawn_runtime(db, status_bot, serve=None)
    cabinet = await spawn_runtime(db, status_cabinet, serve=port)
    await asyncio.sleep(3)  # прогрев: планировщик и uvicorn стартуют
    load_stop = asyncio.Event()
    load_task = asyncio.create_task(_http_load(port, load_stop))

    # Штатное завершение под нагрузкой: команда `stop` обоим процессам.
    wall = {}
    for name, process in (("bot", bot), ("cabinet", cabinet)):
        started = time.perf_counter()
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
        await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_LIMIT_S + 5)
        wall[name] = time.perf_counter() - started
    load_stop.set()
    await load_task

    statuses = {}
    for name, path in (("bot", status_bot), ("cabinet", status_cabinet)):
        statuses[name] = json.loads(path.read_text(encoding="utf-8"))

    # После остановки обоих процессов: WAL и целостность общего файла.
    wal = Path(str(db) + "-wal")
    wal_ok = not wal.exists() or wal.stat().st_size == 0
    con = sqlite3.connect(db)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    return {
        "shutdown_seconds": {
            name: round(max(wall[name], statuses[name]["shutdown_seconds"]), 3) for name in statuses
        },
        "limit_seconds": SHUTDOWN_LIMIT_S,
        "threads_left": {name: statuses[name]["threads_left"] for name in statuses},
        "wal_ok": wal_ok,
        "integrity_check": integrity,
        "ok": True,  # вычисляется ниже — тест падает с понятным сообщением
    }


def test_graceful_shutdown_under_load(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-shutdown.sqlite3")
    result = asyncio.run(_run(db))
    slow = {name: seconds for name, seconds in result["shutdown_seconds"].items() if seconds > SHUTDOWN_LIMIT_S}
    threads = {name: names for name, names in result["threads_left"].items() if names}
    result["ok"] = not slow and not threads and result["wal_ok"] and result["integrity_check"] == "ok"
    write_artifact("shutdown", result)
    assert not slow, f"завершение дольше {SHUTDOWN_LIMIT_S} с: {slow}"
    assert not threads, f"остались потоки после shutdown/dispose: {threads}"
    assert result["wal_ok"], f"файл {db}-wal осиротел после остановки обоих процессов"
    assert result["integrity_check"] == "ok", f"база повреждена: {result['integrity_check']}"
