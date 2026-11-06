"""Статические проверки лендинга (web/landing/index.html).

Руководство на лендинге синхронизировано с фактическими функциями бота
(SPEC.md, §16.1): вход в личный кабинет — из шапки, финального CTA и раздела
«Личный кабинет»; формулировки кнопок — дословно по bot/texts.py.
"""

import re
from pathlib import Path

import pytest

LANDING = Path(__file__).resolve().parents[1] / "web" / "landing" / "index.html"

CABINET_LOGIN_URL = "https://cabinet.electroman.by/login"
TELEGRAM_CTA_URL = "https://t.me/my_bp_diary_bot?start=landing"


@pytest.fixture(scope="module")
def html() -> str:
    """Исходник лендинга одним текстом."""
    return LANDING.read_text(encoding="utf-8")


class TestCabinetLogin:
    """Ссылка «Войти в личный кабинет» на /login веб-кабинета (SPEC.md, §16.1)."""

    def test_login_url_in_three_places(self, html: str) -> None:
        """Шапка, финальный CTA и #docs-cabinet ссылаются на /login кабинета."""
        assert html.count(CABINET_LOGIN_URL) >= 3

    def test_login_buttons_texts(self, html: str) -> None:
        """Кнопки входа подписаны: «Войти» в шапке и «Войти в личный кабинет» в CTA/docs."""
        assert "Войти в личный кабинет" in html
        assert re.search(r">\s*Войти\s*</a>", html)

    def test_telegram_cta_kept(self, html: str) -> None:
        """Telegram-CTA сохранён хотя бы в шапке и финальном блоке."""
        assert html.count(TELEGRAM_CTA_URL) >= 2


class TestHeaderResponsive:
    """Адаптивность шапки (SPEC.md, §16.1; бургер-меню нет)."""

    def test_nav_shows_from_lg(self, html: str) -> None:
        """Навигация показывается с 1024px; на 768px шапка без неё не переполняется."""
        assert re.search(r'<nav class="hidden lg:flex', html)

    def test_logo_name_breakpoints(self, html: str) -> None:
        """Название логотипа: 640–1023 и ≥1280; иначе — только иконка."""
        assert re.search(r'hidden sm:inline lg:hidden xl:inline', html)


class TestSections:
    """Якорные секции лендинга на месте."""

    @pytest.mark.parametrize(
        "section_id",
        [
            "docs",
            "docs-start",
            "docs-record",
            "docs-reminders",
            "docs-meds",
            "docs-stats",
            "docs-cabinet",
            "docs-doctor",
            "docs-data",
            "docs-settings",
            "faq",
            "support",
        ],
    )
    def test_section_id_present(self, html: str, section_id: str) -> None:
        assert f'id="{section_id}"' in html


class TestManualSync:
    """Маркеры актуализации руководства (сверено с bot/texts.py)."""

    @pytest.mark.parametrize(
        "marker",
        [
            "Изменить последнюю",  # BTN_EDIT_LAST
            "Через 30 мин",  # BTN_SNOOZE_30
            "Лекарство удалено",  # MEDS_DELETED
            "мало данных для тренда",  # STATS_TREND_FEW
            "5000",  # ALL_TIME_LIMIT
            "Самочувствие",  # BTN_FEELING
            "Пропускаю",  # BTN_SKIP_SLOT
            "Оставить как есть",  # BTN_KEEP_AS_IS
        ],
    )
    def test_marker_present(self, html: str, marker: str) -> None:
        assert marker in html

    @pytest.mark.parametrize(
        "stale",
        [
            "или в личном кабинете",  # записи в ЛК нет — там только просмотр
            "Напишите автору",  # удаление аккаунта — самоудаление в боте
        ],
    )
    def test_stale_phrase_absent(self, html: str, stale: str) -> None:
        assert stale not in html
