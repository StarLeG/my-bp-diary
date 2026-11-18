"""Робот-автотестировщик приёмки меню (0.7.0; ответы — `doc/MENU-UX-QUESTIONS.md`).

Автоприёмка: юзербот Telethon (§22, CLAUDE.md — не aiogram) проходит по
**каждому элементу меню** живого бота и проверяет, что:

- главное меню — ровно 5 рядов: «✍️ Записать давление» (акцент, MU1/MU3),
  «➕ Добавить запись», подменю «📊 Дневник и статистика» и «⚙️ Настройки и
  профиль», пара «📄 Отчет» / «ℹ️ О боте» (MU5);
- каждый пункт открывает свой экран, а «Назад» возвращает **на уровень выше**
  (канон UI1: подменю → пункт → подменю; «В меню» → главное меню);
- в «Настройках» нет дублей «Мои данные» / «Мои лекарства» (MU2) и живут все
  девять пунктов, включая «Удалить аккаунт» (только открытие экрана!);
- калькулятор — раскладка MU8 (1-2-3 / 4-5-6 / 7-8-9, «C» «0» «⌫», широкая
  «Готово ✅», пара «Изменить» / «Отмена») с подсказкой MU9;
- `/start` не плодит дублей: повторный вызов редактирует закреплённое меню (MU6);
- команды `/record`, `/stats`, `/help`, `/visits` (MU13) открывают свои экраны.

Опыт первых прогонов (заложено в хелперах): экраны редактируются **на месте**
и уходят вниз под командами — ожидание привязано к конкретному сообщению
меню (id закрепа через Bot API), а не к «последним сообщениям истории»;
маркеры берутся из **текстов сообщений**, а не из подписей кнопок.

Запуск (QA-стенд поднят; бот в контейнере один — см. CLAUDE.md про общий
токен; сессия юзербота — `qa/reports/telethon.session`):

    docker compose -f qa/docker-compose.qa.yml run --rm qa \
        pytest qa/test_menu_acceptance_e2e.py -q

Итог прогона — `qa/reports/report.html`; отчёт админу уходит штатно (§22).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pytest

from bot import texts

# ─────────────────────────── Вспомогательные ───────────────────────────


def rows_of(message) -> list[list[tuple[str, str]]]:
    """Ряды кнопок экрана: [(текст, callback_data)]."""
    rows: list[list[tuple[str, str]]] = []
    for row in message.buttons or []:
        rows.append(
            [
                (button.text or "", bytes(button.data or b"").decode("utf-8", "replace"))
                for button in row
            ]
        )
    return rows


def data_texts(message) -> dict[str, str]:
    """Кнопки экрана: callback_data → текст."""
    return {data: text for row in rows_of(message) for text, data in row}


def assert_any_shown(message, markers: list[str]) -> None:
    """Экран содержит один из маркеров (экраны с разными исходами)."""
    shown = message.text or ""
    assert any(marker in shown for marker in markers), (
        f"ни один из маркеров {markers!r} не найден в {shown!r}"
    )


def pinned_message_id(chat_id: int):
    """Id закреплённого сообщения чата: Bot API `getChat.pinned_message`.

    У Telethon нет getter'а закрепа (лишь pin/unpin) — читаем через Bot API
    штатным токеном стенда (`BOT_TOKEN`); нет токена — None (без закрепа).
    """
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}", timeout=15
        ) as response:
            payload = json.load(response)
    except OSError:
        return None
    pinned = (payload.get("result") or {}).get("pinned_message") or {}
    return pinned.get("message_id")


def _wait_menu_text(dialog, menu_message, marker: str, timeout: float = 25.0):
    """Дождаться, что **сообщение меню** (редактируется на месте) покажет маркер.

    Ждём конкретное сообщение, а не «что-то в истории»: после MU6 экраны
    редактируются на месте и уходят вниз под командами — поиск по последним
    сообщениям ненадёжен (найдено роботом-приёмкой).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = dialog.refresh(menu_message)
        if marker in (fresh.text or ""):
            return fresh
        time.sleep(0.5)
    raise AssertionError(
        f"сообщение #{menu_message.id} не показало {marker!r}: "
        f"{dialog.refresh(menu_message).text!r}"
    )


#: Сообщение меню живёт на протяжении прогона (редактируется на месте, id стабилен);
#: `db`/`tg_me` — доступ к БД стенда для чтения канонического id (фикстура `menu`).
_MENU_CACHE: dict = {"message": None, "db": None, "tg_me": None}

#: Аккаунт в FloodWait (конец окна, monotonic): не дёргаем сеть до конца —
#: запросы во время лимита эскалируют его (30219 с после серии прогонов!).
_FLOOD_PAUSE: dict = {"until": 0.0}


def is_admin(user) -> bool:
    """Тестовый аккаунт — администратор стенда (`ADMIN_TELEGRAM_IDS`, §18)."""
    ids = os.environ.get("ADMIN_TELEGRAM_IDS", "").replace(",", " ").split()
    return str(user.id) in ids


def _wait_menu_via_db(dialog, db, tg_me, timeout: float = 25.0):
    """Дождаться канонического меню по id из БД стенда (`users.last_menu_message_id`).

    `/start` редактирует именно это сообщение (MU6) — часто тихо («message is
    not modified») и вне окна истории. Пока бот не дописал новый id (fallback
    при удалённом сообщении) — id перечитывается на каждом опросе.
    """
    deadline = time.monotonic() + timeout
    seen: list = []
    while time.monotonic() < deadline:
        tracked = db.menu_message_id(tg_me.id)
        seen.append(tracked)
        if tracked:
            menu = dialog._loop.run(dialog._client.get_messages(dialog._chat, ids=tracked))
            if menu is not None and texts.MAIN_MENU in (menu.text or ""):
                return menu
        time.sleep(0.5)
    raise AssertionError(
        f"каноническое меню не появилось за {timeout} с (last_menu_message_id: {seen})"
    )


def open_menu(dialog):
    """Открыть главное меню: `/start` редактирует закреплённое сообщение (MU6).

    Канонический id сообщения меню берётся из БД стенда — детерминированно и
    независимо от окна истории; если меню уже на экране — без `/start`
    (экономия трафика: аккаунт ловит FloodWait при серии прогонов).
    """
    db, tg_me = _MENU_CACHE["db"], _MENU_CACHE["tg_me"]
    tracked = db.menu_message_id(tg_me.id) if db is not None else None
    if tracked:
        try:
            fresh = dialog._loop.run(dialog._client.get_messages(dialog._chat, ids=tracked))
            if fresh is not None and texts.MAIN_MENU in (fresh.text or ""):
                return fresh
        except Exception:  # noqa: BLE001 — любой сбой чтения → обычный путь
            pass
    dialog.send_input("/start")
    menu = _wait_menu_via_db(dialog, db, tg_me)
    _MENU_CACHE["message"] = menu
    return menu


@pytest.fixture(scope="session")
def clean_chat(tg_client, bot_dialog_chat, tg_loop):
    """Чистка диалога перед прогоном.

    Сообщения прошлых прогонов ловятся ожиданиями как «ожидаемый экран»
    (история рядом) и уводят робота в фазу — найдено при первом прогоне.
    Удаление — свежие 30 сообщений и не больше: mass-delete сам по себе
    ловит FloodWait (найдено на 3-м прогоне).
    """
    messages = tg_loop.run(tg_client.get_messages(bot_dialog_chat, limit=30))
    if messages:
        tg_loop.run(tg_client.delete_messages(bot_dialog_chat, [m.id for m in messages]))
    return None


@pytest.fixture
def menu(dialog, ensure_onboarded, db, tg_me, clean_chat):
    """Пользователь готов (онбординг + действующие согласия) — главное меню открыто.

    Возвращает **сообщение меню**: все нажатия идут в него (сообщение
    редактируется на месте, §6.2) — так робот не зависит от истории чата.
    FloodWait аккаунта — SKIP без запросов (запросы в лимит его эскалируют).
    """
    from telethon.errors import FloodWaitError

    if time.monotonic() < _FLOOD_PAUSE["until"]:
        rest = int(_FLOOD_PAUSE["until"] - time.monotonic())
        pytest.skip(f"аккаунт в FloodWait ещё {rest} с — прогон приостановлен")
    _MENU_CACHE["db"] = db
    _MENU_CACHE["tg_me"] = tg_me
    try:
        ensure_onboarded()
        db.ensure_patient_consents(tg_me.id)
        return open_menu(dialog)
    except FloodWaitError as error:
        _FLOOD_PAUSE["until"] = time.monotonic() + error.seconds
        pytest.skip(
            f"FloodWait {error.seconds} с — запросы в лимит эскалируют его, "
            "прогон продолжится после паузы"
        )


# ─────────────────────────── Доступность бота ───────────────────────────


class TestBotAlive:
    """Первая проверка прогона: бот жив и отвечает («завис сервер» — вердикт)."""

    def test_00_bot_alive(self, dialog) -> None:
        """Бот отвечает в разумное время; тишина = недоступен (вердикт админу)."""
        try:
            reply = dialog.send("пинг", timeout=20.0)
        except AssertionError as error:
            raise AssertionError(
                "❌ Бот недоступен: не отвечает на сообщение (завис сервер или "
                f"бот остановлен). Подробности: {error}"
            ) from None
        assert reply.text, "бот ответил пустым сообщением"
        # Любой внятный ответ («Выберите пункт меню», подсказка FSM) = бот жив.


# ─────────────────────────── Главное меню ───────────────────────────


class TestMainMenu:
    """Структура главного меню (MU1–MU5) и отсутствие дублей (MU6)."""

    def test_main_menu_five_rows(self, menu, tg_me) -> None:
        """5 базовых рядов; ниже — только служебные (врач/админ, §21/§18.1)."""
        rows = rows_of(menu)
        assert rows[:5] == [
            [(texts.BTN_MEASURE, "menu:measure")],
            [(texts.BTN_ADD_RECORD, "add:menu")],
            [(texts.BTN_MENU_VIEWS, "menu:views")],
            [(texts.BTN_MENU_CONFIG, "menu:config")],
            [(texts.BTN_SEND_REPORT, "menu:report"), (texts.BTN_ABOUT, "menu:about")],
        ]
        extras = rows[5:]
        assert all(len(row) == 1 for row in extras), f"служебные ряды не по одной кнопке: {extras}"
        buttons = {data for row in extras for _, data in row}
        assert buttons <= {"doc:menu", "adm:menu"}, f"лишние кнопки меню: {extras}"
        if is_admin(tg_me):
            assert "adm:menu" in buttons, "админ не видит «🛡 Админка»"

    def test_start_does_not_duplicate(self, menu, dialog, db, tg_me) -> None:
        """`/start` показывает меню новым сообщением и удаляет прежнее (MU6,
        уточнён 2026-11-17: тихая правка выглядела как «бот не отвечает»):
        в чате остаётся ровно одно каноническое меню."""
        old_tracked = db.menu_message_id(tg_me.id)
        dialog.send_input("/start")
        new_menu = _wait_menu_via_db(dialog, db, tg_me)
        assert new_menu.id != old_tracked, "/start не создал новое сообщение меню"
        assert texts.MAIN_MENU in (new_menu.text or ""), "новое меню не показало «Главное меню»"
        old = dialog._loop.run(dialog._client.get_messages(dialog._chat, ids=old_tracked))
        assert old is None, f"прежнее меню #{old_tracked} осталось в чате — дубль"
        # Закреп — best effort (SPEC §6.2, MU6): pinChatMessage покрыт
        # юнит-тестами; live-сверку через getChat убрано — Bot API для личных
        # чатов отдаёт ненадёжный pinned_message (находка робота).


# ─────────────────── Служебные кнопки меню (админ / врач) ───────────────────


class TestConditionalEntries:
    """Условные кнопки меню: «🛡 Админка» (§18.1) и «🩺 Кабинет врача» (§21)."""

    def test_admin_quick_entry(self, menu, dialog, tg_me) -> None:
        """«🛡 Админка» — быстрый вход в админку прямо из меню (0.7.0)."""
        if not is_admin(tg_me):
            pytest.skip("тестовый аккаунт не в ADMIN_TELEGRAM_IDS")
        dialog.press("adm:menu", contains=texts.ADMIN_MENU, message=menu)
        screen = dialog.refresh(menu)
        assert "adm:stats" in data_texts(screen), "не открылось меню админки"
        dialog.press("menu:back", contains=texts.MAIN_MENU, message=menu)

    def test_doctor_panel_entry(self, menu, dialog) -> None:
        """«🩺 Кабинет врача» — меню врача (§21, N5); открывается и возвращается."""
        if "doc:menu" not in data_texts(menu):
            pytest.skip("тестовый аккаунт — не врач")
        dialog.press("doc:menu", contains=texts.screen_header("doctor_panel"), message=menu)
        open_menu(dialog)  # нормализация: у меню врача нет «В меню»


# ─────────────────────── Пункты «📊 Дневник и статистика» ───────────────────────


class TestViewsSubmenu:
    """Подменю «📊 Дневник и статистика» (MU2): три пункта и «Назад»."""

    def open_views(self, dialog, menu):
        return dialog.press("menu:views", contains=texts.VIEWS_MENU, message=menu)

    def test_views_structure(self, menu, dialog) -> None:
        screen = self.open_views(dialog, menu)
        assert rows_of(screen) == [
            [(texts.BTN_DIARY, "menu:diary")],
            [(texts.BTN_STATS, "menu:stats")],
            [(texts.BTN_APPOINTMENTS, "ap:list")],
            [(texts.BTN_BACK, "menu:back")],
        ]

    def test_diary_element(self, menu, dialog) -> None:
        """«📖 Дневник за сегодня» → экран; «Назад» → подменю (уровень выше, UI1)."""
        self.open_views(dialog, menu)
        dialog.press("menu:diary", contains=texts.screen_header("diary"), message=menu)
        dialog.press("menu:views", contains=texts.VIEWS_MENU, message=menu)

    def test_stats_element(self, menu, dialog) -> None:
        """«📊 Статистика» → выбор периода (7 / 30 / свой); «Назад» → подменю."""
        self.open_views(dialog, menu)
        screen = dialog.press("menu:stats", contains=texts.BTN_STATS, message=menu)
        assert {"stats:7", "stats:30", "stats:cal"} <= set(data_texts(screen))
        dialog.press("menu:views", contains=texts.VIEWS_MENU, message=menu)

    def test_visits_element(self, menu, dialog) -> None:
        """«📅 Визиты» → список визитов; «В меню» → главное меню (канон UI1)."""
        self.open_views(dialog, menu)
        dialog.press("ap:list", contains=texts.AP_HEADER, message=menu)
        dialog.press("menu:back", contains=texts.MAIN_MENU, message=menu)

    def test_views_back_to_main(self, menu, dialog) -> None:
        self.open_views(dialog, menu)
        dialog.press("menu:back", contains=texts.MAIN_MENU, message=menu)


# ─────────────────────── Пункты «⚙️ Настройки и профиль» ───────────────────────


class TestConfigSubmenu:
    """Подменю «⚙️ Настройки и профиль» (MU2): четыре пункта и «Назад»."""

    def open_config(self, dialog, menu):
        return dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)

    def open_settings(self, dialog, menu):
        """Меню «Настройки»: текст экрана — только «⚙️ Настройки» (коллизит с
        шапкой подменю), поэтому ждём смены экрана и проверяем кнопки."""
        screen = dialog.press("menu:settings", message=menu)
        buttons = data_texts(screen)
        assert "set:targets" in buttons, "не открылось меню «Настройки»"
        return screen

    def test_config_structure(self, menu, dialog) -> None:
        screen = self.open_config(dialog, menu)
        assert rows_of(screen) == [
            [(texts.BTN_CABINET, "menu:cabinet")],
            [(texts.BTN_PROFILE, "set:profile")],
            [(texts.BTN_MY_MEDS, "set:meds")],
            [(texts.BTN_SETTINGS, "menu:settings")],
            [(texts.BTN_BACK, "menu:back")],
        ]

    def test_cabinet_element(self, menu, dialog) -> None:
        """«📈 Личный кабинет»: запрос e-mail (нет аккаунта) или ссылка (есть)."""
        self.open_config(dialog, menu)
        screen = dialog.press("menu:cabinet", message=menu)
        assert_any_shown(
            screen,
            [texts.CABINET_EMAIL_PROMPT, "Ссылка для входа в личный кабинет"],
        )
        open_menu(dialog)  # нормализация: экран мог уйти в FSM

    def test_profile_element(self, menu, dialog) -> None:
        """«👤 Мои данные» → экран профиля; «Назад» → подменю (UI1, 0.7.0)."""
        self.open_config(dialog, menu)
        dialog.press("set:profile", contains=texts.screen_header("profile"), message=menu)
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)

    def test_meds_element(self, menu, dialog) -> None:
        """«💊 Мои лекарства» → список; «Назад» → подменю (UI1, 0.7.0)."""
        self.open_config(dialog, menu)
        dialog.press("set:meds", contains=texts.screen_header("meds"), message=menu)
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)

    def test_settings_element_without_duplicates(self, menu, dialog) -> None:
        """«⚙️ Настройки» → меню настроек БЕЗ «Моих данных»/«Моих лекарств» (MU2)."""
        self.open_config(dialog, menu)
        screen = self.open_settings(dialog, menu)
        buttons = data_texts(screen)
        assert "set:profile" not in buttons, "«Мои данные» дублируются в «Настройках»"
        assert "set:meds" not in buttons, "«Мои лекарства» дублируются в «Настройках»"
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)

    def test_config_back_to_main(self, menu, dialog) -> None:
        self.open_config(dialog, menu)
        dialog.press("menu:back", contains=texts.MAIN_MENU, message=menu)


# ─────────────────────── Каждый пункт «Настроек» ───────────────────────


class TestSettingsElements:
    """Каждый элемент «Настроек» открывает свой экран и возвращается («Назад» →
    `set:back` — уровень выше: само меню «Настроек», UI1)."""

    #: (callback, маркер экрана, кнопка возврата). CSV/PDF — общий экран
    #: экспорта (доступен и из статистики), его «Назад» ведёт в главное меню.
    ITEM_MARKERS = [
        ("set:targets", texts.TARGETS_ASK, "set:back"),
        ("set:reminders", texts.REMINDERS_MENU, "set:back"),
        ("set:doctor", texts.DOCTOR_MENU, "set:back"),
        ("set:tz", texts.TZ_ASK, "set:back"),
        ("set:archive", texts.ARCHIVE_MENU, "set:back"),
        ("set:csv", texts.CSV_ASK, "menu:back"),
        ("set:pdf", texts.CSV_ASK, "menu:back"),
        ("set:consent", "Согласие дано", "set:back"),
        ("set:delete", texts.DELETE_ACCOUNT_WARNING, "set:back"),
    ]

    @pytest.mark.parametrize(("callback", "marker", "back"), ITEM_MARKERS)
    def test_settings_item(self, menu, dialog, callback, marker, back) -> None:
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        dialog.press("menu:settings", message=menu)
        dialog.press(callback, contains=marker, message=menu)
        # «Удалить аккаунт»: только открытие экрана, «Отмена» → назад (§19).
        screen = dialog.press(back, message=menu)
        if back == "set:back":
            assert "set:targets" in data_texts(screen), "«Назад» не вернул в «Настройки»"
        else:
            assert "menu:views" in data_texts(screen), "«Назад» не вернул в главное меню"

    def test_ui_mode_toggle(self, menu, dialog) -> None:
        """«👁 Крупный режим» переключается и перерисовывает экран (UI7)."""
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        dialog.press("menu:settings", message=menu)
        screen = dialog.press("set:ui:1", message=menu)
        buttons = data_texts(screen)
        assert (
            buttons.get("set:ui:0") == texts.BTN_UI_MODE.format(state=texts.UI_MODE_ON)
        ), "переключатель не показал «вкл»"
        screen = dialog.press("set:ui:0", message=menu)
        buttons = data_texts(screen)
        assert (
            buttons.get("set:ui:1") == texts.BTN_UI_MODE.format(state=texts.UI_MODE_OFF)
        ), "переключатель не показал «выкл»"


# ─────────────────────── Калькулятор и «Добавить запись» ───────────────────────


class TestMeasureAndAdd:
    """«✍️ Записать давление» (MU8/MU9) и «➕ Добавить запись» (§6.14)."""

    def test_calculator_layout_and_hint(self, menu, dialog) -> None:
        dialog.press("menu:measure", contains=texts.screen_header("calculator"), message=menu)
        screen = _wait_menu_text(dialog, menu, texts.KEYPAD_HINT)  # подсказка MU9
        rows = rows_of(screen)
        assert rows[1:5] == [
            [("1", "m:key:1"), ("2", "m:key:2"), ("3", "m:key:3")],
            [("4", "m:key:4"), ("5", "m:key:5"), ("6", "m:key:6")],
            [("7", "m:key:7"), ("8", "m:key:8"), ("9", "m:key:9")],
            [(texts.BTN_KEYPAD_CLEAR, "m:key:clear"), ("0", "m:key:0"),
             (texts.BTN_KEYPAD_BACKSPACE, "m:key:bksp")],
        ]
        assert rows[5] == [(texts.BTN_DONE_KEYPAD, "m:key:ok")]  # широкая «Готово ✅»
        assert rows[6] == [(texts.BTN_RESET, "m:reset"), (texts.BTN_CANCEL, "m:cancel")]

    def test_calculator_digits_and_cancel(self, menu, dialog) -> None:
        dialog.press("menu:measure", contains=texts.screen_header("calculator"), message=menu)
        dialog.press("m:key:1", message=menu)
        _wait_menu_text(dialog, menu, "Введено: 1")  # цифра попадает в активное поле
        dialog.press("m:cancel", contains=texts.MAIN_MENU, message=menu)

    def test_calculator_every_button(self, menu, dialog) -> None:
        """Каждая кнопка калькулятора: цифры 0–9, «C», «⌫», поля, «Изменить», «Отмена».

        Буфер поля — до 3 цифр (§6.3): перед очередной цифрой место освобождается
        «⌫» — иначе нажатие ничего не меняет и `press` ждал бы смены экрана.
        """
        dialog.press("menu:measure", contains=texts.screen_header("calculator"), message=menu)
        dialog.press("m:key:1", message=menu)
        dialog.press("m:key:2", message=menu)
        dialog.press("m:key:3", message=menu)  # буфер полон: «Введено: 123/—»
        for digit in "4567890":
            dialog.press("m:key:bksp", message=menu)  # ⌫ — освободить место
            dialog.press(f"m:key:{digit}", message=menu)
        dialog.press("m:key:clear", message=menu)  # C — очистить активное поле
        _wait_menu_text(dialog, menu, "Введено: —/—")
        dialog.press("m:field:dia", message=menu)  # фокус → ДАД
        dialog.press("m:key:8", message=menu)
        _wait_menu_text(dialog, menu, "Введено: —/8")
        dialog.press("m:field:pulse", message=menu)  # фокус → Пульс
        dialog.press("m:key:7", message=menu)
        _wait_menu_text(dialog, menu, "пульс 7")
        dialog.press("m:reset", message=menu)  # «Изменить» — сброс всех полей
        _wait_menu_text(dialog, menu, "Введено: —/—")
        dialog.press("m:cancel", contains=texts.MAIN_MENU, message=menu)

    #: Кнопка отмены каждого типа; «Отмена» калькулятора ведёт в главное меню (§6.3).
    CANCEL_CALLBACKS = {
        "bp": "m:cancel",
        "med": "menu:back",  # «Сначала добавьте лекарства» — «Добавить»/«В меню»
        "walk": "add:cancel",
        "food": "add:cancel",
        "stress": "add:cancel",
        "sleep": "add:cancel",
        "note": "add:back",
    }

    @pytest.mark.parametrize("type_key", sorted(texts.EVENT_TYPE_LABELS))
    def test_add_record_every_type(self, menu, dialog, db, tg_me, type_key) -> None:
        """Каждый тип «➕ Добавить запись» открывает свой экран и отменяется (§6.14).

        Для `med` список лекарств очищается: пустой список показывает
        «Сначала добавьте лекарства» и тип проверяется по кнопке.
        """
        if type_key == "med":
            db.delete_all_medications(tg_me.id)
        dialog.press("add:menu", contains=texts.EVENT_MENU_ASK, message=menu)
        dialog.press(f"add:{type_key}", message=menu)
        dialog.press(self.CANCEL_CALLBACKS[type_key], message=menu)


# ─────────────────── Находки ручной приёмки (внимание на будущих прогонах) ───────────────────


class TestUserFindings:
    """Находки автора при ручной проверке 2026-11-17 — постоянные проверки.

    Каждый баг-репорт превращён в чек, чтобы будущие прогоны ловили рецидив:
    F1 «ссылка ЛК не кликабельна» → кнопка-ссылка; F2 «команды отвечают
    «Например: 120/80 72»» → `test_commands`; F3 «/start не вызывает главное
    окно» → `test_start_does_not_duplicate`; F4 «Что-то пошло не так» на ЛК →
    сообщение без ошибки; F5 антиспам гасит нажатия → быстрые клики живые;
    F6 ложный «Бот не отвечает» → свежий хартбит `status.json`; F7 «шапка
    отчёта: макс/мин» → `test_f7_*` (все три точки экспорта PDF и сводка ЛК,
    R14-А — пустой период); F8 «единообразие ленты» →
    `test_f8_feed_actions_are_uniform`; F9 «данные ленты в PDF» →
    `test_f9_pdf_has_diary_events_section`; F10 закреп меню без посторонней
    подсказки (находка 2026-11-18) → `test_f10_menu_pin_has_no_stray_hint`;
    F11 дубль ключа в compose-файле (находка 2026-11-18) →
    `test_f11_compose_yaml_has_no_duplicate_keys`; F17 время лекарства не
    двигает напоминание (R8 = А, 2026-11-18) → `test_f17_med_time_...`.
    """

    def test_f1_cabinet_link_has_clickable_button(self, menu, dialog) -> None:
        """F1: у ссылки ЛК — URL-кнопка, когда Telegram принимает адрес
        (`https://` или `http://<IP>`); иначе — сообщение без кнопки и без падения."""
        screen = dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        screen = dialog.press("menu:cabinet", message=menu)
        assert_any_shown(
            screen,
            [texts.CABINET_EMAIL_PROMPT, "Ссылка для входа в личный кабинет"],
        )
        buttons = data_texts(screen)
        assert "menu:back" in buttons, "нет «В меню»"
        url_buttons = [
            b for row in screen.buttons or [] for b in row if b.url
        ]
        link_text = (screen.text or "").rsplit("\n", 1)[-1]
        if link_text.startswith("https://") or re.match(
            r"http://(?:\d{1,3}(?:\.\d{1,3}){3}|[\w-]+(?:\.[\w-]+)+)", link_text
        ):
            assert url_buttons and url_buttons[0].text == texts.BTN_OPEN_CABINET, (
                "Telegram принимает адрес — кнопка «Открыть личный кабинет» должна быть"
            )
        open_menu(dialog)

    def test_f4_cabinet_shows_no_error(self, menu, dialog) -> None:
        """F4: «Личный кабинет» не показывает «Что-то пошло не так» (ST1-ошибка)."""
        screen = dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        screen = dialog.press("menu:cabinet", message=menu)
        assert texts.ERROR_GENERIC not in (screen.text or ""), (
            "экран ЛК упал в общий обработчик ошибок"
        )
        assert texts.ERROR_SYSTEMIC not in (screen.text or "")
        open_menu(dialog)

    def test_f5_rapid_clicks_are_not_swallowed(self, menu, dialog) -> None:
        """F5: антиспам не гасит нажатия (на стенде 0/0): два быстрых клика
        подряд обрабатываются — при включённом throttle второй молчал бы и
        экран остался бы прежним."""
        message = dialog.refresh(menu)
        dialog._loop.run(message.click(data="menu:views"))
        dialog._loop.run(message.click(data="menu:config"))  # без паузы
        screen = _wait_menu_text(dialog, menu, texts.CONFIG_MENU)
        assert "set:profile" in data_texts(screen)

    def test_f6_status_heartbeat_is_fresh(self) -> None:
        """F6: статус-строка лендинга не врёт — хартбит свежий (≤ 3 минут, ST2).

        Бот жив = тик §6.7 пишет `status.json`; устаревший файл = робот
        честно сообщает «бот не отвечает» (находка: ложный красный статус).
        """
        import json as json_module

        from bot.services.heartbeat import is_fresh

        path = os.path.join("web", "landing", "status.json")
        assert os.path.exists(path), "status.json не записан — бот не тикает?"
        payload = json_module.loads(open(path, encoding="utf-8").read())
        assert payload.get("ok") is True
        assert is_fresh(payload["at"]), f"хартбит устарел: {payload.get('at')}"

    # ── F7–F9: отчёт и лента (0.7.2, п. 30; REPORT-UX R1–R9) ──

    @staticmethod
    def _utc(epoch: float) -> str:
        """Момент в UTC для `QaDb.add_*` (`ГГГГ-ММ-ДД ЧЧ:ММ:СС`)."""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))

    @staticmethod
    def _local(db, tg_id: int, utc_stamp: str) -> str:
        """Момент в локальном времени пользователя — как в подписи экстремума."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        user = db.user(tg_id)
        moment = datetime.strptime(utc_stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone(ZoneInfo(user["timezone"])).strftime("%d.%m.%Y, %H:%M")

    def _seed_extremes(self, db, tg_id: int) -> tuple[str, str]:
        """Образец F7 (R1/R2-А): максимум/минимум и «хвосты» ДАД.

        160/120 и 130/56 — ложные экстремумы по ДАД: старый баг давал
        «смесь» 180/120. Возвращает ожидаемые строки экстремумов.
        """
        db.delete_measurements(tg_id)
        db.delete_events(tg_id)
        now = time.time()
        t_max = self._utc(now - 5 * 3600)
        db.add_measurement(tg_id, 180, 100, 66, moment=t_max)
        db.add_measurement(tg_id, 160, 120, 70, moment=self._utc(now - 4 * 3600))
        db.add_measurement(tg_id, 130, 56, 64, moment=self._utc(now - 3 * 3600))
        t_min = self._utc(now - 2 * 3600)
        db.add_measurement(tg_id, 116, 81, 62, moment=t_min)
        return (
            f"Максимальное: 180/100 ({self._local(db, tg_id, t_max)})",
            f"Минимальное: 116/81 ({self._local(db, tg_id, t_min)})",
        )

    @staticmethod
    def _pdf_text(payload: bytes) -> str:
        import io

        from pypdf import PdfReader

        return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages)

    @staticmethod
    def _doctor_cookie(db, tg_id: int) -> str:
        """Сессионная cookie врача активной связи (§21) — как в `shot_landing.py`."""
        from web.cabinet.doctor_auth import DoctorSession

        secret = os.environ.get("WEB_SECRET_KEY", "")
        assert secret, "WEB_SECRET_KEY не задан: им подписывается сессия врача"
        return DoctorSession(secret).make(db.ensure_doctor_link(tg_id))

    def test_f7_extrema_at_every_pdf_export_point(self, menu, dialog, db, tg_me, cabinet_url) -> None:
        """F7: строки экстремумов с датой и временем (R1/R2-А) везде, где
        выгружается PDF: бот (§6.5/§6.6), ЛК и кабинет врача (§10.1), плюс
        сводка ЛК (R4-А). Экстремум — одна запись, не «смесь» 180/120."""
        import io

        import httpx

        expected_max, expected_min = self._seed_extremes(db, tg_me.id)
        user_id = db.user(tg_me.id)["id"]
        token = db.web_session(tg_me.id)
        doctor_cookie = self._doctor_cookie(db, tg_me.id)

        def check_pdf(payload: bytes, where: str) -> None:
            assert payload.startswith(b"%PDF"), f"{where}: это не PDF"
            text = self._pdf_text(payload)
            assert expected_max in text, f"{where}: нет строки {expected_max!r}"
            assert expected_min in text, f"{where}: нет строки {expected_min!r}"
            assert "180/120" not in text, f"{where}: экстремум — «смесь» 180/120"

        # ── ЛК: сводка (R4-А) и PDF ──
        with httpx.Client(
            base_url=cabinet_url, cookies={"cabinet_session": token}, follow_redirects=False
        ) as client:
            html = client.get("/?days=7").text
            assert re.search(
                r"Максимальное.*180/100 \(\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}\)", html, re.S
            ), "в сводке ЛК нет строки «Максимальное»"
            assert re.search(
                r"Минимальное.*116/81 \(\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}\)", html, re.S
            ), "в сводке ЛК нет строки «Минимальное»"
            assert "180/120" not in html
            response = client.get("/export/report.pdf?days=7")
            assert response.status_code == 200, "ЛК не отдал PDF"
            check_pdf(response.content, "ЛК")

        # ── Кабинет врача (§21) ──
        with httpx.Client(
            base_url=cabinet_url, cookies={"doctor_session": doctor_cookie}, follow_redirects=False
        ) as client:
            response = client.get(f"/doctor/patient/{user_id}/export.pdf?days=7")
            assert response.status_code == 200, "кабинет врача не отдал PDF"
            check_pdf(response.content, "врач")

        # ── Бот: «Настройки и профиль» → «Экспорт PDF» (§6.6, §10.1) ──
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        dialog.press("menu:settings", message=menu)
        dialog.press("set:pdf", contains=texts.CSV_ASK, message=menu)
        known = dialog.known_ids()
        dialog.press("pdf:7", message=menu)  # экран: «Готовлю отчёт…» → документ
        doc_message = dialog.wait_new(["Отчёт по давлению за"], since=known)
        assert doc_message.document is not None, "бот прислал не документ"
        buffer = io.BytesIO()
        dialog._loop.run(doc_message.download_media(file=buffer))
        check_pdf(buffer.getvalue(), "бот")
        open_menu(dialog)

    def test_f7_empty_period_refused_at_every_export_point(
        self, menu, dialog, db, tg_me, cabinet_url
    ) -> None:
        """F7, R14-А: PDF за период без измерений (но с событиями) не
        формируется ни в одной из трёх точек — «За этот период записей нет»."""
        import httpx

        db.delete_measurements(tg_me.id)
        db.delete_events(tg_me.id)
        db.add_event(tg_me.id, type_="note", note="событие без измерений")
        user_id = db.user(tg_me.id)["id"]
        token = db.web_session(tg_me.id)
        doctor_cookie = self._doctor_cookie(db, tg_me.id)
        for label, cookies, url in (
            ("ЛК", {"cabinet_session": token}, "/export/report.pdf?days=7"),
            (
                "врач",
                {"doctor_session": doctor_cookie},
                f"/doctor/patient/{user_id}/export.pdf?days=7",
            ),
        ):
            with httpx.Client(base_url=cabinet_url, cookies=cookies, follow_redirects=False) as client:
                response = client.get(url)
                assert response.status_code in (302, 303), f"{label}: пустой период — только редирект"
                assert "error=empty" in response.headers["location"], f"{label}: нет error=empty"
        dialog.press("menu:config", contains=texts.CONFIG_MENU, message=menu)
        dialog.press("menu:settings", message=menu)
        dialog.press("set:pdf", contains=texts.CSV_ASK, message=menu)
        screen = dialog.press("pdf:7", contains=texts.PDF_EMPTY, message=menu)
        assert texts.PDF_EMPTY in (screen.text or ""), "бот не сказал «За этот период записей нет»"
        open_menu(dialog)

    def test_f8_feed_actions_are_uniform(self, db, tg_me, cabinet_url) -> None:
        """F8: единая колонка «Действия» у всех типов строк ленты (R6-А),
        иконки «✏️»/«🗑» с `title` и `aria-label` (R7-А); журнал записи
        отдельной строкой не идёт (§4.11), строка бота — только просмотр."""
        import httpx

        db.delete_measurements(tg_me.id)
        db.delete_events(tg_me.id)
        db.delete_all_medications(tg_me.id)
        now = time.time()
        med_journal = db.add_medication(tg_me.id, "QA-Журнальное", "5 мг", "morning")
        med_cabinet = db.add_medication(tg_me.id, "QA-Кабинетное", "10 мг", "noon")
        med_bot = db.add_medication(tg_me.id, "QA-Ботовское", "20 мг", "evening")
        moment = self._utc(now - 3 * 3600)
        measurement_id = db.add_measurement(tg_me.id, 128, 82, 70, moment=moment)
        db.link_measurement_medication(measurement_id, med_journal)
        db.add_intake_log(tg_me.id, med_journal, moment=moment)  # журнал записи
        db.add_intake_log(tg_me.id, med_cabinet, moment=self._utc(now - 2 * 3600))
        db.add_intake_log(tg_me.id, med_bot, moment=self._utc(now - 3600), source="bot")
        db.add_event(tg_me.id, type_="note", moment=self._utc(now - 900), note="QA-прогулка")
        token = db.web_session(tg_me.id)
        with httpx.Client(base_url=cabinet_url, cookies={"cabinet_session": token}) as client:
            html = client.get("/?days=7").text
        rows = [chunk.split("</tr>")[0] for chunk in html.split('<tr class="feed-row')[1:]]
        assert len(rows) == 4, f"ожидали 4 строки ленты, получили {len(rows)}"
        for row in rows:
            assert 'data-label="Действия"' in row, "нет колонки «Действия»"
        # журнал записи отдельной строкой не идёт — виден в «Лекарстве» измерения
        assert all(
            "Приём лекарств" not in row or "QA-Журнальное" not in row for row in rows
        ), "журнал записи задублирован отдельной строкой"
        # правимые строки (измерение, событие, приём кабинета) — иконки канона
        for row in rows:
            if "QA-Ботовское" in row:
                continue
            assert "icon-btn" in row, "кнопки строки — не канонические иконки"
            assert 'title="Изменить"' in row and 'aria-label="Изменить"' in row
            assert 'title="Удалить"' in row and 'aria-label="Удалить"' in row
        # строка бота — только просмотр: пустая ячейка «Действий»
        bot_row = next(row for row in rows if "QA-Ботовское" in row)
        assert "?edit_int=" not in bot_row and "?confirm_delete_int=" not in bot_row

    def test_f9_pdf_has_diary_events_section(self, db, tg_me, cabinet_url) -> None:
        """F9: в PDF раздел «События дневника» после таблицы (R9-Б): события и
        standalone-приёмы хронологически, журнал приёма не дублируется (уже в
        колонке «Лекарство»)."""
        import httpx

        db.delete_measurements(tg_me.id)
        db.delete_events(tg_me.id)
        db.delete_all_medications(tg_me.id)
        now = time.time()
        med_journal = db.add_medication(tg_me.id, "QA-Журнальное", "5 мг", "morning")
        med_standalone = db.add_medication(tg_me.id, "QA-Стandalone", "10 мг", "noon")
        moment = self._utc(now - 4 * 3600)
        measurement_id = db.add_measurement(tg_me.id, 128, 82, 70, moment=moment)
        db.link_measurement_medication(measurement_id, med_journal)
        db.add_intake_log(tg_me.id, med_journal, moment=moment)  # журнал записи
        db.add_event(tg_me.id, type_="note", moment=self._utc(now - 3 * 3600), note="QA-событие")
        db.add_intake_log(tg_me.id, med_standalone, moment=self._utc(now - 2 * 3600))
        token = db.web_session(tg_me.id)
        with httpx.Client(base_url=cabinet_url, cookies={"cabinet_session": token}) as client:
            response = client.get("/export/report.pdf?days=7")
        assert response.status_code == 200, "ЛК не отдал PDF"
        text = self._pdf_text(response.content)
        assert "События дневника" in text, "нет раздела «События дневника»"
        tail = text.split("События дневника", 1)[1]
        assert "QA-событие" in tail, "событие не попало в раздел"
        assert "QA-Стandalone" in tail, "standalone-приём не попал в раздел"
        assert "QA-Журнальное" not in tail, "журнал приёма задублирован"
        assert tail.index("QA-событие") < tail.index("QA-Стandalone"), "не по хронологии"

    def test_f10_menu_pin_has_no_stray_hint(self, menu, dialog) -> None:
        """F10 (находка 2026-11-18): закреп канонического меню (§6.2) не
        порождает постороннего «Выберите пункт меню» — служебное сообщение
        не свободный ввод (§7.3)."""
        known = dialog.known_ids()
        dialog.send_input("/start")
        dialog.wait_new([texts.MAIN_MENU], since=known)
        time.sleep(3)  # окно для ложного ответа на закреп
        last = dialog.last()
        assert texts.MENU_HINT not in (last.text or ""), (
            "закреп меню породил постороннее «Выберите пункт меню»"
        )

    def test_f11_compose_yaml_has_no_duplicate_keys(self) -> None:
        """F11 (находка 2026-11-18): compose-файлы без дублей ключей YAML.

        Дубль `CABINET_URL` в `qa/docker-compose.qa.yml` PyYAML молча съедал
        (остаётся последний), а docker compose отклонял файл целиком
        («mapping key already defined») — стенд не пересобирался. В образе
        робота виден только qa-файл; корневые compose-файлы проверяет
        юнит-тест `tests/test_compose_yaml.py` (он же ловит рецидив, когда
        сам робот из-за сломанного compose уже не стартует).
        """
        from qa.acceptance_common import compose_files, load_yaml_strict

        root = Path(__file__).resolve().parents[1]
        files = compose_files(root)
        assert files, f"compose-файлы не найдены под {root}"
        for path in files:
            load_yaml_strict(path)  # YAMLError с файлом и строкой на дубле

    def test_f17_med_time_does_not_move_slot_or_text(self) -> None:
        """F17 (R8 = А, 2026-11-18): время лекарства — пользовательская метка.

        Отличное от времени слота время (`medications.time`) не двигает
        срабатывание слота (оно — по времени слота из «Напоминаний», §6.7)
        и не попадает в текст слот-сообщения.
        """
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from bot import texts
        from bot.models import Medication
        from bot.services.reminders import next_slot_occurrence

        med = Medication(
            id=1,
            user_id=1,
            name="Лозартан",
            dosage="50 мг",
            slot="evening",
            time="21:00",  # ≠ время слота 20:00
        )
        # Перечень слот-сообщения строится из имени и дозировки (§6.7).
        line = texts.fmt_med_item(med.name, med.dosage)
        message = texts.fmt_slot_message("evening", [line])
        assert "Лозартан 50 мг" in message
        assert med.time not in message, "время лекарства попало в текст слот-сообщения"
        # Срабатывание слота — по его времени (20:00), не по времени лекарства.
        moment = next_slot_occurrence(
            "20:00", "Europe/Minsk", datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        )
        local = moment.astimezone(ZoneInfo("Europe/Minsk"))
        assert local.strftime("%H:%M") == "20:00"


# ─────────────────────── «📄 Отчет», «ℹ️ О боте», команды ───────────────────────


class TestServiceElements:
    REPORT_MARKERS = [
        texts.DOCTOR_REPORT_NO_LINK,
        "Ссылка для вашего врача",
        "Ссылка отправлена врачу",
        "Отчёт отправлен врачу",
        "Перешлите ссылку врачу",
        "Доступ врача отозван",
    ]

    def test_report_element(self, menu, dialog) -> None:
        screen = dialog.press("menu:report", message=menu)
        assert_any_shown(screen, self.REPORT_MARKERS)
        open_menu(dialog)

    def test_about_element(self, menu, dialog) -> None:
        dialog.press("menu:about", contains="Бота пишет один разработчик", message=menu)
        dialog.press("menu:back", contains=texts.MAIN_MENU, message=menu)

    def test_commands(self, menu, dialog) -> None:
        """Команды MU13: /record, /stats, /help, /visits → свои экраны (§6.9)."""
        record = dialog.send("/record")
        assert texts.KEYPAD_HINT in (record.text or ""), "/record не открыл калькулятор"
        stats = dialog.send("/stats")
        assert texts.BTN_STATS in (stats.text or ""), "/stats не открыл статистику"
        help_screen = dialog.send("/help")
        assert "Бота пишет один разработчик" in (help_screen.text or ""), "/help не открыл «О боте»"
        visits = dialog.send("/visits")
        assert "Визиты и анализы" in (visits.text or ""), "/visits не открыл визиты"
        support = dialog.send("/support")
        assert_any_shown(support, ["Поддержка добровольная", "Бота пишет один разработчик"])
        cabinet = dialog.send("/cabinet")
        assert_any_shown(
            cabinet, [texts.CABINET_EMAIL_PROMPT, "Ссылка для входа в личный кабинет"]
        )
        doctor = dialog.send("/iamdoctor")
        assert_any_shown(doctor, [texts.DOCTOR_REGISTER_ASK, "Вы зарегистрированы как врач"])
        # Команды сбрасывают FSM и не «съедаются» вводом (§6.9) — меню снова на месте.
        dialog.send_input("/start")
        _wait_menu_text(dialog, menu, texts.MAIN_MENU)
