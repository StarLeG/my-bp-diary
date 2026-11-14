"""Общее для проверок авто-приёмки (SPEC.md, §22 «Приёмка перед релизом»).

Шесть долгих проверок (`qa/test_acceptance_*.py`) запускаются только под
`ACCEPTANCE=1`, на своей машине — перед релизом и после правок хранения данных
(`make acceptance`; ответы — `doc/LOAD-CRITERIA-QUESTIONS.md`). Здесь — их
общие инструменты:

- копии стенда через `sqlite3.Connection.backup` (каждый модуль независим);
- метрики: p95 (ближайший ранг), наклон линейной регрессии, RSS процесса;
- артефакты `qa/reports/load/acceptance_<тест>.json` для
  `qa/test_acceptance_regression.py`;
- **runtime** (`python -m qa.acceptance_common runtime …`) — процесс «бот»
  (движок + прод-планировщик `build_scheduler` + тик раз в минуту) и/или
  «кабинет» (uvicorn программно) на копии стенда: для замера RSS («Длительная
  работа») и штатного завершения («Остановка»).

Штатное завершение runtime — команда `stop` в stdin (кросс-платформенно; в
Docker это SIGTERM при `docker stop`, см. `stop_grace_period: 30s`):
`scheduler.shutdown(wait=True)` → `engine.dispose()` (у `--serve` — ещё
`server.should_exit`), затем в `--status` пишутся оставшиеся потоки и время.

Данные стенда — вымышленные (сид `qa/seed/`), БД берётся из `LOAD_DB`
(по умолчанию `data/test-stand.sqlite3`); guard — «test-stand» в имени файла.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import socket
import sqlite3
import statistics
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any, Optional, Sequence

#: Флаг запуска проверок приёмки (в обычном прогоне тесты SKIP).
ACCEPTANCE_ENV = "ACCEPTANCE"

#: БД стенда (копируется в каждую проверку; guard — «test-stand» в имени).
LOAD_DB_ENV = "LOAD_DB"
DEFAULT_STAND_DB = Path("data") / "test-stand.sqlite3"

#: Артефакты приёмки (JSON для `test_acceptance_regression.py`).
REPORTS_DIR = Path("qa") / "reports" / "load"

#: Минимальное число измерений профиля «три года» (~50 000, В2): меньше —
#: стенд не засеян `seed_all.py --scale three-years`.
VOLUME_MIN_MEASUREMENTS = 45_000

#: Имена потоков, которых не должно остаться после штатного завершения (В8).
LEFTOVER_THREAD_PREFIXES = ("asyncio", "sqlalchemy", "aiosqlite")


def stand_db_path() -> Path:
    """Путь БД стенда; чужие файлы (без «test-stand») не трогаются."""
    raw = os.environ.get(LOAD_DB_ENV, "").strip() or str(DEFAULT_STAND_DB)
    path = Path(raw)
    if "test-stand" not in path.name:
        raise AssertionError(f"{LOAD_DB_ENV}={path}: в имени файла должно быть «test-stand»")
    return path


def copy_stand(dest: Path) -> Path:
    """Копия стенда через Online Backup API SQLite (согласованная, не cp).

    Копия мигрируется до текущего поколения схемы (`init_db`, §4): проверки
    открывают её напрямую через `create_engine`, минуя старт приложения, и
    модели должны читаться из любой схемы стенда (0.6.0+: поколение 11,
    `users.meds_ask_shown_at`, `diary_events`).
    """
    source = stand_db_path()
    if not source.exists():
        raise AssertionError(f"нет БД стенда {source}: сначала `python -m qa.seed.seed_all --scale three-years`")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    asyncio.run(_migrate_copy(dest))
    return dest


async def _migrate_copy(db: Path) -> None:
    """Довести копию стенда до `SCHEMA_VERSION` тем же путём, что и бот (§4)."""
    from bot.db import create_engine, init_db

    engine = create_engine(str(db))
    try:
        await init_db(engine)
    finally:
        await engine.dispose()


def require_volume(db: Path) -> int:
    """Проверить профиль «три года»: ≥ 45 000 измерений (В1–В2)."""
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    finally:
        con.close()
    if count < VOLUME_MIN_MEASUREMENTS:
        raise AssertionError(
            f"стенд не засеян профилем «три года»: измерений {count} < {VOLUME_MIN_MEASUREMENTS}; "
            f"запустите `python -m qa.seed.seed_all --db {db} --scale three-years`"
        )
    return count


def write_artifact(name: str, payload: dict) -> Path:
    """Итог проверки в `qa/reports/load/acceptance_<name>.json` (В9)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"acceptance_{name}.json"
    document = {
        "name": name,
        "profile": "acceptance",
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **payload,
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def p95(values: Sequence[float]) -> float:
    """p95 по ближайшему рангу (как в `qa/test_load_doctor.py`)."""
    if not values:
        raise ValueError("нет значений для p95")
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def slope_per_unit(points: Sequence[tuple[float, float]]) -> float:
    """Наклон линейной регрессии методом наименьших квадратов.

    `points` — пары (x, y): для памяти — (минута, МБ). Пустой ряд или нулевой
    разброс x — 0,0 (роста нет).
    """
    if len(points) < 2:
        return 0.0
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def rss_mb(pid: Optional[int] = None) -> float:
    """RSS процесса в МБ (psutil); без pid — текущий процесс."""
    import psutil

    process = psutil.Process(pid) if pid is not None else psutil.Process()
    return process.memory_info().rss / (1024 * 1024)


def first_patient(db: Path) -> tuple[int, int]:
    """`(id, telegram_id)` первого пациента сида (нагрузочный диапазон 990 001…)."""
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT id, telegram_id FROM users WHERE telegram_id BETWEEN 990001 AND 990099"
            " ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise AssertionError(f"в {db} нет нагрузочных пациентов (990 001…): сначала сид")
    return int(row[0]), int(row[1])


async def session_cookie(db: Path) -> str:
    """Cookie кабинета для пациента сида — сессия создаётся напрямую (без пароля)."""
    from bot.db import create_engine, create_session_factory
    from web.cabinet import auth

    web_user_id = _first_web_user_id(db)
    engine = create_engine(str(db))
    try:
        async with create_session_factory(engine)() as session:
            token = await auth.create_web_session(session, web_user_id)
            await session.commit()
    finally:
        await engine.dispose()
    return token


def _first_web_user_id(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT id FROM web_users ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    if row is None:
        raise AssertionError(f"в {db} нет `web_users`: сидите стенд профиля «три года»")
    return int(row[0])


def make_app(db: Path, backup_dir: Path):
    """ASGI-приложение кабинета на копии стенда (без сети и Telegram)."""
    from bot.config import Settings
    from web.cabinet.app import create_app

    settings = Settings(
        bot_token=f"123456:{'A' * 35}",
        web_secret_key="acceptance-secret",
        db_path=str(db),
        backup_dir=str(backup_dir),
    )
    return create_app(settings)


def leftover_threads() -> list[str]:
    """Имена потоков, которых не должно быть после завершения (В8)."""
    names = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    return sorted(n for n in names if n.startswith(LEFTOVER_THREAD_PREFIXES))


def free_port() -> int:
    """Свободный TCP-порт localhost для runtime «кабинет»."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def spawn_runtime(
    db: Path, status: Path, serve: Optional[int] = None, heap_log: Optional[Path] = None
):
    """Поднять runtime («бот» или «кабинет» с `--serve`) подпроцессом (В5, В8)."""
    command = [
        sys.executable, "-m", "qa.acceptance_common", "runtime",
        "--db", str(db), "--status", str(status),
    ]
    if serve is not None:
        command += ["--serve", str(serve)]
    if heap_log is not None:
        command += ["--heap-log", str(heap_log)]
    return await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def spawn_uvicorn(db: Path, port: int, workers: int = 2):
    """Реальный сервер кабинета — как в проде (`uvicorn --workers N`).

    Окно конкуренции меряет реальную конфигурацию: рендер кабинета CPU-bound,
    и один процесс сериализует запросы (SPEC §22, «Окно конкуренции»).
    """
    import sys

    env = {
        **os.environ,
        "BOT_TOKEN": f"123456:{'A' * 35}",
        "WEB_SECRET_KEY": "acceptance-secret",
        "DB_PATH": str(db),
        "BACKUP_DIR": str(db.parent / "acceptance-backups"),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "web.cabinet.app:app",
        "--host", "127.0.0.1", "--port", str(port), "--workers", str(workers),
        "--log-level", "warning",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


# ─────────────────────────── Runtime «бот» / «кабинет» ───────────────────────────

#: Движок процесса runtime; `SQLAlchemyJobStore` сериализует job'ы, поэтому
#: тик — функция уровня модуля с текстовой ссылкой (`qa.acceptance_common:_runtime_tick`).
_RUNTIME_ENGINE = None


async def _runtime_tick() -> None:
    """Лёгкая работа раз в минуту — как прод-тик планировщика (§6.7)."""
    from sqlalchemy import func, select

    from bot.models import Measurement

    async with _RUNTIME_ENGINE.connect() as conn:
        await conn.execute(select(func.count()).select_from(Measurement))


async def _run_runtime(db: Path, serve: Optional[int], status: Path, heap_log: Optional[Path]) -> None:
    """Процесс под наблюдением: планировщик бота и/или uvicorn кабинета.

    `serve` — порт: поднимается uvicorn на копии стенда («кабинет»); без него —
    «бот» (движок + `build_scheduler` + тик раз в минуту). Команда `stop` в
    stdin — штатное завершение; в `--status` — JSON с временем завершения и
    оставшимися потоками.
    """
    from bot.db import create_engine
    from bot.main import build_scheduler

    global _RUNTIME_ENGINE
    engine = create_engine(str(db))
    _RUNTIME_ENGINE = engine
    # Планировщик — только в режиме «бот»: в проде кабинет его не запускает,
    # а два процесса на одном jobstore гонятся за `CREATE TABLE apscheduler_jobs`.
    scheduler = build_scheduler(str(db)) if serve is None else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: сигналов нет — останавливаем командой `stop`

    def _stdin_reader() -> None:
        """Команда `stop` в stdin — демон-потоком: ожидающий `input()` поток
        executor'а не дал бы `shutdown_default_executor()` завершиться."""
        try:
            input()
        except EOFError:
            return
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=_stdin_reader, name="acceptance-stdin", daemon=True).start()
    if scheduler is not None:
        scheduler.start()  # APScheduler 3.x: start()/shutdown() — обычные методы
        scheduler.add_job(
            "qa.acceptance_common:_runtime_tick",
            "interval",
            minutes=1,
            id="acceptance:tick",
            replace_existing=True,
        )
    server = None
    server_task = None
    if serve is not None:
        import uvicorn

        from bot.config import Settings
        from web.cabinet.app import create_app

        app = create_app(
            Settings(
                bot_token=f"123456:{'A' * 35}",
                web_secret_key="acceptance-secret",
                db_path=str(db),
                backup_dir=str(db.parent / "acceptance-backups"),
            )
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=serve, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())

    heap_task = None
    if heap_log is not None:
        tracemalloc.start()

        async def _heap() -> None:
            while not stop.is_set():
                try:
                    with heap_log.open("a", encoding="utf-8") as out:
                        out.write(
                            json.dumps(
                                {
                                    "minute": time.time(),
                                    "heap_mb": tracemalloc.get_traced_memory()[0] / (1024 * 1024),
                                }
                            )
                            + "\n"
                        )
                except OSError:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=300)
                except asyncio.TimeoutError:
                    continue

        heap_task = asyncio.create_task(_heap())

    await stop.wait()  # сигнал или команда `stop`
    started = time.perf_counter()
    # Штатное завершение (В8): сервер (lifespan приложения закрывает его
    # движок) → планировщик → наш движок → фоновые службы loop. Потоки
    # aiosqlite/SQLAlchemy умирают асинхронно — ждём их до 5 с, «остались»
    # только пережившие ожидание.
    if server_task is not None:
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
    if scheduler is not None:
        scheduler.shutdown(wait=True)  # 3.x: синхронный метод (прод-код — так же)
    await engine.dispose()
    if heap_task is not None:
        heap_task.cancel()
        await asyncio.gather(heap_task, return_exceptions=True)
        tracemalloc.stop()
    await asyncio.get_running_loop().shutdown_default_executor()
    for _ in range(50):
        if not leftover_threads():
            break
        await asyncio.sleep(0.1)
    shutdown_seconds = time.perf_counter() - started
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(
        json.dumps(
            {
                "shutdown_seconds": round(shutdown_seconds, 3),
                "threads_left": leftover_threads(),
                "all_threads": sorted(t.name for t in threading.enumerate()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Runtime авто-приёмки (SPEC §22)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    runtime = sub.add_parser("runtime", help="процесс «бот»/«кабинет» для RSS и остановки")
    runtime.add_argument("--db", required=True)
    runtime.add_argument("--serve", type=int, default=None, help="порт uvicorn (кабинет)")
    runtime.add_argument("--status", required=True, help="JSON итога штатного завершения")
    runtime.add_argument("--heap-log", default=None, help="JSONL снимков Python-heap раз в 5 мин")
    args = parser.parse_args()
    asyncio.run(_run_runtime(Path(args.db), args.serve, Path(args.status), Path(args.heap_log) if args.heap_log else None))


if __name__ == "__main__":
    main()
