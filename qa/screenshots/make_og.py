"""Картинка превью ссылки лендинга (Open Graph, 1200×630; SPEC.md, §16.1).

Коллаж из готовых кадров ``web/landing/img/screens/``: телефон с главным меню бота
и браузер с графиками кабинета, слева — название и слоган. Данные на кадрах
вымышленные (демо-стенд). Рендер — Playwright (Chromium)::

    uv run --group qa python -m qa.screenshots.make_og
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANDING = ROOT / "web" / "landing"
OUTPUT = LANDING / "img" / "og.png"
WIDTH, HEIGHT = 1200, 630

TITLE = "Дневник гипертоника"
NOTE = "Бесплатно · в Telegram · без рекламы"


def data_uri(path: Path) -> str:
    """Файл как data-URI: страница из set_content не грузит file://-картинки."""
    mime = {".webp": "image/webp", ".svg": "image/svg+xml", ".png": "image/png"}[path.suffix]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def og_html(landing: Path = LANDING) -> str:
    """HTML-разметка коллажа; картинки встроены data-URI из каталога лендинга."""
    screens = landing / "img" / "screens"
    icon = data_uri(landing / "favicon.svg")
    charts = data_uri(screens / "cabinet-charts.webp")
    bot_menu = data_uri(screens / "bot-menu.webp")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;700;800&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ width: {WIDTH}px; height: {HEIGHT}px; overflow: hidden;
         font-family: Inter, system-ui, sans-serif;
         background: linear-gradient(135deg, #eff6ff 0%, #ffffff 55%, #e0f2fe 100%); }}
  .text {{ position: absolute; left: 64px; top: 0; bottom: 0; width: 470px;
          display: flex; flex-direction: column; justify-content: center; }}
  .brand {{ display: flex; align-items: center; gap: 16px; }}
  .brand img {{ width: 64px; height: 64px; }}
  .brand span {{ font-size: 30px; font-weight: 800; color: #0f172a; }}
  h1 {{ margin-top: 36px; font-size: 52px; line-height: 1.1; font-weight: 800; color: #0f172a; }}
  h1 em {{ font-style: normal; color: #2563eb; }}
  p {{ margin-top: 28px; font-size: 24px; font-weight: 500; color: #475569; }}
  .browser {{ position: absolute; left: 560px; top: 70px; width: 600px; border-radius: 18px;
             background: #fff; box-shadow: 0 24px 60px rgba(15,23,42,.18); overflow: hidden; }}
  .bar {{ height: 30px; background: #f1f5f9; display: flex; align-items: center; gap: 7px; padding: 0 14px; }}
  .bar i {{ width: 10px; height: 10px; border-radius: 50%; background: #cbd5e1; }}
  .browser img {{ display: block; width: 100%; }}
  .phone {{ position: absolute; left: 900px; top: 190px; width: 250px; padding: 9px;
           border-radius: 34px; background: #0f172a; box-shadow: 0 24px 60px rgba(15,23,42,.35); }}
  .phone img {{ display: block; width: 100%; border-radius: 26px; }}
</style></head><body>
  <div class="text">
    <div class="brand"><img src="{icon}" alt=""><span>{TITLE}</span></div>
    <h1>Дневник давления, <em>который не мешает жить</em></h1>
    <p>{NOTE}</p>
  </div>
  <div class="browser"><div class="bar"><i></i><i></i><i></i></div>
    <img src="{charts}" alt=""></div>
  <div class="phone"><img src="{bot_menu}" alt=""></div>
</body></html>"""


def render(output: Path = OUTPUT) -> Path:
    """Отрисовать коллаж в PNG 1200×630."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.set_content(og_html(), wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output))
        finally:
            browser.close()
    return output


if __name__ == "__main__":
    print(f"Готово: {render().relative_to(ROOT)}")
