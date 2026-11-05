"""Личный кабинет: FastAPI + Jinja2 (SPEC.md, раздел 16.2).

Запуск отдельным процессом рядом с ботом: `uvicorn web.cabinet.app:app`
(HTTPS — за reverse proxy с Let's Encrypt; cookie получают флаг Secure при
HTTPS-доступе — по схеме запроса или `CABINET_URL`. По локальному HTTP флаг
снимается: иначе клиент не возвращает Secure-куки обратно и все формы
падают с 403 на CSRF-проверке). Одна SQLite в WAL с ботом; сессия БД на
HTTP-запрос (`Depends`); модели — из `bot/models.py` (SPEC.md, раздел 16.3).

Содержимое: личная карточка, история с фильтрами, сводка с графиком и CSV,
CRUD лекарств, настройки напоминаний. Платёжных данных нет — только ссылка
на внешнюю страницу поддержки из бота.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.config import Settings, get_settings
from bot.db import create_engine, create_session_factory, init_db
from bot.models import Measurement, Medication, Reminder, User, WebUser
from bot.services import csv_export
from bot.services import medications as medications_service
from bot.services import stats
from bot.services.measurements import measurements_for_period
from bot.services.reminders import next_slot_occurrence
from web.cabinet import auth

#: Имя сессионной cookie (SPEC.md, раздел 16.2: HTTP-only, 7 дней).
SESSION_COOKIE = "cabinet_session"
#: Имя cookie с CSRF-токеном (double-submit вместе с hidden field).
CSRF_COOKIE = "csrf_token"

#: Сессия живёт 7 дней — в секундах, для Max-Age cookie.
SESSION_MAX_AGE = 7 * 24 * 3600

#: Порог «слишком много записей» для CSV (SPEC.md: лимит выгрузки).
CSV_EXPORT_LIMIT = 5000

#: Слоты напоминаний, как в боте (SPEC.md, раздел 4.3).
SLOT_ORDER = ("morning", "noon", "evening")

#: Человекочитаемые отметки приёма лекарства в истории.
MEDICATION_TAKEN_LABELS = {True: "Принято", False: "Пропущено", None: "—"}

_BASE_DIR = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


# ─────────────────────────── Вспомогательное ───────────────────────────


def _valid_hhmm(value: str) -> bool:
    """Время в формате HH:MM с реальными часами и минутами."""
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return len(parts[0]) == 2 and len(parts[1]) == 2 and 0 <= hour <= 23 and 0 <= minute <= 59


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


# ─────────────────────────── Приложение ───────────────────────────


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
        settings = get_settings()
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Создать таблицы при старте (идемпотентно)."""
        if engine is not None:
            await init_db(engine)
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Дневник гипертоника — личный кабинет", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.magic = magic
    app.state.csrf = csrf
    app.state.limiter = limiter
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

    async def require_user(
        web_user: Optional[WebUser] = Depends(get_web_user),
    ) -> WebUser:
        """Требовать вход: без сессии — редирект на страницу входа."""
        if web_user is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
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
        if record is None or not auth.verify_password(record.password_hash, password):
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
            user_id=owner.id, email=email, password_hash=auth.hash_password(password)
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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        days: str = Query("7"),
        date_from: str = Query(""),
        date_to: str = Query(""),
        error: str = Query(""),
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Личная карточка, история, сводка, лекарства и напоминания."""
        user = await load_user(db, web_user)
        start_day, end_day = _resolve_period(user.timezone, days, date_from, date_to)
        measurements = await measurements_for_period(db, user, start_day, end_day)
        summary = await stats.summary(db, user, start_day, end_day)
        medications = await medications_service.list_medications(db, user)
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
        response = _templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "web_user": web_user,
                "user": user,
                "user_zone": ZoneInfo(user.timezone),
                "slot_order": SLOT_ORDER,
                "slot_labels": texts.SLOT_LABELS,
                "feeling_labels": texts.FEELING_LABELS,
                "medication_labels": MEDICATION_TAKEN_LABELS,
                "measurements": measurements,
                "summary": summary,
                "medications": medications,
                "reminders": {r.type: r for r in reminders},
                "chart": _chart_payload(measurements, user.timezone),
                "days": days,
                "date_from": date_from,
                "date_to": date_to,
                "start_day": start_day,
                "end_day": end_day,
                "error": error,
                "saved": request.query_params.get("saved", ""),
                "csrf_token": csrf_token,
                "today": datetime.now(ZoneInfo(user.timezone)).date(),
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
            web_user.password_hash = auth.hash_password(new_password)
        user.target_systolic = target_systolic
        user.target_diastolic = target_diastolic
        user.timezone = tz_name
        await db.flush()
        return RedirectResponse(url="/?saved=profile", status_code=303)

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
        slots = [slot for slot in SLOT_ORDER if form.get(f"slot_{slot}")]
        if not (1 <= len(name) <= 100) or not (1 <= len(dosage) <= 50):
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
        if not (1 <= len(name) <= 100) or not (1 <= len(dosage) <= 50) or slot not in SLOT_ORDER:
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

    # ───────────────────── Напоминания ─────────────────────

    @app.post("/settings")
    async def settings_submit(
        request: Request,
        db: AsyncSession = Depends(get_db),
        web_user: WebUser = Depends(require_user),
    ) -> Response:
        """Настройка слотов и повторного замера — те же слоты, что в боте.

        Строки `reminders` — источник истины; job'ы APScheduler живут в
        процессе бота и пересоздаются из строк при его старте.
        """
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        user = await load_user(db, web_user)
        now = datetime.now(ZoneInfo("UTC"))
        for slot in SLOT_ORDER:
            local_time = str(form.get(f"slot_{slot}_time", "")).strip()
            enabled = bool(form.get(f"slot_{slot}_enabled"))
            if enabled and not _valid_hhmm(local_time):
                return RedirectResponse(url="/?error=reminder#reminders", status_code=303)
            reminder = (
                await db.execute(
                    select(Reminder).where(
                        Reminder.user_id == user.id, Reminder.type == slot
                    )
                )
            ).scalar_one_or_none()
            if enabled:
                if reminder is None:
                    reminder = Reminder(
                        user_id=user.id,
                        type=slot,
                        local_time=local_time,
                        next_trigger_time=next_slot_occurrence(
                            local_time, user.timezone, now
                        ),
                        enabled=True,
                    )
                    db.add(reminder)
                else:
                    reminder.local_time = local_time
                    reminder.next_trigger_time = next_slot_occurrence(
                        local_time, user.timezone, now
                    )
                    reminder.enabled = True
            elif reminder is not None:
                reminder.enabled = False
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
