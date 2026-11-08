"""Общие фикстуры QA-стенда (SPEC.md, §22).

E2E-тесты запускаются отдельно от юнит-тестов (`testpaths` их не включает):

    pytest qa/ --html=qa/reports/report.html --self-contained-html

Окружение поднимается `docker compose -f qa/docker-compose.qa.yml up` (бот +
кабинет + контейнер тестов). Переменные: `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, `TELEGRAM_TEST_PHONE` (§3), плюс `QA_BOT_USERNAME`
(тестовый бот) и `BOT_TOKEN` / `ADMIN_TELEGRAM_IDS` для итогового отчёта.

Telegram-часть — Telethon (юзербот; CLAUDE.md: не aiogram). Веб-часть —
Playwright (Chromium). Скриншоты каждого ключевого действия сохраняются в
`qa/reports/screenshots/` (CLAUDE.md, «Скриншоты»). В конце прогона админу в
Telegram уходит краткий отчёт (§22): «✅ Все тесты пройдены. Скриншоты:
[ссылка]» — штатным Bot API бота, чтобы не зависеть от диалогов юзербота.

Первый запуск Telethon требует интерактивной авторизации (код подтверждения
из Telegram); сессия сохраняется в `qa/reports/telethon.session` и далее
переиспользуется (файл под `*.session` в `.gitignore`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from typing import Awaitable, Callable, Iterator, Optional

import pytest
import pytest_asyncio

# Импорты playwright/telethon — внутри фикстур (лениво): юнит-окружению
# тяжёлые QA-зависимости не нужны, а аннотации типов отложены (PEP 563).

#: Авто-sleep на flood wait у Telethon «проглатывается» молча — делаем видимым.
logging.getLogger("telethon").setLevel(logging.WARNING)

#: Папка отчётов (SPEC.md, §5: `qa/reports/`).
REPORTS_DIR = os.path.join("qa", "reports")

#: Папка скриншотов (CLAUDE.md, «Скриншоты»).
SHOTS_DIR = os.path.join(REPORTS_DIR, "screenshots")

# CLAUDE.md, «Скриншоты»: «Убедись, что папка для скриншотов создана (os.makedirs)».
os.makedirs(SHOTS_DIR, exist_ok=True)


def save_screenshot(page: Page, step_name: str) -> None:
    """Скриншот ключевого действия в `qa/reports/screenshots/` (CLAUDE.md)."""
    os.makedirs("qa/reports/screenshots", exist_ok=True)
    page.screenshot(path=f"qa/reports/screenshots/{step_name}.png")


# ─────────────────────────── Переменные окружения ───────────────────────────


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(*names: str) -> Optional[dict[str, str]]:
    """Переменные стенда; None — окружение не настроено (тесты скипаются)."""
    values = {name: _env(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"QA-стенд не настроен: нет переменных {', '.join(missing)}")
    return values  # type: ignore[return-value]


# ─────────────────────── Telethon в синхронных тестах ───────────────────────


class TgLoop:
    """Event loop Telethon в собственном потоке.

    Playwright (sync API) держит loop главного потока «запущенным» через
    greenlet, поэтому корутины Telethon выполняются в отдельном потоке.
    Планировать — только через `run()`: свойство `TelegramClient.loop`
    контекстно-зависимо (`helpers.get_running_loop()`) и из главного потока
    указывает на loop Playwright.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, daemon=True, name="tg-client-loop"
        )
        self._thread.start()

    def run(self, coro, timeout: float = 60.0):
        """Выполнить корутину Telethon в этом loop (из любого потока)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


async def _login(client: TelegramClient, phone: str) -> None:
    """Подключиться и авторизоваться; первый запуск — интерактивный код (§22)."""
    await client.connect()
    if not await client.is_user_authorized():
        await client.start(phone=phone)


async def _logout(client: TelegramClient) -> None:
    """Корутина отключения (двойные sync/async-обёртки вызывать вне loop нельзя)."""
    await client.disconnect()


# ─────────────────────────── БД стенда ───────────────────────────


class QaDb:
    """Прямое чтение/запись SQLite стенда для проверки сохранений (§22, п.2)."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, timeout=10)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def user(self, telegram_id: int) -> Optional[sqlite3.Row]:
        row = self._conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row

    def reset_user(self, telegram_id: int) -> None:
        """Удалить пользователя со всеми данными (чистый прогон онбординга)."""
        user_id_row = self._conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if user_id_row is None:
            return
        user_id = user_id_row["id"]
        self._reset_for_user(user_id)
        self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self._conn.commit()

    def reset_doctor(self, telegram_id: int) -> None:
        """Удалить связи с врачами и их заметки (чистый прогон сценария §21)."""
        user_id_row = self._conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if user_id_row is None:
            return
        user_id = user_id_row["id"]
        self._conn.execute("DELETE FROM doctor_notes WHERE user_id = ?", (user_id,))
        self._conn.execute("DELETE FROM doctor_patients WHERE user_id = ?", (user_id,))
        self._conn.commit()

    def _reset_for_user(self, user_id: int) -> None:
        for table in (
            "medication_logs",
            "medications",
            "measurements",
            "reminders",
            "doctor_notes",
            "doctor_patients",
            "web_sessions",
            "web_users",
        ):
            column = "web_user_id" if table == "web_sessions" else "user_id"
            if table == "web_sessions":
                self._conn.execute(
                    "DELETE FROM web_sessions WHERE web_user_id IN"
                    " (SELECT id FROM web_users WHERE user_id = ?)",
                    (user_id,),
                )
                continue
            self._conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))

    def add_measurement(
        self, telegram_id: int, systolic: int, diastolic: int, pulse: Optional[int] = None
    ) -> int:
        """Добавить измерение напрямую (подготовка данных для Web-сценариев)."""
        user = self.user(telegram_id)
        assert user is not None, f"нет пользователя {telegram_id} — сначала онбординг"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        cursor = self._conn.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse)"
            " VALUES (?, ?, ?, ?, ?)",
            (user["id"], timestamp, systolic, diastolic, pulse),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def backdate_measurements(self, telegram_id: int, hours: int = 4) -> int:
        """Отодвинуть измерения в прошлое (правило «3 часов», §6.7).

        Свежая запись гасит слот-напоминание («не дёргаем зря») — для проверки
        доставки измерения не должны попадать в окно.
        """
        user = self.user(telegram_id)
        if user is None:
            return 0
        moment = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - hours * 3600)
        )
        cursor = self._conn.execute(
            "UPDATE measurements SET timestamp = ? WHERE user_id = ?",
            (moment, user["id"]),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def measurement_count(self, telegram_id: int) -> int:
        user = self.user(telegram_id)
        if user is None:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM measurements WHERE user_id = ?", (user["id"],)
        ).fetchone()
        return int(row["n"])

    def last_measurement(self, telegram_id: int) -> Optional[sqlite3.Row]:
        user = self.user(telegram_id)
        if user is None:
            return None
        return self._conn.execute(
            "SELECT * FROM measurements WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()

    def reminder_count(self, telegram_id: int, slot: str) -> int:
        user = self.user(telegram_id)
        if user is None:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM reminders"
            " WHERE user_id = ? AND type = ? AND enabled = 1",
            (user["id"], slot),
        ).fetchone()
        return int(row["n"])


# ─────────────────────────── Онбординг (общий сценарий §22, п.1) ───────────────────────────


def run_onboarding(dialog: "BotDialog") -> None:
    """Полный сценарий онбординга: часовой пояс, цели, слот утреннего замера."""
    from bot import texts

    dialog.send("/start")
    dialog.wait_screen(texts.WELCOME)
    dialog.press("onb:start", contains=texts.ONB_TZ_ASK)
    dialog.press("onb:tz:Europe/Minsk", contains=texts.ONB_TARGETS_ASK)
    dialog.press("onb:target:130-80", contains=texts.ONB_SLOTS_ASK)
    dialog.press("onb:slot:morning")
    dialog.press(
        "onb:slots:done",
        contains=texts.ONB_TIMES_ASK.format(slot=texts.SLOT_LABELS["morning"]),
    )
    dialog.press("onb:time:morning:07:00", contains=texts.ONB_DONE)


# ─────────────────────────── Диалог с ботом (Telethon) ───────────────────────────


class BotDialog:
    """Диалог юзербота с ботом: send / press / wait_screen / wait_new.

    Бот редактирует сообщения (`edit_message_text`), поэтому «экран» — это
    последнее сообщение бота, а ожидание учитывает и правки, и новые сообщения.
    Нажатия дозируются: §7.2 — debounce 1.5 с (`m:`) и throttle 1 с (остальные
    callback'и), быстрые повторы бот гасит молча.
    """

    #: Минимальный интервал между нажатиями (§7.2: debounce 1.5 с + запас на
    #: дисперсию сети: разница задержек сжимает клиентский интервал на стороне бота).
    MIN_CLICK_INTERVAL = 2.0

    #: Момент последнего нажатия — ОБЩИЙ для всех экземпляров: §7.2 действует на
    #: аккаунт, а `dialog` создаётся заново на каждый тест. Без глобального интервала
    #: первый клик теста улетал в окно throttle предыдущего callback'а и бот гасил
    #: его молча (экран не менялся, `wait_screen` падал по таймауту).
    _last_click_at: float = 0.0

    def __init__(self, client: TelegramClient, chat, loop: "TgLoop") -> None:
        self._client = client
        self._chat = chat
        self._loop = loop

    def _pace(self) -> None:
        """Выдержать интервал между нажатиями (§7.2: debounce/throttle)."""
        wait = BotDialog._last_click_at + self.MIN_CLICK_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        BotDialog._last_click_at = time.monotonic()

    # ── отправка ──

    def send(self, text: str, *, timeout: float = 25.0):
        """Отправить текст боту и дождаться нового ответа бота (не эха)."""
        known = self._known_ids()
        self._loop.run(self._client.send_message(self._chat, text))
        return self._poll(
            lambda message: message.id not in known and not message.out,
            timeout=timeout,
        )

    def press(self, data: str, *, contains=None, timeout: float = 25.0):
        """Нажать inline-кнопку с callback_data `data` в последнем сообщении.

        `contains` — маркер(ы) ожидаемого следующего экрана; без них дожидаемся
        смены текста или клавиатуры (экран может менять только кнопки).
        """
        message = self.last()
        assert self._find_button(message, data) is not None, (
            f"нет кнопки {data!r} в сообщении {message.text!r}"
        )
        old_signature = self._signature(message)
        known = self._known_ids()
        self._pace()
        self._loop.run(message.click(data=data))
        if contains:
            return self.wait_screen(*self._markers(contains), timeout=timeout)
        return self._poll(
            lambda candidate: (candidate.id not in known and not candidate.out)
            or (candidate.id == message.id and self._signature(candidate) != old_signature),
            timeout=timeout,
        )

    # ── ожидание ──

    def wait_screen(self, *markers: str, timeout: float = 25.0):
        """Дождаться, что экран бота (последнее сообщение) содержит все маркеры."""
        return self._poll(
            lambda message: not message.out
            and all(marker in (message.text or "") for marker in markers),
            timeout=timeout,
        )

    def wait_new(self, *markers: str, timeout: float = 150.0):
        """Дождаться нового сообщения бота с маркерами (напоминание, ответ на ввод)."""
        known = self._known_ids()
        return self._poll(
            lambda message: message.id not in known
            and not message.out
            and all(marker in (message.text or "") for marker in markers),
            timeout=timeout,
        )

    # ── внутренняя кухня ──

    def last(self):
        messages = self._loop.run(self._client.get_messages(self._chat, limit=1))
        assert messages, "бот ещё не писал в этот диалог"
        return messages[0]

    def _known_ids(self, limit: int = 10) -> set[int]:
        messages = self._loop.run(self._client.get_messages(self._chat, limit=limit))
        return {m.id for m in messages}

    @staticmethod
    def _markers(contains) -> tuple[str, ...]:
        if isinstance(contains, str):
            return (contains,)
        return tuple(contains)

    @staticmethod
    def _signature(message) -> tuple:
        """Текст экрана + (текст, callback_data) каждой кнопки.

        Текст кнопки включён намеренно: мультивыбор слотов (`onb:slot:*`)
        меняет только «✓» в подписи кнопки, callback_data остаётся прежней.
        """
        buttons: list[tuple[str, str]] = []
        for row in message.buttons or []:
            for button in row:
                data = bytes(button.data or b"").decode("utf-8", "replace")
                buttons.append((button.text or "", data))
        return (message.text or "", tuple(buttons))

    @staticmethod
    def _find_button(message, data: str):
        for row in message.buttons or []:
            for button in row:
                if bytes(button.data or b"").decode("utf-8", "replace") == data:
                    return button
        return None

    def _poll(self, predicate: Callable, *, timeout: float):
        """Опрос экрана бота; на таймауте — фактические тексты последних сообщений.

        Внимание: deadline считается строго до первого запроса; лишних RPC до
        цикла быть не должно — авто-sleep Telethon на flood wait «проглатывается»
        молча и съедал бы весь таймаут до единого опроса.
        """
        deadline = time.monotonic() + timeout
        attempts = 0
        seen: list[str] = []
        while time.monotonic() < deadline:
            messages = self._loop.run(self._client.get_messages(self._chat, limit=5))
            attempts += 1
            seen = [f"#{m.id}{'*' if m.out else ''} {(m.text or '')[:40]!r}" for m in messages]
            for message in messages:
                if predicate(message):
                    return message
            time.sleep(0.5)
        raise AssertionError(
            f"бот не дал ожидаемого ответа за {timeout} с"
            f" (опросов: {attempts}; последние сообщения: {'; '.join(seen) or 'нет'})"
        )


# ─────────────────────────── Итоговый отчёт админу (§22) ───────────────────────────


def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(",", " ").split():
        if chunk.lstrip("-").isdigit():
            ids.append(int(chunk))
    return ids


def _send_telegram(token: str, chat_id: int, text: str) -> None:
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=15)


#: Файлы E2E-набора QA Bot (§22): отчёт админу — только после их запуска,
#: чтобы нагрузочный прогон (test_load_burst.py) не слал отчёт QA-стенда.
_E2E_TEST_FILES = {"test_telegram_e2e.py", "test_web_e2e.py"}
_e2e_started = False


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Запомнить, что E2E-тесты реально запускались (хотя бы один).

    Считается только выполненное тело теста (`when == "call"`): пропуск из-за
    ненастроенного стенда (SKIP на этапе setup) — не запуск, иначе дымовой
    `pytest qa/` слал бы админу отчёт QA-стенда.
    """
    global _e2e_started
    if _e2e_started or report.when != "call" or report.skipped:
        return
    if report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1] in _E2E_TEST_FILES:
        _e2e_started = True


def _notify_admin(session: pytest.Session) -> None:
    """Краткий отчёт админу в Telegram (§22) — штатным Bot API бота."""
    if not _e2e_started:
        print("QA: отчёт админу не отправлен (E2E-тесты не запускались).")
        return
    token = _env("BOT_TOKEN")
    admins = _parse_admin_ids(_env("ADMIN_TELEGRAM_IDS"))
    if not token or not admins:
        print("QA: отчёт админу не отправлен (нет BOT_TOKEN / ADMIN_TELEGRAM_IDS).")
        return
    screenshots = "qa/reports/screenshots/"
    if session.exitstatus == 0:
        text = f"✅ Все тесты пройдены. Скриншоты: {screenshots}"
    else:
        failed = getattr(session, "testsfailed", 0) or 0
        collected = getattr(session, "testscollected", 0) or 0
        text = (
            f"❌ QA Bot: провалено тестов — {failed} из {collected}. Скриншоты: {screenshots}"
        )
    for admin_id in admins:
        try:
            _send_telegram(token, admin_id, text)
        except OSError as exc:  # сеть/Telegram не должны ронять прогон
            print(f"QA: не удалось отправить отчёт админу {admin_id}: {exc}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _notify_admin(session)


# ─────────────────────────── Фикстуры ───────────────────────────


@pytest.fixture(scope="session")
def tg_loop() -> Iterator[TgLoop]:
    """Loop Telethon в отдельном потоке (см. TgLoop)."""
    loop = TgLoop()
    try:
        yield loop
    finally:
        loop.stop()


@pytest.fixture(scope="session")
def tg_client(tg_loop: TgLoop) -> Iterator[TelegramClient]:
    """Юзербот Telethon: авторизация по TELEGRAM_TEST_PHONE (§3, §22).

    Сначала окружение, потом импорт: без настроенного стенда или без группы
    зависимостей `qa` (`uv sync --group qa`) тесты пропускаются, а не падают.
    """
    env = _required_env("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_TEST_PHONE")
    telethon = pytest.importorskip("telethon", reason="нет Telethon: uv sync --group qa")
    TelegramClient = telethon.TelegramClient
    from telethon.sessions import StringSession

    api_id = int(env["TELEGRAM_API_ID"])  # type: ignore[index]
    api_hash = env["TELEGRAM_API_HASH"]  # type: ignore[index]
    phone = env["TELEGRAM_TEST_PHONE"]  # type: ignore[index]
    saved_session = _env("TELETHON_SESSION")
    session = StringSession(saved_session) if saved_session else os.path.join(
        REPORTS_DIR, "telethon"
    )
    client = TelegramClient(session, api_id, api_hash, loop=tg_loop.loop)
    tg_loop.run(_login(client, phone))  # первый запуск — интерактивный код
    try:
        yield client
    finally:
        tg_loop.run(_logout(client))


@pytest.fixture(scope="session")
def tg_me(tg_client, tg_loop: TgLoop):
    """Тестовый аккаунт юзербота (его telegram_id — владелец данных в БД)."""
    return tg_loop.run(tg_client.get_me())


@pytest.fixture(scope="session")
def bot_dialog_chat(tg_client, tg_loop: TgLoop):
    """Диалог с тестируемым ботом (QA_BOT_USERNAME, например @testMasterJournal_Bot)."""
    username = _env("QA_BOT_USERNAME")
    if not username:
        pytest.skip("QA-стенд не настроен: нет переменной QA_BOT_USERNAME")
    return tg_loop.run(tg_client.get_entity(username))


@pytest.fixture
def dialog(tg_client, bot_dialog_chat, tg_loop: TgLoop) -> BotDialog:
    return BotDialog(tg_client, bot_dialog_chat, tg_loop)


@pytest.fixture
def db() -> Iterator[QaDb]:
    """БД стенда (общий том с ботом и кабинетом)."""
    path = _env("DB_PATH", "data/bot.sqlite3")
    if not os.path.exists(path):
        pytest.skip(f"БД стенда не найдена: {path}")
    database = QaDb(path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(scope="session")
def cabinet_url() -> str:
    """База кабинета: в Docker-стенде — http://cabinet:8000 (§22)."""
    return _env("CABINET_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """Chromium Playwright; без группы `load` или без браузера — SKIP, а не ошибка."""
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="нет Playwright: uv sync --group load"
    )
    with sync_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(
                f"Chromium не установлен ({exc.message.splitlines()[0]}): "
                "uv run playwright install chromium"
            )
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    new_page = browser.new_page()
    try:
        yield new_page
    finally:
        new_page.close()


@pytest.fixture
def shot(page: Page):
    """Скриншоты ключевых действий в `qa/reports/screenshots/` (CLAUDE.md)."""
    return lambda step_name: save_screenshot(page, step_name)


@pytest.fixture
def onboard(dialog):
    """Запустить полный сценарий онбординга (§22, п.1)."""
    return lambda: run_onboarding(dialog)


@pytest.fixture
def ensure_onboarded(dialog, db, tg_me, onboard):
    """Гарантировать, что тестовый пользователь прошёл онбординг."""

    def _ensure() -> None:
        user = db.user(tg_me.id)
        if user is not None and user["onboarded"]:
            return
        db.reset_user(tg_me.id)
        onboard()

    return _ensure


# ─────────────────── Нагрузочные тесты (SPEC.md, §22) ───────────────────
#
# Профиль: без флага — дымовой (`qa.seed.SMOKE`, секунды), `LOAD_TEST=1` —
# полный (`qa.seed.FULL`). Артефакты полного прогона — `qa/reports/load/`:
# `<тест>.json` (итоги для `test_load_regression.py`) и `qa-load.jsonl`
# (структурированный лог `qa.load`). Дымовой прогон артефактов не пишет —
# не затирает результаты полного.

#: Полный профиль нагрузки (SPEC §3, `LOAD_TEST`).
LOAD_FULL = os.environ.get("LOAD_TEST", "").strip() == "1"

#: Артефакты нагрузочных тестов (вне git: `qa/reports/` в .gitignore).
LOAD_REPORTS_DIR = os.path.join(REPORTS_DIR, "load")

#: Структурированный лог `qa.load` (JSON-строки, SPEC §22 «Логи и отчёты»).
LOAD_LOG_PATH = os.path.join(LOAD_REPORTS_DIR, "qa-load.jsonl")

#: Регрессия читает артефакты, а не производит их.
_REGRESSION_FILE = "test_load_regression.py"


def _test_file(nodeid: str) -> str:
    """Имя файла теста из nodeid (`qa/test_x.py::Test::test` → `test_x.py`)."""
    return nodeid.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]


def pytest_collection_finish(session: pytest.Session) -> None:
    """Начало полного прогона: убрать JSON прошлого прогона и начать лог заново.

    Срабатывает, только если в сессии есть хотя бы один `test_load_*`, кроме
    регрессии: отдельный запуск регрессии (после Locust) должен видеть
    артефакты, а не стирать их. Locust CSV не трогается (SPEC §22).
    """
    if not LOAD_FULL:
        return
    files = {_test_file(item.nodeid) for item in session.items}
    if not any(name.startswith("test_load_") and name != _REGRESSION_FILE for name in files):
        return
    os.makedirs(LOAD_REPORTS_DIR, exist_ok=True)
    for name in os.listdir(LOAD_REPORTS_DIR):
        if name.endswith(".json"):
            os.remove(os.path.join(LOAD_REPORTS_DIR, name))
    with open(LOAD_LOG_PATH, "w", encoding="utf-8"):
        pass


class _JsonLineFormatter(logging.Formatter):
    """Одна запись лога — одна JSON-строка: время, уровень, сообщение и поля."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "msg": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


class LoadLog:
    """Структурированный лог нагрузки `qa.load` (user_id, op, duration_ms, …)."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def op(
        self,
        op: str,
        *,
        user_id: Optional[int] = None,
        duration_ms: Optional[float] = None,
        **fields: object,
    ) -> None:
        """Записать одну операцию: только в файл (в консоль — сводки)."""
        record_fields = {"op": op, "user_id": user_id, "duration_ms": duration_ms, **fields}
        self.logger.debug(op, extra={"fields": record_fields})

    def summary(self, text: str, **fields: object) -> None:
        """Сводка шага: в файл и кратко в консоль."""
        self.logger.info(text, extra={"fields": fields})


@pytest.fixture(scope="session")
def load_full() -> bool:
    """True — полный профиль (`LOAD_TEST=1`), False — дымовой."""
    return LOAD_FULL


@pytest.fixture(scope="session")
def load_scale(load_full: bool):
    """Объёмы сида профиля: `qa.seed.FULL` или `qa.seed.SMOKE`."""
    from qa.seed import FULL, SMOKE

    return FULL if load_full else SMOKE


@pytest.fixture(scope="session")
def load_password() -> str:
    """Пароль нагрузочных аккаунтов: `LOAD_PASSWORD` или случайный на сессию.

    Случайный пароль живёт только в памяти процесса pytest: не пишется ни в
    файлы, ни в логи (SPEC §22, CLAUDE.md — пароли не хардкодятся).
    """
    import secrets

    from qa.seed import load_password_from_env

    return load_password_from_env() or secrets.token_urlsafe(24)


@pytest.fixture(scope="session")
def load_log(load_full: bool) -> Iterator[LoadLog]:
    """Логгер `qa.load`: JSON-строки в `qa-load.jsonl` (полный профиль) + консоль."""
    logger = logging.getLogger("qa.load")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[qa.load] %(message)s"))
    handlers.append(console)
    if load_full:
        os.makedirs(LOAD_REPORTS_DIR, exist_ok=True)
        file_handler = logging.FileHandler(LOAD_LOG_PATH, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_JsonLineFormatter())
        handlers.append(file_handler)
    for handler in handlers:
        logger.addHandler(handler)
    try:
        yield LoadLog(logger)
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture(scope="session")
def load_report(load_full: bool) -> Callable[[str, dict], Optional[str]]:
    """Записать итог нагрузочного теста в `qa/reports/load/<name>.json`.

    Только в полном профиле (дымовой не затирает артефакты полного); возвращает
    путь к файлу или None. Формат: `{"name", "profile", "finished_at", **payload}`.
    """

    def _write(name: str, payload: dict) -> Optional[str]:
        if not load_full:
            return None
        os.makedirs(LOAD_REPORTS_DIR, exist_ok=True)
        path = os.path.join(LOAD_REPORTS_DIR, f"{name}.json")
        document = {
            "name": name,
            "profile": "full",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **payload,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2, default=str)
        return path

    return _write


# ─────────────── Шаблонная БД стенда (SPEC §22, «Изоляция») ───────────────


@pytest.fixture(scope="session")
def load_template(tmp_path_factory: pytest.TempPathFactory, load_scale, load_password: str):
    """Шаблонная БД стенда: один `seed_all` за сессию pytest (масштаб по профилю).

    Лежит во временной папке сессии под именем `test-stand.sqlite3` — защита
    стенда действует и здесь. Тесты получают копии (`load_db`), сам шаблон
    не меняется.
    """
    from qa.seed.seed_all import seed_all

    path = tmp_path_factory.mktemp("load-template") / "test-stand.sqlite3"
    started = time.perf_counter()
    asyncio.run(seed_all(path, load_scale, load_password, verbose=False))
    print(
        f"\n[qa.load] шаблон стенда ({load_scale.name}): "
        f"{load_scale.measurements} измерений за {time.perf_counter() - started:.1f} с"
    )
    return path


@pytest.fixture
def load_db(tmp_path, load_template) -> str:
    """Копия шаблона для одного теста — через Online Backup API SQLite.

    Копия согласована (backup, а не копирование файла), режим WAL
    сохраняется; путь — `<tmp_path>/test-stand.sqlite3`.
    """
    from qa.seed import ensure_stand_db

    target = ensure_stand_db(tmp_path / "test-stand.sqlite3")
    source = sqlite3.connect(load_template)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return str(target)


# ─────────────── Мок Telegram и виртуальные часы (SPEC §22, уровни 3 и 8) ───────────────


class VirtualClock:
    """Монотонные часы для debounce/throttle: время идёт только по `tick()`.

    Подставляется в `_clock` middleware собранного приложения: прод-код не
    меняется, а тест сам решает, «прошло» ли окно (без `sleep`).
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = 2.0) -> None:
        """Сдвинуть время вперёд (по умолчанию — больше окон debounce и throttle)."""
        self.now += seconds


def make_fake_telegram():
    """Сетевая сессия aiogram без сети: запросы пишутся в `calls`, ответы — фейковые.

    Методы, возвращающие `bool`, получают `True`; остальные — `Message` в том
    же чате (с текстом запроса), чтобы хендлеры могли редактировать «свои»
    сообщения. Исключение для запроса настраивается через `fail_for(chat_id, exc)`.
    """
    import itertools
    from datetime import datetime, timezone

    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message

    class FakeTelegram(BaseSession):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list = []
            self._ids = itertools.count(10_000)
            self._failures: dict[int, BaseException] = {}

        def fail_for(self, chat_id: int, exc: BaseException) -> None:
            """Запросы в этот чат будут падать с `exc` (например, бот заблокирован)."""
            self._failures[chat_id] = exc

        def for_chat(self, chat_id: int) -> list:
            """Запросы бота в чат `chat_id` по порядку."""
            return [call for call in self.calls if getattr(call, "chat_id", None) == chat_id]

        async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
            self.calls.append(method)
            chat_id = getattr(method, "chat_id", None)
            if chat_id in self._failures:
                raise self._failures[chat_id]
            if method.__returning__ is bool:
                return True
            message_id = getattr(method, "message_id", None) or next(self._ids)
            return Message(
                message_id=message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(chat_id or 0), type="private"),
                text=getattr(method, "text", None),
            )

        async def stream_content(self, *args, **kwargs):  # type: ignore[override]
            raise NotImplementedError("скачивание файлов в нагрузке не используется")
            yield b""  # pragma: no cover — делает функцию генератором

        async def close(self) -> None:  # type: ignore[override]
            return None

    return FakeTelegram()


@pytest.fixture
def virtual_clock() -> VirtualClock:
    """Виртуальные часы middleware (debounce/throttle) для одного теста."""
    return VirtualClock()


@pytest_asyncio.fixture
async def bot_app(load_db: str, tmp_path, virtual_clock: VirtualClock):
    """Приложение бота (`build_app`) на копии стенда: мок Telegram, часы, планировщик.

    Сеть не используется: сессия `Bot` — `make_fake_telegram()`. Планировщик —
    прод-конфигурация (`build_scheduler`: `SQLAlchemyJobStore` в отдельном
    файле рядом с БД, SPEC §6.7) и **запущен**: нагрузка проверяет и запись
    job'ов повторного замера под конкуренцией (U1). Debounce и throttle
    работают на `virtual_clock`. Возвращает `App`; `app.bot.session` — мок с
    `calls`.
    """
    from bot.config import Settings
    from bot.main import build_app, build_scheduler
    from bot.middlewares import DebounceMiddleware, ThrottleMiddleware

    settings = Settings(
        bot_token=f"123456:{'A' * 35}",  # формат токена, а не настоящий токен
        db_path=load_db,
        backup_dir=str(tmp_path / "backups"),
        backup_encryption_key="load-test-key",
    )
    scheduler = build_scheduler(load_db)
    app = build_app(settings, scheduler=scheduler)
    app.bot.session = make_fake_telegram()
    replaced = 0
    for middleware in app.dp.callback_query.outer_middleware:
        if isinstance(middleware, (DebounceMiddleware, ThrottleMiddleware)):
            middleware._clock = virtual_clock  # часы экземпляра; прод-код не меняется
            middleware._last_cleanup = virtual_clock()
            replaced += 1
    assert replaced == 2, "в приложении нет debounce/throttle — проверьте build_app"
    scheduler.start()
    try:
        yield app
    finally:
        scheduler.shutdown(wait=False)
        await app.engine.dispose()


# ─────────────── Исполнитель конкурентных операций (burst и гонки) ───────────────


class LockCounter(logging.Handler):
    """Считает повторы `run_with_retry` при «database is locked» (логгер bot.db)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.retries = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "заблокирован" in record.getMessage():
            self.retries += 1


@pytest.fixture
def lock_counter() -> Iterator[LockCounter]:
    handler = LockCounter()
    logger = logging.getLogger("bot.db")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def percentile(values: list[float], share: float) -> float:
    """Перцентиль по ближайшему рангу (p95 → share=0.95); пустой список → 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(share * len(ordered) + 0.5) - 1))
    return ordered[rank]


async def run_burst(
    name: str,
    operations: list[tuple[int, Callable[[], Awaitable[object]]]],
    load_log,
    lock_counter: LockCounter,
) -> tuple[list[object], dict]:
    """Запустить операции одним `gather`, замерить и залогировать каждую.

    `operations` — пары (user_id, фабрика корутины). Исключения не теряются:
    `return_exceptions=True`, упавшие операции считаются в `failures`.
    """
    durations: list[float] = []

    from bot.db import run_with_retry

    async def timed(user_id: int, factory: Callable[[], Awaitable[object]]) -> object:
        started = time.perf_counter()
        try:
            return await run_with_retry(factory)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            durations.append(elapsed_ms)
            load_log.op(name, user_id=user_id, duration_ms=round(elapsed_ms, 2))

    started = time.perf_counter()
    results = await asyncio.gather(
        *(timed(user_id, factory) for user_id, factory in operations), return_exceptions=True
    )
    elapsed = time.perf_counter() - started
    failures = [repr(result) for result in results if isinstance(result, BaseException)]
    stats = {
        "operations": len(operations),
        "failures": len(failures),
        "failure_samples": failures[:5],
        "locked_retries": lock_counter.retries,
        "elapsed_s": round(elapsed, 3),
        "p50_ms": round(percentile(durations, 0.50), 2),
        "p95_ms": round(percentile(durations, 0.95), 2),
    }
    load_log.summary(
        f"{name}: {len(operations)} операций за {elapsed:.2f} с, "
        f"p95 {stats['p95_ms']} мс, ошибок {len(failures)}, повторов {lock_counter.retries}",
        scenario=name,
        **stats,
    )
    return results, stats
