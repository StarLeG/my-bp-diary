"""Хартбит живости бота для статус-строки лендинга (0.7.0, ST2-А).

Тик сверки (§6.7, раз в минуту) пишет файл `status.json` в статическую
директорию лендинга (`web/landing`, отдаётся его nginx'ом):

    {"ok": true, "at": "2026-11-17T12:00:00+00:00"}   # время — UTC

JS лендинга (`web/landing/index.html`) показывает «🟢 Бот работает» или
«🔴 Бот не отвечает с HH:MM» (локальное время браузера): файл старше
3 минут — бот не тикает (упал процесс или завис сервер).

Запись обёрнута в try-except (решение ST2): переполненный диск или права на
запись не должны ронять бота — статус-строка просто устареет.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Порог устаревания хартбита — договорённость со скриптом лендинга (ST2-А).
STALE_AFTER_SECONDS = 180


def write_status(path: str) -> None:
    """Переписать `status.json` текущим моментом (UTC); ошибки — в лог, не наружу.

    Запись — во временный файл и `os.replace` рядом: читатель (nginx/JS) не
    увидит полузаписанный JSON.
    """
    if not path:
        return  # статус выключен (нет пути)
    try:
        payload = {"ok": True, "at": datetime.now(timezone.utc).isoformat()}
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as error:  # noqa: BLE001 — статус не должен ронять бота (ST2)
        logger.warning("Хартбит не записан (%s): %s", path, error)


def is_fresh(at: str, *, now: datetime | None = None) -> bool:
    """Свежий ли хартбит (не старше `STALE_AFTER_SECONDS`); ISO-время — UTC.

    Опорная точка для тестов и будущего `/status` кабинета (ST2-Б, если решат).
    """
    moment = datetime.fromisoformat(at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return 0 <= (current - moment).total_seconds() <= STALE_AFTER_SECONDS
