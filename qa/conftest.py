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
from typing import Callable, Iterator, Optional

import pytest

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
    """Запомнить, что E2E-тесты реально запускались (хотя бы один)."""
    global _e2e_started
    if _e2e_started:
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
    """Юзербот Telethon: авторизация по TELEGRAM_TEST_PHONE (§3, §22)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    env = _required_env("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_TEST_PHONE")
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
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        yield playwright.chromium.launch(headless=True)


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
