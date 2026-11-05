"""Тесты конфигурации (bot/config.py)."""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from bot.config import DEFAULT_TIMEZONE, Settings, get_settings


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
