"""Статические проверки лендинга (web/landing/index.html).

Руководство на лендинге синхронизировано с фактическими функциями бота
(SPEC.md, §16.1): вход в личный кабинет — из шапки, финального CTA и раздела
«Личный кабинет»; формулировки кнопок — дословно по bot/texts.py.
"""

import re
from pathlib import Path

import pytest

LANDING = Path(__file__).resolve().parents[1] / "web" / "landing" / "index.html"

#: Допустимые URL кабинета: боевой и локальный тестовый стенд.
#: Перед релизом вернуть боевые ссылки (CLAUDE.md, раздел 5)!
CABINET_LOGIN_URLS = (
    "https://cabinet.electroman.by/login",
    "http://127.0.0.1:8000/login",
)

#: Допустимые Telegram-CTA: боевой и тестовый бот (см. выше про релиз).
TELEGRAM_CTA_URLS = (
    "https://t.me/my_bp_diary_bot?start=landing",
    "https://t.me/testMasterJournal_Bot?start=landing",
)


def _single_url(html: str, urls: tuple[str, ...]) -> str:
    """Единственный URL из допустимых, используемый на лендинге.

    Смесь боевого и тестового — ошибка: ссылки переключаются все разом.
    """
    found = [url for url in urls if url in html]
    assert len(found) == 1, f"ожидался ровно один URL из {urls}, найдено: {found}"
    return found[0]


@pytest.fixture(scope="module")
def html() -> str:
    """Исходник лендинга одним текстом."""
    return LANDING.read_text(encoding="utf-8")


class TestCabinetLogin:
    """Ссылка «Войти в личный кабинет» на /login веб-кабинета (SPEC.md, §16.1)."""

    def test_login_url_in_three_places(self, html: str) -> None:
        """Шапка, финальный CTA и #docs-cabinet ссылаются на /login кабинета."""
        assert html.count(_single_url(html, CABINET_LOGIN_URLS)) >= 3

    def test_login_buttons_texts(self, html: str) -> None:
        """Кнопки входа подписаны: «Войти» в шапке и «Войти в личный кабинет» в CTA/docs."""
        assert "Войти в личный кабинет" in html
        assert re.search(r">\s*Войти\s*</a>", html)

    def test_telegram_cta_kept(self, html: str) -> None:
        """Telegram-CTA сохранён хотя бы в шапке и финальном блоке."""
        assert html.count(_single_url(html, TELEGRAM_CTA_URLS)) >= 2


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


class TestCommunity:
    """Сообщество в Telegram: футер и карточка автора (doc/TG-CHANNEL-QUESTIONS.md, С2-А)."""

    def test_footer_link(self, html: str) -> None:
        footer = html[html.index("<footer") :]
        assert 'href="https://t.me/my_bp_diary"' in footer
        assert "Сообщество в Telegram" in footer

    def test_support_card_link(self, html: str) -> None:
        support = html[html.index('id="support"') :]
        assert 'href="https://t.me/my_bp_diary"' in support
        assert ">@my_bp_diary</a>" in support
        assert "новости, подсказки, вопросы и идеи" in support

    def test_no_other_group_placeholders(self, html: str) -> None:
        """Ссылка на сообщество — только в футере и карточке автора (С2-А, не С2-В)."""
        assert html.count('href="https://t.me/my_bp_diary"') == 2


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
            "Пропускаю сегодня",  # BTN_SKIP_SLOT (0.5.5)
            "Отметить принятые",  # BTN_PARTIAL (0.5.5)
            "💊 Отметить приём",  # BTN_MARK_INTAKE (§6.4, 0.5.5)
            "Оставить как есть",  # BTN_KEEP_AS_IS
            "Экспорт PDF",  # BTN_EXPORT_PDF
            "Архивные записи",  # BTN_ARCHIVE
            "👤 Мои данные",  # BTN_PROFILE (§6.12)
            "Согласие на обработку данных",  # BTN_CONSENT_SETTINGS (§6.11)
            "privacy.html#consent",  # согласие — до первых вопросов (§6.1)
        ],
    )
    def test_marker_present(self, html: str, marker: str) -> None:
        assert marker in html

    @pytest.mark.parametrize(
        "stale",
        [
            "или в личном кабинете",  # записи в ЛК нет — там только просмотр
            "Напишите автору",  # удаление аккаунта — самоудаление в боте
            "хранятся на сервере проекта",  # сервер в Германии, Telegram (§16.1, Т6)
            "Частично…",  # кнопка переименована в 0.5.5 («Отметить принятые»)
        ],
    )
    def test_stale_phrase_absent(self, html: str, stale: str) -> None:
        assert stale not in html


#: Кадры секции «Как это выглядит» и их заголовки — утверждены дословно
#: (doc/LANDING-SCREENS-QUESTIONS.md, третий раунд); средние по дням и свой
#: период — 0.5.3.
SCREENS = {
    "bot-menu": "Главное меню",
    "bot-keypad": "Ввод как на калькуляторе",
    "bot-saved": "Запись сохранена",
    "bot-reminder": "Напоминание по расписанию",
    "bot-meds": "Какие лекарства приняли",
    "bot-stats": "Статистика за 7 дней",
    "bot-daily": "Средние по дням",
    "bot-calendar": "Свой период",
    "cabinet-charts": "Графики с цветными зонами",
    "cabinet-daily": "Средние по дням",
    "cabinet-history": "История записей",
    "cabinet-add-form": "Добавить измерение",
    "cabinet-medications": "Лекарства",
    "cabinet-import": "Импорт из блокнота",
    "cabinet-history-mobile": "С телефона",
    "doctor-list": "Пациенты",
    "doctor-card": "Карточка пациента",
    "doctor-pdf": "PDF-отчёт",
    "doctor-card-mobile": "Кабинет врача с телефона",
}

SCREENS_DIR = LANDING.parent / "img" / "screens"


class TestScreensSection:
    """Секция «Как это выглядит» (#screens, SPEC.md §16.1)."""

    def test_after_how_before_trust(self, html: str) -> None:
        assert html.index('id="how"') < html.index('id="screens"') < html.index('id="trust"')

    def test_title_and_subtitle(self, html: str) -> None:
        assert "Как это выглядит</h2>" in html
        assert "Настоящие экраны бота и кабинетов. Данные на скриншотах — вымышленные." in html

    @pytest.mark.parametrize("tab", ["Telegram-бот", "Личный кабинет", "Кабинет врача"])
    def test_tab_present(self, html: str, tab: str) -> None:
        assert re.search(rf'role="tab"[^>]*>.*?{tab}</button>', html)

    @pytest.mark.parametrize(("name", "title"), SCREENS.items())
    def test_frame_with_caption(self, html: str, name: str, title: str) -> None:
        assert (SCREENS_DIR / f"{name}.webp").is_file()
        # `?v=…` — версия кадра: картинки кешируются на 30 дней (landing-nginx.conf),
        # при пересъёмке кадра версия меняется, чтобы браузеры взяли новый файл.
        tag = re.search(rf'<img src="img/screens/{name}\.webp(\?v=[\w.]+)?"[^>]*>', html)
        assert tag, name
        for attr in ('loading="lazy"', "width=", "height=", f'alt="{title}"'):
            assert attr in tag.group(0)
        assert f">{title}</h3>" in html

    def test_no_extra_frames(self) -> None:
        assert {p.stem for p in SCREENS_DIR.glob("*.webp")} == set(SCREENS)

    def test_frames_weight_budget(self) -> None:
        total = sum(p.stat().st_size for p in SCREENS_DIR.glob("*.webp"))
        assert total <= 1_500_000

    def test_nav_link(self, html: str) -> None:
        assert 'href="#screens"' in html

    def test_lightbox(self, html: str) -> None:
        assert '<dialog id="screen-lightbox"' in html
        js = (LANDING.parent / "script.js").read_text(encoding="utf-8")
        assert "showModal" in js and "screens-tab" in js
