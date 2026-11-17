"""Режим просмотра робота-приёмщика на устройствах (0.7.0; §22, CLAUDE.md «Скриншоты»).

Тот же обход меню, что у `test_menu_acceptance_e2e.py`, но в **настоящем
Telegram Web** (web.telegram.org/k) в трёх вьюпортах:

- смартфон — 390 × 844 (узкий экран: обрезка подписей, «Визиты и анализ…»);
- планшет — 768 × 1024;
- ПК — 1280 × 800.

На каждом устройстве экраны главного меню, обоих подменю и калькулятора:
кадр в `qa/reports/screenshots/menu-<устройство>-<экран>.png` и контроль, что
**все подписи кнопок помещаются** (без «…» и визуального клиппинга). Итоговый
обзор — `qa/reports/screenshots/menu-devices-review.html`.

Сессия Telegram Web — `qa/reports/screens/tg_state.json` (разовый вход:
`uv run --group load python qa/screenshots/shot_landing.py tg-login`, QR с
телефона тестового аккаунта). Нет сессии — тесты SKIP.

Запуск (QA-стенд поднят, бот один — см. CLAUDE.md про общий токен):

    docker compose -f qa/docker-compose.qa.yml run --rm qa \
        pytest qa/test_menu_devices_e2e.py -q
"""
from __future__ import annotations

import html
import os
import re

import pytest

from bot import texts

#: Вьюпорты режима просмотра: смартфон / планшет / ПК.
DEVICES = [
    ("phone", 390, 844),
    ("tablet", 768, 1024),
    ("desktop", 1280, 800),
]

TG_WEB = "https://web.telegram.org/k/"
TG_STATE = os.path.join("qa", "reports", "screens", "tg_state.json")
SHOTS_DIR = os.path.join("qa", "reports", "screenshots")
REVIEW_HTML = os.path.join(SHOTS_DIR, "menu-devices-review.html")

_TAG_RE = re.compile(r"</?(?:b|i|code|pre|s|u|a)(?:\s+[^>]*)?>|\*\*|__|~~|`")


def plain(text: str) -> str:
    """Текст без разметки (HTML §11 и markdown Telethon/Web)."""
    return _TAG_RE.sub("", text)


def norm(text: str) -> str:
    """Норма подписи для сверки: без U+FE0F и схлопнутых пробелов (как в JS)."""
    return re.sub(r"\s+", " ", text.replace("️", "")).strip()


#: Текст последнего сообщения с кнопками: робот-приёмка живёт на MU6 —
#: экраны редактируются **на месте**, и ответом может быть не последний,
#: а отредактированный пузырь выше в истории.
_JS_LAST_MARKUP_TEXT = """
() => {
  const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
  const textOf = el => { const c = el.cloneNode(true);
    c.querySelectorAll('img').forEach(i => i.replaceWith(i.alt || ''));
    return strip(c.textContent); };
  const bubbles = [...document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)')]
      .filter(b => b.querySelector('.reply-markup-button'));
  return bubbles.length ? textOf(bubbles[bubbles.length - 1]) : '';
}
"""

#: Кнопки последнего сообщения с кнопками: подпись и признак визуального
#: обрезания («…» или текст не влезает в свой бокс — ellipsis-клиппинг).
_JS_BUTTONS_STATE = """
() => {
  const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
  const textOf = el => { const c = el.cloneNode(true);
    c.querySelectorAll('img').forEach(i => i.replaceWith(i.alt || ''));
    return strip(c.textContent); };
  const bubbles = [...document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)')]
      .filter(b => b.querySelector('.reply-markup-button'));
  if (!bubbles.length) return [];
  const clipped = (el) => el.scrollWidth > el.clientWidth + 1
      || el.scrollHeight > el.clientHeight + 1;
  return [...bubbles[bubbles.length - 1].querySelectorAll('.reply-markup-button')]
      .map(b => {
        const holder = b.querySelector('.i18n') || b;
        return {
          label: textOf(b),
          clipped: clipped(holder) || clipped(b)
              || (b.innerText || '').trim().endsWith('\\u2026'),
        };
      });
}
"""

#: Номер кнопки последнего сообщения с кнопками по точной подписи (-1 — нет).
_JS_BUTTON_INDEX = """
(label) => {
  const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
  const textOf = el => { const c = el.cloneNode(true);
    c.querySelectorAll('img').forEach(i => i.replaceWith(i.alt || ''));
    return strip(c.textContent); };
  const bubbles = [...document.querySelectorAll('#column-center .bubbles-inner .bubble:not(.is-date)')]
      .filter(b => b.querySelector('.reply-markup-button'));
  if (!bubbles.length) return -1;
  const buttons = [...bubbles[bubbles.length - 1].querySelectorAll('.reply-markup-button')];
  return buttons.findIndex(b => textOf(b) === strip(label));
}
"""

#: Кадр экрана «устройство-экран» попадает в обзорную страницу.
_SHOTS: list[tuple[str, str, str]] = []  # (устройство, экран, файл)


class WebDialog:
    """Диалог с ботом в Telegram Web K: отправка, нажатия, ожидание, кадр.

    Селекторы и приёмы — как у `qa/screenshots/shot_landing.py` (тот же
    клиент); ожидания привязаны к последнему сообщению **с кнопками**
    (экраны редактируются на месте, §6.2).
    """

    def __init__(self, page) -> None:
        self.page = page

    def open_bot(self, username: str) -> None:
        self.page.goto(f"{TG_WEB}#@{username.lstrip('@')}")
        for attempt in range(3):
            self.page.wait_for_timeout(3000)
            if self.page.locator(".login, .qr-container").count():
                pytest.skip(
                    "сессия Telegram Web истекла: "
                    "uv run --group load python qa/screenshots/shot_landing.py tg-login"
                )
            for _ in range(15):
                if self.page.evaluate(
                    "() => document.querySelectorAll("
                    "'#column-center .bubbles-inner .bubble:not(.is-date)').length"
                ):
                    return
                self.page.wait_for_timeout(1000)
            # Telegram Web иногда зависает на спиннере истории на десятки секунд
            # (проверено в `shot_landing._ensure_history`) — перезагрузка лечит.
            self.page.reload()
        raise AssertionError("Telegram Web не открыл диалог с ботом")

    def send(self, text: str) -> None:
        box = self.page.locator(
            "#column-center .chat-input .input-message-input[contenteditable='true']"
        ).first
        box.wait_for(state="visible", timeout=15_000)
        box.click()
        box.type(text)
        self.page.keyboard.press("Enter")
        # Echo своего сообщения = отправка дошла до Telegram. Нет его — аккаунт
        # в flood-окне (лимит общий с Telethon): SKIP с ясным текстом, а не
        # таймаут ожидания ответа (найдено на прогоне устройств).
        try:
            self.page.wait_for_function(
                """(t) => {
                     const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
                     return [...document.querySelectorAll(
                         '#column-center .bubbles-inner .bubble:not(.is-date)')]
                         .some(b => strip(b.textContent) === strip(t));
                   }""",
                arg=text, timeout=10_000, polling=500,
            )
        except Exception:
            pytest.skip(
                f"Telegram не принял сообщение {text!r}: flood-лимит аккаунта — "
                "повторите прогон позже"
            )

    def tap(self, label: str) -> None:
        """Нажать кнопку последнего сообщения с кнопками (точная подпись)."""
        self.page.wait_for_timeout(1300)  # антидребезг бота (§7.2)
        index = -1
        for _ in range(40):
            index = self.page.evaluate(_JS_BUTTON_INDEX, label)
            if index >= 0:
                break
            self.page.wait_for_timeout(500)
        if index < 0:
            raise AssertionError(
                f"нет кнопки {label!r}; кнопки: {self.page.evaluate(_JS_BUTTONS_STATE)}"
            )
        self.page.locator(".reply-markup-button").nth(
            self._global_index(index)
        ).click()

    def _global_index(self, local_index: int) -> int:
        """Индекс кнопки в DOM страницы (Playwright-локатор считает все кнопки)."""
        return self.page.evaluate(
            """(i) => {
                 const bubbles = [...document.querySelectorAll(
                     '#column-center .bubbles-inner .bubble:not(.is-date)')]
                     .filter(b => b.querySelector('.reply-markup-button'));
                 const target = bubbles[bubbles.length - 1];
                 const all = [...document.querySelectorAll('.reply-markup-button')];
                 return all.indexOf([...target.querySelectorAll('.reply-markup-button')][i]);
               }""",
            local_index,
        )

    def wait_screen(self, marker: str, timeout: int = 30_000) -> None:
        """Дождаться, что последнее сообщение с кнопками покажет маркер."""
        try:
            self.page.wait_for_function(
                """(t) => {
                     const strip = s => s.replace(/\\uFE0F/g, '').replace(/\\s+/g, ' ').trim();
                     const textOf = el => { const c = el.cloneNode(true);
                       c.querySelectorAll('img').forEach(i => i.replaceWith(i.alt || ''));
                       return strip(c.textContent); };
                     const bubbles = [...document.querySelectorAll(
                         '#column-center .bubbles-inner .bubble:not(.is-date)')]
                         .filter(b => b.querySelector('.reply-markup-button'));
                     return bubbles.length && textOf(bubbles[bubbles.length - 1]).includes(strip(t));
                   }""",
                arg=plain(marker), timeout=timeout, polling=500,
            )
        except Exception:
            # Диагностика на провале: хвост пузырей + кадр (CLAUDE.md, «Скриншоты»).
            tail = self.page.evaluate(
                """() => [...document.querySelectorAll(
                     '#column-center .bubbles-inner .bubble:not(.is-date)')]
                     .slice(-3).map(b => (b.innerText || '').slice(0, 60))"""
            )
            os.makedirs(SHOTS_DIR, exist_ok=True)
            self.page.screenshot(path=os.path.join(SHOTS_DIR, "menu-devices-failure.png"))
            raise AssertionError(
                f"нет экрана с маркером {marker!r}; хвост пузырей: {tail!r} "
                f"(кадр: {os.path.join(SHOTS_DIR, 'menu-devices-failure.png')})"
            ) from None
        self.page.wait_for_timeout(500)  # дорисовка клавиатуры

    def buttons_state(self) -> list[dict]:
        return self.page.evaluate(_JS_BUTTONS_STATE)

    def shot(self, device: str, screen: str) -> str:
        os.makedirs(SHOTS_DIR, exist_ok=True)
        name = f"menu-{device}-{screen}.png"
        path = os.path.join(SHOTS_DIR, name)
        self.page.screenshot(path=path)
        _SHOTS.append((device, screen, name))
        return path


#: Подписи кнопок экранов (дословно из `bot.texts`, канон §11).
SCREEN_LABELS = {
    "main": [
        texts.BTN_MEASURE,
        texts.BTN_ADD_RECORD,
        texts.BTN_MENU_VIEWS,
        texts.BTN_MENU_CONFIG,
        texts.BTN_SEND_REPORT,
        texts.BTN_ABOUT,
    ],
    "views": [
        texts.BTN_DIARY,
        texts.BTN_STATS,
        texts.BTN_APPOINTMENTS,
        texts.BTN_BACK,
    ],
    "config": [
        texts.BTN_CABINET,
        texts.BTN_PROFILE,
        texts.BTN_MY_MEDS,
        texts.BTN_SETTINGS,
        texts.BTN_BACK,
    ],
    "calculator": [
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
        texts.BTN_KEYPAD_CLEAR,
        texts.BTN_KEYPAD_BACKSPACE,
        texts.BTN_DONE_KEYPAD,
        texts.BTN_RESET,
        texts.BTN_CANCEL,
    ],
}


def check_screen(dialog: WebDialog, device: str, screen: str) -> None:
    """Кадр + контроль: все подписи экрана на месте и не обрезаны."""
    state = dialog.buttons_state()
    labels = [norm(button["label"]) for button in state]
    for expected in SCREEN_LABELS[screen]:
        assert norm(expected) in labels, (
            f"{device}/{screen}: нет кнопки {expected!r}, видно {labels!r}"
        )
    broken = [button["label"] for button in state if button["clipped"]]
    assert not broken, (
        f"{device}/{screen}: подпись(и) обрезаны на {device}: {broken!r} "
        f"(полные подписи: {labels!r})"
    )
    dialog.shot(device, screen)


@pytest.fixture(scope="session")
def web_username() -> str:
    username = os.environ.get("QA_BOT_USERNAME", "").strip()
    if not username:
        pytest.skip("QA-стенд не настроен: нет переменной QA_BOT_USERNAME")
    return username


@pytest.fixture(scope="session")
def web_state() -> str:
    if not os.path.exists(TG_STATE):
        pytest.skip(
            "нет сессии Telegram Web (qa/reports/screens/tg_state.json): "
            "uv run --group load python qa/screenshots/shot_landing.py tg-login"
        )
    return TG_STATE


class TestMenuOnDevices:
    """Меню в режимах просмотра: смартфон, планшет, ПК (обрезание + кадры)."""

    @pytest.mark.parametrize(("device", "width", "height"), DEVICES, ids=[d[0] for d in DEVICES])
    def test_menu_walk(self, browser, web_state, web_username, device, width, height) -> None:
        context = browser.new_context(
            storage_state=web_state,
            viewport={"width": width, "height": height},
            locale="ru-RU",
        )
        page = context.new_page()
        dialog = WebDialog(page)
        try:
            dialog.open_bot(web_username)
            dialog.send("/start")
            dialog.wait_screen(texts.MAIN_MENU)
            check_screen(dialog, device, "main")

            dialog.tap(texts.BTN_MENU_VIEWS)
            dialog.wait_screen(texts.VIEWS_MENU)
            check_screen(dialog, device, "views")
            dialog.tap(texts.BTN_BACK)
            dialog.wait_screen(texts.MAIN_MENU)

            dialog.tap(texts.BTN_MENU_CONFIG)
            dialog.wait_screen(texts.CONFIG_MENU)
            check_screen(dialog, device, "config")
            dialog.tap(texts.BTN_BACK)
            dialog.wait_screen(texts.MAIN_MENU)

            dialog.tap(texts.BTN_MEASURE)
            dialog.wait_screen(texts.KEYPAD_HINT)
            check_screen(dialog, device, "calculator")
        finally:
            context.close()

    def test_zz_review_page(self, web_state) -> None:
        """Обзорная страница кадров по устройствам (для просмотра и отчёта)."""
        if not _SHOTS:
            pytest.skip("кадры не снимались — тесты устройств пропущены")
        os.makedirs(SHOTS_DIR, exist_ok=True)
        rows = "".join(
            f"<figure><img src='{html.escape(name)}' alt='{html.escape(f'{device}/{screen}')}'"
            f" loading='lazy'/><figcaption>{html.escape(device)} — {html.escape(screen)}"
            "</figcaption></figure>"
            for device, screen, name in _SHOTS
        )
        document = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Меню на устройствах — приёмка 0.7.0</title>"
            "<style>body{font-family:sans-serif;margin:24px}"
            "figure{margin:0 0 24px}img{max-width:100%;border:1px solid #ddd}"
            "figcaption{color:#555}</style>"
            f"<h1>Меню на устройствах ({len(_SHOTS)} кадров)</h1>{rows}"
        )
        with open(REVIEW_HTML, "w", encoding="utf-8") as fh:
            fh.write(document)
        assert _SHOTS, "кадры не снимались — обзорная страница пуста"
