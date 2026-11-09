"""Тесты конфигурации (bot/config.py)."""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from bot.config import (
    DEFAULT_TIMEZONE,
    Settings,
    admin_ids_from_env,
    get_settings,
    parse_admin_ids,
)


def make_settings(**overrides: str) -> Settings:
    """Собрать Settings с валидным токеном и дефолтами."""
    data = {"bot_token": "123:ABC-test-token"}
    data.update(overrides)
    return Settings(**data)


class TestSettingsValidation:
    def test_defaults(self) -> None:
        settings = make_settings()
        assert settings.bot_token == "123:ABC-test-token"
        assert settings.support_url == ""
        assert settings.author_contact == ""
        assert settings.group_url == ""
        assert settings.db_path == "data/bot.sqlite3"
        assert settings.default_timezone == DEFAULT_TIMEZONE
        assert settings.log_level == "INFO"
        assert settings.web_secret_key == ""
        assert settings.cabinet_url == ""

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(bot_token="")

    def test_blank_token_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(bot_token="   ")

    def test_token_is_stripped(self) -> None:
        assert make_settings(bot_token="  tok  ").bot_token == "tok"

    def test_proxy_default_empty(self) -> None:
        assert make_settings().bot_proxy == ""

    @pytest.mark.parametrize(
        "raw",
        [
            "http://127.0.0.1:8080",
            "https://user:pass@proxy.local:3128",
            "http://proxy.local:3128",
            "socks5://127.0.0.1:1080",
            "socks5h://user:pass@proxy.local:1080",
        ],
    )
    def test_proxy_valid(self, raw: str) -> None:
        assert make_settings(bot_proxy=raw).bot_proxy == raw

    def test_proxy_stripped(self) -> None:
        assert make_settings(bot_proxy="  http://p.local:1  ").bot_proxy == "http://p.local:1"

    @pytest.mark.parametrize(
        "raw",
        ["proxy.local:8080", "ftp://p.local", "http://", "   x"],
    )
    def test_proxy_invalid(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            make_settings(bot_proxy=raw)

    def test_proxy_error_hides_credentials(self) -> None:
        """Текст ошибки не раскрывает пароль прокси (§3, безопасность).

        `str(ValidationError)` Pydantic v2 всё равно содержит исходный ввод
        (input), поэтому целиком его не логируют; наружу уходит только loc —
        см. `main()`.
        """
        with pytest.raises(ValidationError) as exc_info:
            make_settings(bot_proxy="ftp://user:secretpw@host:21")
        assert "secretpw" not in exc_info.value.errors()[0]["msg"]

    def test_invalid_timezone_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(default_timezone="Minsk")

    def test_valid_timezone_accepted(self) -> None:
        assert make_settings(default_timezone="Asia/Yekaterinburg").default_timezone == "Asia/Yekaterinburg"

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(log_level="VERBOSE")

    def test_log_level_normalized(self) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"

    def test_known_log_levels_accepted(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            assert make_settings(log_level=level).log_level == level
            assert level in logging.getLevelNamesMapping()


class TestButtonsVisibility:
    @pytest.mark.parametrize("url", ["", "   ", "CHANGE_ME", "change_me", "https://example.com", "https://example.com/", "http://example.com", "TODO"])
    def test_support_button_hidden(self, url: str) -> None:
        assert make_settings(support_url=url).support_button_visible is False

    @pytest.mark.parametrize("url", ["https://example.org", "https://example.org/donate", "  https://example.org  "])
    def test_support_button_visible(self, url: str) -> None:
        assert make_settings(support_url=url).support_button_visible is True

    def test_author_button_hidden_when_empty(self) -> None:
        assert make_settings(author_contact="").author_button_visible is False
        assert make_settings(author_contact="   ").author_button_visible is False

    def test_author_button_visible(self) -> None:
        assert make_settings(author_contact="@author").author_button_visible is True

    @pytest.mark.parametrize("url", ["", "   ", "CHANGE_ME", "change_me", "https://example.com", "https://example.com/", "TODO"])
    def test_group_button_hidden(self, url: str) -> None:
        assert make_settings(group_url=url).group_button_visible is False

    @pytest.mark.parametrize("url", ["https://t.me/my_bp_diary", "  https://t.me/my_bp_diary  ", "https://t.me/my_bp_diary/"])
    def test_group_button_visible(self, url: str) -> None:
        assert make_settings(group_url=url).group_button_visible is True

    def test_cabinet_hidden_when_any_missing(self) -> None:
        assert make_settings().cabinet_button_visible is False
        assert make_settings(web_secret_key="secret").cabinet_button_visible is False
        assert make_settings(cabinet_url="https://cabinet.example").cabinet_button_visible is False
        assert (
            make_settings(web_secret_key="  ", cabinet_url="https://cabinet.example").cabinet_button_visible
            is False
        )

    def test_cabinet_visible_when_configured(self) -> None:
        settings = make_settings(
            web_secret_key="secret", cabinet_url="https://cabinet.example"
        )
        assert settings.cabinet_button_visible is True


class TestGetSettings:
    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "env-token")
        monkeypatch.setenv("BOT_PROXY", "http://proxy.local:3128")
        monkeypatch.setenv("SUPPORT_URL", "https://support.example")
        monkeypatch.setenv("AUTHOR_CONTACT", "@env-author")
        monkeypatch.setenv("DB_PATH", "tmp/x.sqlite3")
        monkeypatch.setenv("DEFAULT_TIMEZONE", "Europe/Moscow")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("WEB_SECRET_KEY", "secret")
        monkeypatch.setenv("CABINET_URL", "https://cabinet.example")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.bot_token == "env-token"
            assert settings.bot_proxy == "http://proxy.local:3128"
            assert settings.support_url == "https://support.example"
            assert settings.author_contact == "@env-author"
            assert settings.db_path == "tmp/x.sqlite3"
            assert settings.default_timezone == "Europe/Moscow"
            assert settings.log_level == "DEBUG"
            assert settings.web_secret_key == "secret"
            assert settings.cabinet_url == "https://cabinet.example"
            # Результат кэшируется: конфиг неизменяем после загрузки.
            assert get_settings() is settings
        finally:
            get_settings.cache_clear()

    def test_cabinet_hides_invalid_proxy_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Кабинет при битом BOT_PROXY падает без пароля в тексте и цепочке (§3)."""
        from web.cabinet.app import create_app

        monkeypatch.setenv("BOT_TOKEN", "env-token")
        monkeypatch.setenv("BOT_PROXY", "socks:user:secretpw@proxy.local:1080")
        monkeypatch.setattr("bot.config.load_dotenv", lambda *args, **kwargs: False)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError) as exc_info:
                create_app()
        finally:
            get_settings.cache_clear()
        assert "bot_proxy" in str(exc_info.value)
        assert "secretpw" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOT_TOKEN", raising=False)
        # .env на диске не должен влиять: тест проверяет отсутствие переменной.
        monkeypatch.setattr("bot.config.load_dotenv", lambda *args, **kwargs: False)
        get_settings.cache_clear()
        try:
            with pytest.raises(ValidationError):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_reads_backup_and_admin_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "env-token")
        monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1, 2, 2, три")
        monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "backup-key")
        monkeypatch.setenv("DELETION_LOG_KEY", "deletion-key")  # обязателен при бэкапах (§3)
        monkeypatch.setenv("BACKUP_DIR", "tmp/backups")
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.example")
        monkeypatch.setenv("S3_BUCKET", "bp-diary")
        monkeypatch.setenv("S3_ACCESS_KEY_ID", "akid")
        monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.admin_telegram_ids == (1, 2)
            assert settings.backup_encryption_key == "backup-key"
            assert settings.backup_dir == "tmp/backups"
            assert settings.s3_endpoint_url == "https://s3.example"
            assert settings.s3_bucket == "bp-diary"
            assert settings.s3_access_key_id == "akid"
            assert settings.s3_secret_access_key == "secret"
            assert settings.s3_configured is True
        finally:
            get_settings.cache_clear()


class TestParseAdminIds:
    def test_empty_returns_empty_tuple(self) -> None:
        assert parse_admin_ids("") == ()
        assert parse_admin_ids("   ") == ()
        assert parse_admin_ids(", ,") == ()

    def test_single_id(self) -> None:
        assert parse_admin_ids("123456") == (123456,)

    def test_multiple_ids_with_spaces(self) -> None:
        assert parse_admin_ids(" 1 , 22 ,333 ") == (1, 22, 333)

    def test_garbage_skipped(self) -> None:
        assert parse_admin_ids("1, abc, 2, 3.5") == (1, 2)

    def test_duplicates_removed_order_kept(self) -> None:
        assert parse_admin_ids("5, 3, 5, 3, 7") == (5, 3, 7)

    def test_negative_ids_allowed(self) -> None:
        # Telegram ID — int; знак не валидируем, только целочисленность.
        assert parse_admin_ids("-1, -1, 2") == (-1, 2)


class TestAdminIdsFromEnv:
    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "10, 20")
        assert admin_ids_from_env() == (10, 20)

    def test_missing_env_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        assert admin_ids_from_env() == ()


class TestValidateAdminIds:
    def test_string_is_parsed(self) -> None:
        assert make_settings(admin_telegram_ids="1, 2").admin_telegram_ids == (1, 2)

    def test_none_becomes_empty(self) -> None:
        assert make_settings(admin_telegram_ids=None).admin_telegram_ids == ()

    def test_sequence_converted_to_tuple(self) -> None:
        assert make_settings(admin_telegram_ids=[7, 8]).admin_telegram_ids == (7, 8)
        assert make_settings(admin_telegram_ids=(9,)).admin_telegram_ids == (9,)

    def test_default_is_empty(self) -> None:
        assert make_settings().admin_telegram_ids == ()


class TestBackupSettingsDefaults:
    def test_backup_fields_default(self) -> None:
        settings = make_settings()
        assert settings.backup_encryption_key == ""
        assert settings.backup_dir == "data/backups"

    def test_s3_fields_default(self) -> None:
        settings = make_settings()
        assert settings.s3_endpoint_url == ""
        assert settings.s3_bucket == ""
        assert settings.s3_access_key_id == ""
        assert settings.s3_secret_access_key == ""
        assert settings.s3_configured is False


class TestS3Configured:
    def _full(self, **overrides: str) -> Settings:
        data = {
            "s3_endpoint_url": "https://s3.example",
            "s3_bucket": "bp-diary",
            "s3_access_key_id": "akid",
            "s3_secret_access_key": "secret",
        }
        data.update(overrides)
        return make_settings(**data)

    def test_all_fields_set(self) -> None:
        assert self._full().s3_configured is True

    @pytest.mark.parametrize(
        "missing",
        ["s3_endpoint_url", "s3_bucket", "s3_access_key_id", "s3_secret_access_key"],
    )
    def test_partial_configuration_is_off(self, missing: str) -> None:
        assert self._full(**{missing: ""}).s3_configured is False

    def test_blank_field_counts_as_unset(self) -> None:
        assert self._full(s3_bucket="   ").s3_configured is False


class TestPrivacyAndDeletionLog:
    """`PRIVACY_URL` обязателен, `DELETION_LOG_KEY` — при бэкапах (§3)."""

    def test_privacy_default(self) -> None:
        settings = Settings(bot_token="t")
        assert settings.privacy_url == "https://bp-diary.electroman.by/privacy.html"
        assert settings.deletion_log_path == "data/deletions.log"

    @pytest.mark.parametrize(
        "url", ["", "  ", "change_me", "https://example.com", "ftp://x.org/p", "privacy.html"]
    )
    def test_privacy_invalid(self, url: str) -> None:
        with pytest.raises(ValidationError, match="PRIVACY_URL"):
            Settings(bot_token="t", privacy_url=url)

    def test_privacy_stripped(self) -> None:
        assert Settings(bot_token="t", privacy_url=" https://a.by/p.html ").privacy_url == "https://a.by/p.html"

    def test_backup_requires_deletion_key(self) -> None:
        with pytest.raises(ValidationError, match="DELETION_LOG_KEY"):
            Settings(bot_token="t", backup_encryption_key="k")
        assert Settings(bot_token="t", backup_encryption_key="k", deletion_log_key="d").deletion_log_key == "d"

    def test_env_empty_privacy_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "env-token")
        monkeypatch.setenv("PRIVACY_URL", "")
        get_settings.cache_clear()
        try:
            with pytest.raises(ValidationError):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "env-token")
        monkeypatch.setenv("PRIVACY_URL", "https://site.by/privacy.html")
        monkeypatch.setenv("DELETION_LOG_KEY", "dk")
        monkeypatch.setenv("DELETION_LOG_PATH", "x/del.log")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.privacy_url == "https://site.by/privacy.html"
            assert settings.deletion_log_key == "dk"
            assert settings.deletion_log_path == "x/del.log"
        finally:
            get_settings.cache_clear()
