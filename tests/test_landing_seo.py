"""SEO и скорость лендинга (SPEC.md, §16.1, «SEO и скорость»; doc/LANDING-SEO-QUESTIONS.md)."""

from __future__ import annotations

import html as html_lib
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from qa.screenshots import make_og
from tools.landing import build_css, make_icons

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "web" / "landing"
SITE = "https://bp-diary.electroman.by/"

#: Классы разметки, у которых нет CSS-правил: хуки JS и маркер group.
NON_TAILWIND_CLASSES = {"group", "screen-open", "screens-tab", "screen-close", "screen-lightbox"}


@pytest.fixture(scope="module")
def html() -> str:
    return (LANDING / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def head(html: str) -> str:
    return html[: html.index("</head>")]


def _meta(head: str, attr: str, name: str) -> str:
    match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)">', head)
    assert match, name
    return html_lib.unescape(match.group(1))


def _text(fragment: str) -> str:
    """Видимый текст фрагмента HTML: без тегов, пробелы схлопнуты."""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


class TestHead:
    def test_title(self, head: str) -> None:
        assert "<title>Дневник артериального давления в Telegram — Дневник гипертоника</title>" in head

    def test_description(self, head: str) -> None:
        assert _meta(head, "name", "description") == (
            "Бесплатный дневник давления в Telegram: запись давления и пульса кнопками, "
            "напоминания о лекарствах, графики и отчёт для врача в PDF. Без рекламы."
        )

    def test_canonical(self, head: str) -> None:
        assert f'<link rel="canonical" href="{SITE}">' in head

    @pytest.mark.parametrize(
        ("prop", "value"),
        [
            ("og:type", "website"),
            ("og:site_name", "Дневник гипертоника"),
            ("og:locale", "ru_RU"),
            ("og:url", SITE),
            ("og:title", "Дневник гипертоника — дневник давления в Telegram"),
            (
                "og:description",
                "Записывайте давление и пульс за пару секунд, получайте напоминания о лекарствах "
                "и показывайте врачу сводку. Бесплатно, без рекламы.",
            ),
            ("og:image", SITE + "img/og.png"),
            ("og:image:width", "1200"),
            ("og:image:height", "630"),
        ],
    )
    def test_open_graph(self, head: str, prop: str, value: str) -> None:
        assert _meta(head, "property", prop) == value

    def test_twitter_card(self, head: str) -> None:
        assert _meta(head, "name", "twitter:card") == "summary_large_image"

    def test_theme_color(self, head: str) -> None:
        assert _meta(head, "name", "theme-color") == "#2563eb"

    @pytest.mark.parametrize(
        ("name", "placeholder"),
        [("yandex-verification", "YOUR_YANDEX_CODE"), ("google-site-verification", "YOUR_GOOGLE_CODE")],
    )
    def test_webmaster_placeholders(self, head: str, name: str, placeholder: str) -> None:
        """В репозитории — плейсхолдеры; реальные коды автор вставляет перед деплоем."""
        assert _meta(head, "name", name) == placeholder

    @pytest.mark.parametrize("counter", ["mc.yandex.ru", "googletagmanager", "google-analytics"])
    def test_no_analytics(self, html: str, counter: str) -> None:
        assert counter not in html

    @pytest.mark.parametrize(
        "link",
        [
            '<link rel="icon" href="favicon.ico" sizes="32x32">',
            '<link rel="icon" href="favicon.svg" type="image/svg+xml">',
            '<link rel="apple-touch-icon" href="apple-touch-icon.png">',
        ],
    )
    def test_icon_links(self, head: str, link: str) -> None:
        assert link in head

    def test_font_preload(self, head: str) -> None:
        assert re.search(r'<link rel="preload" as="style" href="https://fonts\.googleapis\.com/css2\?family=Inter', head)

    def test_static_tailwind(self, html: str) -> None:
        assert "cdn.tailwindcss.com" not in html
        assert "tailwind.config" not in html
        assert '<link rel="stylesheet" href="css/tailwind.css">' in html


@pytest.fixture(scope="module")
def graph(head: str) -> dict[str, dict]:
    """Узлы JSON-LD по @type."""
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', head, re.S)
    assert len(blocks) == 1
    data = json.loads(blocks[0].replace("<\\/", "</"))
    assert data["@context"] == "https://schema.org"
    return {node["@type"]: node for node in data["@graph"]}


class TestJsonLd:
    def test_types(self, graph: dict[str, dict]) -> None:
        assert set(graph) == {"Person", "SoftwareApplication", "FAQPage"}

    def test_application(self, graph: dict[str, dict]) -> None:
        app = graph["SoftwareApplication"]
        assert app["name"] == "Дневник гипертоника"
        assert app["applicationCategory"] == "HealthApplication"
        assert app["operatingSystem"] == "Telegram, Web"
        assert app["offers"]["price"] == "0"
        assert app["installUrl"] == "https://t.me/my_bp_diary_bot?start=landing"
        assert app["author"] == {"@id": graph["Person"]["@id"]}

    def test_person_matches_support(self, graph: dict[str, dict], html: str) -> None:
        person = graph["Person"]
        assert person["name"] in html
        assert person["email"] == "mailto:info@electroman.by"
        assert person["sameAs"] == ["https://t.me/ElectroManBY"]

    def test_faq_matches_visible(self, graph: dict[str, dict], html: str) -> None:
        """Вопросы и ответы JSON-LD — дословно видимый FAQ."""
        section = html[html.index('id="faq"') : html.index('id="docs"')]
        visible = [
            (_text(q.split("<span", 1)[0]), _text(a))
            for q, a in re.findall(r"<summary[^>]*>(.*?)</summary>(.*?)</details>", section, re.S)
        ]
        ld = [
            (item["name"], item["acceptedAnswer"]["text"])
            for item in graph["FAQPage"]["mainEntity"]
        ]
        assert ld == visible
        assert len(ld) == 4


class TestTexts:
    """Правки S6 — утверждены дословно."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "Дневник артериального давления в Telegram",
            "Запись артериального давления и пульса — цифрой в чат или набором как на калькуляторе.",
            "Графики, сводка и CSV для врача.",
            "Артериальное давление и пульс — набором цифр как на калькуляторе или числом в чат, за пару секунд.",
            "Журнал измерений, сводка и выгрузка CSV за период — чтобы приём прошёл по делу, а не по памяти.",
            "Нет. И отдельное приложение ставить не нужно: достаточно открыть бота в Telegram и нажать «Старт» — дальше всё кнопками.",
            "Да. В боте есть экспорт журнала измерений в CSV за 7 дней, 30 дней или всё время — таблица открывается в Excel и подходит для врача.",
        ],
    )
    def test_phrase(self, html: str, phrase: str) -> None:
        assert phrase in _text(html)


class TestAccessibility:
    def test_hint_contrast(self, html: str) -> None:
        assert 'text-slate-500">Нажмите на экран' in html

    def test_lightbox_title_not_heading(self, html: str) -> None:
        assert '<p id="screen-lightbox-title"' in html
        assert not re.search(r'<h\d id="screen-lightbox-title"', html)

    def test_lightbox_img_without_empty_src(self, html: str) -> None:
        tag = re.search(r'<img id="screen-lightbox-img"[^>]*>', html).group(0)
        assert "src=" not in tag


class TestCrawling:
    def test_robots(self) -> None:
        robots = (LANDING / "robots.txt").read_text(encoding="utf-8").splitlines()
        assert "User-agent: *" in robots
        assert "Allow: /" in robots
        assert f"Sitemap: {SITE}sitemap.xml" in robots

    def test_sitemap(self) -> None:
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        tree = ET.parse(LANDING / "sitemap.xml")
        urls = [node.findtext("s:loc", namespaces=ns) for node in tree.getroot().findall("s:url", ns)]
        assert urls == [SITE, SITE + "privacy.html"]
        for node in tree.getroot().findall("s:url", ns):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", node.findtext("s:lastmod", namespaces=ns))

    def test_privacy_head(self) -> None:
        privacy = (LANDING / "privacy.html").read_text(encoding="utf-8")
        assert '<meta name="description" content="' in privacy
        assert f'<link rel="canonical" href="{SITE}privacy.html">' in privacy
        assert "← На главную</a>" in privacy

    def test_privacy_self_delete(self) -> None:
        """Удаление аккаунта — самому в боте, как в FAQ лендинга (не «напишите автору»)."""
        text = _text((LANDING / "privacy.html").read_text(encoding="utf-8"))
        assert "Напишите автору" not in text
        assert "«Настройки» → «Удалить аккаунт»" in text


class TestAssets:
    def test_og_image_size(self) -> None:
        with Image.open(LANDING / "img" / "og.png") as image:
            assert image.size == (1200, 630)

    def test_icons(self) -> None:
        with Image.open(LANDING / "favicon.ico") as ico:
            assert ico.format == "ICO"
        with Image.open(LANDING / "apple-touch-icon.png") as touch:
            assert touch.size == (180, 180)
        assert (LANDING / "favicon.svg").read_text(encoding="utf-8").startswith("<svg")


class TestNginx:
    CONF = ROOT / "deploy" / "landing-nginx.conf"

    def test_outside_web_root(self) -> None:
        """Конфиг не раздаётся как статика."""
        assert self.CONF.is_file()
        assert not (LANDING / "nginx.conf").exists()

    def test_cache_rules(self) -> None:
        conf = self.CONF.read_text(encoding="utf-8")
        assert 'add_header Cache-Control "public, max-age=2592000";' in conf
        assert 'add_header Cache-Control "no-cache";' in conf
        assert re.search(r"location ~\* \\\.\(\?:webp\|png\|ico\|svg\|css\|js\)\$", conf)
        assert "gzip on;" in conf

    def test_caddy_same_rules(self) -> None:
        caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
        assert 'header @static Cache-Control "public, max-age=2592000"' in caddy
        assert 'header !@static Cache-Control "no-cache"' in caddy


def _css_selector(token: str) -> str:
    """Имя класса, экранированное как в CSS, который генерирует Tailwind v3."""
    out = []
    for char in token:
        if char == ",":
            out.append("\\2c ")
        elif char.isalnum() or char in "-_" or ord(char) > 127:
            out.append(char)
        else:
            out.append("\\" + char)
    return "." + "".join(out)


class TestTailwindBuild:
    CSS = LANDING / "css" / "tailwind.css"

    def test_every_class_built(self, html: str) -> None:
        """Каждый класс разметки есть в собранном CSS (иначе — пересобрать build_css.py)."""
        css = self.CSS.read_text(encoding="utf-8")
        tokens = {t for attr in re.findall(r'class="([^"]*)"', html) for t in attr.split()}
        missing = sorted(
            t for t in tokens - NON_TAILWIND_CLASSES if _css_selector(t) not in css
        )
        assert not missing, f"пересоберите tools/landing/build_css.py: {missing[:20]}"

    def test_custom_rule_kept(self) -> None:
        assert "scroll-padding-top:5.5rem" in self.CSS.read_text(encoding="utf-8")

    def test_size_budget(self) -> None:
        assert self.CSS.stat().st_size < 60_000

    @pytest.mark.parametrize(
        ("system", "machine", "key"),
        [
            ("Windows", "AMD64", "windows-x64.exe"),
            ("Linux", "x86_64", "linux-x64"),
            ("Linux", "aarch64", "linux-arm64"),
            ("Darwin", "arm64", "macos-arm64"),
        ],
    )
    def test_platform_key(self, system: str, machine: str, key: str) -> None:
        assert build_css.platform_key(system, machine) == key
        assert len(build_css.SHA256[key]) == 64

    @pytest.mark.parametrize(("system", "machine"), [("Plan9", "x86_64"), ("Linux", "mips")])
    def test_platform_key_unknown(self, system: str, machine: str) -> None:
        with pytest.raises(SystemExit):
            build_css.platform_key(system, machine)

    def test_sha256(self, tmp_path: Path) -> None:
        path = tmp_path / "f"
        path.write_bytes(b"abc")
        assert build_css.sha256(path) == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )


class TestGenerators:
    def test_save_icons(self, tmp_path: Path) -> None:
        buffer = io.BytesIO()
        Image.new("RGBA", (512, 512), (37, 99, 235, 255)).save(buffer, "PNG")
        make_icons.save_icons(buffer.getvalue(), tmp_path)
        with Image.open(tmp_path / "apple-touch-icon.png") as touch:
            assert touch.size == (180, 180)
            assert touch.mode == "RGB"
        with Image.open(tmp_path / "favicon.ico") as ico:
            assert ico.format == "ICO"

    def test_og_html_embeds_images(self) -> None:
        page = make_og.og_html()
        assert page.count("data:image/webp;base64,") == 2
        assert "data:image/svg+xml;base64," in page
        assert "file://" not in page
