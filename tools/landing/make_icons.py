"""Иконки лендинга из favicon.svg (SPEC.md, §16.1, «SEO и скорость»).

Растеризует ``web/landing/favicon.svg`` браузером Playwright (Chromium) и
сохраняет ``favicon.ico`` (16, 32, 48 px) и ``apple-touch-icon.png`` (180 px)::

    uv run --group qa python tools/landing/make_icons.py
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

LANDING = Path(__file__).resolve().parents[2] / "web" / "landing"
SVG = LANDING / "favicon.svg"
ICO_SIZES = (16, 32, 48)
TOUCH_SIZE = 180
#: Размер растра, из которого ужимаются все иконки.
RENDER_SIZE = 512


def render_png(svg: str, size: int = RENDER_SIZE) -> bytes:
    """PNG с прозрачным фоном: SVG, отрисованный Chromium в квадрат size×size."""
    from playwright.sync_api import sync_playwright

    page_html = (
        "<html><body style='margin:0;background:transparent'>"
        f"<div style='width:{size}px;height:{size}px'>{svg}</div></body></html>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(page_html)
            page.locator("svg").evaluate("e => { e.style.width = '100%'; e.style.height = '100%'; }")
            return page.screenshot(omit_background=True)
        finally:
            browser.close()


def save_icons(png: bytes, landing: Path = LANDING) -> None:
    """Сохранить favicon.ico и apple-touch-icon.png из большого PNG."""
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    image.save(landing / "favicon.ico", sizes=[(s, s) for s in ICO_SIZES])
    # iOS не поддерживает прозрачность у apple-touch-icon — кладём на белый фон.
    touch = Image.new("RGBA", image.size, "white")
    touch.alpha_composite(image)
    touch.convert("RGB").resize((TOUCH_SIZE, TOUCH_SIZE), Image.LANCZOS).save(
        landing / "apple-touch-icon.png", optimize=True
    )


if __name__ == "__main__":
    save_icons(render_png(SVG.read_text(encoding="utf-8")))
    print("Готово: favicon.ico, apple-touch-icon.png")
