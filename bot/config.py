"""Конфигурация приложения (SPEC.md, раздел 3).

Переменные окружения читаются из `.env` (python-dotenv) и валидируются
Pydantic-моделью `Settings`. Токен бота и секреты никогда не логируются.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator

#: Часовой пояс по умолчанию, если пользователь пропустил выбор (IANA).
DEFAULT_TIMEZONE = "Europe/Minsk"

#: Уровни логирования, которые принимаются в LOG_LEVEL.
_LOG_LEVELS = frozenset(logging.getLevelNamesMapping())

#: Страница «Персональные данные» по умолчанию (`PRIVACY_URL`, SPEC.md, §3, §16.1).
DEFAULT_PRIVACY_URL = "https://bp-diary.electroman.by/privacy.html"

#: Журнал удалений по умолчанию (`DELETION_LOG_PATH`, §19): вне файла БД.
DEFAULT_DELETION_LOG_PATH = "data/deletions.log"

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


def parse_admin_ids(raw: str) -> tuple[int, ...]:
    """Разобрать `ADMIN_TELEGRAM_IDS`: целые ID через запятую (SPEC.md, раздел 3).

    Пустая строка → пустой кортеж (админ-панель отключена). Мусорные элементы
    отбрасываются с предупреждением; дубликаты убираются, порядок сохраняется.
    """
    result: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            admin_id = int(token)
        except ValueError:
            logging.getLogger(__name__).warning(
                "ADMIN_TELEGRAM_IDS: элемент %r не является целым числом — пропущен", token
            )
            continue
        if admin_id not in result:
            result.append(admin_id)
    return tuple(result)


def admin_ids_from_env() -> tuple[int, ...]:
    """ID администраторов прямо из окружения, без полной валидации `Settings`.

    Используется на импорте роутеров: `get_settings()` требует `BOT_TOKEN` и
    при его отсутствии бросает `ValidationError`, чего на импорте быть не должно.
    Значение то же, что `Settings.admin_telegram_ids` (обе читают
    `ADMIN_TELEGRAM_IDS`).
    """
    return parse_admin_ids(os.getenv("ADMIN_TELEGRAM_IDS", ""))


class Settings(BaseModel):
    """Настройки приложения из переменных окружения.

    Поля соответствуют таблице переменных окружения в SPEC.md, раздел 3.
    """

    bot_token: str
    """Токен бота от @BotFather. Только из окружения, не логируется."""

    bot_proxy: str = ""
    """Прокси для Telegram API (`BOT_PROXY`): HTTP(S) или SOCKS. Пусто = без прокси.

    Может содержать учётные данные (`http://user:pass@host:port`) — как секрет,
    не логируется и не попадает в сообщения об ошибках.
    """

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

    admin_telegram_ids: tuple[int, ...] = ()
    """ID администраторов (`ADMIN_TELEGRAM_IDS`). Пусто = админ-панель отключена."""

    backup_encryption_key: str = ""
    """Ключ шифрования бэкапов (`BACKUP_ENCRYPTION_KEY`, SPEC.md, §20)."""

    backup_dir: str = "data/backups"
    """Каталог локальных копий (`BACKUP_DIR`)."""

    s3_endpoint_url: str = ""
    """Endpoint S3-совместимого хранилища. Пусто = off-site отключён."""

    s3_bucket: str = ""
    """Имя бакета S3 для off-site копий (§20)."""

    s3_access_key_id: str = ""
    """Access key S3."""

    s3_secret_access_key: str = ""
    """Secret key S3."""

    smtp_host: str = ""
    """SMTP-сервер писем врачу (§21). Пусто = письма не отправляются."""

    smtp_port: int = 465
    """Порт SMTP: 465 (SSL) или 587 (STARTTLS)."""

    smtp_security: str = "ssl"
    """Защита соединения: `ssl` или `starttls`."""

    smtp_user: str = ""
    """Логин SMTP."""

    smtp_password: str = ""
    """Пароль SMTP; не логируется."""

    smtp_from: str = ""
    """Отправитель: `Дневник гипертоника <noreply@electroman.by>`."""

    privacy_url: str = DEFAULT_PRIVACY_URL
    """Страница «Персональные данные» (`PRIVACY_URL`, §16.1): якоря `#policy`,
    `#consent`, `#rights`. Обязательна — ссылку требует ст. 5 п. 5 Закона № 99-З."""

    deletion_log_key: str = ""
    """Ключ HMAC журнала удалений (`DELETION_LOG_KEY`, §19); не логируется.
    Обязателен при включённых бэкапах (`BACKUP_ENCRYPTION_KEY`)."""

    deletion_log_path: str = DEFAULT_DELETION_LOG_PATH
    """Файл журнала удалений (`DELETION_LOG_PATH`) — вне файла БД и `BACKUP_DIR`."""

    @field_validator("privacy_url")
    @classmethod
    def _validate_privacy_url(cls, value: str) -> str:
        """Ссылка на политику не может быть пустой или плейсхолдером (§3).

        Её нельзя скрыть, как «Поддержать»: сведения ст. 5 п. 5 должны быть
        доступны до согласия, поэтому неверное значение — ошибка при старте.
        """
        value = value.strip()
        lowered = value.lower().rstrip("/")
        parsed = urlparse(value)
        if (
            not value
            or lowered in _PLACEHOLDER_URLS
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.netloc.lower() == "example.com"
        ):
            raise ValueError("PRIVACY_URL: нужен адрес страницы «Персональные данные»")
        return value

    @model_validator(mode="after")
    def _require_deletion_log_key(self) -> "Settings":
        """С включёнными бэкапами журнал удалений обязателен (§19, §20)."""
        if self.backup_encryption_key.strip() and not self.deletion_log_key.strip():
            raise ValueError(
                "DELETION_LOG_KEY не задан: при BACKUP_ENCRYPTION_KEY он обязателен"
            )
        return self

    @field_validator("smtp_security")
    @classmethod
    def _validate_smtp_security(cls, value: str) -> str:
        """Режим SMTP — `ssl` или `starttls` (регистр не важен)."""
        mode = value.strip().lower() or "ssl"
        if mode not in ("ssl", "starttls"):
            raise ValueError("SMTP_SECURITY: ssl или starttls")
        return mode

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        """Токен обязателен: без него бот не работает."""
        value = value.strip()
        if not value:
            raise ValueError("BOT_TOKEN не задан")
        return value

    @field_validator("bot_proxy")
    @classmethod
    def _validate_proxy(cls, value: str) -> str:
        """Прокси — URL `scheme://[user:pass@]host:port`; пусто = без прокси.

        Схемы: http, https, socks4, socks5, socks5h. Значение может содержать
        пароль, поэтому в текст ошибки оно не включается.
        """
        value = value.strip()
        if not value:
            return ""
        parsed = urlparse(value)
        schemes = {"http", "https", "socks4", "socks5", "socks5h"}
        if parsed.scheme not in schemes or not parsed.netloc:
            raise ValueError(
                "BOT_PROXY: нужен URL вида http://user:pass@host:port "
                "(или socks5://host:port)"
            )
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

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _validate_admin_ids(cls, value: object) -> tuple[int, ...]:
        """Принимается и строка `ADMIN_TELEGRAM_IDS`, и последовательность ID."""
        if isinstance(value, str):
            return parse_admin_ids(value)
        if value is None:
            return ()
        return tuple(int(item) for item in value)  # type: ignore[union-attr]

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

    @property
    def smtp_configured(self) -> bool:
        """Письма врачу включены: заданы сервер и отправитель (§3, §21)."""
        return bool(self.smtp_host.strip() and self.smtp_from.strip())

    @property
    def s3_configured(self) -> bool:
        """Off-site в S3 включён: заданы endpoint, бакет и оба ключа (§20)."""
        return all(
            field.strip()
            for field in (
                self.s3_endpoint_url,
                self.s3_bucket,
                self.s3_access_key_id,
                self.s3_secret_access_key,
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Загрузить настройки из окружения (`.env` подхватывается автоматически).

    Результат кэшируется: конфиг неизменяем после загрузки (SPEC.md, раздел 7.2).
    Для тестов кэш очищается через `get_settings.cache_clear()`.
    """
    load_dotenv()
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        bot_proxy=os.getenv("BOT_PROXY", ""),
        support_url=os.getenv("SUPPORT_URL", ""),
        author_contact=os.getenv("AUTHOR_CONTACT", ""),
        db_path=os.getenv("DB_PATH", "data/bot.sqlite3"),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", DEFAULT_TIMEZONE),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        web_secret_key=os.getenv("WEB_SECRET_KEY", ""),
        cabinet_url=os.getenv("CABINET_URL", ""),
        admin_telegram_ids=parse_admin_ids(os.getenv("ADMIN_TELEGRAM_IDS", "")),
        backup_encryption_key=os.getenv("BACKUP_ENCRYPTION_KEY", ""),
        backup_dir=os.getenv("BACKUP_DIR", "data/backups"),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
        s3_bucket=os.getenv("S3_BUCKET", ""),
        s3_access_key_id=os.getenv("S3_ACCESS_KEY_ID", ""),
        s3_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY", ""),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "") or 465),
        smtp_security=os.getenv("SMTP_SECURITY", "ssl"),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        privacy_url=os.getenv("PRIVACY_URL", DEFAULT_PRIVACY_URL),
        deletion_log_key=os.getenv("DELETION_LOG_KEY", ""),
        deletion_log_path=os.getenv("DELETION_LOG_PATH", "") or DEFAULT_DELETION_LOG_PATH,
    )
