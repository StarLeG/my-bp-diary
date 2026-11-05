"""Точка входа `python -m bot` (SPEC.md, §16.3).

Код выхода: 0 — штатно, 1 — ошибка конфигурации (нет/битый `BOT_TOKEN`).
"""
from __future__ import annotations

import sys

from bot.main import main

if __name__ == "__main__":
    sys.exit(main())
