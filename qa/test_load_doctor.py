"""Burst кабинета врача (SPEC.md, §22, уровень 4).

Врачи стенда одновременно входят по magic link (`GET /doctor?token=…`,
токен подписывается тем же `DoctorMagicLink` и секретом, что у кабинета),
открывают карточку своего пациента, делают 5 выгрузок (CSV и PDF
вперемешку) и оставляют 3 комментария: 10 запросов на врача, ровно 200 в
полном профиле. Транспорт — `httpx.ASGITransport` (кабинет в процессе
pytest, без сервера); у каждого врача свой клиент и свои cookie.

Критерии: все запросы успешны (вход — 303 на карточку своего пациента,
карточка и выгрузки — 200 с нужным типом, комментарий — 303 с
`saved=note`), p95 карточки < 2 с, комментарии привязаны к своим
`doctor_id` / `user_id`. Итог — `qa/reports/load/test_load_doctor.json`.

Калибровка (решение V3 А, без порога): перед пачкой — последовательные GET
одного врача (вход, карточка, CSV, PDF; медиана из трёх) и CPU-микробенчмарк
на чистом Python. Врачи стартуют разом, поэтому p95 карточки — доля общего
времени пачки и растёт вместе с замедлением машины (троттлинг ноутбука —
до ~3×). Калибровка в JSON и в сообщении о падении отделяет «медленную
машину» от «медленного кода». Приёмочный прогон — на машине без троттлинга
(qa/README.md, «Подготовка»).
"""
from __future__ import annotations

import asyncio
import statistics
import time
from collections import defaultdict
from typing import Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from bot.config import Settings
from bot.db import create_engine, create_session_factory
from bot.models import Doctor, DoctorNote, DoctorPatient
from qa.conftest import CPU_BENCH_REFERENCE_MS, machine_calibration
from qa.seed import doctor_email
from web.cabinet.app import create_app
from web.cabinet.doctor_auth import DoctorMagicLink

#: Секрет подписи сессий и ссылок тестового кабинета (не прод-секрет).
SECRET = "load-test-secret"

#: Порог p95 карточки пациента (SPEC §22, уровень 4).
DASHBOARD_P95_LIMIT_MS = 2000.0

#: Выгрузки и комментарии на врача (SPEC §22: 5 + 3, итого 10 запросов с входом).
EXPORTS = ("export.csv", "export.pdf", "export.csv", "export.pdf", "export.csv")
NOTES = 3

#: Калибровка: запросы одного врача по очереди (без комментариев — их число проверяется).
CALIBRATION_KINDS = ("login", "dashboard", "export.csv", "export.pdf")

#: Ориентиры калибровки на машине разработчика без троттлинга (для сравнения, не порог).
REFERENCE_CALIBRATION_MS = {
    "cpu_bench": CPU_BENCH_REFERENCE_MS,
    "login": 8.0,
    "dashboard": 44.0,
    "export.csv": 20.0,
    "export.pdf": 85.0,
}


@pytest_asyncio.fixture
async def doctor_pairs(load_db: str, load_scale) -> list[tuple[int, int]]:
    """Пары (doctor_id, user_id) активных связей нагрузочных врачей стенда."""
    engine = create_engine(load_db)
    try:
        async with create_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(Doctor.id, DoctorPatient.user_id)
                    .join(DoctorPatient, DoctorPatient.doctor_id == Doctor.id)
                    .where(
                        Doctor.email.in_([doctor_email(n) for n in range(1, load_scale.doctors + 1)]),
                        DoctorPatient.status == "active",
                    )
                    .order_by(Doctor.id)
                )
            ).all()
    finally:
        await engine.dispose()
    assert len(rows) == load_scale.doctors, "в шаблоне нет активных связей врачей"
    return [(doctor_id, user_id) for doctor_id, user_id in rows]


class DoctorSession:
    """Один врач: свой httpx-клиент, cookie и замеры запросов."""

    def __init__(self, client: httpx.AsyncClient, doctor_id: int, user_id: int) -> None:
        self.client = client
        self.doctor_id = doctor_id
        self.user_id = user_id
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.errors: list[str] = []
        self.requests = 0

    async def request(self, kind: str, method: str, url: str, **kwargs) -> Optional[httpx.Response]:
        """Запрос с замером; исключение транспорта — ошибка, а не падение флоу."""
        started = time.perf_counter()
        self.requests += 1
        try:
            return await self.client.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 — в отчёт, а не наружу
            self.errors.append(f"{kind}: {exc!r}")
            return None
        finally:
            self.timings[kind].append((time.perf_counter() - started) * 1000)

    def expect(self, kind: str, response: Optional[httpx.Response], ok: bool) -> None:
        if response is not None and not ok:
            self.errors.append(
                f"{kind}: {response.status_code} {response.headers.get('location', '')}"
            )


async def run_doctor(doctor: DoctorSession, magic: DoctorMagicLink) -> None:
    """Флоу врача: вход по ссылке → карточка → 5 выгрузок → 3 комментария."""
    patient_url = f"/doctor/patient/{doctor.user_id}"
    token = magic.make(doctor.doctor_id, doctor.user_id)
    login = await doctor.request("login", "GET", "/doctor", params={"token": token})
    doctor.expect(
        "login", login, login is not None and login.status_code == 303
        and login.headers.get("location") == patient_url
    )
    card = await doctor.request("dashboard", "GET", patient_url, params={"days": "30"})
    doctor.expect(
        "dashboard", card, card is not None and card.status_code == 200 and "bp-chart" in card.text
    )
    for name in EXPORTS:
        export = await doctor.request(name, "GET", f"{patient_url}/{name}", params={"days": "30"})
        expected = "text/csv" if name.endswith(".csv") else "application/pdf"
        doctor.expect(
            name, export, export is not None and export.status_code == 200
            and export.headers.get("content-type", "").startswith(expected)
        )
    for index in range(NOTES):
        note = await doctor.request(
            "note",
            "POST",
            f"{patient_url}/note",
            data={
                "csrf_token": doctor.client.cookies.get("csrf_token", ""),
                "text": f"Нагрузка: рекомендация {index + 1} от врача {doctor.doctor_id}",
            },
        )
        doctor.expect(
            "note", note, note is not None and note.status_code == 303
            and "saved=note" in note.headers.get("location", "")
        )


async def calibrate(
    transport: httpx.ASGITransport, magic: DoctorMagicLink, doctor_id: int, user_id: int
) -> dict[str, float]:
    """Последовательная стоимость запросов врача и CPU-микробенчмарк, мс (медианы)."""
    samples: dict[str, list[float]] = defaultdict(list)
    patient_url = f"/doctor/patient/{user_id}"
    for _ in range(3):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            doctor = DoctorSession(client, doctor_id, user_id)
            await doctor.request("login", "GET", "/doctor", params={"token": magic.make(doctor_id, user_id)})
            await doctor.request("dashboard", "GET", patient_url, params={"days": "30"})
            for name in ("export.csv", "export.pdf"):
                await doctor.request(name, "GET", f"{patient_url}/{name}", params={"days": "30"})
            for kind in CALIBRATION_KINDS:
                samples[kind].extend(doctor.timings[kind])
    result = {kind: round(statistics.median(values), 1) for kind, values in samples.items()}
    result.update(machine_calibration())
    return result


def p95(values: list[float]) -> float:
    """p95 по ближайшему рангу."""
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered) + 0.5) - 1)] if ordered else 0.0


async def test_doctors_burst(load_db, doctor_pairs, load_log, load_report, tmp_path) -> None:
    settings = Settings(
        bot_token=f"123456:{'A' * 35}",
        web_secret_key=SECRET,
        db_path=load_db,
        backup_dir=str(tmp_path / "backups"),
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    magic = DoctorMagicLink(SECRET)
    clients = [
        httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False)
        for _ in doctor_pairs
    ]
    doctors = [
        DoctorSession(client, doctor_id, user_id)
        for client, (doctor_id, user_id) in zip(clients, doctor_pairs)
    ]
    calibration = await calibrate(transport, magic, *doctor_pairs[0])
    started = time.perf_counter()
    try:
        await asyncio.gather(*(run_doctor(doctor, magic) for doctor in doctors))
    finally:
        for client in clients:
            await client.aclose()
    elapsed = time.perf_counter() - started

    engine = create_engine(load_db)
    try:
        async with create_session_factory(engine)() as session:
            notes = (
                await session.execute(
                    select(DoctorNote.doctor_id, DoctorNote.user_id, func.count())
                    .group_by(DoctorNote.doctor_id, DoctorNote.user_id)
                )
            ).all()
    finally:
        await engine.dispose()
    expected_pairs = {(doctor.doctor_id, doctor.user_id) for doctor in doctors}
    notes_by_pair = {(doctor_id, user_id): count for doctor_id, user_id, count in notes}
    misplaced = [
        {"doctor_id": doctor_id, "user_id": user_id, "notes": count}
        for (doctor_id, user_id), count in notes_by_pair.items()
        if (doctor_id, user_id) not in expected_pairs or count != NOTES
    ]
    missing = [pair for pair in expected_pairs if pair not in notes_by_pair]

    timings: dict[str, list[float]] = defaultdict(list)
    for doctor in doctors:
        for kind, values in doctor.timings.items():
            timings[kind].extend(values)
    errors = {doctor.doctor_id: doctor.errors for doctor in doctors if doctor.errors}
    requests = sum(doctor.requests for doctor in doctors)
    expected_requests = len(doctors) * (2 + len(EXPORTS) + NOTES)
    dashboard_p95 = p95(timings["dashboard"])
    report = {
        "passed": not errors
        and requests == expected_requests
        and dashboard_p95 < DASHBOARD_P95_LIMIT_MS
        and not misplaced
        and not missing,
        "doctors": len(doctors),
        "requests": requests,
        "requests_expected": expected_requests,
        "elapsed_s": round(elapsed, 3),
        "p95_ms": {kind: round(p95(values), 2) for kind, values in timings.items()},
        "dashboard_p95_limit_ms": DASHBOARD_P95_LIMIT_MS,
        "calibration_ms": calibration,
        "reference_calibration_ms": REFERENCE_CALIBRATION_MS,
        "errors": errors,
        "misplaced_notes": misplaced,
        "missing_notes": [list(pair) for pair in missing],
    }
    load_report("test_load_doctor", report)
    for doctor in doctors:
        for kind, values in doctor.timings.items():
            for ms in values:
                load_log.op(f"doctor:{kind}", user_id=doctor.user_id, duration_ms=round(ms, 2),
                            doctor_id=doctor.doctor_id)
    load_log.summary(
        f"doctor: {len(doctors)} врачей, {requests} запросов за {elapsed:.2f} с, "
        f"p95 карточки {dashboard_p95:.0f} мс, ошибок {sum(len(e) for e in errors.values())}; "
        f"калибровка: CPU {calibration['cpu_bench']:.0f} мс (×{calibration['slowdown_vs_reference']} "
        f"к эталону), PDF {calibration['export.pdf']:.0f} мс",
        **{key: value for key, value in report.items() if key not in {"errors"}},
    )
    assert not errors, f"неуспешные запросы врачей: {errors}"
    assert requests == expected_requests, f"запросов {requests}, ожидалось {expected_requests}"
    assert dashboard_p95 < DASHBOARD_P95_LIMIT_MS, (
        f"p95 карточки {dashboard_p95:.0f} мс ≥ {DASHBOARD_P95_LIMIT_MS:.0f} мс. Калибровка машины: "
        f"CPU-бенчмарк {calibration['cpu_bench']:.0f} мс — ×{calibration['slowdown_vs_reference']} к эталону "
        f"{REFERENCE_CALIBRATION_MS['cpu_bench']:.0f} мс; PDF {calibration['export.pdf']:.0f} мс "
        f"(эталон {REFERENCE_CALIBRATION_MS['export.pdf']:.0f}). Множитель ≈ 2–3 — машина в "
        "троттлинге, повторите на машине без него (qa/README.md, «Подготовка»); ≈ 1 — медленный код"
    )
    assert not misplaced, f"комментарии не на своих местах: {misplaced}"
    assert not missing, f"нет комментариев у пар (doctor_id, user_id): {missing}"
