"""Съёмка скриншотов лендинга (SPEC.md, §16.1 «Как это выглядит»).

Локальный стенд с вымышленными данными (`demo_data.py`), прод не трогается.
Подкоманды:

- `cabinet` — личный кабинет и кабинет врача (Playwright, Chromium) против
  кабинета на `--url` (по умолчанию `http://127.0.0.1:8000`, запущен на демо-БД
  с тем же `WEB_SECRET_KEY`); вход — сессионной cookie, без паролей;
- `tg-login` — разовый вход в Telegram Web по QR (окно браузера), сессия —
  Playwright `storage_state` в `qa/reports/screens/tg_state.json` (не в git);
- `bot` — кадры диалога с тестовым ботом в Telegram Web по сохранённой сессии:
  узкое окно, в кадр — только область диалога (без списка чатов и профиля);
  `--visits-only` — только «Визиты и анализы» (0.5.8, демо-БД с `--visits`).

Сырые PNG — `qa/reports/screens/raw/`, WebP для лендинга —
`qa/reports/screens/webp/` (на лендинг копируются после проверки кадров).
Рендер первой страницы PDF — `pypdfium2`, подключается на время запуска:

    uv run --group qa --with pypdfium2 python -m qa.screenshots.shot_landing cabinet

Подробности — `qa/screenshots/README.md`.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sqlite3
from pathlib import Path

from PIL import Image

from qa.seed import ensure_stand_db

OUT = Path("qa/reports/screens")
RAW = OUT / "raw"
WEBP = OUT / "webp"
TG_STATE = OUT / "tg_state.json"

DEMO_EMAIL = "ivan.petrov@example.org"
DOCTOR_EMAIL = "smirnova@example.org"

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}

#: Ширина WebP для лендинга (десктопные кадры); мобильные — 780 (390 × 2).
WEBP_WIDTH = 1200
WEBP_QUALITY = 80


def save(png: bytes, name: str, width: int = WEBP_WIDTH) -> None:
    """Сохранить кадр: сырой PNG и WebP нужной ширины (без увеличения)."""
    RAW.mkdir(parents=True, exist_ok=True)
    WEBP.mkdir(parents=True, exist_ok=True)
    (RAW / f"{name}.png").write_bytes(png)
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    image.save(WEBP / f"{name}.webp", "WEBP", quality=WEBP_QUALITY, method=6)
    print(f"  {name}: {image.width}×{image.height}, {(WEBP / f'{name}.webp').stat().st_size // 1024} КБ")


def _ids(db: Path) -> tuple[int, int, int]:
    """(web_user_id демо-пациента, user_id демо-пациента, doctor_id)."""
    con = sqlite3.connect(db)
    try:
        web_user_id, user_id = con.execute(
            "SELECT id, user_id FROM web_users WHERE email = ?", (DEMO_EMAIL,)
        ).fetchone()
        (doctor_id,) = con.execute("SELECT id FROM doctors WHERE email = ?", (DOCTOR_EMAIL,)).fetchone()
    finally:
        con.close()
    return web_user_id, user_id, doctor_id


async def _session_token(db: Path, web_user_id: int) -> str:
    """Сессия кабинета для демо-пациента (как после входа), без пароля."""
    from bot.db import create_engine, create_session_factory
    from web.cabinet import auth

    engine = create_engine(str(db))
    async with create_session_factory(engine)() as session:
        token = await auth.create_web_session(session, web_user_id)
        await session.commit()
    await engine.dispose()
    return token


def _clip(
    page, top_selector: str, bottom_selector: str, pad: int = 16, width: int | None = None
) -> dict:
    """Область страницы от верха одного элемента до низа другого (координаты страницы).

    Снизу отступ вдвое меньше — не захватить начало следующего блока.
    """
    top = page.locator(top_selector).first.bounding_box()
    bottom = page.locator(bottom_selector).last.bounding_box()
    scroll = page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
    y = top["y"] + scroll["y"] - pad
    return {
        "x": 0,
        "y": max(0, y),
        "width": width or page.viewport_size["width"],
        "height": bottom["y"] + scroll["y"] + bottom["height"] + pad // 2 - max(0, y),
    }


#: Скрыть блок «Средние по дням» (§16.2, 0.5.3) в кадрах, где нужны сводка и графики.
_HIDE_DAILY = "#daily-averages { display: none !important; }"


def _wait_charts(page) -> None:
    """Дождаться, пока Chart.js нарисует три панели сводки."""
    page.wait_for_function(
        "() => ['bp-chart','bp-chart-dia','bp-chart-pulse'].every(id => {"
        " const c = document.getElementById(id); return c && c.width > 0 && window.Chart"
        " && Chart.getChart(c); })",
        timeout=15000,
    )
    page.wait_for_timeout(600)  # анимация графиков


def _import_csv(db: Path, user_id: int) -> bytes:
    """CSV архива для кадра импорта: 4 новые строки, 1 дубль, 1 ошибка (§16.2)."""
    con = sqlite3.connect(db)
    (existing,) = con.execute(
        "SELECT timestamp FROM measurements WHERE user_id = ? ORDER BY timestamp LIMIT 1", (user_id,)
    ).fetchone()
    con.close()
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    dup_local = (
        datetime.fromisoformat(str(existing)[:19]).replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Europe/Minsk")).strftime("%d.%m.%Y %H:%M")
    )
    rows = [
        "timestamp_local;systolic;diastolic;pulse;medication_taken;feeling;note",
        "02.09.2026 08:10;152;96;74;да;норма;из блокнота",
        "02.09.2026 20:30;148;92;70;да;;",
        "03.09.2026 08:05;155;98;76;нет;головная боль;",
        "03.09.2026 20:40;146;90;72;да;;",
        f"{dup_local};140;88;70;да;;",
        "01.01.2099 08:00;140;90;70;;;",
    ]
    return ("\r\n".join(rows) + "\r\n").encode("utf-8-sig")


def shoot_cabinet(url: str, db: Path, secret: str) -> None:
    """Кадры личного кабинета и кабинета врача (десктоп + мобильные)."""
    from playwright.sync_api import sync_playwright

    from web.cabinet import doctor_auth
    from web.cabinet.app import DOCTOR_COOKIE, SESSION_COOKIE

    web_user_id, user_id, doctor_id = _ids(db)
    token = asyncio.run(_session_token(db, web_user_id))
    host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    cookies = [
        {"name": SESSION_COOKIE, "value": token, "domain": host, "path": "/"},
        {"name": DOCTOR_COOKIE, "value": doctor_auth.DoctorSession(secret).make(doctor_id),
         "domain": host, "path": "/"},
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--lang=ru-RU"])  # русские подписи полей даты и файла
        context = browser.new_context(viewport=DESKTOP, locale="ru-RU", timezone_id="Europe/Minsk")
        context.add_cookies(cookies)
        page = context.new_page()

        print("Личный кабинет:")
        page.goto(f"{url}/?days=7")
        _wait_charts(page)
        save(page.locator("#daily-averages").screenshot(), "cabinet-daily")
        # Кадр графиков — без таблицы «Средние по дням» (у неё свой кадр, 0.5.3).
        page.add_style_tag(content=_HIDE_DAILY)
        save(page.locator("#summary").screenshot(), "cabinet-charts")

        page.goto(f"{url}/?days=7#history")
        rows = page.locator("#history tbody tr")
        last = min(rows.count(), 10)
        save(page.screenshot(full_page=True, clip=_clip(page, "#history h2", f"#history tbody tr:nth-child({last})")),
             "cabinet-history")

        page.goto(f"{url}/?add=1#history")
        form = page.locator("#measurement-form")
        form.locator('input[name="time"]').fill("08:10")
        form.locator('input[name="time"]').dispatch_event("change")  # предотметка утреннего слота
        form.locator('input[name="systolic"]').fill("132")
        form.locator('input[name="diastolic"]').fill("84")
        form.locator('input[name="pulse"]').fill("70")
        page.check('#measurement-form input[name="medication_taken"]')
        save(page.screenshot(full_page=True, clip=_clip(page, "#history h3", "#measurement-form .row >> nth=-1", width=640)),
             "cabinet-add-form")

        page.goto(f"{url}/#medications")
        save(page.locator("#medications").screenshot(), "cabinet-medications")

        phone = browser.new_context(
            viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True,
            locale="ru-RU", timezone_id="Europe/Minsk",
        )
        phone.add_cookies(cookies)
        mobile = phone.new_page()
        mobile.goto(f"{url}/?days=7#history")
        save(mobile.screenshot(full_page=True, clip=_clip(mobile, "#history h2", "#history tbody tr:nth-child(3)")),
             "cabinet-history-mobile", width=780)

        print("Кабинет врача:")
        page.goto(f"{url}/doctor")
        save(page.locator("main .card").first.screenshot(), "doctor-list")

        page.goto(f"{url}/doctor/patient/{user_id}?days=7")
        _wait_charts(page)
        save(page.screenshot(full_page=True), "doctor-card")

        pdf = page.request.get(f"{url}/doctor/patient/{user_id}/export.pdf?days=30").body()
        import pypdfium2

        first = pypdfium2.PdfDocument(pdf)[0].render(scale=2).to_pil()
        buffer = io.BytesIO()
        first.save(buffer, "PNG")
        save(buffer.getvalue(), "doctor-pdf")

        mobile.goto(f"{url}/doctor/patient/{user_id}?days=7")
        _wait_charts(mobile)
        mobile.add_style_tag(content=_HIDE_DAILY)  # сводка и графики — без таблицы по дням
        save(mobile.screenshot(full_page=True, clip=_clip(mobile, "#summary .facts", "#bp-chart")),
             "doctor-card-mobile", width=780)

        # Импорт меняет демо-БД (добавляет записи) — снимается последним.
        print("Импорт CSV:")
        page.goto(f"{url}/?import=1#history")
        page.set_input_files(
            'input[type="file"]',
            files=[{"name": "bloknot.csv", "mimeType": "text/csv", "buffer": _import_csv(db, user_id)}],
        )
        page.locator('form[action="/import"] button[type="submit"]').click()
        page.wait_for_load_state()
        save(page.screenshot(full_page=True, clip=_clip(page, "#history h3", "#history li:has-text('Строка')")),
             "cabinet-import")
        browser.close()


TG_WEB = "https://web.telegram.org/k/"


def tg_login(timeout_min: int = 10) -> None:
    """Разовый вход в Telegram Web по QR: окно браузера, сессия — в `TG_STATE`.

    Вход делает человек (QR с телефона: «Настройки → Устройства → Подключить
    устройство»; облачный пароль, если есть, — вводится в этом же окне).
    Скрипт только ждёт появления списка чатов и сохраняет `storage_state`
    вместе с IndexedDB — повторная съёмка без QR.
    """
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--lang=ru-RU"])
        context = browser.new_context(viewport={"width": 1000, "height": 800}, locale="ru-RU")
        page = context.new_page()
        page.goto(TG_WEB)
        print(f"Окно Telegram Web открыто: отсканируйте QR (жду до {timeout_min} мин)…", flush=True)
        page.wait_for_selector(".chatlist", timeout=timeout_min * 60_000)
        page.wait_for_timeout(5000)  # дописать ключи в хранилище
        context.storage_state(path=str(TG_STATE), indexed_db=True)
        browser.close()
    print(f"Вход выполнен, сессия сохранена: {TG_STATE}")


BOT_USERNAME_ENV = "QA_BOT_USERNAME"

#: Скрыть то, что не относится к диалогу: счётчик непрочитанных других чатов
#: на кнопке «назад» (личные данные аккаунта) и плашки дат.
_TG_CSS = """
#column-center .sidebar-close-button .badge, #column-center .badge { display: none !important; }
.bubble.service.is-date, .bubbles-date-group .is-sticky { display: none !important; }
"""

#: Оставить видимыми только последние `n` пузырей диалога (старые прогоны QA
#: скрываются на время кадра; история чата не удаляется).
_TG_KEEP_LAST = """
(n) => {
  const all = [...document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)')];
  all.forEach((el, i) => { if (i >= all.length - n) el.setAttribute('data-keep', '1');
                           else el.removeAttribute('data-keep'); });
  let style = document.getElementById('shot-keep');
  if (!style) { style = document.createElement('style'); style.id = 'shot-keep'; document.head.append(style); }
  style.textContent = '#column-center .bubbles-inner .bubble:not([data-keep]) { display: none !important; }';
}
"""
_TG_SHOW_ALL = """
() => { const style = document.getElementById('shot-keep'); if (style) style.textContent = '';
        document.querySelectorAll('[data-keep]').forEach(el => el.removeAttribute('data-keep')); }
"""

#: Прокрутить диалог до последнего сообщения: после прихода напоминания Telegram
#: Web может оставить прокрутку чуть выше низа, и низ последней кнопки уходит
#: под поле ввода — кадр обрезает его. Плавную прокрутку (`scroll-behavior:
#: smooth`) на время прыжка выключаем: кадр снимался посреди анимации.
_TG_SCROLL_BOTTOM = """
() => { const inner = document.querySelector('#column-center .bubbles-inner');
        if (!inner) return;
        for (let n = inner.parentElement; n; n = n.parentElement) {
          if (n.scrollHeight > n.clientHeight + 5) {
            const behavior = n.style.scrollBehavior;
            n.style.scrollBehavior = 'auto';
            n.scrollTop = n.scrollHeight;
            n.style.scrollBehavior = behavior;
          }
        } }
"""

#: Проверка, что все прокручиваемые предки диалога упёрлись в низ (см.
#: `_TG_SCROLL_BOTTOM`). Только контейнеры со скроллом (`overflow-y: auto|scroll`):
#: у предков с `overflow: visible` scrollHeight больше clientHeight, но
#: прокрутки у них нет.
_TG_AT_BOTTOM = """
() => { const inner = document.querySelector('#column-center .bubbles-inner');
        if (!inner) return false;
        for (let n = inner.parentElement; n; n = n.parentElement) {
          const oy = getComputedStyle(n).overflowY;
          if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay')
              && n.scrollHeight - n.scrollTop - n.clientHeight > 2) return false;
        }
        return true; }
"""

#: Скрыть/показать поле ввода на время кадра: оно перекрывает низ последней
#: кнопки, и кадр «Напоминание о визите» обрезал «Перенести | Отменить визит».
_TG_HIDE_COMPOSER = """
(hide) => { const el = document.querySelector('#column-center .chat-input');
            if (el) el.style.display = hide ? 'none' : ''; }
"""

#: Низ видимой части последнего оставленного пузыря (его последней кнопки),
#: viewport px; -1 — пузыря нет.
_TG_KEPT_BOTTOM = """
() => { const kept = [...document.querySelectorAll('#column-center .bubble[data-keep]')].pop();
        if (!kept) return -1;
        const buttons = kept.querySelectorAll('.reply-markup-button');
        const last = buttons.length ? buttons[buttons.length - 1] : kept;
        return last.getBoundingClientRect().bottom; }
"""


#: Номер inline-кнопки последнего сообщения с подписью `label` (-1 — нет).
_TG_BUTTON_INDEX = """
(label) => {
  const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
  const text = el => { const c = el.cloneNode(true);
    c.querySelectorAll('img').forEach(i => i.replaceWith(i.alt || ''));
    return strip(c.textContent); };
  const all = document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)');
  if (!all.length) return -1;
  const buttons = [...all[all.length - 1].querySelectorAll('.reply-markup-button')];
  return buttons.findIndex(b => text(b) === strip(label));
}
"""


class TelegramDialog:
    """Диалог с тестовым ботом в Telegram Web K: отправка, нажатия, ожидание, кадр."""

    def __init__(self, page) -> None:
        self.page = page

    def last(self):
        return self.page.locator("#column-center .bubbles-inner .bubble:not(.is-date)").last

    def wait_text(self, text: str, timeout: int = 30_000) -> None:
        """Дождаться последнего пузыря с текстом (ответ бота или правка сообщения)."""
        self.page.wait_for_function(
            """(t) => { const all = document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)');
                        return all.length && all[all.length - 1].innerText.includes(t); }""",
            arg=text, timeout=timeout,
            # Опрос раз в 0,5 с, а не на каждом кадре: за 10 минут ожидания
            # напоминания опрос по requestAnimationFrame ронял вкладку Telegram Web.
            polling=500,
        )
        self.page.wait_for_timeout(700)  # дорисовка клавиатуры

    def send(self, text: str) -> None:
        box = self.page.locator(".input-message-input[contenteditable='true']").first
        box.click()
        box.type(text)
        self.page.keyboard.press("Enter")

    def tap(self, label: str) -> None:
        """Нажать inline-кнопку последнего сообщения с точным текстом.

        Пауза перед нажатием — как у человека: антидребезг бота
        (`DebounceMiddleware`) отбрасывает слишком частые нажатия.
        """
        self.page.wait_for_timeout(1300)
        # Эмодзи («📅 По дням», «◀») Telegram Web рисует картинками: подпись
        # кнопки сравнивается вместе с `alt` этих картинок, без U+FE0F.
        index = -1
        for _ in range(60):
            index = self.page.evaluate(_TG_BUTTON_INDEX, label)
            if index >= 0:
                break
            self.page.wait_for_timeout(500)
        if index < 0:
            raise AssertionError(f"нет кнопки {label!r} в последнем сообщении")
        self.last().locator(".reply-markup-button").nth(index).click()

    def shot(self, name: str, keep: int, gap: int = 28) -> None:
        """Кадр: шапка чата + последние `keep` пузырей, без поля ввода.

        Пустой фон между шапкой и сообщениями вырезается: шапка и полоса
        сообщений склеиваются с отступом `gap` CSS-пикселей. Низ кадра — на 15 px
        ниже последней кнопки (полоса обоев под ней, как в кадрах, снятых сразу
        после ответа бота): поле ввода на время кадра скрывается, иначе оно
        перекрывает низ кнопки и кадр обрезает её («Напоминание о визите»).
        """
        self.page.evaluate(_TG_HIDE_COMPOSER, True)
        self.page.wait_for_timeout(300)  # пересчёт раскладки Telegram Web
        column = self.page.locator("#column-center").bounding_box()
        limit = column["y"] + column["height"] - 15
        last_bottom = -1.0
        for _ in range(6):
            # Web перерисовывает список сообщений — пометки повторяем каждый раз.
            self.page.evaluate(_TG_KEEP_LAST, keep)
            self.page.evaluate(_TG_SCROLL_BOTTOM)
            # Штатная кнопка «вниз» — тот же путь, которым Web подматывает
            # свежие сообщения; прыжок scrollTop'ом — запасной вариант.
            go_down = self.page.locator("#column-center .bubbles-go-down")
            if go_down.count() and go_down.first.is_visible():
                go_down.first.click()
            self.page.wait_for_timeout(700)
            last_bottom = float(self.page.evaluate(_TG_KEPT_BOTTOM))
            if 0 < last_bottom <= limit:
                break
        else:
            self.page.evaluate(_TG_HIDE_COMPOSER, False)
            self.page.evaluate(_TG_SHOW_ALL)
            raise AssertionError(
                f"{name}: низ кнопок не помещается в кадр ({last_bottom=:.0f} > {limit=:.0f})"
            )
        self.page.wait_for_timeout(200)
        top_bar = self.page.locator("#column-center .chat .topbar").first.bounding_box()
        first_kept = self.page.locator("#column-center .bubble[data-keep]").first.bounding_box()
        header_bottom = top_bar["y"] + top_bar["height"]
        content_top = max(header_bottom, first_kept["y"] - gap)
        crop_bottom = min(column["y"] + column["height"], last_bottom + 15)
        full = Image.open(io.BytesIO(self.page.screenshot(clip={
            "x": column["x"], "y": column["y"], "width": column["width"],
            "height": crop_bottom - column["y"],
        })))
        scale = full.width / column["width"]
        cut_top, cut_bottom = round((header_bottom - column["y"]) * scale), round((content_top - column["y"]) * scale)
        header, content = full.crop((0, 0, full.width, cut_top)), full.crop((0, cut_bottom, full.width, full.height))
        frame = Image.new("RGB", (full.width, header.height + content.height))
        frame.paste(content, (0, header.height))
        frame.paste(header, (0, 0))
        buffer = io.BytesIO()
        frame.save(buffer, "PNG")
        save(buffer.getvalue(), name, width=780)
        self.page.evaluate(_TG_HIDE_COMPOSER, False)
        self.page.evaluate(_TG_SHOW_ALL)


#: Длина своего периода в кадре календаря, дней (§6.5, 0.5.3).
CUSTOM_PERIOD_DAYS = 14


def shoot_bot(bot_username: str, stats_only: bool = False, visits_only: bool = False) -> None:
    """Кадры бота в Telegram Web (P1 А): узкое окно — только диалог.

    `stats_only` — только статистика: сводка, «По дням», календарь своего
    периода (без ожидания напоминания и записи давления).
    `visits_only` — только «Визиты и анализы» (0.5.8): напоминание накануне и
    список (демо-БД собрана с `demo_data --visits`, eve-напоминание приходит
    ближайшим тиком бота после запуска).
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from playwright.sync_api import sync_playwright

    from bot import texts

    if not TG_STATE.exists():
        raise SystemExit(f"Нет {TG_STATE}: сначала `tg-login`")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--lang=ru-RU"])
        context = browser.new_context(
            storage_state=str(TG_STATE), viewport={"width": 420, "height": 900},
            device_scale_factor=2, locale="ru-RU",
        )
        page = context.new_page()
        page.goto(f"{TG_WEB}#@{bot_username}")
        page.wait_for_selector(".input-message-input", timeout=60_000)
        page.add_style_tag(content=_TG_CSS)
        page.wait_for_timeout(3000)
        # Telegram Web открывает чат на позиции из кэша сессии (старые сообщения):
        # «вниз» — к концу переписки, иначе последний пузырь в DOM — не последний в чате.
        go_down = page.locator("#column-center .bubbles-go-down")
        if go_down.count() and go_down.first.is_visible():
            go_down.first.click()
            page.wait_for_timeout(3000)
        # Прокрутка вниз принудительно: при позиции в середине переписки хвост
        # истории в DOM не подгружен и wait_text не видит уже пришедшее сообщение.
        page.evaluate(_TG_SCROLL_BOTTOM)
        page.wait_for_timeout(2000)
        dialog = TelegramDialog(page)
        print("Бот:")

        if visits_only:
            # Напоминание накануне — первым: тик бота шлёт его в любую минуту
            # после старта, а после нажатий в меню его пузырь перестаёт быть
            # последним и wait_text его не увидит.
            print("  жду напоминание о визите (до 10 мин)…", flush=True)
            dialog.wait_text("Завтра визит", timeout=600_000)
            dialog.shot("bot-visit-reminder", keep=1)

            dialog.send("/start")
            dialog.wait_text(texts.BTN_MEASURE)  # главное меню
            dialog.tap(texts.BTN_APPOINTMENTS)
            dialog.wait_text(texts.AP_SOON)  # эмодзи заголовка в Telegram Web — картинка
            dialog.shot("bot-visits", keep=1)
            browser.close()
            return

        if not stats_only:
            # Напоминание — первым: после свежей записи давления бот слот не шлёт (§6.7).
            print("  жду напоминание (до 10 мин)…", flush=True)
            dialog.wait_text("Пора измерить давление", timeout=600_000)
            dialog.shot("bot-reminder", keep=1)

            dialog.send("/start")
            dialog.wait_text(texts.BTN_MEASURE)
            dialog.shot("bot-menu", keep=2)

            dialog.tap(texts.BTN_MEASURE)
            dialog.wait_text(texts.ASK_SYSTOLIC)
            for digit in "135":
                dialog.tap(digit)
            dialog.wait_text(texts.KEYPAD_ENTERED.format(value=135))
            dialog.shot("bot-keypad", keep=1)

            dialog.tap(texts.BTN_DONE)
            for value in ("85", "72"):
                dialog.page.wait_for_timeout(800)
                for digit in value:
                    dialog.tap(digit)
                dialog.wait_text(texts.KEYPAD_ENTERED.format(value=value))
                dialog.tap(texts.BTN_DONE)
            dialog.wait_text(texts.BTN_TOOK_MED)
            dialog.shot("bot-saved", keep=1)

            dialog.tap(texts.BTN_TOOK_MED)
            dialog.wait_text(texts.BTN_MED_YES)
            dialog.tap(texts.BTN_MED_YES)
            dialog.wait_text(texts.MEDS_INTAKE_ASK)
            dialog.shot("bot-meds", keep=2)
            dialog.tap(texts.BTN_CANCEL)  # без отметок: журнал не трогаем
            dialog.page.wait_for_timeout(1000)

            # «Дневник за сегодня» со строкой лекарств (§6.4, 0.5.5).
            dialog.send("/start")
            dialog.wait_text(texts.BTN_DIARY)
            dialog.tap(texts.BTN_DIARY)
            dialog.wait_text("Лекарства:")  # эмодзи 💊 в Telegram Web — картинка
            dialog.shot("bot-diary", keep=1)

            # «Настройки» → «Напоминания»: пауза, режим слота, повтор (§6.6, 0.5.6).
            dialog.send("/start")
            dialog.wait_text(texts.BTN_SETTINGS)
            dialog.tap(texts.BTN_SETTINGS)
            dialog.wait_text(texts.BTN_REMINDERS)
            dialog.tap(texts.BTN_REMINDERS)
            dialog.wait_text("Повторять, если не ответил")
            dialog.shot("bot-reminder-settings", keep=1)

        dialog.send("/start")
        dialog.wait_text(texts.BTN_STATS)
        dialog.tap(texts.BTN_STATS)
        dialog.wait_text(texts.BTN_7_DAYS)
        dialog.tap(texts.BTN_7_DAYS)
        dialog.wait_text("За 7 дней")
        dialog.shot("bot-stats", keep=1)

        # «📅 По дням» и «📆 Свой период» (§6.5, 0.5.3).
        dialog.tap(texts.BTN_BY_DAYS)
        dialog.wait_text("Средние по дням")
        dialog.shot("bot-daily", keep=1)
        dialog.tap(texts.BTN_BACK)  # к фото сводки
        dialog.wait_text("За 7 дней")
        dialog.tap(texts.BTN_BACK)  # к выбору периода
        dialog.wait_text("Свой период")  # эмодзи кнопки в Telegram Web — картинка
        dialog.tap(texts.BTN_CUSTOM_PERIOD)
        dialog.wait_text(texts.STATS_CAL_FIRST)
        today = datetime.now(ZoneInfo("Europe/Minsk")).date()
        start = today - timedelta(days=CUSTOM_PERIOD_DAYS - 1)
        if (start.year, start.month) != (today.year, today.month):
            dialog.tap("◀")
            dialog.wait_text(f"{texts.STATS_CAL_MONTHS[start.month - 1]} {start.year}")
        dialog.tap(str(start.day))
        dialog.wait_text(texts.STATS_CAL_LAST.format(date=start.strftime("%d.%m")))
        dialog.shot("bot-calendar", keep=1)
        if (start.year, start.month) != (today.year, today.month):
            dialog.tap("▶")
            dialog.wait_text(f"{texts.STATS_CAL_MONTHS[today.month - 1]} {today.year}")
        dialog.tap(str(today.day))
        dialog.wait_text("За период")

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Скриншоты лендинга (§16.1)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cabinet = sub.add_parser("cabinet", help="личный кабинет и кабинет врача")
    cabinet.add_argument("--url", default="http://127.0.0.1:8000")
    cabinet.add_argument("--db", default="data/screens-test-stand.sqlite3")
    sub.add_parser("tg-login", help="разовый вход в Telegram Web по QR")
    bot = sub.add_parser("bot", help="кадры диалога с тестовым ботом (бот запущен на демо-БД)")
    bot.add_argument(
        "--stats-only", action="store_true",
        help="только статистика: сводка, «По дням», календарь (без напоминания и записи)",
    )
    bot.add_argument(
        "--visits-only", action="store_true",
        help="только «Визиты и анализы» (0.5.8): напоминание накануне и список "
             "(демо-БД собрана с `demo_data --visits`)",
    )
    args = parser.parse_args()
    if args.cmd == "tg-login":
        tg_login()
    elif args.cmd == "bot":
        username = os.environ.get(BOT_USERNAME_ENV, "").strip().lstrip("@")
        if not username:
            raise SystemExit(f"{BOT_USERNAME_ENV} не задан: username тестового бота")
        shoot_bot(username, stats_only=args.stats_only, visits_only=args.visits_only)
    elif args.cmd == "cabinet":
        secret = os.environ.get("WEB_SECRET_KEY", "")
        if not secret:
            raise SystemExit("WEB_SECRET_KEY не задан: тот же ключ, что у запущенного кабинета")
        shoot_cabinet(args.url.rstrip("/"), ensure_stand_db(args.db), secret)


if __name__ == "__main__":
    main()
