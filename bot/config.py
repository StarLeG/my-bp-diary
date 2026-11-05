"""Конфигурация приложения (SPEC.md, раздел 3).

Переменные окружения читаются из `.env` (python-dotenv) и валидируются
Pydantic-моделью `Settings`. Токен бота и секреты никогда не логируются.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

#: Часовой пояс по умолчанию, если пользователь пропустил выбор (IANA).
DEFAULT_TIMEZONE = "Europe/Minsk"

#: Уровни логирования, которые принимаются в LOG_LEVEL.
_LOG_LEVELS = frozenset(logging.getLevelNamesMapping())

#: Плейсхолдеры URL: при них кнопка «Поддержать» скрывается.
_PLACEHOLDER_URLS = frozenset(
    {
        "change_me",
        "changeme",
        "example.com",
        "http://example.com",
        "https://example.com",
        "your_url_here",
        "todo",
    }
)


class Settings(BaseModel):
    """Настройки приложения из переменных окружения.

    Поля соответствуют таблице переменных окружения в SPEC.md, раздел 3.
    """

    bot_token: str
    """Токен бота от @BotFather. Только из окружения, не логируется."""

    support_url: str = ""
    """Ссылка на страницу поддержки. Пусто или плейсхолдер → кнопка скрыта."""

    author_contact: str = ""
    """Контакт автора. Пусто → кнопка «Написать автору» скрыта."""

    db_path: str = "data/bot.sqlite3"
    """Путь к файлу SQLite."""

    default_timezone: str = DEFAULT_TIMEZONE
    """Часовой пояс (IANA), если пользователь пропустил выбор."""

    log_level: str = "INFO"
    """Уровень логирования."""

    web_secret_key: str = ""
    """Секрет для подписи сессий и magic link личного кабинета."""

    cabinet_url: str = ""
    """Base URL личного кабинета. Пусто = кабинет не развёрнут."""

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        """Токен обязателен: без него бот не работает."""
        value = value.strip()
        if not value:
            raise ValueError("BOT_TOKEN не задан")
        return value

    @field_validator("default_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Таймзона должна быть валидным IANA-именем (zoneinfo)."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"Некорректный часовой пояс: {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Уровень логирования должен быть известен модулю logging."""
        value = value.strip().upper()
        if value not in _LOG_LEVELS:
            raise ValueError(f"Некорректный LOG_LEVEL: {value!r}")
        return value

    @property
    def support_button_visible(self) -> bool:
        """Кнопка «Поддержать» видна, только если URL задан и не плейсхолдер."""
        url = self.support_url.strip().lower().rstrip("/")
        return bool(url) and url not in _PLACEHOLDER_URLS

    @property
    def author_button_visible(self) -> bool:
        """Кнопка «Написать автору» видна, только если контакт задан."""
        return bool(self.author_contact.strip())

    @property
    def cabinet_button_visible(self) -> bool:
        """Кабинет развёрнут: заданы и секрет подписи, и base URL."""
        return bool(self.web_secret_key.strip() and self.cabinet_url.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Загрузить настройки из окружения (`.env` подхватывается автоматически).

    Результат кэшируется: конфиг неизменяем после загрузки (SPEC.md, раздел 7.2).
    Для тестов кэш очищается через `get_settings.cache_clear()`.
    """
    load_dotenv()
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        support_url=os.getenv("SUPPORT_URL", ""),
        author_contact=os.getenv("AUTHOR_CONTACT", ""),
        db_path=os.getenv("DB_PATH", "data/bot.sqlite3"),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", DEFAULT_TIMEZONE),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        web_secret_key=os.getenv("WEB_SECRET_KEY", ""),
        cabinet_url=os.getenv("CABINET_URL", ""),
    )
