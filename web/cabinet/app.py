"""Личный кабинет: FastAPI + Jinja2 (SPEC.md, раздел 16.2).

Запуск отдельным процессом рядом с ботом: `uvicorn web.cabinet.app:app`
(HTTPS — за reverse proxy с Let's Encrypt; cookie получают флаг Secure при
HTTPS-доступе — по схеме запроса или `CABINET_URL`. По локальному HTTP флаг
снимается: иначе клиент не возвращает Secure-куки обратно и все формы
падают с 403 на CSRF-проверке). Одна SQLite в WAL с ботом; сессия БД на
HTTP-запрос (`Depends`); модели — из `bot/models.py` (SPEC.md, раздел 16.3).

Содержимое: личная карточка, история с фильтрами, сводка с графиком и CSV,
CRUD лекарств, настройки напоминаний, добавление/правка/удаление записей и
импорт CSV архива (§16.2). Платёжных данных нет — только ссылка на внешнюю
страницу поддержки из бота.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile  # request.form() возвращает starlette-экземпляр
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import schemas, texts
from bot.config import Settings, get_settings
from bot.db import create_engine, create_session_factory, init_db
from bot.models import Doctor, Measurement, Medication, Reminder, User, WebSession, WebUser
from bot.services import consent as consent_service
from bot.services import csv_export
from bot.services import doctor as doctor_service
from bot.services import appointments as appointments_service
from bot.services import medications as medications_service
from bot.services import pdf_report
from bot.services import profile as profile_service
from bot.services import stats
from bot.services.measurements import (
    DuplicateMeasurementError,
    delete_measurement,
    get_measurement,
    measurements_for_period,
    save_measurement,
    update_measurement_values,
)
from bot.services.medications import MedicationNotFoundError
from bot.services.reminders import DEFAULT_SLOT_TIMES, enable_slot_row, next_slot_occurrence
from web.cabinet import auth, csv_import, doctor_auth
from web.cabinet.chart_zones import CHART_ZONES, day_zone, row_zone, table_legend

#: Имя сессионной cookie (SPEC.md, раздел 16.2: HTTP-only, 7 дней).
SESSION_COOKIE = "cabinet_session"
#: Имя cookie с CSRF-токеном (double-submit вместе с hidden field).
CSRF_COOKIE = "csrf_token"
#: Имя сессионной cookie врача (SPEC.md, раздел 21: stateless, 7 дней).
DOCTOR_COOKIE = "doctor_session"

#: Сессия живёт 7 дней — в секундах, для Max-Age cookie.
SESSION_MAX_AGE = 7 * 24 * 3600

#: Порог «слишком много записей» для CSV (SPEC.md: лимит выгрузки).
CSV_EXPORT_LIMIT = 5000

#: Слоты напоминаний, как в боте (SPEC.md, раздел 4.3).
SLOT_ORDER = ("morning", "noon", "evening")

#: Окно предотметки лекарств в форме записи: слот не дальше 2 ч от времени (§16.2).
PREFILL_WINDOW_MINUTES = 120

_BASE_DIR = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))
# Подсветка строк «Истории записей» по зонам давления — общая для кабинета
# пациента и врача (§16.2, §21): один источник порогов и цветов с графиками.
_templates.env.globals["row_zone"] = row_zone
_templates.env.globals["history_zone_legend"] = table_legend()
# Подписи пациента у врача (§6.12): список, заголовок карточки, строка данных.
_templates.env.globals["patient_list_label"] = profile_service.list_label
_templates.env.globals["patient_card_title"] = profile_service.card_title
_templates.env.globals["patient_card_details"] = profile_service.card_details


# ─────────────────────────── Вспомогательное ───────────────────────────


def _normalize_hhmm(value: str) -> Optional[str]:
    """Время слота → нормализованная строка «ЧЧ:ММ»; час — 1–2 цифры («2:30» = «02:30»).

    Та же валидация, что в боте (`bot.schemas.validate_time`).
    None — не распознано.
    """
    try:
        parsed = schemas.validate_time(value)
    except ValueError:
        return None
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _client_ip(request: Request) -> str:
    """IP клиента для rate limiting (за reverse proxy — X-Forwarded-For не берём)."""
    return request.client.host if request.client else "unknown"


def _resolve_period(
    tz_name: str, days: str, date_from: str, date_to: str
) -> tuple[date, date]:
    """Границы периода фильтра: 7 / 30 дней / произвольный (локальные сутки)."""
    today = datetime.now(ZoneInfo(tz_name)).date()
    if days == "custom":
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError:
            return today - timedelta(days=6), today
        if start > end:
            start, end = end, start
        return start, end
    span = 30 if days == "30" else 7
    return today - timedelta(days=span - 1), today


async def _pdf_report_response(
    db: AsyncSession,
    user: User,
    start_day: date,
    end_day: date,
    *,
    subject: str,
    back_url: str,
    period: Mapping[str, str],
    file_stem: Optional[str] = None,
) -> Response:
    """PDF-отчёт за период — общий для ЛК и кабинета врача (§10.1).

    Пустой период и лимит — файл не формируется: редирект на страницу
    (`back_url`) с тем же периодом и `error=empty` / `error=too_many`.
    Транзакция чтения закрывается до генерации; сам PDF рисуется в thread
    pool, чтобы не стоял весь кабинет (§7.2 п.7).
    """
    try:
        data = await pdf_report.collect_report(
            db, user, start_day, end_day, subject=subject, file_stem=file_stem
        )
    except (pdf_report.ReportEmptyError, pdf_report.ReportTooLargeError) as exc:
        error = "empty" if isinstance(exc, pdf_report.ReportEmptyError) else "too_many"
        query = urlencode({**period, "error": error})
        return RedirectResponse(url=f"{back_url}?{query}", status_code=303)
    await db.commit()
    payload = await asyncio.to_thread(pdf_report.build_pdf, data)
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{data.file_name}"'},
    )


#: Наибольшая длина периода для таблицы «Средние по дням», дней (§16.2, 0.5.3).
DAILY_AVERAGES_MAX_DAYS = 92


def _daily_averages(
    measurements: list[Measurement], tz_name: str, start_day: date, end_day: date
) -> Optional[stats.DailyAverages]:
    """«Средние по дням» за период фильтра; None — период длиннее 92 дней (§16.2)."""
    if (end_day - start_day).days + 1 > DAILY_AVERAGES_MAX_DAYS:
        return None
    return stats.daily_averages(measurements, tz_name, start_day, end_day)


#: «История записей» по дням (§16.2, 0.5.9): срез показа, шаг догрузки, окно
#: развёртки дней (последние 7 дней от сегодня — развёрнуты).
HISTORY_DAYS_DEFAULT = 92
HISTORY_DAYS_STEP = 30
HISTORY_EXPAND_DAYS = 7

#: Короткие имена недели и месяцев для заголовков истории и «Прошедших».
_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_MONTHS_NOMINATIVE = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

#: Подписи отмены события по типу (словарь «Единого UI-канона», §16.2, В7а).
AP_CANCEL_LABELS = {
    "doctor": "Отменить визит",
    "lab": "Отменить сдачу",
    "exam": "Отменить обследование",
}


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма множественного числа: 1 запись, 2 записи, 5 записей."""
    if count % 100 in (11, 12, 13, 14):
        return many
    if count % 10 == 1:
        return one
    if count % 10 in (2, 3, 4):
        return few
    return many


def _history_context(
    measurements: list[Measurement],
    tz_name: str,
    today: date,
    start_day: date,
    end_day: date,
    shown: str,
) -> dict:
    """Дни «Истории записей» аккордеоном (§16.2, 0.5.9) — общий для ЛК и врача.

    Группы — по локальной дате, новые сверху; строки внутри дня — по времени.
    Заголовок дня: дата, число записей, округлённые средние САД/ДАД и точка
    зоны (`chart_zones.day_zone`, «худшая из зон»). Развёрнуты дни за последние
    `HISTORY_EXPAND_DAYS` дней от сегодня; период короче недели — без свёртки
    (`flat`). Период длиннее `HISTORY_DAYS_DEFAULT` дней — срез `shown`
    последних дней периода («Показать ещё 30 дней», состояние — `?shown=N`);
    экспорт всегда за полный период.
    """
    period_days = (end_day - start_day).days + 1
    flat = period_days <= HISTORY_EXPAND_DAYS
    visible = min(HISTORY_DAYS_DEFAULT, period_days)
    if shown.isdigit() and int(shown) > visible:
        visible = min(int(shown), period_days)
    window_start = max(start_day, end_day - timedelta(days=visible - 1))
    tz = ZoneInfo(tz_name)
    by_day: dict[date, list[Measurement]] = {}
    for m in measurements:
        moment = m.timestamp.astimezone(tz)
        if window_start <= moment.date() <= end_day:
            by_day.setdefault(moment.date(), []).append(m)
    days: list[dict] = []
    for day in sorted(by_day, reverse=True):
        rows = sorted(by_day[day], key=lambda m: m.timestamp)
        sys_avg = round(sum(m.systolic for m in rows) / len(rows))
        dia_avg = round(sum(m.diastolic for m in rows) / len(rows))
        days.append(
            {
                "date": day,
                "label": f"{_WEEKDAYS_SHORT[day.weekday()]}, {day:%d.%m}",
                "count": len(rows),
                "count_text": f"{len(rows)} {_plural(len(rows), 'запись', 'записи', 'записей')}",
                "avg": f"{sys_avg}/{dia_avg}",
                "zone": day_zone(sys_avg, dia_avg),
                "open": (today - day).days < HISTORY_EXPAND_DAYS,
                "rows": rows,
            }
        )
    return {
        "days": days,
        "flat": flat,
        "period_days": period_days,
        "shown": visible,
        "more_shown": min(visible + HISTORY_DAYS_STEP, period_days) if visible < period_days else None,
    }


def _chart_payload(measurements: list[Measurement], tz_name: str) -> list[dict]:
    """Точки графика Chart.js: подпись по локальному времени + значения."""
    tz = ZoneInfo(tz_name)
    return [
        {
            "t": m.timestamp.astimezone(tz).strftime("%d.%m %H:%M"),
            "s": m.systolic,
            "d": m.diastolic,
            "p": m.pulse,
        }
        for m in measurements
    ]


def _measurement_form_values(
    edit: Optional[Measurement], tz: ZoneInfo, now: datetime
) -> dict:
    """Значения формы записи: правка — из записи, добавление — текущие (§16.2)."""
    moment = (edit.timestamp if edit is not None else now).astimezone(tz)
    return {
        "date": moment.date().isoformat(),
        "time": moment.strftime("%H:%M"),
        "systolic": edit.systolic if edit is not None else "",
        "diastolic": edit.diastolic if edit is not None else "",
        "pulse": edit.pulse if edit is not None and edit.pulse is not None else "",
        "medication_taken": edit.medication_taken is True if edit is not None else False,
        "feeling": (edit.feeling or "") if edit is not None else "",
        "note": (edit.note or "") if edit is not None else "",
    }


def _slot_times(reminders: Mapping[str, Reminder]) -> list[tuple[str, str]]:
    """Время слотов «Утро» / «Обед» / «Вечер» для предотметки лекарств (§16.2).

    Время включённого напоминания слота; напоминание выключено или его нет —
    умолчание `DEFAULT_SLOT_TIMES`. Пары в порядке `SLOT_ORDER` (он же —
    порядок выбора при равном расстоянии).
    """
    result: list[tuple[str, str]] = []
    for slot in SLOT_ORDER:
        reminder = reminders.get(slot)
        if reminder is not None and reminder.enabled and reminder.local_time:
            result.append((slot, reminder.local_time))
        else:
            result.append((slot, DEFAULT_SLOT_TIMES[slot]))
    return result


def _day_minutes(hhmm: str) -> int:
    """«ЧЧ:ММ» → минуты от начала суток."""
    parsed = schemas.validate_time(hhmm)
    return parsed.hour * 60 + parsed.minute


def prefill_slot(hhmm: str, slot_times: list[tuple[str, str]]) -> Optional[str]:
    """Слот предотметки лекарств для времени измерения (§16.2).

    Среди слотов, чьё время в пределах ±2 ч от `hhmm`, — ближайший; ни
    одного — None. Расстояние считается по кругу суток (00:30 и 23:30 — час),
    при равенстве побеждает ранний слот. «Кризис» не предотмечается никогда:
    его нет в `slot_times`. Та же логика — в inline-скрипте формы.
    """
    moment = _day_minutes(hhmm)
    best: Optional[tuple[int, str]] = None
    for slot, local_time in slot_times:
        gap = abs(moment - _day_minutes(local_time))
        gap = min(gap, 24 * 60 - gap)
        if gap <= PREFILL_WINDOW_MINUTES and (best is None or gap < best[0]):
            best = (gap, slot)
    return best[1] if best is not None else None


def _parse_measurement_form(form: FormData, tz_name: str) -> dict:
    """Разобрать и проверить форму записи измерения (§9, §16.2).

    Чекбокс «Принял лекарство»: отмечен — True, снят — None («не отмечал»);
    «Другое» самочувствия — короткий текст в заметку. Выбор лекарств
    (`medication_ids`) учитывается только при отмеченной галочке; без неё —
    пустой набор.

    Исключения:
        ValueError: поле не прошло валидацию. Текст — для отладки: форма
            редиректится с `?error=measurement`, сообщение на экран не идёт.
        MedicationNotFoundError: id лекарства — не число («Лекарство не
            найдено»; чужие и удалённые id отсекает `resolve_medications`).
    """
    try:
        day = date.fromisoformat(str(form.get("date", "")).strip())
        moment_time = schemas.validate_time(str(form.get("time", "")))
        moment = schemas.combine_moment(day, moment_time, tz_name)
        values = schemas.MeasurementInput(
            systolic=int(str(form.get("systolic", "")).strip()),
            diastolic=int(str(form.get("diastolic", "")).strip()),
            pulse=int(str(form.get("pulse", "")).strip()) if str(form.get("pulse", "")).strip() else None,
        )
        schemas.validate_pair(values.systolic, values.diastolic)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError("проверьте дату, время и значения") from exc
    medication_taken = True if form.get("medication_taken") else None
    medication_ids: set[int] = set()
    if medication_taken:
        for raw in form.getlist("medication_ids"):
            raw_id = str(raw).strip()
            if not raw_id.isdigit():
                raise MedicationNotFoundError(raw_id)
            medication_ids.add(int(raw_id))
    feeling = str(form.get("feeling", "")).strip()
    if feeling not in ("", "normal", "headache", "weakness", "other"):
        raise ValueError("самочувствие не распознано")
    note_raw = str(form.get("note", "")).strip()
    other_raw = str(form.get("feeling_other", "")).strip() if feeling == "other" else ""
    if other_raw:
        note_raw = f"{note_raw}\n{other_raw}" if note_raw else other_raw
    try:
        note = schemas.validate_note(note_raw) if note_raw else None
    except ValueError as exc:
        raise ValueError("заметка длиннее 500 символов") from exc
    return {
        "moment": moment,
        "systolic": values.systolic,
        "diastolic": values.diastolic,
        "pulse": values.pulse,
        "medication_taken": medication_taken,
        "medication_ids": medication_ids,
        "feeling": feeling or None,
        "note": note,
    }


def _safe_doctor_next(url: str) -> str:
    """Куда вернуть врача после согласия: только страницы кабинета врача.

    Внешние адреса и чужие пути отбрасываются (защита от open redirect).
    """
    if url == "/doctor" or (url.startswith("/doctor/") and "//" not in url and "\\" not in url):
        return url
    return "/doctor"


# ─────────────────────────── Приложение ───────────────────────────


#: robots.txt кабинета: страницы входа и данных не индексируются (SPEC.md, §16.2).
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


def create_app(
    settings: Optional[Settings] = None,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> FastAPI:
    """Собрать приложение кабинета.

    Аргументы:
        settings: настройки; по умолчанию читаются из окружения.
        session_factory: фабрика сессий БД; по умолчанию создаётся из
            `settings.db_path` (общая SQLite с ботом).
    """
    if settings is None:
        try:
            settings = get_settings()
        except ValidationError as exc:
            # `str(ValidationError)` содержит исходный ввод (пароль BOT_PROXY,
            # токен): наружу — только имена полей, исходное исключение глушится.
            fields = ", ".join(sorted({str(err["loc"][0]) for err in exc.errors()}))
            raise RuntimeError(
                f"Ошибка конфигурации ({fields}): проверьте .env — пример в .env.example."
            ) from None
    if not settings.web_secret_key.strip():
        raise RuntimeError(
            "WEB_SECRET_KEY не задан: личный кабинет подписывает сессии и "
            "ссылки входа этим секретом (SPEC.md, раздел 3)."
        )
    if session_factory is None:
        engine = create_engine(settings.db_path)
        session_factory = create_session_factory(engine)
    else:
        engine = None

    magic = auth.MagicLink(settings.web_secret_key)
    csrf = auth.Csrf(settings.web_secret_key)
    limiter = auth.RateLimiter()
    doctor_magic = doctor_auth.DoctorMagicLink(settings.web_secret_key)
    doctor_signin = doctor_auth.DoctorSignInLink(settings.web_secret_key)
    doctor_sessions = doctor_auth.DoctorSession(settings.web_secret_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Создать таблицы при старте (идемпотентно), убрать просроченные списания ссылок."""
        if engine is not None:
            await init_db(engine)
            async with session_factory() as session:
                await doctor_auth.purge_used(session)
                await session.commit()
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Дневник гипертоника — личный кабинет", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.magic = magic
    app.state.csrf = csrf
    app.state.limiter = limiter
    app.state.doctor_magic = doctor_magic
    app.state.doctor_sessions = doctor_sessions
    app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

    # ───────────────────── Зависимости ─────────────────────

    async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
        """Сессия БД на HTTP-запрос (SPEC.md, раздел 16.3)."""
        async with request.app.state.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def get_web_user(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> Optional[WebUser]:
        """Учётная запись по сессионной cookie (или None — не залогинен)."""
        token = request.cookies.get(SESSION_COOKIE, "")
        return await auth.get_web_user_by_token(db, token)

    async def require_session(
        web_user: Optional[WebUser] = Depends(get_web_user),
    ) -> WebUser:
        """Требовать вход: без сессии — редирект на страницу входа."""
        if web_user is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return web_user

    async def require_user(
        web_user: WebUser = Depends(require_session),
        db: AsyncSession = Depends(get_db),
    ) -> WebUser:
        """Вход + действующее согласие (§6.11, К3): без него — страница `/consent`.

        Все страницы ЛК, экспорт и импорт идут через эту зависимость; сама
        страница согласия — через `require_session`.
        """
        if not await consent_service.has_processing(db, web_user.user_id):
            raise HTTPException(status_code=303, headers={"Location": "/consent"})
        return web_user

    async def load_user(db: AsyncSession, web_user: WebUser) -> User:
        """Пользователь бота за учётной записью кабинета."""
        user = (
            await db.execute(select(User).where(User.id == web_user.user_id))
        ).scalar_one_or_none()
        if user is None:  # pragma: no cover — FK не даёт осиротеть записи
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        return user

    def check_csrf(request: Request, form_token: str) -> None:
        """Отклонить форму без корректного CSRF-токена (403)."""
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        if not csrf.validate(form_token, cookie_token):
            raise HTTPException(status_code=403, detail="Некорректный CSRF-токен")

    def cookie_secure(request: Request) -> bool:
        """Secure-флаг cookie: при HTTPS-доступе (напрямую или по схеме `CABINET_URL`).

        По обычному HTTP клиент не возвращает Secure-куки обратно — формы
        падают с 403 на CSRF-проверке. Локальный деплой (`http://127.0.0.1:8000`)
        работает без флага; продакшен за HTTPS (`CABINET_URL=https://…`) — с ним.
        """
        return request.url.scheme == "https" or settings.cabinet_url.strip().lower().startswith(
            "https://"
        )

    def set_auth_cookies(
        request: Request, response: Response, session_token: str, csrf_token: str
    ) -> None:
        """Поставить сессионную и CSRF-куки (HTTP-only, SameSite=Lax, Secure по схеме)."""
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )

    def clear_auth_cookies(response: Response) -> None:
        """Снять куки при выходе."""
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    def login_redirect() -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    # ───────────────────── Кабинет врача: зависимости ─────────────────────

    async def get_doctor(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> Optional[Doctor]:
        """Врач по сессионной cookie (или None — не залогинен, §21)."""
        token = request.cookies.get(DOCTOR_COOKIE, "")
        doctor_id = doctor_sessions.verify(token)
        if doctor_id is None:
            return None
        return (
            await db.execute(select(Doctor).where(Doctor.id == doctor_id))
        ).scalar_one_or_none()

    async def require_doctor(
        request: Request, doctor: Optional[Doctor] = Depends(get_doctor)
    ) -> Doctor:
        """Вход врача и его действующее согласие (§6.11, К2).

        Без сессии — страница входа; без согласия — `/doctor/consent` с
        возвратом на запрошенную страницу.
        """
        if doctor is None:
            raise HTTPException(status_code=303, headers={"Location": "/doctor"})
        if not consent_service.doctor_ok(doctor):
            query = urlencode({"next": request.url.path})
            raise HTTPException(
                status_code=303, headers={"Location": f"/doctor/consent?{query}"}
            )
        return doctor

    def set_doctor_cookie(request: Request, response: Response, doctor_id: int) -> None:
        """Поставить сессионную cookie врача (HTTP-only, 7 дней, Secure по схеме)."""
        response.set_cookie(
            DOCTOR_COOKIE,
            doctor_sessions.make(doctor_id),
            max_age=doctor_auth.DOCTOR_SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )

    async def require_patient_link(
        db: AsyncSession, doctor: Doctor, user_id: int
    ) -> User:
        """Пациент, к которому у врача есть доступ (§21, §6.11).

        Проверяется на каждый запрос данных пациента (CLAUDE.md, «Роли и
        доступ»): связь `active`, согласие пациента на этого врача, основное
        согласие пациента и согласие самого врача. Иначе — как отсутствующий:
        редирект с ошибкой.
        """
        if not await consent_service.doctor_can_see(db, doctor, user_id):
            raise HTTPException(
                status_code=303, headers={"Location": "/doctor?error=denied"}
            )
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:  # pragma: no cover — FK CASCADE не даёт осиротеть записи
            raise HTTPException(status_code=404, detail="Пациент не найден")
        return user

    # ───────────────────── Вход, регистрация, magic link ─────────────────────

    async def _magic_response(request: Request, db: AsyncSession, token: str) -> Response:
        """Вход по magic link из бота (§16.2).

        Токен подписывает `telegram_id` пользователя. Существующему
        пользователю — сессия; новичку — страница регистрации (первая связка
        пароля). Токен одноразовый: `verify` не списывает (страница может
        перерисоваться), `consume` списывает при входе.
        """
        telegram_id = magic.verify(token)
        if telegram_id is None:
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        owner = (
            await db.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if owner is None:
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        record = (
            await db.execute(select(WebUser).where(WebUser.user_id == owner.id))
        ).scalar_one_or_none()
        if record is None:
            # Первая связка: предложить создать пароль для входа без бота.
            return RedirectResponse(url=f"/register?token={token}", status_code=303)
        telegram_id = magic.consume(token)
        if telegram_id is None:  # pragma: no cover — параллельное использование ссылки
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        session_token = await auth.create_web_session(db, record.id)
        response = RedirectResponse(url="/", status_code=303)
        set_auth_cookies(request, response, session_token, csrf.issue())
        return response

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots_txt() -> str:
        """Закрыть кабинет от индексации поисковиками (SPEC.md, §16.2)."""
        return ROBOTS_TXT

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> Response:
        """Форма входа e-mail + пароль; `?token=` — вход по magic link (§16.2)."""
        token = request.query_params.get("token", "")
        if token:
            return await _magic_response(request, db, token)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "login.html",
            {"error": request.query_params.get("error", ""), "csrf_token": csrf_token},
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/login")
    async def login_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: Optional[WebUser] = Depends(get_web_user),
    ) -> Response:
        """Вход e-mail + пароль (Argon2id; rate limiting 5 попыток/мин)."""
        if web_user is not None:
            return RedirectResponse(url="/", status_code=303)
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        key = f"{_client_ip(request)}:{email}"
        if not limiter.allow(key):
            raise HTTPException(status_code=429, detail="Слишком много попыток, подождите минуту")
        limiter.hit(key)
        record = (
            await db.execute(select(WebUser).where(WebUser.email == email))
        ).scalar_one_or_none()
        if record is None or not await asyncio.to_thread(
            auth.verify_password, record.password_hash, password
        ):
            return RedirectResponse(url="/login?error=wrong", status_code=303)
        limiter.reset(key)
        session_token = await auth.create_web_session(db, record.id)
        csrf_token = csrf.issue()
        response = RedirectResponse(url="/", status_code=303)
        set_auth_cookies(request, response, session_token, csrf_token)
        return response

    @app.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request) -> Response:
        """Регистрация e-mail + пароль — только по magic link из бота.

        Токен ещё не списан: страница лишь показывает форму, списывает
        токен успешный POST.
        """
        token = request.query_params.get("token", "")
        user_id = magic.verify(token)
        if user_id is None:
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": request.query_params.get("error", ""),
                "token": token,
                "csrf_token": csrf_token,
            },
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/register")
    async def register_submit(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        """Создать учётную запись кабинета (Argon2id) и открыть сессию.

        Токен подписывает `telegram_id`; списывается (`consume`) только после
        успешной валидации полей — при опечатке в форме та же ссылка остаётся
        рабочей.
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        token = str(form.get("token", ""))
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        confirm = str(form.get("password_confirm", ""))
        if not auth.valid_email(email):
            return _register_error(request, token, "email")
        if not auth.valid_password(password):
            return _register_error(request, token, "password")
        if password != confirm:
            return _register_error(request, token, "mismatch")
        telegram_id = magic.verify(token)
        if telegram_id is None:
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        owner = (
            await db.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if owner is None:
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        existing = (
            await db.execute(
                select(WebUser).where(
                    (WebUser.user_id == owner.id) | (WebUser.email == email)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _register_error(request, token, "exists")
        if magic.consume(token) is None:  # pragma: no cover — параллельное использование ссылки
            return RedirectResponse(url="/login?error=link_expired", status_code=303)
        record = WebUser(
            user_id=owner.id,
            email=email,
            password_hash=await asyncio.to_thread(auth.hash_password, password),
        )
        db.add(record)
        await db.flush()
        session_token = await auth.create_web_session(db, record.id)
        response = RedirectResponse(url="/", status_code=303)
        set_auth_cookies(request, response, session_token, csrf.issue())
        return response

    def _register_error(request: Request, token: str, code: str) -> Response:
        """Показать форму регистрации с кодом ошибки (токен ещё не списан)."""
        return _templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": code,
                "token": token,
                "csrf_token": request.cookies.get(CSRF_COOKIE, ""),
            },
            status_code=400,
        )

    @app.get("/magic")
    async def magic_login(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        """Вход по magic link из бота (`/cabinet`): одноразовая ссылка, 15 минут."""
        return await _magic_response(request, db, request.query_params.get("token", ""))

    # ───────────────────── Согласие пациента (§6.11, К3, К5) ─────────────────────

    def _consent_page(request: Request, view: str, **context: object) -> Response:
        """Страница согласия с CSRF-кукой (общая для К3 и К5)."""
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "consent.html",
            {
                "view": view,
                "csrf_token": csrf_token,
                "title": texts.CABINET_CONSENT_TITLE,
                "paragraphs": texts.consent_paragraphs(),
                "privacy_url": settings.privacy_url,
                "full_link": texts.CABINET_CONSENT_FULL_LINK,
                "declined_text": texts.CABINET_CONSENT_DECLINED,
                "withdraw_text": texts.CABINET_WITHDRAW_CONFIRM,
                "withdrawn_text": texts.CABINET_WITHDRAWN,
                **context,
            },
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/consent", response_class=HTMLResponse)
    async def consent_page(
        request: Request,
        declined: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_session),
    ) -> Response:
        """К3: согласие пациента; `?declined=1` — «Не согласен»."""
        if await consent_service.has_processing(db, web_user.user_id):
            return RedirectResponse(url="/", status_code=303)
        return _consent_page(request, "declined" if declined else "ask")

    @app.post("/consent")
    async def consent_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_session),
    ) -> Response:
        """«Согласен» в кабинете: запись `consents`, `channel='cabinet'` (§6.11)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        if user.deletion_requested_at is not None:
            user.deletion_requested_at = None  # передумал до удаления ботом
            await db.flush()
        await consent_service.give_processing(db, user.id, consent_service.CHANNEL_CABINET)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/consent/withdraw", response_class=HTMLResponse)
    async def consent_withdraw_page(
        request: Request, web_user: WebUser = Depends(require_user)
    ) -> Response:
        """К5: подтверждение отзыва согласия в кабинете."""
        return _consent_page(request, "withdraw")

    @app.post("/consent/withdraw")
    async def consent_withdraw(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """К5: согласие закрыто, аккаунт ждёт удаления ботом, сессии удалены.

        Кабинет не управляет планировщиком напоминаний бота, а job'ы
        удаляются строго до `users` (§19), поэтому удаление делает бот при
        ближайшей ежечасной проверке (`deletion_requested_at`).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        await consent_service.request_deletion(db, web_user.user_id)
        await db.execute(delete(WebSession).where(WebSession.web_user_id == web_user.id))
        response = _consent_page(request, "withdrawn")
        clear_auth_cookies(response)
        return response

    @app.post("/logout")
    async def logout(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: Optional[WebUser] = Depends(get_web_user),
    ) -> Response:
        """Выход: сессия удаляется из SQLite, куки снимаются."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            await auth.delete_web_session(db, token)
        response = RedirectResponse(url="/login", status_code=303)
        clear_auth_cookies(response)
        return response

    # ───────────────────── Дашборд ─────────────────────

    async def _dashboard_response(
        request: Request,
        db: AsyncSession,
        web_user: WebUser,
        days: str,
        date_from: str,
        date_to: str,
        error: str = "",
        saved: str = "",
        extra: Optional[dict] = None,
        *,
        user: User,
        shown: str = "",
    ) -> Response:
        """Собрать страницу дашборда; `extra` — контекст форм записи/импорта (§16.2).

        `user` — пользователь бота, уже загруженный вызывающим хендлером
        (`load_user`), чтобы не запрашивать его повторно.
        """
        tz = ZoneInfo(user.timezone)
        now_local = datetime.now(tz)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        measurements = await measurements_for_period(db, user, start_day, end_day)
        summary = await stats.summary(db, user, start_day, end_day)
        daily = _daily_averages(measurements, user.timezone, start_day, end_day)
        medication_cells = await medications_service.medication_cells(db, user, measurements)
        medications = await medications_service.list_medications(db, user)
        ap_upcoming = await appointments_service.list_upcoming(db, user, now_local.date())
        past_items = [
            (
                appt,
                appointments_service.status_label(appt, now_local.date()),
                appointments_service.awaiting_answer(appt, now_local.date()),
            )
            for appt in (await appointments_service.list_past(db, user, now_local.date(), limit=None))[0]
        ]
        # «Прошедшие» (0.5.9): «Ждёт отметки» — сверху, вне групп; остальные —
        # аккордеоном по месяцам, текущий месяц развёрнут (§16.2).
        ap_awaiting = [item for item in past_items if item[2]]
        ap_months: list[dict] = []
        for item in past_items:
            if item[2]:
                continue
            key = (item[0].starts_on.year, item[0].starts_on.month)
            if not ap_months or ap_months[-1]["key"] != key:
                ap_months.append({"key": key, "items": []})
            ap_months[-1]["items"].append(item)
        current_month = (now_local.date().year, now_local.date().month)
        for month in ap_months:
            count = len(month["items"])
            month["label"] = f"{_MONTHS_NOMINATIVE[month['key'][1] - 1]} {month['key'][0]}"
            month["count_text"] = f"{count} {_plural(count, 'событие', 'события', 'событий')}"
            month["current"] = month["key"] == current_month
            del month["key"]
        doctor_notes = await doctor_service.notes_for_patient(db, user.id)
        consent = await consent_service.current_processing(db, user.id)
        personal_messages = {
            "personal_name": (texts.PROFILE_NAME_BAD, False),
            "personal_birth": (texts.PROFILE_BIRTH_BAD, False),
            "personal_young": (texts.PROFILE_TOO_YOUNG, False),
        }
        personal_saved = {
            "personal": (texts.PROFILE_SAVED, True),
            "personal_deleted": (texts.PROFILE_DELETED, True),
        }
        message, ok = personal_messages.get(error) or personal_saved.get(saved) or ("", False)
        filled = profile_service.has_profile(user)
        personal = {
            "filled": filled,
            "name": profile_service.full_name(user) if filled else "",
            "birth": f"{user.birth_date:%d.%m.%Y}" if filled else "",
            "age": profile_service.age_text(user) if filled else "",
            "sex": profile_service.SEX_LABELS[user.sex][0] if filled else "",  # type: ignore[index]
            "not_filled": texts.CABINET_PROFILE_NOT_FILLED,
            "hint": texts.PROFILE_EMPTY.split("\n\n", 1)[1],
            "message": message,
            "ok": ok,
        }
        reminders = list(
            (
                await db.execute(
                    select(Reminder)
                    .where(Reminder.user_id == user.id, Reminder.type.in_(SLOT_ORDER))
                    .order_by(Reminder.type)
                )
            ).scalars()
        )
        csrf_token = csrf.issue()
        context = {
            "web_user": web_user,
            "user": user,
            "consent_line": (
                texts.CABINET_CONSENT_LINE.format(
                    date=consent.given_at.astimezone(tz).strftime("%d.%m.%Y")
                )
                if consent is not None
                else ""
            ),
            "user_zone": tz,
            "personal": personal,
            "slot_order": SLOT_ORDER,
            "med_slot_order": medications_service.SLOTS,
            "slot_labels": texts.SLOT_LABELS,
            "feeling_labels": texts.FEELING_LABELS,
            "medication_cells": medication_cells,
            "measurements": measurements,
            "summary": summary,
            "daily": daily,
            "daily_max_days": DAILY_AVERAGES_MAX_DAYS,
            "medications": medications,
            "reminders": {r.type: r for r in reminders},
            "slot_has_meds": {slot: any(m.slot == slot for m in medications) for slot in SLOT_ORDER},
            "mode_labels": texts.MODE_LABELS,
            "paused_until": (
                user.reminders_paused_until
                if user.reminders_paused_until is not None
                and user.reminders_paused_until >= now_local.date()
                else None
            ),
            "doctor_notes": doctor_notes,
            "ap_upcoming": ap_upcoming,
            "ap_awaiting": ap_awaiting,
            "ap_months": ap_months,
            "ap_kinds": texts.AP_KIND_BUTTONS,
            "ap_suggestions": [t for kind in texts.AP_KINDS for t in texts.AP_SUGGESTIONS[kind]],
            "ap_max_day": appointments_service.max_day(now_local.date()),
            "ap_after_buttons": texts.AP_AFTER_BUTTONS,
            "ap_cancel_labels": AP_CANCEL_LABELS,
            "chart": _chart_payload(measurements, user.timezone),
            "chart_zones": CHART_ZONES,
            "days": days,
            "date_from": date_from,
            "date_to": date_to,
            "start_day": start_day,
            "end_day": end_day,
            "error": error,
            "saved": saved,
            "csrf_token": csrf_token,
            "today": now_local.date(),
            "now_local": now_local,
            # формы записи и импорта (§16.2)
            "show_add_form": False,
            "edit_measurement": None,
            "confirm_delete_id": None,
            "confirm_delete_when": "",
            "show_import_form": False,
            "import_report": None,
            "import_error": "",
            # «История записей» по дням и формы/подтверждения 0.5.9 (§16.2)
            "history": _history_context(
                measurements, user.timezone, now_local.date(), start_day, end_day, shown
            ),
            "history_can_edit": True,
            "edit_medication": None,
            "confirm_delete_med": None,
            "edit_ap_id": None,
            "confirm_delete_ap": None,
            "confirm_cancel_ap": None,
        }
        if extra:
            context.update(extra)
        context["form_values"] = _measurement_form_values(
            context.get("edit_measurement"), tz, now_local
        )
        # Выбор лекарств в форме записи (§16.2): правка — текущие связи,
        # добавление — предотметка ближайшего слота по времени формы.
        context["slot_times"] = _slot_times(context["reminders"])
        context["prefill_window"] = PREFILL_WINDOW_MINUTES
        context["form_medication_ids"] = set()
        if context["show_add_form"]:
            edit_measurement = context.get("edit_measurement")
            if edit_measurement is not None:
                context["form_medication_ids"] = await medications_service.measurement_intake_ids(
                    db, user, edit_measurement.id
                )
            else:
                slot = prefill_slot(context["form_values"]["time"], context["slot_times"])
                context["form_medication_ids"] = {m.id for m in medications if m.slot == slot}
        response = _templates.TemplateResponse(request, "dashboard.html", context)
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        error: str = Query(""),
        saved: str = Query(""),
        add: str = Query(""),
        edit: str = Query(""),
        confirm_delete: str = Query(""),
        import_form: str = Query("", alias="import"),
        edit_med: str = Query(""),
        edit_ap: str = Query(""),
        confirm_delete_med: str = Query(""),
        confirm_delete_ap: str = Query(""),
        confirm_cancel_ap: str = Query(""),
        shown: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Личная карточка, история, сводка, лекарства и напоминания.

        Параметры управляют формами (§16.2): `?add=1` — форма добавления,
        `?edit={id}` — правка записи, `?confirm_delete={id}` — подтверждение
        удаления записи, `?import=1` — форма импорта CSV. С 0.5.9: `?edit_med={id}`
        — форма «Изменить лекарство», `?edit_ap={id}` — форма правки события,
        `?confirm_delete_med` / `?confirm_delete_ap` / `?confirm_cancel_ap` —
        блоки подтверждения (не `js confirm()`), `?shown=N` — глубина истории
        по дням («Показать ещё 30 дней»).
        """
        user = await load_user(db, web_user)
        extra: dict = {}
        if add or error in ("measurement", "duplicate", "medication_missing"):
            extra["show_add_form"] = True
        if edit.isdigit():
            measurement = await get_measurement(db, user, int(edit))
            if measurement is not None:
                extra["show_add_form"] = True
                extra["edit_measurement"] = measurement
        if confirm_delete.isdigit():
            victim = await get_measurement(db, user, int(confirm_delete))
            if victim is not None:
                extra["confirm_delete_id"] = victim.id
                extra["confirm_delete_when"] = victim.timestamp.astimezone(
                    ZoneInfo(user.timezone)
                ).strftime("%d.%m.%Y %H:%M")
        if import_form:
            extra["show_import_form"] = True
        medications = await medications_service.list_medications(db, user)
        if edit_med.isdigit():
            extra["edit_medication"] = next(
                (m for m in medications if m.id == int(edit_med)), None
            )
        if confirm_delete_med.isdigit():
            extra["confirm_delete_med"] = next(
                (m for m in medications if m.id == int(confirm_delete_med)), None
            )
        for raw, key in (
            (edit_ap, "edit_ap_id"),
            (confirm_delete_ap, "confirm_delete_ap"),
            (confirm_cancel_ap, "confirm_cancel_ap"),
        ):
            if raw.isdigit():
                appt = await appointments_service.get_owned(db, user, int(raw))
                if appt is not None:
                    extra[key] = appt.id if key == "edit_ap_id" else appt
        return await _dashboard_response(
            request, db, web_user, days, date_from, date_to, error, saved, extra,
            user=user, shown=shown,
        )

    # ───────────────────── Профиль ─────────────────────

    @app.post("/profile")
    async def profile_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Смена целей, часового пояса и пароля (вход уже подтверждён сессией)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        try:
            target_systolic = int(str(form.get("target_systolic", "")))
            target_diastolic = int(str(form.get("target_diastolic", "")))
        except ValueError:
            return RedirectResponse(url="/?error=profile", status_code=303)
        if not (70 <= target_systolic <= 260 and 40 <= target_diastolic <= 160):
            return RedirectResponse(url="/?error=profile", status_code=303)
        tz_name = str(form.get("timezone", "")).strip()
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return RedirectResponse(url="/?error=profile", status_code=303)
        new_password = str(form.get("new_password", ""))
        if new_password:
            if not auth.valid_password(new_password):
                return RedirectResponse(url="/?error=password", status_code=303)
            web_user.password_hash = await asyncio.to_thread(auth.hash_password, new_password)
        user.target_systolic = target_systolic
        user.target_diastolic = target_diastolic
        user.timezone = tz_name
        await db.flush()
        return RedirectResponse(url="/?saved=profile", status_code=303)

    # ───────────────────── Данные пациента (§6.12) ─────────────────────

    @app.post("/profile/personal")
    async def personal_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """«Мои данные»: ФИО, дата рождения (16+) и пол — правила бота (§6.12)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        today = profile_service.today_for(user)
        try:
            birth = schemas.validate_birth_date(
                date.fromisoformat(str(form.get("birth_date", "")).strip()), today
            )
        except schemas.TooYoungError:
            return RedirectResponse(url="/?error=personal_young#personal", status_code=303)
        except ValueError:
            return RedirectResponse(url="/?error=personal_birth#personal", status_code=303)
        try:
            personal = profile_service.validate(
                str(form.get("last_name", "")),
                str(form.get("first_name", "")),
                str(form.get("middle_name", "")).strip() or None,
                birth,
                str(form.get("sex", "")),
                today,
            )
        except ValueError:  # ФИО или пол
            return RedirectResponse(url="/?error=personal_name#personal", status_code=303)
        await profile_service.save(db, user, personal)
        return RedirectResponse(url="/?saved=personal#personal", status_code=303)

    @app.post("/profile/personal/delete")
    async def personal_delete(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """«Удалить данные»: врач снова видит имя из Telegram (§6.12)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        await profile_service.clear(db, await load_user(db, web_user))
        return RedirectResponse(url="/?saved=personal_deleted#personal", status_code=303)

    # ───────────────────── Лекарства (CRUD) ─────────────────────

    @app.post("/medications")
    async def medication_add(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Добавить лекарство: по строке на каждый выбранный слот."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        name = str(form.get("name", "")).strip()
        dosage = str(form.get("dosage", "")).strip()
        slots = [slot for slot in medications_service.SLOTS if form.get(f"slot_{slot}")]
        # Дозировка опциональна: пустая строка — «без дозировки» (§4.4).
        if not (1 <= len(name) <= 100) or len(dosage) > 50:
            return RedirectResponse(url="/?error=medication", status_code=303)
        if not slots:
            return RedirectResponse(url="/?error=medication", status_code=303)
        await medications_service.add_medication(db, user, name, dosage, slots)
        await db.commit()
        return RedirectResponse(url="/?saved=medication#medications", status_code=303)

    @app.post("/medications/{medication_id}")
    async def medication_update(
        medication_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Отредактировать лекарство: название, дозировку и слот (проверка владельца)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        medication = (
            await db.execute(
                select(Medication).where(
                    Medication.id == medication_id, Medication.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if medication is None:
            return RedirectResponse(url="/?error=medication#medications", status_code=303)
        name = str(form.get("name", "")).strip()
        dosage = str(form.get("dosage", "")).strip()
        slot = str(form.get("slot", ""))
        # Дозировка опциональна: пустая строка — «без дозировки» (§4.4).
        if (
            not (1 <= len(name) <= 100)
            or len(dosage) > 50
            or slot not in medications_service.SLOTS
        ):
            return RedirectResponse(url="/?error=medication#medications", status_code=303)
        try:
            medication.name = name
            medication.dosage = dosage
            medication.slot = slot
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return RedirectResponse(url="/?error=medication#medications", status_code=303)
        return RedirectResponse(url="/?saved=medication#medications", status_code=303)

    @app.post("/medications/{medication_id}/delete")
    async def medication_delete(
        medication_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Удалить лекарство (проверка владельца, логи — каскадом)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        await medications_service.delete_medication(db, user, medication_id)
        await db.commit()
        return RedirectResponse(url="/?saved=medication#medications", status_code=303)

    # ───────────────────── Визиты и анализы (§6.13, §16.2, 0.5.8) ─────────────────────

    def _appointment_form(form, user: User, kind_required: bool):
        """Проверить поля формы события; ошибка — None (те же правила, что в боте)."""
        kind = str(form.get("kind", ""))
        title = appointments_service.valid_title(str(form.get("title", "")))
        note = appointments_service.valid_note(str(form.get("note", "")))
        raw_time = str(form.get("time", "")).strip()
        hhmm = appointments_service.parse_hhmm(raw_time) if raw_time else None
        try:
            day = date.fromisoformat(str(form.get("date", "")))
        except ValueError:
            return None
        if (
            (kind_required and kind not in texts.AP_KINDS)
            or title is None
            or note is None
            or (raw_time and hhmm is None)
            or appointments_service.day_problem(user, day) is not None
            or appointments_service.time_passed(user, day, hhmm)
        ):
            return None
        return kind, title, day, hhmm, note

    async def _owned_appointment(db: AsyncSession, user: User, appointment_id: int):
        appt = await appointments_service.get_owned(db, user, appointment_id)
        if appt is None:
            raise HTTPException(status_code=404)
        return appt

    def _ap_redirect(kind: str) -> Response:
        if kind == "saved":
            return RedirectResponse(url="/?saved=appointment#appointments", status_code=303)
        return RedirectResponse(url=f"/?error={kind}#appointments", status_code=303)

    @app.post("/appointments")
    async def appointment_add(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Добавить визит, анализы или обследование (§16.2); лимит — 30 «Скоро»."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        fields = _appointment_form(form, user, kind_required=True)
        if fields is None:
            return _ap_redirect("appointment")
        kind, title, day, hhmm, note = fields
        try:
            await appointments_service.create(db, user, kind, title, day, hhmm, note)
        except appointments_service.LimitReached:
            return _ap_redirect("appointment_limit")
        return _ap_redirect("saved")

    @app.post("/appointments/{appointment_id}")
    async def appointment_update(
        appointment_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Изменить событие «Скоро»: вид, название, дату, время, заметку.

        Вид в ЛК меняется (в отличие от бота, Н4, §6.13); смена даты/времени
        пересчитывает флаги уведомлений (§6.13).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        appt = await _owned_appointment(db, user, appointment_id)
        fields = _appointment_form(form, user, kind_required=True)
        if fields is None or not appointments_service.is_upcoming(appt, appointments_service.today(user)):
            return _ap_redirect("appointment")
        kind, title, day, hhmm, note = fields
        if (day, hhmm) != (appt.starts_on, appt.starts_at):
            await appointments_service.reschedule(db, user, appt, day, hhmm)
        await appointments_service.set_kind(db, appt, kind)
        await appointments_service.set_title(db, appt, title)
        await appointments_service.set_note(db, appt, note)
        return _ap_redirect("saved")

    @app.post("/appointments/{appointment_id}/cancel")
    async def appointment_cancel(
        appointment_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        appt = await _owned_appointment(db, user, appointment_id)
        if not appointments_service.is_upcoming(appt, appointments_service.today(user)):
            return _ap_redirect("appointment")
        await appointments_service.set_status(db, appt, "cancelled")
        return _ap_redirect("saved")

    @app.post("/appointments/{appointment_id}/status")
    async def appointment_status(
        appointment_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """«Был(а)» / «Не был(а)» у события, которое ждёт отметки (Н2)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        appt = await _owned_appointment(db, user, appointment_id)
        status = str(form.get("status", ""))
        if status not in ("done", "missed") or not appointments_service.awaiting_answer(
            appt, appointments_service.today(user)
        ):
            return _ap_redirect("appointment")
        await appointments_service.set_status(db, appt, status)
        return _ap_redirect("saved")

    @app.post("/appointments/{appointment_id}/moved")
    async def appointment_moved(
        appointment_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """«Перенесли на…» после даты: прежняя → `moved`, новая `planned` (Н1)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        appt = await _owned_appointment(db, user, appointment_id)
        merged = {"title": appt.title, "note": appt.note, "date": form.get("date", ""), "time": form.get("time", "")}
        fields = _appointment_form(merged, user, kind_required=False)
        if fields is None or not appointments_service.awaiting_answer(appt, appointments_service.today(user)):
            return _ap_redirect("appointment")
        _, _, day, hhmm, _ = fields
        try:
            created = await appointments_service.moved_after(db, user, appt, day, hhmm)
        except appointments_service.LimitReached:
            await db.rollback()
            return _ap_redirect("appointment_limit")
        if created is None:
            return _ap_redirect("appointment")
        return _ap_redirect("saved")

    @app.post("/appointments/{appointment_id}/delete")
    async def appointment_delete(
        appointment_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        await _owned_appointment(db, user, appointment_id)
        await appointments_service.remove(db, user, appointment_id)
        return _ap_redirect("saved")

    # ───────────────────── Записи измерений ─────────────────────

    @app.post("/measurements")
    async def measurement_add(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Добавить запись измерения из ЛК (§16.2): момент — любой прошедший.

        Дубль за ту же минуту отсекается сервисом; сохранение идемпотентно
        при двойной отправке формы (§7.2 п.4). Выбранные лекарства — связи и
        журнал на момент записи, в той же транзакции; лок приёмов берётся до
        первой записи в БД и держится до коммита (§7.2).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        try:
            values = _parse_measurement_form(form, user.timezone)
            medications = await medications_service.resolve_medications(
                db, user, values["medication_ids"]
            )
        except MedicationNotFoundError:
            return RedirectResponse(url="/?error=medication_missing#history", status_code=303)
        except ValueError:
            return RedirectResponse(url="/?error=measurement#history", status_code=303)
        async with medications_service.intake_lock():
            try:
                measurement = await save_measurement(
                    db,
                    user,
                    values["systolic"],
                    values["diastolic"],
                    values["pulse"],
                    timestamp=values["moment"],
                )
            except DuplicateMeasurementError:
                await db.rollback()
                return RedirectResponse(url="/?error=duplicate#history", status_code=303)
            measurement.medication_taken = values["medication_taken"]
            measurement.feeling = values["feeling"]
            measurement.note = values["note"]
            if medications:
                await medications_service.sync_medication_logs(
                    db, user, measurement.id, measurement.timestamp, medications
                )
            await db.commit()
        return RedirectResponse(url="/?saved=measurement#history", status_code=303)

    @app.post("/measurements/{measurement_id}")
    async def measurement_update(
        measurement_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Правка записи: меняет и момент, с проверкой дубля за минуту (§16.2).

        Ошибка валидации или дубль — назад на форму правки этой же записи
        (`?edit={id}`), а не на форму добавления. Лекарства (§16.2): журнал
        записи переносится на новый момент, затем связи и журнал приводятся к
        выбору формы (снятая галочка «Принял лекарство» снимает всё) — одной
        транзакцией под локом приёмов, взятым до первой записи (§7.2).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        try:
            values = _parse_measurement_form(form, user.timezone)
            medications = await medications_service.resolve_medications(
                db, user, values["medication_ids"]
            )
        except MedicationNotFoundError:
            return RedirectResponse(
                url=f"/?edit={measurement_id}&error=medication_missing#history", status_code=303
            )
        except ValueError:
            return RedirectResponse(
                url=f"/?edit={measurement_id}&error=measurement#history", status_code=303
            )
        async with medications_service.intake_lock():
            current = await get_measurement(db, user, measurement_id)
            if current is None:
                return RedirectResponse(url="/?error=measurement#history", status_code=303)
            old_moment = current.timestamp
            try:
                measurement = await update_measurement_values(
                    db,
                    user,
                    measurement_id,
                    values["systolic"],
                    values["diastolic"],
                    values["pulse"],
                    timestamp=values["moment"],
                )
            except DuplicateMeasurementError:
                await db.rollback()
                return RedirectResponse(
                    url=f"/?edit={measurement_id}&error=duplicate#history", status_code=303
                )
            if measurement is None:  # pragma: no cover — удалена между чтением и UPDATE под локом
                await db.rollback()
                return RedirectResponse(url="/?error=measurement#history", status_code=303)
            # Чекбокс двузначен, флаг — трёхзначен: снятый чекбокс не затирает
            # ответ «нет» из бота (False), а только снимает «да».
            if not (values["medication_taken"] is None and measurement.medication_taken is False):
                measurement.medication_taken = values["medication_taken"]
            measurement.feeling = values["feeling"]
            measurement.note = values["note"]
            await medications_service.update_medication_log_timestamps(
                db, user, measurement_id, old_moment, measurement.timestamp
            )
            await medications_service.sync_medication_logs(
                db, user, measurement_id, measurement.timestamp, medications
            )
            await db.commit()
        return RedirectResponse(url="/?saved=measurement#history", status_code=303)

    @app.post("/measurements/{measurement_id}/delete")
    async def measurement_delete(
        measurement_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Удалить запись — только после подтверждения на форме (§16.2).

        Повторная отправка идемпотентна: уже удалённая (или чужая) запись
        выглядит как отсутствующая, редирект тот же. Сначала удаляется журнал
        записи (по паре `medication_id + timestamp` её связей), затем запись —
        связи уходят каскадом (§16.2); под локом приёмов до коммита (§7.2).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        async with medications_service.intake_lock():
            measurement = await get_measurement(db, user, measurement_id)
            if measurement is not None:
                await medications_service.delete_medication_logs_for_measurement(
                    db, user, measurement.id, measurement.timestamp
                )
                await delete_measurement(db, user, measurement_id)
            await db.commit()
        return RedirectResponse(url="/?saved=measurement#history", status_code=303)

    # ───────────────────── Импорт CSV ─────────────────────

    @app.post("/import")
    async def import_measurements_csv(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Импорт CSV архива: разбор в потоке, вставка одной транзакцией (§16.2).

        Разбор файла — в `asyncio.to_thread` (§7.2 п.7); двойная отправка
        формы дублей не создаёт — все строки второго прогона попадают в
        «Пропущено дублей» (§7.2 п.4). Отчёт рендерится прямо здесь: повторный
        refresh страницы лишь повторит разбор, ничего не вставив.
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        days = str(form.get("days", "7"))
        date_from = str(form.get("date_from", ""))
        date_to = str(form.get("date_to", ""))
        extra: dict = {"show_import_form": True}
        upload = form.get("file")
        # Читается не больше лимита + 1 байт: огромный файл не попадает в память.
        data = (
            await upload.read(csv_import.MAX_IMPORT_BYTES + 1)
            if isinstance(upload, UploadFile) and upload.filename
            else b""
        )
        if not data:
            extra["import_error"] = "Выберите файл."
            return await _dashboard_response(
                request, db, web_user, days, date_from, date_to, extra=extra, user=user
            )
        if len(data) > csv_import.MAX_IMPORT_BYTES:
            extra["import_error"] = csv_import.FILE_TOO_MANY_ROWS
            return await _dashboard_response(
                request, db, web_user, days, date_from, date_to, extra=extra, user=user
            )
        try:
            parsed = await asyncio.to_thread(csv_import.parse, data, user.timezone)
        except ValueError as exc:
            extra["import_error"] = str(exc)
            return await _dashboard_response(
                request, db, web_user, days, date_from, date_to, extra=extra, user=user
            )
        report = await csv_import.import_rows(db, user, parsed)
        await db.commit()
        extra["import_report"] = report
        return await _dashboard_response(
            request, db, web_user, days, date_from, date_to, extra=extra, user=user
        )

    # ───────────────────── Напоминания ─────────────────────

    @app.post("/settings")
    async def settings_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Настройка слотов и повторного замера — те же слоты, что в боте.

        Строки `reminders` — источник истины; job'ы APScheduler живут в
        процессе бота и пересоздаются из строк при его старте. С 0.5.6 —
        режим слота, «Повторять, если не ответил» и пауза (§6.7, §16.2):
        выключенный повтор обнуляет `followup_at` (job бота это увидит и
        ничего не отправит).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        now = datetime.now(ZoneInfo("UTC"))
        meds_slots = {m.slot for m in await medications_service.list_medications(db, user)}
        modes: dict[str, str] = {}
        for slot in SLOT_ORDER:
            mode = str(form.get(f"slot_{slot}_mode", "both"))
            if mode not in ("both", "meds", "pressure"):
                return RedirectResponse(url="/?error=reminder#reminders", status_code=303)
            if mode == "meds" and slot not in meds_slots:
                return RedirectResponse(url="/?error=reminder_meds#reminders", status_code=303)
            modes[slot] = mode
        today = now.astimezone(ZoneInfo(user.timezone)).date()
        pause_raw = str(form.get("pause", "keep"))
        if form.get("pause_off"):
            paused_until = None
        elif pause_raw == "keep":
            paused_until = user.reminders_paused_until
        elif pause_raw == "":
            paused_until = None
        elif pause_raw in ("1", "3", "7"):
            paused_until = today + timedelta(days=int(pause_raw) - 1)
        else:
            return RedirectResponse(url="/?error=reminder#reminders", status_code=303)
        for slot in SLOT_ORDER:
            local_time = _normalize_hhmm(str(form.get(f"slot_{slot}_time", "")).strip())
            enabled = bool(form.get(f"slot_{slot}_enabled"))
            if enabled and local_time is None:
                return RedirectResponse(url="/?error=reminder#reminders", status_code=303)
            reminder = (
                await db.execute(
                    select(Reminder).where(
                        Reminder.user_id == user.id, Reminder.type == slot
                    )
                )
            ).scalar_one_or_none()
            if enabled and local_time is not None:
                if reminder is None:
                    reminder = Reminder(
                        user_id=user.id,
                        type=slot,
                        local_time=local_time,
                        next_trigger_time=next_slot_occurrence(
                            local_time, user.timezone, now
                        ),
                        enabled=True,
                        mode=modes[slot],
                    )
                    db.add(reminder)
                else:
                    # Все поля слота — одним UPDATE, без смеси с параллельной правкой (§7.2).
                    await enable_slot_row(
                        db, reminder, local_time, next_slot_occurrence(local_time, user.timezone, now)
                    )
            elif reminder is not None:
                reminder.enabled = False
            if reminder is not None:
                reminder.mode = modes[slot]
        followup_enabled = bool(form.get("followup_enabled"))
        if not followup_enabled:
            await db.execute(
                update(Reminder).where(Reminder.user_id == user.id).values(followup_at=None)
            )
        user.followup_enabled = followup_enabled
        user.reminders_paused_until = paused_until
        delay_raw = str(form.get("med_remeasure_delay", ""))
        if delay_raw == "":
            user.med_remeasure_delay = None
        elif delay_raw in ("30", "60", "120"):
            user.med_remeasure_delay = int(delay_raw)
        else:
            return RedirectResponse(url="/?error=reminder#reminders", status_code=303)
        await db.flush()
        return RedirectResponse(url="/?saved=reminder#reminders", status_code=303)

    # ───────────────────── Экспорт CSV ─────────────────────

    @app.get("/export/measurements.csv")
    async def export_measurements_csv(
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """CSV измерений за период (UTF-8 с BOM; лимит — как в боте)."""
        user = await load_user(db, web_user)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        payload = await csv_export.export_measurements(
            db, user, start_day, end_day, limit=CSV_EXPORT_LIMIT
        )
        if payload is None:
            return RedirectResponse(url="/?error=too_many", status_code=303)
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{csv_export.file_name("bp", start_day, end_day)}"'
                )
            },
        )

    @app.get("/export/medications.csv")
    async def export_medications_csv(
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """CSV приёмов лекарств за период (для приверженности у врача)."""
        user = await load_user(db, web_user)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        payload = await csv_export.export_medications(
            db, user, start_day, end_day, limit=CSV_EXPORT_LIMIT
        )
        if payload is None:
            return RedirectResponse(url="/?error=too_many", status_code=303)
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{csv_export.file_name("meds", start_day, end_day)}"'
                )
            },
        )

    @app.get("/export/report.pdf")
    async def export_report_pdf(
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """PDF-отчёт пациента за период фильтра — только свои данные (§16.2, §10.1)."""
        user = await load_user(db, web_user)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        return await _pdf_report_response(
            db,
            user,
            start_day,
            end_day,
            subject=pdf_report.patient_subject(user),
            back_url="/",
            period={"days": days, "date_from": date_from, "date_to": date_to},
        )

    # ───────────────────── Кабинет врача (SPEC.md, раздел 21) ─────────────────────

    def set_csrf_cookie(request: Request, response: Response, csrf_token: str) -> None:
        """Поставить CSRF-куку (double-submit вместе с hidden field формы)."""
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
            path="/",
        )

    async def _doctor_confirm_page(
        request: Request, db: AsyncSession, token: str
    ) -> Response:
        """Ссылка доступа врача: страница подтверждения, без списания (§21).

        GET только показывает кнопку «Открыть дневник»: почтовые сканеры
        (Outlook Safe Links, антивирусы) открывают ссылки из писем заранее и
        иначе сжигали бы одноразовую ссылку. Списание — POST `/doctor/open`.
        """
        access = doctor_magic.load(token)
        if access is None or await doctor_auth.is_used(db, access.jti):
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "doctor_dashboard.html",
            {"view": "confirm", "doctor": None, "token": token, "csrf_token": csrf_token},
        )
        set_csrf_cookie(request, response, csrf_token)
        return response

    async def _doctor_signin_page(
        request: Request, db: AsyncSession, token: str
    ) -> Response:
        """Ссылка входа врача из бота: страница подтверждения, без списания (§21)."""
        signin = doctor_signin.load(token)
        if signin is None or await doctor_auth.is_used(db, signin.jti):
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "doctor_dashboard.html",
            {"view": "signin", "doctor": None, "token": token, "csrf_token": csrf_token},
        )
        set_csrf_cookie(request, response, csrf_token)
        return response

    @app.post("/doctor/signin")
    async def doctor_signin_post(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        """«Войти»: списать ссылку входа и открыть кабинет (§21).

        Связей не меняет; с пациентом в ссылке — сразу его карточка (доступ
        к ней всё равно проверяется по `status='active'`).
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        signin = doctor_signin.load(str(form.get("token", "")))
        if signin is None:
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        doctor = (
            await db.execute(select(Doctor).where(Doctor.id == signin.doctor_id))
        ).scalar_one_or_none()
        if doctor is None or not await doctor_auth.consume(db, signin):
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        target = (
            f"/doctor/patient/{signin.patient_user_id}"
            if signin.patient_user_id is not None
            else "/doctor"
        )
        response = RedirectResponse(url=target, status_code=303)
        set_doctor_cookie(request, response, doctor.id)
        return response

    @app.post("/doctor/open")
    async def doctor_open(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        """«Открыть дневник»: списать ссылку, активировать связь, войти (§21).

        Списание атомарно (`used_tokens`): из двух нажатий проходит одно.
        `revoked`-связь не активируется — пациент отозвал доступ раньше.
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        access = doctor_magic.load(str(form.get("token", "")))
        if access is None:
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        doctor = (
            await db.execute(select(Doctor).where(Doctor.id == access.doctor_id))
        ).scalar_one_or_none()
        if doctor is None or not await doctor_auth.consume(db, access):
            return RedirectResponse(url="/doctor?error=link_expired", status_code=303)
        if not await doctor_service.activate_link(db, doctor.id, access.patient_user_id):
            return RedirectResponse(url="/doctor?error=denied", status_code=303)
        response = RedirectResponse(
            url=f"/doctor/patient/{access.patient_user_id}", status_code=303
        )
        set_doctor_cookie(request, response, doctor.id)
        return response

    @app.get("/doctor", response_class=HTMLResponse)
    async def doctor_page(
        request: Request,
        db: AsyncSession = Depends(get_db),
        doctor: Optional[Doctor] = Depends(get_doctor),
    ) -> Response:
        """Кабинет врача: вход или список пациентов (§21).

        `?token=` — ссылка доступа (72 часа, одноразовая): страница
        подтверждения, вход и активация связи — по кнопке (POST `/doctor/open`).
        """
        token = request.query_params.get("token", "")
        if token:
            return await _doctor_confirm_page(request, db, token)
        signin = request.query_params.get("signin", "")
        if signin:
            return await _doctor_signin_page(request, db, signin)
        csrf_token = csrf.issue()
        if doctor is not None and not consent_service.doctor_ok(doctor):
            return RedirectResponse(url="/doctor/consent", status_code=303)
        if doctor is not None:
            patients = profile_service.sorted_patients(
                await consent_service.visible_patients(db, doctor)
            )
            response = _templates.TemplateResponse(
                request,
                "doctor_dashboard.html",
                {
                    "view": "list",
                    "doctor": doctor,
                    "patients": patients,
                    "error": request.query_params.get("error", ""),
                    "csrf_token": csrf_token,
                },
            )
        else:
            response = _templates.TemplateResponse(
                request,
                "doctor_dashboard.html",
                {
                    "view": "login",
                    "doctor": None,
                    "error": request.query_params.get("error", ""),
                    "csrf_token": csrf_token,
                },
            )
        set_csrf_cookie(request, response, csrf_token)
        return response

    @app.get("/doctor/consent", response_class=HTMLResponse)
    async def doctor_consent_page(
        request: Request,
        next_url: str = Query("/doctor", alias="next"),
        error: str = Query(""),
        doctor: Optional[Doctor] = Depends(get_doctor),
    ) -> Response:
        """К2: согласие врача — одна страница для всех путей входа (§6.11)."""
        if doctor is None:
            return RedirectResponse(url="/doctor", status_code=303)
        if consent_service.doctor_ok(doctor):
            return RedirectResponse(url=_safe_doctor_next(next_url), status_code=303)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "doctor_consent.html",
            {
                "csrf_token": csrf_token,
                "next_url": _safe_doctor_next(next_url),
                "error": error,
                "text": texts.DOCTOR_CABINET_CONSENT_TEXT,
                "checkbox_text": texts.DOCTOR_CABINET_CONSENT_CHECKBOX,
                "policy_text": texts.DOCTOR_CABINET_CONSENT_POLICY,
                "privacy_url": settings.privacy_url,
            },
        )
        set_csrf_cookie(request, response, csrf_token)
        return response

    @app.post("/doctor/consent")
    async def doctor_consent_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        doctor: Optional[Doctor] = Depends(get_doctor),
    ) -> Response:
        """«Продолжить» (К2): без отметки — снова страница с ошибкой."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        if doctor is None:
            return RedirectResponse(url="/doctor", status_code=303)
        next_url = _safe_doctor_next(str(form.get("next", "/doctor")))
        if str(form.get("agree", "")) != "1":
            query = urlencode({"next": next_url, "error": "agree"})
            return RedirectResponse(url=f"/doctor/consent?{query}", status_code=303)
        await consent_service.give_doctor_self(db, doctor, consent_service.CHANNEL_CABINET)
        return RedirectResponse(url=next_url, status_code=303)

    @app.post("/doctor/login")
    async def doctor_login(
        request: Request,
        db: AsyncSession = Depends(get_db),
        doctor: Optional[Doctor] = Depends(get_doctor),
    ) -> Response:
        """Вход врача e-mail + пароль (Argon2id; rate limiting 5 попыток/мин).

        Работает для врачей с установленным паролем: в MVP пароль при
        добавлении генерируется ботом случайным, основной вход — по ссылке.
        """
        if doctor is not None:
            return RedirectResponse(url="/doctor", status_code=303)
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        key = f"doctor:{_client_ip(request)}:{email}"
        if not limiter.allow(key):
            raise HTTPException(
                status_code=429, detail="Слишком много попыток, подождите минуту"
            )
        limiter.hit(key)
        record = (
            await db.execute(select(Doctor).where(Doctor.email == email))
        ).scalar_one_or_none()
        if record is None or not await asyncio.to_thread(
            auth.verify_password, record.password_hash, password
        ):
            return RedirectResponse(url="/doctor?error=wrong", status_code=303)
        limiter.reset(key)
        response = RedirectResponse(url="/doctor", status_code=303)
        set_doctor_cookie(request, response, record.id)
        return response

    @app.post("/doctor/logout")
    async def doctor_logout(request: Request) -> Response:
        """Выход врача: stateless-сессия снимается удалением cookie (§21)."""
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        response = RedirectResponse(url="/doctor", status_code=303)
        response.delete_cookie(DOCTOR_COOKIE, path="/")
        return response

    @app.get("/doctor/patient/{user_id}", response_class=HTMLResponse)
    async def doctor_patient(
        user_id: int,
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        shown: str = Query(""),
        db: AsyncSession = Depends(get_db),
        doctor: Doctor = Depends(require_doctor),
    ) -> Response:
        """Карточка пациента: график, таблица, сводка и заметки врача (§21).

        Только чтение: врач не редактирует записи пациента, добавляет лишь
        заметки (§21, «Логика для врача»). История — тем же компонентом по
        дням, что в ЛК (§16.2, 0.5.9), без колонки действий; `?shown=N` —
        глубина показа как в ЛК.
        """
        user = await require_patient_link(db, doctor, user_id)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        measurements = await measurements_for_period(db, user, start_day, end_day)
        summary = await stats.summary(db, user, start_day, end_day)
        daily = _daily_averages(measurements, user.timezone, start_day, end_day)
        medication_cells = await medications_service.medication_cells(db, user, measurements)
        notes = await doctor_service.notes_for_patient(db, user.id)
        csrf_token = csrf.issue()
        response = _templates.TemplateResponse(
            request,
            "doctor_dashboard.html",
            {
                "view": "patient",
                "doctor": doctor,
                "user": user,
                "user_zone": ZoneInfo(user.timezone),
                "feeling_labels": texts.FEELING_LABELS,
                "medication_cells": medication_cells,
                "measurements": measurements,
                "summary": summary,
                "daily": daily,
                "daily_max_days": DAILY_AVERAGES_MAX_DAYS,
                "notes": notes,
                "chart": _chart_payload(measurements, user.timezone),
                "chart_zones": CHART_ZONES,
                "days": days,
                "date_from": date_from,
                "date_to": date_to,
                "start_day": start_day,
                "end_day": end_day,
                "history": _history_context(
                    measurements,
                    user.timezone,
                    datetime.now(ZoneInfo(user.timezone)).date(),
                    start_day,
                    end_day,
                    shown,
                ),
                "history_can_edit": False,
                "error": request.query_params.get("error", ""),
                "saved": request.query_params.get("saved", ""),
                "csrf_token": csrf_token,
            },
        )
        set_csrf_cookie(request, response, csrf_token)
        return response

    @app.get("/doctor/patient/{user_id}/export.csv")
    async def doctor_patient_export_csv(
        user_id: int,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        db: AsyncSession = Depends(get_db),
        doctor: Doctor = Depends(require_doctor),
    ) -> Response:
        """CSV измерений пациента за период (только `status='active'`, §21)."""
        user = await require_patient_link(db, doctor, user_id)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        payload = await csv_export.export_measurements(
            db, user, start_day, end_day, limit=CSV_EXPORT_LIMIT
        )
        if payload is None:
            return RedirectResponse(
                url=f"/doctor/patient/{user_id}?error=too_many", status_code=303
            )
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{csv_export.file_name(profile_service.file_base(user) or "bp", start_day, end_day)}"'
                )
            },
        )

    @app.get("/doctor/patient/{user_id}/export.pdf")
    async def doctor_patient_export_pdf(
        user_id: int,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        db: AsyncSession = Depends(get_db),
        doctor: Doctor = Depends(require_doctor),
    ) -> Response:
        """PDF-отчёт по пациенту за период (только `status='active'`, §21, §10.1)."""
        user = await require_patient_link(db, doctor, user_id)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        return await _pdf_report_response(
            db,
            user,
            start_day,
            end_day,
            subject=pdf_report.doctor_subject_for(user),
            back_url=f"/doctor/patient/{user_id}",
            file_stem=profile_service.file_base(user),
            period={"days": days, "date_from": date_from, "date_to": date_to},
        )

    @app.post("/doctor/patient/{user_id}/note")
    async def doctor_patient_note(
        user_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db),
        doctor: Doctor = Depends(require_doctor),
    ) -> Response:
        """Заметка врача пациенту: сохраняется в `doctor_notes` (§21, §4.10).

        Видна пациенту в его личном кабинете; длина — до 1000 символов.
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await require_patient_link(db, doctor, user_id)
        try:
            await doctor_service.add_note(
                db, doctor.id, user.id, str(form.get("text", ""))
            )
        except ValueError:
            return RedirectResponse(
                url=f"/doctor/patient/{user_id}?error=note#notes", status_code=303
            )
        return RedirectResponse(
            url=f"/doctor/patient/{user_id}?saved=note#notes", status_code=303
        )

    return app


_app_cache: Optional[FastAPI] = None


def __getattr__(name: str) -> FastAPI:
    """Ленивая сборка `app` для `uvicorn web.cabinet.app:app` (SPEC.md, раздел 16.3).

    Создание отложено до обращения: импорт модуля в тестах не требует
    настроенного окружения (`.env` нужен только реальному запуску).
    """
    global _app_cache
    if name == "app":
        if _app_cache is None:
            _app_cache = create_app()
        return _app_cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
